#!/usr/bin/env python3
"""
Local web UI for run_pipeline.py: VOD link + start/end in, one FCPXML per ticked layout +
SRT out, plus the Review screen (fix words by ear). Open http://127.0.0.1:8765

Streamer profiles are picked by clicking a card on the New run form; the VOD's Twitch
channel pre-selects one, never guesses (docs/design-notes.md#three-output-formats-because-no-single-one-is-enough 3b).

    venv/bin/python src/ui_server.py [--port 8765]

Thin on purpose (docs/design-notes.md#orchestrator-guarantees): the server keeps NO state of its own.
Every page is rebuilt from the project folders on disk; runs are separate
`run_pipeline.py` processes (own session, so closing the tab or restarting this server
never stops one). Only the orchestrator writes project.json; this server writes only
review_decisions.json (refused while that project is running).

Security (audit F-01/F-02, laptop only): binds 127.0.0.1; rejects any Host header
but 127.0.0.1/localhost on our port (DNS rebinding); every POST needs a matching Origin,
JSON content type and the per-start token embedded in the page (CSRF); files are served
only if a project's manifest lists them; commands are argv lists, never a shell.
Stdlib only.
"""

import argparse
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import resolve_read
import run_pipeline as rp
import streamer_store

UI_DIR = os.path.join(rp.ROOT, "ui")
STATIC = {"/static/app.js": "app.js", "/static/style.css": "style.css"}
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,80}$")
SPAN_RE = re.compile(r"^(\d{1,6})-(\d{1,6})$")
FRAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}\.jpg$")
MAX_BODY = 1 << 20
TOKEN = secrets.token_urlsafe(32)
PY = sys.executable
PIPELINE = os.path.join(rp.SRC, "run_pipeline.py")


class HttpError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code, self.msg = code, msg


# ---------------------------------------------------------------- projects (read side)

def project_dir(name):
    if not NAME_RE.match(name or ""):
        raise HttpError(404, "no such project")
    pdir = os.path.join(rp.projects_dir(), name)
    if not os.path.exists(os.path.join(pdir, "project.json")):
        raise HttpError(404, "no such project")
    return pdir


def load_manifest(pdir):
    m = rp.load_json(os.path.join(pdir, "project.json"))
    if m.get("schema_version") != rp.SCHEMA_VERSION:
        raise HttpError(409, "project uses schema %s; this UI reads %d"
                        % (m.get("schema_version"), rp.SCHEMA_VERSION))
    return m


def listed_files(m):
    names = set()
    for rec in list(m["stages"].values()) + m.get("history", []):
        names.update((rec.get("outputs") or {}).values())
    return names


def file_url(name, f):
    return "/files/%s/%s" % (urllib.parse.quote(name), urllib.parse.quote(f))


def projects_list():
    root = rp.projects_dir()
    out = []
    for name in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        pdir = os.path.join(root, name)
        if not os.path.isdir(pdir) or name.startswith((".", "_")):
            continue
        if not os.path.exists(os.path.join(pdir, "project.json")):
            out.append({"name": name, "state": "legacy"})
            continue
        try:
            m = load_manifest(pdir)
            live = rp.is_live(pdir)
            state = rp.overall(rp.evaluate(pdir, m, live))
        except (HttpError, ValueError, OSError) as e:
            out.append({"name": name, "state": "unreadable", "error": str(e)})
            continue
        s = m["source"]
        out.append({"name": name, "state": state, "title": s.get("title"), "route": m["route"],
                    "range": ("%s → %s" % (rp.hms(s["start_s"]), rp.hms(s["end_s"])))
                    if m["route"] == "vod" else os.path.basename(s.get("clip", "")),
                    "length": rp.span_label(s["length_s"]), "created": m.get("created"),
                    "game": m.get("game")})
    out.sort(key=lambda p: p.get("created") or "", reverse=True)
    return out


def tail_file(path, n=40):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 64 * 1024))
            text = f.read()
    except OSError:
        return []
    lines = [ln.rstrip() for ln in text.replace("\r", "\n").split("\n") if ln.strip()]
    return lines[-n:]


