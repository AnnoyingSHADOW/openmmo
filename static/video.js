"use strict";
/* Video maker: topic -> Claude script -> narration in a cloned voice -> Pollinations images -> MP4 with captions.
   Uses helpers from app.js ($, api, jsonOpts, fmtDur, setMsg-style elements). */

const vs = { config: null, project: null, job: null, imagesJob: null, aspect: "9:16", voices: [], inited: false };
const CPS = { km: 17, en: 15 }; // spoken characters per second, for the length estimate
const PREFS_KEY = "vc.videoPrefs.v2"; // v2: Koulen became the default caption font

function loadPrefs() { try { return JSON.parse(localStorage.getItem(PREFS_KEY) || "{}"); } catch { return {}; } }
function savePrefs(p) { try { localStorage.setItem(PREFS_KEY, JSON.stringify(p)); } catch {} }

// ------------------------------------------------------------------ tabs
function showTab(name) {
  document.querySelectorAll(".tab").forEach((t) => {
    const on = t.dataset.tab === name;
    t.classList.toggle("active", on); t.setAttribute("aria-selected", on);
  });
  $("#tab-speech").hidden = name !== "speech";
  $("#tab-video").hidden = name !== "video";
  try { localStorage.setItem("vc.tab", name); } catch {}
  if (name === "video") vInit();
}
document.querySelectorAll(".tab").forEach((t) => (t.onclick = () => showTab(t.dataset.tab)));

// ------------------------------------------------------------------ init
async function vInit() {
  await Promise.all([vLoadConfig(), vLoadVoices(), vLoadGallery(), loadAssets(), loadMusic()]);
  if (!vs.inited) {
    vs.inited = true;
    const prefs = loadPrefs();
    if (!prefs.imgFix1) {  // one-time: FLUX schnell now needs pollen, and a stuck "gradient" choice gave placeholder pictures
      prefs.imgFix1 = true;
      prefs.image_source = "pollinations";
      prefs.image_model = "tongyi-mai/z-image-turbo";
      savePrefs(prefs);
    }
    applySettings(prefs);
  }
  updateRenderButton();
}

async function vLoadConfig() {
  vs.config = await api("/api/video/config");
  const c = vs.config;
  const model = $("#v-image-model"), keep = model.value;
  model.innerHTML = "";
  c.image_models.forEach((m) => model.add(new Option(m.label, m.id)));
  if (keep) model.value = keep;
  const font = $("#v-font"), keepF = font.value;
  font.innerHTML = "";
  c.fonts.forEach((f) => font.add(new Option((c.bundled_fonts || []).includes(f) ? `${f} (project font)` : f, f)));
  if (keepF) font.value = keepF;
  $("#v-key-status").textContent = c.has_pollinations_key
    ? `✓ saved ${c.key_hint}${c.key_from_env ? " (from environment)" : ""}`
    : "not set: get a free key at enter.pollinations.ai";
  $("#v-key-status").className = c.has_pollinations_key ? "ok" : "muted";
  const li = c.local_images || {};
  const lm = $("#v-local-model"), keepL = lm.value;
  lm.innerHTML = "";
  (li.models || []).forEach((m) => lm.add(new Option(m.label + (m.downloaded ? "" : ` (${m.size_gb} GB download)`), m.id)));
  lm.value = keepL && [...lm.options].some((o) => o.value === keepL) ? keepL : (li.default || lm.value);
  $("#v-local-model-field").hidden = !li.installed;
  $("#v-local-status").className = li.installed ? "ok" : "err";
  if (!c.claude_available) {
    $("#v-write").disabled = true;
    $("#v-write-msg").textContent = "Claude Code CLI not found. Install it and run `claude` once to sign in.";
  }
  syncImageSource();
}

async function vLoadVoices() {
  vs.voices = await api("/api/voices");
  const sel = $("#v-voice"), keep = sel.value;
  sel.innerHTML = "";
  vs.voices.forEach((v) => sel.add(new Option(v.name, v.id)));
  sel.add(new Option("✦ No clone: describe a voice in Voice style", ""));
  if (keep !== "" && vs.voices.some((v) => v.id === keep)) sel.value = keep;
}

// ------------------------------------------------------------------ settings <-> controls
function collectSettings() {
  return {
    voice_id: $("#v-voice").value || null,
    voice_style: $("#v-voice-style").value.trim(),
    use_transcript: true,
    aspect: vs.aspect,
    image_source: $("#v-image-source").value,
    image_model: $("#v-image-model").value,
    local_model: $("#v-local-model").value,
    image_style: $("#v-image-style").value,
    caption_font: $("#v-font").value,
    caption_style: $("#v-cap-style").value,
    caption_highlight: $("#v-highlight").value,
    highlight_color: $("#v-hl-color").value,
    show_title: $("#v-show-title").checked,
    captions: $("#v-captions").checked,
    logo_asset: $("#v-logo").value || null,
    logo_pos: $("#v-logo-pos").value || "tr",
    logo_size: $("#v-logo-size").value || "s",
    sfx: $("#v-sfx").checked,
    music: $("#v-music").value || null,
    music_volume: +$("#v-music-vol").value || 0.15,
  };
}
function applySettings(s) {
  if (!s) return;
  if (s.voice_id && [...$("#v-voice").options].some((o) => o.value === s.voice_id)) $("#v-voice").value = s.voice_id;
  if (s.voice_style !== undefined) $("#v-voice-style").value = s.voice_style;
  if (s.aspect) setAspect(s.aspect);
  const setSel = (id, v) => { if (v && [...$(id).options].some((o) => o.value === v)) $(id).value = v; };
  setSel("#v-image-source", s.image_source);
  setSel("#v-image-model", s.image_model);
  setSel("#v-local-model", s.local_model);
  setSel("#v-image-style", s.image_style);
  setSel("#v-font", s.caption_font);
  setSel("#v-cap-style", s.caption_style);
  setSel("#v-highlight", s.caption_highlight);
  if (s.highlight_color) $("#v-hl-color").value = s.highlight_color;
  syncHighlight();
  if (s.show_title !== undefined) $("#v-show-title").checked = s.show_title;
  if (s.captions !== undefined) $("#v-captions").checked = s.captions;
  if (s.logo_asset !== undefined && !vs.assetsLoaded) vs.pendingLogo = s.logo_asset || "";  // applied once assets load
  setSel("#v-logo", s.logo_asset);
  setSel("#v-logo-pos", s.logo_pos);
  setSel("#v-logo-size", s.logo_size);
  if (s.sfx !== undefined) $("#v-sfx").checked = s.sfx;
  if (s.music !== undefined && !vs.musicLoaded) vs.pendingMusic = s.music || "";  // applied once the list loads
  if (s.music !== undefined) $("#v-music").value = [...$("#v-music").options].some((o) => o.value === s.music) ? s.music : "";
  if (s.music_volume) $("#v-music-vol").value = String(s.music_volume);
  syncMusic();
  if (s.language) $("#v-lang").value = s.language;
  if (s.seconds) $("#v-seconds").value = String(s.seconds);
  if (s.claude_model) $("#v-claude").value = s.claude_model;
  if (s.mode && MODE_COPY[s.mode]) setMode(s.mode);
  syncImageSource();
}
function rememberPrefs() {
  savePrefs({ ...collectSettings(), language: $("#v-lang").value, seconds: +$("#v-seconds").value, claude_model: $("#v-claude").value, mode: vs.mode });
}

function setAspect(a) {
  vs.aspect = a;
  document.querySelectorAll("#v-aspect .seg").forEach((b) => b.classList.toggle("active", b.dataset.v === a));
}
document.querySelectorAll("#v-aspect .seg").forEach((b) => (b.onclick = () => { setAspect(b.dataset.v); rememberPrefs(); }));

