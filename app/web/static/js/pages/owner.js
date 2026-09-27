import { h, clear, fill, api, state, fmtNum, fmtDate, fmtRel, fmtDuration, tabs, pill, table, openModal, toast, withLoading, emptyState, confirmDialog, statCard, userCell, debounce, onLive, chart, PALETTE } from "../core.js";
import { connectOwnerWs } from "../app.js";

const oapi = (p, o) => api(`/api/owner${p}`, o);

export async function render(root) {
  connectOwnerWs();
  const body = h("div");
  const show = (k) => ({ overview, guilds, errors, logs, commands, backups })[k](body);
  fill(root, h("div.page-head", h("div", h("h2", "👑 Owner Panel"), h("p", "Globaler Überblick über alle Server, Fehler, Systemstatus und Wartung."))),
    tabs([["overview", "📊 Übersicht"], ["guilds", "🌐 Server"], ["errors", "🐞 Fehler"], ["logs", "📜 Globale Logs"], ["commands", "⌨️ Commands"], ["backups", "💾 DB-Backups"]], "overview", show), body);
  show("overview");
}

async function overview(el) {
  const d = await oapi("/overview");
  const b = d.bot;
  const maint = h("input", { type: "checkbox", checked: b.maintenance });
  maint.addEventListener("change", async () => {
    try { await oapi("/maintenance", { method: "POST", body: { enabled: maint.checked } }); toast(maint.checked ? "Wartungsmodus aktiv" : "Wartungsmodus beendet", "warning"); }
    catch { maint.checked = !maint.checked; }
  });
  const restart = h("button.btn.danger", "🔄 Bot neustarten");
  restart.addEventListener("click", async () => {
    if (!(await confirmDialog({ title: "Bot neustarten?", text: "Der Prozess wird beendet und von Docker/Start-Skript neu gestartet. Das Dashboard ist kurz nicht erreichbar.", typeToConfirm: "RESTART" }))) return;
    await oapi("/restart", { method: "POST" }); toast("Neustart ausgelöst …", "warning");
  });
  const sync = h("button.btn", "⌨️ Commands neu synchronisieren");
  sync.addEventListener("click", () => withLoading(sync, async () => { await oapi("/sync-commands", { method: "POST" }); toast("Synchronisiert", "success"); }));
  const annTitle = h("input", { type: "text", placeholder: "Titel", maxlength: 200 });
  const annMsg = h("textarea", { placeholder: "Nachricht an alle Server …" });
  const annBtn = h("button.btn.primary", "📢 An alle Server senden");
  annBtn.addEventListener("click", async () => {
    if (!annTitle.value.trim() || !annMsg.value.trim()) return toast("Titel und Nachricht erforderlich", "warning");
    if (!(await confirmDialog({ title: "Globale Ankündigung senden?", text: `Wird an ${b.guilds} Server gesendet.`, danger: false }))) return;
    withLoading(annBtn, async () => { const r = await oapi("/announce", { method: "POST", body: { title: annTitle.value, message: annMsg.value } }); toast(`Gesendet: ${r.sent} · Fehlgeschlagen: ${r.failed}`, "success"); });
  });
  const apiBox = h("div.glass.card", h("h3", "🌐 API-Status"), h("div.skeleton", { style: { height: "120px" } }));
  const statBoxes = {};
  fill(el, 
    h("div.grid.g4", ...[["guilds", "Server", "🌐", fmtNum(b.guilds)], ["users", "User", "👥", fmtNum(b.users)], ["latency", "Latenz", "📶", `${b.latency ?? "—"} ms`], ["uptime", "Uptime", "⏱️", fmtDuration(b.uptime)],
      ["mem", "RAM (Bot)", "🧠", `${b.memory_mb} MB`], ["cpu", "CPU (Bot)", "⚙️", `${b.cpu}%`], ["shards", "Shards", "🧩", b.shards], ["errors", "Offene Fehler", "🐞", fmtNum(d.errors_open)]]
      .map(([k, l, i, v]) => (statBoxes[k] = statCard(l, v, i)))),
    h("div.grid.g3", { style: { marginTop: "16px" } },
      h("div.glass.card", h("h3", "🗄️ System"), h("dl.kv", h("dt", "Datenbank"), h("dd", pill(d.database.ok ? `${d.database.dialect} · ${d.database.ping_ms} ms` : "Fehler", d.database.ok ? "green" : "red")),
        h("dt", "DB-Version"), h("dd", { style: { fontSize: "12px" } }, d.database.version), h("dt", "Cache"), h("dd", pill(`${d.cache.backend}${d.cache.ok ? "" : " (Fehler)"}`, d.cache.ok ? "green" : "red")),
        h("dt", "Python"), h("dd", d.python), h("dt", "PID"), h("dd", d.pid), h("dt", "Profile"), h("dd", fmtNum(d.profiles)), h("dt", "Streamer"), h("dd", fmtNum(d.streamers)),
        h("dt", "System-CPU / RAM"), h("dd", `${b.system_cpu}% / ${b.system_mem}%`)),
        d.shards.length ? h("div", { style: { marginTop: "12px" } }, h("div.muted", "Shards"), h("div.chips", d.shards.map((s) => h("span.chip", `#${s.id} · ${s.latency ?? "—"} ms`, s.closed ? " ⛔" : " ✅")))) : null),
      apiBox,
      h("div.glass.card", h("h3", "🛠️ Steuerung"), h("div.stack",
        h("label.switch", maint, h("span.track"), h("span", "Wartungsmodus (nur Owner können Commands nutzen)")), sync, restart))),
    h("div.glass.card", { style: { marginTop: "16px" } }, h("h3", "📢 Globale Ankündigung"), h("div.stack", annTitle, annMsg, h("div.row", { style: { justifyContent: "flex-end" } }, annBtn))));
  oapi("/api-status").then((r) => {
    fill(apiBox, h("h3", "🌐 API-Status"), h("div.list", Object.entries(r).map(([k, v]) => h("div.item", h("b", { style: { width: "72px", flex: "none" } }, k),
      h("div.row", { style: { flex: 1, gap: "6px" } }, v.reachable ? pill(`erreichbar · ${v.ms} ms`, "green") : pill(v.error || `Status ${v.status}`, "red"),
        v.global_credentials !== undefined ? pill(v.global_credentials ? "globale Keys ✓" : "keine globalen Keys", v.global_credentials ? "purple" : "") : null)))));
  }).catch(() => {});
  onLive("status", (s) => {
    const set = (k, v) => statBoxes[k] && (statBoxes[k].querySelector(".value").textContent = v);
    set("guilds", fmtNum(s.guilds)); set("users", fmtNum(s.users)); set("latency", `${s.latency ?? "—"} ms`); set("uptime", fmtDuration(s.uptime));
    set("mem", `${s.memory_mb} MB`); set("cpu", `${s.cpu}%`);
  });
}

