"use strict";
// Video Pipeline UI. No state of its own: every view is rebuilt from the server, which
// rebuilds it from the project folders on disk. All text goes in via textContent (h()).

const TOKEN = document.querySelector('meta[name="csrf-token"]').content;
const app = document.getElementById("app");
let timer = null;           // polling for the current view
let viewId = 0;             // stale responses from a previous view are dropped

// ------------------------------------------------------------------ helpers

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids.flat(Infinity)) {
    if (k === null || k === undefined || k === false) continue;
    el.append(k instanceof Node ? k : document.createTextNode(String(k)));
  }
  return el;
}

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST", headers: {"Content-Type": "application/json", "X-CSRF-Token": TOKEN},
    body: JSON.stringify(body)};
  const r = await fetch(path, opts);
  let data = {};
  try { data = await r.json(); } catch (e) { /* empty body */ }
  if (!r.ok) throw new Error(data.error || ("HTTP " + r.status));
  return data;
}

function toast(msg, isErr) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.className = "show" + (isErr ? " err" : "");
  clearTimeout(t._h);
  t._h = setTimeout(() => { t.className = ""; }, isErr ? 7000 : 3000);
}

const LABEL = {done: "✅ done", running: "⏳ running", stale: "⚠️ stale", missing: "⚠️ file missing",
  interrupted: "⏸ interrupted", failed: "❌ failed", waiting: "· waiting", skipped: "⤼ skipped",
  incomplete: "· incomplete", legacy: "made by hand", unreadable: "❌ unreadable"};
const badge = s => h("span", {class: "badge s-" + s}, LABEL[s] || s);

function dur(secs) {
  if (secs === null || secs === undefined) return "";
  secs = Math.round(secs);
  if (secs < 60) return secs + "s";
  if (secs < 3600) return Math.floor(secs / 60) + "m" + String(secs % 60).padStart(2, "0") + "s";
  return Math.floor(secs / 3600) + "h" + String(Math.floor(secs % 3600 / 60)).padStart(2, "0") + "m";
}
const clock = ms => {
  const s = Math.floor(ms / 1000);
  return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
};
function since(iso) {
  if (!iso) return "";
  return dur((Date.now() - Date.parse(iso)) / 1000);
}

function fill(el, ...nodes) {
  el.replaceChildren(...nodes.flat(Infinity).filter(n => n !== null && n !== undefined && n !== false));
}
const setView = (...nodes) => fill(app, ...nodes);

async function copy(text) {
  try { await navigator.clipboard.writeText(text); toast("Copied"); }
  catch (e) { toast("Couldn't copy -- select the path instead", true); }
}

// ------------------------------------------------------------------ router

