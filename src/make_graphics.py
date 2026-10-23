#!/usr/bin/env python3
"""
Match transcript words against an icon map and write graphics.json -- the overlay
manifest that make_timeline.py --graphics places on the timeline.

    python src/make_graphics.py --transcript projects/<name>/transcript_ms.json \
                            --map icons/<set>/map.json \
                            --out projects/<name>/graphics_v1.json

Deterministic keyword lookup, no AI (docs/design-notes.md#graphics-without-a-renderer,
decision 2). Matches whole words, case-insensitive, longest trigger first, so
"left full back" wins over "left back" and "goal" never fires inside "goalkeeper".

Each icon is baked onto a full-frame transparent canvas with its position already
applied, because OTIO cannot carry scale/position that Resolve will honour -- the
PNG goes on the timeline at 100% and lands in the right place. Baked frames are
written next to the output, so a project folder stays self-contained.

Placement rules, applied in time order:
  hold      an icon stays up at least hold_seconds, not just for the ~280ms word
  cooldown  the same icon won't re-fire within cooldown_seconds of its last end
  overlap   one icon at a time: the higher priority wins, ties keep the earlier one

Needs ImageMagick (`convert`) for baking. Stdlib otherwise.
Operating rule 2 applies: refuses to overwrite --out without --force.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys

GRAVITY = {
    "top-left": "northwest",
    "top-right": "northeast",
    "bottom-left": "southwest",
    "bottom-right": "southeast",
    "top": "north",
    "lower-third": "south",
    "center": "center",
}

DEFAULTS = {
    "position": "top-right",
    "size_px": 240,
    "margin_px": 60,
    "hold_seconds": 2.5,
    "cooldown_seconds": 10.0,
    "priority": 0,
}


def norm(token):
    """Lowercase and strip surrounding punctuation, keeping inner hyphens/apostrophes."""
    return re.sub(r"^[^\w]+|[^\w]+$", "", token.lower())


def load_words(path):
    """One word per entry -- same contract as make_captions.load_words: the
    transcript must come from whisper run with -ml 1 -sow."""
    with open(path, encoding="utf-8") as f:
        t = json.load(f)
    if "transcription" not in t:
        sys.exit("error: %s is not a whisper.cpp JSON transcript" % path)
    words = []
    for seg in t["transcription"]:
        text = seg["text"].strip()
        if text and norm(text):
            words.append({"text": text, "norm": norm(text),
                          "start": seg["offsets"]["from"] / 1000.0,
                          "end": seg["offsets"]["to"] / 1000.0})
    return words


def load_map(path):
    with open(path, encoding="utf-8") as f:
        m = json.load(f)
    if "icons" not in m or not m["icons"]:
        sys.exit("error: map %s has no 'icons'" % path)
    base = os.path.dirname(os.path.abspath(path))
    defaults = dict(DEFAULTS, **m.get("defaults", {}))
    canvas = m.get("canvas", {"width": 1920, "height": 1080})

    icons = {}
    for name, spec in m["icons"].items():
        spec = dict(defaults, **spec)
        if "image" not in spec or not spec.get("triggers"):
            sys.exit("error: icon %r needs 'image' and a non-empty 'triggers' list" % name)
        if spec["position"] not in GRAVITY:
            sys.exit("error: icon %r: position must be one of %s"
                     % (name, ", ".join(GRAVITY)))
        img = spec["image"]
        spec["image"] = img if os.path.isabs(img) else os.path.join(base, img)
        if not os.path.exists(spec["image"]):
            sys.exit("error: icon %r: image not found: %s" % (name, spec["image"]))
        spec["trigger_tokens"] = [[norm(t) for t in trig.split() if norm(t)]
                                  for trig in spec["triggers"]]
        icons[name] = spec
    return icons, canvas


def find_matches(words, icons):
    """Scan left to right; at each word try every trigger, longest first. A matched
    phrase consumes its words so one mention can't fire two icons."""
    triggers = sorted(((len(toks), name, toks, trig)
                       for name, spec in icons.items()
                       for toks, trig in zip(spec["trigger_tokens"], spec["triggers"])),
                      key=lambda x: -x[0])
    matches = []
    i = 0
    while i < len(words):
        for n, name, toks, trig in triggers:
            if [w["norm"] for w in words[i:i + n]] == toks:
                matches.append({"icon": name, "trigger": trig,
                                "said": " ".join(w["text"] for w in words[i:i + n]),
                                "word_start": words[i]["start"],
                                "word_end": words[i + n - 1]["end"]})
                i += n
                break
        else:
            i += 1
    return matches


