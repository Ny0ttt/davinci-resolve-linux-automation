#!/usr/bin/env python3
"""
Word timing: whisper.cpp's own word times -> times that follow the voice (docs/design-notes.md#animated-captions-via-drt,
step 0). the author checked the result by eye and ear on run2 ("much better").

whisper's -ml 1 -sow word times end where the next word starts, so silences get handed to the word
before them, and lines after a silence come seconds early (run2: the first line at 0.54 s, speech
from ~7.3 s). Two fixes, both measured on run2:

  start = DTW time of the word's first token (whisper run with --dtw <model> -nfa -ojf; kept in order)
  end   = next word's start, or where its speech segment ends (+pad), whichever comes first
          (Silero VAD via whisper.cpp's whisper-vad-speech-segments) -- at least min_word_ms long

Output = the same whisper JSON, same entries in the same order (the correction/review word numbers
don't move), only `offsets` replaced; whisper's own are kept as `offsets_whisper`. Words without a
DTW time (DTW off) keep whisper's start; the end is still trimmed to speech.

    python src/align_words.py --transcript transcript_v1.json --audio audio_v1.wav \\
        --vad-tool whisper.cpp/build/bin/whisper-vad-speech-segments \\
        --vad-model whisper.cpp/models/ggml-silero-v5.1.2.bin --out transcript_aligned_v1.json
"""
import argparse
import json
import os
import re
import subprocess
import sys

SEGMENT = re.compile(r"Speech segment \d+: start = ([0-9.]+), end = ([0-9.]+)")


def speech_segments(tool, model, audio, min_silence_ms):
    """[(start ms, end ms)] of speech. The tool prints centiseconds."""
    p = subprocess.run([tool, "-np", "-vm", model, "-vsd", str(min_silence_ms), "-f", audio],
                       capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if p.returncode != 0:
        sys.exit("error: VAD failed (%d): %s" % (p.returncode, p.stderr.strip()[-300:]))
    segs = [(float(a) * 10, float(b) * 10) for a, b in SEGMENT.findall(p.stdout)]
    if not segs and "Detected 0" not in p.stdout:
        sys.exit("error: VAD output not understood: %r" % p.stdout[:300])
    return segs


def dtw_start(entry):
    """ms of the word's first real token on the DTW clock, or None (t_dtw is in 10 ms units)."""
    for t in entry.get("tokens", []):
        if t.get("t_dtw", -1) >= 0 and not t.get("text", "").startswith("["):
            return t["t_dtw"] * 10
    return None


def align(entries, segs, pad_ms=150, min_word_ms=120):
    """New (start, end) ms for every entry with text, in place. Returns counts."""
    words = [e for e in entries if e.get("text", "").strip()]
    starts, prev, from_dtw = [], 0, 0
    for e in words:
        a = dtw_start(e)
        from_dtw += a is not None
        a = e["offsets"]["from"] if a is None else a
        a = max(int(round(a)), prev)                  # DTW can wobble backwards; words stay in order
        starts.append(a)
        prev = a
    for i, e in enumerate(words):
        a = starts[i]
        nxt = starts[i + 1] if i + 1 < len(words) else max(e["offsets"]["to"], a)
        seg_end = next((b for s, b in segs if s - pad_ms <= a <= b + pad_ms), a + 400)
        b = max(a + min_word_ms, min(nxt, seg_end + pad_ms))
        e["offsets_whisper"] = dict(e["offsets"])
        e["offsets"] = {"from": a, "to": int(round(b))}
        e["timestamps"] = {"from": stamp(a), "to": stamp(b)}
    return {"words": len(words), "dtw_starts": from_dtw}


def stamp(ms):
    ms = int(round(ms))
    return "%02d:%02d:%02d,%03d" % (ms // 3600000, ms // 60000 % 60, ms // 1000 % 60, ms % 1000)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--transcript", required=True, help="whisper.cpp JSON (-ml 1 -sow; -ojf --dtw for DTW starts)")
    ap.add_argument("--audio", required=True)
    ap.add_argument("--vad-tool", required=True)
    ap.add_argument("--vad-model", required=True)
    ap.add_argument("--min-silence-ms", type=int, default=200)
    ap.add_argument("--pad-ms", type=int, default=150)
    ap.add_argument("--min-word-ms", type=int, default=120)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    if os.path.exists(args.out):
        sys.exit("error: %s already exists (write a new version)" % args.out)
    data = json.load(open(args.transcript, encoding="utf-8"))
    segs = speech_segments(args.vad_tool, args.vad_model, args.audio, args.min_silence_ms)
    counts = align(data["transcription"], segs, args.pad_ms, args.min_word_ms)
    data["align"] = dict(counts, method="dtw+vad" if counts["dtw_starts"] else "whisper+vad",
                         pad_ms=args.pad_ms, min_word_ms=args.min_word_ms,
                         min_silence_ms=args.min_silence_ms, speech=[[round(a), round(b)] for a, b in segs])
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print("%s: %d words, %d DTW starts, %d speech segments -> %s"
          % (data["align"]["method"], counts["words"], counts["dtw_starts"], len(segs), args.out))


if __name__ == "__main__":
    main()
