#!/usr/bin/env python3
"""
Score emotion spikes in audio_signals.json and write a rough-cut cutlist.json.

    python src/score_cutlist.py --signals projects/<name>/audio_signals_v1.json \
                            --media ../<name>/clip_edit_dnxhr_lb.mov --fps 60 \
                            --out projects/<name>/cutlist_v1.json

High recall by design (handoff "Rough-cut design"): a moment is a highlight if ANY
signal trips. Highlights -> V1, everything else -> V2; nothing is deleted, so a wrong
call costs one drag in Resolve. Output feeds make_timeline.py unchanged.

Every threshold is relative to this recording's own baseline (median / MAD), because
mic gain and game volume differ per stream. Triggers, per row:

  LOUD     loudness >= baseline + --loud-z robust SDs            (shouting, screaming)
  PITCH    voice raised >= --pitch-semitones above his usual pitch,
           counted ONLY while loud enough to be his voice        (excited, screaming)
           -- unguarded, pitch fires on game music in quiet stretches
  SUSTAIN  >= --sustain-s of continuous voiced AND loud audio    (long "GOOOAL", "AAAH")
  BURST    bursty onsets while voiced + loud                     (laughter-like)
  FAST     fast speech while loud                                (excited rant)

Each segment's "reason" lists what tripped and when, so the call can be checked.
Operating rule 2 applies: refuses to overwrite --out without --force.
"""

import argparse
import json
import math
import os
import sys

import numpy as np