function syncImageSource() {
  const src = $("#v-image-source").value, pol = src === "pollinations";
  $("#v-model-field").hidden = !pol;
  $("#v-key-box").hidden = !pol;
  $("#v-local-box").hidden = src !== "local";
  syncLocalHelp();
  updateRenderButton();
}

const LOCAL_SPEED = { "flux2-klein-4b": "about 25 s per picture", "z-image-turbo": "about 1.5 minutes per picture",
                      "sd-turbo": "a few seconds per picture", "sdxl-turbo": "about 30-60 s per picture on a 4 GB GPU" };
const LOCAL_SETUP_ARG = { "z-image-turbo": "best", "flux2-klein-4b": "fast", "sd-turbo": "fast", "sdxl-turbo": "best" };
function syncLocalHelp() {
  const li = (vs.config && vs.config.local_images) || {};
  const m = (li.models || []).find((x) => x.id === $("#v-local-model").value);
  const cmd = li.setup_cmd || "./setup-local-images.sh", win = li.backend === "cuda";
  $("#v-local-status").textContent = !li.installed ? "not set up" : li.loaded && m && li.loaded === m.id ? "✓ model loaded" : "✓ ready";
  $("#v-local-help").innerHTML = !li.installed
    ? (win ? `One-time setup: in a terminal, run <code>${cmd}</code> in the app folder (installs PyTorch + diffusers and a 2.6 GB model; <code>${cmd} best</code> adds SDXL-Turbo, 7 GB), then restart the app.`
           : `One-time setup: in Terminal, run <code>${cmd}</code> in the app folder (installs mflux and a 4.6 GB model), then restart <code>./run.sh</code>.`)
    : (win ? "Runs on your NVIDIA GPU: no key, no credits, nothing leaves this computer. " : "Runs on your Mac's GPU: no key, no credits, nothing leaves the Mac. ")
      + (m ? `${LOCAL_SPEED[m.id] || ""}; the first one also loads the model.` : "")
      + (m && !m.downloaded ? ` Not downloaded yet: run <code>${cmd} ${LOCAL_SETUP_ARG[m.id] || "fast"}</code> first (${m.size_gb} GB), or it downloads slowly on first use.` : "")
      + " After switching models, click “New AI picture for every scene”.";
}
$("#v-local-model").addEventListener("change", () => { syncLocalHelp(); rememberPrefs(); });

// Why the chosen image source can't make pictures yet ("" when it can).
function imageSourceProblem() {
  const src = $("#v-image-source").value, c = vs.config || {};
  if (src === "pollinations" && !c.has_pollinations_key) return "Add a Pollinations key, or choose another image source.";
  if (src === "local" && !(c.local_images && c.local_images.installed)) return "Set up local images first (see “On this computer”).";
  return "";
}
$("#v-image-source").onchange = () => { syncImageSource(); rememberPrefs(); };
function syncHighlight() {
  const off = $("#v-highlight").value === "none" || !$("#v-captions").checked;
  $("#v-hl-color").disabled = off;
  document.querySelectorAll(".sw").forEach((b) => { b.disabled = off; b.classList.toggle("on", b.dataset.c === $("#v-hl-color").value.toLowerCase()); });
}
document.querySelectorAll(".sw").forEach((b) => (b.onclick = () => { $("#v-hl-color").value = b.dataset.c; syncHighlight(); rememberPrefs(); }));
$("#v-hl-color").addEventListener("input", syncHighlight);
$("#v-highlight").addEventListener("change", syncHighlight);
$("#v-captions").addEventListener("change", syncHighlight);
["#v-highlight", "#v-hl-color", "#v-voice", "#v-image-model", "#v-image-style", "#v-font", "#v-cap-style", "#v-show-title", "#v-captions", "#v-lang", "#v-seconds", "#v-claude"]
  .forEach((id) => $(id).addEventListener("change", rememberPrefs));
$("#v-voice-style").addEventListener("change", rememberPrefs);

$("#v-key-save").onclick = async () => {
  const key = $("#v-key").value.trim();
  try {
    vs.config = await api("/api/video/config", jsonOpts("PUT", { pollinations_key: key }));
    $("#v-key").value = "";
    await vLoadConfig();
  } catch (e) { $("#v-key-status").textContent = e.message; $("#v-key-status").className = "err"; }
  updateRenderButton();
};

function updateRenderButton() {
  const btn = $("#v-render"), hint = $("#v-render-hint");
  const allOwn = vs.project && vs.project.scenes.length && vs.project.scenes.every((sc) => sc.custom_image);
  const sourceProblem = allOwn ? "" : imageSourceProblem();
  let why = "";
  if (!vs.project) why = "Write a script first.";
  else if (vs.job) why = "";
  else if (sourceProblem) why = sourceProblem;
  else if (!$("#v-voice").value && !$("#v-voice-style").value.trim()) why = "Pick a narrator voice.";
  else if (!state.ready) why = "Waiting for the voice model…";
  btn.disabled = !!why || !!vs.job || !!vs.imagesJob;
  if (vs.project) updateImagesButton();
  btn.textContent = vs.job ? "Making video…" : (vs.project && vs.project.render ? "Re-make video" : "Make video");
  hint.textContent = why;
}
$("#v-voice-style").addEventListener("input", updateRenderButton);
$("#v-voice").addEventListener("change", updateRenderButton);

// ------------------------------------------------------------------ script
// ---- logo & asset library (data/assets), placed on scenes at render time
vs.assets = [];
const POS_LABELS = { tl: "Top left", tc: "Top centre", tr: "Top right", c: "Centre", bl: "Bottom left", bc: "Bottom centre", br: "Bottom right" };
const SIZE_LABELS = { xs: "Tiny", s: "Small", m: "Medium", l: "Large", xl: "Huge" };
const SIZE_SHARE = { xs: 0.10, s: 0.16, m: 0.25, l: 0.40, xl: 0.60 };
function fillOptions(sel, labels, value) {
  sel.innerHTML = "";
  Object.entries(labels).forEach(([v, t]) => sel.add(new Option(t, v)));
  if (value) sel.value = value;
}
fillOptions($("#v-logo-pos"), POS_LABELS, "tr");
fillOptions($("#v-logo-size"), SIZE_LABELS, "s");
const assetUrl = (id) => `/api/assets/${id}.png`;

// ------------------------------------------------------------------ background music
async function loadMusic() {
  try { vs.music = await api("/api/music"); } catch { vs.music = []; }
  vs.musicLoaded = true;
  renderMusic();
}
function renderMusic() {
  const sel = $("#v-music"), keep = vs.pendingMusic ?? sel.value;
  sel.innerHTML = '<option value="">None</option>';
  (vs.music || []).forEach((m) => sel.add(new Option(`${m.name} (${Math.floor(m.seconds / 60)}:${String(Math.round(m.seconds % 60)).padStart(2, "0")})`, m.id)));
  sel.value = [...sel.options].some((o) => o.value === keep) ? keep : "";
  vs.pendingMusic = undefined;
  syncMusic();
}
function syncMusic() {
  const id = $("#v-music").value, prev = $("#v-music-preview");
  $("#v-music-del").hidden = !id;
  const pct = `${Math.round(+$("#v-music-vol").value * 100)}%`;
  $("#v-music-vol-label").textContent = id ? pct : `${pct} · add or pick a track first`;
  prev.hidden = !id;
  const src = id ? `/api/music/${id}/audio` : "";
  if (src && !prev.src.endsWith(src)) prev.src = src;
  if (!src) { prev.pause(); prev.removeAttribute("src"); }
}
$("#v-music").onchange = () => { syncMusic(); rememberPrefs(); };
$("#v-music-vol").oninput = () => { syncMusic(); rememberPrefs(); };
$("#v-sfx").onchange = () => rememberPrefs();
$("#v-music-add").onclick = () => $("#v-music-file").click();
$("#v-music-file").onchange = async () => {
  const file = $("#v-music-file").files[0];
  $("#v-music-file").value = "";
  if (!file) return;
  const m = $("#v-music-msg");
  m.textContent = "Adding…";
  try {
    const fd = new FormData(); fd.append("file", file, file.name);
    vs.music = await api("/api/music", { method: "POST", body: fd });
    vs.pendingMusic = vs.music[vs.music.length - 1]?.id || "";
    renderMusic(); rememberPrefs();
    m.textContent = "Added. Use music you have the rights to (your own, or royalty-free).";
  } catch (e) { m.textContent = e.message; }
};
$("#v-music-del").onclick = async () => {
  const id = $("#v-music").value, track = (vs.music || []).find((x) => x.id === id);
  if (!track || !confirm(`Delete “${track.name}” from your music library?`)) return;
  vs.music = await api(`/api/music/${id}`, { method: "DELETE" });
  renderMusic(); rememberPrefs();
};

