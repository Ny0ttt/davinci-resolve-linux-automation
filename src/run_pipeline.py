#!/usr/bin/env python3
"""
Orchestrator: Twitch VOD link + time range in -> FCPXML timeline(s) + SRT captions out.

    python src/run_pipeline.py new --vod https://www.twitch.tv/CHANNEL/v/VODID \\
                               --start 01:14:00 --duration "2m30s"     # or --end 01:16:30
                               [--layouts normal,short] [--streamer NAME]
    python src/run_pipeline.py new --clip /path/to/clip.mp4 --game GAME   # pre-cut clip route
    python src/run_pipeline.py layouts PROJECT normal,short   # add/remove a layout, rebuild timelines
    python src/run_pipeline.py choose PROJECT --options fcpxml,srt   # what a run makes (docs/design-notes.md#orchestrator-guarantees)
    python src/run_pipeline.py status [PROJECT]       # one project, or all of them
    python src/run_pipeline.py resume PROJECT         # continue an interrupted / failed run
    python src/run_pipeline.py rebuild PROJECT --from align
    python src/run_pipeline.py cancel PROJECT

Design: docs/design-notes.md#orchestrator-guarantees, hardened per docs/design-notes.md#orchestrator-guarantees.

Stages call the existing scripts as subprocesses, so each stays standalone-testable.
A run = streamer profile (streamers/*.json: how they sound, webcam box, layouts; picked from
the VOD's Twitch channel) x game pack (games/*.json: icon map, Twitch category match), and one
FCPXML per ticked layout (docs/design-notes.md#three-output-formats-because-no-single-one-is-enough); the OTIO is still written as a
backup. Every stage writes NEW versioned files (never overwrite); project.json says which version is current and what each was built from.

Guarantees:
  - a stage only counts as done after its output passes a check; a failed or interrupted
    attempt's files are removed, so no half-written file ever passes as done
  - stale = an input's content fingerprint or a setting changed since the output was
    built (not file times, which copying changes)
  - one process writes project.json (holds the project's .lock); writes are atomic
  - each tool runs in its own process group: cancel / Ctrl-C stops whisper and ffmpeg
  - the laptop is kept from sleeping during a run (systemd-inhibit)
  - ranges over 30 min are refused until audio signals are windowed (audit F-05, F-03);
    --allow-long overrides
"""

import argparse
import datetime
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import time

# Two different anchors, deliberately separate -- conflating them is how a move to src/
# silently breaks every data path:
#   SRC  = where the code lives, for invoking a sibling module as a subprocess
#   ROOT = the repo root, for everything the USER owns: tools.json, streamers/, games/,
#          profiles/, projects/, templates/, ui/, whisper.cpp, requirements.lock
SRC = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SRC)
SCHEMA_VERSION = 1
MAX_RANGE_S = 30 * 60


def projects_dir():
    return os.environ.get("VP_PROJECTS") or os.path.join(ROOT, "projects")


def die(msg, code=1):
    print("error: " + msg, file=sys.stderr)
    sys.exit(code)


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------- input parsing

VOD_URL = re.compile(r"^(?:https?://)?(?:www\.|m\.)?twitch\.tv/"
                     r"(?:videos/|[A-Za-z0-9_]{1,40}/v(?:ideo)?/)(\d{5,14})/?(?:[?#].*)?$")
