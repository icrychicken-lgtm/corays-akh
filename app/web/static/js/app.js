// ───────────────────────── Nova Dashboard – App-Shell, Router, Live-Verbindung ─────────────────────────
import { h, $, clear, fill, icon, state, api, gapi, t, toast, setAccent, debounce, userCell, fmtDuration, fmtNum, emptyState } from "./core.js";

// Oben nur das Wichtigste (Streams zuerst). Die Abschnitte darunter sind einklappbar (Standard: zu).
const NAV = [
  ["section_main"],
  ["dashboard", "🏠", null, "staff"], ["streamer", "📡", "streamer", "staff"], ["members", "👥", null, "staff"], ["moderation", "🛡️", "moderation", "staff"],
  ["tickets", "🎫", "tickets", "staff"], ["settings", "⚙️", null, "admin"],
  ["section_community"],
  ["levels", "⭐", "levels", "staff"], ["economy", "💰", "economy", "staff"], ["giveaways", "🎁", "giveaways", "staff"], ["suggestions", "💡", "suggestions", "staff"],
  ["events", "🎉", "events", "staff"], ["welcome", "👋", "welcome", "admin"], ["music", "🎵", "music", "staff"],
  ["section_manage"],
  ["automod", "🤖", "automod", "admin"], ["notifications", "🔔", "notifications", "admin"], ["logs", "📝", "logging", "staff"], ["roles", "🎭", "roles", "staff"],
  ["analytics", "📊", null, "staff"], ["features", "⌨️", null, "staff"],
];
const COLLAPSIBLE = new Set(["section_community", "section_manage"]);
const PAGES = {
  dashboard: () => import("./pages/home.js"), members: () => import("./pages/members.js"), moderation: () => import("./pages/moderation.js"),
  automod: () => import("./pages/automod.js"), tickets: () => import("./pages/tickets.js"), giveaways: () => import("./pages/giveaways.js"),
  levels: () => import("./pages/levels.js"), economy: () => import("./pages/economy.js"), music: () => import("./pages/music.js"),
  streamer: () => import("./pages/streamer.js"), analytics: () => import("./pages/analytics.js"), roles: () => import("./pages/roles.js"),
  notifications: () => import("./pages/notifications.js"), suggestions: () => import("./pages/suggestions.js"), events: () => import("./pages/events.js"),
  logs: () => import("./pages/logs.js"), welcome: () => import("./pages/welcome.js"), features: () => import("./pages/features.js"), settings: () => import("./pages/settings.js"),
};

let ws = null, wsRetry = 0, wsGuild = null;

// ── Navigation ──
export function navigate(path, replace = false) {
  if (location.pathname === path && !replace) return route();
  history[replace ? "replaceState" : "pushState"]({}, "", path);
  route();
}
window.addEventListener("popstate", route);
document.addEventListener("click", (e) => {
  const a = e.target.closest("a[data-link]");
  if (!a || e.metaKey || e.ctrlKey || e.button !== 0) return;
  e.preventDefault();
  $(".layout")?.classList.remove("nav-open");
  navigate(a.getAttribute("href"));
});

async function boot() {
  try {
    state.me = await api("/api/me", { quiet: true });
  } catch {
    return renderLogin();
  }
  state.lang = (navigator.language || "de").startsWith("en") ? "en" : "de";
  document.title = `${state.me.bot_name} Dashboard`;
  route();
}

function renderLogin() {
  const params = new URLSearchParams(location.search);
  fill($("#root"), h("div.login", h("div.glass.login-card",
    h("div.brand-logo"), h("h1", "Nova Dashboard"),
    h("p", "Verwalte deinen Community-Server – Moderation, Tickets, Streams, Economy und mehr."),
    params.get("login") === "cancelled" ? h("div.callout.warn", "Anmeldung abgebrochen.") : null,
    h("a.btn.discord-btn", { href: "/auth/login" }, "Mit Discord anmelden"),
    h("div.login-features", h("div", "Moderation"), h("div", "Streams"), h("div", "Analytics")))));
}