async function loadAssets() {
  try { vs.assets = await api("/api/assets"); } catch { vs.assets = []; }
  vs.assetsLoaded = true;
  renderAssetLibrary();
}
function renderAssetLibrary() {
  const grid = $("#v-assets");
  grid.innerHTML = vs.assets.length ? "" : '<span class="small muted">No assets yet. Add your logo, a sticker or a product photo.</span>';
  vs.assets.forEach((a) => {
    const t = document.createElement("div");
    t.className = "asset";
    t.title = `${a.name} (${a.w}×${a.h}). Click to copy asset:${a.id} to use it as a diagram icon.`;
    t.innerHTML = `<img alt="" src="${assetUrl(a.id)}" /><button type="button" class="asset-del" aria-label="Delete ${a.name}">✕</button>`;
    $("img", t).onclick = async () => {  // copy the code that puts this picture in a diagram box
      try { await navigator.clipboard.writeText(`asset:${a.id}`); $("#v-asset-msg").textContent = `Copied asset:${a.id}. Paste it after [id] in a diagram box line.`; }
      catch { $("#v-asset-msg").textContent = `Diagram icon code: asset:${a.id}`; }
    };
    $(".asset-del", t).onclick = async () => {
      if (!confirm(`Delete “${a.name}” from your asset library? Scenes that use it will show without it.`)) return;
      vs.assets = await api(`/api/assets/${a.id}`, { method: "DELETE" });
      renderAssetLibrary();
    };
    grid.appendChild(t);
  });
  const logo = $("#v-logo"), keep = vs.pendingLogo ?? logo.value;
  logo.innerHTML = '<option value="">None</option>';
  vs.assets.forEach((a) => logo.add(new Option(a.name, a.id)));
  logo.value = vs.assets.some((a) => a.id === keep) ? keep : "";
  vs.pendingLogo = undefined;
  if (vs.project) renderScenes();
}
$("#v-asset-add").onclick = () => $("#v-asset-file").click();
$("#v-asset-file").onchange = async () => {
  const files = [...$("#v-asset-file").files];
  $("#v-asset-file").value = "";
  if (!files.length) return;
  const m = $("#v-asset-msg");
  m.textContent = `Adding ${files.length} file${files.length > 1 ? "s" : ""}…`; m.className = "small muted";
  try {
    const fd = new FormData();
    files.forEach((f) => fd.append("files", f, f.name));
    fd.append("remove_white", $("#v-asset-white").checked ? "true" : "false");
    vs.assets = await api("/api/assets", { method: "POST", body: fd });
    m.textContent = "";
    renderAssetLibrary();
  } catch (e) { m.textContent = e.message; m.className = "small err"; }
};
["#v-logo", "#v-logo-pos", "#v-logo-size"].forEach((id) => $(id).addEventListener("change", () => {
  rememberPrefs();
  document.querySelectorAll("#v-scenes .scene").forEach((el) => {
    const sc = vs.project && vs.project.scenes.find((x) => (x.id || "") === el.dataset.sid);
    if (sc) paintOverlays(el, sc);
  });
}));

// Logo + scene assets drawn on the scene thumbnail, at the same place and size as in the video.
function paintOverlays(el, sc) {
  const media = $(".scene-media", el);
  media.querySelectorAll(".sc-ov").forEach((n) => n.remove());
  const [aw, ah] = ((vs.project && vs.project.settings && vs.project.settings.aspect) || vs.aspect || "9:16").split(":").map(Number);
  const items = (sc.overlays || []).filter((o) => vs.assets.some((a) => a.id === o.asset)).map((o) => ({ ...o, opacity: 1 }));
  const logo = $("#v-logo").value;
  if (logo && vs.assets.some((a) => a.id === logo)) items.push({ asset: logo, pos: $("#v-logo-pos").value, size: $("#v-logo-size").value, opacity: 0.9 });
  const mx = (4.5 * Math.min(aw, ah)) / aw, my = (4.5 * Math.min(aw, ah)) / ah;  // margin in % of width / height
  items.forEach((o) => {
    const im = document.createElement("img");
    im.className = "sc-ov";
    im.src = assetUrl(o.asset);
    im.style.width = `${(SIZE_SHARE[o.size] || 0.16) * 100}%`;
    im.style.opacity = o.opacity;
    const p = o.pos || "c", st = im.style;
    let tx = "0", ty = "0";
    if (p[0] === "t") st.top = `${my}%`; else if (p[0] === "b") st.bottom = `${my}%`; else { st.top = "50%"; ty = "-50%"; }
    if (p === "c") { st.left = "50%"; tx = "-50%"; }
    else if (p[1] === "l") st.left = `${mx}%`; else if (p[1] === "r") st.right = `${mx}%`; else { st.left = "50%"; tx = "-50%"; }
    st.transform = `translate(${tx}, ${ty})`;
    media.appendChild(im);
  });
}

function renderOverlayRows(el, sc) {
  const box = $(".sc-ovs", el);
  box.innerHTML = "";
  (sc.overlays || []).forEach((o, k) => {
    const row = document.createElement("div");
    row.className = "ov-row";
    row.innerHTML = `<img alt="" /><select class="ov-asset"></select><select class="ov-pos"></select><select class="ov-size"></select>
      <button type="button" class="btn btn-ghost btn-xs danger" aria-label="Remove asset from scene">✕</button>`;
    const a = $(".ov-asset", row);
    vs.assets.forEach((x) => a.add(new Option(x.name, x.id)));
    if (!vs.assets.some((x) => x.id === o.asset)) a.add(new Option("(deleted asset)", o.asset));
    a.value = o.asset;
    fillOptions($(".ov-pos", row), POS_LABELS, o.pos);
    fillOptions($(".ov-size", row), SIZE_LABELS, o.size);
    $("img", row).src = assetUrl(o.asset);
    const update = () => {
      o.asset = a.value; o.pos = $(".ov-pos", row).value; o.size = $(".ov-size", row).value;
      $("img", row).src = assetUrl(o.asset);
      paintOverlays(el, sc);
    };
    row.querySelectorAll("select").forEach((x) => (x.onchange = update));
    $("button", row).onclick = () => { sc.overlays.splice(k, 1); renderOverlayRows(el, sc); paintOverlays(el, sc); };
    box.appendChild(row);
  });
}