UNITS = {"h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
         "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
         "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1}


def parse_vod(text):
    """VOD id from a bare id or a twitch.tv VOD URL; anything else is refused."""
    text = text.strip()
    if re.fullmatch(r"\d{5,14}", text):
        return text
    m = VOD_URL.match(text)
    if m:
        return m.group(1)
    if re.search(r"twitch\.tv/.*clip|clips\.twitch\.tv", text):
        raise ValueError("Twitch clips aren't supported -- give the VOD link (twitch.tv/videos/ID)")
    raise ValueError("not a Twitch VOD link or id: %r" % text)


def parse_time(text):
    """Seconds from '01:14:00', '74:00', '1h14m', '2m30s', '2 mins 30 sec', '150'."""
    t = text.strip().lower()
    if re.fullmatch(r"\d+(?::\d{1,2}){1,2}(?:\.\d+)?", t):
        parts = [float(p) for p in t.split(":")]
        if any(p >= 60 for p in parts[1:]):
            raise ValueError("minutes/seconds must be under 60: %r" % text)
        secs = 0.0
        for p in parts:
            secs = secs * 60 + p
        return secs
    tokens = re.findall(r"(\d+(?:\.\d+)?)\s*([a-z]*)\s*", t)
    if not tokens or "".join(a + b for a, b in tokens) != re.sub(r"\s+", "", t):
        raise ValueError("can't read time %r (try 01:14:00, 1h14m or 2m30s)" % text)
    secs = 0.0
    for num, unit in tokens:
        if unit and unit not in UNITS:
            raise ValueError("unknown time unit %r in %r" % (unit, text))
        secs += float(num) * UNITS.get(unit, 1)
    return secs


def hms(secs):
    secs = int(round(secs))
    return "%02d:%02d:%02d" % (secs // 3600, secs % 3600 // 60, secs % 60)


def span_label(secs):
    secs = int(round(secs))
    h, m, s = secs // 3600, secs % 3600 // 60, secs % 60
    return "".join("%d%s" % (v, u) for v, u in ((h, "h"), (m, "m"), (s, "s")) if v) or "0s"


def project_name(vod, start, length):
    s = int(round(start))
    start_label = "%02dh%02dm" % (s // 3600, s % 3600 // 60) + ("%02ds" % (s % 60) if s % 60 else "")
    return "vod%s_%s_%s" % (vod, start_label, span_label(length))


# ---------------------------------------------------------------- files

def fingerprint(path):
    """Content fingerprint. Small files: full sha256. Big media: size + 3 sampled 4 MB
    blocks (hashing a 30 GB file on every status would take minutes)."""
    size = os.path.getsize(path)
    h = hashlib.sha256()
    with open(path, "rb") as f:
        if size <= 64 * 2**20:
            for block in iter(lambda: f.read(2**20), b""):
                h.update(block)
            return "sha256:" + h.hexdigest()
        h.update(str(size).encode())
        for pos in (0, size // 2, size - 4 * 2**20):
            f.seek(pos)
            h.update(f.read(4 * 2**20))
    return "quick:" + h.hexdigest()


def fp_json(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]


def write_json_atomic(path, data):
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    dfd = os.open(os.path.dirname(os.path.abspath(path)), os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------- config

def load_tools():
    path = os.environ.get("VP_TOOLS") or os.path.join(ROOT, "tools.json")
    tools = {}
    for k, v in load_json(path).items():
        if k.startswith("_"):
            continue
        v = os.path.expanduser(v)
        if "/" in v:
            tools[k] = v if os.path.isabs(v) else os.path.join(ROOT, v)
        else:
            tools[k] = shutil.which(v) or v
    return tools


def root_path(p):
    return p if os.path.isabs(p) else os.path.join(ROOT, p)


def streamers_dir():
    return os.environ.get("VP_STREAMERS") or os.path.join(ROOT, "streamers")


def streamer_path(name):
    return os.path.join(streamers_dir(), name + ".json")


def load_streamer(name):
    path = streamer_path(name)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,60}", name or "") or not os.path.exists(path):
        die("streamer profile not found: %s (have: %s)" % (path, ", ".join(list_streamers())))
    return load_json(path)


def list_streamers():
    d = streamers_dir()
    return sorted(f[:-5] for f in os.listdir(d)
                  if f.endswith(".json") and not f.startswith("_")) if os.path.isdir(d) else []


def streamer_for_twitch(login):
    """Twitch channel login -> profile name: the file named after it, or one listing it."""
    if not login:
        return None
    for name in list_streamers():
        st = load_json(streamer_path(name))
        if login.lower() in [name.lower()] + [x.lower() for x in st.get("twitch_logins", [])]:
            return name
    return None


LAYOUT_NAME = re.compile(r"^[a-z][a-z0-9_]{0,30}$")


def profile_layouts(st):
    """{name: layout} a streamer profile offers (docs/design-notes.md#three-output-formats-because-no-single-one-is-enough)."""
    return {k: v for k, v in (st.get("layouts") or {"normal": {"title": "Normal 16:9", "kind": "full"}}).items()
            if not k.startswith("_")}


def layout_problems(st, names):
    have = profile_layouts(st)
    out = []
    if not names:
        out.append("tick at least one layout")
    for n in names:
        if n not in have or not LAYOUT_NAME.match(n) or n == "otio":
            out.append("layout %r isn't in this streamer's profile (have: %s)" % (n, ", ".join(have)))
        elif have[n].get("kind") == "stack" and not st.get("webcam"):
            out.append("layout %s needs a webcam box in the streamer profile" % n)
    return out


# What a run makes besides the always-on steps (docs/design-notes.md#orchestrator-guarantees). Order = how the UI lists them.
OPTIONS = {
    "fcpxml": {"title": "FCPXML timelines", "group": "timeline",
               "help": "one Resolve timeline per layout (File → Import → Timeline)"},
    "otio": {"title": "OTIO backup timeline", "group": "timeline",
             "help": "the older timeline format, Normal layout only"},
    "srt": {"title": "SRT captions file", "group": "captions",
            "help": "drag onto a subtitle track on the Normal timeline"},
    "icons": {"title": "Icons on trigger words", "group": "addon",
              "help": "only works when the game pack has an icon map"},
}
DEFAULT_OPTIONS = list(OPTIONS)      # no "options" in a project/profile = everything on (as before)


def project_options(m):
    return m.get("options", DEFAULT_OPTIONS)


def option_problems(st, layouts, names):
    out = ["unknown option %r (have: %s)" % (n, ", ".join(OPTIONS)) for n in names if n not in OPTIONS]
    if not any(OPTIONS.get(n, {}).get("group") == "timeline" for n in names):
        out.append("tick at least one timeline file (%s)" % ", ".join(
            OPTIONS[n]["title"] for n in OPTIONS if OPTIONS[n]["group"] == "timeline"))
    elif st and "fcpxml" not in names and any(profile_layouts(st).get(n, {}).get("kind") == "stack"
                                             for n in layouts):
        out.append("the Short layout needs FCPXML timelines (OTIO can't draw it)")
    return out


def option_list():
    return [dict(v, name=k) for k, v in OPTIONS.items()]


def needs_webcam(st, names):
    return any(profile_layouts(st).get(n, {}).get("kind") == "stack" for n in names)


def games_dir():
    return os.environ.get("VP_GAMES") or os.path.join(ROOT, "games")


def load_game(name):
    path = os.path.join(games_dir(), name + ".json")
    if not os.path.exists(path):
        die("game pack not found: %s (have: %s)" % (path, ", ".join(list_games())))
    g = load_json(path)
    base = os.path.dirname(path)
    g["_icon_map_path"] = os.path.join(base, g["icon_map"]) if g.get("icon_map") else None
    return g


def list_games():
    d = games_dir()
    return sorted(f[:-5] for f in os.listdir(d) if f.endswith(".json")) if os.path.isdir(d) else []


def game_for_twitch(twitch_game):
    for name in list_games():
        if twitch_game in load_json(os.path.join(games_dir(), name + ".json")).get("twitch_games", []):
            return name
    return None


# ---------------------------------------------------------------- probing

def ffprobe(tools, path):
    out = subprocess.run([tools["ffprobe"], "-v", "error", "-show_entries",
                          "format=duration:stream=codec_type,codec_name,r_frame_rate",
                          "-of", "json", path], capture_output=True, text=True,
                         stdin=subprocess.DEVNULL)
    if out.returncode != 0:
        raise RuntimeError("ffprobe failed on %s: %s" % (path, out.stderr.strip()[:300]))
    info = json.loads(out.stdout)
    video = [s for s in info.get("streams", []) if s.get("codec_type") == "video"]
    audio = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
    fps = None
    if video:
        num, den = video[0]["r_frame_rate"].split("/")
        fps = float(num) / float(den) if float(den) else None
    return {"duration": float(info.get("format", {}).get("duration", 0) or 0), "fps": fps,
            "video_codec": video[0]["codec_name"] if video else None,
            "audio_codec": audio[0]["codec_name"] if audio else None}


def vod_info(tools, vod):
    """Title, length and game from Twitch (TwitchDownloaderCLI info --format raw)."""
    out = subprocess.run([tools["twitch_downloader"], "info", "--id", vod, "--format", "raw",
                          "--banner", "false"], capture_output=True, text=True, timeout=120,
                         stdin=subprocess.DEVNULL)
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            video = json.loads(line)["data"]["video"]
        except (ValueError, KeyError, TypeError):
            continue
        if video and "lengthSeconds" in video:
            return {"title": video.get("title"), "length_s": video["lengthSeconds"],
                    "twitch_game": (video.get("game") or {}).get("displayName"),
                    "streamer": (video.get("owner") or {}).get("login")}
    raise RuntimeError("Twitch has no VOD %s (deleted, sub-only or typo?)\n%s"
                       % (vod, (out.stderr or out.stdout).strip()[-400:]))


def tool_versions(tools):
    def first_line(cmd):
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                               stdin=subprocess.DEVNULL)
            return (p.stdout or p.stderr).strip().splitlines()[0][:120]
        except (OSError, IndexError, subprocess.TimeoutExpired):
            return None
    v = {"ffmpeg": first_line([tools["ffmpeg"], "-version"]),
         "python": sys.version.split()[0]}
    # whisper.cpp is installed by the user, not vendored, so its version comes from the
    # binary itself. Was `git rev-parse` against a submodule until 2026-10-23.
    if tools.get("whisper_cli"):
        v["whisper_cpp"] = first_line([tools["whisper_cli"], "--version"])
    lock = os.path.join(ROOT, "requirements.lock")
    if os.path.exists(lock):
        v["requirements_lock"] = fingerprint(lock)
    return v


# ---------------------------------------------------------------- stages
#
# Each stage: which runs it's in, its inputs (role -> (upstream stage, upstream role)),
# its outputs (role -> file pattern; {v} = version), settings that affect the output
# (params -> stale when they change), how to run it, and a check of what it produced.

PY = sys.executable


def script(name):
    """A sibling module in src/, for running it as a subprocess."""
    return os.path.join(SRC, name)


class Ctx:
    def __init__(self, pdir, manifest, tools):
        self.pdir, self.m, self.tools = pdir, manifest, tools
        self.streamer = load_streamer(manifest["streamer"])
        self.streamer_path = streamer_path(manifest["streamer"])
        self.game = load_game(manifest["game"])
        # projects from before layouts existed made the normal edit only
        self.layouts = manifest.get("layouts", ["normal"])
        self.options = project_options(manifest)

    def path(self, name):
        return os.path.join(self.pdir, name)

    def current(self, stage, role):
        return self.path(self.m["stages"][stage]["outputs"][role])


def p_download(c):
    s = c.m["source"]
    return {"vod": s["vod_id"], "start": s["start_s"], "end": s["end_s"]}


def cmd_download(c, ins, outs):
    s = c.m["source"]
    tmp = c.path(".download_tmp")
    os.makedirs(tmp, exist_ok=True)
    return [c.tools["twitch_downloader"], "videodownload", "--id", s["vod_id"],
            "-b", hms(s["start_s"]), "-e", hms(s["end_s"]), "-o", outs["video"],
            "--collision", "Exit", "--temp-path", tmp, "--banner", "false"]


def check_video(c, outs, want_s=None):
    info = ffprobe(c.tools, outs["video"])
    if not info["video_codec"] or not info["fps"]:
        return "no video stream"
    if want_s and abs(info["duration"] - want_s) > 3:
        return "duration %.1fs, expected %.1fs" % (info["duration"], want_s)
    return None


def run_import(c, ins, outs):
    src = c.m["source"]["clip"]
    try:
        os.link(src, outs["video"])
    except OSError:
        shutil.copy2(src, outs["video"])


def cmd_audio(c, ins, outs):
    return [c.tools["ffmpeg"], "-nostdin", "-hide_banner", "-n", "-i", ins["video"], "-vn",
            "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", outs["audio"]]


def check_audio(c, ins, outs):
    a, v = ffprobe(c.tools, outs["audio"]), ffprobe(c.tools, ins["video"])
    if abs(a["duration"] - v["duration"]) > 1.5:
        return "audio %.1fs vs video %.1fs" % (a["duration"], v["duration"])
    return None


def p_transcribe(c):
    w = c.streamer["whisper"]
    return {"model": w["model"], "model_fp": fingerprint(root_path(w["model"])),
            "language": w["language"], "args": w["args"]}


def cmd_transcribe(c, ins, outs):
    w = c.streamer["whisper"]
    return [c.tools["whisper_cli"], "-m", root_path(w["model"]), "-l", w["language"],
            *w["args"], "-oj", "-f", ins["audio"], "-of", outs["transcript"][:-len(".json")]]


def words_in(path):
    return [e for e in load_json(path).get("transcription", []) if e.get("text", "").strip()]


def check_transcript(c, ins, outs):
    if not words_in(outs["transcript"]):
        return "whisper heard no words (silent audio or wrong track?)"
    return None


def align_cfg(c):
    return c.streamer.get("align", {})


def p_align(c):
    a = align_cfg(c)
    return {"align": {k: v for k, v in a.items() if not k.startswith("_")},
            "vad_model_fp": fingerprint(root_path(a.get("vad_model", VAD_MODEL))),
            "script_fp": fingerprint(script("align_words.py"))}


VAD_MODEL = "whisper.cpp/models/ggml-silero-v5.1.2.bin"


def cmd_align(c, ins, outs):
    a = align_cfg(c)
    return [PY, script("align_words.py"), "--transcript", ins["transcript"], "--audio", ins["audio"],
            "--vad-tool", c.tools["whisper_vad"], "--vad-model", root_path(a.get("vad_model", VAD_MODEL)),
            "--min-silence-ms", str(a.get("min_silence_ms", 200)), "--pad-ms", str(a.get("pad_ms", 150)),
            "--min-word-ms", str(a.get("min_word_ms", 120)), "--out", outs["transcript"]]


def check_align(c, ins, outs):
    new, old = words_in(outs["transcript"]), words_in(ins["transcript"])
    if [e["text"] for e in new] != [e["text"] for e in old]:
        return "aligned transcript's words differ from whisper's"
    if any(e["offsets"]["to"] <= e["offsets"]["from"] for e in new):
        return "aligned transcript has zero-length words"
    return None


def check_captions(c, ins, outs):
    """Captions came out with cues and a non-empty transcript."""
    with open(outs["captions"], encoding="utf-8") as f:
        cues = sum(1 for line in f if "-->" in line)
    if not cues:
        return "captions file has no cues"
    if not words_in(outs["transcript"]):
        return "corrected transcript is empty"
    return None


REVIEW_FILE = "review_decisions.json"


def words_of(transcript):
    """Same word list apply_edits.py numbers its edits against."""
    return [{"i": i, "text": e["text"].strip(), "start": e["offsets"]["from"], "end": e["offsets"]["to"]}
            for i, e in enumerate(x for x in transcript["transcription"] if x["text"].strip())]


def span_words(words, k):
    first, last = (int(x) for x in k.split("-"))
    return " ".join(w["text"] for w in words[first:last + 1])


def merge_review(words, decisions):
    """The reviewer's decisions -> the final edit list, and counts.
    decisions: {"first-last": {"action": "text", "text": str}}."""
    def key(e):
        return "%d-%d" % (e["first"], e["last"])
    moved = [k for k, d in decisions.items() if "words" in d and d["words"] != span_words(words, k)]
    if moved:
        # Keys are word numbers: a re-transcribe (e.g. DTW turned on) shifts them, and a
        # decision would land on other words. Refuse instead of guessing.
        raise ValueError("%d review decision(s) were made on an older transcript (e.g. %s was %r, "
                         "now %r) -- open Review and redo them" % (
                             len(moved), moved[0], decisions[moved[0]]["words"], span_words(words, moved[0])))
    final = []
    for k, d in decisions.items():
        if d.get("action") != "text":
            continue            # accept/revert only ever applied to a model's edit
        first, last = (int(x) for x in k.split("-"))
        if not (0 <= first <= last < len(words)):
            raise ValueError("decision %s is outside the transcript" % k)
        final.append({"first": first, "last": last, "text": d["text"], "by": "human",
                      "original": " ".join(w["text"] for w in words[first:last + 1]),
                      "reason": "human, by ear"})
    final.sort(key=lambda e: e["first"])
    for a, b in zip(final, final[1:]):
        if b["first"] <= a["last"]:
            raise ValueError("decisions overlap at words %s and %s" % (key(a), key(b)))
    return final, {"human_edits": len(final)}


def load_decisions(pdir):
    p = os.path.join(pdir, REVIEW_FILE)
    return load_json(p).get("decisions", {}) if os.path.exists(p) else {}


def p_review(c):
    p = c.path(REVIEW_FILE)
    return {"decisions_fp": fingerprint(p) if os.path.exists(p) else None,
            "caption_profile_fp": fingerprint(root_path(c.streamer["caption_profile"])),
            "script_fp": fingerprint(script("apply_edits.py")),
            "captions_fp": fingerprint(script("make_captions.py"))}


def cmd_review(c, ins, outs):
    words = words_of(load_json(ins["whisper"]))
    final, stats = merge_review(words, load_decisions(c.pdir))
    write_json_atomic(outs["edits"], {
        "_comment": "REVIEW (run_pipeline.py): the reviewer's by-ear decisions, resolved against the transcript",
        "source_sha256": hashlib.sha256(open(ins["whisper"], "rb").read()).hexdigest(),
        "decisions_file": REVIEW_FILE, "stats": stats, "edits": final})
    return [PY, script("apply_edits.py"), "--transcript", ins["whisper"],
            "--human-edits", outs["edits"], "--profile", root_path(c.streamer["caption_profile"]),
            "--out", outs["transcript"], "--captions-out", outs["captions"]]


def check_review(c, ins, outs):
    rejected = load_json(outs["log"]).get("rejected", [])
    if rejected:
        return "%d review edits didn't fit the transcript: %s" % (
            len(rejected), rejected[0].get("rejected_because"))
    return check_captions(c, ins, outs)


def after_review(c, ins, outs, rec):
    rec.update(load_json(outs["edits"])["stats"])


def p_media(c):
    return {"ffmpeg_args": c.streamer["media"]["ffmpeg_args"]}


def cmd_media(c, ins, outs):
    return [c.tools["ffmpeg"], "-nostdin", "-hide_banner", "-n", "-i", ins["video"],
            "-map", "0:v:0", "-map", "0:a:0?", *c.streamer["media"]["ffmpeg_args"], outs["media"]]


def check_media(c, ins, outs):
    m, v = ffprobe(c.tools, outs["media"]), ffprobe(c.tools, ins["video"])
    if abs(m["duration"] - v["duration"]) > 1.5:
        return "media %.1fs vs source %.1fs" % (m["duration"], v["duration"])
    if m["video_codec"] in ("h264", "hevc", "av1"):
        return "media is %s, which free Resolve on Linux can't decode" % m["video_codec"]
    return None


def p_signals(c):
    return {"script_fp": fingerprint(script("audio_signals.py"))}


def cmd_signals(c, ins, outs):
    return [PY, script("audio_signals.py"), "--audio", ins["audio"],
            "--transcript", ins["transcript"], "--out", outs["signals"]]


def check_json_rows(key):
    def check(c, ins, outs):
        role = next(iter(outs))
        try:
            data = json.load(open(outs[role]), parse_constant=lambda t: 1 / 0)
        except (ValueError, ZeroDivisionError):
            return "%s is not valid JSON or contains NaN" % os.path.basename(outs[role])
        return None if data.get(key) else "%s has no %s" % (os.path.basename(outs[role]), key)
    return check


def p_score(c):
    return {"scorer": c.streamer.get("scorer", {}), "script_fp": fingerprint(script("score_cutlist.py"))}


def cmd_score(c, ins, outs):
    fps = ffprobe(c.tools, ins["media"])["fps"]
    extra = []
    for k, v in sorted(c.streamer.get("scorer", {}).items()):
        if not k.startswith("_"):
            extra += ["--" + k.replace("_", "-"), str(v)]
    return [PY, script("score_cutlist.py"), "--signals", ins["signals"],
            "--media", os.path.basename(ins["media"]), "--fps", "%g" % fps,
            "--out", outs["cutlist"], *extra]


def run_cut(c, ins, outs):
    """Pre-cut clip: the whole clip is the highlight -- everything on V1."""
    info = ffprobe(c.tools, ins["media"])
    write_json_atomic(outs["cutlist"], {
        "_comment": "PRE-CUT CLIP (run_pipeline.py): whole clip on V1, no scoring",
        "media": os.path.basename(ins["media"]), "fps": info["fps"], "duration": info["duration"],
        "segments": [{"start": 0.0, "end": round(info["duration"], 3), "track": "V1",
                      "reason": "pre-cut clip"}]})


def p_icons(c):
    m = c.game["_icon_map_path"]
    out = {"icon_map": m and fingerprint(m), "script_fp": fingerprint(script("make_graphics.py"))}
    if "icons" not in c.options:
        out["off"] = True        # only when off: projects from before options don't go stale
    return out


def icons_skip(c):
    if "icons" not in c.options:
        return "icons not ticked"
    if not c.game["_icon_map_path"]:
        return "game pack %s has no icon map" % c.game["name"]
    return None


def cmd_icons(c, ins, outs):
    return [PY, script("make_graphics.py"), "--transcript", ins["transcript"],
            "--map", c.game["_icon_map_path"], "--out", outs["graphics"]]


def check_graphics(c, ins, outs):
    # zero icons is a valid result (no trigger words said); a missing key is not
    return None if "items" in load_json(outs["graphics"]) else "graphics file has no items list"


def p_webcam(c):
    return {"webcam": c.streamer.get("webcam"), "script_fp": fingerprint(script("make_webcam.py")),
            "writer_fp": fingerprint(script("make_fcpxml.py"))}


def webcam_skip(c):
    if not needs_webcam(c.streamer, c.layouts):
        return "no stacked layout (Short) ticked"
    return None


def cmd_webcam(c, ins, outs):
    return [PY, script("make_webcam.py"), "--cutlist", ins["cutlist"], "--media", ins["media"],
            "--profile", c.streamer_path, "--out", outs["webcam"],
            "--ffmpeg", c.tools["ffmpeg"], "--ffprobe", c.tools["ffprobe"]]


def check_webcam(c, ins, outs):
    info = ffprobe(c.tools, outs["webcam"])
    return None if info["video_codec"] == "mpeg4" else "webcam clip is %s, not mpeg4" % info["video_codec"]


def timeline_outputs(m):
    """One FCPXML per ticked layout + the OTIO backup (kept until a full-VOD FCPXML import works),
    each only when ticked (docs/design-notes.md#orchestrator-guarantees)."""
    opts = project_options(m)
    out = {"otio": "timeline_v{v}.otio"} if "otio" in opts else {}
    if "fcpxml" in opts:
        for n in m.get("layouts", ["normal"]):
            out[n] = "timeline_%s_v{v}.fcpxml" % n
    return out


def p_timeline(c):
    lays = profile_layouts(c.streamer)
    fmts = [n for n in ("fcpxml", "otio") if n in c.options]
    return {"layouts": c.layouts, "layout_cfg": {n: lays.get(n) for n in c.layouts},
            "script_fp": fingerprint(script("make_fcpxml.py")),
            "otio_fp": fingerprint(script("make_timeline.py")),
            **({"formats": fmts} if fmts != ["fcpxml", "otio"] else {})}  # only when changed: no churn


def cmd_timeline(c, ins, outs):
    cmd = [PY, script("make_fcpxml.py"), "--cutlist", ins["cutlist"], "--media", ins["media"],
           "--profile", c.streamer_path, "--captions", ins["captions"], "--name", os.path.basename(c.pdir),
           "--ffprobe", c.tools["ffprobe"]]
    if "otio" in outs:
        cmd += ["--otio-out", outs["otio"]]
    for n in c.layouts:
        if n in outs:
            cmd += ["--layout", "%s=%s" % (n, outs[n])]
    if ins.get("webcam"):
        cmd += ["--webcam", ins["webcam"]]
    if ins.get("graphics"):
        cmd += ["--graphics", ins["graphics"]]
    return cmd


def check_timeline(c, ins, outs):
    import xml.etree.ElementTree as ET
    if "otio" in outs and not load_json(outs["otio"]).get("OTIO_SCHEMA", "").startswith("Timeline"):
        return "not an OTIO timeline"
    for n in [x for x in c.layouts if x in outs]:
        try:
            root = ET.parse(outs[n]).getroot()
        except ET.ParseError as e:
            return "%s is not valid XML: %s" % (os.path.basename(outs[n]), e)
        if root.tag != "fcpxml" or root.find(".//sequence/spine") is None:
            return "%s has no timeline in it" % os.path.basename(outs[n])
    return None


STAGES = [
    dict(name="download", routes=("vod",), inputs={}, outputs={"video": "clip_v{v}.mp4"},
         params=p_download, cmd=cmd_download,
         check=lambda c, i, o: check_video(c, o, c.m["source"]["end_s"] - c.m["source"]["start_s"]),
         est_time=0.25, est_bytes=0.6e6),
    dict(name="import", routes=("clip",), inputs={}, outputs={"video": "clip_v{v}{ext}"},
         params=lambda c: {"clip_fp": fingerprint(c.m["source"]["clip"])}, run=run_import,
         check=lambda c, i, o: check_video(c, o), est_time=0.0, est_bytes=0.6e6),
    dict(name="audio", inputs={"video": ("@video", "video")}, outputs={"audio": "audio_v{v}.wav"},
         params=lambda c: {}, cmd=cmd_audio, check=check_audio, est_time=0.01, est_bytes=32e3),
    dict(name="transcribe", inputs={"audio": ("audio", "audio")},
         outputs={"transcript": "transcript_v{v}.json"}, params=p_transcribe,
         cmd=cmd_transcribe, check=check_transcript, est_time=3.2, est_bytes=150),
    # Word times that follow the voice (docs/design-notes.md#animated-captions-via-drt step 0).
    # review applies the reviewer's edits to the aligned transcript -> SRT, captions and
    # signals all get the voice-following timing.
    dict(name="align", inputs={"audio": ("audio", "audio"), "transcript": ("transcribe", "transcript")},
         outputs={"transcript": "transcript_aligned_v{v}.json"}, params=p_align, cmd=cmd_align,
         check=check_align, est_time=0.01, est_bytes=300),
    dict(name="review", inputs={"whisper": ("align", "transcript")},
         outputs={"edits": "review_edits_v{v}.json", "transcript": "transcript_final_v{v}.json",
                  "log": "transcript_final_v{v}.corrections.json", "captions": "captions_final_v{v}.srt"},
         params=p_review, cmd=cmd_review, check=check_review, after=after_review,
         est_time=0.005, est_bytes=200),
    dict(name="media", inputs={"video": ("@video", "video")}, outputs={"media": "clip_edit_v{v}.mov"},
         params=p_media, cmd=cmd_media, check=check_media, est_time=0.3, est_bytes=1.34e6),
    dict(name="signals", routes=("vod",), inputs={"audio": ("audio", "audio"),
                                                  "transcript": ("review", "transcript")},
         outputs={"signals": "audio_signals_v{v}.json"}, params=p_signals, cmd=cmd_signals,
         check=check_json_rows("rows"), est_time=0.08, est_bytes=300),
    dict(name="score", routes=("vod",), inputs={"signals": ("signals", "signals"),
                                                "media": ("media", "media")},
         outputs={"cutlist": "cutlist_v{v}.json"}, params=p_score, cmd=cmd_score,
         check=check_json_rows("segments"), est_time=0.001, est_bytes=0),
    dict(name="cut", routes=("clip",), inputs={"media": ("media", "media")},
         outputs={"cutlist": "cutlist_v{v}.json"}, params=lambda c: {}, run=run_cut,
         check=check_json_rows("segments"), est_time=0, est_bytes=0),
    dict(name="webcam", inputs={"cutlist": ("@cutlist", "cutlist"), "media": ("media", "media")},
         outputs={"webcam": "webcam_v{v}.mov"}, params=p_webcam, cmd=cmd_webcam, skip=webcam_skip,
         check=check_webcam, est_time=0.1, est_bytes=0.2e6),
    dict(name="icons", inputs={"transcript": ("review", "transcript")},
         outputs={"graphics": "graphics_v{v}.json"}, params=p_icons, cmd=cmd_icons,
         skip=icons_skip, check=check_graphics, est_time=0.001, est_bytes=0),
    dict(name="timeline", inputs={"cutlist": ("@cutlist", "cutlist"), "media": ("media", "media"),
                                  "captions": ("review", "captions"), "webcam": ("webcam?", "webcam"),
                                  "graphics": ("icons?", "graphics")},
         outputs=timeline_outputs, params=p_timeline, cmd=cmd_timeline,
         check=check_timeline, est_time=0.001, est_bytes=0),
]
STAGE = {s["name"]: s for s in STAGES}


def deliverables(m):
    """[(role, label, file)] of a finished project, in the order to use them."""
    st = m["stages"]
    out = []
    tl = st.get("timeline", {}).get("outputs", {})
    for n in m.get("layouts", ["normal"]):
        if n in tl:
            out.append((n, "Timeline -- %s" % n, tl[n]))
    if "srt" in project_options(m):
        out.append(("captions", "Captions (SRT)", st["review"]["outputs"]["captions"]))
    out.append(("media", "Video (Resolve copy)", st["media"]["outputs"]["media"]))
    if st.get("webcam", {}).get("status") == "done":
        out.append(("webcam", "Webcam clip (used by the Short)", st["webcam"]["outputs"]["webcam"]))
    if "otio" in tl:
        out.append(("otio", "OTIO timeline (backup)", tl["otio"]))
    return out


def route_stages(route):
    return [s for s in STAGES if route in s.get("routes", ("vod", "clip"))]


def upstream_name(ref, route):
    """'@video' = the route's video stage; '@cutlist' = score or cut; 'icons?' = optional."""
    name = ref.rstrip("?")
    if name == "@video":
        return "download" if route == "vod" else "import"
    if name == "@cutlist":
        return "score" if route == "vod" else "cut"
    return name


# ---------------------------------------------------------------- evaluation (read-only)

def evaluate(pdir, m, live):
    """Per stage: (state, reason). States: done, stale, missing, waiting, running,
    interrupted, failed, skipped. Reads only -- safe while a run is going."""
    route, out = m["route"], {}
    try:
        c = Ctx(pdir, m, None)
    except SystemExit:
        return {s["name"]: ("failed", "streamer/game config missing") for s in route_stages(route)}
    for s in route_stages(route):
        rec = m["stages"].get(s["name"])
        if rec and rec["status"] == "running":
            out[s["name"]] = ("running", "") if live else ("interrupted", "run stopped mid-stage")
            continue
        if rec and rec["status"] in ("failed", "interrupted"):
            out[s["name"]] = (rec["status"], rec.get("error", ""))
            continue
        if not rec:
            out[s["name"]] = ("waiting", "")
            continue
        if rec["status"] == "skipped":
            # Nothing was built, so there are no inputs to compare: it stays skipped while
            # the reason holds and the settings (e.g. the game's icon map) are unchanged.
            try:
                skip = s.get("skip") and s["skip"](c)
                same = rec.get("params_fp") == fp_json(s["params"](c))
            except (OSError, KeyError):
                skip, same = None, False
            out[s["name"]] = ("skipped", skip) if skip and same else ("stale", "no longer skipped")
            continue
        reason = None
        for role, (ref, urole) in s["inputs"].items():
            up = upstream_name(ref, route)
            urec = m["stages"].get(up)
            if ref.endswith("?") and (not urec or urec["status"] == "skipped"):
                if role in rec.get("inputs", {}):
                    reason = "%s is now skipped" % up
                continue
            if out.get(up, ("waiting",))[0] not in ("done",):
                reason = "%s is %s" % (up, out.get(up, ("waiting",))[0])
                break
            cur = urec["outputs"][urole]
            built = rec.get("inputs", {}).get(role)
            if not built:
                reason = "has no record of its %s input" % role
                break
            try:
                cur_fp = fingerprint(os.path.join(pdir, cur))
            except OSError:
                reason = "%s is missing" % cur
                break
            # Content decides, not the name: a rebuilt upstream that came out identical
            # (e.g. the same transcript text as v2) must not restage everything downstream.
            if built["fp"] != cur_fp:
                reason = ("%s changed since" % cur if built["file"] == cur
                          else "built from %s, current is %s" % (built["file"], cur))
                break
        note = ""
        if not reason:
            try:
                if rec.get("params_fp") != fp_json(s["params"](c)):
                    reason = "settings changed (profile, game pack, or script)"
            except (OSError, KeyError) as e:
                reason = "can't read settings: %s" % e
        if not reason:
            gone = [f for f in rec["outputs"].values() if not os.path.exists(os.path.join(pdir, f))]
            if gone:
                out[s["name"]] = ("missing", ", ".join(gone) + " deleted")
                continue
        out[s["name"]] = ("stale", reason) if reason else ("done", note)
    return out


def overall(states):
    vals = [st for st, _ in states.values()]
    for s in ("running", "interrupted", "failed"):
        if s in vals:
            return s
    if all(v in ("done", "skipped") for v in vals):
        return "done"
    if any(v in ("stale", "missing") for v in vals):
        return "stale"
    return "incomplete"


# ---------------------------------------------------------------- lock / liveness

def lock_project(pdir):
    """Exclusive, non-blocking. Held for the whole run; the kernel drops it if we die,
    so a lock we CAN take means nobody is running this project."""
    fd = os.open(os.path.join(pdir, ".lock"), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def is_live(pdir):
    fd = lock_project(pdir)
    if fd is None:
        return True
    os.close(fd)
    return False


def proc_start(pid):
    try:
        with open("/proc/%d/stat" % pid) as f:
            return int(f.read().rsplit(")", 1)[1].split()[19])
    except (OSError, IndexError, ValueError):
        return None


def boot_id():
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip()
    except OSError:
        return None


# ---------------------------------------------------------------- running

class Cancelled(Exception):
    pass


class Runner:
    def __init__(self, pdir, m, tools):
        self.pdir, self.m, self.tools = pdir, m, tools
        self.child = None
        self.cancelled = False
        self.runlog = open(os.path.join(pdir, "logs", "run.log"), "a", encoding="utf-8")

    def say(self, msg):
        line = "%s  %s" % (time.strftime("%H:%M:%S"), msg)
        print(line, flush=True)
        self.runlog.write(line + "\n")
        self.runlog.flush()

    def save(self):
        write_json_atomic(os.path.join(self.pdir, "project.json"), self.m)

    def on_signal(self, signum, frame):
        self.cancelled = True
        if self.child and self.child.poll() is None:
            try:
                os.killpg(self.child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def next_version(self, s):
        seen = [h["version"] for h in self.m["history"] if h["stage"] == s["name"]]
        rec = self.m["stages"].get(s["name"])
        v = max(seen + [rec["version"] if rec else 0]) + 1
        while any(os.path.exists(os.path.join(self.pdir, f)) for f in self.out_names(s, v).values()):
            v += 1
        return v

    def out_names(self, s, v):
        ext = os.path.splitext(self.m["source"].get("clip", ""))[1] or ".mp4"
        pats = s["outputs"](self.m) if callable(s["outputs"]) else s["outputs"]
        return {r: p.format(v=v, ext=ext) for r, p in pats.items()}

    def run_stage(self, c, s):
        name, route = s["name"], self.m["route"]
        ins, ins_rec = {}, {}
        for role, (ref, urole) in s["inputs"].items():
            up = upstream_name(ref, route)
            urec = self.m["stages"].get(up)
            if ref.endswith("?") and (not urec or urec["status"] == "skipped"):
                continue
            f = urec["outputs"][urole]
            ins[role] = os.path.join(self.pdir, f)
            ins_rec[role] = {"file": f, "fp": fingerprint(ins[role])}
        params_fp = fp_json(s["params"](c))
        old = self.m["stages"].get(name)
        if old and old["status"] in ("done", "skipped"):
            self.m["history"].append(dict(old, stage=name))

        skip = s.get("skip") and s["skip"](c)
        if skip:
            self.m["stages"][name] = {"status": "skipped", "version": old["version"] if old else 0,
                                      "outputs": {}, "inputs": {}, "params_fp": params_fp,
                                      "note": skip, "finished": now()}
            self.save()
            self.say("⤼ %-10s skipped: %s" % (name, skip))
            return

        v = self.next_version(s)
        names = self.out_names(s, v)
        outs = {r: os.path.join(self.pdir, f) for r, f in names.items()}
        log_name = os.path.join("logs", "%s_v%d.log" % (name, v))
        rec = {"status": "running", "version": v, "outputs": names, "inputs": ins_rec,
               "params_fp": params_fp, "started": now(), "log": log_name,
               "pid": os.getpid(), "pid_start": proc_start(os.getpid()), "boot_id": boot_id()}
        self.m["stages"][name] = rec
        self.save()
        self.say("▶ %-10s v%d → %s" % (name, v, ", ".join(names.values())))
        t0 = time.time()
        error = None
        try:
            with open(os.path.join(self.pdir, log_name), "w", encoding="utf-8") as log:
                if "cmd" in s:
                    cmd = s["cmd"](c, ins, outs)
                    log.write("$ %s\n\n" % " ".join(cmd))
                    log.flush()
                    self.child = subprocess.Popen(cmd, cwd=self.pdir, stdin=subprocess.DEVNULL,
                                                  stdout=log, stderr=subprocess.STDOUT,
                                                  start_new_session=True)
                    while True:
                        try:
                            code = self.child.wait(timeout=1)
                            break
                        except subprocess.TimeoutExpired:
                            if self.cancelled:
                                self.stop_child()
                    self.child = None
                    if self.cancelled:
                        raise Cancelled()
                    if code != 0:
                        error = "exit %d -- %s" % (code, tail(os.path.join(self.pdir, log_name)))
                else:
                    s["run"](c, ins, outs)
            if not error:
                missing = [f for f in names.values() if not os.path.exists(os.path.join(self.pdir, f))]
                error = ("did not write " + ", ".join(missing)) if missing else s["check"](c, ins, outs)
        except Cancelled:
            self.cleanup(names)
            rec.update(status="interrupted", error="cancelled", finished=now())
            self.save()
            self.say("■ %-10s cancelled; partial files removed" % name)
            raise
        except Exception as e:  # a stage bug must not leave the manifest saying "running"
            error = "%s: %s" % (type(e).__name__, e)
        rec["seconds"] = round(time.time() - t0, 1)
        rec["finished"] = now()
        if error:
            self.cleanup(names)
            rec.update(status="failed", error=error)
            self.save()
            self.say("✗ %-10s failed after %s: %s" % (name, dur(rec["seconds"]), error))
            return error
        if s.get("after"):
            s["after"](c, ins, outs, rec)
        rec["status"] = "done"
        for k in ("pid", "pid_start", "boot_id"):
            rec.pop(k, None)
        self.save()
        self.say("✓ %-10s %s" % (name, dur(rec["seconds"])))

    def stop_child(self):
        try:
            os.killpg(self.child.pid, signal.SIGTERM)
            self.child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(self.child.pid, signal.SIGKILL)
            self.child.wait()
        except ProcessLookupError:
            pass

    def cleanup(self, names):
        """Remove this attempt's files. Safe: the names were unused when it started."""
        for f in names.values():
            p = os.path.join(self.pdir, f)
            if os.path.exists(p):
                os.remove(p)
        shutil.rmtree(os.path.join(self.pdir, ".download_tmp"), ignore_errors=True)

    def run(self, force_from=None):
        c = Ctx(self.pdir, self.m, self.tools)
        stages = route_stages(self.m["route"])
        names = [s["name"] for s in stages]
        if force_from and force_from not in names:
            # Without this, names.index() raises and the run dies on a traceback. Stages the
            # route doesn't have (and ones removed since, e.g. the old `correct`) land here.
            die("no stage %r on the %s route -- stages are: %s"
                % (force_from, self.m["route"], ", ".join(names)))
        forced = set(names[names.index(force_from):]) if force_from else set()
        self.m["run"] = {"state": "running", "pid": os.getpid(), "pid_start": proc_start(os.getpid()),
                         "boot_id": boot_id(), "started": now()}
        for k in [k for k, v in self.m["stages"].items() if v["status"] == "running"]:
            # We hold the lock, so that attempt's process is dead (crash, power loss,
            # SIGKILL) and never cleaned up after itself. Its names were unused when it
            # started, so removing them can't touch anything finished.
            self.cleanup(self.m["stages"][k]["outputs"])
            self.m["stages"][k].update(status="interrupted", error="run stopped mid-stage")
            self.say("⏸ %-10s was interrupted; its partial files removed" % k)
        self.save()
        result = "done"
        try:
            for s in stages:
                state, reason = evaluate(self.pdir, self.m, live=True)[s["name"]]
                if state in ("done", "skipped") and s["name"] not in forced:
                    continue
                if state == "stale":
                    self.say("↻ %-10s stale: %s" % (s["name"], reason))
                if self.run_stage(c, s):
                    result = "failed"
                    break
        except Cancelled:
            result = "cancelled"
        self.m["run"].update(state=result, finished=now())
        self.save()
        return result


def tail(path, n=6):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = [ln.rstrip() for ln in f.read().replace("\r", "\n").splitlines() if ln.strip()]
        return " | ".join(lines[-n:])
    except OSError:
        return ""


def dur(secs):
    secs = int(round(secs or 0))
    if secs < 60:
        return "%ds" % secs
    if secs < 3600:
        return "%dm%02ds" % (secs // 60, secs % 60)
    return "%dh%02dm" % (secs // 3600, secs % 3600 // 60)


# ---------------------------------------------------------------- preflight / estimate

def learned_factors():
    """Seconds of work per second of footage, per stage, from finished runs (UX-2)."""
    seen = {}
    root = projects_dir()
    for name in os.listdir(root) if os.path.isdir(root) else []:
        mp = os.path.join(root, name, "project.json")
        if not os.path.exists(mp):
            continue
        try:
            m = load_json(mp)
        except ValueError:
            continue
        length = m.get("source", {}).get("length_s")
        for st, rec in m.get("stages", {}).items():
            if length and rec.get("status") == "done" and rec.get("seconds") is not None:
                seen.setdefault(st, []).append(rec["seconds"] / length)
    return {k: sorted(v)[len(v) // 2] for k, v in seen.items()}


def estimate(route, length, skip=()):
    learned = learned_factors()
    secs = 0.0
    nbytes = 0.0
    for s in route_stages(route):
        if s["name"] in skip:
            continue
        secs += learned.get(s["name"], s["est_time"]) * length
        nbytes += s["est_bytes"] * length
    return {"seconds": secs, "bytes": nbytes,
            "learned": sorted(set(learned) & {s["name"] for s in route_stages(route)})}


def on_battery():
    base = "/sys/class/power_supply"
    try:
        for d in os.listdir(base):
            with open(os.path.join(base, d, "type")) as f:
                if f.read().strip() == "Mains":
                    with open(os.path.join(base, d, "online")) as g:
                        return g.read().strip() == "0"
    except OSError:
        pass
    return False


def missing_tool(tools, name):
    """The preflight problem string for an unusable tool, or None if it is fine. Shared so
    checks that run before preflight (the VOD lookup) report it in the same words."""
    p = tools.get(name)
    if not p or not (os.path.isfile(p) and os.access(p, os.X_OK)):
        return "tool %s not found: %s (tools.json)" % (name, p)
    return None


def preflight(m, tools, est):
    """Returns (problems, warnings). Problems block the run."""
    problems, warnings = [], []
    need = ["ffmpeg", "ffprobe", "whisper_cli", "whisper_vad"]
    if m["route"] == "vod":
        need.append("twitch_downloader")
    problems += [p for p in (missing_tool(tools, t) for t in need) if p]
    try:
        g = load_game(m["game"])
        checks = [("icon map", g["_icon_map_path"])]
        if m.get("streamer"):
            st = load_streamer(m["streamer"])
            checks += [("whisper model", root_path(st["whisper"]["model"])),
                       ("VAD model", root_path(st.get("align", {}).get("vad_model", VAD_MODEL))),
                       ("caption profile", root_path(st["caption_profile"]))]
            problems += layout_problems(st, m.get("layouts", ["normal"]))
            problems += option_problems(st, m.get("layouts", ["normal"]), project_options(m))
        for label, p in checks:
            if p and not os.path.exists(p):
                problems.append("%s not found: %s" % (label, p))
    except SystemExit:
        problems.append("streamer profile or game pack missing (see the error above)")
    root = projects_dir()
    # projects/ is gitignored, so it does not exist in a fresh checkout -- measure the nearest
    # existing ancestor instead of creating it, so --dry-run still writes nothing.
    probe = os.path.abspath(root)
    while not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    free = shutil.disk_usage(probe).free
    need_bytes = est["bytes"] * 1.15 + 2e9
    if free < need_bytes:
        problems.append("disk: needs ~%.1f GB, %.1f GB free" % (need_bytes / 1e9, free / 1e9))
    if on_battery():
        warnings.append("on battery -- plug in; a long run will drain it")
    if not shutil.which("systemd-inhibit"):
        warnings.append("systemd-inhibit missing: the laptop may sleep mid-run")
    return problems, warnings


# ---------------------------------------------------------------- commands

def new_project(args):
    tools = load_tools()
    if bool(args.vod) == bool(args.clip):
        die("give exactly one of --vod or --clip")
    if args.vod:
        try:
            vod = parse_vod(args.vod)
            start = parse_time(args.start) if args.start else die("--start is required with --vod")
            if args.end and args.duration:
                die("give --end or --duration, not both")
            if args.end:
                end = parse_time(args.end)
            elif args.duration:
                end = start + parse_time(args.duration)
            else:
                die("give --end or --duration")
        except ValueError as e:
            die(str(e))
        if end <= start:
            die("end (%s) must be after start (%s)" % (hms(end), hms(start)))
        length = end - start
        if length > MAX_RANGE_S and not args.allow_long:
            die("range is %s; over 30 min isn't safe yet (the audio signals stage is not "
                "windowed and may run out of RAM -- audit F-05/F-03). --allow-long to force"
                % span_label(length))
        # This lookup runs before preflight (the metadata decides the game pack and the
        # project name), so check its tool here too or an unedited tools.json placeholder
        # surfaces as a bare "[Errno 2] No such file or directory" instead of the real cause.
        bad = missing_tool(tools, "twitch_downloader")
        if bad:
            die(bad)
        print("checking VOD %s on Twitch..." % vod)
        try:
            info = vod_info(tools, vod)
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
            die(str(e))
        if end > info["length_s"]:
            die("end %s is past the VOD's length %s" % (hms(end), hms(info["length_s"])))
        game = args.game or game_for_twitch(info["twitch_game"])
        if not game:
            die("no game pack for Twitch game %r -- pick one with --game (have: %s) or add it to "
                "a pack's twitch_games" % (info["twitch_game"], ", ".join(list_games())))
        source = {"vod_id": vod, "url": "https://www.twitch.tv/videos/" + vod, "start_s": start,
                  "end_s": end, "length_s": length, "title": info["title"],
                  "twitch_game": info["twitch_game"], "vod_length_s": info["length_s"],
                  "twitch_login": info.get("streamer")}
        name = args.name or project_name(vod, start, length)
        route = "vod"
    else:
        clip = os.path.abspath(args.clip)
        if not os.path.isfile(clip):
            die("clip not found: %s" % clip)
        if not args.game:
            die("--game is required with --clip (have: %s)" % ", ".join(list_games()))
        game = args.game
        info = ffprobe(tools, clip)
        if not info["video_codec"]:
            die("no video stream in %s" % clip)
        source = {"clip": clip, "length_s": info["duration"], "title": args.title}
        name = args.name or re.sub(r"[^A-Za-z0-9_-]+", "_", os.path.splitext(os.path.basename(clip))[0])
        route = "clip"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,80}", name):
        die("project name may only use letters, digits, _ and -: %r" % name)
    load_game(game)
    streamer, streamer_source, pick_problem = pick_streamer(args, source)
    st = load_streamer(streamer) if streamer else None
    layouts = ([x.strip() for x in args.layouts.split(",") if x.strip()] if args.layouts
               else (st or {}).get("default_layouts") or ["normal"])
    options = ([x.strip() for x in args.options.split(",") if x.strip()] if args.options is not None
               else (st or {}).get("default_options") or DEFAULT_OPTIONS)
    m = {"schema_version": SCHEMA_VERSION, "created": now(), "route": route, "source": source,
         "streamer": streamer, "streamer_source": streamer_source, "layouts": layouts,
         "options": options, "game": game,
         "stages": {}, "history": [], "run": {}}
    est = estimate(route, source["length_s"],
                   skip=() if st and needs_webcam(st, layouts) else ("webcam",))
    problems, warnings = preflight(m, tools, est)
    if pick_problem:
        problems.insert(0, pick_problem)
    pdir = os.path.join(projects_dir(), name)
    if os.path.isdir(pdir) and not os.path.exists(os.path.join(pdir, "project.json")):
        problems.append("folder %s was made by hand (no project.json) -- rename or move it, or use "
                        "--name for another project name" % name)

    if args.json:
        opts = [{"name": n, "title": l.get("title", n), "kind": l.get("kind")}
                for n, l in profile_layouts(st).items()] if st else []
        print(json.dumps({"name": name, "route": route, "source": source, "game": game,
                          "streamer": streamer, "streamer_source": streamer_source,
                          "layouts": layouts, "layout_options": opts, "estimate": est,
                          "options": options, "option_list": option_list(),
                          "problems": problems, "warnings": warnings,
                          "exists": os.path.exists(os.path.join(pdir, "project.json"))}))
        if args.dry_run:
            return 0
    else:
        print_plan(name, m, est, problems, warnings)
    if os.path.exists(os.path.join(pdir, "project.json")):
        die("project %s already exists -- `status %s` / `resume %s`" % (name, name, name))
    if os.path.exists(pdir):
        die("folder %s exists but was made by hand (no project.json) -- pick another --name" % name)
    if problems:
        die("preflight failed -- nothing was started")
    if args.dry_run:
        print("(dry run: nothing created)")
        return 0
    os.makedirs(os.path.join(pdir, "logs"))
    m["tools"] = tool_versions(tools)
    write_json_atomic(os.path.join(pdir, "project.json"), m)
    return start_run(pdir, tools)


def pick_streamer(args, source):
    """-> (profile name or None, how it was picked, problem or None). Never guesses: a
    channel with no profile gets none, so another streamer's webcam box is never used."""
    have = list_streamers()
    if args.streamer:
        if args.streamer not in have:
            return None, None, "no streamer profile %r (have: %s)" % (args.streamer, ", ".join(have))
        return args.streamer, "chosen", None
    login = source.get("twitch_login")
    if "vod_id" in source:
        found = streamer_for_twitch(login)
        if found:
            return found, "twitch", None
        return None, None, ("no streamer profile for Twitch channel %r -- pick one (have: %s)"
                            % (login, ", ".join(have) or "none"))
    if len(have) == 1:
        return have[0], "only profile", None
    return None, None, "pick a streamer profile with --streamer (have: %s)" % ", ".join(have)


def print_plan(name, m, est, problems, warnings):
    s = m["source"]
    print("\n  project   %s" % name)
    if m["route"] == "vod":
        print("  VOD       %s  \"%s\"" % (s["url"], s["title"]))
        print("  range     %s → %s  (%s)" % (hms(s["start_s"]), hms(s["end_s"]), span_label(s["length_s"])))
    else:
        print("  clip      %s  (%s)" % (s["clip"], span_label(s["length_s"])))
    print("  profile   %s × %s%s" % (m["streamer"], m["game"],
                                     "  (from the Twitch channel)" if m.get("streamer_source") == "twitch" else ""))
    print("  layouts   %s" % ", ".join(m.get("layouts", [])))
    print("  options   %s" % ", ".join(project_options(m)))
    print("  estimate  ~%s · ~%.1f GB%s" % (
        dur(est["seconds"]), est["bytes"] / 1e9,
        "  (learned: %s)" % ", ".join(est["learned"]) if est["learned"] else "  (default speeds)"))
    for w in warnings:
        print("  ⚠️  %s" % w)
    for p in problems:
        print("  ❌ %s" % p)
    print()


def open_project(name):
    pdir = os.path.join(projects_dir(), name)
    mp = os.path.join(pdir, "project.json")
    if not os.path.exists(mp):
        die("no project.json in %s%s" % (pdir, " (made by hand before the orchestrator)"
                                         if os.path.isdir(pdir) else ""))
    m = load_json(mp)
    if m.get("schema_version") != SCHEMA_VERSION:
        die("%s is schema %s; this orchestrator reads %d" % (name, m.get("schema_version"), SCHEMA_VERSION))
    return pdir, m


def start_run(pdir, tools, force_from=None):
    """Re-exec under systemd-inhibit (so the laptop won't sleep), then run with the lock."""
    if not os.environ.get("VP_INHIBITED") and shutil.which("systemd-inhibit") \
            and not os.environ.get("VP_NO_INHIBIT"):
        argv = [sys.executable, os.path.abspath(__file__), "resume", os.path.basename(pdir)]
        if force_from:
            argv += ["--from", force_from]
        env = dict(os.environ, VP_INHIBITED="1")
        if os.environ.get("VP_PROJECTS") is None:
            env["VP_PROJECTS"] = projects_dir()
        inhibit = ["systemd-inhibit", "--what=sleep:idle", "--who=video-editing-pipeline",
                   "--why=rendering %s" % os.path.basename(pdir), "--mode=block"]
        sys.stdout.flush()
        os.execvpe(inhibit[0], inhibit + argv, env)
    return run_locked(pdir, tools, force_from)


def run_locked(pdir, tools, force_from=None):
    fd = lock_project(pdir)
    if fd is None:
        die("%s is already running (another process holds its lock)" % os.path.basename(pdir))
    try:
        m = load_json(os.path.join(pdir, "project.json"))
        os.makedirs(os.path.join(pdir, "logs"), exist_ok=True)
        r = Runner(pdir, m, tools)
        signal.signal(signal.SIGTERM, r.on_signal)
        signal.signal(signal.SIGINT, r.on_signal)
        signal.signal(signal.SIGHUP, r.on_signal)
        r.say("run started (%s)" % ("rebuild from " + force_from if force_from else "resume/new"))
        result = r.run(force_from)
        r.say("run %s" % result)
    finally:
        os.close(fd)
    print()
    show_project(pdir, load_json(os.path.join(pdir, "project.json")))
    return {"done": 0, "cancelled": 130}.get(result, 1)


def cmd_resume(args):
    pdir, m = open_project(args.project)
    if args.__dict__.get("from") and args.__dict__["from"] not in [s["name"] for s in route_stages(m["route"])]:
        die("unknown stage %r for a %s run" % (args.__dict__["from"], m["route"]))
    tools = load_tools()
    problems, warnings = preflight(m, tools, {"bytes": 0})
    for w in warnings:
        print("⚠️  " + w)
    if problems:
        die("preflight failed:\n  " + "\n  ".join(problems))
    if os.environ.get("VP_INHIBITED"):
        return run_locked(pdir, tools, args.__dict__.get("from"))
    return start_run(pdir, tools, args.__dict__.get("from"))


def cmd_layouts(args):
    return cmd_choose(argparse.Namespace(project=args.project, layouts=args.layouts, options=None))


def cmd_choose(args):
    """Change which layouts / options a project makes, then rebuild only what that needs (the
    webcam clip, icons and the timelines)."""
    pdir, m = open_project(args.project)
    split = lambda v: [x.strip() for x in v.split(",") if x.strip()]
    layouts = split(args.layouts) if args.layouts is not None else m.get("layouts", ["normal"])
    options = split(args.options) if args.options is not None else project_options(m)
    st = load_streamer(m["streamer"])
    problems = layout_problems(st, layouts) + option_problems(st, layouts, options)
    if problems:
        die("; ".join(problems))
    fd = lock_project(pdir)
    if fd is None:
        die("%s is running -- change it after it finishes" % args.project)
    try:
        m = load_json(os.path.join(pdir, "project.json"))
        m["layouts"], m["options"] = layouts, options
        write_json_atomic(os.path.join(pdir, "project.json"), m)
    finally:
        os.close(fd)
    print("%s: layouts now %s; options %s" % (args.project, ", ".join(layouts), ", ".join(options)))
    return cmd_resume(argparse.Namespace(project=args.project))


def cmd_cancel(args):
    pdir, m = open_project(args.project)
    if not is_live(pdir):
        print("%s is not running" % args.project)
        return 0
    run = m.get("run", {})
    pid = run.get("pid")
    if not pid or proc_start(pid) != run.get("pid_start") or boot_id() != run.get("boot_id"):
        die("can't identify the running process safely; not sending any signal")
    os.kill(pid, signal.SIGTERM)
    print("cancel sent to %s (pid %d); it stops the current tool and removes its partial files"
          % (args.project, pid))
    return 0


ICON = {"done": "✅", "stale": "⚠️ ", "missing": "⚠️ ", "running": "⏳", "interrupted": "⏸ ",
        "failed": "❌", "waiting": "· ", "skipped": "⤼ "}


def show_project(pdir, m):
    live = is_live(pdir)
    states = evaluate(pdir, m, live)
    s = m["source"]
    head = "%s  [%s]" % (os.path.basename(pdir), overall(states))
    print(head)
    if m["route"] == "vod":
        print("  \"%s\"  %s → %s (%s)  %s × %s" % (s.get("title"), hms(s["start_s"]), hms(s["end_s"]),
                                                   span_label(s["length_s"]), m["streamer"], m["game"]))
    else:
        print("  clip %s (%s)  %s × %s" % (s["clip"], span_label(s["length_s"]), m["streamer"], m["game"]))
    for st in route_stages(m["route"]):
        name = st["name"]
        state, reason = states[name]
        rec = m["stages"].get(name, {})
        files = ", ".join(rec.get("outputs", {}).values()) if state != "waiting" else ""
        extra = dur(rec["seconds"]) if rec.get("seconds") is not None and state != "running" else ""
        print("  %s %-10s %-44s %s" % (ICON[state], name, files[:44], extra))
        if reason:
            print("     %s%s" % ("" if state != "failed" else "→ ", reason[:200]))
    print("  layouts   %s" % ", ".join(m.get("layouts", ["normal"])))
    print("  options   %s" % ", ".join(project_options(m)))
    if overall(states) == "done":
        print("  deliverables (import into Resolve):")
        for role, label, f in deliverables(m):
            print("     %-32s %s" % (label, os.path.join(pdir, f)))
        print("     Resolve: File → Import → Timeline → each .fcpxml; on the normal one drag the .srt "
              "onto a subtitle track (the Short has its captions as text already)")
    elif overall(states) in ("interrupted", "failed", "stale", "incomplete") and not live:
        print("  → python src/run_pipeline.py resume %s" % os.path.basename(pdir))


def cmd_status(args):
    if args.project:
        pdir, m = open_project(args.project)
        show_project(pdir, m)
        return 0
    root = projects_dir()
    rows = []
    for name in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        pdir = os.path.join(root, name)
        if not os.path.isdir(pdir) or name.startswith((".", "_")):
            continue
        mp = os.path.join(pdir, "project.json")
        if not os.path.exists(mp):
            rows.append(("  ", name, "legacy (made by hand, no project.json)"))
            continue
        m = load_json(mp)
        state = overall(evaluate(pdir, m, is_live(pdir)))
        rows.append((ICON.get(state, "  "), name, state))
    if not rows:
        print("no projects yet -- start one with: python src/run_pipeline.py new --vod LINK --start T --end T")
    for icon, name, state in rows:
        print("%s %-40s %s" % (icon, name, state))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("new", help="start a run from a VOD range or a pre-cut clip")
    n.add_argument("--vod", help="Twitch VOD link or id")
    n.add_argument("--start", help="e.g. 01:14:00 or 1h14m")
    n.add_argument("--end", help="e.g. 01:16:30")
    n.add_argument("--duration", help="instead of --end, e.g. 2m30s or '2 mins 30 sec'")
    n.add_argument("--clip", help="pre-cut clip file (no scoring: whole clip on V1)")
    n.add_argument("--title", help="clip route: what the clip is (recorded in the project metadata)")
    n.add_argument("--game", help="game pack (default: picked from the VOD's Twitch game)")
    n.add_argument("--streamer", help="streamer profile (default: picked from the VOD's Twitch channel)")
    n.add_argument("--layouts", help="comma list, e.g. normal,short (default: the profile's default_layouts)")
    n.add_argument("--options", help="comma list of %s (default: the profile's default_options, else all)"
                   % ", ".join(OPTIONS))
    n.add_argument("--name", help="project folder name (default: vod<ID>_<start>_<length>)")
    n.add_argument("--allow-long", action="store_true", help="allow ranges over 30 min")
    n.add_argument("--dry-run", action="store_true", help="preflight + estimate only")
    n.add_argument("--json", action="store_true", help="print the plan as JSON (for the UI)")
    r = sub.add_parser("resume", help="continue: runs every stage that isn't done and current")
    r.add_argument("project")
    r.add_argument("--from", help=argparse.SUPPRESS)
    b = sub.add_parser("rebuild", help="re-run from a stage (new versions; old ones kept)")
    b.add_argument("project")
    b.add_argument("--from", required=True, help="stage name, e.g. align")
    s = sub.add_parser("status", help="one project, or a list of all")
    s.add_argument("project", nargs="?")
    c = sub.add_parser("cancel", help="stop a running project")
    c.add_argument("project")
    lay = sub.add_parser("layouts", help="change which timelines a project makes, e.g. normal,short")
    lay.add_argument("project")
    lay.add_argument("layouts")
    ch = sub.add_parser("choose", help="change layouts and/or options, e.g. --options fcpxml,srt")
    ch.add_argument("project")
    ch.add_argument("--layouts")
    ch.add_argument("--options")
    args = ap.parse_args(argv)
    return {"new": new_project, "resume": cmd_resume, "rebuild": cmd_resume, "status": cmd_status,
            "cancel": cmd_cancel, "layouts": cmd_layouts, "choose": cmd_choose}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
