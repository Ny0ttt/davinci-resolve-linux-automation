"""Smoke tests: is this repo internally consistent and startable?

Deliberately fixture-free. There is no media, no transcript and no third-party speech
anywhere in here, which is what lets the suite exist at all -- see README Known gaps.

What these tests CANNOT tell you: whether captions are accurate, whether the timing is
right, or whether a timeline looks correct in DaVinci Resolve. Those need a real clip, a
built whisper.cpp and a human watching a timeline. Nothing here replaces that.

Each test below corresponds to a defect that actually shipped:
  - preflight on a missing projects/   -- crashed the first command the README gives
  - configs pointing at absent paths   -- a profile referenced a samples/ dir that does
                                          not ship
  - README vs STAGES                   -- the stage table claimed a .drt output that is
                                          not wired up
  - no paid dependency                 -- the whole point of v0.2.0
  - UI launching the orchestrator      -- the src/ move left ui_server pointing at
                                          ROOT/run_pipeline.py, so every UI run died
  - the quickstart's own sample        -- dropping the whisper.cpp submodule would have
                                          taken the quickstart's clip with it
  - the drt stage was never ported     -- the writer, its templates and its byte formats
                                          shipped from the start, but no option or stage
                                          called them, so .drt was unreachable from a run
  - drt_skip with no fps recorded      -- the guard reads stages.media.fps, so the media
                                          stage has to write it or the guard never fires
  - a broken symlink out of the repo   -- shipped a private absolute path, and the
                                          privacy scan skipped it as "not a file"
"""

import importlib
import json
import os
import re
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
sys.path.insert(0, SRC)

# whisper.cpp is installed and built by the user OUTSIDE this repo (it was a submodule
# until 2026-10-23). Shipped configs therefore carry "/path/to/..." placeholders, which
# are expected not to resolve in a fresh checkout or in CI.
USER_BUILT = ("/path/to/", "whisper.cpp/")

MODULES = sorted(f[:-3] for f in os.listdir(SRC)
                  if f.endswith(".py") and not f.startswith("_"))

# This file names the private terms it searches for, so it would flag itself.
SELF = "tests/" + os.path.basename(__file__)


def repo(*parts):
    return os.path.join(ROOT, *parts)


def read(path):
    with open(repo(path), encoding="utf-8") as f:
        return f.read()


