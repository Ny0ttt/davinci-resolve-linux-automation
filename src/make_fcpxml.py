#!/usr/bin/env python3
"""
Cutlist -> one FCPXML timeline per layout, for DaVinci Resolve (File > Import > Timeline).

    python src/make_fcpxml.py --cutlist cutlist_v1.json --profile streamers/example.json \\
        --layout normal=timeline_normal_v1.fcpxml --layout short=timeline_short_v1.fcpxml \\
        --captions captions_final_v1.srt --webcam webcam_v1.mov [--graphics graphics_v1.json] \\
        [--otio-out timeline_v1.otio]

Why FCPXML and not OTIO: OTIO can't set the timeline size or position/zoom/crop a clip in a
way free Resolve honours; FCPXML can. Every structure written here was import-tested in
free Resolve 21.1.1 first (docs/design-notes.md#three-output-formats-because-no-single-one-is-enough):

  full  (layout kind) -- the "normal" 16:9 edit, same as the OTIO one (probe p1):
        spine = V1 gap/highlight alternation, so each highlight sits at its source time;
        lane 1 = V2 filler; lane 2 = icon stills. Every spine item has start == offset (gaps
        too), so a connected item's offset is simply its absolute time.
  stack (layout kind) -- e.g. the vertical Short (probes t4c + t5c): highlights back to back;
        the webcam (pre-cut by make_webcam.py) and the gameplay stacked on V1/V2 with the
        profile's zoom/position; captions as text titles on V3.

Units: layouts are written in Resolve Inspector units (what you reads and tweaks).
FCPXML position % = Inspector px / timeline height x 100; scale = Inspector zoom. Measured
from Resolve's Project.db, not assumed.

Hard rules (each one cost a probe round):
  - integer frames end to end; a caption's end and the next one's start are snapped to
    frames BEFORE taking the duration (rounding them separately made Resolve silently drop
    titles: "Item already exists ... Cannot add item to track")
  - no two items overlap on one lane -- asserted, never left to Resolve
  - no big crop/zoom in a vertical timeline (Resolve: "Crop Left ... out of range") -- the
    webcam is cropped by ffmpeg beforehand and only positioned here
  - FCPXML <caption> is dropped by free Resolve (SRT and iTT roles) -> text titles
  - media by percent-encoded file:// URL (spaces and brackets proven); stills whose file
    name has a digit are refused (Resolve reads it as an image-sequence frame number)

Operating rule 2: refuses to overwrite an existing output.
"""

import argparse
import fractions
import json
import os
import struct
import subprocess
import sys
import urllib.request
from xml.sax.saxutils import escape, quoteattr

HERE = os.path.dirname(os.path.abspath(__file__))        # src/, for invoking make_timeline.py
VIDEO_TRACKS = ("V1", "V2")
TITLE_UID = ".../Titles.localized/Bumper:Opener.localized/Basic Title.localized/Basic Title.moti"
TITLE_POSITION_KEY = "9999/999166631/999166633/1/100/101"


class FcpxmlError(Exception):
    pass


# ---------------------------------------------------------------- time

class Rate:
    """Frame rate as an exact fraction, so 29.97 writes 1001/30000s rather than drifting."""

    def __init__(self, fps):
        self.fps = float(fps)
        if self.fps <= 0:
            raise FcpxmlError("fps must be positive, got %r" % fps)
        self.frac = fractions.Fraction(self.fps).limit_denominator(1001)
        self.frame = 1 / self.frac

    def frames(self, seconds):
        return round(float(seconds) * self.fps)

    def t(self, n):
        """n frames as FCPXML time, unreduced -- '315/30s', the form Resolve was tested with."""
        n = int(n)
        if n == 0:
            return "0s"
        return "%d/%ds" % (n * self.frame.numerator, self.frame.denominator)

    @property
    def frame_duration(self):
        return self.t(1)


# ---------------------------------------------------------------- inputs

def load_cutlist(path):
    with open(path) as f:
        cut = json.load(f)
    for key in ("media", "fps", "segments"):
        if key not in cut:
            raise FcpxmlError("cutlist is missing required key: %r" % key)
    rate = Rate(cut["fps"])
    segs = []
    for i, s in enumerate(cut["segments"]):
        track = s.get("track", "V1")
        if track not in VIDEO_TRACKS:
            raise FcpxmlError("segment %d: track must be V1 or V2, got %r" % (i, track))
        a, b = rate.frames(s["start"]), rate.frames(s["end"])
        if b <= a:
            raise FcpxmlError("segment %d: end must be after start" % i)
        segs.append({"a": a, "b": b, "track": track, "reason": s.get("reason", "")})
    for track in VIDEO_TRACKS:
        no_overlap([(s["a"], s["b"]) for s in segs if s["track"] == track], "cutlist %s" % track)
    return cut, rate, segs