// ---- input mode: Claude writes the script, or keep the pasted words exactly
vs.mode = "write";
const MODE_COPY = {
  write: {
    label: "Topic, idea or source text",
    ph: "A topic (e.g. ហេតុអ្វីគួរផឹកទឹកពេលព្រឹក · 3 tips to sleep better), or paste an article / notes / story for Claude to turn into a script.",
    btn: "Write script with Claude",
  },
  tutorial: {
    label: "Lesson topic",
    ph: "What to teach, e.g. Python for loops for total beginners · How Wi-Fi works · Excel SUM and AVERAGE. Add the level or what to cover if you like.",
    btn: "Write tutorial with Claude",
  },
  keep: {
    label: "Your script or story (read word for word)",
    ph: "Paste your full story or script. Claude only splits it into scenes and writes the image prompts; the narration stays exactly as you wrote it. Blank lines between paragraphs help.",
    btn: "Split into scenes with Claude",
  },
};
function setMode(m) {
  vs.mode = m;
  document.querySelectorAll("#v-mode .seg").forEach((b) => b.classList.toggle("active", b.dataset.v === m));
  $("#v-topic-label").textContent = MODE_COPY[m].label;
  $("#v-topic").placeholder = MODE_COPY[m].ph;
  $("#v-write").textContent = MODE_COPY[m].btn;
  $("#v-length-field").hidden = m === "keep";
  $("#v-lang-field").hidden = m === "keep"; // keep mode: language comes from the pasted text
  $("#v-suggest").hidden = m !== "tutorial";
  if (m === "tutorial") renderSuggestions();
  updateTopicCount();
}

// ---- suggested lessons (Tutorial mode): click a chip to use it as the topic
vs.madeTopics = new Set();
function suggestGroup() {
  const groups = window.LESSON_TOPICS || [];
  let g = null;
  try { g = localStorage.getItem("vc.lessonGroup"); } catch {}
  return groups.some((x) => x.group === g) ? g : (groups[0] && groups[0].group);
}
function renderSuggestions() {
  const groups = window.LESSON_TOPICS || [];
  const tabs = $("#v-suggest-groups"), list = $("#v-suggest-list");
  if (!groups.length) { $("#v-suggest").hidden = true; return; }
  const current = suggestGroup();
  tabs.innerHTML = "";
  groups.forEach((g) => {
    const made = g.items.filter((it) => vs.madeTopics.has(it.prompt)).length;
    const b = document.createElement("button");
    b.type = "button";
    b.className = "seg" + (g.group === current ? " active" : "");
    b.textContent = made ? `${g.group} ${made}/${g.items.length}` : g.group;
    b.onclick = () => { try { localStorage.setItem("vc.lessonGroup", g.group); } catch {} renderSuggestions(); };
    tabs.appendChild(b);
  });
  list.innerHTML = "";
  const topic = $("#v-topic").value.trim();
  groups.find((g) => g.group === current).items.forEach((it, k) => {
    const c = document.createElement("button");
    c.type = "button";
    const made = vs.madeTopics.has(it.prompt);
    c.className = "chip" + (made ? " made" : "") + (topic === it.prompt ? " active" : "");
    c.textContent = `${k + 1}. ${it.label}${made ? " ✓" : ""}`;
    c.title = it.prompt;
    c.onclick = () => {
      $("#v-topic").value = it.prompt;
      updateTopicCount();
      renderSuggestions();
      $("#v-topic").focus();
    };
    list.appendChild(c);
  });
}
document.querySelectorAll("#v-mode .seg").forEach((b) => (b.onclick = () => { setMode(b.dataset.v); rememberPrefs(); }));
function updateTopicCount() {
  const t = $("#v-topic").value;
  const spoken = t.replace(/[\u200b\u200c\u2060\ufeff]/g, "").replace(/\s+/g, " ").trim();
  const khmer = /[\u1780-\u17ff]/.test(spoken);
  let txt = spoken.length ? `${spoken.length.toLocaleString()} chars` : "";
  if (vs.mode === "keep" && spoken.length) {
    const secs = spoken.length / (khmer ? CPS.km : CPS.en);
    txt += ` · about ${secs >= 90 ? (secs / 60).toFixed(1) + " min" : Math.round(secs) + " s"} of narration`;
    if (secs > 330) txt += " (limit 5 min)";
  }
  $("#v-topic-count").textContent = txt;
  $("#v-topic-count").className = "small " + (vs.mode === "keep" && spoken.length / (khmer ? CPS.km : CPS.en) > 330 ? "err" : "muted");
}
$("#v-topic").addEventListener("input", updateTopicCount);

$("#v-write").onclick = async () => {
  const topic = $("#v-topic").value.trim();
  const msg = $("#v-write-msg");
  if (topic.length < 2) { msg.textContent = "Type a topic first."; return; }
  const btn = $("#v-write");
  btn.disabled = true; btn.textContent = vs.mode === "keep" ? "Claude is splitting…" : "Claude is writing…";
  const long = vs.mode === "keep" ? topic.length > 2500 : +$("#v-seconds").value > 90;
  msg.textContent = long ? "Long video: this can take 1–3 minutes." : "Usually 10–30 seconds.";
  try {
    rememberPrefs();
    const p = await api("/api/videos/script", jsonOpts("POST", {
      topic, language: $("#v-lang").value, seconds: +$("#v-seconds").value,
      claude_model: $("#v-claude").value, settings: collectSettings(), mode: vs.mode,
    }));
    msg.textContent = `Script ready: ${p.scenes.length} scenes. Edit anything, then make the video.`;
    showProject(p);
    vLoadGallery();
  } catch (e) { msg.textContent = e.message; }
  btn.disabled = !(vs.config && vs.config.claude_available); btn.textContent = MODE_COPY[vs.mode].btn;
};

function showProject(p) {
  vs.project = p;
  $("#v-editor").hidden = false;
  $("#v-title").value = p.title || "";
  applySettings(p.settings);
  renderScenes();
  showVideo(p);
  updateRenderButton();
  $("#v-error").hidden = !p.error || p.status !== "error";
  if (p.error && p.status === "error") $("#v-error").textContent = p.error;
}

function sceneImageUrl(p, sc) {
  const file = sc.custom_image || sc.ai_image;
  return file ? `/api/videos/${p.id}/files/${file}?v=${sc.image_v || 0}` : "";
}

function textHash(str) {
  let h = 0;
  for (let k = 0; k < str.length; k++) h = (Math.imul(31, h) + str.charCodeAt(k)) | 0;
  return (h >>> 0).toString(36);
}
// Slide preview, drawn by the server from the saved project (so it changes when the saved text/settings change).
function slideUrl(p, sc) {
  if (!sc.id) return "";
  const st = p.settings || {}, i = p.scenes.indexOf(sc);
  const v = textHash(JSON.stringify([sc.layout, sc.savedSlide ?? sc.slide, st.aspect, st.caption_font, st.highlight_color,
    st.captions, p.title, i, p.scenes.length, sc.custom_image, sc.ai_image, sc.image_v]));
  return `/api/videos/${p.id}/scenes/${sc.id}/slide?v=${v}`;
}
function paintSceneMedia(el, sc) {
  paintPictureOrSlide(el, sc);
  paintOverlays(el, sc);
}
function paintPictureOrSlide(el, sc) {
  const p = vs.project;
  const media = $(".scene-media", el), pic = $("img", media), badge = $(".sc-badge", el);
  if (sc.layout) {
    const url = slideUrl(p, sc);
    media.classList.add("slide-pic");
    media.classList.toggle("empty-pic", !url);
    $(".sc-empty", media).textContent = url ? "" : "Save to preview";
    if (url && !pic.src.endsWith(url)) pic.src = url;
    const edited = sc.slideEdited ? " · edited, update preview" : "";
    badge.textContent = (sc.layout === "cover" ? (sc.custom_image ? "Title · your picture" : sc.ai_image ? "Title · AI picture" : "Title · picture not made yet") : "Slide") + edited;
    badge.className = "sc-badge" + (sc.slideEdited || (sc.layout === "cover" && sc.stale) ? " stale" : sc.custom_image ? " own" : "");
    $(".sc-revert", el).hidden = !(sc.layout === "cover" && sc.custom_image);
    return;
  }
  const url = sceneImageUrl(p, sc);
  media.classList.toggle("empty-pic", !url);
  if (url && !pic.src.endsWith(url)) pic.src = url;
  badge.textContent = sc.custom_image ? "Your image" : sc.ai_image ? (sc.stale ? "AI · prompt changed" : "AI image") : "No image yet";
  badge.className = "sc-badge" + (sc.custom_image ? " own" : sc.stale ? " stale" : "");
  $(".sc-revert", el).hidden = !sc.custom_image;
}

