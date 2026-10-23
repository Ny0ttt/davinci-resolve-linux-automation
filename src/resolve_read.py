#!/usr/bin/env python3
"""
Read the webcam / gameplay zoom and position you set in DaVinci Resolve, so a streamer
profile's Short layout can be copied from Resolve instead of typed in.

    python src/resolve_read.py [--db PATH/Project.db] [--width 1080] [--height 1920]

Free Resolve has no scripting, but it saves each project as SQLite
(~/.local/share/DaVinciResolve/Resolve Project Library/.../Projects/<name>/Project.db).
A clip's Inspector transform is in Sm2TiItem.EffectFiltersBA: a small header, then zstd,
then protobuf. Measured on a hand-tweaked Resolve project (docs/design-notes.md#unit-conversion), not assumed:

    transform filter (id 4) params:  42 = Zoom X   43 = Zoom Y   40 = Position X   41 = Position Y
    Position Y is stored as a fraction of the timeline height (0.8615 <-> 1654 px at 1920),
    Position X as a fraction of the timeline WIDTH (0.0092593 <-> 10 px at 1080; New Project 7).
    A parameter left at its default isn't stored at all.

Any other value in the filter is returned under "unknown" instead of being guessed at. The
first clip of each kind is used; if later clips of that kind differ, "differs" says how many.

Reads a COPY of the database (Resolve may be writing it). Needs the `zstd` command.
"""

import argparse
import glob
import json
import os
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile

TRANSFORM_FILTER = 4
FIELDS = {42: "zoom_x", 43: "zoom_y", 40: "x_frac", 41: "y_frac"}


class ResolveReadError(Exception):
    pass


def projects_root():
    return os.environ.get("VP_RESOLVE_PROJECTS") or os.path.expanduser(
        "~/.local/share/DaVinciResolve/Resolve Project Library/Resolve Projects/Users/guest/Projects")


def project_dbs():
    """Project.db files, newest save first."""
    dbs = glob.glob(os.path.join(projects_root(), "*", "Project.db"))
    return sorted(dbs, key=os.path.getmtime, reverse=True)


def unzstd(blob):
    i = blob.find(b"\x28\xb5\x2f\xfd")
    if i < 0:
        return blob
    p = subprocess.run(["zstd", "-dc"], input=blob[i:], capture_output=True)
    if p.returncode:
        raise ResolveReadError("zstd couldn't read a clip's settings: %s" % p.stderr.decode()[:200])
    return p.stdout


def varint(b, i):
    r = s = 0
    while True:
        x = b[i]
        i += 1
        r |= (x & 0x7F) << s
        s += 7
        if x < 0x80:
            return r, i


def parse(b):
    """Minimal protobuf -> [(field, value)]; nested messages parsed when they parse."""
    out, i = [], 0
    while i < len(b):
        key, i = varint(b, i)
        field, wt = key >> 3, key & 7
        if wt == 0:
            v, i = varint(b, i)
        elif wt == 1:
            v = struct.unpack("<d", b[i:i + 8])[0]
            i += 8
        elif wt == 2:
            n, i = varint(b, i)
            raw = b[i:i + n]
            i += n
            try:
                v = parse(raw) if raw else []
            except (IndexError, ValueError, struct.error):
                v = raw
        elif wt == 5:
            v = struct.unpack("<f", b[i:i + 4])[0]
            i += 4
        else:
            raise ValueError("wire type %d" % wt)
        out.append((field, v))
    return out


def transform_params(blob):
    """EffectFiltersBA -> {param id: double} of the transform filter."""
    if not blob:
        return {}
    params = {}
    for f, filt in parse(unzstd(blob)):
        if f != 1 or not isinstance(filt, list) or (1, TRANSFORM_FILTER) not in filt:
            continue
        for f2, slot in filt:
            if f2 != 9 or not isinstance(slot, list):
                continue
            pid = next((v for k, v in slot if k == 1), None)
            for k, val in slot:
                if k == 3 and isinstance(val, list):
                    for k2, inner in val:
                        if k2 == 1 and isinstance(inner, list):
                            for k3, d in inner:
                                if k3 == 2 and isinstance(d, float):
                                    params[pid] = d
    return params