async function guilds(el) {
  let q = "";
  const box = h("div");
  const load = async () => {
    const d = await oapi(`/guilds?q=${encodeURIComponent(q)}`);
    fill(box, table([
      { label: "Server", render: (g) => h("div.user-cell", g.icon ? h("img", { src: g.icon, alt: "" }) : h("span.guild-icon", g.name.slice(0, 2)), h("span", g.name, h("small.mono", g.id))) },
      { label: "Mitglieder", render: (g) => fmtNum(g.members) }, { label: "Owner", render: (g) => userCell(g.owner) }, { label: "Boosts", key: "boosts" },
      { label: "Shard", key: "shard" }, { label: "Bot seit", render: (g) => fmtRel(g.joined_at) },
      { label: "", cls: "actions", render: (g) => h("div.row", { style: { justifyContent: "flex-end", gap: "6px" } }, h("a.btn.sm", { href: `/g/${g.id}`, "data-link": true }, "Öffnen"),
        h("button.btn.sm.danger", { onclick: async () => {
          if (!(await confirmDialog({ title: `Server „${g.name}“ verlassen?`, text: "Der Bot verlässt den Server. Daten bleiben erhalten.", typeToConfirm: g.name }))) return;
          await oapi(`/guilds/${g.id}/leave`, { method: "POST", body: { confirm: g.name } }); toast("Server verlassen", "success"); load();
        } }, "Verlassen")) },
    ], d.items, { empty: "Keine Server" }));
  };
  const search = h("input", { type: "search", placeholder: "Server suchen (Name oder ID) …" });
  search.addEventListener("input", debounce(() => { q = search.value; load(); }));
  fill(el, h("div.glass.card", h("div.toolbar", search), box));
  await load();
}