def place(matches, icons):
    """Apply hold, cooldown and one-at-a-time. Returns (placed, skipped)."""
    placed, skipped = [], []
    last_end = {}
    for m in matches:
        spec = icons[m["icon"]]
        start = m["word_start"]
        end = max(m["word_end"], start + spec["hold_seconds"])

        if m["icon"] in last_end and start < last_end[m["icon"]] + spec["cooldown_seconds"]:
            skipped.append(dict(m, reason="cooldown"))
            continue

        if placed and start < placed[-1]["end"]:
            prev = placed[-1]
            if spec["priority"] > icons[prev["icon"]]["priority"]:
                prev["end"] = start
                if prev["end"] - prev["start"] < 0.5:
                    skipped.append(dict(placed.pop(), reason="overlap: replaced by higher priority"))
            else:
                skipped.append(dict(m, reason="overlap with %s" % prev["icon"]))
                continue

        placed.append(dict(m, start=round(start, 3), end=round(end, 3)))
        last_end[m["icon"]] = end
    return placed, skipped


DIGIT_WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]


def no_digits(text):
    """Spell out digits and replace spaces.

    Resolve treats ANY digit run in a still's filename as an image-sequence frame
    number: given `icon_240px.png` it went looking for `icon_255px.png`,
    `icon_260px.png`... and showed the clip as media offline. Baked filenames must
    therefore contain no digits at all. Spelling them out (rather than stripping)
    keeps `player1` and `player2` distinct.
    """
    return re.sub(r"\d", lambda m: "-" + DIGIT_WORDS[int(m.group())] + "-",
                  text.replace(" ", "_")).replace("--", "-").strip("-")


def bake(spec, canvas, frames_dir):
    """Icon -> full-frame transparent PNG with position baked in. Cached by name."""
    stem = os.path.splitext(os.path.basename(spec["image"]))[0]
    name = no_digits("%s_%s_size%dpx" % (stem, spec["position"], spec["size_px"]))
    out = os.path.join(frames_dir, name + ".png")
    if not os.path.exists(out):
        size = spec["size_px"]
        subprocess.run(
            ["convert", "-size", "%dx%d" % (canvas["width"], canvas["height"]), "xc:none",
             "(", spec["image"], "-resize", "%dx%d" % (size, size), ")",
             "-gravity", GRAVITY[spec["position"]],
             "-geometry", "+%d+%d" % (spec["margin_px"], spec["margin_px"]),
             "-composite", "PNG32:" + out],
            check=True)
    return out


def main():
    ap = argparse.ArgumentParser(description="Match transcript words to icons -> graphics.json")
    ap.add_argument("--transcript", required=True, help="whisper.cpp JSON transcript (-ml 1 -sow)")
    ap.add_argument("--map", required=True, help="icon map JSON, e.g. icons/<set>/map.json")
    ap.add_argument("--out", required=True, help="output graphics JSON (use a NEW versioned filename)")
    ap.add_argument("--force", action="store_true", help="allow overwriting --out")
    args = ap.parse_args()

    if os.path.exists(args.out) and not args.force:
        sys.exit("refusing to overwrite %s (pass --force, or use a new versioned "
                 "filename -- see operating rule 2)" % args.out)
    if not shutil.which("convert"):
        sys.exit("error: ImageMagick `convert` not found -- needed to bake icon frames")

    words = load_words(args.transcript)
    icons, canvas = load_map(args.map)
    placed, skipped = place(find_matches(words, icons), icons)

    out_dir = os.path.dirname(os.path.abspath(args.out))
    frames_dir = os.path.join(out_dir, "graphics_frames")
    os.makedirs(frames_dir, exist_ok=True)
    for p in placed:
        # Stored relative to graphics.json so the project folder stays portable.
        p["image"] = os.path.relpath(bake(icons[p["icon"]], canvas, frames_dir), out_dir)

    manifest = {
        "_generated_by": "make_graphics.py",
        "transcript": os.path.relpath(os.path.abspath(args.transcript), out_dir),
        "map": os.path.relpath(os.path.abspath(args.map), out_dir),
        "canvas": canvas,
        "items": [{k: p[k] for k in ("icon", "image", "start", "end", "trigger", "said")}
                  for p in placed],
        "skipped": [{k: s[k] for k in ("icon", "trigger", "said", "word_start", "reason")}
                    for s in skipped],
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print("words:    %d" % len(words))
    print("matches:  %d  (placed %d, skipped %d)" % (len(placed) + len(skipped), len(placed), len(skipped)))
    for p in placed:
        print("  %7.2f-%7.2f  %-12s  said %r" % (p["start"], p["end"], p["icon"], p["said"]))
    for s in skipped:
        print("  skipped %7.2f  %-12s  said %r  (%s)" % (s["word_start"], s["icon"], s["said"], s["reason"]))
    print("\nwrote %s" % args.out)


if __name__ == "__main__":
    main()
