// Patient voice console: half-duplex tap-to-speak call with Gnani Prisma (STT) and Timbre (TTS).
import { WavRecorder, MicUnavailableError } from "./recorder.js";

const $ = (id) => document.getElementById(id);
const MAX_SECONDS = 25;

const state = { session: null, lang: "hi-en", busy: false, audio: null, card: null };

// Phrases the UI sends on the caller's behalf, in the call's language.
const PHRASES = {
  confirm: { "en-IN": "Yes, please confirm.", "hi-IN": "हाँ, कन्फर्म कर दीजिए।", "hi-en": "Haan, confirm kar dijiye." },
  decline: { "en-IN": "No, don't do it.", "hi-IN": "नहीं, रहने दीजिए।", "hi-en": "Nahi, rehne dijiye." },
  staff: { "en-IN": "I want to talk to the clinic staff.", "hi-IN": "मुझे स्टाफ से बात करनी है।",
           "hi-en": "Mujhe staff se baat karni hai." },
  slot: {
    "en-IN": (s) => `The ${s.time} slot on ${s.weekday}, ${prettyDate(s.date)} please.`,
    "hi-IN": (s) => `${prettyDate(s.date)} को ${s.time} वाला स्लॉट दीजिए।`,
    "hi-en": (s) => `${prettyDate(s.date)} ko ${s.time} wala slot de dijiye.`,
  },
};
const LANG_LABEL = { "hi-IN": "हिंदी", "en-IN": "English", "hi-en": "Hinglish" };

const recorder = new WavRecorder({
  maxSeconds: MAX_SECONDS,
  onLevel: (rms) => { $("level").style.width = `${Math.min(100, rms * 400)}%`; },
  onAutoStop: () => stopAndSend(),
});

// ------------------------------------------------------------------ helpers

function prettyDate(iso) {
  const d = new Date(`${iso}T00:00:00`);
  return d.toLocaleDateString("en-IN", { day: "numeric", month: "long" });
}
function shortDay(iso) {
  return new Date(`${iso}T00:00:00`).toLocaleDateString("en-IN", { weekday: "short", day: "numeric", month: "short" });
}
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null) node.setAttribute(k, v);
  }
  for (const c of children.flat(Infinity)) if (c !== null && c !== undefined && c !== false) node.append(c);
  return node;
}
function hasDevanagari(text) { return /[ऀ-ॿ]/.test(text); }

async function api(path, options = {}) {
  const res = await fetch(path, options);
  if (res.status === 401) {                    // hosted demo: access code missing or expired
    location.href = `/access.html?next=${encodeURIComponent(location.pathname)}`;
    throw new Error("Access code required");
  }
  let body = null;
  try { body = await res.json(); } catch { /* non-JSON */ }
  if (!res.ok) throw new Error(body?.detail || `Request failed (${res.status})`);
  return body;
}
const postJSON = (path, data) => api(path, { method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify(data) });

function setStatus(text, kind = "idle") {
  $("status").textContent = text;
  $("status").dataset.state = kind;
}
function setBusy(busy) {
  state.busy = busy;
  for (const b of document.querySelectorAll("#live .chip, #type-form button, #staff, .slot, .box .btn"))
    b.disabled = busy;
  $("mic").disabled = busy && !recorder.recording;
}

// ------------------------------------------------------------------ audio

function stopAudio() {
  if (state.audio) { state.audio.pause(); state.audio.currentTime = 0; }
}
function play(url) {
  stopAudio();
  return new Promise((resolve) => {
    const audio = new Audio(url);
    state.audio = audio;
    audio.onended = audio.onerror = () => resolve();
    audio.play().catch(() => resolve());
  });
}
function b64ToUrl(b64) {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return URL.createObjectURL(new Blob([bytes], { type: "audio/wav" }));
}

// ------------------------------------------------------------------ transcript & trace