const ASPECT_WORDS = { "9:16": "vertical 9:16 (portrait, like a phone screen)", "16:9": "horizontal 16:9 (landscape)", "1:1": "square 1:1" };
function externalPrompt(sc) {
  // A prompt to paste into Gemini / ChatGPT / any image generator, matching this video's settings.
  const style = $("#v-image-style").value;
  return `Generate an image: ${(sc.image_prompt || "").trim()}\n\nStyle: ${style}. Aspect ratio: ${ASPECT_WORDS[vs.aspect] || vs.aspect}. ` +
    "No text, letters, captions, watermarks or logos in the image.";
}

// ---- animated slides: "+++" splits a slide into parts, "|" in the narration says when each part appears
const DIAGRAM_TEMPLATE = "```diagram\n[you] tabler:device-laptop You\n[server] tabler:server Server | [db] tabler:database Database\n+++\nyou -> server: request\n+++\nserver -> db: query\ndb --> server: data\n+++\nserver --> you: answer\n```";
const QUIZ_TEMPLATE = "# Quiz\n```quiz\nWhat does this code print?\n- [ ] first answer\n- [x] the right answer\n- [ ] a common mistake\n```";
function slideParts(md) {
  // "+++" and "+++ focus N" lines, plus one part per quiz (its answer reveal)
  const parts = (md.match(/^[ \t]*\+\+\+(?:[ \t]+focus[ \t]+(?:off|\d[\d,\s-]*))?[ \t]*$/gim) || []).length
    + (md.match(/^[ \t]*```[ \t]*quiz[ \t]*$/gim) || []).length;
  if (parts) return { parts, auto: false };
  let arrows = 0;  // a diagram without +++: each arrow plays on its own
  for (const m of md.matchAll(/^[ \t]*```[ \t]*(?:diagram|flow|arch)[ \t]*\n([\s\S]*?)^[ \t]*```/gim))
    arrows += m[1].split("\n").filter((l) => /^\s*[\w-]+\s*(<-->|<->|-->|->)\s*[\w-]+/.test(l)).length;
  return { parts: arrows, auto: arrows > 0 };
}
function addSlideLogo(ta, name) {  // add to the slide's [logo] line, or start one at the top
  const lines = ta.value.split("\n"), k = lines.findIndex((l) => /^\s*\[logo\]/i.test(l));
  if (k >= 0) { if (!lines[k].split(/\s+/).includes(name)) lines[k] = `${lines[k].trimEnd()} ${name}`; }
  else lines.unshift(`[logo] ${name}`);
  ta.value = lines.join("\n");
  ta.dispatchEvent(new Event("input"));
}
function partsStatus(md, narration) {
  const { parts, auto } = slideParts(md || "");
  const logo = /^\s*\[logo\]/im.test(md || "");
  if (!parts) return { text: logo ? "The logo pops in at the start. Add “+ Part” to make more things appear one by one." : "Still slide. Add “+ Part” to make things appear one by one.", warn: false };
  const marks = ((narration || "").match(/\|/g) || []).length;
  const what = auto ? `${parts} arrows play one by one` : `${parts + 1} parts appear one by one`;
  if (marks === parts) return { text: `${what}, timed by the ${marks} “|” in the narration ✓`, warn: false };
  if (!marks) return { text: `${what}. Put ${parts} “|” in the narration to time them (now spread over the sentences).`, warn: true };
  return { text: `${what}, but the narration has ${marks} “|” (needs ${parts}).`, warn: true };
}
function insertText(ta, text) {
  const s = ta.selectionStart ?? ta.value.length, e = ta.selectionEnd ?? s;
  ta.value = ta.value.slice(0, s) + text + ta.value.slice(e);
  ta.focus(); ta.setSelectionRange(s + text.length, s + text.length);
  ta.dispatchEvent(new Event("input"));
}
function insertLine(ta, text) {  // on its own line(s) at the cursor
  const s = ta.selectionStart ?? ta.value.length, before = ta.value.slice(0, s), after = ta.value.slice(ta.selectionEnd ?? s);
  insertText(ta, (before && !before.endsWith("\n") ? "\n" : "") + text + (after.startsWith("\n") ? "" : "\n"));
}
function openSlidePlayer(p, sid) {
  const [w, h] = { "9:16": [1080, 1920], "16:9": [1920, 1080], "1:1": [1080, 1080] }[(p.settings || {}).aspect] || [1080, 1920];
  const veil = document.createElement("div");
  veil.className = "player-veil";
  veil.innerHTML = `<div class="player-box" role="dialog" aria-label="Slide animation">
    <div class="player-frame"><iframe title="Slide animation" scrolling="no"></iframe></div>
    <div class="player-bar"><span class="small muted">Plays in a loop. Timing is estimated from the text; the video follows the real voice.</span>
      <button type="button" class="btn btn-ghost btn-xs">Close</button></div></div>`;
  document.body.appendChild(veil);
  const scale = Math.min((window.innerWidth - 48) / w, (window.innerHeight - 120) / h);
  const frame = $(".player-frame", veil), ifr = $("iframe", veil);
  frame.style.width = `${Math.floor(w * scale)}px`; frame.style.height = `${Math.floor(h * scale)}px`;
  Object.assign(ifr.style, { width: `${w}px`, height: `${h}px`, transform: `scale(${scale})` });
  ifr.src = `/api/videos/${p.id}/scenes/${sid}/play?t=${Date.now()}`;
  const onKey = (e) => { if (e.key === "Escape") close(); };
  const close = () => { veil.remove(); document.removeEventListener("keydown", onKey); };
  document.addEventListener("keydown", onKey);
  veil.onclick = (e) => { if (e.target === veil) close(); };
  $(".player-bar button", veil).onclick = close;
}

function setSceneBusy(el, busy, text = "") {
  $(".scene-media", el).classList.toggle("busy", busy);
  $(".sc-busy", el).textContent = text;
  el.querySelectorAll(".scene-tools button").forEach((b) => (b.disabled = busy));
}

function renderScenes() {
  const box = $("#v-scenes");
  box.innerHTML = "";
  const p = vs.project;
  const lang = p.language || "km";
  p.scenes.forEach((sc, i) => {
    const el = document.createElement("div");
    el.className = "scene";
    el.dataset.sid = sc.id || "";
    el.innerHTML = `
      <div class="scene-side">
        <div class="scene-media" title="Drop an image here to use it for this scene">
          <img alt="" />
          <span class="sc-empty small">No image yet</span>
          <span class="sc-busy small"></span>
        </div>
        <span class="sc-badge"></span>
        <div class="scene-tools">
          <button type="button" class="btn btn-ghost btn-xs sc-new" title="Generate a different AI picture now (same prompt)">New AI image</button>
          <button type="button" class="btn btn-ghost btn-xs sc-up" title="Use your own picture for this scene">Upload</button>
          <button type="button" class="btn btn-ghost btn-xs sc-copy" title="Copy a ready-to-paste prompt for Gemini, ChatGPT or any image app; then drop the picture back here">Copy prompt</button>
          <button type="button" class="btn btn-ghost btn-xs sc-revert" hidden title="Remove your picture and use the AI image">Use AI image</button>
        </div>
        <input type="file" class="sc-file" accept="image/*,.heic,.heif" hidden />
      </div>
      <div class="scene-body">
        <div class="scene-head">
          <strong>Scene ${i + 1}</strong>
          <span class="muted small sc-est"></span>
          <span class="spacer"></span>
          <button type="button" class="btn btn-ghost btn-xs danger sc-del" aria-label="Remove scene">✕</button>
        </div>
        <textarea class="sc-narr" rows="3" lang="${lang}" placeholder="What the narrator says"></textarea>
        ${sc.layout ? `<div class="sc-slide-head">
          <label class="small muted">Slide</label>
          <select class="sc-layout"><option value="slide">Slide</option><option value="cover">Title + AI picture</option></select>
          <span class="spacer"></span>
          <button type="button" class="btn btn-ghost btn-xs sc-play" title="Save and play this slide's animation">▶ Play</button>
          <button type="button" class="btn btn-ghost btn-xs sc-preview" title="Save and redraw this slide">Update preview</button>
        </div>
        <textarea class="sc-slide" rows="6" spellcheck="false" placeholder="# Heading&#10;- a short point&#10;+++&#10;\`\`\`python&#10;print('hello')&#10;\`\`\`&#10;+++&#10;\`\`\`output&#10;hello&#10;\`\`\`"></textarea>
        <div class="sc-parts-row">
          <button type="button" class="btn btn-ghost btn-xs sc-part" title="Everything after this line appears later, when the narration reaches the next “|”">+ Part</button>
          <button type="button" class="btn btn-ghost btn-xs sc-dia" title="Insert an animated diagram: boxes, and arrows with a moving dot">+ Diagram</button>
          <button type="button" class="btn btn-ghost btn-xs sc-focus" title="Spotlight a code line: it lights up, the rest dims and the view zooms in, when the narration reaches the next “|”">+ Focus</button>
          <button type="button" class="btn btn-ghost btn-xs sc-quiz" title="Insert a quiz: options appear, a 5-second countdown runs, then the answer lights up green">+ Quiz</button>
          <button type="button" class="btn btn-ghost btn-xs sc-logo" title="Show a real logo (Java, Python, Excel…) as a big tile on this slide">+ Logo</button>
          <a class="small sc-icons" href="https://icon-sets.iconify.design/tabler/" target="_blank" rel="noopener" title="Browse free icons: copy a name like tabler:server or logos:python into a diagram box line">Icons ↗</a>
          <button type="button" class="btn btn-ghost btn-xs sc-mark" title="Insert “|” in the narration at the cursor: the next part of the slide appears here">+ | in narration</button>
          <span class="sc-parts small"></span>
        </div>
        <div class="sc-logo-pick" hidden>
          <input type="search" class="sc-logo-q" placeholder="Search a logo, e.g. java, python, excel" />
          <div class="sc-logo-res"></div>
        </div>` : ""}
        <label class="small muted sc-img-label">${sc.layout ? "Background picture prompt (English)" : "Image prompt (English)"}</label>
        <textarea class="sc-img" rows="3" placeholder="What the picture shows"></textarea>
        <div class="sc-ovs"></div>
        <button type="button" class="btn btn-ghost btn-xs sc-add-ov" title="Put a logo, sticker or photo from your asset library on this scene">+ Logo / asset</button>
        <span class="sc-msg small"></span>
      </div>`;
    const shape = { "9:16": ["9 / 16", "120px"], "16:9": ["16 / 9", "168px"], "1:1": ["1 / 1", "132px"] }[(p.settings && p.settings.aspect) || "9:16"];
    if (shape) { $(".scene-media", el).style.aspectRatio = shape[0]; $(".scene-media", el).style.width = shape[1]; }
    const narr = $(".sc-narr", el), img = $(".sc-img", el);
    narr.value = sc.narration; img.value = sc.image_prompt;
    if (sc.layout) {
      const slideBox = $(".sc-slide", el), layoutSel = $(".sc-layout", el);
      if (sc.savedSlide === undefined) sc.savedSlide = sc.slide || "";
      slideBox.value = sc.slide || "";
      layoutSel.value = sc.layout;
      const syncLayout = () => {
        const slideOnly = sc.layout === "slide";
        img.hidden = slideOnly; $(".sc-img-label", el).hidden = slideOnly;
        ["sc-new", "sc-up", "sc-copy"].forEach((c) => ($("." + c, el).hidden = slideOnly));
        $(".scene-media", el).title = slideOnly ? "Slide preview: click to open full size" : "Drop an image here to use it behind the title";
      };
      syncLayout();
      const syncParts = () => {
        const st = partsStatus(slideBox.value, narr.value), box = $(".sc-parts", el);
        box.textContent = st.text;
        box.className = "sc-parts small" + (st.warn ? " warn" : " muted");
      };
      syncParts();
      narr.addEventListener("input", syncParts);
      slideBox.oninput = () => { sc.slide = slideBox.value; sc.slideEdited = sc.slide !== sc.savedSlide; paintSceneMedia(el, sc); syncParts(); };
      layoutSel.onchange = () => { sc.layout = layoutSel.value; sc.slideEdited = true; syncLayout(); paintSceneMedia(el, sc); };
      $(".sc-preview", el).onclick = async () => {
        setSceneBusy(el, true, "Drawing…");
        try { await saveProject(); renderScenes(); } catch (e) { setSceneBusy(el, false); msg(e.message, true); }
      };
      $(".sc-play", el).onclick = async () => {
        try {
          await saveProject(); renderScenes();
          const fresh = sc.id ? vs.project.scenes.find((x) => x.id === sc.id) : vs.project.scenes[i];
          if (fresh && fresh.id) openSlidePlayer(vs.project, fresh.id);
        } catch (e) { msg(e.message, true); }
      };
      $(".sc-part", el).onclick = () => insertLine(slideBox, "+++");
      $(".sc-dia", el).onclick = () => insertLine(slideBox, DIAGRAM_TEMPLATE);
      $(".sc-quiz", el).onclick = () => insertLine(slideBox, /^\s*#{1,3}\s/m.test(slideBox.value) ? QUIZ_TEMPLATE.replace(/^# .*\n/, "") : QUIZ_TEMPLATE);
      $(".sc-focus", el).onclick = () => {  // at the cursor; if the cursor is inside a code block, just after it
        const v = slideBox.value, pos = slideBox.selectionStart ?? v.length;
        const fences = [...v.matchAll(/^[ \t]*```.*$/gm)];
        const before = fences.filter((m) => m.index < pos).length;
        if (before % 2 === 1) {
          const close = fences.find((m) => m.index >= pos);
          if (close) { const end = close.index + close[0].length; slideBox.setSelectionRange(end, end); }
        }
        insertLine(slideBox, "+++ focus 1");
      };
      $(".sc-mark", el).onclick = () => insertText(narr, " | ");
      const pick = $(".sc-logo-pick", el), q = $(".sc-logo-q", el), res = $(".sc-logo-res", el);
      $(".sc-logo", el).onclick = () => { pick.hidden = !pick.hidden; if (!pick.hidden) q.focus(); };
      let searchTimer = 0;
      q.oninput = () => {
        clearTimeout(searchTimer);
        searchTimer = setTimeout(async () => {
          const term = q.value.trim();
          if (term.length < 2) { res.innerHTML = ""; return; }
          res.innerHTML = '<span class="small muted">Searching…</span>';
          try {
            const names = await api(`/api/icons/search?q=${encodeURIComponent(term)}`);
            if (q.value.trim() !== term) return;
            res.innerHTML = names.length ? "" : '<span class="small muted">No logos found. Try the English name.</span>';
            names.forEach((nm) => {
              const [pre, n] = nm.split(":"), b = document.createElement("button");
              b.type = "button"; b.className = "logo-opt"; b.title = nm;
              b.innerHTML = `<img alt="${nm}" loading="lazy" src="/api/icons/${pre}/${n}.svg">`;
              b.onclick = () => { addSlideLogo(slideBox, nm); pick.hidden = true; q.value = ""; res.innerHTML = ""; };
              res.appendChild(b);
            });
          } catch (e) { res.innerHTML = ""; msg(e.message, true); }
        }, 350);
      };
    }
    const est = () => { $(".sc-est", el).textContent = `~${Math.max(1, Math.round(narr.value.length / (CPS[lang] || 16)))} s`; updateEstimate(); };
    narr.oninput = () => { sc.narration = narr.value; est(); };
    img.oninput = () => { sc.image_prompt = img.value; if (sc.ai_image) { sc.stale = true; paintSceneMedia(el, sc); } };
    est();
    paintSceneMedia(el, sc);
    renderOverlayRows(el, sc);
    $(".sc-add-ov", el).onclick = () => {
      if (!vs.assets.length) { msg("Add a logo or image under “Logo & assets” (right side) first.", true); return; }
      sc.overlays = [...(sc.overlays || []), { asset: vs.assets[vs.assets.length - 1].id, pos: "c", size: "m" }].slice(0, 4);
      renderOverlayRows(el, sc); paintSceneMedia(el, sc);
    };

    const msg = (t, err) => { const m = $(".sc-msg", el); m.textContent = t; m.className = "sc-msg small" + (err ? " err" : ""); };
    const sceneId = async () => {  // new scenes get an id when the script is saved
      if (!sc.id) { await saveProject(); renderScenes(); return null; }
      return sc.id;
    };
    $(".sc-new", el).onclick = async () => {
      msg("");
      setSceneBusy(el, true, "Generating…");
      try {
        await saveProject();  // so the latest prompt is used
        const fresh = vs.project.scenes.find((x) => x.id === sc.id);
        const r = await api(`/api/videos/${vs.project.id}/scenes/${fresh.id}/new-image`, { method: "POST" });
        Object.assign(fresh, r, { stale: false });
        renderScenes();
      } catch (e) { setSceneBusy(el, false); msg(e.message, true); }
    };
    const upload = async (file) => {
      if (!file) return;
      msg("");
      setSceneBusy(el, true, "Uploading…");
      try {
        let sid = await sceneId();
        if (!sid) sid = vs.project.scenes[i].id;
        const fd = new FormData(); fd.append("image", file, file.name || "image");
        const r = await api(`/api/videos/${vs.project.id}/scenes/${sid}/image`, { method: "POST", body: fd });
        Object.assign(vs.project.scenes.find((x) => x.id === sid), r);
        renderScenes();
      } catch (e) { setSceneBusy(el, false); msg(e.message, true); }
    };
    $(".sc-copy", el).onclick = async (e) => {
      const b = e.currentTarget;
      try {
        await navigator.clipboard.writeText(externalPrompt(sc));
        b.textContent = "Copied ✓";
        setTimeout(() => (b.textContent = "Copy prompt"), 1500);
      } catch { msg("Couldn't copy. Select the image prompt text and copy it manually.", true); }
    };
    const fileIn = $(".sc-file", el);
    $(".sc-up", el).onclick = () => fileIn.click();
    fileIn.onchange = () => { upload(fileIn.files[0]); fileIn.value = ""; };
    const media = $(".scene-media", el);
    ["dragenter", "dragover"].forEach((ev) => media.addEventListener(ev, (e) => { e.preventDefault(); media.classList.add("over"); }));
    ["dragleave", "drop"].forEach((ev) => media.addEventListener(ev, (e) => { e.preventDefault(); media.classList.remove("over"); }));
    const pictureAllowed = () => sc.layout !== "slide";  // slides are drawn from text, not pictures
    media.addEventListener("drop", (e) => { if (pictureAllowed()) upload(e.dataTransfer.files[0]); });
    media.onclick = () => {
      if (media.classList.contains("busy")) return;
      if (!pictureAllowed()) { const src = $("img", media).src; if (src) window.open(src, "_blank"); return; }  // full-size slide
      fileIn.click();
    };
    $(".sc-revert", el).onclick = async () => {
      setSceneBusy(el, true);
      try {
        const r = await api(`/api/videos/${vs.project.id}/scenes/${sc.id}/image`, { method: "DELETE" });
        Object.assign(sc, r); delete sc.custom_image; if (r.custom_image) sc.custom_image = r.custom_image;
        renderScenes();
      } catch (e) { setSceneBusy(el, false); msg(e.message, true); }
    };
    $(".sc-del", el).onclick = () => {
      if (p.scenes.length < 2) return;
      p.scenes.splice(i, 1);
      renderScenes();
    };
    box.appendChild(el);
  });
  updateEstimate();
  updateRenderButton();  // also refreshes the images button
}

