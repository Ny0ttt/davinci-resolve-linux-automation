#!/usr/bin/env python3
"""
Per-second emotion signals from a stream's audio track.

    python src/audio_signals.py --audio projects/<name>/audio.wav \
                            --out projects/<name>/audio_signals.json \
                            [--transcript projects/<name>/transcript_corrected_v1.json]

Writes one row per hop (default 0.5 s) with raw measurements only -- no scoring,
no thresholds. score_cutlist.py decides what counts as a spike.

  loudness_db   RMS energy in dB                 (shouting, screaming)
  pitch_hz      median voiced F0 (pyin), or null (raised voice, screaming)
  voiced        fraction of frames that are voiced
  brightness_hz spectral centroid                (screams and laughs are bright)
  onset         mean onset strength              (bursty: laughter, hits, slams)
  words_per_s   from transcript word timestamps   (rapid excited speech)

Caveat: stream audio is the streamer's voice MIXED with game sound. Loudness and
brightness also rise on in-game crowd noise; pitch is the most voice-specific signal.
Operating rule 2 applies: refuses to overwrite --out without --force.
"""

import argparse
import json
import os
import sys

import librosa
import numpy as np


def words_from_transcript(path):
    with open(path, encoding="utf-8") as f:
        t = json.load(f)
    return [(seg["offsets"]["from"] / 1000.0, seg["offsets"]["to"] / 1000.0)
            for seg in t["transcription"] if seg["text"].strip()]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--audio", required=True, help="audio file (any rate; resampled to 16 kHz mono)")
    ap.add_argument("--out", required=True, help="output JSON")
    ap.add_argument("--transcript", help="whisper JSON, for words_per_s")
    ap.add_argument("--hop", type=float, default=0.5, help="seconds per row (default 0.5)")
    ap.add_argument("--force", action="store_true", help="allow overwriting --out")
    args = ap.parse_args()

    if os.path.exists(args.out) and not args.force:
        sys.exit("error: %s exists; use a new versioned name or --force" % args.out)

    sr = 16000
    y, _ = librosa.load(args.audio, sr=sr, mono=True)
    duration = len(y) / sr
    frame, hop = 1024, 256                      # 16 ms hop at 16 kHz
    rms = librosa.feature.rms(y=y, frame_length=frame, hop_length=hop)[0]
    centroid = librosa.feature.spectral_centroid(y=y, sr=sr, n_fft=frame, hop_length=hop)[0]
    onset = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop)
    # 65-1000 Hz covers normal speech up to screaming.
    f0, voiced_flag, _ = librosa.pyin(y, fmin=65, fmax=1000, sr=sr,
                                      frame_length=frame * 2, hop_length=hop)
    t_frames = librosa.frames_to_time(np.arange(len(rms)), sr=sr, hop_length=hop)

    words = words_from_transcript(args.transcript) if args.transcript else None

    rows = []
    n = int(np.ceil(duration / args.hop))
    for i in range(n):
        a, b = i * args.hop, min((i + 1) * args.hop, duration)
        m = (t_frames >= a) & (t_frames < b)
        if not m.any():
            continue
        r = float(np.mean(rms[m[:len(rms)]]))
        v = voiced_flag[m[:len(voiced_flag)]]
        f = f0[m[:len(f0)]][v] if v.any() else np.array([])
        row = {
            "t": round(a, 3),
            "loudness_db": round(float(20 * np.log10(max(r, 1e-6))), 2),
            "pitch_hz": round(float(np.median(f)), 1) if f.size else None,
            "voiced": round(float(v.mean()), 3),
            "brightness_hz": round(float(np.mean(centroid[m[:len(centroid)]])), 1),
            "onset": round(float(np.mean(onset[m[:len(onset)]])), 3),
        }
        if words is not None:
            # words whose midpoint falls in a 2 s window centred on this row
            c = (a + b) / 2
            row["words_per_s"] = round(sum(1 for s, e in words
                                           if abs((s + e) / 2 - c) <= 1.0) / 2.0, 2)
        rows.append(row)

    out = {"source_audio": os.path.abspath(args.audio), "duration": round(duration, 3),
           "hop_seconds": args.hop, "sample_rate": sr,
           "transcript": os.path.abspath(args.transcript) if args.transcript else None,
           "rows": rows}
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1)
    print("wrote %s: %d rows over %.1f s" % (args.out, len(rows), duration))


if __name__ == "__main__":
    main()
