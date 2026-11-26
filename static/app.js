"use strict";

const $ = (s, el = document) => el.querySelector(s);
const MAX_REC = 30;

const SCRIPTS = {
  en: [
    "Yesterday I walked to the little bakery on the corner and ordered a warm croissant with a cup of coffee. The morning was quiet, the sky was clear, and for a moment everything felt wonderfully simple.",
    "Technology changes quickly, but good communication never goes out of style. Whether you're telling a story, explaining an idea, or just saying hello, the way you speak makes all the difference.",
    "I'm recording this short sample so my voice can be cloned. I'm speaking naturally, at my usual pace, the same way I would talk to a friend over dinner on a relaxed Friday evening.",
    "The old lighthouse stood at the edge of the cliff, watching over the harbor for nearly a hundred years. On stormy nights, its steady beam guided the fishing boats safely back home.",
  ],
  km: [
    "សួស្តី! ខ្ញុំកំពុងថតសំឡេងរបស់ខ្ញុំ ដើម្បីបង្កើតសំឡេងចម្លង។ ថ្ងៃនេះអាកាសធាតុល្អណាស់ ខ្ញុំចង់ទៅដើរលេងនៅមាត់ទន្លេ ហើយញ៉ាំកាហ្វេទឹកដោះគោទឹកកកមួយកែវ។",
    "កាលពីម្សិលមិញ ខ្ញុំបានទៅផ្សារជាមួយម្តាយរបស់ខ្ញុំ។ យើងបានទិញផ្លែឈើ បន្លែ និងត្រីស្រស់ៗ សម្រាប់ធ្វើម្ហូបពេលល្ងាច។ ក្រោយមក យើងអង្គុយសម្រាកនៅហាងកាហ្វេតូចមួយ ហើយនិយាយលេងគ្នាយ៉ាងសប្បាយ។",
  ],
  zh: [
    "今天天气很好，我想去公园散散步，顺便买一杯咖啡。生活中的小幸福，往往就藏在这些平凡的时刻里。",
  ],
};
const STYLE_CHIPS = ["warm and friendly", "excited, energetic", "calm, slow pace", "whispering softly", "serious news anchor", "sad, low voice", "cheerful and fast"];
const DESIGN_CHIPS = ["young woman, gentle and warm", "deep-voiced older man, slow and wise", "energetic young male host", "soft-spoken British narrator"];

const state = {
  voices: [],
  selected: localStorage.getItem("vc.voice") || "",
  clips: [],           // clips waiting to be saved: {id, blob, name, duration, transcript, url}
  target: null,        // {id, name} when adding clips to an existing voice
  managing: null,      // voice id whose clip panel is open
  scriptIdx: 0,
  scriptLang: (() => { try { return localStorage.getItem("vc.scriptLang") || "en"; } catch { return "en"; } })(),
  ready: false,
  job: null,
};

