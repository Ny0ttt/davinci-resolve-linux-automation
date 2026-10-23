#!/usr/bin/env python3
"""
Cutlist + reviewed words -> a DaVinci Resolve .drt timeline per layout, with the captions as
word chunks that pop in (docs/design-notes.md#animated-captions-via-drt, docs/design-notes.md#animated-captions-via-drt style A).

    python src/make_drt.py --cutlist cutlist_v1.json --profile streamers/example.json \\
        --words transcript_final_v1.json --layout normal=timeline_normal_v1.drt \\
        --layout short=timeline_short_v1.drt --webcam webcam_v1.mov [--name NAME]

Why .drt: FCPXML can't carry caption animation into Resolve (docs/design-notes.md#three-output-formats-because-no-single-one-is-enough
rounds 4-9); Resolve's own timeline file can (round 10). The format is undocumented, so every
timeline is built from the author's own Resolve exports in templates/drt/ (Resolve 21.1.1): their XML
skeleton, media pool entries and caption styles are kept; we write the clips, their times,
the media paths/lengths and the caption text + pop. drt_format.py has the byte formats.

  full  (normal): V1 highlights + V2 filler at their source time, A1 the audio of every
        segment, V3 captions at their source time.
  stack (short):  highlights back to back; webcam (make_webcam.py) + gameplay with the
        profile's zoom/position; A1 gameplay audio; V3 captions inside the highlights.

Captions: the reviewed words (transcript_final, voice-following times from the align stage)
cut into chunks of <= max_words, breaking on punctuation and pauses; a chunk shows from its
first word to the next chunk (or its last word + hold on silence); a chunk longer than
max_chars goes on two lines. Each pops: Zoom 0.89 -> 1.21 -> 1.0 over 0.2 s (the author's measured values).

Operating rule 2: refuses to overwrite an existing output.
"""
import argparse
import json
import os
import re
import struct
import sys
import time
import uuid
import zipfile

import drt_format as df
from make_fcpxml import FcpxmlError, clamp, highlights, load_cutlist, media_frames, probe

HERE = os.path.dirname(os.path.abspath(__file__))        # src/, for sibling modules
ROOT = os.path.dirname(HERE)                             # repo root, for shipped data
TEMPLATES = os.path.join(ROOT, "templates", "drt")
RESOLVE = "21.1.1"
TEMPLATE = {"full": "normal_%s.drt" % RESOLVE, "stack": "short_%s.drt" % RESOLVE}
POP_DONOR = "pop_%s.drt" % RESOLVE
TEMPLATE_FPS = 60.0          # the author's exports are 60 fps timelines (FrameRate in the sequence)
# per layout kind, when the profile has no captions block (the author's exports, 2026-10-15)
CAPTION_DEFAULTS = {
    "full": {"size": 60, "y": -328, "max_words": 3, "max_chars": 32},
    "stack": {"size": 51, "y": -568, "max_words": 3, "max_chars": 18},
}
COMMON = {"pause_ms": 300, "hold_ms": 500, "min_s": 0.25,
          "pop": [[0.0, 0.89], [0.1, 1.21], [0.2, 1.0]]}
PUNCT = re.compile(r"[.,!?;:…]$")


class DrtWriteError(Exception):
    pass


# ------------------------------------------------------------------ captions -> chunks

def load_words(path):
    with open(path) as f:
        data = json.load(f)
    out = []
    for e in data.get("transcription", []):
        text = e.get("text", "").strip()
        if text:
            out.append({"text": text, "a": e["offsets"]["from"] / 1000.0, "b": e["offsets"]["to"] / 1000.0})
    return out