def kind_of(path):
    base = os.path.basename(path or "")
    if base.startswith("webcam"):
        return "webcam"
    if base.startswith("clip_edit"):
        return "gameplay"
    return None


def layer_values(params, width, height):
    zoom = params.get(42, 1.0)
    out = {"zoom": round(zoom, 4), "x": round(params.get(40, 0.0) * width, 2),
           "y": round(params.get(41, 0.0) * height, 2)}
    if abs(params.get(43, zoom) - zoom) > 1e-6:
        out["zoom_y"] = round(params[43], 4)
    unknown = {str(k): v for k, v in params.items() if k not in FIELDS}
    if unknown:
        out["unknown"] = unknown
    return out


def read_layout(db=None, height=1920, width=1080):
    """Newest saved Resolve project with a webcam clip on a video track -> the first webcam
    and first gameplay clip's zoom/position in Inspector units."""
    dbs = [db] if db else project_dbs()
    if not dbs:
        raise ResolveReadError("no Resolve projects found in %s" % projects_root())
    for path in dbs:
        with tempfile.TemporaryDirectory() as tmp:
            copy = os.path.join(tmp, "Project.db")
            shutil.copy2(path, copy)
            con = sqlite3.connect(copy)
            try:
                rows = con.execute(
                    "SELECT i.Name, CAST(i.Start AS INTEGER), i.MediaFilePath, i.EffectFiltersBA, "
                    "tl.Name, tl.ModTimeInSecs FROM Sm2TiItem i "
                    "JOIN Sm2TiTrack t ON t.Sm2TiTrack_id = i.Sm2TiTrack_id "
                    "JOIN Sm2Timeline tl ON tl.Sequence = t.Sequence WHERE t.Type = 0").fetchall()
                project = (con.execute("SELECT ProjectName FROM SM_Project").fetchone() or [None])[0]
            except sqlite3.Error as e:
                raise ResolveReadError("can't read %s: %s" % (path, e))
            finally:
                con.close()
        timelines = {}
        for name, start, media, blob, tl, mod in rows:
            k = kind_of(media)
            if k:
                timelines.setdefault((mod or 0, tl), []).append((start or 0, k, blob))
        with_cam = [key for key, items in timelines.items() if any(k == "webcam" for _, k, _ in items)]
        if not with_cam:
            continue
        mod, tl = max(with_cam)
        layers, counts = {}, {}
        for start, k, blob in sorted(timelines[(mod, tl)], key=lambda x: x[0]):
            v = layer_values(transform_params(blob), width, height)
            n, differ = counts.get(k, (0, 0))
            if k not in layers:
                layers[k] = v
            elif v != layers[k]:
                differ += 1
            counts[k] = (n + 1, differ)
        if "gameplay" not in layers:
            continue
        for k, (n, differ) in counts.items():
            if differ:
                layers[k]["differs"] = {"clips": n, "different": differ}
        return {"project": project or os.path.basename(os.path.dirname(path)), "timeline": tl,
                "saved": os.path.getmtime(path), "db": path, "width": width, "height": height,
                "webcam": layers["webcam"], "gameplay": layers["gameplay"]}
    raise ResolveReadError("no saved Resolve timeline has both a webcam_*.mov and a clip_edit_*.mov clip "
                           "-- import a Short timeline, adjust it, save the project (Ctrl+S), then try again")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", help="a Project.db (default: the newest saved project)")
    ap.add_argument("--width", type=int, default=1080, help="timeline width in pixels")
    ap.add_argument("--height", type=int, default=1920, help="timeline height in pixels")
    args = ap.parse_args(argv)
    try:
        print(json.dumps(read_layout(args.db, args.height, args.width), indent=1))
    except ResolveReadError as e:
        sys.exit("error: %s" % e)


if __name__ == "__main__":
    main()