def clamp(segs, frames):
    """The scorer's last segment can round a frame past the file; never reference frames
    that aren't there. make_webcam.py applies the same, so both agree on every highlight."""
    return [dict(s, b=min(s["b"], frames)) for s in segs if s["a"] < frames]


def media_frames(info, rate):
    return int(info["duration"] * rate.fps + 1e-6)


def highlights(segs):
    """V1 segments in time order, each with `w`: where it starts in the webcam file and in a
    stacked timeline (they're laid back to back). make_webcam.py renders the same list."""
    out, pos = [], 0
    for s in sorted((s for s in segs if s["track"] == "V1"), key=lambda s: s["a"]):
        out.append(dict(s, w=pos))
        pos += s["b"] - s["a"]
    return out


def no_overlap(spans, what):
    spans = sorted(spans)
    for (a0, b0), (a1, b1) in zip(spans, spans[1:]):
        if a1 < b0:
            raise FcpxmlError("%s: items overlap (frames %d-%d and %d-%d) -- Resolve would drop or "
                              "trim one" % (what, a0, b0, a1, b1))


def parse_srt(path, rate):
    """SRT -> [(first_frame, end_frame, text)], edges snapped to frames first, then made
    non-overlapping (an end past the next start is pulled back to it)."""
    def sec(t):
        t = t.strip().replace(".", ",")
        hms, ms = t.split(",")
        h, m, s = (int(x) for x in hms.split(":"))
        return h * 3600 + m * 60 + s + int(ms) / 1000
    cues = []
    with open(path, encoding="utf-8-sig") as f:
        blocks = f.read().replace("\r\n", "\n").strip().split("\n\n")
    for block in blocks:
        lines = block.strip().split("\n")
        arrow = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if arrow is None:
            continue
        a, b = lines[arrow].split("-->")
        text = "\n".join(ln for ln in lines[arrow + 1:]).strip()
        if text:
            cues.append([rate.frames(sec(a)), rate.frames(sec(b.split()[0])), text])
    cues.sort(key=lambda c: c[0])
    for c, nxt in zip(cues, cues[1:]):
        c[1] = min(c[1], nxt[0])
    return [tuple(c) for c in cues if c[1] > c[0]]


def png_size(path):
    with open(path, "rb") as f:
        head = f.read(24)
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        raise FcpxmlError("not a PNG: %s" % path)
    return struct.unpack(">II", head[16:24])


def load_stills(path, rate):
    with open(path) as f:
        g = json.load(f)
    base = os.path.dirname(os.path.abspath(path))
    items = []
    for i, it in enumerate(g.get("items", [])):
        image = it["image"] if os.path.isabs(it["image"]) else os.path.join(base, it["image"])
        if not os.path.exists(image):
            raise FcpxmlError("graphics item %d: image not found: %s" % (i, image))
        if any(ch.isdigit() for ch in os.path.basename(image)):
            raise FcpxmlError("graphics item %d: image filename contains a digit, which Resolve "
                              "treats as an image sequence (media offline): %s" % (i, image))
        a, b = rate.frames(it["start"]), rate.frames(it["end"])
        if b <= a:
            raise FcpxmlError("graphics item %d: end must be after start" % i)
        items.append({"a": a, "b": b, "image": image})
    no_overlap([(x["a"], x["b"]) for x in items], "graphics")
    return sorted(items, key=lambda x: x["a"])


def probe(path, ffprobe="ffprobe"):
    p = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=width,height,r_frame_rate:format=duration", "-of", "json", path],
                       capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if p.returncode != 0:
        raise FcpxmlError("ffprobe failed on %s: %s" % (path, p.stderr.strip()[:300]))
    info = json.loads(p.stdout)
    st = (info.get("streams") or [{}])[0]
    if not st.get("width"):
        raise FcpxmlError("no video stream in %s" % path)
    num, den = st["r_frame_rate"].split("/")
    return {"width": st["width"], "height": st["height"], "fps": float(num) / float(den),
            "duration": float(info.get("format", {}).get("duration") or 0)}


def file_url(path):
    return "file://" + urllib.request.pathname2url(os.path.abspath(path))


# ---------------------------------------------------------------- xml pieces