def chunk_words(words, max_words=3, max_chars=18, pause_ms=300, hold_ms=500, min_s=0.25, **_):
    """[(start s, end s, [lines])]. Words -> phrases (end at punctuation or a pause), each
    phrase cut into the fewest even chunks of <= max_words (7 words -> 3 + 2 + 2, never
    3 + 3 + 1, so a name isn't left alone). A chunk shows until the next one starts, but not
    longer than its last word + hold over a silence; > max_chars -> two lines."""
    phrases, cur = [], []
    for w in words:
        if cur and w["a"] - cur[-1]["b"] > pause_ms / 1000.0:
            phrases.append(cur)
            cur = []
        cur.append(w)
        if PUNCT.search(w["text"]):
            phrases.append(cur)
            cur = []
    if cur:
        phrases.append(cur)
    groups = []
    for ph in phrases:
        n = -(-len(ph) // max_words)
        size, extra = divmod(len(ph), n)
        i = 0
        for k in range(n):
            j = i + size + (1 if k < extra else 0)
            groups.append(ph[i:j])
            i = j
    out = []
    for i, g in enumerate(groups):
        a = g[0]["a"]
        nxt = groups[i + 1][0]["a"] if i + 1 < len(groups) else None
        b = g[-1]["b"] + hold_ms / 1000.0
        if nxt is not None:
            b = min(b, nxt)
        b = max(b, min(a + min_s, nxt) if nxt is not None else a + min_s)
        out.append((a, b, wrap([x["text"] for x in g], max_chars)))
    return out


def wrap(texts, max_chars):
    one = " ".join(texts)
    if len(one) <= max_chars or len(texts) < 2:
        return [one]
    best = min(range(1, len(texts)), key=lambda k: abs(len(" ".join(texts[:k])) - len(" ".join(texts[k:]))))
    return [" ".join(texts[:best]), " ".join(texts[best:])]


def place_full(chunks, fps, total):
    """[(timeline frame, frames, lines)] for a layout at source time."""
    out, last = [], 0
    for a, b, lines in chunks:
        f0, f1 = max(round(a * fps), last), min(round(b * fps), total)
        if f1 - f0 >= 1:
            out.append((f0, f1 - f0, lines))
            last = f1
    return out


def place_stack(chunks, fps, hl, min_frames):
    """Chunks inside the highlights, moved to where each highlight sits in the stacked timeline."""
    out, last = [], 0
    for a, b, lines in chunks:
        f0, f1 = round(a * fps), round(b * fps)
        for s in hl:
            lo, hi = max(f0, s["a"]), min(f1, s["b"])
            if hi - lo >= min_frames:
                t0 = max(s["w"] + lo - s["a"], last)
                t1 = s["w"] + hi - s["a"]
                if t1 > t0:
                    out.append((t0, t1 - t0, lines))
                    last = t1
    return out


# ------------------------------------------------------------------ template handling

class Template:
    def __init__(self, path):
        self.path = path
        self.z = zipfile.ZipFile(path)
        self.names = self.z.namelist()
        self.seq_name = next(n for n in self.names if n.startswith("SeqContainer/"))
        self.seq = self.z.read(self.seq_name).decode("utf-8")
        self.pool = self.z.read("MediaPool/Master/MpFolder.xml").decode("utf-8")
        self.project = self.z.read("project.xml").decode("utf-8")
        head = re.search(r'DbAppVer="([^"]+)"', self.seq)
        if not head or not head.group(1).startswith(RESOLVE):
            raise DrtWriteError("template %s isn't from Resolve %s" % (path, RESOLVE))

    def tracks(self):
        """[(kind 'video'/'audio', track xml)] in file order (V1, V2, V3, A1)."""
        out = []
        for vec, kind in (("VideoTrackVec", "video"), ("AudioTrackVec", "audio")):
            body = re.search(r"<%s>(.*?)\n </%s>" % (vec, vec), self.seq, re.S).group(1)
            for m in re.finditer(r"<Sm2TiTrack DbId=.*?\n   </Sm2TiTrack>", body, re.S):
                out.append((kind, m.group(0)))
        return out


def items_of(track_xml):
    return re.findall(r"     <Element>\n      <Sm2Ti\w+ DbId=.*?\n     </Element>", track_xml, re.S)


def tag(xml, name):
    m = re.search(r"<%s>([^<]*)</%s>|<%s/>" % (name, name, name), xml)
    if not m:
        raise DrtWriteError("template item has no <%s>" % name)
    return m.group(1) or ""


def set_tag(xml, name, value):
    value = "" if value is None else str(value)
    new = "<%s>%s</%s>" % (name, value, name) if value != "" else "<%s/>" % name
    out, n = re.subn(r"<%s>[^<]*</%s>|<%s/>" % (name, name, name), lambda _: new, xml, count=1)
    if not n:
        raise DrtWriteError("template item has no <%s>" % name)
    return out


def fresh_ids(xml):
    """New DbIds for a cloned item (the item and its thumbnail)."""
    return re.sub(r'DbId="[0-9a-f-]{36}"', lambda _: 'DbId="%s"' % uuid.uuid4(), xml)


def item_fields(start, generator=False):
    """FieldsBlob of a timeline item: the minimal form Resolve itself writes
    (field 1 -> 1 -> 21 = start; video/audio also 1 -> 4 = 1)."""
    inner = df.join([(21, 0, start)])
    if generator:
        body = df.join([df.ld(1, inner), df.ld(2, b"\x00\x00\x00$x\x9ccf`d \x04\x00\x00\xb2\x00\x05")])
    else:
        body = df.join([df.ld(1, df.join([df.ld(1, inner), (4, 0, 1)])),
                        df.ld(2, b"\x00\x00\x00\x00\x00\x00\x00\x06" + b"\xff" * 8)])
    return df.pack(body, compress=False).hex()


def timemap(frames, fps):
    return "02" + struct.pack(">d", (frames - 1) / fps).hex()


# ------------------------------------------------------------------ media pool

def patch_clip_blob(hexblob, path):
    """Media pool Clip blob: folder (1), file name (2, 6), modified time (3 text, 13 microseconds)."""
    st = os.stat(path)
    proto = df.unpack(bytes.fromhex(hexblob))
    out = []
    for f, wt, v in df.split(proto):
        if f == 1 and wt == 2:
            v = os.path.dirname(os.path.abspath(path)).encode()
        elif f in (2, 6) and wt == 2:
            v = os.path.basename(path).encode()
        elif f == 3 and wt == 2:
            v = time.ctime(st.st_mtime).encode()
        elif f == 13 and wt == 0:
            v = int(st.st_mtime) * 1000000
        out.append((f, wt, v))
    return df.pack(df.join(out)).hex()


def patch_extents(hexblob, seconds):
    """The media clip's FieldsBlob: MediaExtents = QByteArray of two LE doubles (0, length s)."""
    proto = df.unpack(bytes.fromhex(hexblob))
    k = "MediaExtents".encode("utf-16-be")
    i = proto.find(k)
    if i < 0:
        return hexblob
    at = i + len(k) + 5 + 4 + 8              # QVariant type + null, QByteArray length, start
    proto = proto[:at] + struct.pack("<d", seconds) + proto[at + 8:]
    return df.pack(proto).hex()


def patch_pool_clip(pool, old_name, path, info, fps, audio_samples=None):
    """Retarget the template's media pool entry for `old_name` to `path` (length, size, name)."""
    m = re.search(r"<Sm2MpVideoClip DbId=\"[^\"]+\">\s*<FieldsBlob>[0-9a-f]*</FieldsBlob>\s*<Name>%s</Name>.*?</Sm2MpVideoClip>"
                  % re.escape(old_name), pool, re.S)
    if not m:
        raise DrtWriteError("template media pool has no %s" % old_name)
    x = m.group(0)
    frames = info["frames"]
    x = x.replace("<Name>%s</Name>" % old_name, "<Name>%s</Name>" % os.path.basename(path), 1)
    clips = list(re.finditer(r"<Clip>([0-9a-f]+)</Clip>", x))
    for c in reversed(clips):
        x = x[:c.start(1)] + patch_clip_blob(c.group(1), path) + x[c.end(1):]

    def sub_blob(x, tagname, fn):
        c = re.search(r"<%s>([0-9a-f]+)</%s>" % (tagname, tagname), x)
        return x[:c.start(1)] + fn(bytes.fromhex(c.group(1))).hex() + x[c.end(1):]
    x = sub_blob(x, "Time", lambda b: df.qmap_set(b, "NumFrames", ">i", frames))
    w, h = info["width"], info["height"]
    x = sub_blob(x, "Geometry", lambda b: df.qmap_set(df.qmap_set(b, "Resolution", ">qq", (w, h)),
                                                       "FrameSize", ">i", int(w * h * 1.5) + 8192))
    if audio_samples is not None and "<TracksBA>" in x:
        n, sr = audio_samples
        x = sub_blob(x, "TracksBA", lambda b: df.qmap_set(b, "Duration", ">q", n))
        fb = re.search(r"<FieldsBlob>([0-9a-f]+)</FieldsBlob>", x)
        if fb:
            x = x[:fb.start(1)] + patch_extents(fb.group(1), n / float(sr)) + x[fb.end(1):]
    return pool[:m.start()] + x + pool[m.end():], re.search(r'<Sm2MpVideoClip DbId="([^"]+)"', x).group(1)


def audio_samples(path, ffprobe):
    """(samples, rate) as Resolve records them: the file's duration to the millisecond x rate
    (run2: 150.004667 s -> 150.005 s -> 7200240, what Resolve wrote; the stream's own count is
    7199744)."""
    import subprocess
    p = subprocess.run([ffprobe, "-v", "error", "-select_streams", "a:0", "-show_entries",
                        "stream=sample_rate:format=duration", "-of", "json", path], capture_output=True, text=True)
    data = json.loads(p.stdout or "{}")
    s = (data.get("streams") or [{}])[0]
    if not s.get("sample_rate") or not data.get("format", {}).get("duration"):
        return None
    sr = int(s["sample_rate"])
    return int(round(round(float(data["format"]["duration"]), 3) * sr)), sr


# ------------------------------------------------------------------ captions as Text titles

class CaptionMaker:
    def __init__(self, proto_item, donor_transform, cfg, fps):
        self.item = proto_item
        efx = bytes.fromhex(tag(proto_item, "EffectFiltersBA"))
        self.proto = df.unpack(efx)
        lines, styles, self.flags = df.text_block_parts(df.get_text_block(self.proto))
        self.style = df.set_style_size(styles[0], float(cfg["size"]))
        self.y = float(cfg["y"])
        self.fps = fps
        pop = [(round(t * fps), v) for t, v in cfg["pop"]]
        self.transform = None
        self.donor = donor_transform
        self.pop = pop

    def make(self, start, dur, lines, height):
        tf = df.set_transform(self.donor, {41: self.y / height}, keys={42: self.pop, 43: self.pop})
        proto = df.set_text_block(self.proto, df.text_block(lines, self.style, self.flags))
        proto = df.replace_filter(proto, df.TRANSFORM_FILTER, tf)
        x = fresh_ids(self.item)
        x = set_tag(x, "Name", " ".join(lines)[:60].replace("&", "&amp;").replace("<", "&lt;"))
        x = set_tag(x, "Start", start)
        x = set_tag(x, "Duration", dur)
        x = set_tag(x, "FieldsBlob", item_fields(start, generator=True))
        return set_tag(x, "EffectFiltersBA", df.pack(proto).hex())


def donor_transform():
    t = Template(os.path.join(TEMPLATES, POP_DONOR))
    for m in re.finditer(r"<Sm2TiGenerator DbId=.*?<EffectFiltersBA>([0-9a-f]+)</EffectFiltersBA>", t.seq, re.S):
        tf = df.get_filter(df.unpack(bytes.fromhex(m.group(1))), df.TRANSFORM_FILTER)
        if tf and 42 in df.transform_slots(tf) and df.read_keys(df.transform_slots(tf)[42]):
            return tf
    raise DrtWriteError("pop template has no caption with Zoom keyframes")


# ------------------------------------------------------------------ building one timeline

def media_item(proto, start, dur, src_in, media_id, path, frames, fps, transform=None):
    x = fresh_ids(proto)
    x = set_tag(x, "Name", os.path.basename(path))
    x = set_tag(x, "Start", start)
    x = set_tag(x, "Duration", dur)
    x = set_tag(x, "In", src_in or None)
    x = set_tag(x, "MediaRef", media_id)
    x = set_tag(x, "MediaFilePath", os.path.abspath(path).replace("&", "&amp;"))
    x = set_tag(x, "MediaTimemapBA", timemap(frames, fps))
    x = set_tag(x, "FieldsBlob", item_fields(start))
    if transform is not None:
        x = set_tag(x, "EffectFiltersBA", transform)
    return x


def layer_transform(proto_item, layer, height, width):
    """The template item's transform with the profile's zoom / position (Inspector units)."""
    efx = tag(proto_item, "EffectFiltersBA")
    if not efx:
        raise DrtWriteError("template clip has no transform to set")
    proto = df.unpack(bytes.fromhex(efx))
    tf = df.get_filter(proto, df.TRANSFORM_FILTER)
    vals = {42: layer["zoom"], 43: layer.get("zoom_y", layer["zoom"]), 41: layer["y"] / height}
    if abs(layer.get("x", 0)) > 1e-9:
        vals[40] = layer["x"] / width
    proto = df.replace_filter(proto, df.TRANSFORM_FILTER, df.set_transform(tf, vals))
    return df.pack(proto).hex()


def build(kind, name, segs, media, webcam, layout, chunks, fps, ffprobe):
    t = Template(os.path.join(TEMPLATES, TEMPLATE[kind]))
    tracks = t.tracks()
    vids = [x for k, x in tracks if k == "video"]
    auds = [x for k, x in tracks if k == "audio"]
    if len(vids) != 3 or len(auds) != 1:
        raise DrtWriteError("template %s: expected 3 video + 1 audio tracks" % t.path)
    pool = t.pool
    samples = audio_samples(media["path"], ffprobe)
    pool, media_id = patch_pool_clip(pool, re.search(r"<Name>(clip_edit[^<]*)</Name>", pool).group(1),
                                     media["path"], media, fps, samples)
    cfg = dict(COMMON, **CAPTION_DEFAULTS[kind])
    cfg.update(layout.get("drt_captions") or {})
    gen_proto = items_of(vids[2])[0]
    captions = CaptionMaker(gen_proto, donor_transform(), cfg, fps)
    min_f = max(1, round(cfg["min_s"] * fps))
    new_items = {}
    if kind == "full":
        height, width = media["height"], media["width"]
        total = min(max(s["b"] for s in segs), media["frames"])
        v_proto, a_proto = items_of(vids[0])[0], items_of(auds[0])[0]
        for idx, track in ((0, "V1"), (1, "V2")):
            new_items[idx] = [media_item(v_proto, s["a"], s["b"] - s["a"], s["a"], media_id, media["path"],
                                         media["frames"], fps)
                              for s in sorted((s for s in segs if s["track"] == track), key=lambda s: s["a"])]
        new_items["a"] = [media_item(a_proto, s["a"], s["b"] - s["a"], s["a"], media_id, media["path"],
                                     media["frames"], fps) for s in sorted(segs, key=lambda s: s["a"])]
        placed = place_full(chunks, fps, total)
    else:
        width, height = layout["size"]
        hl = highlights(segs)
        if not hl:
            raise DrtWriteError("no highlights (V1 segments) to put in the %s layout" % name)
        total = hl[-1]["w"] + hl[-1]["b"] - hl[-1]["a"]
        if webcam is None:
            raise DrtWriteError("layout %s needs --webcam" % name)
        if webcam["frames"] < total - 1:
            raise DrtWriteError("webcam clip has %d frames, the highlights need %d -- rebuild it"
                                % (webcam["frames"], total))
        pool, cam_id = patch_pool_clip(pool, re.search(r"<Name>(webcam[^<]*)</Name>", pool).group(1),
                                       webcam["path"], webcam, fps)
        # template: V1 = webcam, V2 = gameplay (the author's Short export); the profile says which goes where
        protos = {"webcam": items_of(vids[0])[0], "gameplay": items_of(vids[1])[0]}
        order = sorted(("webcam", "gameplay"), key=lambda n: layout[n]["track"])
        if layout[order[0]]["track"] == layout[order[1]]["track"]:
            raise DrtWriteError("webcam and gameplay are both on track %d" % layout["webcam"]["track"])
        for idx, n in enumerate(order):
            src = {"webcam": (cam_id, webcam), "gameplay": (media_id, media)}[n]
            tr = layer_transform(protos[n], layout[n], height, width)
            new_items[idx] = [media_item(protos[n], s["w"], s["b"] - s["a"], s["w"] if n == "webcam" else s["a"],
                                         src[0], src[1]["path"], src[1]["frames"], fps, transform=tr) for s in hl]
        a_proto = items_of(auds[0])[0]
        new_items["a"] = [media_item(a_proto, s["w"], s["b"] - s["a"], s["a"], media_id, media["path"],
                                     media["frames"], fps) for s in hl]
        placed = place_stack(chunks, fps, hl, min_f)
    new_items[2] = [captions.make(f0, d, lines, height) for f0, d, lines in placed]

    seq = t.seq
    for idx, (kind_, xml) in enumerate(tracks):
        key = idx if kind_ == "video" else "a"
        items = "\n".join(new_items[key])
        body = re.sub(r"<Items>.*?\n    </Items>|<Items/>",
                      lambda _: "<Items>\n%s\n    </Items>" % items if items else "<Items/>", xml, count=1, flags=re.S)
        seq = seq.replace(xml, body, 1)
    pool = re.sub(r"(<Sm2MpTimelineClip DbId=\"[^\"]+\">\s*<FieldsBlob>[0-9a-f]*</FieldsBlob>\s*<Name>)[^<]*",
                  lambda m: m.group(1) + name, pool, count=1)
    pool = re.sub(r"(<Sm2Timeline DbId=\"[^\"]+\">\s*<FieldsBlob/>\s*<Name>)[^<]*",
                  lambda m: m.group(1) + name, pool, count=1)
    pool = re.sub(r"<MediaExtents>[0-9a-f]{32}</MediaExtents>",
                  "<MediaExtents>%s</MediaExtents>" % (struct.pack("<dd", 0.0, total / fps).hex()), pool, count=1)
    project = re.sub(r"<ProjectName>[^<]*</ProjectName>", "<ProjectName>%s</ProjectName>" % name, t.project, count=1)
    files = {n: t.z.read(n) for n in t.names}
    files[t.seq_name] = seq.encode("utf-8")
    files["MediaPool/Master/MpFolder.xml"] = pool.encode("utf-8")
    files["project.xml"] = project.encode("utf-8")
    return files, {"items": {str(k): len(v) for k, v in new_items.items()}, "captions": len(placed), "frames": total}


def write_new(path, files):
    if os.path.exists(path):
        raise DrtWriteError("%s already exists -- never overwritten" % path)
    tmp = path + ".part"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for n, data in files.items():
            z.writestr(n, data)
    os.replace(tmp, path)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cutlist", required=True)
    ap.add_argument("--profile", required=True)
    ap.add_argument("--words", required=True, help="transcript_final_vN.json (reviewed, aligned)")
    ap.add_argument("--layout", action="append", required=True, metavar="NAME=OUT.drt")
    ap.add_argument("--media", help="override the cutlist's media path")
    ap.add_argument("--webcam", help="make_webcam.py output (stacked layouts)")
    ap.add_argument("--name", help="timeline name prefix")
    ap.add_argument("--ffprobe", default="ffprobe")
    args = ap.parse_args(argv)
    try:
        cut, rate, segs = load_cutlist(args.cutlist)
        with open(args.profile) as f:
            profile = json.load(f)
        media_path = os.path.abspath(args.media) if args.media else (
            cut["media"] if os.path.isabs(cut["media"])
            else os.path.join(os.path.dirname(os.path.abspath(args.cutlist)), cut["media"]))
        info = probe(media_path, args.ffprobe)
        if abs(info["fps"] - rate.fps) > 0.01:
            raise DrtWriteError("media is %.3f fps but the cutlist says %.3f" % (info["fps"], rate.fps))
        if abs(rate.fps - TEMPLATE_FPS) > 0.01:
            raise DrtWriteError("the .drt templates are %g fps timelines; this media is %g fps "
                                "(export a template at that rate first)" % (TEMPLATE_FPS, rate.fps))
        media = dict(info, path=media_path, frames=media_frames(info, rate))
        segs = clamp(segs, media["frames"])
        webcam = None
        if args.webcam:
            wi = probe(args.webcam, args.ffprobe)
            webcam = dict(wi, path=os.path.abspath(args.webcam), frames=media_frames(wi, rate))
        words = load_words(args.words)
        base = args.name or os.path.splitext(os.path.basename(args.cutlist))[0]
        layouts = {k: v for k, v in (profile.get("layouts") or {}).items() if not k.startswith("_")}
        for spec in args.layout:
            lname, _, out = spec.partition("=")
            layout = layouts.get(lname)
            if not layout or not out:
                raise DrtWriteError("layout %r isn't in %s (or no output given)" % (lname, args.profile))
            kind = layout.get("kind")
            if kind not in TEMPLATE:
                raise DrtWriteError("layout %s: no .drt template for kind %r" % (lname, kind))
            cfg = dict(COMMON, **CAPTION_DEFAULTS[kind])
            cfg.update(layout.get("drt_captions") or {})
            chunks = chunk_words(words, **cfg)
            files, stats = build(kind, "%s %s" % (base, lname), segs, media, webcam, layout, chunks,
                                 rate.fps, args.ffprobe)
            write_new(out, files)
            print("%-8s %s  (%d captions, %s)" % (lname, out, stats["captions"],
                                                 ", ".join("%s: %d" % kv for kv in stats["items"].items())), flush=True)
    except (DrtWriteError, df.DrtError, FcpxmlError, OSError, KeyError) as e:
        sys.exit("error: %s" % e)


if __name__ == "__main__":
    main()