def robust(values):
    v = np.array([x for x in values if x is not None], dtype=float)
    med = float(np.median(v))
    mad = float(np.median(np.abs(v - med))) * 1.4826 or 1e-6
    return med, mad


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--signals", required=True)
    ap.add_argument("--media", required=True, help="media path written into the cutlist")
    ap.add_argument("--fps", type=float, required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--loud-z", type=float, default=1.5)
    ap.add_argument("--silence-db", type=float, default=-55.0,
                    help="rows quieter than this are excluded from the baseline")
    ap.add_argument("--voice-z", type=float, default=0.0,
                    help="loudness (SDs over his talking median) needed to treat "
                         "pitch/onsets as his voice")
    ap.add_argument("--pitch-semitones", type=float, default=6.0)
    ap.add_argument("--pitch-max-hz", type=float, default=650.0,
                    help="above this is music/whistle, not a human scream")
    ap.add_argument("--sustain-s", type=float, default=1.5)
    ap.add_argument("--sustain-z", type=float, default=1.0,
                    help="SUSTAIN also needs this much loudness: talking non-stop is not a shout")
    ap.add_argument("--burst-z", type=float, default=1.5)
    ap.add_argument("--fast-wps", type=float, default=5.0,
                    help="his normal Malay pace already hits 4-4.5 words/s")
    ap.add_argument("--pre-s", type=float, default=2.0, help="lead-in before a trigger")
    ap.add_argument("--post-s", type=float, default=2.5, help="tail after a trigger")
    ap.add_argument("--merge-gap-s", type=float, default=3.0)
    ap.add_argument("--min-len-s", type=float, default=3.0)
    ap.add_argument("--min-talking-s", type=float, default=10.0,
                    help="refuse to score with less talking than this: no baseline to compare to")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    if os.path.exists(args.out) and not args.force:
        sys.exit("error: %s exists; use a new versioned name or --force" % args.out)

    with open(args.signals) as f:
        sig = json.load(f)
    rows, hop, duration = sig["rows"], sig["hop_seconds"], sig["duration"]

    # Baseline = his normal TALKING level, not the whole track: near-silent gaps
    # (-60..-120 dB) would drag the median down and make ordinary speech look loud.
    talking = [r for r in rows if r["voiced"] >= 0.5 and r["loudness_db"] > args.silence_db]
    if len(talking) * hop < args.min_talking_s:
        # Silent / music-only audio: a median of nothing is NaN and every trigger
        # silently fails, giving "0 highlights" that looks like a real result.
        sys.exit("error: only %.1f s of talking in %s (need %.0f s, --min-talking-s) -- "
                 "wrong audio track, muted mic, or silence; nothing to score against"
                 % (len(talking) * hop, args.signals, args.min_talking_s))
    l_med, l_sd = robust(r["loudness_db"] for r in talking)
    o_med, o_sd = robust(r["onset"] for r in talking)
    voice_floor = l_med + args.voice_z * l_sd
    # His usual pitch: voiced rows loud enough to be him, below the music ceiling.
    pitches = [r["pitch_hz"] for r in rows
               if r["pitch_hz"] and r["loudness_db"] >= voice_floor
               and r["pitch_hz"] <= args.pitch_max_hz]
    p_base = float(np.median(pitches)) if pitches else None
    if p_base is None:
        print("warning: no usable pitch in his voice range -- PITCH trigger off", file=sys.stderr)

    trig = []
    run = 0.0
    for r in rows:
        t = []
        loud = r["loudness_db"]
        is_voice = loud >= voice_floor and r["voiced"] >= 0.5
        if loud >= l_med + args.loud_z * l_sd:
            t.append("LOUD")
        p = r["pitch_hz"]
        if p_base and is_voice and p and p <= args.pitch_max_hz and \
                12 * math.log2(p / p_base) >= args.pitch_semitones:
            t.append("PITCH")
        run = run + hop if (r["voiced"] >= 0.8 and loud >= l_med + args.sustain_z * l_sd) else 0.0
        if run >= args.sustain_s:
            t.append("SUSTAIN")
        if is_voice and r["onset"] >= o_med + args.burst_z * o_sd:
            t.append("BURST")
        if is_voice and r.get("words_per_s", 0) >= args.fast_wps:
            t.append("FAST")
        trig.append(t)

    # Rows -> padded windows -> merged segments.
    wins = [[max(0.0, r["t"] - args.pre_s), min(duration, r["t"] + hop + args.post_s), t, r["t"]]
            for r, t in zip(rows, trig) if t]
    segs = []
    for a, b, t, at in wins:
        if segs and a - segs[-1]["end"] <= args.merge_gap_s:
            segs[-1]["end"] = max(segs[-1]["end"], b)
        else:
            segs.append({"start": a, "end": b, "hits": []})
        segs[-1]["hits"].append((at, t))
    segs = [s for s in segs if s["end"] - s["start"] >= args.min_len_s]

    out_segs, cursor = [], 0.0
    for s in segs:
        if s["start"] > cursor:
            out_segs.append({"start": round(cursor, 3), "end": round(s["start"], 3),
                             "track": "V2", "reason": "filler"})
        kinds = {}
        for at, t in s["hits"]:
            for k in t:
                kinds.setdefault(k, []).append(at)
        reason = "; ".join("%s x%d @%s" % (k, len(v), ",".join("%.1f" % x for x in v[:4])
                                            + ("..." if len(v) > 4 else ""))
                           for k, v in sorted(kinds.items(), key=lambda kv: -len(kv[1])))
        out_segs.append({"start": round(s["start"], 3), "end": round(s["end"], 3),
                         "track": "V1", "reason": "highlight -- " + reason,
                         "score": sum(len(t) for _, t in s["hits"])})
        cursor = s["end"]
    if cursor < duration:
        out_segs.append({"start": round(cursor, 3), "end": round(duration, 3),
                         "track": "V2", "reason": "filler"})

    out = {"_comment": "SCORER OUTPUT (score_cutlist.py) from %s"
                       % os.path.basename(args.signals),
           "media": args.media, "fps": args.fps, "duration": duration,
           "baseline": {"loudness_db_median": round(l_med, 2), "loudness_db_sd": round(l_sd, 2),
                        "voice_floor_db": round(voice_floor, 2), "pitch_base_hz": p_base and round(p_base, 1),
                        "onset_median": round(o_med, 3), "onset_sd": round(o_sd, 3)},
           "params": {k: v for k, v in vars(args).items()
                      if k not in ("signals", "media", "fps", "out", "force")},
           "segments": out_segs}
    with open(args.out, "w") as f:
        json.dump(out, f, indent=1, allow_nan=False)  # a NaN here means a bug upstream

    hl = [s for s in out_segs if s["track"] == "V1"]
    print("baseline: loudness %.1f dB (sd %.1f), his pitch %s"
          % (l_med, l_sd, "%.0f Hz" % p_base if p_base else "n/a"))
    print("%d highlights, %.1f s of %.1f s on V1 (%.0f%%)"
          % (len(hl), sum(s["end"] - s["start"] for s in hl), duration,
             100 * sum(s["end"] - s["start"] for s in hl) / duration))
    for s in hl:
        print("  %6.1f-%6.1f  %s" % (s["start"], s["end"], s["reason"]))


if __name__ == "__main__":
    main()
