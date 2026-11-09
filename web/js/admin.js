// Clinic admin dashboard. Polls every 3 s so voice-console activity appears live.
const $ = (id) => document.getElementById(id);
const POLL_MS = 3000;
const LANG = { "hi-IN": "Hindi", "en-IN": "English", "hi-en": "Hinglish" };
const OUTCOME_TAG = { BOOKED: "ok", CANCELLED: "info", RESCHEDULED: "info", ESCALATED: "bad", ANSWERED: "info",
  IN_PROGRESS: "warn" };
const REASON = { MEDICAL: "Medical / symptom", HUMAN_REQUEST: "Asked for staff", NOT_UNDERSTOOD: "Could not understand",
  SYSTEM_ERROR: "System problem", OTHER: "Other request" };

let activeTab = "appts";
let knownRefs = null;
let openCall = null;

// ------------------------------------------------------------------ helpers

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null && v !== false) node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat(Infinity)) if (c !== null && c !== undefined && c !== false) node.append(c);
  return node;
}
const tag = (kind, text) => el("span", { class: `tag tag-${kind}` }, text);
const hasDev = (t) => /[ऀ-ॿ]/.test(t || "");
const textEl = (t, tagName = "span") => el(tagName, hasDev(t) ? { lang: "hi" } : {}, t || "");

async function api(path, options = {}) {
  const res = await fetch(path, options);
  if (res.status === 401) {                    // hosted demo: access code missing or expired
    location.href = `/access.html?next=${encodeURIComponent(location.pathname)}`;
    throw new Error("Access code required");
  }
  let body = null;
  try { body = await res.json(); } catch { /* empty */ }
  if (!res.ok) {
    const detail = Array.isArray(body?.detail) ? body.detail.map((d) => d.msg).join("; ") : body?.detail;
    throw new Error(detail || `Request failed (${res.status})`);
  }
  return body;
}
const send = (method, path, data) => api(path, { method, headers: { "Content-Type": "application/json" },
  body: JSON.stringify(data ?? {}) });