def project_view(name):
    pdir = project_dir(name)
    m = load_manifest(pdir)
    live = rp.is_live(pdir)
    states = rp.evaluate(pdir, m, live)
    state = rp.overall(states)
    stages = []
    focus = None
    for st in rp.route_stages(m["route"]):
        n = st["name"]
        rec = m["stages"].get(n, {})
        s, reason = states[n]
        stages.append({"name": n, "state": s, "reason": reason, "version": rec.get("version"),
                       "outputs": list((rec.get("outputs") or {}).values()) if s != "waiting" else [],
                       "seconds": rec.get("seconds"), "started": rec.get("started"),
                       "cost_usd": rec.get("cost_usd")})
        if s in ("running", "failed", "interrupted") and not focus:
            focus = n
    log_stage = focus or next((x["name"] for x in reversed(stages) if x["state"] == "done"), None)
    log_rec = m["stages"].get(log_stage, {}) if log_stage else {}
    deliverables = []
    if state == "done":
        for role, label, f in rp.deliverables(m):
            deliverables.append({"role": role, "label": label, "file": f, "path": os.path.join(pdir, f)})
    try:
        options = streamer_card(m["streamer"])["layouts"]
    except SystemExit:
        options = []
    rev = m["stages"].get("review", {})
    return {"name": name, "state": state, "live": live, "route": m["route"], "source": m["source"],
            "game": m["game"], "streamer": m["streamer"], "created": m.get("created"),
            "run": m.get("run", {}), "stages": stages,
            "deliverables": deliverables, "folder": pdir,
            "layouts": m.get("layouts", ["normal"]), "layout_options": options,
            "options": rp.project_options(m), "option_list": option_list(m["game"]),
            "log": {"stage": log_stage, "lines": tail_file(os.path.join(pdir, log_rec["log"]))
                    if log_rec.get("log") else []},
            "run_log": tail_file(os.path.join(pdir, "logs", "run.log"), 12),
            "review": {"human_edits": rev.get("human_edits")},
            "can_review": m["stages"].get("align", {}).get("status") == "done"}


def valid_span(k, words):
    first, last = (int(x) for x in k.split("-"))
    return 0 <= first <= last < len(words)


def words_fp(words):
    """Which transcript the Review page was opened on (word numbers are only valid for it)."""
    return hashlib.sha256("\n".join(w["text"] for w in words).encode()).hexdigest()[:16]


def review_view(name):
    pdir = project_dir(name)
    m = load_manifest(pdir)
    st = m["stages"]
    if st.get("transcribe", {}).get("status") != "done":
        raise HttpError(409, "there is no transcript to review yet")
    # same words either way; the aligned times (if built) follow the voice for "play this bit"
    timed = st["align"] if st.get("align", {}).get("status") == "done" else st["transcribe"]
    words = rp.words_of(rp.load_json(os.path.join(pdir, timed["outputs"]["transcript"])))
    video_stage = "download" if m["route"] == "vod" else "import"
    video = st.get(video_stage, {}).get("outputs", {}).get("video")
    live = rp.is_live(pdir)
    review_state = rp.evaluate(pdir, m, live).get("review", ("waiting", ""))[0]
    # a re-transcribe moves word numbers: decisions whose words changed are left out (the page
    # lists them to redo), so they can't show on -- or be saved onto -- other words
    decisions, moved = {}, []
    for k, d in rp.load_decisions(pdir).items():
        now = rp.span_words(words, k) if valid_span(k, words) else None
        if "words" in d and d["words"] != now:
            moved.append(dict(d, span=k, now=now))
        else:
            decisions[k] = d
    return {"name": name, "words": words, "decisions": decisions, "moved": moved,
            "words_fp": words_fp(words),
            "video": file_url(name, video) if video else None, "live": live,
            "review_state": review_state, "title": m["source"].get("title"), "game": m["game"]}


# ---------------------------------------------------------------- actions (write side)

def any_live():
    root = rp.projects_dir()
    for name in os.listdir(root) if os.path.isdir(root) else []:
        pdir = os.path.join(root, name)
        if os.path.exists(os.path.join(pdir, "project.json")) and rp.is_live(pdir):
            return name
    return None