async function route() {
  state.cleanup.forEach((fn) => { try { fn(); } catch {} });
  state.cleanup = [];
  if (!state.me) return;
  const parts = location.pathname.split("/").filter(Boolean);
  if (parts[0] === "owner") {
    if (!state.me.is_owner) return navigate("/", true);
    await shell(null);
    return loadPage("owner", parts.slice(1), () => import("./pages/owner.js"));
  }
  if (parts[0] !== "g" || !/^\d+$/.test(parts[1] || "")) {
    const last = localStorage.getItem("nova:last-guild");
    if (last && parts.length === 0 && !new URLSearchParams(location.search).has("pick")) return navigate(`/g/${last}`, true);
    return guildPicker();
  }
  const gid = parts[1];
  const page = parts[2] || "dashboard";
  if (state.guildId !== gid || !state.ctx) {
    try {
      state.ctx = await api(`/api/g/${gid}/context`, { quiet: true });
    } catch (e) {
      localStorage.removeItem("nova:last-guild");
      toast(e.status === 403 ? "Kein Zugriff auf diesen Server" : "Server nicht verfügbar", "error");
      return navigate("/?pick=1", true);
    }
    state.guildId = gid;
    state.meta = null;
    localStorage.setItem("nova:last-guild", gid);
    setAccent(localStorage.getItem("nova:accent") || "#c8a45d");
    if (state.ctx.language) state.lang = state.ctx.language;
  }
  await shell(state.ctx);
  if (!PAGES[page]) return fill($(".content"), emptyState("Seite nicht gefunden", "🧭"));
  const navItem = NAV.find((n) => n[0] === page);
  if (navItem && navItem[3] === "admin" && state.ctx.level !== "admin") return fill($(".content"), emptyState("Nur für Admins", "🔒"));
  loadPage(page, parts.slice(3), PAGES[page]);
  connectWs(gid);
}

async function loadPage(name, params, loader) {
  const content = $(".content");
  fill(content, h("div.skeleton", { style: { height: "140px", marginBottom: "16px" } }), h("div.skeleton", { style: { height: "320px" } }));
  document.querySelectorAll(".nav a").forEach((a) => a.classList.toggle("active", a.dataset.page === name));
  $(".topbar h1").textContent = name === "owner" ? t("owner") : t(name);
  try {
    const mod = await loader();
    const fresh = h("div.content");
    content.replaceWith(fresh);
    await mod.render(fresh, params);
  } catch (e) {
    console.error(e);
    if (e && e.status) return;
    fill($(".content"), h("div.callout.err", "Seite konnte nicht geladen werden: " + (e.message || e)));
  }
}

