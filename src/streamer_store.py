"""
Create / update streamer profiles (streamers/<name>.json) from the web UI's form.
docs/design-notes.md#unit-conversion.

A new profile starts as a copy of an existing one (whisper, captions, scorer, media and
layouts), then takes what the form says: name, channel(s), language, the webcam box drawn on a
real frame, and the Short's zoom/position (Resolve Inspector units, typed or read from
Resolve). Everything is validated here -- the form is never trusted.
"""

import copy
import os
import re
import time

import run_pipeline as rp

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,40}$")
LOGIN_RE = re.compile(r"^[A-Za-z0-9_]{2,25}$")
LANG_RE = re.compile(r"^[a-z]{2,3}$")

SHORT_DEFAULT = {"title": "Vertical Short (webcam top)", "kind": "stack", "size": [1080, 1920],
                 "webcam": {"track": 1, "zoom": 1.0, "x": 0.0, "y": 0.0},
                 "gameplay": {"track": 2, "zoom": 1.0, "x": 0.0, "y": 0.0},
                 "captions": {"_note": "Text titles on V3. Placeholder style until tuned in Resolve.",
                              "font": "Helvetica", "size": 60, "face": "Bold", "color": [1, 1, 1, 1],
                              "position": [0, -400], "min_s": 0.2}}


class ProfileError(Exception):
    pass


def _num(v, lo, hi, what):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
        raise ProfileError("%s must be a number between %g and %g" % (what, lo, hi))
    return float(v)


def _text(v, limit, what):
    if not isinstance(v, str) or not v.strip() or len(v) > limit or any(ord(c) < 32 for c in v):
        raise ProfileError("%s: one line, up to %d characters" % (what, limit))
    return " ".join(v.split())


