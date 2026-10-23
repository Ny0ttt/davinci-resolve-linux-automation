"""Generate caption files from a corrected whisper.cpp transcript, driven by a tuning profile.

This is the single entry point for the caption stage. It replaces the ~dozen
one-off generate_captions_*.py variants that accumulated during tuning; the logic
here is the exact pipeline validated as "best" against a manual reference edit
(see profiles/*.json for the measured parameters and their provenance).

Usage:
    python src/make_captions.py --transcript projects/<name>/transcript_corrected.json \
                            --profile profiles/example_tutorial.json \
                            --out projects/<name>/captions_v1.srt

    # WebVTT instead of SubRip:
    python src/make_captions.py ... --out projects/<name>/captions_v1.vtt --format vtt

Stdlib only -- runs on any Python 3.9+, no virtualenv needed.

Order of operations matters and is not arbitrary:

  1. Merge rapid repeated words        (on ORIGINAL timestamps)
  2. Group words into lines            (on ORIGINAL timestamps)
  3. Apply the per-line duration ceiling
  4. Apply the measured timing bias    (on FINAL line boundaries only)
  5. Spread collapsed lines            (whisper gave several lines one timestamp; profile
                                        post_processing.spread_collapsed, default on)
  6. Clamp overlaps

Steps 1 and 2 must run on unshifted timestamps. The bias correction shifts start
and end by different amounts, so applying it earlier distorts the inter-word gaps
that grouping depends on, and segmentation breaks.
"""

import argparse
import json
import re
import sys
from pathlib import Path


def load_words(transcript_path):
    """Flatten a whisper.cpp JSON transcript into a list of word dicts.

    Note: this reads transcript["transcription"] -- which is whisper's *segment*
    list, not a word list. It yields one word per entry only because the
    transcribe stage runs whisper with `-ml 1 -sow` (max segment length 1,
    split-on-word), which makes every segment exactly one word. If a transcript
    was produced without those flags, entries will be whole phrases and the
    grouping below will produce garbage. Offsets are in milliseconds.
    """
    with open(transcript_path, encoding="utf-8") as f:
        transcript = json.load(f)

    if "transcription" not in transcript:
        sys.exit(f"error: {transcript_path} has no 'transcription' key -- not a whisper.cpp JSON transcript?")

    words = []
    for seg in transcript["transcription"]:
        text = seg["text"].strip()
        if text:
            words.append({
                "text": text,
                "start": seg["offsets"]["from"],
                "end": seg["offsets"]["to"],
            })

    if not words:
        sys.exit(f"error: {transcript_path} contained no non-empty segments")
    return words


def normalize(token):
    """Strip trailing sentence punctuation and case, for repeated-word comparison."""
    return re.sub(r"[.,!?]+$", "", token).lower()


def merge_repeats(raw_words, merge_gap_ms):
    """Collapse runs of the same word spoken in quick succession into one unit.

    Whisper's per-word timestamps are unreliable on rapid repeats ("cancel cancel
    cancel"), so a run is treated as a single caption unit spanning the whole run.
    Units produced this way are marked `merged`, which also forces a line break
    after them during grouping.
    """
    words = []
    i = 0
    while i < len(raw_words):
        run = [raw_words[i]]
        j = i + 1
        while j < len(raw_words):
            same_word = normalize(raw_words[j]["text"]) == normalize(raw_words[j - 1]["text"])
            gap = raw_words[j]["start"] - raw_words[j - 1]["end"]
            if same_word and gap < merge_gap_ms:
                run.append(raw_words[j])
                j += 1
            else:
                break
        if len(run) > 1:
            words.append({
                "text": " ".join(w["text"] for w in run),
                "start": run[0]["start"],
                "end": run[-1]["end"],
                "merged": True,
            })
            i = j
        else:
            words.append(raw_words[i])
            i += 1
    return words