function flash(message, bad = false) {
  const f = $("flash");
  f.textContent = message;
  f.className = bad ? "flash bad" : "flash";
  f.hidden = false;
  clearTimeout(flash.t);
  flash.t = setTimeout(() => { f.hidden = true; }, 5000);
}
// Local calendar date as YYYY-MM-DD (toISOString would give the UTC date).
const localISO = (d = new Date()) => d.toLocaleDateString("en-CA");
function dayLabel(iso) {
  const today = localISO();
  const d = new Date(`${iso}T00:00:00`);
  const label = d.toLocaleDateString("en-IN", { weekday: "long", day: "numeric", month: "long" });
  const tomorrow = localISO(new Date(Date.now() + 864e5));
  return iso === today ? `Today · ${label}` : iso === tomorrow ? `Tomorrow · ${label}` : label;
}
function timeAgo(iso) {
  const mins = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min ago`;
  const h = Math.floor(mins / 60);
  return h < 24 ? `${h} h ago` : new Date(iso).toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" });
}
function clock(iso) {
  return new Date(iso).toLocaleString("en-IN", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}
// Don't re-render a panel while someone is typing in it.
function busyEditing(panelId) {
  const panel = $(panelId);
  const active = document.activeElement;
  if (active && panel.contains(active) && ["INPUT", "TEXTAREA"].includes(active.tagName) && active.type !== "checkbox") return true;
  return [...panel.querySelectorAll(".inline-form input, .inline-form textarea")].some((i) => i.value.trim());
}

// ------------------------------------------------------------------ summary

async function loadSummary() {
  const s = await api("/api/admin/summary");
  $("today").textContent = dayLabel(s.date);
  $("m-appts").textContent = s.appointments_today;
  $("m-appts-sub").textContent = `${s.completed_today} completed`;
  $("m-slots").textContent = s.open_slots_today;
  $("m-calls").textContent = s.ai_calls_today;
  $("m-calls-sub").textContent = s.escalated_calls_today ? `${s.escalated_calls_today} passed to staff` : "all handled by AI";
  $("m-fu").textContent = s.open_followups;
  $("m-fu-sub").textContent = s.urgent_followups ? `${s.urgent_followups} urgent` : s.open_followups ? "routine" : "nothing waiting";
  $("m-fu-tile").classList.toggle("alert", s.open_followups > 0);
  $("fu-badge").hidden = !s.open_followups;
  $("fu-badge").textContent = s.open_followups;
}

// ------------------------------------------------------------------ appointments

async function loadAppointments() {
  const rows = await api(`/api/admin/appointments?include_cancelled=${$("show-cancelled").checked}`);
  const tbody = $("appts");
  if (busyEditing("panel-appts")) return;
  const refs = new Set(rows.map((r) => r.ref));
  tbody.innerHTML = "";
  if (!rows.length) tbody.append(el("tr", {}, el("td", { colspan: 7, class: "muted" }, "No appointments in the next 7 days.")));
  let day = null;
  for (const a of rows) {
    if (a.date !== day) {
      day = a.date;
      tbody.append(el("tr", { class: "day-row" }, el("td", { colspan: 7 }, dayLabel(day))));
    }
    const fresh = knownRefs && !knownRefs.has(a.ref);
    const status = a.status === "CONFIRMED" ? tag("ok", "Confirmed") : a.status === "COMPLETED" ? tag("info", "Completed")
      : tag("bad", "Cancelled");
    const source = a.source === "AI" ? tag("info", "Booked by AI") : a.source === "STAFF" ? tag("warn", "Staff") : tag("warn", "Earlier");
    const tr = el("tr", { class: `${a.status === "CANCELLED" ? "cancelled" : ""} ${fresh ? "fresh" : ""}` },
      el("td", {}, el("strong", {}, a.start_time)),
      el("td", {}, a.patient, el("div", { class: "muted small" }, a.demo_contact)),
      el("td", {}, a.doctor, el("div", { class: "muted small" }, `${a.specialty} · ₹${a.fee_inr}`)),
      el("td", {}, status, a.cancel_reason ? el("div", { class: "muted small" }, a.cancel_reason) : null),
      el("td", {}, source, a.rescheduled_from ? el("div", { class: "muted small" }, `moved from ${a.rescheduled_from}`) : null),
      el("td", {}, el("code", {}, a.ref)),
      el("td", {}, a.status === "CONFIRMED" ? cancelControl(a) : null));
    tbody.append(tr);
  }
  knownRefs = refs;
}

function cancelControl(a) {
  const wrap = el("div");
  const btn = el("button", { class: "btn btn-sm btn-danger", type: "button" }, "Cancel…");
  btn.addEventListener("click", () => {
    const input = el("input", { "aria-label": `Reason for cancelling ${a.ref}`, placeholder: "Reason (required)" });
    const form = el("form", { class: "inline-form" }, input,
      el("button", { class: "btn btn-sm btn-danger", type: "submit" }, "Confirm cancel"),
      el("button", { class: "btn btn-sm btn-secondary", type: "button", onclick: () => { form.remove(); btn.hidden = false; } }, "Keep"));
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        await send("POST", `/api/admin/appointments/${a.ref}/cancel`, { reason: input.value });
        flash(`${a.ref} cancelled. The slot is free for new bookings.`);
        form.remove();          // also releases focus, so the next poll re-renders the table
        await refresh();
      } catch (err) { flash(err.message, true); }
    });
    btn.hidden = true;
    wrap.append(form);
    input.focus();
  });
  wrap.append(btn);
  return wrap;
}

// ------------------------------------------------------------------ follow-ups

async function loadFollowups() {
  if (busyEditing("panel-fu")) return;
  const items = await api(`/api/admin/followups?status=${$("show-resolved").checked ? "ALL" : "OPEN"}`);
  const box = $("followups");
  box.innerHTML = "";
  if (!items.length) { box.append(el("p", { class: "muted" }, "No callbacks waiting. 🎉")); return; }
  for (const f of items) {
    const urgent = f.priority === "URGENT";
    const resolved = f.status === "RESOLVED";
    const card = el("article", { class: `ticket ${urgent ? "urgent" : ""} ${resolved ? "resolved" : ""}` },
      el("div", { class: "ticket-head" },
        resolved ? tag("ok", "Resolved") : urgent ? tag("bad", "⚠ Urgent") : tag("info", "Routine"),
        el("strong", {}, REASON[f.reason] || f.reason),
        el("span", { class: "muted small" }, `· ${timeAgo(f.created_at)}`),
        f.language ? tag("info", LANG[f.language] || f.language) : null),
      el("div", {}, el("strong", {}, f.patient || "Unknown caller"), " ",
        el("span", { class: "muted small" }, f.demo_contact || "")),
      el("blockquote", { class: "quote" }, "Caller said: ", textEl(`“${f.trigger_text}”`)),
      resolved
        ? el("p", { class: "small" }, `Resolution: ${f.resolution_note} (${timeAgo(f.resolved_at)})`)
        : resolveForm(f),
      f.call_id ? el("button", { class: "btn btn-sm btn-secondary", type: "button",
        onclick: () => { openCall = f.call_id; selectTab("calls"); } }, "View full call") : null);
    box.append(card);
  }
}

function resolveForm(f) {
  const input = el("input", { "aria-label": "What did staff do?", placeholder: "Note, e.g. Called back, advised hospital visit" });
  const form = el("form", { class: "inline-form" }, input,
    el("button", { class: "btn btn-sm btn-primary", type: "submit" }, "Mark resolved"));
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await send("POST", `/api/admin/followups/${f.id}/resolve`, { note: input.value });
      input.value = "";
      input.blur();
      flash("Follow-up resolved.");
      await refresh();
    } catch (err) { flash(err.message, true); }
  });
  return form;
}

// ------------------------------------------------------------------ calls

async function loadCalls() {
  const calls = await api("/api/admin/calls");
  const tbody = $("calls");
  tbody.innerHTML = "";
  if (!calls.length) tbody.append(el("tr", {}, el("td", { colspan: 7, class: "muted" }, "No calls yet.")));
  for (const c of calls) {
    const expanded = openCall === c.id;
    tbody.append(el("tr", {},
      el("td", {}, clock(c.started_at)),
      el("td", {}, c.patient || "New caller", el("div", { class: "muted small" }, c.demo_contact || "")),
      el("td", {}, c.language ? tag("info", LANG[c.language] || c.language) : "–"),
      el("td", {}, c.intent || "–"),
      el("td", {}, tag(OUTCOME_TAG[c.outcome] || "info", (c.outcome || "–").replace("_", " ").toLowerCase())),
      el("td", {}, textEl(c.summary || "")),
      el("td", {}, el("button", { class: "btn btn-sm btn-secondary", type: "button", "aria-expanded": expanded,
        onclick: () => { openCall = expanded ? null : c.id; loadCalls(); } }, expanded ? "Hide" : `Transcript (${c.turn_count})`))));
    if (expanded) tbody.append(el("tr", {}, el("td", { colspan: 7 }, await transcript(c.id))));
  }
}

async function transcript(callId) {
  const { turns } = await api(`/api/admin/calls/${callId}`);
  return el("ol", { class: "turns" }, turns.map((t) => {
    const timing = [t.stt_ms && `STT ${t.stt_ms} ms`, t.llm_ms && `LLM ${t.llm_ms} ms`, t.tts_ms && `TTS ${t.tts_ms} ms`]
      .filter(Boolean).join(" · ");
    return el("li", { class: t.role },
      el("strong", {}, t.role === "user" ? "Caller: " : "CareCall AI: "), textEl(t.text),
      (timing || t.gnani_request_id) ? el("div", { class: "tmeta" },
        [timing, t.gnani_request_id && `Gnani request ${t.gnani_request_id}`].filter(Boolean).join(" · ")) : null);
  }));
}

// ------------------------------------------------------------------ clinic data

async function loadDoctors() {
  const docs = await api("/api/admin/doctors");
  const tbody = $("doctors");
  tbody.innerHTML = "";
  const today = localISO();
  for (const d of docs) {
    const dateInput = el("input", { type: "date", min: today, value: today, "aria-label": `Leave date for ${d.name}` });
    const toggle = async (on) => {
      try {
        const r = await send("POST", `/api/admin/doctors/${d.id}/leave`, { date: dateInput.value, on });
        flash(on ? `${d.name} marked on leave for ${dayLabel(r.date)}.` +
          (r.existing_bookings_that_day ? ` ${r.existing_bookings_that_day} existing booking(s) that day need a call.` : "")
          : `${d.name} available again on ${dayLabel(r.date)}.`);
        await loadDoctors();
      } catch (err) { flash(err.message, true); }
    };
    tbody.append(el("tr", {},
      el("td", {}, el("strong", {}, d.name), el("div", { class: "muted small" }, d.specialty)),
      el("td", {}, `₹${d.fee_inr}`, el("div", { class: "muted small" }, `${d.slot_mins}-min slots`)),
      el("td", {}, d.days.join(", "), el("div", { class: "muted small" }, d.hours.join(" · "))),
      el("td", {},
        d.leave.length ? el("div", {}, d.leave.map((x) => [tag("warn", `On leave ${dayLabel(x)}`), " "])) : el("div", { class: "muted small" }, "No leave planned"),
        el("div", { class: "inline-form" }, dateInput,
          el("button", { class: "btn btn-sm btn-danger", type: "button", onclick: () => toggle(true) }, "Mark on leave"),
          el("button", { class: "btn btn-sm btn-secondary", type: "button", onclick: () => toggle(false) }, "Available")))));
  }
}

async function loadFaqs() {
  const faqs = await api("/api/admin/faqs");
  const box = $("faqs");
  box.innerHTML = "";
  for (const f of faqs) {
    const en = el("textarea", { id: `en-${f.id}` }, f.answer_en);
    const hi = el("textarea", { id: `hi-${f.id}`, lang: "hi" }, f.answer_hi);
    const form = el("form", { class: "faq" },
      el("strong", {}, f.category), el("span", { class: "muted small" }, ` · ${f.id}`),
      el("label", { for: `en-${f.id}` }, "English answer"), en,
      el("label", { for: `hi-${f.id}` }, "Hindi answer"), hi,
      el("div", { class: "actions" }, el("button", { class: "btn btn-sm btn-primary", type: "submit" }, "Save")));
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        await send("PUT", `/api/admin/faqs/${f.id}`, { answer_en: en.value, answer_hi: hi.value, active: true });
        flash(`Saved. The AI will use the new ${f.category.toLowerCase()} answer from the next question.`);
      } catch (err) { flash(err.message, true); }
    });
    box.append(form);
  }
}

// ------------------------------------------------------------------ tabs & polling

const LOADERS = { appts: loadAppointments, fu: loadFollowups, calls: loadCalls, data: null };

function selectTab(name) {
  activeTab = name;
  for (const t of ["appts", "fu", "calls", "data"]) {
    $(`tab-${t}`).setAttribute("aria-selected", String(t === name));
    $(`panel-${t}`).hidden = t !== name;
  }
  if (name === "data") { loadDoctors(); loadFaqs(); }
  refresh();
}

async function refresh() {
  try {
    await loadSummary();
    if (LOADERS[activeTab]) await LOADERS[activeTab]();
    $("updated").textContent = `Live · updated ${new Date().toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", second: "2-digit" })}`;
  } catch (err) {
    $("updated").textContent = `Connection problem: ${err.message}`;
  }
}