function updateImagesButton() {
  const p = vs.project, btn = $("#v-gen-images");
  if (!p) return;
  const pictured = p.scenes.filter((sc) => sc.layout !== "slide");
  const missing = pictured.filter((sc) => !sc.custom_image && (!sc.ai_image || sc.stale)).length;
  btn.textContent = missing ? `Generate images (${missing})` : "Images ready ✓";
  btn.disabled = !!vs.imagesJob || !!vs.job;
  const all = $("#v-regen-all");
  all.disabled = !!vs.imagesJob || !!vs.job || pictured.every((sc) => sc.custom_image);
  const own = p.scenes.filter((sc) => sc.custom_image).length;
  all.title = own ? `New AI picture for every scene except the ${own} you uploaded` : "New AI picture for every scene";
  btn.classList.toggle("btn-primary", !!missing);
  btn.classList.toggle("btn-ghost", !missing);
}

$("#v-gen-images").onclick = () => startImages(false);
$("#v-regen-all").onclick = () => startImages(true);

async function startImages(regenerate) {
  const msg = $("#v-images-msg");
  msg.textContent = ""; msg.className = "small";
  try {
    const problem = imageSourceProblem();
    if (problem) throw new Error(problem);
    await saveProject();
    renderScenes();
    const { job_id } = await api(`/api/videos/${vs.project.id}/images${regenerate ? "?regenerate=true" : ""}`, { method: "POST" });
    vs.imagesJob = job_id;
    updateImagesButton(); updateRenderButton();
    document.querySelectorAll("#v-scenes .scene").forEach((el) => {
      const sc = vs.project.scenes.find((x) => x.id === el.dataset.sid);
      if (sc && !sc.custom_image && (regenerate || !sc.ai_image || sc.stale)) setSceneBusy(el, true, "Generating…");
    });
    pollImagesJob(job_id);
  } catch (e) { msg.textContent = e.message; msg.className = "small err"; }
}