// ------------------------------------------------------------------ API
async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let msg = res.statusText;
    try { const j = await res.json(); msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch {}
    throw new Error(msg || `HTTP ${res.status}`);
  }
  return res.json();
}
const jsonOpts = (method, body) => ({ method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

// ------------------------------------------------------------------ status
async function pollStatus() {
  try {
    const s = await api("/api/status");
    const pill = $("#status");
    pill.className = "pill pill-" + (s.status === "ready" ? "ready" : s.status === "error" ? "error" : "loading");
    $("#status-text").textContent =
      s.status === "ready" ? `Ready · ${s.device} · ${s.dtype === "bfloat16" ? "bf16" : s.dtype === "float32" ? "fp32" : s.dtype}` :
      s.status === "error" ? "Model error — see terminal" :
      "Loading model… (first run downloads ~5 GB)";
    pill.title = s.error || s.model;
    state.ready = s.status === "ready";
    state.modelStatus = s.status;
    updateGenButton();
    if (typeof updateRenderButton === "function") updateRenderButton();
    if (s.status === "loading") setTimeout(pollStatus, 2000);
  } catch {
    $("#status-text").textContent = "Server offline";
    $("#status").className = "pill pill-error";
    setTimeout(pollStatus, 3000);
  }
}

// ------------------------------------------------------------------ helpers
const fmtTime = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
const fmtDur = (s) => (s >= 60 ? fmtTime(s) : `${s.toFixed(1)}s`);
function escapeHtml(s) { return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
function setMsg(text, kind = "") { const m = $("#voice-msg"); m.textContent = text; m.className = "small " + kind; }

const player = new Audio();
let playingBtn = null;
function togglePlay(btn, url) {
  if (playingBtn === btn && !player.paused) { player.pause(); return; }
  if (playingBtn) playingBtn.classList.remove("playing");
  player.src = url; player.play(); playingBtn = btn; btn.classList.add("playing");
}
player.addEventListener("pause", () => playingBtn && playingBtn.classList.remove("playing"));
player.addEventListener("ended", () => playingBtn && playingBtn.classList.remove("playing"));

// ------------------------------------------------------------------ new voice form
function showScript() {
  const list = SCRIPTS[state.scriptLang] || SCRIPTS.en;
  const p = $("#script-text");
  p.textContent = list[state.scriptIdx % list.length];
  p.lang = state.scriptLang;
  $("#script-lang").value = state.scriptLang;
}
$("#script-next").onclick = () => { state.scriptIdx++; showScript(); };
$("#script-lang").onchange = (e) => {
  state.scriptLang = e.target.value; state.scriptIdx = 0;
  try { localStorage.setItem("vc.scriptLang", state.scriptLang); } catch {}
  $("#asr-lang").value = state.scriptLang === "km" ? "km" : "auto";
  showScript();
};

$("#new-voice-toggle").onclick = (e) => {
  const form = $("#voice-form");
  form.hidden = !form.hidden;
  e.target.textContent = form.hidden ? "New voice" : "Hide";
  e.target.setAttribute("aria-expanded", String(!form.hidden));
};

document.querySelectorAll(".seg").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll(".seg").forEach((x) => { x.classList.toggle("active", x === b); x.setAttribute("aria-selected", x === b); });
  $("#src-record").hidden = b.dataset.src !== "record";
  $("#src-upload").hidden = b.dataset.src !== "upload";
}));

// --- clip tray (a voice can be built from several recordings / files)
const MAX_TOTAL = 120;
let clipSeq = 0;

function addClip(blob, name, duration, transcript = "") {
  const clip = { id: ++clipSeq, blob, name, duration, transcript, url: URL.createObjectURL(blob) };
  state.clips.push(clip);
  if (!duration) {
    const a = new Audio();
    a.preload = "metadata";
    a.onloadedmetadata = () => { if (isFinite(a.duration)) { clip.duration = a.duration; renderClips(); } };
    a.src = clip.url;
  }
  renderClips();
}

function removeClip(id) {
  const c = state.clips.find((x) => x.id === id);
  if (c) URL.revokeObjectURL(c.url);
  state.clips = state.clips.filter((x) => x.id !== id);
  renderClips();
}

function renderClips() {
  const tray = $("#clip-tray");
  tray.innerHTML = "";
  state.clips.forEach((c, i) => {
    const el = document.createElement("div");
    el.className = "clip";
    el.innerHTML = `
      <div class="clip-top">
        <button type="button" class="play" aria-label="Play clip"></button>
        <span class="clip-name"></span>
        <button type="button" class="btn btn-ghost btn-xs danger">Remove</button>
      </div>
      <textarea rows="2" placeholder="Transcript of this clip (optional): the exact words spoken"></textarea>`;
    $(".clip-name", el).textContent = `${i + 1}. ${c.name}${c.duration ? " · " + fmtDur(c.duration) : ""}`;
    $(".play", el).onclick = (e) => togglePlay(e.currentTarget, c.url);
    $(".danger", el).onclick = () => removeClip(c.id);
    const ta = $("textarea", el);
    ta.value = c.transcript;
    ta.oninput = () => { c.transcript = ta.value; };
    tray.appendChild(el);
  });
  const total = state.clips.reduce((s, c) => s + (c.duration || 0), 0);
  const tot = $("#clips-total");
  tot.textContent = state.clips.length
    ? `${state.clips.length} clip${state.clips.length > 1 ? "s" : ""} · ${fmtDur(total)} total`
    : "none yet";
  tot.className = total > MAX_TOTAL ? "err" : total >= 15 ? "ok" : "muted";
  $("#asr-btn").disabled = !state.clips.length;
  updateSaveButton();
}

function updateSaveButton() {
  const named = state.target || $("#voice-name").value.trim();
  $("#save-voice").disabled = !(state.clips.length && named);
}
$("#voice-name").addEventListener("input", updateSaveButton);