function route() {
  clearInterval(timer);
  timer = null;
  document.onkeydown = null;
  const id = ++viewId;
  const full = location.hash.replace(/^#/, "") || "/";
  const [hash, query] = full.split("?");
  const q = new URLSearchParams(query || "");
  const m = hash.match(/^\/p\/([^/]+)(\/review)?$/);
  if (hash === "/new") return viewNew(id);
  if (hash === "/streamers") return viewStreamers(id);
  const sm = hash.match(/^\/streamers\/([^/]+)$/);
  if (sm) return viewStreamerForm(id, sm[1] === "new" ? null : decodeURIComponent(sm[1]), q);
  if (m && m[2]) return viewReview(id, decodeURIComponent(m[1]));
  if (m) return viewProject(id, decodeURIComponent(m[1]));
  return viewList(id);
}
window.addEventListener("hashchange", route);

// ------------------------------------------------------------------ runs list

async function viewList(id) {
  document.title = "Runs · Video Pipeline";
  async function load() {
    let list;
    try { list = await api("/api/projects"); } catch (e) { return toast(e.message, true); }
    if (id !== viewId) return;
    const rows = list.map(p => h("tr", {},
      h("td", {}, p.state === "legacy" ? h("span", {}, p.name)
        : h("a", {href: "#/p/" + encodeURIComponent(p.name)}, p.name),
        p.title ? h("div", {class: "muted small"}, p.title) : null),
      h("td", {}, p.range || "", p.length ? h("div", {class: "muted small"}, p.length) : null),
      h("td", {}, p.game || ""),
      h("td", {}, badge(p.state))));
    setView(
      h("div", {class: "row"}, h("h1", {class: "grow"}, "Runs"),
        h("a", {class: "btn primary", href: "#/new"}, "New run")),
      list.length ? h("div", {class: "panel"}, h("table", {class: "list"},
        h("thead", {}, h("tr", {}, h("th", {}, "Project"), h("th", {}, "Range"), h("th", {}, "Game"),
          h("th", {}, "State"))), h("tbody", {}, rows)))
        : h("div", {class: "panel"}, h("p", {}, "No runs yet."),
          h("a", {class: "btn primary", href: "#/new"}, "Start your first run")),
      h("p", {class: "muted small"}, "Everything here is read from the project folders. Closing this tab never stops a run."));
  }
  await load();
  timer = setInterval(load, 4000);
}

// ------------------------------------------------------------------ new run

async function viewNew(id) {
  document.title = "New run · Video Pipeline";
  let games = [], streamers = [];
  try { [games, streamers] = await Promise.all([api("/api/games"), api("/api/streamers")]); }
  catch (e) { toast(e.message, true); }
  if (id !== viewId) return;
  const vod = h("input", {type: "text", id: "vod", placeholder: "https://www.twitch.tv/channel/v/123456789",
    autocomplete: "off", required: true});
  const start = h("input", {type: "text", id: "start", placeholder: "01:14:00", autocomplete: "off"});
  const endEl = h("input", {type: "text", id: "end", placeholder: "01:16:30", autocomplete: "off"});
  const durEl = h("input", {type: "text", id: "duration", placeholder: "2m30s", autocomplete: "off"});
  let mode = "duration";
  const endBox = h("div", {}, h("label", {for: "end"}, "End time"), endEl);
  const durBox = h("div", {}, h("label", {for: "duration"}, "Length ", h("span", {class: "muted"}, "e.g. 2m30s, 2 mins 30 sec")), durEl);
  endBox.hidden = true;
  const modeRadios = h("div", {role: "radiogroup", "aria-label": "End or length"},
    h("label", {class: "radio"}, h("input", {type: "radio", name: "mode", value: "duration", checked: true,
      onchange: () => { mode = "duration"; endBox.hidden = true; durBox.hidden = false; invalidate(); }}), "Length"),
    h("label", {class: "radio"}, h("input", {type: "radio", name: "mode", value: "end",
      onchange: () => { mode = "end"; endBox.hidden = false; durBox.hidden = true; invalidate(); }}), "End time"));
  const game = h("select", {id: "game"}, h("option", {value: ""}, "Pick from the VOD's Twitch game"),
    games.map(g => h("option", {value: g.name}, g.title + (g.has_icons ? "" : " (no icons yet)"))));
  const result = h("div", {"aria-live": "polite"});
  const checkBtn = h("button", {type: "submit", class: "primary"}, "Check");
  const startBtn = h("button", {type: "button", class: "primary", disabled: true}, "Start run");
  let checked = null, okToStart = false;

  // Streamer: click a card. The VOD's Twitch channel pre-selects one after Check
  // ("detected from link"); a channel with no profile pre-selects nothing.
  let picked = null, pickedBy = null, layoutSel = new Set(), optionSel = new Set();
  let optionInfo = {};      // after Check: why an option can't work with this game (e.g. no icon map)
  const cardsBox = h("div", {class: "cards", role: "radiogroup", "aria-labelledby": "streamer-label"});
  const layoutsBox = h("div", {class: "layouts", role: "group", "aria-labelledby": "layouts-label"});
  const optionsBox = h("div", {class: "layouts", role: "group", "aria-labelledby": "options-label"});
  const card = name => streamers.find(x => x.name === name);
  const allowed = (c, l) => !l.needs_webcam || c.has_webcam;

  function pick(name, by) {
    const c = card(name);
    picked = name; pickedBy = by;
    layoutSel = new Set(c.default_layouts.filter(n => c.layouts.some(l => l.name === n && allowed(c, l))));
    if (!layoutSel.size && c.layouts.length) layoutSel.add(c.layouts[0].name);
    optionSel = new Set(c.default_options);
    renderPickers();
  }
  function renderPickers() {
    fill(cardsBox, streamers.length ? streamers.map(c => h("button", {type: "button", role: "radio",
      class: "card" + (c.name === picked ? " on" : ""), "aria-checked": c.name === picked ? "true" : "false",
      onclick: () => { pick(c.name, "you"); invalidate(); }},
      h("strong", {}, c.title),
      c.name === picked && pickedBy === "link" ? h("span", {class: "tag ok"}, "detected from link") : null,
      h("span", {class: "small muted"}, "webcam box " + (c.has_webcam ? "✓" : "✗")),
      h("span", {class: "small"}, c.layouts.map(l => l.title).join(" · ")))) :
      h("p", {class: "banner bad"}, "No streamer profiles in streamers/ yet."));
    const c = picked && card(picked);
    fill(layoutsBox, !c ? h("p", {class: "muted small mt0"}, "Pick a streamer to see its layouts -- or press Check and the VOD's channel picks it.")
      : c.layouts.map(l => h("label", {class: "radio"}, h("input", {type: "checkbox", value: l.name,
          checked: layoutSel.has(l.name), disabled: !allowed(c, l),
          onchange: ev => { if (ev.target.checked) layoutSel.add(l.name); else layoutSel.delete(l.name); invalidate(); }}),
        l.title, allowed(c, l) ? null : h("span", {class: "muted small"}, " (no webcam box for " + c.title + ")"))));
    fill(optionsBox, !c ? h("p", {class: "muted small mt0"}, "Pick a streamer to see what you can get.")
      : c.option_list.map(o => h("label", {class: "radio", title: o.help},
          h("input", {type: "checkbox", value: o.name, checked: optionSel.has(o.name),
            onchange: ev => { if (ev.target.checked) optionSel.add(o.name); else optionSel.delete(o.name); invalidate(); }}),
          o.title, optionInfo[o.name] ? h("span", {class: "muted small"}, " (" + optionInfo[o.name] + ")") : null)));
  }

  function body() {
    const b = {vod: vod.value, start: start.value, game: game.value};
    if (mode === "end") b.end = endEl.value; else b.duration = durEl.value;
    if (picked) {
      b.streamer = picked; b.layouts = [...layoutSel];
      b.options = card(picked).option_list.map(o => o.name).filter(n => optionSel.has(n));
    }
    return b;
  }
  function invalidate() {
    checked = null; startBtn.disabled = true;
    const panel = result.querySelector(".panel");
    if (panel && !panel.querySelector(".outdated"))
      panel.prepend(h("div", {class: "banner warn outdated"}, "Changed since the check -- press Check again before starting."));
  }
  for (const el of [vod, start, endEl, durEl, game]) el.addEventListener("input", invalidate);
  vod.addEventListener("input", () => { if (pickedBy === "link") { picked = null; pickedBy = null; renderPickers(); } });

  async function check(ev) {
    ev.preventDefault();
    checkBtn.disabled = true;
    fill(result, h("p", {class: "muted"}, "Checking the VOD on Twitch…"));
    try {
      const p = await api("/api/plan", body());
      if (id !== viewId) return;
      if (p.streamer && !picked && card(p.streamer)) pick(p.streamer, p.streamer_source === "twitch" ? "link" : "you");
      optionInfo = Object.fromEntries((p.option_list || []).filter(o => o.unavailable).map(o => [o.name, o.unavailable]));
      renderPickers();
      const s = p.source, est = p.estimate;
      okToStart = !p.problems.length && !p.exists && !!picked && layoutSel.size > 0;
      const who = p.streamer ? (card(p.streamer) || {title: p.streamer}).title : "no streamer picked";
      fill(result, h("div", {class: "panel"},
        h("div", {class: "row"}, h("strong", {class: "grow"}, s.title || p.name), h("code", {}, p.name)),
        h("p", {}, hmsText(s.start_s) + " → " + hmsText(s.end_s) + "  (" + dur(s.length_s) + ")  ·  " +
          who + " × " + p.game + (s.twitch_game ? "  ·  Twitch: " + s.twitch_game : "") +
          (s.twitch_login ? "  ·  channel " + s.twitch_login : "")),
        p.layouts && p.streamer ? h("p", {}, "Layouts: " + p.layouts.map(n => (p.layout_options.find(o => o.name === n) || {title: n}).title).join(", ")) : null,
        h("p", {}, "Estimate: ~" + dur(est.seconds) + " · ~" + (est.bytes / 1e9).toFixed(1) + " GB",
          h("span", {class: "muted small"}, est.learned.length ? "  (from your past runs)" : "  (default speeds until a run finishes)")),
        p.warnings.map(w => h("div", {class: "banner warn"}, "⚠️ " + w)),
        p.problems.map(w => {
          const login = (w.match(/no streamer profile for Twitch channel '([^']+)'/) || [])[1];
          return h("div", {class: "banner bad"}, "❌ " + w,
            login ? [" -- click a streamer card above, or ", h("a", {class: "btn", href: "#/streamers/new?" +
              new URLSearchParams({login, vod: vod.value, at: start.value})}, "Create profile for " + login)]
            : /pick a streamer/.test(w) ? " -- click a streamer card above, then Check again." : null);
        }),
        p.exists ? h("div", {class: "banner bad"}, "This range already has a project: ",
          h("a", {href: "#/p/" + encodeURIComponent(p.name)}, "open it")) : null,
        okToStart ? h("p", {class: "muted small"}, "Starting runs every stage through to the timelines and SRT -- no stops. " +
          "You can close this tab; the run keeps going.") : null));
      checked = okToStart ? JSON.stringify(body()) : null;
      startBtn.disabled = !okToStart;
      if (okToStart) startBtn.focus();
    } catch (e) {
      fill(result, h("div", {class: "banner bad"}, "❌ " + e.message));
    } finally { checkBtn.disabled = false; }
  }

  async function go() {
    if (!checked || checked !== JSON.stringify(body())) return invalidate();
    startBtn.disabled = true;
    startBtn.textContent = "Starting…";
    try {
      const r = await api("/api/runs", body());
      location.hash = "#/p/" + encodeURIComponent(r.name);
    } catch (e) {
      toast(e.message, true);
      startBtn.textContent = "Start run";
      startBtn.disabled = false;
    }
  }
  startBtn.addEventListener("click", go);

  renderPickers();
  setView(h("h1", {}, "New run"),
    h("p", {class: "muted"}, "Give the VOD link and the range. At the end you get one Resolve timeline per layout and the SRT captions."),
    h("form", {class: "panel", onsubmit: check},
      h("label", {for: "vod"}, "Twitch VOD link"), vod,
      h("div", {class: "fields"},
        h("div", {}, h("label", {for: "start"}, "Start time ", h("span", {class: "muted"}, "e.g. 01:14:00 or 1h14m")), start),
        h("div", {}, h("label", {}, "Until"), modeRadios),
        durBox, endBox),
      h("label", {for: "game"}, "Game"), game,
      h("label", {id: "streamer-label"}, "Streamer ", h("span", {class: "muted"}, "(the VOD's channel picks one; click to change)")), cardsBox,
      h("label", {id: "layouts-label"}, "Layouts ", h("span", {class: "muted"}, "(one Resolve timeline each)")), layoutsBox,
      h("label", {id: "options-label"}, "What you get ", h("span", {class: "muted"}, "(ticked from the streamer's defaults)")), optionsBox,
      h("div", {class: "row mt"}, checkBtn, startBtn)),
    result);
  vod.focus();
}

function hmsText(secs) {
  secs = Math.round(secs);
  return [Math.floor(secs / 3600), Math.floor(secs % 3600 / 60), secs % 60]
    .map(n => String(n).padStart(2, "0")).join(":");
}

// ------------------------------------------------------------------ project / run

async function viewProject(id, name) {
  let lastState = null;
  let choice = null;        // unapplied "What you get" ticks
  async function act(path, body, ok) {
    try { await api(path, body || {}); if (ok) toast(ok); load(); }
    catch (e) { toast(e.message, true); }
  }
  async function load() {
    let p;
    try { p = await api("/api/projects/" + encodeURIComponent(name)); }
    catch (e) { if (id === viewId) setView(h("div", {class: "banner bad"}, e.message)); return; }
    if (id !== viewId) return;
    document.title = name + " · " + (LABEL[p.state] || p.state);
    const s = p.source;
    const head = h("div", {class: "row"},
      h("div", {class: "grow"}, h("h1", {}, s.title || name),
        h("div", {class: "muted"}, p.route === "vod"
          ? hmsText(s.start_s) + " → " + hmsText(s.end_s) + " (" + dur(s.length_s) + ") · VOD " + s.vod_id
          : "clip " + s.clip, " · " + p.streamer + " × " + p.game)),
      badge(p.state));

    const actions = h("div", {class: "row actions"});
    if (p.live) actions.append(h("button", {class: "danger", onclick: () => {
      if (confirm("Cancel this run? The current step stops and its partial files are removed. Finished steps are kept; Resume continues later."))
        act("/api/projects/" + encodeURIComponent(name) + "/cancel", {}, "Cancelling…");
    }}, "Cancel run"));
    if (!p.live && ["interrupted", "failed", "stale", "incomplete"].includes(p.state))
      actions.append(h("button", {class: "primary", onclick: () =>
        act("/api/projects/" + encodeURIComponent(name) + "/resume", {}, "Resumed")}, "Resume"));
    actions.append(folderButton(name));
    if (p.can_review) actions.append(h("a", {class: "btn", href: "#/p/" + encodeURIComponent(name) + "/review"}, "Review words by ear"));
    if (!p.live) {
      const sel = h("select", {"aria-label": "Rebuild from stage", class: "w-auto"},
        p.stages.map(x => h("option", {value: x.name}, "from " + x.name)));
      actions.append(h("span", {class: "row nowrap"}, sel, h("button", {onclick: () => {
        if (confirm("Rebuild from " + sel.value + "? Makes new versions of that step and everything after it; old files are kept."))
          act("/api/projects/" + encodeURIComponent(name) + "/resume", {from: sel.value}, "Rebuild started");
      }}, "Rebuild")));
    }

    const stages = h("ul", {class: "stages"}, p.stages.map(x => h("li", {class: "s-" + x.state},
      h("span", {title: LABEL[x.state]}, (LABEL[x.state] || "").split(" ")[0]),
      h("strong", {}, x.name),
      h("span", {class: "mono small"}, x.outputs.join(", ")),
      h("span", {class: "small muted"}, x.state === "running" ? "running " + since(x.started)
        : [dur(x.seconds), x.cost_usd ? " · ~$" + x.cost_usd.toFixed(2) : ""].join("")),
      x.reason ? h("span", {class: "reason"}, (x.state === "failed" ? "→ " : "") + x.reason) : null)));

    let done = null;
    if (p.state === "done") {
      done = h("div", {class: "panel mt"},
        h("h2", {class: "mt0"}, "Ready for Resolve"),
        h("ul", {class: "files"}, p.deliverables.map(d => h("li", {},
          h("strong", {}, d.label + ": "),
          h("code", {}, d.path), " ", h("button", {class: "link", onclick: () => copy(d.path)}, "copy path")))),
        h("h2", {}, "Import checklist"),
        h("ol", {class: "checklist"},
          h("li", {}, "Resolve → File → Import → Timeline… → pick a .fcpxml (one per layout; each becomes its own timeline)"),
          p.options.includes("srt") ? h("li", {}, "Normal: drag the .srt from the Media Pool onto a subtitle track. Short: the captions are already on V3 as text") : null,
          h("li", {}, "Normal: highlights are on V1, everything else on V2 -- nothing was cut away"),
          h("li", {}, "Keep the .mov files where they are: the timelines point at them"),
          p.options.includes("otio") ? h("li", {class: "muted"}, "The .otio is a backup of the normal edit, kept until a full VOD imports fine as FCPXML") : null));
    }

    // ticks you haven't applied yet survive the 1.5 s refresh (null = show what the project has)
    const lays = choice ? choice.layouts : new Set(p.layouts);
    const opts = choice ? choice.options : new Set(p.options);
    const sameSet = (a, b) => a.size === b.length && b.every(n => a.has(n));
    const changed = () => !(sameSet(lays, p.layouts) && sameSet(opts, p.options));
    const btn = h("button", {class: changed() ? "primary" : "", disabled: p.live || !changed()}, "Update");
    function tick(set, n) {
      return ev => {
        choice = {layouts: lays, options: opts};
        if (ev.target.checked) set.add(n); else set.delete(n);
        btn.disabled = p.live || !changed();
        btn.classList.toggle("primary", changed());
      };
    }
    const optBox = (set, n, title, help, off) => h("label", {class: "radio", title: help || ""},
      h("input", {type: "checkbox", value: n, checked: set.has(n), disabled: p.live || !!off, onchange: tick(set, n)}),
      title, off ? h("span", {class: "muted small"}, " (" + off + ")") : null);
    btn.addEventListener("click", async () => {
      const body = {layouts: p.layout_options.map(l => l.name).filter(n => lays.has(n)),
        options: p.option_list.map(o => o.name).filter(n => opts.has(n))};
      choice = null;
      await act("/api/projects/" + encodeURIComponent(name) + "/choices", body, "Rebuilding the timelines…");
    });
    const groups = [["timeline", "Timeline files"], ["captions", "Captions file"], ["addon", "Add-ons"]];
    const choicesPanel = h("div", {class: "panel mt"}, h("h2", {class: "mt0"}, "What you get"),
      p.layout_options && p.layout_options.length ? h("div", {class: "row"}, h("strong", {class: "small"}, "Layouts"),
        p.layout_options.map(l => optBox(lays, l.name, l.title))) : null,
      groups.map(([g, label]) => h("div", {class: "row"}, h("strong", {class: "small"}, label),
        p.option_list.filter(o => o.group === g).map(o => optBox(opts, o.name, o.title, o.help, o.unavailable)))),
      h("div", {class: "row mt-s"}, btn),
      h("p", {class: "small muted"}, "Changing these rebuilds only the webcam clip, icons and timelines -- seconds. Old versions are kept."));
    const log = h("div", {class: "panel mt"},
      h("h2", {class: "mt0"}, "Log" + (p.log.stage ? " — " + p.log.stage : "")),
      h("div", {class: "log mono", tabindex: "0"}, p.log.lines.length ? p.log.lines.join("\n") : "(nothing yet)"),
      h("details", {}, h("summary", {class: "small"}, "Run history"),
        h("div", {class: "log mono"}, p.run_log.join("\n"))));

    setView(h("p", {class: "small"}, h("a", {href: "#/"}, "← Runs")), head, actions,
      p.live ? h("p", {class: "muted small"}, "Running in the background -- you can close this tab and come back.") : null,
      h("div", {class: "panel"}, stages), done, choicesPanel, log);
    const box = app.querySelector(".log");
    if (box) box.scrollTop = box.scrollHeight;
    if (lastState && lastState !== p.state && p.state === "done") toast("Run finished");
    lastState = p.state;
  }
  await load();
  timer = setInterval(load, 1500);
}

// ------------------------------------------------------------------ review by ear

// opens the project folder in the file manager (any state: to drag files into Resolve, check logs)
function folderButton(name) {
  return h("button", {type: "button", onclick: async () => {
    try { await api("/api/projects/" + encodeURIComponent(name) + "/open-folder", {}); }
    catch (e) { toast(e.message, true); }
  }}, "Go to folder");
}

async function viewReview(id, name) {
  let data;
  try { data = await api("/api/projects/" + encodeURIComponent(name) + "/review"); }
  catch (e) { return setView(h("p", {}, h("a", {href: "#/p/" + encodeURIComponent(name)}, "← Back")), h("div", {class: "banner bad"}, e.message)); }
  if (id !== viewId) return;
  document.title = "Review · " + name;
  const words = data.words;
  let decisions = data.decisions || {};
  let moved = data.moved || [];   // decisions made on an older transcript: redo by ear
  let sel = null;          // {first, last}
  let anchor = null;
  let onlyFixes = false;
  let follow = true;
  let stopAt = null;
  const norm = t => t.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, "");

  const video = data.video ? h("video", {src: data.video, controls: true, preload: "metadata"}) : h("p", {class: "muted"}, "No video file.");
  const linesBox = h("div", {class: "lines panel", tabindex: "-1"});
  const editor = h("div", {class: "editor panel", hidden: true, "aria-live": "polite"});
  const counts = h("span", {class: "small"});
  const applyBtn = h("button", {class: "primary"}, "Apply & rebuild captions + timeline");

  const key = (a, b) => a + "-" + b;

  // Display units: one of the reviewer's spans, or a single plain word.
  function units() {
    const ownAt = {};
    for (const [k, d] of Object.entries(decisions)) {
      const [a, b] = k.split("-").map(Number);
      ownAt[a] = {first: a, last: b, d};
    }
    const out = [];
    let i = 0;
    while (i < words.length) {
      const o = ownAt[i];
      if (o) { out.push({kind: "human", first: o.first, last: o.last, d: o.d}); i = o.last + 1; continue; }
      out.push({kind: "word", first: i, last: i}); i += 1;
    }
    return out;
  }
  const orig = (a, b) => words.slice(a, b + 1).map(w => w.text).join(" ");

  function unitView(u) {
    const isSel = sel && u.first >= sel.first && u.last <= sel.last;
    const attrs = {type: "button", "data-first": u.first, "data-last": u.last, onclick: ev => pick(u, ev.shiftKey)};
    if (u.kind === "word") return h("button", Object.assign(attrs, {class: "w" + (isSel ? " sel" : "")}), words[u.first].text);
    const cls = u.d.text ? "human" : "deleted";
    const text = u.d.text || orig(u.first, u.last);
    const tip = "Your fix. Whisper heard: " + orig(u.first, u.last);
    return h("button", Object.assign(attrs, {class: "w fix " + cls + (isSel ? " sel" : ""), title: tip, "aria-label": text + " — " + tip}), text);
  }

  function lineGroups() {
    // caption-like lines: break on sentence end, a pause over 0.7 s, or 9 words
    const us = units(), lines = [];
    let cur = [];
    for (const u of us) {
      const prev = cur.length ? cur[cur.length - 1] : null;
      if (prev && (words[u.first].start - words[prev.last].end > 700 || cur.length >= 9)) { lines.push(cur); cur = []; }
      cur.push(u);
      if (/[.?!]$/.test(words[u.last].text)) { lines.push(cur); cur = []; }
    }
    if (cur.length) lines.push(cur);
    return lines;
  }

  function render() {
    const lines = lineGroups();
    const rows = [];
    for (const ln of lines) {
      const hasFix = ln.some(u => u.kind !== "word");
      if (onlyFixes && !hasFix) continue;
      const t0 = words[ln[0].first].start, t1 = words[ln[ln.length - 1].last].end;
      rows.push(h("div", {class: "line", "data-start": t0, "data-end": t1},
        h("button", {class: "t", type: "button", title: "Play from here", onclick: () => seek(t0 - 300, null)}, clock(t0)),
        h("div", {}, ln.map((u, i) => [i ? " " : "", unitView(u)]))));
    }
    fill(linesBox, ...(rows.length ? rows : [h("p", {class: "muted"}, onlyFixes ? "No fixes." : "No words.")]));
    const own = Object.entries(decisions).filter(([, d]) => d.action === "text").length;
    counts.textContent = own + (own === 1 ? " fix of yours" : " fixes of yours");
    applyBtn.disabled = data.live || data.review_state === "done";
    applyBtn.title = applyBtn.disabled ? (data.live ? "Wait for the run to finish" : "Nothing new to apply") : "";
  }

  function seek(ms, until) {
    if (!video.play) return;
    video.currentTime = Math.max(0, ms / 1000);
    stopAt = until;
    video.play().catch(() => {});
  }
  if (video.addEventListener) video.addEventListener("timeupdate", () => {
    const ms = video.currentTime * 1000;
    if (stopAt !== null && ms >= stopAt) { video.pause(); stopAt = null; }
    let playing = null;
    for (const el of linesBox.querySelectorAll(".line")) {
      const on = ms >= +el.dataset.start && ms <= +el.dataset.end + 400;
      el.classList.toggle("playing", on);
      if (on) playing = el;
    }
    if (playing && follow && !video.paused) playing.scrollIntoView({block: "nearest"});
  });

  function pick(u, extend) {
    if (extend && anchor && u.kind === "word") {
      sel = {first: Math.min(anchor.first, u.first), last: Math.max(anchor.last, u.last)};
    } else {
      sel = {first: u.first, last: u.last};
      anchor = sel;
    }
    render();
    openEditor();
  }

  function openEditor() {
    const {first, last} = sel;
    const k = key(first, last);
    const d = decisions[k];
    const heard = orig(first, last);
    const input = h("input", {type: "text", id: "fixtext", value: d && d.action === "text" ? d.text : heard,
      "aria-label": "Your words", autocomplete: "off"});
    const single = first === last;
    const others = single ? words.filter(w => w.i !== first && norm(w.text) === norm(heard) &&
      !Object.keys(decisions).some(kk => { const [a, b] = kk.split("-").map(Number); return a <= w.i && w.i <= b; })) : [];
    const otherBoxes = others.map(w => h("input", {type: "checkbox", value: w.i}));
    const status = h("p", {class: "small muted"});

    async function save(newDec, extra) {
      const next = Object.assign({}, decisions);
      for (const [kk, v] of Object.entries(newDec)) { if (v === null) delete next[kk]; else next[kk] = v; }
      try {
        await api("/api/projects/" + encodeURIComponent(name) + "/review", {decisions: next, words_fp: data.words_fp});
        moved = [];
        decisions = next;
        if (extra) await extra();
        data.review_state = "stale";
        status.textContent = "Saved. Press Apply when you're done to rebuild the captions and timeline.";
        render();
        applyBtn.classList.add("primary");
      } catch (e) { toast(e.message, true); }
    }
    function saveText(text) {
      const dec = {};
      dec[k] = {action: "text", text};
      for (const cb of otherBoxes) if (cb.checked) dec[key(+cb.value, +cb.value)] = {action: "text", text};
      save(dec);
    }
    input.addEventListener("keydown", ev => { if (ev.key === "Enter") { ev.preventDefault(); saveText(input.value.trim()); } });

    editor.hidden = false;
    fill(editor, 
      h("div", {class: "row"}, h("strong", {class: "grow"}, clock(words[first].start) + "–" + clock(words[last].end)),
        h("button", {type: "button", onclick: () => seek(words[first].start - 1200, words[last].end + 900)}, "▶ Play this"),
        h("button", {type: "button", class: "link", onclick: closeEditor, "aria-label": "Close editor"}, "Close")),
      h("p", {class: "heard"}, "Whisper heard: ", h("strong", {}, heard)),
      h("div", {class: "opts"},
        h("button", {type: "button", onclick: () => saveText("")}, "Remove " + (single ? "this word" : "these words")),
        d ? h("button", {type: "button", class: "link", onclick: () => { const x = {}; x[k] = null; save(x); }}, "Undo my decision") : null),
      h("label", {for: "fixtext"}, "Your words ", h("span", {class: "muted"}, "(Enter saves)")),
      h("div", {class: "row"}, h("div", {class: "grow"}, input), h("button", {type: "button", class: "primary", onclick: () => saveText(input.value.trim())}, "Save")),
      others.length ? h("details", {}, h("summary", {}, "Also fix \"" + heard + "\" elsewhere (" + others.length + ")"),
        h("p", {class: "small muted"}, "Tick each one you've checked -- the same word can be right somewhere else."),
        h("div", {class: "others"}, others.map((w, i) => h("label", {}, otherBoxes[i],
          h("span", {}, clock(w.start) + "  …" + orig(Math.max(0, w.i - 4), Math.min(words.length - 1, w.i + 4)) + "…"),
          h("button", {type: "button", class: "link", onclick: () => seek(w.start - 1200, w.end + 900)}, "▶"))))) : null,
      status);
    input.focus();
  }
  function closeEditor() { sel = null; editor.hidden = true; render(); }

  function jump(dir) {
    const fixes = units().filter(u => u.kind !== "word");
    if (!fixes.length) return;
    const cur = sel ? sel.first : (dir > 0 ? -1 : words.length);
    const next = dir > 0 ? fixes.find(u => u.first > cur) || fixes[0] : [...fixes].reverse().find(u => u.first < cur) || fixes[fixes.length - 1];
    sel = {first: next.first, last: next.last}; anchor = sel;
    render(); openEditor();
    const el = linesBox.querySelector('[data-first="' + next.first + '"]');
    if (el) { el.scrollIntoView({block: "center"}); el.focus(); }
    seek(words[next.first].start - 1200, words[next.last].end + 900);
  }
  document.onkeydown = ev => {
    if (ev.target.closest("input, select, textarea")) { if (ev.key === "Escape") ev.target.blur(); return; }
    if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
    if (ev.key === "j" || ev.key === "J") { ev.preventDefault(); jump(1); }
    else if (ev.key === "k" || ev.key === "K") { ev.preventDefault(); jump(-1); }
    else if (ev.key === "Escape") closeEditor();
  };

  applyBtn.addEventListener("click", async () => {
    applyBtn.disabled = true;
    try {
      // saving the current decisions drops the moved ones, so the review stage can run
      if (moved.length) await api("/api/projects/" + encodeURIComponent(name) + "/review", {decisions, words_fp: data.words_fp});
      await api("/api/projects/" + encodeURIComponent(name) + "/resume", {});
      location.hash = "#/p/" + encodeURIComponent(name);
    } catch (e) { toast(e.message, true); applyBtn.disabled = false; }
  });

  const filter = h("input", {type: "checkbox", onchange: ev => { onlyFixes = ev.target.checked; render(); }});
  const followBox = h("input", {type: "checkbox", checked: true, onchange: ev => { follow = ev.target.checked; }});
  setView(
    h("p", {class: "small"}, h("a", {href: "#/p/" + encodeURIComponent(name)}, "← " + name)),
    h("div", {class: "row"}, h("h1", {class: "grow"}, "Review words by ear"), counts, folderButton(name)),
    data.live ? h("div", {class: "banner warn"}, "This project is running. You can listen, but saving waits until it finishes.") : null,
    moved.length ? h("div", {class: "banner warn"},
      "The words were re-transcribed, so " + moved.length + " of your decisions no longer match and were left out. Listen to these again and redo them if still needed (they're dropped when you save or Apply):",
      h("ul", {}, moved.map(m => h("li", {}, "“" + m.words + "” — " +
        (m.action === "text" ? "your fix “" + m.text + "”" : "your removal") +
        (m.now ? " (those word numbers now read “" + m.now + "”)" : ""))))) : null,
    h("div", {class: "review"},
      h("div", {class: "side"}, video, editor,
        h("div", {class: "panel mt-s"},
          h("p", {class: "legend"}, h("span", {class: "fix human"}, "your fix"), h("span", {class: "fix deleted"}, "removed")),
          h("p", {class: "small muted"}, h("span", {class: "kbd"}, "J"), " / ", h("span", {class: "kbd"}, "K"), " next / previous fix · ",
            "Shift-click to select several words · ", h("span", {class: "kbd"}, "Esc"), " close"),
          h("label", {class: "radio"}, filter, "Only lines with fixes"), " ",
          h("label", {class: "radio"}, followBox, "Follow playback"),
          h("div", {class: "mt-s"}, applyBtn),
          h("p", {class: "small muted"}, "Apply makes new caption + timeline versions from your decisions in seconds; old versions are kept."))),
      linesBox));
  render();
}