async function pollImagesJob(id) {
  let j;
  try { j = await api(`/api/jobs/${id}`); } catch (e) { j = { status: "error", error: e.message }; }
  // show each picture as soon as it arrives
  try {
    const fresh = await api(`/api/videos/${vs.project.id}`);
    fresh.scenes.forEach((f) => {
      const sc = vs.project.scenes.find((x) => x.id === f.id);
      if (!sc) return;
      const changed = f.image_v && f.image_v !== sc.image_v;
      ["ai_image", "image_key", "image_v", "custom_image"].forEach((k) => (sc[k] = f[k]));
      const el = document.querySelector(`#v-scenes .scene[data-sid="${f.id}"]`);
      if (el && changed) { sc.stale = false; setSceneBusy(el, false); paintSceneMedia(el, sc); }
    });
  } catch {}
  $("#v-images-msg").textContent = j.message || "";
  if (j.status === "queued" || j.status === "running") { setTimeout(() => pollImagesJob(id), 1200); return; }
  vs.imagesJob = null;
  document.querySelectorAll("#v-scenes .scene").forEach((el) => setSceneBusy(el, false));
  $("#v-images-msg").textContent = j.status === "error" ? j.error : "Review the pictures. Replace any you don't like, then make the video.";
  $("#v-images-msg").className = "small" + (j.status === "error" ? " err" : "");
  renderScenes();
  updateRenderButton();
}