function setTarget(voice) {
  state.target = voice ? { id: voice.id, name: voice.name } : null;
  $("#form-target").hidden = !voice;
  $("#name-field").hidden = !!voice;
  if (voice) $("#target-name").textContent = voice.name;
  $("#save-voice").textContent = voice ? "Add to voice" : "Save voice";
  updateSaveButton();
}
$("#target-cancel").onclick = () => { setTarget(null); };

// --- recording
let rec = null, recStream = null, recTimer = null, recStart = 0, meterRAF = null, audioCtx = null;
$("#rec-btn").onclick = () => (rec && rec.state === "recording" ? stopRec() : startRec());

async function startRec() {
  try {
    recStream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false, channelCount: 1 },
    });
  } catch (e) {
    setMsg("Microphone blocked — allow mic access for this page in your browser.", "err");
    return;
  }
  const mime = ["audio/webm;codecs=opus", "audio/mp4", "audio/webm"].find((m) => MediaRecorder.isTypeSupported(m)) || "";
  rec = new MediaRecorder(recStream, mime ? { mimeType: mime } : undefined);
  const chunks = [];
  const script = $("#script-text").textContent;
  rec.ondataavailable = (e) => e.data.size && chunks.push(e.data);
  rec.onstop = () => {
    const type = rec.mimeType || "audio/webm";
    const ext = type.includes("mp4") ? ".m4a" : ".webm";
    const secs = (performance.now() - recStart) / 1000;
    recStream.getTracks().forEach((t) => t.stop());
    if (secs < 2) { setMsg("That was too short — hold the button for at least a few seconds.", "err"); return; }
    setMsg("");
    const takes = state.clips.filter((c) => c.name.startsWith("Take")).length + 1;
    const blob = new Blob(chunks, { type });
    blob.name = `take-${takes}${ext}`;
    addClip(blob, `Take ${takes}`, secs, script);
    state.scriptIdx++; showScript();
    $("#rec-hint").textContent = `Take ${takes} added. Read the next script to add another, or save.`;
  };
  rec.start(250);
  recStart = performance.now();
  $("#rec-btn").classList.add("recording");
  $("#rec-btn").setAttribute("aria-label", "Stop recording");
  $("#rec-hint").textContent = "Recording… click again to stop.";
  recTimer = setInterval(() => {
    const s = (performance.now() - recStart) / 1000;
    $("#rec-time").textContent = fmtTime(s);
    if (s >= MAX_REC) stopRec();
  }, 200);
  startMeter(recStream);
}
function stopRec() {
  if (!rec || rec.state !== "recording") return;
  rec.stop();
  clearInterval(recTimer);
  cancelAnimationFrame(meterRAF);
  if (audioCtx) { audioCtx.close(); audioCtx = null; }
  $("#rec-btn").classList.remove("recording");
  $("#rec-btn").setAttribute("aria-label", "Start recording");
  $("#rec-time").textContent = "0:00";
}
function startMeter(stream) {
  audioCtx = new AudioContext();
  const src = audioCtx.createMediaStreamSource(stream);
  const an = audioCtx.createAnalyser();
  an.fftSize = 1024; src.connect(an);
  const buf = new Float32Array(an.fftSize);
  const cv = $("#rec-meter"), ctx = cv.getContext("2d");
  const accent = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim();
  const levels = [];
  const draw = () => {
    an.getFloatTimeDomainData(buf);
    let peak = 0; for (const v of buf) peak = Math.max(peak, Math.abs(v));
    levels.push(peak); if (levels.length > 80) levels.shift();
    ctx.clearRect(0, 0, cv.width, cv.height);
    ctx.fillStyle = accent;
    const w = cv.width / 80;
    levels.forEach((l, i) => {
      const h = Math.max(2, Math.min(1, l * 1.6) * cv.height);
      ctx.fillRect(i * w + 1, (cv.height - h) / 2, w - 2, h);
    });
    meterRAF = requestAnimationFrame(draw);
  };
  draw();
}

// --- upload (several files at once)
const drop = $("#drop");
$("#file-input").onchange = (e) => { [...e.target.files].forEach(handleFile); e.target.value = ""; };
["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", (e) => [...e.dataTransfer.files].forEach(handleFile));
function handleFile(f) {
  if (!state.target && !$("#voice-name").value.trim()) $("#voice-name").value = f.name.replace(/\.[^.]+$/, "").slice(0, 60);
  addClip(f, f.name, 0, "");
}