def group_lines(words, grouping, ceiling):
    """Break the word stream into caption lines and cap each line's duration.

    A line ends at whichever comes first: sentence-ending punctuation, a pause
    longer than pause_threshold_ms, the char/word cap, a merged repeat run, or
    the end of the transcript.

    The duration ceiling caps how long a line may linger based on its word count.
    It is applied UNCONDITIONALLY -- there is deliberately no "back off if this
    creates a dead gap" exception. An earlier version considered one; the
    unconditional form is what was validated as best against the manual reference,
    even though it can occasionally leave a visible gap where whisper's timestamps
    are pathological (e.g. garbled URL-reading speech). See the duration_ceiling
    note in the profile JSON.
    """
    pause_threshold_ms = grouping["pause_threshold_ms"]
    max_chars = grouping["max_chars"]
    max_words = grouping["max_words"]
    break_on_punct = grouping.get("break_on_sentence_punctuation", True)

    per_word = ceiling["per_word_seconds"]
    base = ceiling["base_seconds"]

    lines = []
    current = []
    for i, w in enumerate(words):
        current.append(w)
        line_text = " ".join(x["text"] for x in current)

        ends_sentence = break_on_punct and w["text"].endswith((".", "!", "?"))
        next_gap = (words[i + 1]["start"] - w["end"]) if i + 1 < len(words) else float("inf")
        # Caps are checked after appending, so a line may exceed max_chars slightly.
        # This is the validated behaviour -- do not "fix" it without re-measuring.
        too_long = len(line_text) >= max_chars or len(current) >= max_words
        is_last = i == len(words) - 1

        if ends_sentence or next_gap >= pause_threshold_ms or too_long or w.get("merged") or is_last:
            start = current[0]["start"] / 1000.0
            end = current[-1]["end"] / 1000.0
            n_words = sum(len(x["text"].split()) for x in current)
            cap = start + n_words * per_word + base
            if end > cap:
                end = cap
            lines.append({"start": start, "end": end, "text": line_text})
            current = []

    return lines


def apply_bias(lines, bias):
    """Shift finished line boundaries by the measured whisper timing offset.

    Whisper's timestamps run systematically early or late depending on voice and
    content type; these shifts are a measured median offset against a manual
    reference edit, not a guess. They are profile-specific -- do not carry them
    to a different streamer or content type without re-measuring.
    """
    start_shift = bias["start_shift_ms"] / 1000.0
    end_shift = bias["end_shift_ms"] / 1000.0
    for line in lines:
        # Profile stores these as negative values meaning "pull earlier".
        line["start"] = max(0.0, line["start"] + start_shift)
        line["end"] = max(line["start"] + 0.05, line["end"] + end_shift)
    return lines


def spread_collapsed(lines, min_s=0.5):
    """Lines that START at the same moment share the time whisper did give them.

    Whisper sometimes stamps a whole stretch of words with one timestamp (seen in
    practice: two consecutive lines both at 79.600). Grouping then makes a line
    with no length, and clamping squeezes it to start == end -- a cue no player shows.
    Each run of lines with one start gets [start, last end] split by text length. Only if
    that leaves under min_s per line does the run start earlier, into silence before it
    (never before the previous line's end). Where the words really were isn't known, so
    nothing moves more than that.
    """
    out, i = [], 0
    while i < len(lines):
        j = i + 1
        while j < len(lines) and abs(lines[j]["start"] - lines[i]["start"]) < 1e-6:
            j += 1
        run = lines[i:j]
        if len(run) > 1 or run[0]["end"] - run[0]["start"] < 1e-6:
            start, end = run[0]["start"], max(x["end"] for x in run)
            short = len(run) * min_s - (end - start)
            if short > 0:
                floor = out[-1]["end"] if out else 0.0
                start = max(floor, start - short)
            weights = [max(len(x["text"]), 1) for x in run]
            t = start
            for x, w in zip(run, weights):
                x["start"] = t
                t = x["end"] = t + (end - start) * w / sum(weights)
            run[-1]["end"] = end
        out.extend(run)
        i = j
    return out