function init() {
  for (const t of ["appts", "fu", "calls", "data"]) $(`tab-${t}`).addEventListener("click", () => selectTab(t));
  // Arrow keys move between tabs (WAI-ARIA tabs pattern).
  $("tab-appts").parentElement.addEventListener("keydown", (e) => {
    const order = ["appts", "fu", "calls", "data"];
    const i = order.indexOf(activeTab);
    if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
      const next = order[(i + (e.key === "ArrowRight" ? 1 : order.length - 1)) % order.length];
      selectTab(next);
      $(`tab-${next}`).focus();
    }
  });
  $("show-cancelled").addEventListener("change", loadAppointments);
  $("show-resolved").addEventListener("change", loadFollowups);
  $("reset").addEventListener("click", async () => {
    if (!confirm("Reset all demo data? Bookings, calls and follow-ups go back to the starting state.")) return;
    try {
      const r = await send("POST", "/api/admin/reset");
      knownRefs = null;
      openCall = null;
      flash(`Demo data reset: ${r.counts.appointments} appointments, ${r.counts.followups} follow-ups.`);
      if (activeTab === "data") { loadDoctors(); loadFaqs(); }
      await refresh();
    } catch (err) { flash(err.message, true); }
  });
  refresh();
  setInterval(() => { if (!document.hidden) refresh(); }, POLL_MS);
}

init();