// ── Layout ──
async function shell(ctx) {
  const existing = $(".layout");
  if (existing && existing.dataset.guild === String(ctx ? ctx.guild.id : "owner")) {
    updateNavDots();
    return;
  }
  const gid = ctx ? ctx.guild.id : null;
  const nav = h("nav.nav");
  if (ctx) {
    const current = location.pathname.split("/")[3] || "dashboard";
    let target = nav;
    for (const item of NAV) {
      if (item.length === 1) {
        const sec = item[0];
        if (!COLLAPSIBLE.has(sec)) { nav.append(h("div.nav-section", t(sec))); target = nav; continue; }
        // Einklappbarer Abschnitt: Zustand pro Browser merken, offen wenn die aktuelle Seite darin liegt
        const start = NAV.indexOf(item) + 1;
        const end = NAV.findIndex((x, i) => i >= start && x.length === 1);
        const pages = NAV.slice(start, end === -1 ? undefined : end).map((x) => x[0]);
        let open = pages.includes(current);
        try { open = open || localStorage.getItem(`nova:nav:${sec}`) === "1"; } catch {}
        const body = h("div", { hidden: !open });
        const arrow = h("span", { style: { float: "right" } }, open ? "▾" : "▸");
        const head = h("div.nav-section", { role: "button", tabindex: 0, style: { cursor: "pointer", userSelect: "none" } }, t(sec), arrow);
        const toggle = () => {
          body.hidden = !body.hidden;
          arrow.textContent = body.hidden ? "▸" : "▾";
          try { localStorage.setItem(`nova:nav:${sec}`, body.hidden ? "0" : "1"); } catch {}
        };
        head.addEventListener("click", toggle);
        head.addEventListener("keydown", (e) => (e.key === "Enter" || e.key === " ") && (e.preventDefault(), toggle()));
        nav.append(head, body);
        target = body;
        continue;
      }
      const [key, , module, level] = item;
      if (level === "admin" && ctx.level !== "admin") continue;
      target.append(h("a", { href: `/g/${gid}/${key === "dashboard" ? "" : key}`.replace(/\/$/, ""), "data-link": true, dataset: { page: key, module: module || "" } },
        icon(key), h("span", t(key)), module ? h("i.dot" + (ctx.modules[module] ? ".on" : "")) : null));
    }
  }
  if (state.me.is_owner) {
    nav.append(h("div.nav-section", "Owner"), h("a", { href: "/owner", "data-link": true, dataset: { page: "owner" } }, icon("owner"), h("span", t("owner"))));
  }
  const guildBtn = h("a.guild-switch", { href: "/?pick=1", "data-link": true, title: t("switch_server") },
    ctx?.guild.icon ? h("img.guild-icon", { src: ctx.guild.icon, alt: "" }) : h("span.guild-icon", (ctx?.guild.name || "N").slice(0, 2)),
    h("span.gname", ctx ? ctx.guild.name : "Owner"), h("span.muted", icon("updown")));
  const sidebar = h("aside.sidebar",
    h("div.brand", h("div.brand-logo"), h("div", h("b", state.me.bot_name), h("small", "Community Dashboard"))),
    guildBtn, nav,
    h("div.sidebar-foot", h("img", { src: state.me.user.avatar, alt: "" }),
      h("div.uname", state.me.user.name, h("small", ctx ? (ctx.level === "admin" ? "Admin" : "Staff") : "Owner")),
      h("button.btn.ghost.icon", { title: t("logout"), onclick: logout }, icon("logout"))));
  const pill = h("div.live-pill#live", h("i"), h("span", "Verbinde …"));
  const topbar = h("header.topbar",
    h("button.btn.ghost.icon.burger", { "aria-label": "Menü", onclick: () => $(".layout").classList.toggle("nav-open") }, icon("menu")),
    h("h1", ""), ctx ? searchBox(gid) : h("div", { style: { marginLeft: "auto" } }), pill);
  const layout = h("div.layout", { dataset: { guild: String(gid || "owner") } }, sidebar, h("div.overlay", { onclick: () => layout.classList.remove("nav-open") }),
    h("main.main", topbar, h("div.content")));
  fill($("#root"), layout);
  document.addEventListener("modules-changed", updateNavDots);
}

function updateNavDots() {
  if (!state.ctx) return;
  document.querySelectorAll(".nav a[data-module]").forEach((a) => {
    const m = a.dataset.module;
    if (m) a.querySelector(".dot")?.classList.toggle("on", !!state.ctx.modules[m]);
  });
}

async function logout() {
  await api("/auth/logout", { method: "POST" }).catch(() => {});
  localStorage.removeItem("nova:last-guild");
  location.href = "/";
}

// ── Globale Suche ──
function searchBox(gid) {
  const input = h("input", { type: "search", placeholder: t("search"), "aria-label": "Suche" });
  const box = h("div.glass.search-results", { hidden: true });
  const wrap = h("div.search-wrap", input, box);
  const go = (path) => { box.hidden = true; input.value = ""; navigate(path); };
  const run = debounce(async () => {
    const q = input.value.trim();
    if (q.length < 2) { box.hidden = true; return; }
    const r = await gapi(`/search?q=${encodeURIComponent(q)}`, { quiet: true }).catch(() => null);
    if (!r) return;
    clear(box);
    const grp = (label, items, fn) => { if (items.length) { box.append(h("div.grp", label)); items.forEach((i) => box.append(fn(i))); } };
    grp("User", r.users, (u) => h("a", { href: "#", onclick: (e) => { e.preventDefault(); go(`/g/${gid}/members/${u.id}`); } }, h("img", { src: u.avatar, alt: "" }), u.display || u.name, h("span.muted", u.name)));
    grp("Tickets", r.tickets, (x) => h("a", { href: "#", onclick: (e) => { e.preventDefault(); go(`/g/${gid}/tickets/${x.id}`); } }, `🎫 #${x.number}`, h("span.muted", x.subject)));
    grp("Cases", r.cases, (x) => h("a", { href: "#", onclick: (e) => { e.preventDefault(); go(`/g/${gid}/moderation?case=${x.number}`); } }, `🛡️ #${x.number} ${x.action}`, h("span.muted", x.user)));
    grp("Suggestions", r.suggestions, (x) => h("a", { href: "#", onclick: (e) => { e.preventDefault(); go(`/g/${gid}/suggestions`); } }, `💡 #${x.number}`, h("span.muted", x.content)));
    grp("Giveaways", r.giveaways, (x) => h("a", { href: "#", onclick: (e) => { e.preventDefault(); go(`/g/${gid}/giveaways`); } }, `🎁 ${x.prize}`, h("span.muted", x.ended ? "beendet" : "aktiv")));
    grp("Commands", r.commands, (x) => h("a", { href: "#", onclick: (e) => { e.preventDefault(); go(`/g/${gid}/settings/commands`); } }, `/${x.name}`, h("span.muted", x.custom ? "Custom" : x.description || "")));
    grp("Logs", r.logs, (x) => h("a", { href: "#", onclick: (e) => { e.preventDefault(); go(`/g/${gid}/logs?q=${encodeURIComponent(q)}`); } }, `📝 ${x.category}/${x.action}`, h("span.muted", x.content)));
    if (!box.childNodes.length) box.append(h("div.muted", { style: { padding: "12px" } }, "Keine Treffer"));
    box.hidden = false;
  }, 280);
  input.addEventListener("input", run);
  input.addEventListener("focus", () => input.value.length > 1 && (box.hidden = false));
  input.addEventListener("blur", () => setTimeout(() => (box.hidden = true), 180));
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); input.focus(); }
  });
  return wrap;
}