def run_args(body):
    """Form fields -> argv for `run_pipeline.py new`. Parsed here first so junk never
    reaches a process; run_pipeline parses again (it's the authority)."""
    def field(k, limit=200):
        v = body.get(k)
        if v is None or v == "":
            return None
        if not isinstance(v, str) or len(v) > limit or any(ord(ch) < 32 for ch in v):
            raise HttpError(400, "bad value for %s" % k)
        return v.strip()
    vod, start, end, duration, game, streamer = (field(k) for k in ("vod", "start", "end", "duration",
                                                                      "game", "streamer"))
    try:
        rp.parse_vod(vod or "")
        rp.parse_time(start or "")
        if end:
            rp.parse_time(end)
        if duration:
            rp.parse_time(duration)
    except ValueError as e:
        raise HttpError(400, str(e))
    if bool(end) == bool(duration):
        raise HttpError(400, "give an end time or a duration")
    if game and game not in rp.list_games():
        raise HttpError(400, "unknown game pack %r" % game)
    if streamer and streamer not in rp.list_streamers():
        raise HttpError(400, "unknown streamer profile %r" % streamer)
    layouts = clean_layouts(body.get("layouts")) if body.get("layouts") is not None else None
    options = clean_options(body.get("options")) if body.get("options") is not None else None
    argv = ["new", "--vod", vod, "--start", start]
    argv += ["--end", end] if end else ["--duration", duration]
    if game:
        argv += ["--game", game]
    if streamer:
        argv += ["--streamer", streamer]
    if layouts:
        argv += ["--layouts", ",".join(layouts)]
    if options is not None:
        argv += ["--options", ",".join(options)]
    if body.get("allow_long") is True:
        argv += ["--allow-long"]
    return argv


def clean_layouts(raw):
    if not isinstance(raw, list) or not raw or len(raw) > 10 or \
            not all(isinstance(x, str) and rp.LAYOUT_NAME.match(x) for x in raw):
        raise HttpError(400, "tick at least one layout")
    return list(dict.fromkeys(raw))


def clean_options(raw):
    if not isinstance(raw, list) or len(raw) > 20 or not all(isinstance(x, str) and x in rp.OPTIONS for x in raw):
        raise HttpError(400, "options must be some of: %s" % ", ".join(rp.OPTIONS))
    return list(dict.fromkeys(raw))


def option_list(game=None):
    """rp.OPTIONS for the checkboxes, + whether the game pack can do each (icons need an icon map)."""
    out = rp.option_list()
    try:
        has_map = bool(rp.load_game(game)["_icon_map_path"]) if game else None
    except SystemExit:
        has_map = None
    for o in out:
        if o["name"] == "icons" and has_map is False:
            o["unavailable"] = "game pack %s has no icon map yet" % game
    return out


def plan(body):
    p = subprocess.run([PY, PIPELINE, *run_args(body), "--dry-run", "--json"], capture_output=True,
                       text=True, timeout=180, stdin=subprocess.DEVNULL)
    for line in reversed(p.stdout.splitlines()):
        if line.startswith("{"):
            out = json.loads(line)
            out["option_list"] = option_list(out.get("game"))
            return out
    raise HttpError(400, (p.stderr.strip() or p.stdout.strip()).replace("error: ", "", 1)[-600:]
                    or "couldn't check that VOD")


def spawn(argv, tag):
    """Detached run_pipeline.py: own session, survives the tab and this server."""
    d = os.path.join(rp.projects_dir(), ".launches")
    os.makedirs(d, exist_ok=True)
    log = open(os.path.join(d, "%s_%s.log" % (time.strftime("%Y%m%d-%H%M%S"), tag)), "w")
    subprocess.Popen([PY, PIPELINE, *argv], stdin=subprocess.DEVNULL, stdout=log,
                     stderr=subprocess.STDOUT, start_new_session=True, cwd=rp.ROOT)
    log.close()