// ------------------------------------------------------------------ streamer profiles

async function viewStreamers(id) {
  document.title = "Streamers · Video Pipeline";
  let list = [];
  try { list = await api("/api/streamers"); } catch (e) { return toast(e.message, true); }
  if (id !== viewId) return;
  setView(h("div", {class: "row"}, h("h1", {class: "grow"}, "Streamers"),
      h("a", {class: "btn primary", href: "#/streamers/new"}, "New streamer")),
    h("p", {class: "muted"}, "A profile says how a streamer sounds (language), where their webcam is, and how their Short is laid out. A VOD link picks the profile from its Twitch channel."),
    h("div", {class: "cards"}, list.map(c => h("a", {class: "card", href: "#/streamers/" + encodeURIComponent(c.name)},
      h("strong", {}, c.title), h("span", {class: "small muted"}, "twitch.tv/" + (c.twitch_logins[0] || c.name)),
      h("span", {class: "small muted"}, "webcam box " + (c.has_webcam ? "✓" : "✗")),
      h("span", {class: "small"}, c.layouts.map(l => l.title).join(" · ")),
      h("span", {class: "small"}, "Edit →")))));
}

const LANGS = [["ms", "Malay (+ English code-switching)"], ["en", "English"], ["id", "Indonesian"], ["tl", "Filipino"],
  ["th", "Thai"], ["vi", "Vietnamese"], ["zh", "Chinese"], ["ja", "Japanese"], ["ko", "Korean"], ["es", "Spanish"],
  ["pt", "Portuguese"], ["ar", "Arabic"], ["hi", "Hindi"], ["ta", "Tamil"]];