def num(v):
    s = "%.4f" % v
    return s.rstrip("0").rstrip(".") if "." in s else s


def transform(layer, seq_h):
    """Resolve Inspector units -> FCPXML adjust-transform."""
    x = num(layer.get("x", 0) / seq_h * 100)
    y = num(layer.get("y", 0) / seq_h * 100)
    z = num(layer.get("zoom", 1))
    return '<adjust-transform position="%s %s" scale="%s %s"/>' % (x, y, z, z)


def a(**attrs):
    return "".join(" %s=%s" % (k.rstrip("_"), quoteattr(str(v))) for k, v in attrs.items() if v is not None)


class Doc:
    def __init__(self, rate):
        self.rate = rate
        self.resources = []
        self._ids = 0

    def rid(self, prefix):
        self._ids += 1
        return "%s%d" % (prefix, self._ids)

    def fmt(self, w, h, rate=True):
        rid = self.rid("f")
        self.resources.append("<format%s/>" % a(id=rid, frameDuration=self.rate.frame_duration if rate else None,
                                                  width=w, height=h, colorSpace="1-1-1 (Rec. 709)"))
        return rid

    def asset(self, path, w, h, frames, audio, still=False):
        rid = self.rid("r")
        f = self.fmt(w, h, rate=not still)
        dur = "0s" if still else self.rate.t(frames)
        extra = dict(hasAudio="1", audioSources="1", audioChannels="2", audioRate="48000") if audio \
            else dict(hasAudio="0")
        self.resources.append('<asset%s><media-rep kind="original-media" src=%s/></asset>' % (
            a(id=rid, name=os.path.basename(path), start="0s", duration=dur, hasVideo="1",
              format=f, **extra), quoteattr(file_url(path))))
        return rid

    def title_effect(self):
        rid = self.rid("e")
        self.resources.append("<effect%s/>" % a(id=rid, name="Basic Title", uid=TITLE_UID))
        return rid

    def render(self, name, seq_w, seq_h, total, spine):
        seq_fmt = self.fmt(seq_w, seq_h)
        return """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<!-- written by make_fcpxml.py (video-editing-pipeline) -->
<fcpxml version="1.10">
  <resources>
    %s
  </resources>
  <library>
    <event%s>
      <project%s>
        <sequence%s>
          <spine>
            %s
          </spine>
        </sequence>
      </project>
    </event>
  </library>
</fcpxml>
""" % ("\n    ".join(self.resources), a(name="video-editing-pipeline"), a(name=name),
       a(format=seq_fmt, duration=self.rate.t(total), tcStart="0s", tcFormat="NDF",
         audioLayout="stereo", audioRate="48k"),
       "\n            ".join(spine))


def clip_xml(rate, ref, name, offset, start, dur, lane=None, inner=""):
    return "<asset-clip%s>%s</asset-clip>" % (
        a(ref=ref, name=name, lane=lane, offset=rate.t(offset), start=rate.t(start),
          duration=rate.t(dur), tcFormat="NDF"), inner)


def title_xml(rate, effect, n, offset, dur, text, style, lane):
    pos = style.get("position", [0, 0])
    color = " ".join(num(c) for c in style.get("color", [1, 1, 1, 1]))
    sid = "ts%d" % n
    return ("<title%s>" % a(ref=effect, lane=lane, offset=rate.t(offset), name="caption %d" % n,
                            duration=rate.t(dur))
            + '<param name="Position" key="%s" value="%s %s"/>' % (TITLE_POSITION_KEY, num(pos[0]), num(pos[1]))
            + '<text><text-style ref="%s">%s</text-style></text>' % (sid, escape(text))
            + "<text-style-def%s><text-style%s/></text-style-def>" % (
                a(id=sid), a(font=style.get("font", "Helvetica"), fontSize=style.get("size", 60),
                             fontFace=style.get("face", "Bold"), fontColor=color, alignment="center"))
            + "</title>")


# ---------------------------------------------------------------- layouts