def start_run(body):
    busy = any_live()
    if busy:
        raise HttpError(409, "%s is running -- one heavy run at a time" % busy)
    p = plan(body)
    if p.get("exists"):
        raise HttpError(409, "project %s already exists" % p["name"])
    if p["problems"]:
        raise HttpError(400, "preflight: " + "; ".join(p["problems"]))
    spawn(run_args(body), p["name"])
    deadline = time.time() + 120   # Twitch check, then the folder appears
    while time.time() < deadline:
        if os.path.exists(os.path.join(rp.projects_dir(), p["name"], "project.json")):
            return {"name": p["name"]}
        time.sleep(0.3)
    raise HttpError(500, "the run didn't start -- see projects/.launches/")


def resume(name, body):
    pdir = project_dir(name)
    if rp.is_live(pdir):
        raise HttpError(409, "already running")
    busy = any_live()
    if busy:
        raise HttpError(409, "%s is running -- one heavy run at a time" % busy)
    stage = body.get("from")
    if stage is not None:
        m = load_manifest(pdir)
        if stage not in [s["name"] for s in rp.route_stages(m["route"])]:
            raise HttpError(400, "unknown stage")
        spawn(["rebuild", name, "--from", stage], name)
    else:
        spawn(["resume", name], name)
    deadline = time.time() + 15
    while time.time() < deadline and not rp.is_live(pdir):
        time.sleep(0.2)
    return {"ok": True}


def cancel(name):
    project_dir(name)
    p = subprocess.run([PY, PIPELINE, "cancel", name], capture_output=True, text=True, timeout=30,
                       stdin=subprocess.DEVNULL)
    if p.returncode:
        raise HttpError(409, p.stderr.strip().replace("error: ", "", 1))
    return {"ok": True, "message": p.stdout.strip()}


def save_decisions(name, body):
    pdir = project_dir(name)
    if rp.is_live(pdir):
        raise HttpError(409, "this project is running -- save after it finishes")
    raw = body.get("decisions")
    if not isinstance(raw, dict) or len(raw) > 5000:
        raise HttpError(400, "decisions must be an object")
    clean = {}
    for k, d in raw.items():
        if not SPAN_RE.match(k) or not isinstance(d, dict):
            raise HttpError(400, "bad decision key %r" % k)
        action = d.get("action")
        if action not in ("accept", "revert", "text"):
            raise HttpError(400, "bad action for %s" % k)
        entry = {"action": action, "at": rp.now()}      # + "words": filled in below
        if action == "text":
            text = d.get("text")
            if not isinstance(text, str) or len(text) > 300 or any(ord(ch) < 32 for ch in text):
                raise HttpError(400, "bad text for %s" % k)
            entry["text"] = " ".join(text.split())
        clean[k] = entry
    view = review_view(name)
    if "words_fp" in body and body["words_fp"] != view["words_fp"]:
        raise HttpError(409, "the transcript changed since this page was opened -- reload Review and redo your decisions")
    for k, entry in clean.items():
        # the words this decision is about: the review stage refuses it if a re-transcribe
        # moved them (keys are word numbers)
        entry["words"] = rp.span_words(view["words"], k)
    try:
        rp.merge_review(view["words"], clean)   # same rules the stage applies
    except ValueError as e:
        raise HttpError(400, str(e))
    old = rp.load_decisions(pdir)
    for k, d in clean.items():   # keep the original decision time when nothing changed
        if k in old and {x: y for x, y in old[k].items() if x not in ("at", "words")} == \
                {x: y for x, y in d.items() if x not in ("at", "words")}:
            d["at"] = old[k].get("at", d["at"])
    rp.write_json_atomic(os.path.join(pdir, rp.REVIEW_FILE),
                         {"_comment": "the author's by-ear decisions (Review screen). Keys are whisper "
                                      "word spans; applied by run_pipeline.py's review stage.",
                          "decisions": clean})
    return {"ok": True, "count": len(clean)}