def clamp_overlaps(lines):
    """Ensure no line's end runs past the next line's start."""
    for i in range(len(lines) - 1):
        if lines[i]["end"] > lines[i + 1]["start"]:
            lines[i]["end"] = lines[i + 1]["start"]
    return lines


def fmt_timestamp(t, decimal=","):
    """HH:MM:SS,mmm for SubRip; HH:MM:SS.mmm for WebVTT."""
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", decimal)


def render_srt(lines):
    out = []
    for i, line in enumerate(lines, start=1):
        out.append(str(i))
        out.append(f"{fmt_timestamp(line['start'], ',')} --> {fmt_timestamp(line['end'], ',')}")
        out.append(line["text"])
        out.append("")
    return "\n".join(out)


def render_vtt(lines):
    out = ["WEBVTT", ""]
    for i, line in enumerate(lines, start=1):
        out.append(str(i))
        out.append(f"{fmt_timestamp(line['start'], '.')} --> {fmt_timestamp(line['end'], '.')}")
        out.append(line["text"])
        out.append("")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(
        description="Generate caption files from a corrected whisper.cpp transcript using a tuning profile.",
    )
    ap.add_argument("--transcript", required=True, help="corrected whisper.cpp JSON transcript")
    ap.add_argument("--profile", required=True, help="tuning profile JSON from profiles/")
    ap.add_argument("--out", required=True, help="output caption file (use a NEW versioned filename)")
    ap.add_argument("--format", choices=["srt", "vtt"], help="output format (default: inferred from --out extension)")
    ap.add_argument("--force", action="store_true", help="allow overwriting an existing output file")
    args = ap.parse_args()

    out_path = Path(args.out)
    # Operating rule: never overwrite a variation. A "fix" routinely turns out to be
    # a regression, and without the previous version on disk there is no way back.
    if out_path.exists() and not args.force:
        sys.exit(
            f"error: {out_path} already exists.\n"
            f"       Write a new versioned filename instead (captions_v2{out_path.suffix}, ...),\n"
            f"       or pass --force if you genuinely mean to overwrite it."
        )

    fmt = args.format or out_path.suffix.lstrip(".").lower()
    if fmt not in ("srt", "vtt"):
        sys.exit(f"error: cannot infer format from '{out_path.name}' -- pass --format srt or --format vtt")

    with open(args.profile, encoding="utf-8") as f:
        profile = json.load(f)

    for key in ("line_grouping", "repeated_word_handling", "duration_ceiling", "timing_bias_correction"):
        if key not in profile:
            sys.exit(f"error: profile {args.profile} is missing required section '{key}'")

    raw_words = load_words(args.transcript)
    words = merge_repeats(raw_words, profile["repeated_word_handling"]["merge_gap_ms"])
    lines = group_lines(words, profile["line_grouping"], profile["duration_ceiling"])
    lines = apply_bias(lines, profile["timing_bias_correction"])
    if profile.get("post_processing", {}).get("spread_collapsed", True):
        lines = spread_collapsed(lines)
    if profile.get("post_processing", {}).get("overlap_clamp", True):
        lines = clamp_overlaps(lines)

    text = render_srt(lines) if fmt == "srt" else render_vtt(lines)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")

    merged_count = sum(1 for w in words if w.get("merged"))
    print(f"profile:    {profile.get('profile_name', args.profile)}")
    print(f"words in:   {len(raw_words)} ({merged_count} repeat runs merged)")
    print(f"lines out:  {len(lines)}")
    print(f"wrote:      {out_path} ({fmt})")
    print("\nFirst 10 lines:")
    for line in lines[:10]:
        dur = line["end"] - line["start"]
        print(f"  {line['start']:7.2f}-{line['end']:7.2f} ({dur:4.2f}s)  {line['text']}")


if __name__ == "__main__":
    main()
