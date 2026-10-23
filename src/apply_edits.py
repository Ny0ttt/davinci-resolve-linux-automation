#!/usr/bin/env python3
"""
Apply a reviewer's by-ear edits to a whisper transcript, without disturbing word timing.

    python src/apply_edits.py --transcript projects/<name>/transcript_aligned_v1.json \
                          --human-edits projects/<name>/review_edits_v1.json \
                          --profile profiles/<name>.json \
                          --out projects/<name>/transcript_final_v1.json

The transcript is never rewritten wholesale. The edit list names contiguous word spans
({first, last, original, text}); this script validates each against the words actually at
those numbers and applies it. Every word an edit doesn't touch keeps its exact whisper
timestamp. Inside an edited span, a same-length replacement inherits the old timings
one-to-one; a different-length one shares the span's original start-end out by character
length. The span's outer edges never move. Empty text deletes the words.
Design and rationale: docs/design-notes.md#correction-as-a-separate-reviewable-stage

Edits come from the Review screen via run_pipeline.py's review stage. There is no model in
this path and no network call.

Output is plain whisper.cpp JSON, so make_captions.py and make_graphics.py read it
unchanged. A <out>.corrections.json log lists every applied edit and every rejected one
with why.

Every run ends by building the .srt from the edited transcript (make_captions.py,
--profile). --captions-only rebuilds just the .srt from an existing --out.

Operating rule 2 applies: refuses to overwrite --out without --force.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))        # src/, for invoking make_captions.py


def captions_path_for(out):
    d, name = os.path.split(out)
    stem = os.path.splitext(name)[0]
    if stem.startswith("transcript"):
        stem = "captions" + stem[len("transcript"):]
    else:
        stem += "_captions"
    return os.path.join(d, stem + ".srt")


def file_sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def check_writable(paths, force):
    for p in paths:
        if os.path.exists(p) and not force:
            sys.exit("refusing to overwrite %s (pass --force, or use a new versioned "
                     "filename -- see operating rule 2)" % p)


def make_srt(transcript, profile, out):
    """Captions from the edited transcript, via make_captions.py (always run)."""
    cmd = [sys.executable, os.path.join(HERE, "make_captions.py"), "--transcript", transcript,
           "--profile", profile, "--out", out, "--force"]  # clash already checked by caller
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit("error: make_captions.py failed -- %s exists but has NO .srt:\n%s%s"
                 % (transcript, proc.stdout, proc.stderr))
    print(proc.stdout.rstrip())
    print("wrote %s" % out)


def norm(text):
    return re.sub(r"[^\w]+", " ", text.lower()).strip()


def fmt_ts(ms):
    ms = int(round(ms))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


def load_transcript(path):
    with open(path, encoding="utf-8") as f:
        t = json.load(f)
    if "transcription" not in t:
        sys.exit("error: %s is not a whisper.cpp JSON transcript" % path)
    # Same word definition as make_captions.load_words: one non-empty entry per word
    # (requires whisper run with -ml 1 -sow).
    words = [{"text": seg["text"].strip(),
              "start": seg["offsets"]["from"], "end": seg["offsets"]["to"]}
             for seg in t["transcription"] if seg["text"].strip()]
    if not words:
        sys.exit("error: %s contains no words" % path)
    return t, words


def validate(edit, words):
    """The reviewer's own fixes (Review screen): only checks that the words are where they saw
    them. No span/growth limits and deletions allowed (audit F-15, his decision 4)."""
    first, last = edit["first"], edit["last"]
    if not (0 <= first <= last < len(words)):
        return "indices %d-%d outside the transcript's words 0-%d" % (first, last, len(words) - 1)
    actual = " ".join(w["text"] for w in words[first:last + 1])
    if norm(edit["original"]) != norm(actual):
        return "'original' %r does not match words %d-%d %r" % (edit["original"], first, last, actual)
    return None


def retime(span_words, new_tokens):
    """Timings for the replacement tokens. Outer edges of the span never move."""
    if len(new_tokens) == len(span_words):
        return [(w["start"], w["end"]) for w in span_words]
    start, end = span_words[0]["start"], span_words[-1]["end"]
    weights = [max(1, len(t)) for t in new_tokens]
    total, t, out = sum(weights), start, []
    for i, wgt in enumerate(weights):
        t_end = end if i == len(weights) - 1 else start + (end - start) * sum(weights[:i + 1]) / total
        out.append((round(t), round(t_end)))
        t = t_end
    return out


def load_edits(path, transcript):
    """The review stage's edit list, checked against the transcript it was made for."""
    with open(path, encoding="utf-8") as f:
        reviewed = json.load(f)
    if reviewed.get("source_sha256") != file_sha256(transcript):
        sys.exit("--human-edits: %s was made for a different transcript than %s -- "
                 "word numbers won't line up" % (path, transcript))
    return reviewed["edits"]