async function viewStreamerForm(id, editName, q) {
  document.title = (editName ? "Edit " + editName : "New streamer") + " · Video Pipeline";
  let list = [], prof = null;
  try {
    list = await api("/api/streamers");
    if (editName) prof = await api("/api/streamers/" + encodeURIComponent(editName));
  } catch (e) { return setView(h("div", {class: "banner bad"}, e.message)); }
  if (id !== viewId) return;
  let base = prof;            // where layout values come from
  const login = (q.get("login") || "").replace(/[^A-Za-z0-9_]/g, "");
  const nameEl = h("input", {type: "text", id: "pname", value: editName || login.toLowerCase(), disabled: !!editName,
    autocomplete: "off", "aria-describedby": "pname-help"});
  const titleEl = h("input", {type: "text", id: "ptitle", value: prof ? prof.title || editName : login, autocomplete: "off"});
  const loginsEl = h("input", {type: "text", id: "plogins", autocomplete: "off",
    value: prof ? (prof.twitch_logins || []).join(", ") : login.toLowerCase()});
  const curLang = prof ? (prof.whisper || {}).language : "ms";
  const langEl = h("select", {id: "plang"}, LANGS.concat(LANGS.some(l => l[0] === curLang) ? [] : [[curLang, curLang]])
    .map(([c, t]) => h("option", {value: c, selected: c === curLang}, t + " (" + c + ")")));
  const copyEl = editName ? null : h("select", {id: "pcopy"}, list.map(c => h("option", {value: c.name}, c.title)));

  // ---- webcam box, in pixels of the grabbed frame (`of`)
  let box = prof && prof.webcam ? prof.webcam.box.slice() : null;
  let of = prof && prof.webcam ? prof.webcam.of.slice() : null;
  const hasCam = h("input", {type: "checkbox", checked: !prof || !!prof.webcam, onchange: () => { camBox.hidden = !hasCam.checked; shortBox.hidden = !hasCam.checked; syncDefaults(); }});
  const vodEl = h("input", {type: "text", id: "fvod", autocomplete: "off", value: q.get("vod") || "",
    placeholder: "https://www.twitch.tv/" + (login || "channel") + "/v/…"});
  const atEl = h("input", {type: "text", id: "fat", autocomplete: "off", value: q.get("at") || "", placeholder: "01:14:00"});
  const grabBtn = h("button", {type: "button"}, "Grab frame");
  const img = h("img", {alt: "A frame of the stream -- drag a box over the webcam", draggable: "false"});
  const rect = h("div", {class: "rect", hidden: true}, h("div", {class: "handle", "aria-hidden": "true"}));
  const frameBox = h("div", {class: "framebox", hidden: true}, img, rect);
  const nums = ["x", "y", "w", "h"].map((k, i) => h("input", {type: "number", min: 0, step: 1, "aria-label": "Box " + k,
    class: "num", oninput: ev => { if (!box) box = [0, 0, 16, 16]; box[i] = Math.max(0, Math.round(+ev.target.value || 0)); drawRect(); }}));
  const boxInfo = h("span", {class: "small muted"});

  function drawRect() {
    nums.forEach((n, i) => { if (box && document.activeElement !== n) n.value = box[i]; });
    boxInfo.textContent = of ? "in a " + of[0] + "×" + of[1] + " frame" : "grab a frame to draw it";
    if (!box || !of || frameBox.hidden || !img.clientWidth) { rect.hidden = true; return; }
    const k = img.clientWidth / of[0];
    rect.hidden = false;
    rect.style.left = box[0] * k + "px"; rect.style.top = box[1] * k + "px";
    rect.style.width = box[2] * k + "px"; rect.style.height = box[3] * k + "px";
  }
  img.addEventListener("load", drawRect);
  window.addEventListener("resize", drawRect);

  let drag = null;
  frameBox.addEventListener("pointerdown", ev => {
    if (!of) return;
    ev.preventDefault();
    const r = img.getBoundingClientRect(), k = of[0] / r.width;
    const px = (ev.clientX - r.left) * k, py = (ev.clientY - r.top) * k;
    const mode = ev.target.classList.contains("handle") ? "resize" : ev.target === rect ? "move" : "draw";
    if (mode === "draw") box = [Math.round(px), Math.round(py), 16, 16];
    drag = {mode, px, py, start: box.slice(), k, r};
    try { frameBox.setPointerCapture(ev.pointerId); } catch (e) { /* synthetic events in tests */ }
    drawRect();
  });
  frameBox.addEventListener("pointermove", ev => {
    if (!drag) return;
    const px = (ev.clientX - drag.r.left) * drag.k, py = (ev.clientY - drag.r.top) * drag.k;
    const [x, y, w, hh] = drag.start, dx = px - drag.px, dy = py - drag.py;
    if (drag.mode === "move") box = [x + dx, y + dy, w, hh];
    else box = [Math.min(x, x + w + dx), Math.min(y, y + hh + dy), Math.abs(w + dx), Math.abs(hh + dy)];
    if (drag.mode === "draw") box = [Math.min(drag.px, px), Math.min(drag.py, py), Math.abs(px - drag.px), Math.abs(py - drag.py)];
    box = box.map(Math.round);
    box[2] = Math.max(16, Math.min(box[2], of[0])); box[3] = Math.max(16, Math.min(box[3], of[1]));
    box[0] = Math.max(0, Math.min(box[0], of[0] - box[2])); box[1] = Math.max(0, Math.min(box[1], of[1] - box[3]));
    drawRect();
  });
  const endDrag = () => { drag = null; };
  frameBox.addEventListener("pointerup", endDrag);
  frameBox.addEventListener("pointercancel", endDrag);

  grabBtn.addEventListener("click", async () => {
    grabBtn.disabled = true; grabBtn.textContent = "Downloading a few seconds…";
    try {
      const f = await api("/api/streamer-frame", {vod: vodEl.value, at: atEl.value});
      if (box && of && (of[0] !== f.width || of[1] !== f.height)) {
        const sx = f.width / of[0], sy = f.height / of[1];
        box = [box[0] * sx, box[1] * sy, box[2] * sx, box[3] * sy].map(Math.round);
      }
      if (!box && base && base.webcam) {           // start from the copied profile's box
        const sx = f.width / base.webcam.of[0], sy = f.height / base.webcam.of[1];
        box = base.webcam.box.map((v, i) => Math.round(v * (i % 2 ? sy : sx)));
      }
      of = [f.width, f.height];
      frameBox.hidden = false;
      img.src = f.url;
      toast("Drag a box over the webcam -- drag inside it to move, the corner to resize");
    } catch (e) { toast(e.message, true); }
    finally { grabBtn.disabled = false; grabBtn.textContent = "Grab frame"; }
  });

  // ---- Short layout (Resolve Inspector units)
  const layer = (name, v) => {
    const f = {zoom: h("input", {type: "number", step: "0.01", class: "num", "aria-label": name + " zoom", value: v.zoom}),
      x: h("input", {type: "number", step: "0.001", class: "num", "aria-label": name + " position X", value: v.x || 0}),
      y: h("input", {type: "number", step: "0.001", class: "num", "aria-label": name + " position Y", value: v.y || 0})};
    return {f, row: h("div", {class: "row"}, h("strong", {class: "lname"}, name),
      h("label", {class: "radio"}, "Zoom ", f.zoom), h("label", {class: "radio"}, "X ", f.x), h("label", {class: "radio"}, "Y ", f.y))};
  };
  let cam = null, game = null, onTop = null, fromResolve = null;
  const shortRows = h("div", {});
  const readInfo = h("p", {class: "small muted"});
  function fillShort(lay) {
    lay = lay || {webcam: {zoom: 1, x: 0, y: 0, track: 1}, gameplay: {zoom: 1, x: 0, y: 0, track: 2}};
    cam = layer("Webcam", lay.webcam); game = layer("Gameplay", lay.gameplay);
    onTop = h("input", {type: "checkbox", checked: (lay.gameplay.track || 2) > (lay.webcam.track || 1)});
    fill(shortRows, cam.row, game.row, h("label", {class: "radio mt-s"}, onTop, "Gameplay on top of the webcam (gameplay on V2)"));
  }
  const readBtn = h("button", {type: "button", onclick: async () => {
    try {
      const r = await api("/api/resolve-read", {width: 1080, height: 1920});
      for (const [lay, vals] of [[cam, r.webcam], [game, r.gameplay]]) {
        lay.f.zoom.value = vals.zoom; lay.f.x.value = vals.x; lay.f.y.value = vals.y;
      }
      fromResolve = "Resolve project " + r.project + ", timeline " + r.timeline + ", saved " + r.saved_at;
      const odd = ["webcam", "gameplay"].filter(k => r[k].unknown || r[k].zoom_y);
      const varies = ["webcam", "gameplay"].filter(k => r[k].differs).map(k =>
        r[k].differs.different + " of the " + r[k].differs.clips + " " + k + " clips have different values");
      readInfo.textContent = "Read from " + fromResolve + " (first clip of each)." +
        (varies.length ? " Note: " + varies.join("; ") + " -- adjust the first clip if that's the look you want." : "") +
        (odd.length ? " Note: " + odd.join(" and ") + " has a value I can't map yet (uneven zoom or an unknown setting) -- check by eye." : "");
      toast("Values read from Resolve");
    } catch (e) { toast(e.message, true); readInfo.textContent = e.message; }
  }}, "Read from Resolve");
  const shortBox = h("div", {}, h("h2", {}, "Vertical Short"),
    h("p", {class: "small muted"}, "Zoom and position as Resolve's Inspector shows them on a 1080×1920 timeline. Easiest: import a Short, adjust the webcam and gameplay clips in Resolve, save the project (Ctrl+S), then press Read from Resolve."),
    shortRows, h("div", {class: "row mt-s"}, readBtn), readInfo);
  const camBox = h("div", {},
    h("div", {class: "fields"}, h("div", {}, h("label", {for: "fvod"}, "A VOD of this streamer"), vodEl),
      h("div", {}, h("label", {for: "fat"}, "At ", h("span", {class: "muted"}, "a moment the webcam is on screen")), atEl)),
    h("div", {class: "row mt-s"}, grabBtn), frameBox,
    h("div", {class: "row mt-s"}, h("span", {class: "small"}, "Box"), nums, boxInfo));

  // ---- default layouts
  const defNormal = h("input", {type: "checkbox", checked: true});
  const defShort = h("input", {type: "checkbox"});
  const optionDefs = (await api("/api/streamers")).find(() => true);
  const allOptions = optionDefs ? optionDefs.option_list : [];
  const defOpts = Object.fromEntries(allOptions.map(o => [o.name, h("input", {type: "checkbox", checked: true})]));
  const setOpts = d => { for (const [n, el] of Object.entries(defOpts)) el.checked = !d || d.includes(n); };
  function syncDefaults() { defShort.disabled = !hasCam.checked; if (!hasCam.checked) defShort.checked = false; }

  async function loadBase(name) {
    try { base = await api("/api/streamers/" + encodeURIComponent(name)); } catch (e) { base = null; }
    fillShort(base && base.layouts && base.layouts.short);
    const d = (base && base.default_layouts) || ["normal"];
    defNormal.checked = d.includes("normal"); defShort.checked = d.includes("short");
    setOpts(base && base.default_options);
  }
  if (editName) {
    fillShort(prof.layouts && prof.layouts.short);
    const d = prof.default_layouts || ["normal"];
    defNormal.checked = d.includes("normal"); defShort.checked = d.includes("short");
    setOpts(prof.default_options);
  } else {
    await loadBase(copyEl.value);
    copyEl.addEventListener("change", () => loadBase(copyEl.value));
  }
  if (id !== viewId) return;

  const saveBtn = h("button", {type: "submit", class: "primary"}, editName ? "Save changes" : "Create profile");
  async function save(ev) {
    ev.preventDefault();
    const form = {name: nameEl.value.trim(), title: titleEl.value.trim(), language: langEl.value,
      twitch_logins: loginsEl.value.split(/[\s,]+/).filter(Boolean),
      default_layouts: [defNormal.checked ? "normal" : null, defShort.checked ? "short" : null].filter(Boolean),
      default_options: allOptions.map(o => o.name).filter(n => defOpts[n].checked)};
    if (copyEl) form.copy_from = copyEl.value;
    if (hasCam.checked) {
      if (!box || !of) return toast("Grab a frame and draw the webcam box first (or untick 'shows a webcam')", true);
      form.webcam = {box, of};
      form.short = {webcam: {zoom: +cam.f.zoom.value, x: +cam.f.x.value, y: +cam.f.y.value},
        gameplay: {zoom: +game.f.zoom.value, x: +game.f.x.value, y: +game.f.y.value}, gameplay_on_top: onTop.checked};
      if (fromResolve) form.short.from_resolve = fromResolve;
    } else form.webcam = null;
    saveBtn.disabled = true;
    try {
      const r = await api("/api/streamers", {profile: form, update: !!editName});
      toast(editName ? "Saved" : "Profile created");
      location.hash = "#/streamers/" + encodeURIComponent(r.name);
    } catch (e) { toast(e.message, true); }
    finally { saveBtn.disabled = false; }
  }

  syncDefaults();
  camBox.hidden = !hasCam.checked; shortBox.hidden = !hasCam.checked;
  setView(h("p", {class: "small"}, h("a", {href: "#/streamers"}, "← Streamers")),
    h("h1", {}, editName ? "Edit " + (prof.title || editName) : "New streamer"),
    h("form", {class: "panel", onsubmit: save},
      h("div", {class: "fields"},
        h("div", {}, h("label", {for: "pname"}, "Profile name"), nameEl,
          h("p", {id: "pname-help", class: "small muted mt0"}, editName ? "Can't be renamed." : "lower-case letters, digits, - and _")),
        h("div", {}, h("label", {for: "ptitle"}, "Display name"), titleEl),
        h("div", {}, h("label", {for: "plogins"}, "Twitch channel(s) ", h("span", {class: "muted"}, "as in twitch.tv/…")), loginsEl),
        h("div", {}, h("label", {for: "plang"}, "Language they speak"), langEl)),
      copyEl ? [h("label", {for: "pcopy"}, "Copy settings from ", h("span", {class: "muted"}, "(whisper, captions, highlight scoring, layouts)")), copyEl] : null,
      h("h2", {}, "Webcam"),
      h("label", {class: "radio"}, hasCam, "This streamer shows a webcam"),
      camBox, shortBox,
      h("h2", {}, "Ticked by default on New run"),
      h("div", {class: "layouts"}, h("label", {class: "radio"}, defNormal, "Normal 16:9"),
        h("label", {class: "radio"}, defShort, "Vertical Short")),
      h("div", {class: "layouts"}, allOptions.map(o => h("label", {class: "radio", title: o.help}, defOpts[o.name], o.title))),
      h("div", {class: "row mt"}, saveBtn)));
  drawRect();
}

route();