def shipped(*patterns):
    """Tracked files matching globs, via git -- so untracked scratch never leaks in.

    venv/, whisper.cpp/ and projects/ are skipped. All three are gitignored,
    but a local checkout can still have them in the index -- and then the suite would
    parametrize over site-packages (files deliberately not UTF-8) or over a run's own output
    in projects/, which is not shipped and not ours to assert on.
    """
    out = subprocess.run(["git", "ls-files", *patterns], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout
    return [p for p in out.splitlines()
            if p and not p.startswith(("venv/", "whisper.cpp/", "projects/"))
            and (os.path.isfile(repo(p)) or os.path.islink(repo(p)))]


def has_cli(name):
    """ui_server's main() binds a socket, so it is import-tested but not --help tested."""
    return name != "ui_server" and "__main__" in read("src/" + name + ".py")


CLIS = [m for m in MODULES if has_cli(m)]


# ---------------------------------------------------------------- it loads at all

@pytest.mark.parametrize("name", MODULES)
def test_module_imports(name):
    """A rename that misses a call site fails here instead of mid-run."""
    importlib.import_module(name)


@pytest.mark.parametrize("name", CLIS)
def test_cli_help(name):
    r = subprocess.run([sys.executable, repo("src", name + ".py"), "--help"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, "%s --help failed:\n%s" % (name, r.stderr)
    assert r.stdout.strip(), "%s --help printed nothing" % name


def test_ui_points_at_a_real_pipeline():
    """ui_server launches run_pipeline.py as a subprocess, by absolute path.

    Regression: the src/ move left that path built from ROOT instead of SRC, so every
    run started from the UI died with `can't open file '.../run_pipeline.py': [Errno 2]`.
    test_module_imports cannot catch it -- os.path.join happily builds a string for a
    file that does not exist, and ui_server's main() binds a socket so it is never run
    here. Note UI_DIR legitimately uses ROOT (ui/ really is at the repo root), which is
    what made the wrong line look consistent with its neighbour.
    """
    ui = importlib.import_module("ui_server")
    assert os.path.isfile(ui.PIPELINE), "ui_server.PIPELINE does not exist: %s" % ui.PIPELINE


# ---------------------------------------------------------------- preflight

def test_preflight_survives_a_fresh_checkout(tmp_path, monkeypatch):
    """projects/ is gitignored, so it does not exist until the first run.

    Regression: preflight called shutil.disk_usage() on it and raised an unhandled
    FileNotFoundError -- on `new --clip ... --dry-run`, the first command the README
    gives a newcomer. Invisible on a machine that had already run the pipeline.
    """
    import run_pipeline

    monkeypatch.setenv("VP_PROJECTS", str(tmp_path / "does" / "not" / "exist"))
    monkeypatch.setenv("VP_TOOLS", repo("tools.example.json"))
    m = {"route": "clip", "game": "example", "streamer": "example",
         "layouts": ["normal"], "options": ["fcpxml", "srt"]}
    problems, warnings = run_pipeline.preflight(
        m, run_pipeline.load_tools(), {"bytes": 0, "seconds": 0})
    assert isinstance(problems, list) and isinstance(warnings, list)


def test_preflight_names_tools_json_for_a_bad_tool_path():
    """The message has to say where to fix it, not just what is missing.

    Regression: the VOD route called the Twitch metadata lookup before preflight, so an
    unedited tools.json placeholder surfaced as a bare "[Errno 2] No such file or
    directory" with no mention of tools.json -- even under --dry-run.
    """
    import run_pipeline

    msg = run_pipeline.missing_tool({"twitch_downloader": "/path/to/nope"},
                                    "twitch_downloader")
    assert msg and "twitch_downloader" in msg and "tools.json" in msg
    assert run_pipeline.missing_tool({"ffmpeg": sys.executable}, "ffmpeg") is None


# ---------------------------------------------------------------- shipped configs

@pytest.mark.parametrize("path", shipped("*.json", "*/*.json"))
def test_shipped_json_parses(path):
    with open(repo(path), encoding="utf-8") as f:
        json.load(f)


def test_config_paths_either_exist_or_are_user_built():
    """Every path a shipped config points at must resolve in a fresh clone.

    The exception is whisper.cpp/, which the user builds. Regression: a profile sent the
    reader to `samples/README.md` for a self-test, in a directory removed for privacy and
    never replaced.
    """
    import run_pipeline

    dangling = []
    for path in shipped("streamers/*.json", "games/*.json", "profiles/*.json"):
        blob = read(path)
        for ref in re.findall(r'"((?:[\w.-]+/)+[\w.-]+\.(?:json|bin|srt|md|txt))"', blob):
            if ref.startswith(USER_BUILT) or os.path.exists(repo(ref)):
                continue
            dangling.append("%s -> %s" % (path, ref))
    assert not dangling, "shipped configs point at missing paths:\n  " + "\n  ".join(dangling)


def test_quickstart_sample_ships_and_has_video():
    """The README's first command runs on samples/jfk.mp4, so it has to be in the repo.

    Two defects in one: the file was read from whisper.cpp/samples/ until 2026-10-23, when
    that submodule stopped being vendored; and the first vendored copy was the bare .wav,
    which the clip route rejects outright -- `error: no video stream in ...`. The quickstart
    therefore needs a file that is present AND carries a video track.
    """
    clip = repo("samples", "jfk.mp4")
    assert os.path.isfile(clip), "samples/jfk.mp4 is missing"
    streams = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                              "-show_entries", "stream=codec_type", "-of", "csv=p=0", clip],
                             capture_output=True, text=True, timeout=30).stdout
    assert "video" in streams, "samples/jfk.mp4 has no video stream"


def test_prose_samples_references_resolve():
    """Anything pointing a reader into samples/ must actually be there.

    Regression: a profile sent the reader to `samples/README.md` for a self-test, in a
    directory that had been removed for privacy and never replaced. CHANGELOG.md is
    exempt -- a changelog has to be able to name what it removed.
    """
    bad = []
    for path in shipped("*.json", "*/*.json", "src/*.py", "*.md"):
        if path in (SELF, "CHANGELOG.md"):
            continue
        for n, line in enumerate(read(path).splitlines(), 1):
            for ref in re.findall(r"samples/[\w.-]+", line):
                if not os.path.exists(repo(ref)):
                    bad.append("%s:%d -> %s" % (path, n, ref))
    assert not bad, "points into samples/ at something absent: %s" % ", ".join(bad)


# ---------------------------------------------------------------- no paid dependency

def test_tools_example_has_no_llm_key():
    tools = json.loads(read("tools.example.json"))
    assert "claude" not in tools
    assert not [k for k in tools if "claude" in k.lower()]


def test_no_module_invokes_an_llm():
    """v0.2.0's headline claim: no network call, no account, no paid CLI."""
    offenders = []
    for name in MODULES:
        for n, line in enumerate(read("src/" + name + ".py").splitlines(), 1):
            if re.search(r"\bclaude\b", line, re.I):
                offenders.append("src/%s.py:%d" % (name, n))
    assert not offenders, "LLM reference survives in: %s" % ", ".join(offenders)


# ---------------------------------------------------------------- docs match code

def test_readme_stage_table_matches_the_code():
    """Regression: the table advertised a .drt output the orchestrator does not write."""
    import run_pipeline

    rows = re.findall(r"^\| `([^`]+)`(?: / `([^`]+)`)? \| .* \| (.*) \|$",
                      read("README.md"), re.M)
    documented = set()
    for a, b, _ in rows:
        documented.update(x for x in (a, b) if x)
    defined = {s["name"] for s in run_pipeline.STAGES}
    assert documented == defined, "README stage table vs STAGES:\n  only in README: %s\n  only in code: %s" % (
        sorted(documented - defined), sorted(defined - documented))

    # and no row may promise an artifact no module produces. This guard sat dormant from the
    # day it was written -- the README had no .drt row, so the branch never ran and never
    # showed that its paths were missing the src/ prefix. Adding the drt stage on 2026-10-23
    # is what finally executed it.
    outputs = " ".join(out for _, _, out in rows)
    if ".drt" in outputs:
        assert any(".drt" in read("src/%s.py" % m) for m in MODULES) \
            and "make_drt" in read("src/run_pipeline.py"), \
            "README lists a .drt output but nothing writes one"


def test_doc_anchors_resolve():
    def slugs(path):
        out = set()
        for line in read(path).splitlines():
            m = re.match(r"^#{1,6}\s+(.*)", line)
            if m:
                t = re.sub(r"[`*_]", "", m.group(1).strip())
                out.add(re.sub(r"[^\w\s-]", "", t).strip().lower().replace(" ", "-"))
        return out

    docs = {p: slugs(p) for p in shipped("docs/*.md")}
    broken = []
    for path in shipped("src/*.py", "*.md", "docs/*.md", "ui/*.js"):
        for doc, anchor in re.findall(r"(docs/[\w.-]+\.md)#([\w-]+)", read(path)):
            if doc not in docs or anchor not in docs[doc]:
                broken.append("%s -> %s#%s" % (path, doc, anchor))
    assert not broken, "dead doc anchors:\n  " + "\n  ".join(broken)


def test_relative_links_resolve():
    broken = []
    for path in shipped("*.md", "docs/*.md", "*/*.md"):
        base = os.path.dirname(path)
        for target in re.findall(r"\]\(([^)\s]+)\)", read(path)):
            if target.startswith(("http", "#", "mailto")):
                continue
            rel = target.split("#")[0]
            if rel and not os.path.exists(repo(os.path.normpath(os.path.join(base, rel)))):
                broken.append("%s -> %s" % (path, target))
    assert not broken, "dead relative links:\n  " + "\n  ".join(broken)


# ---------------------------------------------------------------- privacy

# This repo is a public extraction of a private pipeline developed against one streamer's
# VODs. None of that may ever reach it. Terms, not paths, because a leak historically
# arrived as a string inside an unrelated file.
PRIVATE = ["fixzplays", "ea_fc", "gameplay_commentary_malay", "2898820098", "Carrick",
           "/home/ny0ttt"]


@pytest.mark.parametrize("term", PRIVATE)
def test_no_private_data(term):
    hits = []
    for path in shipped():
        if not path or path == SELF:
            continue
        if os.path.islink(repo(path)):
            # A link ships its target string, and a broken one fails isfile() below.
            if term.lower() in os.readlink(repo(path)).lower():
                hits.append(path)
            continue
        if not os.path.isfile(repo(path)):
            continue
        try:
            blob = read(path)
        except (UnicodeDecodeError, IsADirectoryError):
            continue
        if term.lower() in blob.lower():
            hits.append(path)
    assert not hits, "%r appears in: %s" % (term, ", ".join(hits))


def test_no_symlink_leaves_the_repo():
    """Regression: a stray `ggml-` link into the private lab's whisper.cpp shipped in the
    first push. It was broken in every clone, so the isfile() scan above never saw it."""
    bad = []
    for path in shipped():
        if path and os.path.islink(repo(path)):
            target = os.path.realpath(repo(path))
            if not target.startswith(ROOT + os.sep):
                bad.append("%s -> %s" % (path, os.readlink(repo(path))))
    assert not bad, "symlinks out of the repo: %s" % ", ".join(bad)


# ---------------------------------------------------------------- dependencies

def test_runtime_lock_has_no_dev_tools():
    """pytest belongs in requirements-dev.txt; installing the pipeline must not pull it."""
    lock = read("requirements.lock").lower()
    for pkg in ("pytest", "pluggy", "iniconfig"):
        assert pkg not in lock, "%s is pinned in the runtime lock" % pkg


def test_readme_python_floor_matches_the_lock():
    """Regression: the README promised 3.10+ while the lock needed 3.12+, so installs on
    Ubuntu 22.04 and Debian 12 failed at the first command. The CI matrix proves the floor
    empirically; this only keeps the README honest about it."""
    floor = re.search(r"\*\*Python\*\*\s*\|\s*(\d+)\.(\d+)", read("README.md"))
    assert floor, "README Requirements table has no Python row"
    assert (int(floor.group(1)), int(floor.group(2))) >= (3, 12)


# ---------------------------------------------------------------- the .drt stage
#
# Ported from the lab repo on 2026-10-23 (plan/drt-in-orchestrator.md). The writer,
# drt_format.py and templates/drt/ were here all along; only the wiring was missing.

def test_drt_is_a_wired_timeline_option():
    """Regression: make_drt.py shipped but no option or stage ever called it."""
    import run_pipeline as rp

    assert "drt" in rp.OPTIONS, "no drt option"
    assert rp.OPTIONS["drt"]["group"] == "timeline"
    assert "drt" in {s["name"] for s in rp.STAGES}, "no drt stage"
    stage = rp.STAGE["drt"]
    assert stage["inputs"]["words"] == ("review", "transcript"), \
        "the .drt captions come from the reviewed, aligned transcript"


def test_default_options_are_spelled_out():
    """DEFAULT_OPTIONS was list(OPTIONS), so adding an option silently turned it on for
    every existing project. Keep it explicit, and keep it a real set of options."""
    import run_pipeline as rp

    assert "DEFAULT_OPTIONS = [" in read("src/run_pipeline.py"), \
        "DEFAULT_OPTIONS must be spelled out, not derived from OPTIONS"
    unknown = set(rp.DEFAULT_OPTIONS) - set(rp.OPTIONS)
    assert not unknown, "DEFAULT_OPTIONS names options that do not exist: %s" % sorted(unknown)
    missing = set(rp.OPTIONS) - set(rp.DEFAULT_OPTIONS)
    assert not missing, ("option %s is in OPTIONS but not DEFAULT_OPTIONS -- intentional? "
                         "say so here, do not just add it" % sorted(missing))


def test_drt_skips_instead_of_failing_on_other_frame_rates():
    """The templates are 60 fps Resolve exports, so the quickstart's 30 fps sample cannot
    make a .drt. It must SKIP with a readable reason, not fail the run: make_drt.py's own
    refusal would abort the stage."""
    import run_pipeline as rp

    class C:
        def __init__(self, options, fps):
            self.options = options
            self.m = {"stages": {"media": {"fps": fps} if fps else {}}}

    assert rp.drt_skip(C(["drt"], 60.0)) is None, "60 fps media must run the stage"

    msg = rp.drt_skip(C(["drt"], 30.0))
    assert msg and "30 fps" in msg and "60 fps" in msg, \
        "the skip must name both rates, not just say no: %r" % msg
    assert "FCPXML" in msg, "the skip must point at the format that does work"

    assert rp.drt_skip(C(["fcpxml"], 60.0)), "unticked drt must skip"
    # Projects made before the media stage recorded fps: no guard, let make_drt decide.
    assert rp.drt_skip(C(["drt"], None)) is None


def test_media_stage_records_the_fps_drt_skip_reads():
    """drt_skip runs on every UI refresh and must not shell out to ffprobe, so it reads
    stages.media.fps. If the media stage stops writing it the guard silently never fires."""
    import run_pipeline as rp

    assert rp.STAGE["media"].get("after") is rp.after_media, \
        "the media stage must run after_media, which records fps"
    assert 'rec["fps"]' in read("src/run_pipeline.py")
    assert 'c.m["stages"].get("media", {}).get("fps")' in read("src/run_pipeline.py"), \
        "drt_skip must read the fps the media stage recorded"


def test_a_stacked_layout_accepts_drt_as_well_as_fcpxml():
    """OTIO cannot draw a Short. Both formats that can must satisfy the check."""
    import run_pipeline as rp

    st = {"layouts": {"normal": {"title": "Normal 16:9", "kind": "full"},
                      "short": {"title": "Short", "kind": "stack"}},
          "webcam": {"x": 0, "y": 0, "w": 10, "h": 10}}
    assert not rp.option_problems(st, ["short"], ["drt"]), "drt alone must satisfy a Short"
    assert not rp.option_problems(st, ["short"], ["fcpxml"])
    assert rp.option_problems(st, ["short"], ["otio"]), "OTIO alone must not"


def test_drt_outputs_follow_the_ticked_layouts():
    import run_pipeline as rp

    assert rp.drt_outputs({"options": ["drt"], "layouts": ["normal", "short"]}) == {
        "normal": "timeline_normal_v{v}.drt", "short": "timeline_short_v{v}.drt"}
    assert rp.drt_outputs({"options": ["fcpxml"], "layouts": ["normal"]}) == {}, \
        "unticked drt must produce no outputs at all"


def test_drt_templates_the_code_names_all_ship():
    """make_drt.py builds every timeline from these; a rename would break at run time."""
    import make_drt

    for name in make_drt.TEMPLATE.values():
        assert os.path.exists(os.path.join(rp_templates(), name)), "missing template %s" % name
    assert make_drt.TEMPLATE_FPS == 60.0


def rp_templates():
    import run_pipeline as rp
    return rp.DRT_TEMPLATES


def test_a_finished_drt_is_offered_as_a_deliverable():
    """The stage wrote the file but deliverables() did not list it, so neither the CLI
    summary nor the UI ever showed it."""
    import run_pipeline as rp

    m = {"layouts": ["normal"], "options": ["drt", "fcpxml", "srt"],
         "stages": {"timeline": {"outputs": {"normal": "timeline_normal_v1.fcpxml"}},
                    "drt": {"status": "done", "outputs": {"normal": "timeline_normal_v1.drt"}},
                    "review": {"outputs": {"captions": "captions_final_v1.srt"}},
                    "media": {"outputs": {"media": "clip_edit_v1.mov"}}}}
    roles = [r for r, _, _ in rp.deliverables(m)]
    assert "drt_normal" in roles, "a finished .drt must be offered: %s" % roles
    assert roles.index("drt_normal") < roles.index("normal"), \
        "the animated timeline is the main deliverable when it exists"

    m["stages"]["drt"] = {"status": "skipped"}
    assert "drt_normal" not in [r for r, _, _ in rp.deliverables(m)], \
        "a skipped .drt must not be offered"
