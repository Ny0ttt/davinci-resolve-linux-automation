# Changelog

## Unreleased

**Breaking: whisper.cpp is no longer vendored as a git submodule.** This repo's history now
depends on nothing outside it — no `.gitmodules`, no gitlink, no external URL in the commit
graph. whisper.cpp is an externally installed tool, exactly like `ffmpeg` and
`TwitchDownloaderCLI` already were.

- Removed `.gitmodules` and the submodule wiring; `whisper.cpp/` is now gitignored. If you keep
  a build there it stays on your disk and is simply untracked.
- **`git clone --recurse-submodules` is no longer the install command.** Clone normally, then
  install and build whisper.cpp wherever you like and point `tools.json` at its binaries. See
  the README's new *whisper.cpp* and *Point this repo at it* sections. Tested against **v1.9.5**.
- Shipped configs still default to `whisper.cpp/build/bin/` and `whisper.cpp/models/`, so cloning
  whisper.cpp into the repo root keeps the quickstart working with no edits — the directory is
  gitignored, so it stays out of your history. Installed it elsewhere? Set `whisper_cli` /
  `whisper_vad` in `tools.json` and `whisper.model` / `align.vad_model` in your streamer profile.
  Preflight names any path it cannot find and refuses before the run starts. Note the old defaults
  pointed into a submodule that held only whisper.cpp's `for-tests-*` stubs, never a real model.
- Added `samples/jfk.mp4` (373 KB) so the quickstart still runs without you supplying footage —
  the public-domain JFK audio whisper.cpp ships, muxed onto a plain 1080p30 background. The clip
  route requires a video stream and rejects a bare `.wav` with `error: no video stream`. The
  README's first command now uses it; it was previously read out of the submodule.
- Run manifests record the whisper version from `whisper_cli --version` instead of a
  `git rev-parse` against the submodule — it now reports the binary you actually ran.

### Fixed

- **The UI could not start a run.** The `src/` restructure left `ui_server.PIPELINE` built from
  `ROOT` instead of `SRC`, so every run launched from the UI died with
  `can't open file '.../run_pipeline.py': [Errno 2]`. Import tests could not catch it, because
  `os.path.join` builds a string for a file that does not exist and `ui_server`'s `main()` binds
  a socket so it is never executed under test. Covered now by
  `test_ui_points_at_a_real_pipeline`.

## v0.2.0 — 2026-10-21

**Breaking: the LLM transcript-correction path is gone, and with it the only paid
dependency.** The pipeline now runs end to end on free, local tools.

- Removed the `context` and `correct` stages. `transcribe → align → review` is the new
  transcript path, and `review` applies your by-ear fixes from the Review screen.
- Removed the `claude` entry from `tools.json`. Nothing in the pipeline makes a network
  call or needs an account.
- Renamed `correct_transcript.py` to `apply_edits.py` and stripped it to the edit
  applier and SRT writer. Its `--context`, `--model`, `--chunk-words`,
  `--context-words`, `--min-confidence`, `--reuse-edits` and `--dry-run` flags are gone.
- Game packs no longer carry `correction_context`; `games/example_context.txt` is
  removed. Packs keep `icon_map` and `twitch_games`.
- Review screen: removed the Claude columns, the "Keep Claude's" action, the
  confidence/legend markers and the **"Remember for next time"** checkbox (it fed the
  correction context file, which no longer exists).
- `rebuild --from <unknown stage>` now reports the valid stage names instead of raising
  an unhandled error. `rebuild --from correct` lands here.
- A settings change now marks a stage stale rather than leaving a note, since no stage
  costs money to re-run.
- Captions are correspondingly rawer: see Known Gaps in the README.

Existing projects resume without migration. Their `review` stage shows stale and
rebuilds in seconds.

### Fixed in the same release — first-run blockers, found by running a cold clone

- **The documented command for the VAD model did not work.** The README said
  `download-ggml-model.sh silero-v5.1.2`, which answers `Invalid model` — silero models
  come from `download-vad-model.sh`. Without that file the align stage cannot start, so
  this blocked every new install.
- **The stated Python version was wrong.** `requirements.lock` pins librosa, numpy and
  scipy builds that require **Python 3.12+**; the README promised 3.10+, so installs on
  Ubuntu 22.04 and Debian 12 failed at the first command.
- **The VOD route reported tool problems as a bare `[Errno 2]`.** The Twitch metadata
  lookup runs before preflight, so an unedited `tools.json` placeholder produced an
  errno with no hint of the cause, even under `--dry-run`. It now gives preflight's
  message, naming the tool and the file.
- The 30-minute range refusal no longer explains itself with the deleted correction
  stage; the real reason is the un-windowed audio signals stage.
- `profiles/example_tutorial.json` no longer points at a `samples/` directory and a
  self-test that are not in this repo.
- The stages table no longer lists `.drt` as a `timeline` output — the DRT writer is not
  wired into the orchestrator, as Known gaps says.
- `pytest` is no longer pinned in `requirements.txt` / `requirements.lock`; it moved to
  `requirements-dev.txt`, so installing the pipeline no longer pulls a test runner.
- The README now shows the CPU build command instead of only mentioning that CUDA is
  optional after a block that hardcodes it.

### Changed — repo layout

- **All 15 Python modules moved to `src/`.** The root now holds only directories plus
  `README`, `LICENSE`, `CHANGELOG`, `Makefile` and the requirements files. A `## Layout`
  section in the README maps every directory.
- **Commands gained a `src/` prefix**: `venv/bin/python src/run_pipeline.py new ...`. Every
  `--help` synopsis, the systemd unit, `make ui` and the runtime "resume" hint were updated to
  match, so nothing tells you to type a path that no longer exists.
- **`__file__` was doing two jobs and is now split.** `run_pipeline.py` defines `SRC` (where the
  code lives, for invoking a sibling module as a subprocess) and `ROOT` (the repo root, for
  everything the user owns: `tools.json`, `streamers/`, `games/`, `profiles/`, `projects/`,
  `templates/`, `ui/`, `whisper.cpp`, `requirements.lock`). `make_drt.py` got the same split for
  its `templates/drt/` donors. Conflating the two is exactly how a move like this silently
  breaks every data path.

### Added

- **A smoke suite and CI.** `make check` now runs `tests/test_smoke.py` as well as the
  dependency check, and GitHub Actions runs it on every push. It is fixture-free -- no
  media, no transcripts -- so it checks that the repo is consistent and startable
  (imports, `--help`, preflight on a clean checkout, shipped config paths, docs vs code,
  no paid dependency, no private data) and nothing about output quality. A second CI job
  asserts `requirements.lock` resolves on the Python version the README claims and not
  below it.
- `requirements-dev.txt` for the test runner, kept out of `requirements.lock`.

## v0.1.0 — 2026-10-16

First public release.

- Staged pipeline: download → media conform → transcribe → correct → align →
  caption → score → webcam → timeline.
- DaVinci Resolve output in three interchange formats: FCPXML (layout +
  transforms), OTIO (cuts only), and DRT (animated captions).
- SRT captions with per-profile line grouping, repeat merging, overlap clamping.
- Word-level timing via whisper.cpp DTW plus Silero VAD end-trimming.
- Optional LLM transcript correction stage (`claude -p`), with human review.
- Emotion/loudness rough-cut scorer.
- Vertical Short layout (stacked webcam over gameplay) alongside Normal 16:9.
- Local web UI at `127.0.0.1:8765` for runs, review, and streamer profiles.
- Streamer profiles × game packs as the config model.