// --- transcribe every clip that has no transcript yet
$("#asr-btn").onclick = async () => {
  const todo = state.clips.filter((c) => !c.transcript.trim());
  if (!todo.length) { setMsg("Every clip already has a transcript.", "ok"); return; }
  const btn = $("#asr-btn");
  btn.disabled = true;
  const km = $("#asr-lang").value === "km";
  let done = 0;
  try {
    for (const c of todo) {
      btn.textContent = `Transcribing ${++done}/${todo.length}…`;
      setMsg(km ? "Transcribing Khmer… the first use downloads a ~1 GB model." : "Transcribing… the first use downloads a ~900 MB model.", "");
      const fd = new FormData();
      fd.append("audio", c.blob, c.blob.name || c.name);
      fd.append("lang", $("#asr-lang").value);
      const r = await api("/api/transcribe", { method: "POST", body: fd });
      c.transcript = r.text || "";
      renderClips();
    }
    setMsg("Transcripts filled. Fix any mistakes so each one matches its clip exactly.", "ok");
  } catch (e) { setMsg(e.message, "err"); }
  btn.textContent = "Transcribe all"; btn.disabled = !state.clips.length;
};

// --- save (new voice, or add clips to an existing one)
$("#voice-form").onsubmit = async (e) => {
  e.preventDefault();
  if (!state.clips.length) return;
  const btn = $("#save-voice");
  btn.disabled = true; btn.textContent = "Saving…";
  setMsg($("#voice-denoise").checked ? "Cleaning and combining clips…" : "Combining clips…", "");
  try {
    const fd = new FormData();
    fd.append("denoise", $("#voice-denoise").checked);
    fd.append("transcripts", JSON.stringify(state.clips.map((c) => c.transcript.trim())));
    state.clips.forEach((c, i) => fd.append("audio", c.blob, c.blob.name || `clip-${i + 1}.wav`));
    let v;
    if (state.target) {
      v = await api(`/api/voices/${state.target.id}/clips`, { method: "POST", body: fd });
    } else {
      fd.append("name", $("#voice-name").value.trim());
      v = await api("/api/voices", { method: "POST", body: fd });
    }
    const warn = (v.warnings || []).join(" ");
    setMsg(`Saved “${v.name}” · ${v.clips.length} clips · ${fmtDur(v.duration)}.${warn ? " " + warn : ""}`, warn ? "err" : "ok");
    resetForm();
    state.selected = v.id; try { localStorage.setItem("vc.voice", v.id); } catch {}
    await loadVoices();
  } catch (err) { setMsg(err.message, "err"); }
  btn.textContent = state.target ? "Add to voice" : "Save voice"; updateSaveButton();
};
function resetForm() {
  state.clips.forEach((c) => URL.revokeObjectURL(c.url));
  state.clips = [];
  $("#voice-name").value = ""; $("#voice-denoise").checked = false;
  $("#rec-time").textContent = "0:00";
  $("#rec-hint").textContent = "Record several short takes — each one becomes a clip.";
  setTarget(null);
  renderClips();
}

// ------------------------------------------------------------------ voice list
async function loadVoices() {
  state.voices = await api("/api/voices");
  if (state.selected && state.selected !== "__design" && !state.voices.some((v) => v.id === state.selected)) state.selected = "";
  if (!state.selected && state.voices.length) state.selected = state.voices[0].id;
  renderVoices(); renderSelect(); updateSpeakControls();
}

function transcriptBadge(v) {
  const n = (v.clips || []).length || 1, t = v.transcribed_clips ?? (v.transcript ? n : 0);
  if (t && t >= n) return '<span class="badge good">transcript</span>';
  if (t) return `<span class="badge good">transcript ${t}/${n}</span>`;
  return '<span class="badge">no transcript</span>';
}

