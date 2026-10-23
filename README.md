# davinci-resolve-linux-automation

Turn a long gaming VOD into an **editable DaVinci Resolve project** — transcript,
captions, a scored rough cut, and a vertical Short — without opening Resolve, and
without Resolve Studio.

Nothing is rendered or burned in. The pipeline writes interchange files (FCPXML / OTIO
/ DRT) and an SRT; you import them and keep editing normally.

```
VOD link + time range
   │
   ├─ transcribe  (whisper.cpp, word-level)
   ├─ align       (DTW word starts + VAD-trimmed ends)
   ├─ review      (your by-ear fixes, optional -- the run works with none)
   ├─ score       (loudness / emotion spikes → highlight candidates)
   │
   ▼
timeline_normal.fcpxml   ·   timeline_short.fcpxml   ·   captions.srt
```

---

## Status: v0.2.0 — it works, but setup is manual

This tool is built and tested on one specific machine, then cleaned up for publication. **Profile
setup and onboarding are not designed yet — expect to read the code.** Issues and
questions are welcome. 

There is a smoke suite (`make check`, run on every push by CI) but it is deliberately narrow:
it proves the repo is internally consistent and startable — every module imports, every CLI
responds to `--help`, `preflight` survives a clean checkout, shipped configs point at paths that
exist, and the docs agree with the code. It ships **no media and no fixtures**, so it cannot and
does not check that captions are accurate, that timing is right, or that a timeline looks correct
in Resolve. A green check is not a quality signal.

Output quality is validated privately against real footage before each release. `--dry-run`
checks your setup, not your output. So if a stage produces something wrong, that is worth an
issue — don't assume your environment is at fault.


This would be the beta version of this tool. Lots of work to be done here. This document will be updated at times

What this is *not*: a one-click tool, a hosted service, or something that will guess
your layout for you. You will measure your own webcam box and tune your own caption
timing.

Your workflow after implementing this: 
1. Get VOD from chosen social media platform
2. Run the tool
3. Import highlighted clips, layouts, transcribe.
4. Review and export the clear cut, transcribed timeline

Happy exploring.

---

## Requirements

