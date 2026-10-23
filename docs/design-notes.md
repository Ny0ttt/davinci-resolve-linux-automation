# Design notes

Why the architecture is shaped the way it is. Most of these decisions were forced by
a constraint rather than chosen freely — see [constraints.md](constraints.md) for the
constraints themselves.

---

## Interchange files, not scripting

Resolve's Python scripting API is Studio-only. On the free edition there is no way to
build a timeline programmatically.

The route that works is to **write a project file and import it by hand**. Import needs
no scripting and no licence. Confirmed working on free Resolve 21.1.1 with zero
scripting.

An alternative was evaluated and rejected: driving Resolve through an MCP server. Its
only subtitle path was Resolve's own auto-transcribe, which is the dead end this
project exists to replace.

**Consequence:** the pipeline's last stage writes files, and you import them. That also
means every result stays fully editable afterwards — nothing is burned in.

---

## Three output formats, because no single one is enough

| Format | Carries | Does not carry |
|---|---|---|
| **OTIO** | cuts, track layout, media references | frame size, crop, transforms, subtitles |
| **FCPXML** | all of the above **plus** transforms, scale, position, titles | title keyframes (animation) |
| **DRT** | everything, including caption animation | — (but it is Resolve's own format, reverse-engineered) |

OTIO was the first thing that worked and is still written as a backup. FCPXML replaced
it for layout once transforms were needed. DRT exists only because of the keyframe
limitation below.

Captions ship as a separate `.srt` for the plain route — OTIO has no subtitle track
type at all, and the FCPXML caption role (`iTT`) does not import. Basic Titles do
import, which is how the plain FCPXML route gets text on screen.

---

## Animated captions via DRT

Resolve **cannot import title keyframes through FCPXML** — not from a hand-written
file, not via compound clips, and not even from its own export round-tripped back in.
It silently keeps the first keyframe and drops the rest.

Nine approaches were tried before the working one:

| Attempt | Result |
|---|---|
| FCPXML keyframes, two syntactic forms | only the first keyframe survives |
| Keyframes retimed against the parent clip's clock | same |
| Byte-for-byte copy of Resolve's own export | same — **Resolve cannot re-import its own keyframes** |
| Captions as keyframed compound clips | compounds flattened, transforms dropped |
| Pre-rendered caption clips with alpha (PNG-in-MOV, ProRes, qtrle, CineForm) | works, but bakes the text — rejected, text must stay editable |
| Resolve's built-in Animated Subtitles | Studio-only |
| **`.drt` with a transform filter carrying Zoom keyframes** | **works** |

`.drt` is a zip of XML plus binary blobs. `drt_format.py` writes the keyframe list and
text-block structures directly, verified byte-identical against Resolve's own exports.
Timelines are built from real exported templates in `templates/drt/` rather than
generated from nothing, so the surrounding structure is known-good.

The caption animation itself is a scale pop — Zoom 0.89 → 1.21 → 1.0 over 0.2 s at each
caption's start.

**Status:** the DRT writer is probe-validated but **not yet wired into the
orchestrator**. See the README's status section.

---

## Caption timing is an audio problem, not a parameter problem

The longest-running defect in this project. Whisper folds trailing silence into the
preceding word, so captions tile the whole timeline and linger through silence.

Three fixes were tried in order:

1. **A per-word duration ceiling.** Caps cue length by word count. This is the obvious
   fix and it is wrong — it truncates real speech. On one clip it left captions
   covering 105 s of 180 s with 13 holes over a second. Disabled by sentinel in
   `profiles/example_gameplay.json`, with the reasoning kept inline.
2. **A measured timing bias shift.** Real, but only valid for the content it was
   measured on. Carried to faster speech it compressed every cue to near-zero.
3. **Word-level alignment from the audio.** The actual fix: word *starts* from
   whisper's DTW token times, word *ends* trimmed back to where speech stops according
   to Silero VAD. Caption-over-speech coverage went from **37% to 81%** and the first
   caption moved from 0.54 s to 7.50 s on the test clip.

This is why there is an `align` stage between `transcribe` and `review`, and why the
streamer profile carries an `align` block. The lesson generalises: when timing is
wrong because information is missing, no amount of parameter tuning recovers it.

---

## Correction as a separate, reviewable stage

Speech-to-text on code-switched speech with game jargon produces predictable errors —
proper nouns, loanwords, and terms whose correct spelling is domain knowledge. No
automatic pass fixes those reliably, so correction is **a reviewer's job**, done by ear
on the `review` stage's Review screen and applied by `apply_edits.py`.

An earlier version put a language model in this slot, fed by a per-game context file.
It was removed in v0.2.0: it made a paid CLI a hard dependency of every run, which is
the wrong trade for a tool meant to be cloned and run by anyone. The stage boundary it
established is what mattered, and that stayed.

Design choices that matter:

- **Correction is its own stage**, not folded into transcribe or caption, so edits can
  be reviewed, re-applied and audited independently of the words they came from.
- **Every edit is logged** with the original text, so corrections are auditable and
  reversible rather than silently overwriting the transcript.
- **Edits are content-hashed against the transcript.** Re-applying a saved edit list to
  a different or changed transcript is refused rather than applied to the wrong words.
- **A decision on a span whose words have since moved is rejected, not re-stamped** —
  getting this wrong silently corrupted timings once.
- **Stages are versioned, never overwritten.** Every stage writes `*_vN` and the
  project file records which version is current and what it was built from.

The review stage runs with no edits at all, so a project with nothing to fix still
produces final captions.

---

## Rough-cut scoring

The scorer picks highlight candidates from audio signals — loudness and emotional
spikes (shouting, laughter) — rather than from the transcript. Highlights go on V1 and
everything else stays on V2 **at its original absolute position**, so nothing is thrown
away and the rough cut can be undone by hand in Resolve.

It refuses silent audio rather than producing NaN scores.

**Status:** `cutlist.json` is the pipeline's central artifact and the scorer is the only
producer. This is the least mature stage.

---

## Config model: streamer x game

A run is a **streamer profile** crossed with a **game pack**.

- **Streamer profile** (`streamers/*.json`) — how one person sounds and looks: whisper
  flags, language, align settings, webcam box, layouts, caption style, media codec.
- **Game pack** (`games/*.json`) — what the game is: icon map, Twitch categories.
  Shared between streamers.

The split exists because the two change independently. Adding a second person who plays
the same game should not mean duplicating the game's jargon, and one person playing a
new game should not mean re-measuring their webcam.

---

## Graphics without a renderer

Icons triggered by transcript words go on a video track as ordinary image clips, and
Resolve composites the alpha natively. No separate renderer, no browser, no compositing
step.

The constraint that shapes this: interchange formats cannot reliably express scale or
position for an image clip. So **position is baked into the PNG** — each icon is a
full-frame mostly-transparent canvas. See [../icons/README.md](../icons/README.md).

**Known weakness:** icon hold and cooldown are fixed durations and do not follow the
speaker's phrasing or tempo, which can look mechanical. Timing that tracks the phrase
is unsolved.

---

## Orchestrator guarantees

The pipeline is long-running and failure-prone — downloads drop, whisper takes hours, a
model call can fail. The orchestrator is built around that:

- A stage counts as done **only after its output passes a check**; a failed or
  interrupted attempt's files are deleted, so no half-written file is ever mistaken for
  a finished one.
- **Staleness is content-based**, not timestamp-based — a stage is stale when an input's
  fingerprint or a setting changed. File times change when you copy a folder; hashes
  do not.
- One process holds the project lock and writes the project file atomically.
- Each tool runs in its own **process group**, so cancelling actually stops whisper
  and ffmpeg rather than orphaning them.
- The machine is kept awake during a run.
- **A stage re-runs when its inputs' content changes, not when a file's timestamp
  changes.** Rebuilding an upstream stage that produces an identical artifact leaves
  everything downstream alone.
- Ranges over 30 minutes are refused by default, because audio signals are not yet
  windowed. `--allow-long` overrides.

---

## Unit conversion

FCPXML transform values are a percentage of **frame height**. Resolve's Inspector
`Position X` is a fraction of **timeline width**. Both were established by reading
Resolve's own `Project.db` after nudging a clip by hand, not assumed from documentation.

Profiles store **Inspector units** — the numbers you can read and tweak in Resolve —
and `make_fcpxml.py` converts. `resolve_read.py` reads values back out of a Resolve
project so a layout can be tuned by hand and then captured.