function renderVoices() {
  const list = $("#voice-list");
  list.innerHTML = "";
  if (!state.voices.length) {
    list.innerHTML = '<div class="empty">No voices yet. Record or upload a few short clips above to clone your first voice.</div>';
    return;
  }
  const tpl = $("#voice-item-tpl");
  for (const v of state.voices) {
    const el = tpl.content.firstElementChild.cloneNode(true);
    el.dataset.id = v.id;
    el.classList.toggle("selected", v.id === state.selected);
    $(".vi-name", el).textContent = v.name;
    const n = (v.clips || []).length || 1;
    $(".vi-meta", el).innerHTML = `${n} clip${n > 1 ? "s" : ""} · ${fmtDur(v.duration)}` + transcriptBadge(v) +
      (v.denoised ? '<span class="badge">denoised</span>' : "");
    $(".play", el).onclick = (e) => { e.stopPropagation(); togglePlay(e.currentTarget, `/api/voices/${v.id}/audio`); };
    $(".del", el).onclick = async (e) => {
      e.stopPropagation();
      if (!confirm(`Delete voice “${v.name}” and all its clips? This cannot be undone.`)) return;
      await api(`/api/voices/${v.id}`, { method: "DELETE" });
      if (state.managing === v.id) state.managing = null;
      loadVoices();
    };
    const edit = $(".edit", el);
    edit.textContent = state.managing === v.id ? "Close" : "Manage";
    edit.onclick = (e) => {
      e.stopPropagation();
      state.managing = state.managing === v.id ? null : v.id;
      renderVoices();
    };
    const pick = () => selectVoice(v.id);
    el.onclick = pick;
    el.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(); } };
    list.appendChild(el);
    if (state.managing === v.id) list.appendChild(renderManage(v));
  }
}

function renderManage(v) {
  const box = document.createElement("div");
  box.className = "manage card";
  box.innerHTML = `
    <label class="field"><span>Name</span><input type="text" maxlength="60" class="m-name" /></label>
    <div class="m-clips"></div>
    <div class="form-foot">
      <button type="button" class="btn btn-ghost btn-sm m-add">+ Add clips</button>
      <span class="m-msg small"></span>
      <button type="button" class="btn btn-primary btn-sm m-save">Save changes</button>
    </div>`;
  $(".m-name", box).value = v.name;
  const clipsBox = $(".m-clips", box);
  const edits = {};
  (v.clips || []).forEach((c, i) => {
    const row = document.createElement("div");
    row.className = "clip";
    row.innerHTML = `
      <div class="clip-top">
        <button type="button" class="play" aria-label="Play clip"></button>
        <span class="clip-name"></span>
        <button type="button" class="btn btn-ghost btn-xs danger">Remove</button>
      </div>
      <textarea rows="2" placeholder="Transcript of this clip (optional)"></textarea>`;
    $(".clip-name", row).textContent = `${i + 1}. ${c.source || "clip"} · ${fmtDur(c.duration)}`;
    $(".play", row).onclick = (e) => togglePlay(e.currentTarget, `/api/voices/${v.id}/clips/${c.file}`);
    const ta = $("textarea", row);
    ta.value = c.transcript || "";
    ta.oninput = () => { edits[c.file] = ta.value; };
    const rm = $(".danger", row);
    rm.disabled = v.clips.length < 2;
    rm.title = rm.disabled ? "A voice needs at least one clip" : "";
    rm.onclick = async () => {
      if (!confirm(`Remove clip ${i + 1} from “${v.name}”?`)) return;
      try { await api(`/api/voices/${v.id}/clips/${c.file}`, { method: "DELETE" }); await loadVoices(); }
      catch (e) { $(".m-msg", box).textContent = e.message; }
    };
    clipsBox.appendChild(row);
  });
  $(".m-add", box).onclick = () => {
    setTarget(v);
    const form = $("#voice-form");
    if (form.hidden) $("#new-voice-toggle").click();
    form.scrollIntoView({ behavior: "smooth", block: "start" });
  };
  $(".m-save", box).onclick = async () => {
    const msg = $(".m-msg", box);
    try {
      await api(`/api/voices/${v.id}`, jsonOpts("PATCH", { name: $(".m-name", box).value, clip_transcripts: edits }));
      await loadVoices();
    } catch (e) { msg.textContent = e.message; msg.className = "m-msg small err"; }
  };
  return box;
}

function renderSelect() {
  const sel = $("#voice-select");
  sel.innerHTML = "";
  for (const v of state.voices) sel.add(new Option(v.name, v.id));
  sel.add(new Option("✦ Design a new voice from a description", "__design"));
  sel.value = state.selected || "__design";
  if (!state.selected) state.selected = "__design";
}

