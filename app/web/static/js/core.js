// ───────────────────────── Nova Dashboard – Kern (DOM, API, UI-Primitive) ─────────────────────────
// Sicherheit: Nutzerdaten werden ausschließlich als Textknoten eingefügt (kein innerHTML mit fremden Daten).

export const state = { me: null, guildId: null, ctx: null, meta: null, lang: "de", wsHandlers: new Map(), cleanup: [] };

// ── DOM-Builder ──
export function h(tag, attrs, ...children) {
  const m = /^([a-z0-9-]+)?((?:[.#][\w-]+)*)$/i.exec(tag) || [];
  const el = document.createElement(m[1] || "div");
  (m[2] || "").replace(/([.#])([\w-]+)/g, (_, t, v) => (t === "." ? el.classList.add(v) : (el.id = v)));
  if (attrs !== null && attrs !== undefined && (typeof attrs !== "object" || attrs instanceof Node || Array.isArray(attrs))) {
    children.unshift(attrs);
    attrs = null;
  }
  // Ruhiges Design: führende Emojis in Überschriften entfernen
  if (/^h[1-4]$/.test(el.tagName.toLowerCase()) && typeof children[0] === "string") children[0] = stripEmoji(children[0]);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className += (el.className ? " " : "") + v;
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k === "_html") el.innerHTML = v; // nur für vertrauenswürdige, statische Inhalte
    else if (["value", "checked", "disabled", "selected", "multiple", "hidden"].includes(k)) el[k] = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  append(el, children);
  return el;
}

export function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

export const stripEmoji = (s) => s.replace(/^(?:\p{Extended_Pictographic}[\u{FE0F}\u{200D}\p{Extended_Pictographic}]*\s*)+/u, "");

const ICONS = {
  dashboard: '<rect x="3" y="3" width="7" height="9" rx="1"/><rect x="14" y="3" width="7" height="5" rx="1"/><rect x="14" y="12" width="7" height="9" rx="1"/><rect x="3" y="16" width="7" height="5" rx="1"/>',
  members: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/>',
  moderation: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
  automod: '<rect x="4" y="8" width="16" height="12" rx="2"/><path d="M12 8V4M9 14h.01M15 14h.01"/>',
  tickets: '<path d="M3 9a3 3 0 0 0 0 6v3a1 1 0 0 0 1 1h16a1 1 0 0 0 1-1v-3a3 3 0 0 0 0-6V6a1 1 0 0 0-1-1H4a1 1 0 0 0-1 1z"/><path d="M13 5v2M13 11v2M13 17v2"/>',
  giveaways: '<rect x="3" y="8" width="18" height="4" rx="1"/><path d="M12 8v13M19 12v7a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2v-7M7.5 8a2.5 2.5 0 0 1 0-5C11 3 12 8 12 8s1-5 4.5-5a2.5 2.5 0 0 1 0 5"/>',
  levels: '<path d="M12 2l3.09 6.26L22 9.27l-5 4.87L18.18 21 12 17.77 5.82 21 7 14.14l-5-4.87 6.91-1.01z"/>',
  economy: '<circle cx="8" cy="8" r="6"/><path d="M18.09 10.37A6 6 0 1 1 10.34 18M7 6h1v4"/>',
  music: '<path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>',
  streamer: '<path d="M4.9 19.1C1 15.2 1 8.8 4.9 4.9M7.8 16.2c-2.3-2.3-2.3-6.1 0-8.5M16.2 7.8c2.3 2.3 2.3 6.1 0 8.5M19.1 4.9C23 8.8 23 15.1 19.1 19"/><circle cx="12" cy="12" r="2"/>',
  suggestions: '<path d="M9 18h6M10 22h4M15.09 14c.18-.98.65-1.74 1.41-2.5A4.65 4.65 0 0 0 18 8 6 6 0 0 0 6 8c0 1 .23 2.23 1.5 3.5A4.61 4.61 0 0 1 8.91 14"/>',
  events: '<rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/>',
  analytics: '<path d="M3 3v18h18M18 17V9M13 17V5M8 17v-3"/>',
  roles: '<path d="M12.586 2.586A2 2 0 0 0 11.172 2H4a2 2 0 0 0-2 2v7.172a2 2 0 0 0 .586 1.414l8.704 8.704a2.426 2.426 0 0 0 3.42 0l6.58-6.58a2.426 2.426 0 0 0 0-3.42z"/><circle cx="7.5" cy="7.5" r=".5"/>',
  notifications: '<path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9M10.3 21a1.94 1.94 0 0 0 3.4 0"/>',
  logs: '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7z"/><path d="M14 2v5h5M16 13H8M16 17H8M10 9H8"/>',
  settings: '<path d="M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6"/>',
  owner: '<path d="M2 4l3 12h14l3-12-6 7-4-7-4 7-6-7zM5 20h14"/>',
  logout: '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9"/>',
  welcome: '<path d="M13 4h3a2 2 0 0 1 2 2v14M2 20h3M13 20h9M10 12v.01M13 4.56v16.16a1 1 0 0 1-1.24.97L5 20V5.56a2 2 0 0 1 1.52-1.94l4-1A2 2 0 0 1 13 4.56z"/>',
  features: '<path d="M4 17l6-6-6-6M12 19h8"/>',
  menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
  updown: '<path d="M7 15l5 5 5-5M7 9l5-5 5 5"/>',
};
// Statische, vertrauenswürdige SVG-Icons (keine Nutzerdaten)
export const icon = (name) => h("span.ico", { _html: `<svg class="i" viewBox="0 0 24 24">${ICONS[name] || ""}</svg>` });

export const $ = (sel, root = document) => root.querySelector(sel);
export const clear = (el) => { while (el.firstChild) el.firstChild.remove(); return el; };
// Inhalt ersetzen – null/false-Kinder werden ignoriert (natives append() würde "null" rendern)
export const fill = (el, ...children) => append(clear(el), children);

// ── i18n (Dashboard-Oberfläche) ──
const DICT = {
  de: {
    dashboard: "Dashboard", welcome: "Willkommen", features: "Befehle", members: "Mitglieder", moderation: "Moderation", automod: "Automod", tickets: "Tickets", giveaways: "Giveaways",
    levels: "Level", economy: "Economy", music: "Musik", streamer: "Streamer", analytics: "Analytics", roles: "Rollen",
    notifications: "Benachrichtigungen", suggestions: "Suggestions", events: "Events", logs: "Logs", settings: "Einstellungen",
    owner: "Owner Panel", save: "Speichern", cancel: "Abbrechen", delete: "Löschen", edit: "Bearbeiten", create: "Erstellen",
    search: "Suchen …", loading: "Lädt …", empty: "Noch nichts hier", saved: "Gespeichert", logout: "Abmelden", switch_server: "Server wechseln",
    confirm: "Bestätigen", close: "Schließen", add: "Hinzufügen", online: "Online", offline: "Offline", actions: "Aktionen", back: "Zurück",
    enabled: "Aktiv", disabled: "Deaktiviert", section_main: "Übersicht", section_community: "Community", section_manage: "Verwaltung",
  },
  en: {
    dashboard: "Dashboard", welcome: "Welcome", features: "Commands", members: "Members", moderation: "Moderation", automod: "Automod", tickets: "Tickets", giveaways: "Giveaways",
    levels: "Levels", economy: "Economy", music: "Music", streamer: "Streamer", analytics: "Analytics", roles: "Roles",
    notifications: "Notifications", suggestions: "Suggestions", events: "Events", logs: "Logs", settings: "Settings",
    owner: "Owner Panel", save: "Save", cancel: "Cancel", delete: "Delete", edit: "Edit", create: "Create",
    search: "Search …", loading: "Loading …", empty: "Nothing here yet", saved: "Saved", logout: "Log out", switch_server: "Switch server",
    confirm: "Confirm", close: "Close", add: "Add", online: "Online", offline: "Offline", actions: "Actions", back: "Back",
    enabled: "Enabled", disabled: "Disabled", section_main: "Overview", section_community: "Community", section_manage: "Management",
  },
};
export const t = (key) => (DICT[state.lang] || DICT.de)[key] || DICT.de[key] || key;

// ── API ──
export class ApiError extends Error {
  constructor(status, message, errors) { super(message); this.status = status; this.errors = errors || {}; }
}

export async function api(path, { method = "GET", body, quiet = false } = {}) {
  const opts = { method, credentials: "same-origin", headers: { Accept: "application/json" } };
  if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  if (method !== "GET" && state.me) opts.headers["X-CSRF-Token"] = state.me.csrf;
  let res;
  try { res = await fetch(path, opts); } catch (e) {
    if (!quiet) toast("Keine Verbindung zum Server", "error");
    throw new ApiError(0, "network");
  }
  if (res.status === 401 && !path.endsWith("/api/me")) { location.href = "/"; throw new ApiError(401, "unauthorized"); }
  const ct = res.headers.get("content-type") || "";
  const data = ct.includes("json") ? await res.json().catch(() => ({})) : await res.text();
  if (!res.ok) {
    let msg = "Fehler", errors = {};
    const d = data && data.detail !== undefined ? data.detail : data;
    if (typeof d === "string") msg = d;
    else if (d && typeof d === "object") { msg = d.message || msg; errors = d.errors || {}; }
    if (res.status === 429) msg = "Zu viele Anfragen – bitte kurz warten.";
    if (!quiet) toast(msg, "error");
    throw new ApiError(res.status, msg, errors);
  }
  return data;
}
export const gapi = (path, opts) => api(`/api/g/${state.guildId}${path}`, opts);

// ── Formatierung ──
const nf = () => new Intl.NumberFormat(state.lang === "en" ? "en-US" : "de-DE");
export const fmtNum = (n) => (n === null || n === undefined ? "—" : nf().format(n));
export function fmtDate(iso, withTime = true) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString(state.lang === "en" ? "en-GB" : "de-DE", withTime ? { dateStyle: "medium", timeStyle: "short" } : { dateStyle: "medium" });
}
export function fmtRel(iso) {
  if (!iso) return "—";
  const diff = (new Date(iso) - Date.now()) / 1000;
  const rtf = new Intl.RelativeTimeFormat(state.lang, { numeric: "auto" });
  const units = [["year", 31536000], ["month", 2592000], ["week", 604800], ["day", 86400], ["hour", 3600], ["minute", 60], ["second", 1]];
  for (const [u, s] of units) if (Math.abs(diff) >= s || u === "second") return rtf.format(Math.round(diff / s), u);
}
export function fmtDuration(sec) {
  if (sec === null || sec === undefined) return "—";
  sec = Math.floor(sec);
  const d = Math.floor(sec / 86400), hh = Math.floor((sec % 86400) / 3600), mm = Math.floor((sec % 3600) / 60);
  return [d && `${d}d`, (d || hh) && `${hh}h`, `${mm}m`].filter(Boolean).join(" ");
}
export const fmtMinutes = (m) => `${Math.floor((m || 0) / 60)}h ${(m || 0) % 60}m`;

// ── Toasts ──
export function toast(message, kind = "info", ms = 3800) {
  const icon = { success: "✅", error: "⛔", warning: "⚠️", info: "ℹ️" }[kind] || "ℹ️";
  const el = h("div.toast." + kind, h("span", icon), h("div", message));
  $("#toasts").append(el);
  setTimeout(() => { el.classList.add("out"); setTimeout(() => el.remove(), 260); }, ms);
}

// ── Modal & Bestätigung ──
export function openModal({ title, body, footer = [], size = "" , onClose } = {}) {
  const close = () => { bg.remove(); document.removeEventListener("keydown", esc); onClose && onClose(); };
  const esc = (e) => e.key === "Escape" && close();
  const modal = h("div.modal" + (size ? "." + size : ""), { role: "dialog", "aria-modal": "true" },
    h("div.modal-head", h("h3", title), h("button.btn.ghost.icon", { onclick: close, "aria-label": t("close") }, "✕")),
    h("div.modal-body", body),
    footer.length ? h("div.modal-foot", footer) : null);
  const bg = h("div.modal-bg", { onmousedown: (e) => e.target === bg && close() }, modal);
  document.body.append(bg);
  document.addEventListener("keydown", esc);
  setTimeout(() => modal.querySelector("input, select, textarea")?.focus(), 50);
  return { close, el: modal };
}

export function confirmDialog({ title = "Sicher?", text = "", confirmText, danger = true, typeToConfirm = null } = {}) {
  return new Promise((resolve) => {
    let done = false;
    const input = typeToConfirm ? h("input", { type: "text", placeholder: typeToConfirm }) : null;
    const ok = h("button.btn" + (danger ? ".danger" : ".primary"), { disabled: !!typeToConfirm }, confirmText || t("confirm"));
    if (input) input.addEventListener("input", () => (ok.disabled = input.value !== typeToConfirm));
    const body = h("div.stack", h("p", { style: { margin: 0, color: "var(--text-2)" } }, text),
      input ? h("div.field", h("label", `Zur Bestätigung „${typeToConfirm}“ eingeben`), input) : null);
    const m = openModal({ title, body, size: "sm", onClose: () => !done && resolve(false),
      footer: [h("button.btn.ghost", { onclick: () => m.close() }, t("cancel")), ok] });
    ok.addEventListener("click", () => { done = true; m.close(); resolve(true); });
  });
}

export async function withLoading(btn, fn) {
  btn.classList.add("loading");
  try { return await fn(); } finally { btn.classList.remove("loading"); }
}

// ── Bausteine ──
export const pill = (text, color = "") => h("span.pill" + (color ? "." + color : ""), text);
export function userCell(u, sub) {
  if (!u) return h("span.muted", "—");
  return h("div.user-cell", u.avatar ? h("img", { src: u.avatar, alt: "", loading: "lazy" }) : null,
    h("span", u.display || u.name || u.id, sub ? h("small", sub) : null));
}
export function emptyState(text = t("empty"), icon = "✨") { return h("div.empty", h("b", icon), text); }
export function skeleton(height = 120) { return h("div.skeleton", { style: { height: height + "px" } }); }
export function statCard(label, value, icon, sub) {
  return h("div.glass.stat", h("div.ico", icon), h("div.label", label), h("div.value", value), sub ? h("div.sub", sub) : null);
}
export function progressBar(value, total) {
  const pct = total ? Math.min(100, Math.round((value / total) * 100)) : 0;
  return h("div.progress", h("i", { style: { width: pct + "%" } }));
}

export function tabs(items, active, onChange) {
  const bar = h("div.tabs", { role: "tablist" });
  for (const [key, rawLabel] of items) {
    const label = stripEmoji(rawLabel);
    const b = h("button", { class: key === active ? "active" : "", role: "tab", onclick: () => {
      bar.querySelectorAll("button").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      onChange(key);
    } }, label);
    bar.append(b);
  }
  return bar;
}

// Tabelle (responsive: wird auf dem Handy zu Karten)
export function table(columns, rows, { onRow, empty } = {}) {
  if (!rows.length) return emptyState(empty);
  const thead = h("thead", h("tr", columns.map((c) => h("th", { class: c.cls || "" }, c.label))));
  const tbody = h("tbody", rows.map((r) => h("tr", { class: onRow ? "click" : "", onclick: onRow ? (e) => { if (!e.target.closest("button, a")) onRow(r); } : null },
    columns.map((c) => h("td", { class: c.cls || "", "data-label": c.label }, c.render ? c.render(r) : (r[c.key] ?? "—"))))));
  return h("div.table-wrap", h("table.t.responsive", thead, tbody));
}

export function pager(page, perPage, total, onPage) {
  const pages = Math.max(1, Math.ceil(total / perPage));
  return h("div.pager", h("span", `${fmtNum(total)} Einträge · Seite ${page}/${pages}`),
    h("div.row", h("button.btn.sm", { disabled: page <= 1, onclick: () => onPage(page - 1) }, "‹"),
      h("button.btn.sm", { disabled: page >= pages, onclick: () => onPage(page + 1) }, "›")));
}

export function debounce(fn, ms = 300) {
  let tm;
  return (...a) => { clearTimeout(tm); tm = setTimeout(() => fn(...a), ms); };
}

// ── Live-Events (WebSocket) ──
export function onLive(event, fn) {
  if (!state.wsHandlers.has(event)) state.wsHandlers.set(event, new Set());
  state.wsHandlers.get(event).add(fn);
  const off = () => state.wsHandlers.get(event)?.delete(fn);
  state.cleanup.push(off);
  return off;
}

// ── Charts ──
export function chart(canvas, config) {
  if (!window.Chart) return null;
  const Chart = window.Chart;
  Chart.defaults.color = "#6c6c74";
  Chart.defaults.font.family = "Inter, system-ui, sans-serif";
  Chart.defaults.borderColor = "rgba(255,255,255,.06)";
  const c = new Chart(canvas, config);
  state.cleanup.push(() => c.destroy());
  return c;
}
export function accentRGBA(a = 1) {
  const rgb = getComputedStyle(document.documentElement).getPropertyValue("--accent-rgb").trim() || "139, 92, 246";
  return `rgba(${rgb}, ${a})`;
}
export const PALETTE = ["#c8a45d", "#5fb37a", "#7a8ba8", "#d9a441", "#d25a5a", "#b58ab8", "#6fa8a0", "#b89b5e", "#8c8c94", "#c97b56"];

export function lineChart(canvas, labels, datasets, { hourly = false, stacked = false, bar = false } = {}) {
  const fmtLabel = (l) => {
    const d = new Date(hourly ? l + ":00Z" : l + "T00:00:00Z");
    return hourly ? d.toLocaleTimeString(state.lang, { hour: "2-digit", minute: "2-digit" }) : d.toLocaleDateString(state.lang, { day: "2-digit", month: "2-digit" });
  };
  return chart(canvas, {
    type: bar ? "bar" : "line",
    data: { labels: labels.map(fmtLabel), datasets: datasets.map((ds, i) => {
      const color = ds.color || PALETTE[i % PALETTE.length];
      return { tension: .35, borderWidth: 2, pointRadius: 0, pointHoverRadius: 4, fill: !bar && datasets.length === 1,
        borderColor: color, backgroundColor: bar ? color + "cc" : color + "22", borderRadius: bar ? 6 : 0, ...ds };
    }) },
    options: {
      responsive: true, maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
      plugins: { legend: { display: datasets.length > 1, labels: { boxWidth: 10, boxHeight: 10, usePointStyle: true } },
        tooltip: { backgroundColor: "rgba(15,15,25,.95)", borderColor: "rgba(255,255,255,.1)", borderWidth: 1, padding: 10, cornerRadius: 10 } },
      scales: { x: { grid: { display: false }, stacked, ticks: { maxTicksLimit: 10 } },
        y: { beginAtZero: true, stacked, grid: { color: "rgba(255,255,255,.05)" }, ticks: { precision: 0 } } },
    },
  });
}

export function setAccent(hex) {
  if (!/^#[0-9a-f]{6}$/i.test(hex || "")) return;
  const n = parseInt(hex.slice(1), 16);
  document.documentElement.style.setProperty("--accent", hex);
  document.documentElement.style.setProperty("--accent-rgb", `${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}`);
}

export function copy(text) {
  navigator.clipboard?.writeText(text).then(() => toast("Kopiert", "success", 1500));
}