def build_full(doc, name, segs, media, stills, total):
    """The normal edit: V1 highlights at their source time, V2 filler, V3 stills."""
    rate = doc.rate
    m = doc.asset(media["path"], media["width"], media["height"], media["frames"], audio=True)
    spine, pos = [], 0
    for s in sorted((s for s in segs if s["track"] == "V1"), key=lambda s: s["a"]):
        if s["a"] > pos:
            spine.append(["gap", pos, s["a"], None])
        spine.append(["clip", s["a"], s["b"], s])
        pos = s["b"]
    if total > pos:
        spine.append(["gap", pos, total, None])
    kids = [[] for _ in spine]

    def attach(start, xml):
        for i, (_, s0, s1, _) in enumerate(spine):
            if s0 <= start < s1:
                kids[i].append(xml)
                return
        raise FcpxmlError("nothing on the timeline at frame %d to attach to" % start)

    for s in sorted((s for s in segs if s["track"] == "V2"), key=lambda s: s["a"]):
        attach(s["a"], clip_xml(rate, m, "filler %.2f" % (s["a"] / rate.fps), s["a"], s["a"],
                                s["b"] - s["a"], lane=1))
    for it in stills:
        w, h = png_size(it["image"])
        ref = doc.asset(it["image"], w, h, 0, audio=False, still=True)
        attach(it["a"], "<video%s/>" % a(ref=ref, name=os.path.basename(it["image"]), lane=2,
                                          offset=rate.t(it["a"]), start="0s", duration=rate.t(it["b"] - it["a"])))
    body = []
    for (kind, s0, s1, seg), k in zip(spine, kids):
        if kind == "gap":
            body.append("<gap%s>%s</gap>" % (a(name="Gap", offset=rate.t(s0), start=rate.t(s0),
                                                  duration=rate.t(s1 - s0)), "".join(k)))
        else:
            body.append(clip_xml(rate, m, "highlight %.2f" % (s0 / rate.fps), s0, s0, s1 - s0, inner="".join(k)))
    return doc.render(name, media["width"], media["height"], total, body)


def build_stack(doc, name, hl, media, webcam, layout, cues):
    """Highlights back to back; webcam + gameplay stacked by `track`; captions as titles."""
    rate = doc.rate
    seq_w, seq_h = layout["size"]
    if not hl:
        raise FcpxmlError("no highlights (V1 segments) to put in the %s layout" % name)
    need = hl[-1]["w"] + hl[-1]["b"] - hl[-1]["a"]
    if webcam["frames"] < need - 1:
        raise FcpxmlError("webcam clip has %d frames, the highlights need %d -- rebuild it"
                          % (webcam["frames"], need))
    g = doc.asset(media["path"], media["width"], media["height"], media["frames"], audio=True)
    w = doc.asset(webcam["path"], webcam["width"], webcam["height"], webcam["frames"], audio=False)
    layers = sorted([("webcam", w, layout["webcam"]), ("gameplay", g, layout["gameplay"])],
                    key=lambda x: x[2]["track"])
    if layers[0][2]["track"] == layers[1][2]["track"]:
        raise FcpxmlError("webcam and gameplay are both on track %d" % layers[0][2]["track"])
    style = layout.get("captions") or {}
    effect = doc.title_effect() if cues else None
    min_f = max(1, rate.frames(style.get("min_s", 0.2)))
    body, tpos, n = [], 0, 0
    for i, s in enumerate(hl, 1):
        d = s["b"] - s["a"]
        starts = {"webcam": s["w"], "gameplay": s["a"]}
        (bn, bref, blay), (tn, tref, tlay) = layers
        spine_start = starts[bn]
        inner = transform(blay, seq_h)
        inner += clip_xml(rate, tref, "%s %d" % (tn, i), spine_start, starts[tn], d, lane=1,
                          inner=transform(tlay, seq_h))
        for c0, c1, text in cues:
            lo, hi = max(c0, s["a"]), min(c1, s["b"])
            if hi - lo >= min_f:
                n += 1
                inner += title_xml(rate, effect, n, spine_start + lo - s["a"], hi - lo, text, style, lane=2)
        body.append(clip_xml(rate, bref, "%s %d" % (bn, i), tpos, spine_start, d, inner=inner))
        tpos += d
    return doc.render(name, seq_w, seq_h, tpos, body)


# ---------------------------------------------------------------- checks on what we wrote

def check_lanes(xml_text, rate):
    """Parse back what we wrote: every lane, at timeline level, free of overlaps."""
    import xml.etree.ElementTree as ET
    root = ET.fromstring(xml_text)
    spine = root.find(".//sequence/spine")

    def t(v):
        v = v[:-1]
        if "/" in v:
            n, d = v.split("/")
            return fractions.Fraction(int(n), int(d))
        return fractions.Fraction(v)
    lanes = {}
    for item in spine:
        off, start = t(item.get("offset")), t(item.get("start", "0s"))
        lanes.setdefault(0, []).append((off, off + t(item.get("duration"))))
        for child in item:
            if child.get("lane"):
                c_off = off + t(child.get("offset")) - start
                lanes.setdefault(int(child.get("lane")), []).append((c_off, c_off + t(child.get("duration"))))
    for lane, spans in lanes.items():
        f = [(round(x * rate.frac), round(y * rate.frac)) for x, y in spans]
        no_overlap(f, "lane %d" % lane)
    return {lane: len(spans) for lane, spans in lanes.items()}