def open_folder(name):
    pdir = project_dir(name)
    subprocess.Popen(["xdg-open", pdir], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    return {"ok": True}


def set_layouts(name, body):
    """Project page checkboxes -> `run_pipeline.py layouts`: rebuilds the webcam clip and
    timelines only (seconds)."""
    pdir = project_dir(name)
    m = load_manifest(pdir)
    layouts = clean_layouts(body.get("layouts"))
    problems = rp.layout_problems(rp.load_streamer(m["streamer"]), layouts)
    if problems:
        raise HttpError(400, "; ".join(problems))
    if rp.is_live(pdir):
        raise HttpError(409, "this project is running -- change layouts after it finishes")
    busy = any_live()
    if busy:
        raise HttpError(409, "%s is running -- one heavy run at a time" % busy)
    spawn(["layouts", name, ",".join(layouts)], name)
    deadline = time.time() + 15   # the command records the new list before it starts the run
    while time.time() < deadline and load_manifest(pdir).get("layouts") != layouts:
        time.sleep(0.2)
    return {"ok": True, "layouts": layouts}


def set_choices(name, body):
    """Project page "What you get" -> `run_pipeline.py choose`: rebuilds the webcam clip, icons
    and timelines only (seconds)."""
    pdir = project_dir(name)
    m = load_manifest(pdir)
    layouts = clean_layouts(body.get("layouts"))
    options = clean_options(body.get("options"))
    st = rp.load_streamer(m["streamer"])
    problems = rp.layout_problems(st, layouts) + rp.option_problems(st, layouts, options)
    if problems:
        raise HttpError(400, "; ".join(problems))
    if rp.is_live(pdir):
        raise HttpError(409, "this project is running -- change it after it finishes")
    busy = any_live()
    if busy:
        raise HttpError(409, "%s is running -- one heavy run at a time" % busy)
    spawn(["choose", name, "--layouts", ",".join(layouts), "--options", ",".join(options)], name)
    deadline = time.time() + 15   # the command records the choice before it starts the run
    while time.time() < deadline:
        mm = load_manifest(pdir)
        if mm.get("layouts") == layouts and rp.project_options(mm) == options:
            break
        time.sleep(0.2)
    return {"ok": True, "layouts": layouts, "options": options}


def streamer_card(name):
    st = rp.load_streamer(name)
    return {"name": name, "title": st.get("title", name), "twitch_logins": st.get("twitch_logins", []),
            "has_webcam": bool(st.get("webcam")), "default_layouts": st.get("default_layouts", ["normal"]),
            "default_options": st.get("default_options") or rp.DEFAULT_OPTIONS,
            "option_list": rp.option_list(),
            "layouts": [{"name": n, "title": lay.get("title", n), "kind": lay.get("kind"),
                         "needs_webcam": lay.get("kind") == "stack"}
                        for n, lay in rp.profile_layouts(st).items()]}


def streamers():
    return [streamer_card(n) for n in rp.list_streamers()]


def streamer_full(name):
    if name not in rp.list_streamers():
        raise HttpError(404, "no such streamer profile")
    return rp.load_streamer(name)


def save_streamer(body):
    try:
        prof = streamer_store.save(body.get("profile"), update=body.get("update") is True)
    except streamer_store.ProfileError as e:
        raise HttpError(400, str(e))
    return {"ok": True, "name": prof["name"]}


def frames_dir():
    return os.path.join(rp.projects_dir(), ".frames")


def grab_frame(body):
    """One frame of a VOD at a time, for drawing the webcam box: downloads ~3 s with the same
    TwitchDownloaderCLI the runs use, keeps a JPEG in projects/.frames/."""
    try:
        vod = rp.parse_vod(body.get("vod") or "")
        at = rp.parse_time(body.get("at") or "")
    except (ValueError, AttributeError) as e:
        raise HttpError(400, str(e) or "give a VOD link and a time")
    tools = rp.load_tools()
    os.makedirs(frames_dir(), exist_ok=True)
    tag = "%s_%d_%s" % (vod, int(at), secrets.token_hex(4))
    tmp = os.path.join(frames_dir(), "tmp_" + tag)
    os.makedirs(tmp)
    try:
        clip = os.path.join(tmp, "clip.mp4")
        p = subprocess.run([tools["twitch_downloader"], "videodownload", "--id", vod, "-b", rp.hms(at),
                            "-e", rp.hms(at + 3), "-o", clip, "--collision", "Exit", "--temp-path", tmp,
                            "--banner", "false"], capture_output=True, text=True, timeout=180,
                           stdin=subprocess.DEVNULL)
        if p.returncode or not os.path.exists(clip):
            raise HttpError(400, "couldn't download that moment: " + (p.stderr or p.stdout).strip()[-300:])
        out = os.path.join(frames_dir(), tag + ".jpg")
        p = subprocess.run([tools["ffmpeg"], "-nostdin", "-v", "error", "-ss", "1", "-i", clip,
                            "-frames:v", "1", "-q:v", "3", out], capture_output=True, text=True,
                           timeout=60, stdin=subprocess.DEVNULL)
        if p.returncode or not os.path.exists(out):
            raise HttpError(400, "couldn't read a frame: " + p.stderr.strip()[-300:])
        info = rp.ffprobe(tools, clip)
        size = subprocess.run([tools["ffprobe"], "-v", "error", "-select_streams", "v:0", "-show_entries",
                               "stream=width,height", "-of", "csv=p=0", out], capture_output=True, text=True,
                              stdin=subprocess.DEVNULL).stdout.strip().split(",")
        return {"url": "/frames/" + tag + ".jpg", "width": int(size[0]), "height": int(size[1]),
                "fps": info["fps"]}
    except subprocess.TimeoutExpired:
        raise HttpError(504, "Twitch took too long")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def read_resolve(body):
    height, width = body.get("height", 1920), body.get("width", 1080)
    if not all(isinstance(v, int) and not isinstance(v, bool) and 100 <= v <= 8000 for v in (height, width)):
        raise HttpError(400, "bad timeline size")
    try:
        r = resolve_read.read_layout(height=height, width=width)
    except resolve_read.ResolveReadError as e:
        raise HttpError(404, str(e))
    r["saved_at"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(r.pop("saved")))
    r.pop("db", None)
    return r


def games():
    out = []
    for n in rp.list_games():
        g = rp.load_game(n)
        out.append({"name": n, "title": g.get("title", n), "twitch_games": g.get("twitch_games", []),
                    "has_icons": bool(g.get("_icon_map_path"))})
    return out


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "video-pipeline"
    sys_version = ""

    def log_message(self, fmt, *args):
        if os.environ.get("VP_UI_QUIET"):
            return
        sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    def allowed_hosts(self):
        port = self.server.server_address[1]
        return {"127.0.0.1:%d" % port, "localhost:%d" % port}

    def send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; media-src 'self'; img-src 'self' data:; "
                         "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def guard(self):
        if self.headers.get("Host") not in self.allowed_hosts():
            raise HttpError(421, "wrong host")

    def dispatch(self, method):
        try:
            self.guard()
            url = urllib.parse.urlsplit(self.path)
            parts = [urllib.parse.unquote(p) for p in url.path.split("/") if p]
            if method == "POST":
                body = self.read_json()
                return self.send(200, self.post(parts, body))
            return self.get(url, parts)
        except HttpError as e:
            self.send(e.code, {"error": e.msg})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except SystemExit as e:  # run_pipeline helpers die() on bad config
            self.send(500, {"error": str(e.code if e.code is not None else "config error")})
        except Exception as e:  # never leak a traceback page; keep it in the server log
            import traceback
            traceback.print_exc()
            self.send(500, {"error": "%s: %s" % (type(e).__name__, e)})

    def do_GET(self):
        self.dispatch("GET")

    def do_HEAD(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def read_json(self):
        origin = self.headers.get("Origin")
        if origin not in {"http://" + h for h in self.allowed_hosts()}:
            raise HttpError(403, "cross-origin request refused")
        if not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), TOKEN):
            raise HttpError(403, "missing or wrong token -- reload the page")
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            raise HttpError(415, "JSON only")
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise HttpError(413, "too large")
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            raise HttpError(400, "bad JSON")
        if not isinstance(body, dict):
            raise HttpError(400, "bad JSON")
        return body

    def get(self, url, parts):
        if not parts:
            with open(os.path.join(UI_DIR, "index.html"), encoding="utf-8") as f:
                page = f.read().replace("{{CSRF_TOKEN}}", TOKEN)
            return self.send(200, page.encode(), "text/html; charset=utf-8")
        if url.path in STATIC:
            with open(os.path.join(UI_DIR, STATIC[url.path]), "rb") as f:
                data = f.read()
            ctype = "text/javascript" if url.path.endswith(".js") else "text/css"
            return self.send(200, data, ctype + "; charset=utf-8")
        if parts[0] == "files" and len(parts) == 3:
            return self.serve_file(parts[1], parts[2])
        if parts[0] == "frames" and len(parts) == 2 and FRAME_RE.match(parts[1]):
            path = os.path.join(frames_dir(), parts[1])
            if os.path.isfile(path):
                with open(path, "rb") as f:
                    return self.send(200, f.read(), "image/jpeg")
            raise HttpError(404, "not found")
        if parts[0] == "api":
            q = urllib.parse.parse_qs(url.query)
            if parts[1:] == ["projects"]:
                return self.send(200, projects_list())
            if parts[1:] == ["games"]:
                return self.send(200, games())
            if parts[1:] == ["streamers"]:
                return self.send(200, streamers())
            if len(parts) == 3 and parts[1] == "streamers":
                return self.send(200, streamer_full(parts[2]))
            if len(parts) == 3 and parts[1] == "projects":
                return self.send(200, project_view(parts[2]))
            if len(parts) == 4 and parts[1] == "projects" and parts[3] == "review":
                return self.send(200, review_view(parts[2]))
            if len(parts) == 4 and parts[1] == "projects" and parts[3] == "log":
                pdir = project_dir(parts[2])
                m = load_manifest(pdir)
                stage = (q.get("stage") or [""])[0]
                rec = m["stages"].get(stage)
                if not rec or not rec.get("log"):
                    raise HttpError(404, "no log")
                return self.send(200, {"lines": tail_file(os.path.join(pdir, rec["log"]), 200)})
        raise HttpError(404, "not found")

    def post(self, parts, body):
        if parts[:1] != ["api"]:
            raise HttpError(404, "not found")
        if parts[1:] == ["plan"]:
            return plan(body)
        if parts[1:] == ["runs"]:
            return start_run(body)
        if parts[1:] == ["streamers"]:
            return save_streamer(body)
        if parts[1:] == ["streamer-frame"]:
            return grab_frame(body)
        if parts[1:] == ["resolve-read"]:
            return read_resolve(body)
        if len(parts) == 4 and parts[1] == "projects":
            name, action = parts[2], parts[3]
            if action == "resume":
                return resume(name, body)
            if action == "cancel":
                return cancel(name)
            if action == "review":
                return save_decisions(name, body)
            if action == "open-folder":
                return open_folder(name)
            if action == "layouts":
                return set_layouts(name, body)
            if action == "choices":
                return set_choices(name, body)
        raise HttpError(404, "not found")

    def serve_file(self, name, fname):
        pdir = project_dir(name)
        if "/" in fname or fname.startswith(".") or fname not in listed_files(load_manifest(pdir)):
            raise HttpError(404, "not found")
        path = os.path.join(pdir, fname)
        if not os.path.isfile(path):
            raise HttpError(404, "not found")
        size = os.path.getsize(path)
        ctype = mimetypes.guess_type(fname)[0] or "application/octet-stream"
        if fname.endswith((".json", ".otio", ".srt", ".txt", ".fcpxml")):
            ctype = "text/plain; charset=utf-8"
        start, end, code = 0, size - 1, 200
        rng = self.headers.get("Range")
        if rng:
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng.strip())
            if not m or (not m.group(1) and not m.group(2)):
                return self.send(416, b"", "text/plain", {"Content-Range": "bytes */%d" % size})
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            if start > end or start >= size:
                return self.send(416, b"", "text/plain", {"Content-Range": "bytes */%d" % size})
            code = 206
        length = end - start + 1
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        if code == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(path, "rb") as f:
            f.seek(start)
            left = length
            while left > 0:
                chunk = f.read(min(1 << 20, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=int(os.environ.get("VP_PORT", 8765)))
    args = ap.parse_args()
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    httpd.daemon_threads = True
    print("video pipeline UI: http://127.0.0.1:%d  (projects: %s)" % (args.port, rp.projects_dir()),
          flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