| | |
|---|---|
| **OS** | Linux (developed on Ubuntu; nothing is deliberately Linux-only except the Resolve notes) |
| **DaVinci Resolve** | 21.x, **free edition is enough**. Studio not required |
| **Python** | 3.12+ (`requirements.lock` pins librosa, numpy and scipy builds that need it) |
| **ffmpeg / ffprobe** | any recent build |
| **whisper.cpp** | installed separately and built by you (CUDA strongly recommended). Tested against **v1.9.5**. [Repo](https://github.com/ggerganov/whisper.cpp) |
| **TwitchDownloaderCLI** | *optional* — only for the VOD download stage. [Releases](https://github.com/lay295/TwitchDownloader/releases) |


---

## Install

```bash
git clone https://github.com/Ny0ttt/davinci-resolve-linux-automation
cd davinci-resolve-linux-automation

python3 -m venv venv
venv/bin/pip install -r requirements.lock
```

### whisper.cpp

**whisper.cpp is not bundled.** It is a separate project with its own release cycle; this repo
only calls its binaries. Tested against **v1.9.5**.

The shipped config defaults expect it at `whisper.cpp/` inside this repo, so the simplest thing is
to clone it right here. **`whisper.cpp/` is gitignored** — nothing about it enters this repo's
history, and there is no submodule.

```bash
# from the repo root
git clone https://github.com/ggerganov/whisper.cpp
cd whisper.cpp
cmake -B build -DGGML_CUDA=1 && cmake --build build -j --config Release
sh ./models/download-ggml-model.sh medium
sh ./models/download-vad-model.sh silero-v5.1.2      # for the align stage
cd ..
```


Note the two different scripts: the VAD model is **not** in `download-ggml-model.sh`'s list, so
asking it for `silero-v5.1.2` just prints `Invalid model`.

`medium` is ~1.5 GB and the silero VAD model is small; budget the download plus a few minutes
of build time before the first run.

**No CUDA toolkit?** Drop `-DGGML_CUDA=1` — `cmake -B build && cmake --build build -j --config
Release` builds the same binaries on CPU. Transcription then runs at about 3.2× realtime, so a
5-hour VOD takes most of a day; short clips are fine.

### Point this repo at it

```bash
cp tools.example.json tools.json
```

**If you cloned whisper.cpp into this repo, you are done** — the defaults in `tools.example.json`
and `streamers/example.json` already point at `whisper.cpp/build/bin/` and `whisper.cpp/models/`,
and relative paths resolve from the repo root.

If you put it elsewhere, edit two places: `whisper_cli` and `whisper_vad` in `tools.json`, and
`whisper.model` + `align.vad_model` in the streamer profile you run with. Absolute paths are fine
and are the simplest thing that works.

---


## Run it

### First run — check your setup, then do one clip

A fresh clone ships a working `streamers/example.json`, `games/example.json` and an 11-second
sample clip at `samples/jfk.mp4`, so you can get your first output without supplying footage or
editing anything — provided whisper.cpp is where the step above put it.

Start with the preflight, which checks your tools, models and disk space and starts nothing:

```bash
venv/bin/python src/run_pipeline.py new \
    --clip samples/jfk.mp4 --game example --streamer example --dry-run
```

Fix whatever it reports, then drop `--dry-run`:

```bash
venv/bin/python src/run_pipeline.py new \
    --clip samples/jfk.mp4 --game example --streamer example
```

Output lands in `projects/jfk/`. Import the `.fcpxml` into Resolve and drag the `.srt`
onto a subtitle track. Swap in your own `--clip /path/to/clip.mp4` once that works.

The clip route puts the whole clip on V1 and does no scoring, which is deliberate for a first
run: it skips the download and webcam stages and the rough-cut scorer, so there is less to go
wrong and less to tune.

### Then: your own profile and game pack

Once a clip has gone through, copy the examples and edit them:

```bash
cp streamers/example.json streamers/myname.json
cp games/example.json     games/mygame.json
```

The comments in each file explain every field; the two that matter most are `whisper.language`
(**always set it explicitly** — see below) and `webcam.box`.

> Once more than one streamer profile exists, the clip route can no longer guess — pass
> `--streamer <name>` on every run.

### Then: a Twitch VOD

```bash
venv/bin/python src/run_pipeline.py new \
    --vod https://www.twitch.tv/<channel>/v/<id> \
    --start 01:14:00 --duration 2m30s \
    --streamer myname --layouts normal,short
```

Other commands:

```bash
src/run_pipeline.py status [PROJECT]        # one project, or all
src/run_pipeline.py resume PROJECT          # continue an interrupted run
src/run_pipeline.py rebuild PROJECT --from align
src/run_pipeline.py layouts PROJECT normal,short
src/run_pipeline.py choose PROJECT --options fcpxml,srt
src/run_pipeline.py cancel PROJECT
```

Output lands in `projects/<name>/`. Import the `.fcpxml` into Resolve and drag the
`.srt` onto a subtitle track.

> **Launch Resolve through `resolve-nvidia.sh`** if you are on hybrid graphics,
> or the viewer will be black. See
> [docs/constraints.md](docs/constraints.md#resolve-on-hybrid-graphics).

### Web UI

```bash
make ui     # http://127.0.0.1:8765
```

Start runs, review the transcript by ear, and build streamer profiles by dragging a box
on a real frame. It binds to localhost only. `make install-service` starts it at login;
`make uninstall-service` undoes that.

---

## Layout

```
src/            every Python module -- the orchestrator and the 14 stage scripts
ui/             the web UI (served by src/ui_server.py; localhost only)
docs/           design notes and the hardware/Resolve constraints behind them
streamers/      one JSON per person: whisper flags, webcam box, layouts   (yours stay local)
games/          one JSON per game: icon map, Twitch categories            (yours stay local)
profiles/       caption timing profiles                                   (yours stay local)
templates/drt/  Resolve project exports the .drt writer builds from
icons/          your own icon art                                         (not shipped)
tests/          fixture-free smoke tests -- see Status above for what they do and don't prove
projects/       every run's output lands here                             (gitignored)
samples/        jfk.mp4, an 11s clip for the first run
```

Only `src/` holds code. Everything beside it is either data you own or output. Module names
referenced in these docs (`make_fcpxml.py`, `apply_edits.py`, `score_cutlist.py`, …) all live in
`src/`.

---

## Stages

Every stage writes **new versioned files** and never overwrites. The project file
records which version is current and what each was built from, so `rebuild --from X`
is always safe.

| Stage | Does | Output |
|---|---|---|
| `download` / `import` | fetch the VOD range, or take your clip | `clip_v1.mp4` |
| `audio` | extract mono audio | `audio_v1.wav` |
| `transcribe` | whisper.cpp, one word per entry, DTW token times | `transcript_v1.json` |
| `align` | word starts from DTW, ends trimmed to speech (VAD) | `transcript_aligned_v1.json` |
| `review` | apply your by-ear fixes from the Review screen (none is fine) | `transcript_final_v1.json` + SRT |
| `media` | transcode to something Resolve can actually read | `clip_edit_v1.mov` |
| `signals` | loudness and emotion signals | `audio_signals_v1.json` |
| `score` / `cut` | pick highlight candidates | `cutlist_v1.json` |
| `webcam` | cut out the webcam for vertical layouts | `webcam_v1.mov` |
| `icons` | place images where trigger words occur | `graphics_v1.json` |
| `timeline` | write the interchange files | `timeline_*.fcpxml`, `.otio` |

Guarantees worth knowing: a stage counts as done only once its output passes a check;
interrupted attempts are cleaned up rather than left to look finished; staleness is
decided by content fingerprint, not file time; cancelling actually kills whisper and
ffmpeg. You can see all this in the web UI.

Details in
[docs/design-notes.md](docs/design-notes.md#orchestrator-guarantees).

---

## Configuration model

A run is a **streamer profile × game pack**.

- `streamers/*.json` — how one person sounds and looks: whisper flags, language, align
  settings, webcam box, layouts, caption style, media codec.
- `games/*.json` — what the game is: icon map, the Twitch categories it matches.
  Shared between streamers.
- `profiles/*.json` — caption timing and line-grouping, referenced by a streamer
  profile.


### Whisper.cpp settings

**Always set `whisper.language` explicitly.** whisper.cpp's `-l` defaults to `en`, not
auto-detect — on non-English speech it produces a fluent English *translation* instead
of a transcript, with no warning and output that looks entirely plausible. Example script are in Malay Language.

---

## Known gaps

Stated plainly so nobody files an issue for something already known:

- **Captions are raw whisper output plus your own fixes.** There is no automatic
  correction pass, so game jargon, player names and unusual spellings come out wrong more
  often than a model-corrected transcript would. The Review screen is how you fix them, by
  ear, a word at a time. An LLM correction stage existed until v0.2.0 and was removed
  because it made a paid CLI a hard dependency of every run.
- **The DRT writer is not wired into the orchestrator.** Animated captions work and are
  probe-validated, but you run `src/make_drt.py` by hand. This is the next piece of work.
- **The rough-cut scorer is the least mature stage.** `cutlist.json` is the pipeline's
  central artifact and the scorer is its only producer. It works; it has not been
  validated across much content.
- **Caption silence gating is only handled upstream.** The align stage mitigates it; the
  caption stage itself has no silence model. See
  [docs/design-notes.md](docs/design-notes.md#caption-timing-is-an-audio-problem-not-a-parameter-problem).
- **Icon hold and cooldown are fixed durations.** They don't follow the speaker's tempo
  and can look mechanical.
- **Timing bias is unmeasured** in the example gameplay profile, deliberately — see the
  notes inside the file.
- **Ranges over 30 minutes are refused** by default, because the audio signals stage is not
  windowed and can run out of RAM. `--allow-long` overrides.
- **Icon packs are not included.** Supply your own art — [icons/README.md](icons/README.md).
- FCPXML parity has been import-tested at normal scale; a 600-clip stress file crashed
  Resolve on import.

---

## Documentation

- [docs/constraints.md](docs/constraints.md) — platform limits found the hard way.
  **Read this one.**
- [docs/design-notes.md](docs/design-notes.md) — why the architecture is shaped this way.
- [icons/README.md](icons/README.md) — icon pack format.
- [templates/drt/README.md](templates/drt/README.md) — where the DRT templates come from.

---

## Legal

Use this on footage you own or have the rights to. Downloading VODs is between you and
the platform's terms of service;  `TwitchDownloaderCLI` is not bundled.

`templates/drt/*.drt` are the author's own DaVinci Resolve project exports, included so
the DRT writer has known-good structures to build from.

`samples/jfk.mp4` is the audio of John F. Kennedy's 1961 inaugural address — a work of the
United States federal government and therefore in the public domain, and the same recording
whisper.cpp ships as its own sample — muxed here onto a plain 1080p30 colour background so the
clip route has the video stream it requires. It exists so the quickstart runs without you
supplying footage. whisper.cpp itself is MIT-licensed and is **not** bundled with this project. DaVinci Resolve and Blackmagic
Design are trademarks of Blackmagic Design Pty Ltd; this project is not affiliated with
or endorsed by them.

MIT — see [LICENSE](LICENSE).