# ---------------------------------------------------------------- main

def write_new(path, text):
    if os.path.exists(path):
        raise FcpxmlError("refusing to overwrite %s (use a new versioned filename)" % path)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cutlist", required=True)
    ap.add_argument("--profile", required=True, help="streamer profile (layouts + webcam)")
    ap.add_argument("--layout", action="append", default=[], metavar="NAME=OUT.fcpxml")
    ap.add_argument("--media", help="override the cutlist's media path")
    ap.add_argument("--captions", help="SRT -> text titles on stacked layouts")
    ap.add_argument("--webcam", help="make_webcam.py output (stacked layouts)")
    ap.add_argument("--graphics", help="graphics JSON -> stills on V3 (full layouts)")
    ap.add_argument("--otio-out", help="also write the OTIO timeline (backup) via make_timeline.py")
    ap.add_argument("--name", help="timeline name prefix")
    ap.add_argument("--ffprobe", default="ffprobe")
    args = ap.parse_args(argv)
    if not args.layout and not args.otio_out:
        ap.error("nothing to write: give --layout and/or --otio-out")
    try:
        cut, rate, segs = load_cutlist(args.cutlist)
        with open(args.profile) as f:
            profile = json.load(f)
        media_path = os.path.abspath(args.media) if args.media else (
            cut["media"] if os.path.isabs(cut["media"])
            else os.path.join(os.path.dirname(os.path.abspath(args.cutlist)), cut["media"]))
        if not os.path.exists(media_path):
            raise FcpxmlError("media not found: %s" % media_path)
        info = probe(media_path, args.ffprobe)
        if abs(info["fps"] - rate.fps) > 0.01:
            raise FcpxmlError("media is %.3f fps but the cutlist says %.3f" % (info["fps"], rate.fps))
        media = dict(info, path=media_path, frames=media_frames(info, rate))
        segs = clamp(segs, media["frames"])
        stills = load_stills(args.graphics, rate) if args.graphics else []
        cues = parse_srt(args.captions, rate) if args.captions else []
        base = args.name or os.path.splitext(os.path.basename(args.cutlist))[0]
        wanted = []
        for spec in args.layout:
            name, _, out = spec.partition("=")
            layout = {k: v for k, v in (profile.get("layouts") or {}).items() if not k.startswith("_")}.get(name)
            if not layout or not out:
                raise FcpxmlError("layout %r isn't in %s (or no output given)" % (name, args.profile))
            wanted.append((name, layout, out))
        for name, layout, out in wanted:
            doc = Doc(rate)
            title = "%s %s" % (base, name)
            if layout["kind"] == "full":
                total = min(max([s["b"] for s in segs] + [x["b"] for x in stills]), max(media["frames"], 1))
                text = build_full(doc, title, segs, media, stills, total)
            elif layout["kind"] == "stack":
                if not args.webcam:
                    raise FcpxmlError("layout %s needs --webcam (make_webcam.py)" % name)
                wi = probe(args.webcam, args.ffprobe)
                webcam = dict(wi, path=os.path.abspath(args.webcam), frames=media_frames(wi, rate))
                text = build_stack(doc, title, highlights(segs), media, webcam, layout, cues)
            else:
                raise FcpxmlError("layout %s: unknown kind %r" % (name, layout.get("kind")))
            lanes = check_lanes(text, rate)
            write_new(out, text)
            print("%-8s %s  (%s)" % (name, out, ", ".join("lane %d: %d" % kv for kv in sorted(lanes.items()))),
                  flush=True)
        if args.otio_out:
            cmd = [sys.executable, os.path.join(HERE, "make_timeline.py"), "--cutlist", args.cutlist,
                   "--out", args.otio_out, "--name", base]
            if args.media:
                cmd += ["--media", args.media]
            if args.graphics:
                cmd += ["--graphics", args.graphics]
            p = subprocess.run(cmd, stdin=subprocess.DEVNULL)
            if p.returncode:
                raise FcpxmlError("make_timeline.py (OTIO backup) failed with exit %d" % p.returncode)
    except FcpxmlError as e:
        sys.exit("error: %s" % e)


if __name__ == "__main__":
    main()