function selectVoice(id) {
  state.selected = id; localStorage.setItem("vc.voice", id);
  $("#voice-select").value = id;
  document.querySelectorAll(".voice-item").forEach((el) => el.classList.toggle("selected", el.dataset.id === id));
  updateSpeakControls();
}
$("#voice-select").onchange = (e) => selectVoice(e.target.value);

// ------------------------------------------------------------------ speak
function currentVoice() { return state.voices.find((v) => v.id === state.selected) || null; }

function updateSpeakControls() {
  const design = state.selected === "__design";
  const v = currentVoice();
  $("#style-label").innerHTML = design
    ? 'Voice description <em class="muted">— required: who is speaking and how</em>'
    : 'Style <em class="muted">— optional</em>';
  $("#tts-style").placeholder = design ? "e.g. a young woman with a gentle, warm voice" : "e.g. warm and cheerful, slightly faster";
  const chips = $("#style-chips");
  chips.innerHTML = "";
  for (const c of design ? DESIGN_CHIPS : STYLE_CHIPS) {
    const b = document.createElement("button");
    b.type = "button"; b.className = "chip"; b.textContent = c;
    b.onclick = () => { $("#tts-style").value = c; updateSimNote(); };
    chips.appendChild(b);
  }
  const hasT = !!(v && v.transcript);
  $("#sim-wrap").hidden = design;
  $("#use-transcript").disabled = !hasT;
  $("#sim-wrap").classList.toggle("disabled", !hasT);
  updateSimNote();
  updateGenButton();
}
function updateSimNote() {
  const v = currentVoice();
  const note = $("#sim-note");
  if (!v) return;
  if (!v.transcript) note.textContent = "(add a transcript to this voice to enable)";
  else if ($("#tts-style").value.trim() && $("#use-transcript").checked)
    note.textContent = "(on: style is followed less — turn off for stronger style control)";
  else if (v.clips && v.transcribed_clips < v.clips.length)
    note.textContent = `(uses the ${v.transcribed_clips} clip${v.transcribed_clips > 1 ? "s" : ""} that have transcripts)`;
  else note.textContent = "(uses the voice's transcript for the closest match)";
}
$("#tts-style").addEventListener("input", updateSimNote);
$("#use-transcript").addEventListener("change", updateSimNote);

function updateGenButton() {
  const text = $("#tts-text").value.trim();
  $("#gen-btn").disabled = !state.ready || !text || !!state.job;
  $("#gen-btn").textContent = !state.ready ? (state.modelStatus === "error" ? "Model unavailable" : "Waiting for model…") : state.job ? "Generating…" : "Generate speech";
}
$("#tts-text").addEventListener("input", () => {
  const n = $("#tts-text").value.length;
  $("#text-count").textContent = `${n.toLocaleString()} chars`;
  updateGenButton();
});
$("#cfg").oninput = (e) => ($("#cfg-out").textContent = (+e.target.value).toFixed(1));
$("#steps").oninput = (e) => ($("#steps-out").textContent = e.target.value);

document.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === "Enter" && !$("#gen-btn").disabled) generate();
});
// Shortcut hint: ⌘ on a Mac, Ctrl elsewhere (both keys work everywhere)
if (/Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent)) {
  document.querySelectorAll(".kbd-hint").forEach((el) => (el.textContent = el.textContent.replace("Ctrl", "⌘")));
}
$("#gen-btn").onclick = generate;
$("#cancel-btn").onclick = () => state.job && api(`/api/jobs/${state.job}/cancel`, { method: "POST" });

async function generate() {
  const design = state.selected === "__design";
  const style = $("#tts-style").value.trim();
  const err = $("#gen-error");
  err.hidden = true;
  if (design && !style) { err.textContent = "Describe the voice you want in the Voice description field."; err.hidden = false; return; }
  const seedVal = $("#seed").value.trim();
  const body = {
    text: $("#tts-text").value.trim(),
    voice_id: design ? null : state.selected,
    style,
    use_transcript: $("#use-transcript").checked,
    cfg_value: +$("#cfg").value,
    inference_timesteps: +$("#steps").value,
    normalize: $("#normalize").checked,
    seed: seedVal === "" ? null : parseInt(seedVal, 10),
  };
  try {
    const { job_id } = await api("/api/tts", jsonOpts("POST", body));
    state.job = job_id; updateGenButton();
    $("#progress").hidden = false; $("#result").hidden = true;
    pollJob(job_id);
  } catch (e) { err.textContent = e.message; err.hidden = false; }
}

