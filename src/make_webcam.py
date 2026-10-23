#!/usr/bin/env python3
"""
Cut the streamer's webcam out of the highlights, for stacked layouts (the vertical Short).

    python src/make_webcam.py --cutlist cutlist_v1.json --profile streamers/example.json --out webcam_v1.mov

Why a separate clip: free Resolve rejects the big crop + zoom a vertical timeline needs
("Filter param Crop Left / Zoom X has value out of range", probe t4), so ffmpeg crops and
upscales (lanczos, sharper than Resolve's zoom) and the FCPXML only positions the result.

Only the highlight ranges (V1) are rendered, back to back in time order -- the same list
make_fcpxml.highlights() lays out, so frame N of this file is frame N of the Short.
~64 MB per 2.5 min of highlights at 1080 wide. Video only (the gameplay clip carries the
audio). Reads the Resolve copy (`media`), so frame numbers match the timeline exactly.

Operating rule 2: refuses to overwrite an existing output.
"""

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys

import make_fcpxml as fx

VIDEO_ARGS = ["-c:v", "mpeg4", "-q:v", "3", "-g", "30", "-pix_fmt", "yuv420p"]  # free-Resolve-readable


def crop_for(webcam, src_w, src_h):
    """Profile box (pixels of a frame of size `of`) -> crop in this source's pixels, and the
    output size (`width` wide, even height)."""
    x, y, w, h = webcam["box"]
    of_w, of_h = webcam.get("of", [src_w, src_h])
    sx, sy = src_w / of_w, src_h / of_h
    cx, cy = max(0, round(x * sx)), max(0, round(y * sy))
    cw, ch = min(src_w - cx, round(w * sx)), min(src_h - cy, round(h * sy))
    if cw < 8 or ch < 8:
        raise fx.FcpxmlError("webcam box %s is outside a %dx%d source" % (webcam["box"], src_w, src_h))
    out_w = int(webcam.get("width", 1080))
    out_h = max(2, round(ch * out_w / cw / 2) * 2)
    return (cx, cy, cw, ch), (out_w, out_h)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cutlist", required=True)
    ap.add_argument("--profile", required=True, help="streamer profile with a `webcam` box")
    ap.add_argument("--out", required=True)
    ap.add_argument("--media", help="override the cutlist's media path")
    ap.add_argument("--ffmpeg", default="ffmpeg")
    ap.add_argument("--ffprobe", default="ffprobe")
    args = ap.parse_args(argv)
    # cancel sends SIGTERM to our process group: exit through `finally` so the temp parts go
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    tmp = None
    try:
        if os.path.exists(args.out):
            raise fx.FcpxmlError("refusing to overwrite %s" % args.out)
        cut, rate, segs = fx.load_cutlist(args.cutlist)
        with open(args.profile) as f:
            webcam = json.load(f).get("webcam")
        if not webcam:
            raise fx.FcpxmlError("%s has no webcam box" % args.profile)
        media = os.path.abspath(args.media) if args.media else (
            cut["media"] if os.path.isabs(cut["media"])
            else os.path.join(os.path.dirname(os.path.abspath(args.cutlist)), cut["media"]))
        info = fx.probe(media, args.ffprobe)
        hl = fx.highlights(fx.clamp(segs, fx.media_frames(info, rate)))
        if not hl:
            raise fx.FcpxmlError("no highlights (V1 segments) to cut the webcam from")
        (cx, cy, cw, ch), (ow, oh) = crop_for(webcam, info["width"], info["height"])
        # setpts: start each part at 0. (An output -r here re-timed the half-frame seek
        # offset into a duplicated first frame and a dropped last one -- measured on run2.)
        vf = "crop=%d:%d:%d:%d,scale=%d:%d:flags=lanczos,setpts=PTS-STARTPTS" % (cw, ch, cx, cy, ow, oh)
        tmp = os.path.join(os.path.dirname(os.path.abspath(args.out)), ".webcam_tmp_%d" % os.getpid())
        os.makedirs(tmp)
        parts = []
        for i, s in enumerate(hl):
            part = os.path.join(tmp, "part%05d.mov" % i)
            # Half a frame early: -ss returns the first frame at or after it, and a float
            # that rounds just past frame `a` would skip it (measured: returns exactly `a`).
            ss = max(0.0, (s["a"] - 0.5) / rate.fps)
            subprocess.run([args.ffmpeg, "-nostdin", "-v", "error", "-ss", "%.6f" % ss, "-i", media,
                            "-frames:v", str(s["b"] - s["a"]), "-an", "-vf", vf, "-fps_mode", "passthrough",
                            *VIDEO_ARGS, part], check=True)
            parts.append(part)
        listing = os.path.join(tmp, "parts.txt")
        with open(listing, "w") as f:
            f.writelines("file '%s'\n" % p for p in parts)
        partial = os.path.join(tmp, "joined.mov")  # inside tmp: a cancel never leaves it behind
        subprocess.run([args.ffmpeg, "-nostdin", "-v", "error", "-f", "concat", "-safe", "0", "-i", listing,
                        "-c", "copy", partial], check=True)
        got = fx.media_frames(fx.probe(partial, args.ffprobe), rate)
        need = hl[-1]["w"] + hl[-1]["b"] - hl[-1]["a"]
        if abs(got - need) > 1:
            raise fx.FcpxmlError("webcam clip came out %d frames, expected %d" % (got, need))
        os.replace(partial, args.out)
        print("webcam: %d highlights, %d frames, %dx%d from crop %dx%d+%d+%d -> %s"
              % (len(hl), got, ow, oh, cw, ch, cx, cy, args.out))
    except fx.FcpxmlError as e:
        sys.exit("error: %s" % e)
    except subprocess.CalledProcessError as e:
        sys.exit("error: ffmpeg failed (exit %d)" % e.returncode)
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
