# Hard constraints

Everything here was found the hard way on a real machine. If you are building
something similar, this is the part worth reading first.

Reference environment: DaVinci Resolve **21.1.1, free edition**, Linux, hybrid
graphics (AMD Vega iGPU + NVIDIA GTX 1050), whisper.cpp 1.9.5.

---

## DaVinci Resolve, free edition on Linux

### No h264 decode. No AAC at all.

The free edition on Linux cannot decode h264 video, and cannot decode AAC audio in
any edition on this platform. Importing a normal Twitch VOD fails with:

```
avc1 not Found in Repository
```

h264 decode is a Studio-only feature. AAC is simply absent. **Every source file must
be transcoded before Resolve will touch it** — this is why the pipeline has a media
conform stage at all, and why it is not optional.

Formats tested:

| Codec | Result | Cost |
|---|---|---|
| h264 / AAC (source) | **fails to import** | — |
| DNxHR LB | works | 11.5 MB/s ≈ **220 GB** for a 5.7 h VOD |
| MPEG-4 Part 2 `-q:v 3` + PCM s16le | works | ≈ **4.8 GB/h** |
| MPEG-2 + PCM | works | ≈ 9–14 GB/h |

MPEG-4 Part 2 + PCM is the default in `streamers/example.json`. DNxHR is correct for
quality work but a full VOD exceeds most people's free disk space.

### The scripting API is Studio-gated

Resolve's Python scripting API requires Studio. On the free edition there is no
programmatic way to build a timeline, which is the whole reason this project writes
**interchange files** (FCPXML / OTIO / DRT) and lets you import them by hand. That
import needs no scripting and works on the free edition.

### Resolve cannot import title keyframes

This one cost nine rounds of probing. Resolve will **not** import animation keyframes
on a title through FCPXML — not from a hand-written file, not from compound clips, and
**not even from its own export round-tripped back in**. It keeps the first keyframe and
discards the rest, silently.

Also confirmed along the way:

- Compound clips are flattened on import and their transforms are dropped.
- A vertical timeline rejects `Crop Left` / `Zoom` values that are legal in 16:9
  (`out of range` in the Resolve log) — pre-crop with ffmpeg and use position only.
- The `iTT` caption role does not import. Basic Titles do.

**Consequence:** animated captions cannot go through FCPXML. They go through `.drt`
instead — see [design-notes.md](design-notes.md#animated-captions-via-drt).

### OTIO cannot express layout or subtitles

OpenTimelineIO has no subtitle track type, and no frame size, crop or transform that
Resolve honours. It is good for **cuts only**. Layout and transforms need FCPXML;
captions need SRT (dragged in) or `.drt` (animated).

### Unit systems do not match

- FCPXML transform values are a **percentage of frame height** (per Apple's spec).
- Resolve's Inspector `Position X` is a **fraction of timeline width** (transform
  parameter 40), confirmed by reading Resolve's own `Project.db`.

`make_fcpxml.py` converts between them. Profiles store Inspector units, because those
are the numbers you can actually read and tweak in Resolve.

### Digits in PNG filenames break image clips

Resolve interprets trailing digits in an image filename as an **image-sequence frame
number** and will mis-import or refuse the clip. Name icons `left_stick.png`, never
`left_stick_01.png`.

---

## Resolve on hybrid graphics

### Black preview — OpenGL/CUDA interop

On a laptop with an integrated GPU plus an NVIDIA card, the viewer renders black: the
iGPU is driving OpenGL while Resolve runs CUDA on the discrete card, and the interop
fails. Fix is a PRIME offload wrapper — see `resolve-nvidia.sh`. **Always launch
Resolve through it**, not from the desktop icon.

### `Local.GPU.Mode` must be CUDA on NVIDIA

If Resolve stops booting, check `config.dat` for `Local.GPU.Mode = OpenCL`. On this
hardware OpenCL produced 12 boot failures in 13 minutes; CUDA ran a full day with
zero. The symptom is in `/opt/resolve/logs/rollinglog.txt*` — grep for
`Selected compute API`.

That log diff is the fastest diagnostic for any Resolve startup problem. Reach for it
before touching driver packages.

---

## whisper.cpp

### `-l` defaults to `en`, not auto-detect

This is the single most damaging default in the stack. **Omitting `-l` does not mean
auto-detect.** whisper.cpp assumes English, and on non-English speech it will produce
fluent English *translation* rather than a transcript — with no warning, and output
that looks perfectly plausible.

Always set the language explicitly in the streamer profile. Setting it correctly also
ran **3 minutes faster** than the wrong run on the same clip, and preserved
code-switched English terms as English.

### Word timing needs DTW, and DTW needs flash attention off

For per-word timestamps:

```
-ml 1 -sow -nfa --dtw medium -ojf
```

- `-ml 1 -sow` forces one word per transcript entry, which the caption and review
  stages both require.
- `--dtw <preset>` must **match the model** you are running.
- DTW is **silently disabled when flash attention is on**, hence `-nfa`.
- `-ojf` keeps per-token `t_dtw` in the JSON for the align stage.

### Whisper attributes trailing silence to the last word

A two-syllable word can carry a 1.9 s timestamp because the following silence is
folded into it. Captions then tile the entire timeline and linger through silence.

No caption-stage parameter can fix this — the information is not in the transcript.
The fix is to trim word ends against speech regions from the audio (Silero VAD), which
is what the align stage does. On a real clip it moved caption-over-speech coverage from
**37% to 81%** and the first caption from 0.54 s to 7.50 s.

Tempting wrong fix: a per-word duration ceiling. It caps the symptom and truncates real
speech — on one clip it left captions covering 105 s of a 180 s clip with 13 holes over
a second. See the notes in `profiles/example_gameplay.json`.

### CPU-only inference is slow

CPU-only whisper.cpp runs at roughly **3.2× realtime** on the reference machine, so a
multi-hour VOD takes 10–15 hours. Build with CUDA if you intend to process full VODs.
The pipeline refuses ranges over 30 minutes by default (`--allow-long` overrides).

---

## ffmpeg

### `-r` duplicates the first frame of each cut

Using `-r` when assembling per-cut webcam segments duplicates the first frame of every
cut, producing a cumulative **1-frame desync** against the gameplay track. Fixed in
`make_webcam.py`; worth knowing if you write your own.

---

## Other

- **Twitch VOD downloads** are the user's responsibility. Use this on footage you own
  or have the rights to. `TwitchDownloaderCLI` is not bundled.
- **`.drt` is a zip of XML plus binary blobs** including zstd-compressed sections. A
  zstd frame written without a declared content size makes Resolve silently fall back
  to a Basic Title. `drt_format.py` handles this.