async function pollJob(id) {
  let j;
  try { j = await api(`/api/jobs/${id}`); } catch (e) { j = { status: "error", error: e.message }; }
  const bar = $(".bar"), fill = $("#progress-fill");
  if (j.status === "queued" || j.status === "running") {
    const multi = j.total > 1;
    bar.classList.toggle("indeterminate", !multi);
    if (multi) fill.style.width = `${(j.done / j.total) * 100}%`;
    $("#progress-text").textContent = j.status === "queued"
      ? "Queued…"
      : `${multi ? `Part ${Math.min(j.done + 1, j.total)} of ${j.total} · ` : "Synthesizing · "}${j.elapsed || 0}s`;
    setTimeout(() => pollJob(id), 700);
    return;
  }
  state.job = null; updateGenButton();
  $("#progress").hidden = true; bar.classList.remove("indeterminate"); fill.style.width = "0";
  if (j.status === "done") {
    showResult(j.result);
    loadHistory();
  } else if (j.status !== "cancelled") {
    $("#gen-error").textContent = j.error || "Generation failed"; $("#gen-error").hidden = false;
  }
}

function showResult(r) {
  const a = $("#result-audio");
  a.src = `/api/outputs/${r.id}.wav`;
  $("#result-dl").href = `/api/outputs/${r.id}.wav?download=1`;
  $("#result-tg").href = `/api/outputs/${r.id}.ogg`;
  $("#result-info").textContent = `${fmtDur(r.duration)} audio · made in ${r.gen_seconds}s · seed ${r.seed}`;
  $("#result").hidden = false;
  a.play().catch(() => {});
}

// ------------------------------------------------------------------ history
async function loadHistory() {
  const items = await api("/api/history");
  const box = $("#history");
  box.innerHTML = "";
  if (!items.length) { box.innerHTML = '<div class="empty">Generated clips will show up here.</div>'; return; }
  for (const h of items.slice(0, 60)) {
    const el = document.createElement("div");
    el.className = "h-item";
    el.innerHTML = `
      <div class="h-top">
        <button class="play" aria-label="Play"></button>
        <div class="h-text"></div>
      </div>
      <div class="h-meta small muted">
        <span class="h-voice"></span>
        <span>· ${fmtDur(h.duration)} · seed ${h.seed}</span>
        <span class="spacer"></span>
        <span class="h-actions">
        <button class="btn btn-ghost btn-xs reuse">Reuse</button>
        <a class="btn btn-ghost btn-xs" href="/api/outputs/${h.id}.ogg" title="Ogg/Opus voice note for Telegram">Telegram voice</a>
        <a class="btn btn-ghost btn-xs" href="/api/outputs/${h.id}.wav?download=1">WAV</a>
        <button class="btn btn-ghost btn-xs danger del">Delete</button>
        </span>
      </div>`;
    $(".h-text", el).textContent = h.text;
    $(".h-voice", el).textContent = h.voice_name + (h.style ? ` (${h.style})` : "");
    $(".play", el).onclick = (e) => togglePlay(e.currentTarget, `/api/outputs/${h.id}.wav`);
    $(".reuse", el).onclick = () => {
      $("#tts-text").value = h.text; $("#tts-text").dispatchEvent(new Event("input"));
      if (h.voice_id && state.voices.some((v) => v.id === h.voice_id)) selectVoice(h.voice_id);
      else if (!h.voice_id) selectVoice("__design");
      $("#tts-style").value = h.style || "";
      $("#seed").value = h.seed;
      $("#cfg").value = h.cfg_value; $("#cfg-out").textContent = (+h.cfg_value).toFixed(1);
      $("#steps").value = h.inference_timesteps; $("#steps-out").textContent = h.inference_timesteps;
      updateSimNote();
      $("#tts-text").scrollIntoView({ behavior: "smooth", block: "center" });
    };
    $(".del", el).onclick = async () => { await api(`/api/history/${h.id}`, { method: "DELETE" }); loadHistory(); };
    box.appendChild(el);
  }
}

// ------------------------------------------------------------------ init
showScript();
renderClips();
$("#asr-lang").value = state.scriptLang === "km" ? "km" : "auto";
pollStatus();
loadVoices();
loadHistory();