function addBubble(role, text, { lang, note, audioUrl, escalated } = {}) {
  const meta = el("div", { class: "meta" },
    el("span", {}, role === "user" ? "You" : "CareCall AI"),
    lang ? el("span", { class: "tag tag-info" }, LANG_LABEL[lang] || lang) : null,
    note ? el("span", {}, note) : null,
    audioUrl ? el("button", { class: "replay", type: "button", "aria-label": "Replay this reply",
      onclick: () => play(audioUrl) }, "🔊 Replay") : null);
  const body = el("div", hasDevanagari(text) ? { lang: "hi" } : {}, text);
  const item = el("li", { class: `bubble ${role}${escalated ? " escalated" : ""}` }, meta, body);
  $("transcript").append(item);
  item.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

function addTrace(step, model, lang, ms, id) {
  const tbody = $("trace");
  if (tbody.querySelector("td[colspan]")) tbody.innerHTML = "";
  tbody.prepend(el("tr", {}, el("td", {}, step), el("td", {}, model), el("td", {}, lang || "–"),
    el("td", {}, ms ?? "–"), el("td", { class: "id" }, id || "–")));
}

// ------------------------------------------------------------------ request card

function renderCard(card) {
  const box = $("card");
  if (!card) return;
  state.card = card;
  box.innerHTML = "";
  if (card.type === "slots") {
    box.append(el("p", {}, el("strong", {}, card.doctor), ` · fee ₹${card.fee_inr}`));
    if (!card.slots.length) {
      box.append(el("p", { class: "muted" }, "No open slots in the next 7 days."));
      return;
    }
    box.append(el("p", { class: "muted small" }, "Tap a time, or say it:"));
    box.append(el("div", { class: "slots" }, card.slots.map((s) =>
      el("button", { class: "slot", type: "button", onclick: () => sendText(PHRASES.slot[state.lang](s)) },
        el("strong", {}, s.time), `${s.weekday}, ${prettyDate(s.date)}`))));
  } else if (card.type === "pending") {
    const title = { book: "Please confirm this booking", cancel: "Please confirm the cancellation",
                    reschedule: "Please confirm the new time" }[card.action];
    const rows = [["Doctor", card.doctor], ["Day", `${card.weekday}, ${prettyDate(card.date)}`], ["Time", card.time]];
    if (card.action === "reschedule") rows.splice(1, 0, ["Moving from", `${shortDay(card.from_date)} ${card.from_time}`]);
    if (card.action === "cancel") rows.unshift(["Booking", card.ref]);
    if (card.fee_inr !== undefined) rows.push(["Fee", `₹${card.fee_inr}`]);
    if (card.patient) rows.push(["Patient", card.patient]);
    box.append(el("div", { class: "box pending" },
      el("h3", {}, title),
      el("dl", { class: "summary" }, rows.map(([k, v]) => [el("dt", {}, k), el("dd", {}, v)])),
      el("p", { class: "small" }, "Say “Yes / हाँ”, or use the buttons."),
      el("div", { class: "actions" },
        el("button", { class: "btn btn-primary", type: "button",
          onclick: () => sendText(PHRASES.confirm[state.lang], { confirm: true }) }, "✓ Confirm"),
        el("button", { class: "btn btn-secondary", type: "button",
          onclick: () => sendText(PHRASES.decline[state.lang]) }, "No"))));
  } else if (card.type === "confirmed") {
    const heading = { book: "Appointment confirmed", cancel: "Appointment cancelled",
                      reschedule: "Appointment moved" }[card.action];
    const ref = card.new_ref || card.ref;
    box.append(el("div", { class: "box confirmed", role: "status" },
      el("h3", {}, `✓ ${heading}`),
      ref ? el("div", {}, "Booking reference", el("div", { class: "ref" }, ref)) : null,
      el("dl", { class: "summary" },
        el("dt", {}, "Doctor"), el("dd", {}, card.doctor),
        el("dt", {}, "When"), el("dd", {}, `${card.weekday}, ${prettyDate(card.date)} · ${card.time}`)),
      card.action !== "cancel" ? el("p", { class: "small" }, "Please arrive 15 minutes early with your booking reference.") : null));
  } else if (card.type === "escalated") {
    box.append(el("div", { class: `box ${card.priority === "URGENT" ? "urgent" : "normal"}`, role: "alert" },
      el("h3", {}, card.emergency ? "⚠ Possible emergency" : "☎ Clinic staff will call you"),
      card.emergency
        ? el("p", {}, "Call 108 or 112 now, or go to the nearest hospital. Our staff have also been alerted.")
        : el("p", {}, "Your request has been passed to the front desk. They will call you back during clinic hours.")));
  }
}

// ------------------------------------------------------------------ call flow

async function startCall() {
  $("start").disabled = true;
  $("setup-error").hidden = true;
  try {
    const patientId = $("caller").value || null;
    const res = await postJSON("/api/session", { patient_id: patientId, speak: true });
    state.session = res.session_id;
    state.lang = res.lang;
    $("caller-name").textContent = res.patient_name ? `${res.patient_name} (synthetic)` : "New caller";
    $("setup").hidden = true;
    $("live").hidden = false;
    $("staff").disabled = false;
    $("transcript").innerHTML = "";
    const url = res.audio_b64 ? b64ToUrl(res.audio_b64) : null;
    addBubble("bot", res.reply, { lang: res.lang, audioUrl: url, note: res.tts_error });
    if (res.tts) addTrace("TTS", `${res.tts.model} · ${res.tts.voice}`, res.tts.language,
      res.tts.cached ? "cache" : res.tts.latency_ms, res.tts.request_id);
    if (url) { setStatus("CareCall AI is speaking…", "speaking"); await play(url); }
    setStatus("Ready — tap the green button and speak", "idle");
    $("mic").focus();
  } catch (err) {
    $("setup-error").textContent = err.message;
    $("setup-error").hidden = false;
    $("start").disabled = !$("consent").checked;
  }
}

async function endCall() {
  stopAudio();
  recorder.cancel();
  if (state.session) { try { await postJSON(`/api/session/${state.session}/end`, {}); } catch { /* ignore */ } }
  state.session = null;
  $("live").hidden = true;
  $("setup").hidden = false;
  $("staff").disabled = true;
  $("start").disabled = !$("consent").checked;
  setMicIdle();
}

function setMicIdle() {
  $("mic").classList.remove("recording");
  $("mic-label").innerHTML = 'Tap to speak <span lang="hi">· बोलने के लिए दबाएँ</span>';
  $("timer").textContent = "";
}

let timerHandle = null;
async function toggleMic() {
  if (recorder.recording) return stopAndSend();
  if (state.busy) return;
  stopAudio();                                   // barge-in: speaking over the bot stops playback
  try {
    await recorder.start();
  } catch (err) {
    setStatus(err instanceof MicUnavailableError ? err.message : `Microphone error: ${err.message}`, "error");
    $("type").focus();
    return;
  }
  $("mic").classList.add("recording");
  $("mic-label").innerHTML = 'Tap when done <span lang="hi">· बोलना पूरा होने पर दबाएँ</span>';
  setStatus("Listening…", "listening");
  timerHandle = setInterval(() => {
    const s = Math.floor(recorder.elapsed());
    $("timer").textContent = `0:${String(s).padStart(2, "0")} / 0:${MAX_SECONDS}`;
  }, 250);
}

async function stopAndSend() {
  clearInterval(timerHandle);
  setMicIdle();
  const wav = await recorder.stop();
  if (!wav) return;
  setBusy(true);
  setStatus("Transcribing with Gnani Prisma…", "working");
  let stt;
  try {
    const form = new FormData();
    form.append("session_id", state.session);
    form.append("audio", wav, "turn.wav");
    stt = await api("/api/transcribe", { method: "POST", body: form });
  } catch (err) {
    setStatus(err.message, "error");
    setBusy(false);
    return;
  }
  addTrace("STT", stt.model, stt.language, stt.latency_ms, stt.request_id);
  if (stt.text) addBubble("user", stt.text, { lang: stt.language, note: "spoken" });
  else addBubble("user", "(nothing heard)", { note: `${stt.audio_seconds}s of audio` });
  await sendTurn(stt.text, { stt });
}

async function sendText(text, opts = {}) {
  if (!text.trim() || state.busy || !state.session) return;
  stopAudio();
  addBubble("user", text, { note: opts.confirm ? "button" : "typed" });
  await sendTurn(text, opts);
}

async function sendTurn(text, { stt = null, confirm = false } = {}) {
  setBusy(true);
  setStatus("Thinking…", "working");
  try {
    const res = await postJSON("/api/turn", { session_id: state.session, text, confirm, stt, speak: true });
    state.lang = res.lang;
    const url = res.audio_b64 ? b64ToUrl(res.audio_b64) : null;
    addBubble("bot", res.reply, { lang: res.lang, audioUrl: url, escalated: res.escalated,
      note: res.tts_error ? `audio unavailable: ${res.tts_error}` : null });
    if (res.tts) addTrace("TTS", `${res.tts.model} · ${res.tts.voice}`, res.tts.language,
      res.tts.cached ? "cache" : res.tts.latency_ms, res.tts.request_id);
    if (res.source === "llm") {
      const waited = res.llm?.rate_limit_waits ? ` · waited for rate limit ×${res.llm.rate_limit_waits}` : "";
      addTrace("Dialog", `${res.llm?.model || "Gemini"} (tools: ${res.tools.join(", ") || "none"})${waited}`,
        res.lang, res.latency_ms.llm, null);
    }
    renderCard(res.card);
    setBusy(false);
    if (url) { setStatus("CareCall AI is speaking… (tap the mic to interrupt)", "speaking"); await play(url); }
    if (!recorder.recording) setStatus("Ready — tap the green button and speak", "idle");
  } catch (err) {
    setBusy(false);
    setStatus(err.message, "error");
    if (/session/i.test(err.message)) await endCall();
  }
}

// ------------------------------------------------------------------ wiring

async function init() {
  try {
    const patients = await api("/api/patients");
    const group = el("optgroup", { label: "Synthetic patients" },
      patients.map((p) => el("option", { value: p.id }, `${p.name} · ${p.demo_contact}`)));
    $("caller").append(group);
  } catch {
    $("setup-error").textContent = "Could not reach the CareCall server.";
    $("setup-error").hidden = false;
  }
  $("consent").addEventListener("change", (e) => { $("start").disabled = !e.target.checked; });
  $("start").addEventListener("click", startCall);
  $("end").addEventListener("click", endCall);
  $("mic").addEventListener("click", toggleMic);
  $("staff").addEventListener("click", () => sendText(PHRASES.staff[state.lang]));
  $("type-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const text = $("type").value;
    $("type").value = "";
    sendText(text);
  });
  for (const chip of document.querySelectorAll(".chip"))
    chip.addEventListener("click", () => sendText(chip.dataset.say));
  // Space bar toggles the mic when focus isn't in a text field.
  document.addEventListener("keydown", (e) => {
    if (e.code === "Space" && !$("live").hidden && !["INPUT", "SELECT", "TEXTAREA", "BUTTON"].includes(document.activeElement.tagName)) {
      e.preventDefault();
      toggleMic();
    }
  });
}

init();