async function errors(el) {
  let resolved = false, q = "";
  const box = h("div");
  const load = async () => {
    const d = await oapi(`/errors?resolved=${resolved}&q=${encodeURIComponent(q)}`);
    fill(box, table([
      { label: "ID", render: (e) => h("b.mono", e.id) }, { label: "Command", render: (e) => h("span.mono", e.command || "—") },
      { label: "Fehler", render: (e) => h("div", h("b", e.error_type), h("div.muted", (e.message || "").slice(0, 90))) },
      { label: "Server", render: (e) => e.guild_name || (e.guild_id ? h("span.mono", e.guild_id) : "—") }, { label: "User", render: (e) => e.user_id ? h("span.mono", e.user_id) : "—" },
      { label: "Zeit", render: (e) => fmtRel(e.created_at) },
      { label: "", cls: "actions", render: (e) => h("button.btn.sm" + (e.resolved ? "" : ".success"), { onclick: async () => {
        await oapi(`/errors/${e.id}/resolve`, { method: "POST", body: { resolved: !e.resolved } }); load(); } }, e.resolved ? "Wieder öffnen" : "✔ Gelöst") },
    ], d.items, { onRow: (e) => openModal({ title: `Fehler ${e.id} · ${e.error_type}`, size: "lg", body: h("div.stack",
      h("dl.kv", h("dt", "Command"), h("dd", e.command || "—"), h("dt", "Server"), h("dd", e.guild_name || e.guild_id || "—"), h("dt", "User"), h("dd", e.user_id || "—"), h("dt", "Zeit"), h("dd", fmtDate(e.created_at))),
      h("pre.code", e.traceback)) }), empty: "Keine Fehler 🎉" }));
  };
  const sel = h("select", h("option", { value: "false" }, "Offen"), h("option", { value: "true" }, "Gelöst"));
  sel.addEventListener("change", () => { resolved = sel.value === "true"; load(); });
  const search = h("input", { type: "search", placeholder: "Error-ID, Command, Meldung …" });
  search.addEventListener("input", debounce(() => { q = search.value; load(); }));
  fill(el, h("div.glass.card", h("div.toolbar", search, sel), box));
  onLive("error", () => !resolved && load());
  await load();
}

async function logs(el) {
  let q = "";
  const box = h("div");
  const load = async () => {
    const d = await oapi(`/logs?q=${encodeURIComponent(q)}`);
    fill(box, h("div.grid.g2",
      h("div.glass.card", h("h3", "📜 Audit (alle Server)"), d.audit.length ? h("div.list", d.audit.map((a) => h("div.item", h("div.grow", h("div.title", h("b", a.actor_name), " → ", a.action),
        h("div.meta", `${a.guild_name || "global"} · ${a.target || "—"}`)), h("span.muted", fmtRel(a.created_at))))) : emptyState("Keine Einträge")),
      h("div.glass.card", h("h3", "📝 Letzte Ereignisse"), d.events.length ? h("div.list", d.events.map((l) => h("div.item", h("div.grow", h("div.title", pill(l.category), " ", l.action, l.user_name ? ` · ${l.user_name}` : ""),
        h("div.meta", `${l.guild_name || l.guild_id} · ${(l.content || "").slice(0, 80)}`)), h("span.muted", fmtRel(l.created_at))))) : emptyState("Keine Einträge"))));
  };
  const search = h("input", { type: "search", placeholder: "Aktion, Person, Ziel …" });
  search.addEventListener("input", debounce(() => { q = search.value; load(); }));
  fill(el, h("div.toolbar", search), box);
  await load();
}

async function commands(el) {
  const d = await oapi("/commands?days=30");
  const canvas = h("canvas");
  fill(el, h("div.grid.g2",
    h("div.glass.card", h("h3", "⌨️ Nutzung (30 Tage, alle Server)"), d.usage.length ? h("div.chart-box", canvas) : emptyState("Noch keine Nutzung")),
    h("div.glass.card", h("h3", `📋 Registrierte Commands · ${d.registered.length}`), h("div.chips", d.registered.map((c) => h("span.chip.mono", "/" + c))))));
  if (d.usage.length) chart(canvas, { type: "bar", data: { labels: d.usage.slice(0, 15).map((u) => "/" + u.name), datasets: [{ data: d.usage.slice(0, 15).map((u) => u.count), backgroundColor: PALETTE[0] + "cc", borderRadius: 6 }] },
    options: { indexAxis: "y", responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { y: { grid: { display: false } } } } });
}

async function backups(el) {
  const d = await oapi("/backups");
  const btn = h("button.btn.primary", "💾 Vollständiges DB-Backup erstellen");
  btn.addEventListener("click", () => withLoading(btn, async () => { await oapi("/backups", { method: "POST" }); toast("Backup erstellt", "success"); backups(el); }));
  fill(el, h("div.callout", "Vollbackups nutzen pg_dump (PostgreSQL) bzw. eine SQLite-Kopie. Wiederherstellung: siehe README (scripts/restore.sh)."),
    h("div.glass.card", h("div.card-head", h("h3", "💾 Datenbank-Backups"), btn), table([
      { label: "Erstellt", render: (b) => fmtDate(b.created_at) }, { label: "Datei", render: (b) => h("span.mono", b.filename) },
      { label: "Größe", render: (b) => `${(b.size / 1024 / 1024).toFixed(2)} MB` }, { label: "Typ", render: (b) => pill(b.auto ? "automatisch" : "manuell") },
      { label: "", cls: "actions", render: (b) => h("a.btn.sm", { href: `/api/owner/backups/${b.id}/download` }, "⬇ Download") },
    ], d.items, { empty: "Noch keine Vollbackups" })));
}