def main():
    ap = argparse.ArgumentParser(description="Apply reviewed edits to a transcript, keeping word timing.")
    ap.add_argument("--transcript", required=True, help="whisper.cpp JSON transcript (-ml 1 -sow)")
    ap.add_argument("--out", required=True, help="edited transcript JSON (use a NEW versioned filename)")
    ap.add_argument("--human-edits", metavar="EDITS_JSON",
                    help="reviewed edit list (from run_pipeline.py's review stage): "
                         "{source_sha256, edits:[{first,last,original,text,by}]}; empty text "
                         "deletes the words. Omit to copy the transcript through unchanged.")
    ap.add_argument("--profile", help="caption profile for the .srt (required; the "
                    "orchestrator passes the streamer profile's caption_profile)")
    ap.add_argument("--captions-out", help="caption file (default: derived from --out, "
                    "transcript_final_vN.json -> captions_final_vN.srt)")
    ap.add_argument("--captions-only", action="store_true",
                    help="skip the edits; only build the .srt from an existing --out")
    ap.add_argument("--force", action="store_true", help="allow overwriting --out")
    args = ap.parse_args()

    # Design decision: a run always ends in a .srt built from the edited transcript --
    # otherwise the newest captions silently keep the unedited words.
    captions_out = args.captions_out or captions_path_for(args.out)
    log_path = os.path.splitext(args.out)[0] + ".corrections.json"
    if not args.profile:
        sys.exit("--profile is required: give a caption profile from profiles/ "
                 "(the orchestrator passes the streamer profile's caption_profile)")
    if not os.path.exists(args.profile):
        sys.exit("caption profile not found: %s" % args.profile)
    if args.captions_only:
        if not os.path.exists(args.out):
            sys.exit("--captions-only: %s does not exist" % args.out)
        check_writable([captions_out], args.force)
        make_srt(args.out, args.profile, captions_out)
        return
    check_writable([args.out, log_path, captions_out], args.force)

    original, words = load_transcript(args.transcript)

    accepted, rejected = [], []
    if args.human_edits:
        edits = load_edits(args.human_edits, args.transcript)
        print("applying %d reviewed edits from %s" % (len(edits), args.human_edits))
        for e in edits:
            why = validate(e, words)
            (rejected.append(dict(e, rejected_because=why)) if why else accepted.append(e))

    # One edit per word: if two accepted edits overlap, keep the earlier one.
    accepted.sort(key=lambda e: e["first"])
    kept = []
    for e in accepted:
        if kept and e["first"] <= kept[-1]["last"]:
            rejected.append(dict(e, rejected_because="overlaps the edit at words %d-%d"
                                 % (kept[-1]["first"], kept[-1]["last"])))
        else:
            kept.append(e)

    by_first = {e["first"]: e for e in kept}
    entries, applied, i = [], [], 0
    while i < len(words):
        e = by_first.get(i)
        if e is None:
            w = words[i]
            entries.append({"timestamps": {"from": fmt_ts(w["start"]), "to": fmt_ts(w["end"])},
                            "offsets": {"from": w["start"], "to": w["end"]},
                            "text": " " + w["text"]})
            i += 1
            continue
        span = words[e["first"]:e["last"] + 1]
        tokens = e["text"].split()
        for tok, (s, t) in zip(tokens, retime(span, tokens)):
            entries.append({"timestamps": {"from": fmt_ts(s), "to": fmt_ts(t)},
                            "offsets": {"from": s, "to": t},
                            "text": " " + tok, "corrected": True})
        applied.append({"words": "%d-%d" % (e["first"], e["last"]),
                        "time": "%.2f-%.2f" % (span[0]["start"] / 1000, span[-1]["end"] / 1000),
                        "before": " ".join(w["text"] for w in span),
                        "after": e["text"], "reason": e.get("reason", ""),
                        **({"by": e["by"]} if "by" in e else {})})
        i = e["last"] + 1

    edited = {k: v for k, v in original.items() if k != "transcription"}
    edited["edits"] = {
        "by": "apply_edits.py",
        "source": os.path.basename(args.transcript),
        "edits_applied": len(applied),
        "log": os.path.basename(log_path),
        "captions": os.path.basename(captions_out),
    }
    edited["transcription"] = entries
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(edited, f, indent=1, ensure_ascii=False)
        f.write("\n")

    log = {"source": os.path.abspath(args.transcript),
           "source_sha256": file_sha256(args.transcript),
           "human_edits_from": args.human_edits,
           "captions": {"file": captions_out, "profile": args.profile}, "applied": applied,
           "rejected": rejected}
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(log, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print("\nwords in: %d   words out: %d" % (len(words), len(entries)))
    print("applied %d   rejected %d" % (len(applied), len(rejected)))
    for a in applied:
        print("  %-13s %-32s -> %s" % (a["time"], a["before"], a["after"]))
    print("\nwrote %s\nwrote %s" % (args.out, log_path))
    make_srt(args.out, args.profile, captions_out)


if __name__ == "__main__":
    main()