def build(form, existing=None):
    """Form dict -> profile dict. `existing` = the profile being edited (None for new)."""
    if not isinstance(form, dict):
        raise ProfileError("bad form")
    name = form.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise ProfileError("profile name: 2-41 characters, lower-case letters, digits, - and _")
    if existing is None:
        src = form.get("copy_from")
        if src not in rp.list_streamers():
            raise ProfileError("copy settings from: pick an existing profile")
        prof = copy.deepcopy(rp.load_streamer(src))
        prof["_comment"] = ("STREAMER PROFILE for %s, made in the web UI %s from %s's settings "
                            "(docs/design-notes.md#unit-conversion)." % (name, time.strftime("%Y-%m-%d"), src))
    else:
        prof = copy.deepcopy(existing)
    prof["name"] = name
    prof["title"] = _text(form.get("title") or name, 60, "title")
    logins = form.get("twitch_logins") or []
    if not isinstance(logins, list) or not logins or len(logins) > 5 or \
            not all(isinstance(x, str) and LOGIN_RE.match(x) for x in logins):
        raise ProfileError("Twitch channel: at least one, letters/digits/_ only (as in the twitch.tv link)")
    prof["twitch_logins"] = list(dict.fromkeys(x.lower() for x in logins))
    lang = form.get("language")
    if not isinstance(lang, str) or not LANG_RE.match(lang):
        raise ProfileError("language: a whisper code like ms, en, id (never empty -- whisper would "
                           "translate into English)")
    prof.setdefault("whisper", {})["language"] = lang

    cam = form.get("webcam")
    layouts = {k: v for k, v in (prof.get("layouts") or {}).items()}
    layouts.setdefault("normal", {"title": "Normal 16:9", "kind": "full"})
    if cam is None:
        prof.pop("webcam", None)
        layouts.pop("short", None)
    else:
        if not isinstance(cam, dict):
            raise ProfileError("webcam: bad value")
        of = cam.get("of")
        if not isinstance(of, list) or len(of) != 2:
            raise ProfileError("webcam: grab a frame first")
        fw, fh = (int(_num(v, 160, 7680, "frame size")) for v in of)
        box = cam.get("box")
        if not isinstance(box, list) or len(box) != 4:
            raise ProfileError("webcam: draw the box on the frame")
        x, y, w, h = (int(round(_num(v, 0, 7680, "webcam box"))) for v in box)
        if w < 16 or h < 16 or x + w > fw or y + h > fh:
            raise ProfileError("webcam box must be at least 16x16 and inside the %dx%d frame" % (fw, fh))
        prof["webcam"] = {"_note": "Drawn on a %dx%d frame in the web UI, %s." % (fw, fh, time.strftime("%Y-%m-%d")),
                          "box": [x, y, w, h], "of": [fw, fh], "width": 1080}
        short = copy.deepcopy(layouts.get("short") or SHORT_DEFAULT)
        sf = form.get("short")
        if sf is not None:
            if not isinstance(sf, dict):
                raise ProfileError("short layout: bad value")
            for layer in ("webcam", "gameplay"):
                v = sf.get(layer)
                if not isinstance(v, dict):
                    raise ProfileError("short layout: %s values missing" % layer)
                short.setdefault(layer, {}).update(
                    zoom=_num(v.get("zoom"), 0.05, 20, "%s zoom" % layer),
                    x=_num(v.get("x", 0), -10000, 10000, "%s X" % layer),
                    y=_num(v.get("y", 0), -10000, 10000, "%s Y" % layer))
            top = bool(sf.get("gameplay_on_top", True))
            short["gameplay"]["track"], short["webcam"]["track"] = (2, 1) if top else (1, 2)
            if sf.get("from_resolve"):
                short["_from"] = _text(sf["from_resolve"], 200, "source note")
        layouts["short"] = short
    prof["layouts"] = dict(layouts)
    defaults = form.get("default_layouts", prof.get("default_layouts") or ["normal"])
    offered = [k for k in layouts if not k.startswith("_")]
    if not isinstance(defaults, list) or not defaults or not all(d in offered for d in defaults):
        raise ProfileError("default layouts must be some of: %s" % ", ".join(offered))
    prof["default_layouts"] = defaults
    problems = rp.layout_problems(prof, defaults)
    opts = form.get("default_options", prof.get("default_options"))
    if opts is not None:                   # none = everything on (docs/design-notes.md#orchestrator-guarantees)
        if not isinstance(opts, list) or not all(isinstance(o, str) for o in opts):
            raise ProfileError("default options must be a list")
        problems += rp.option_problems(prof, defaults, opts)
        prof["default_options"] = list(dict.fromkeys(opts))
    if problems:
        raise ProfileError("; ".join(problems))
    return prof


def save(form, update=False):
    """Validate and write streamers/<name>.json. New: refuses an existing name. Update: keeps
    the old file as <name>.json.bak-<time> (not *.json, so it never shows as a profile)."""
    name = form.get("name") if isinstance(form, dict) else None
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise ProfileError("profile name: 2-41 characters, lower-case letters, digits, - and _")
    path = rp.streamer_path(name)
    exists = os.path.exists(path)
    if update and not exists:
        raise ProfileError("no profile %r to update" % name)
    if not update and exists:
        raise ProfileError("a profile named %r already exists -- open it to edit, or pick another name" % name)
    for other in rp.list_streamers():
        if other != name:
            taken = set(x.lower() for x in rp.load_streamer(other).get("twitch_logins", [])) | {other.lower()}
            clash = taken & set(x.lower() for x in form.get("twitch_logins") or [] if isinstance(x, str))
            if clash:
                raise ProfileError("Twitch channel %s already belongs to profile %s" % (", ".join(sorted(clash)), other))
    prof = build(form, rp.load_streamer(name) if update else None)
    os.makedirs(rp.streamers_dir(), exist_ok=True)
    if update:
        with open(path, "rb") as f, open(path + ".bak-" + time.strftime("%Y%m%d-%H%M%S"), "wb") as b:
            b.write(f.read())
    rp.write_json_atomic(path, prof)
    return prof