function updateEstimate() {
  const p = vs.project; if (!p) return;
  const chars = p.scenes.reduce((s, sc) => s + (sc.narration || "").length, 0);
  const secs = chars / (CPS[p.language] || 16) + p.scenes.length * 0.8;
  $("#v-est").textContent = `${p.scenes.length} scenes · about ${Math.round(secs)} s`;
}

$("#v-add-scene").onclick = async () => {
  if (!vs.project) return;
  const tutorial = vs.project.scenes.some((sc) => sc.layout);
  vs.project.scenes.push(tutorial
    ? { narration: "", image_prompt: "", layout: "slide", slide: "# ", seed: Math.floor(Math.random() * 2 ** 31) }
    : { narration: "", image_prompt: "", seed: Math.floor(Math.random() * 2 ** 31) });
  renderScenes();
  const all = document.querySelectorAll("#v-scenes .sc-narr");
  all[all.length - 1].focus();
};

async function saveProject() {
  const p = vs.project;
  const keep = p.scenes.filter((sc) => (sc.narration || "").trim() || sc.id);
  const scenes = keep.map((sc) => ({
    id: sc.id || null, narration: (sc.narration || "").trim() || "…",
    image_prompt: (sc.image_prompt || "").trim(), seed: sc.seed || null,
    layout: sc.layout || null, slide: sc.layout ? (sc.slide || "") : "",
    overlays: (sc.overlays || []).filter((o) => o.asset),
  }));
  if (!scenes.length) throw new Error("Write some narration first.");
  const stale = new Set(p.scenes.filter((sc) => sc.stale).map((sc) => sc.id));
  const saved = await api(`/api/videos/${p.id}`, jsonOpts("PUT", { title: $("#v-title").value, scenes, settings: collectSettings() }));
  saved.scenes.forEach((sc) => { if (stale.has(sc.id)) sc.stale = true; });
  vs.project = saved;
  return saved;
}
$("#v-save").onclick = async () => {
  try { await saveProject(); $("#v-write-msg").textContent = "Draft saved."; renderScenes(); vLoadGallery(); }
  catch (e) { $("#v-write-msg").textContent = e.message; }
};

// ------------------------------------------------------------------ render
$("#v-render").onclick = async () => {
  const err = $("#v-error");
  err.hidden = true;
  try {
    rememberPrefs();
    await saveProject();
    renderScenes();
    const { job_id } = await api(`/api/videos/${vs.project.id}/render`, { method: "POST" });
    vs.job = job_id;
    updateRenderButton();
    $("#v-progress").hidden = false;
    pollVideoJob(job_id);
  } catch (e) { err.textContent = e.message; err.hidden = false; }
};
$("#v-cancel").onclick = () => vs.job && api(`/api/jobs/${vs.job}/cancel`, { method: "POST" });

async function pollVideoJob(id) {
  let j;
  try { j = await api(`/api/jobs/${id}`); } catch (e) { j = { status: "error", error: e.message }; }
  if (j.status === "queued" || j.status === "running") {
    $("#v-progress-fill").style.width = `${Math.round(((j.done || 0) / (j.total || 1)) * 100)}%`;
    $("#v-progress-text").textContent = `${j.message || "Working…"}${j.elapsed ? ` · ${Math.round(j.elapsed)}s` : ""}`;
    setTimeout(() => pollVideoJob(id), 1000);
    return;
  }
  vs.job = null;
  $("#v-progress").hidden = true;
  $("#v-progress-fill").style.width = "0";
  if (vs.project) {
    try { showProject(await api(`/api/videos/${vs.project.id}`)); } catch {}
  }
  if (j.status === "error") { $("#v-error").textContent = j.error || "Video failed"; $("#v-error").hidden = false; }
  updateRenderButton();
  vLoadGallery();
}

function showVideo(p) {
  const r = p.render;
  if (!r || !r.video) { $("#v-result").hidden = true; return; }
  const v = $("#v-player");
  const src = `/api/videos/${p.id}/files/video.mp4?v=${r.rendered_at}`;
  if (!v.src.endsWith(src)) {
    v.poster = `/api/videos/${p.id}/files/thumb.jpg?v=${r.rendered_at}`;
    v.src = src;
  }
  v.classList.toggle("wide", (p.settings.aspect || "9:16") !== "9:16");
  $("#v-download").href = `/api/videos/${p.id}/files/video.mp4?download=1`;
  $("#v-result-info").textContent = `${fmtDur(r.duration)} · ${p.scenes.length} scenes · made in ${Math.round(r.seconds_taken || 0)} s`;
  $("#v-result").hidden = false;
}

// ------------------------------------------------------------------ gallery
async function vLoadGallery() {
  const items = await api("/api/videos");
  vs.madeTopics = new Set(items.map((it) => (it.topic || "").trim()));
  if (vs.mode === "tutorial") renderSuggestions();
  const g = $("#v-gallery");
  g.innerHTML = "";
  if (!items.length) { g.innerHTML = '<div class="empty">Your videos will appear here.</div>'; return; }
  for (const it of items) {
    const el = document.createElement("div");
    el.className = "g-item" + (vs.project && vs.project.id === it.id ? " selected" : "");
    el.tabIndex = 0;
    el.innerHTML = `
      <div class="g-thumb"></div>
      <div class="g-main"><div class="g-title"></div><div class="small muted g-meta"></div></div>
      <button type="button" class="btn btn-ghost btn-xs danger g-del">Delete</button>`;
    $(".g-title", el).textContent = it.title || it.topic;
    const status = it.has_video ? fmtDur(it.duration || 0) : it.status === "error" ? "failed" : it.status === "rendering" ? "rendering…" : "draft";
    $(".g-meta", el).textContent = `${status} · ${it.scenes} scenes · ${it.aspect || "9:16"}`;
    if (it.has_video) {
      const img = document.createElement("img");
      img.src = `/api/videos/${it.id}/files/thumb.jpg?v=${it.version || 0}`;
      img.alt = "";
      $(".g-thumb", el).appendChild(img);
    }
    const open = async () => {
      showProject(await api(`/api/videos/${it.id}`));
      document.querySelectorAll(".g-item").forEach((x) => x.classList.toggle("selected", x === el));
      $("#v-editor").scrollIntoView({ behavior: "smooth", block: "start" });
    };
    el.onclick = open;
    el.onkeydown = (e) => { if (e.key === "Enter") open(); };
    $(".g-del", el).onclick = async (e) => {
      e.stopPropagation();
      if (!confirm(`Delete “${it.title || it.topic}” and its video? This cannot be undone.`)) return;
      await api(`/api/videos/${it.id}`, { method: "DELETE" });
      if (vs.project && vs.project.id === it.id) { vs.project = null; $("#v-editor").hidden = true; $("#v-result").hidden = true; }
      vLoadGallery(); updateRenderButton();
    };
    g.appendChild(el);
  }
}

setMode("write");

// open the tab used last time
try { if (localStorage.getItem("vc.tab") === "video") showTab("video"); } catch {}