// ── Server-Auswahl ──
async function guildPicker() {
  state.ctx = null; state.guildId = null;
  fill($("#root"), h("div.boot", h("div.boot-logo"), h("p", t("loading"))));
  const data = await api("/api/guilds");
  const card = (g, invite) => h("a.glass.guild-card" + (invite ? ".invite" : ""), invite ? { href: `/invite?guild_id=${g.id}` } : { href: `/g/${g.id}`, "data-link": true },
    g.icon ? h("img", { src: g.icon, alt: "" }) : h("span.guild-icon", g.name.slice(0, 2)), h("b", g.name),
    invite ? h("span.pill.yellow", "Bot einladen") : h("span.muted", `${fmtNum(g.members)} Mitglieder · ${g.level === "admin" ? "Admin" : "Staff"}`));
  fill($("#root"), h("div.content", { style: { maxWidth: "1100px", paddingTop: "48px" } },
    h("div.page-head", h("div", h("h2", `Hallo ${state.me.user.name} 👋`), h("p", "Wähle einen Server, den du verwalten möchtest.")),
      h("div.actions", state.me.is_owner ? h("a.btn", { href: "/owner", "data-link": true }, "Owner Panel") : null, h("button.btn.ghost", { onclick: logout }, t("logout")))),
    data.manageable.length ? h("div.guild-grid", data.manageable.map((g) => card(g, false))) : h("div.glass.card", emptyState("Du hast auf keinem Server mit dem Bot Dashboard-Zugriff.", "🔒")),
    data.invitable.length ? [h("h3", { style: { margin: "32px 0 14px" } }, "Bot zu einem Server hinzufügen"), h("div.guild-grid", data.invitable.map((g) => card(g, true)))] : null));
}

// ── WebSocket (Live-Updates) ──
function connectWs(gid) {
  if (ws && wsGuild === gid && ws.readyState <= 1) return;
  if (ws) { ws.onclose = null; ws.close(); }
  wsGuild = gid;
  const url = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws?guild=${gid || 0}`;
  ws = new WebSocket(url);
  ws.onopen = () => { wsRetry = 0; };
  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.event === "status") updatePill(msg.data);
    state.wsHandlers.get(msg.event)?.forEach((fn) => { try { fn(msg.data); } catch (e) { console.error(e); } });
  };
  ws.onclose = (ev) => {
    updatePill(null);
    if ([4401, 4403].includes(ev.code)) return;
    setTimeout(() => wsGuild === gid && connectWs(gid), Math.min(30000, 1000 * 2 ** wsRetry++));
  };
}
export const connectOwnerWs = () => connectWs(0);

function updatePill(s) {
  const pill = $("#live");
  if (!pill) return;
  pill.className = "live-pill " + (s ? (s.online ? "on" : "off") : "off");
  pill.lastChild.textContent = s ? (s.online ? `${t("online")} · ${s.latency ?? "–"} ms` : t("offline")) : "Getrennt";
  pill.title = s ? `Uptime ${fmtDuration(s.uptime)} · RAM ${s.memory_mb} MB · CPU ${s.cpu}%` : "";
}

boot();
