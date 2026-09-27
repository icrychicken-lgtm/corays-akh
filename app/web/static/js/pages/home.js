import { withLoading, confirmDialog, toast, openModal } from "../core.js";
import { h, clear, fill, gapi, state, statCard, fmtNum, fmtRel, fmtDuration, lineChart, table, pill, onLive, userCell, emptyState, chart, PALETTE } from "../core.js";

export const ACTION_COLOR = { warn: "yellow", timeout: "blue", untimeout: "green", kick: "red", ban: "red", softban: "red", unban: "green" };

// Einrichtungs-Checkliste mit Auto-Setup – verschwindet, sobald alles erledigt ist
function setupCard() {
  const card = h("div");
  gapi("/setup/status", { quiet: true }).then((st) => {
    const items = [
      ["welcome", "Willkommens-Channel"], ["streamer", "Stream-Alert-Channel"], ["tickets", "Ticket-Panel"], ["logs", "Mod-Log"],
      ["streamer_connected", "Streamer verbunden"], ["bot_role_top", "Bot-Rolle ganz oben"],
    ];
    const done = items.filter(([k]) => st[k]).length;
    if (done === items.length) return;
    const admin = state.ctx.level === "admin";
    const btn = h("button.btn.primary", "Auto-Setup starten");
    btn.addEventListener("click", () => withLoading(btn, async () => {
      if (!(await confirmDialog({ title: "Server automatisch einrichten?", danger: false, confirmText: "Einrichten",
        text: "Erstellt Channels (#willkommen, #stream-alerts, #clips, #level-ups, #tickets, Staff-Bereich), Rollen (Stream-Ping, Stammzuschauer …) und verbindet alle Module. Bestehende Channels mit gleichem Namen werden wiederverwendet, nichts wird gelöscht." }))) return;
      const r = await gapi("/setup/auto", { method: "POST" });
      toast("Server eingerichtet", "success");
      openModal({ title: "Fertig eingerichtet", body: h("div.list", r.summary.filter(Boolean).map((l) => h("div.item", l))) });
      state.meta = null;
      fill(card);
    }));
    fill(card, h("div.glass.card.callout.gold", { style: { padding: "20px 22px" } },
      h("div.row.between", h("div", h("h3", { style: { margin: "0 0 4px" } }, `Einrichtung · ${done}/${items.length} erledigt`),
        h("div.muted", "Mit einem Klick richtet der Bot Channels, Rollen, Tickets und Stream-Alerts ein.")), admin ? btn : null),
      h("div.chips", { style: { marginTop: "14px" } }, items.map(([k, l]) => h("span.chip" + (st[k] ? ".toggle.on" : ""), `${st[k] ? "✓" : "○"}  ${l}`)))));
  }).catch(() => {});
  return card;
}

export async function render(root) {
  const d = await gapi("/overview");
  const s = d.stats;
  const g = d.guild;
  const stats = [
    ["members", "Mitglieder", "👥", s.members, `${fmtNum(s.bots)} Bots`], ["online", "Online", "🟢", s.online ?? "—", s.online === null ? "Presence-Intent deaktiviert" : "gerade online"],
    ["voice", "Im Voice", "🎙️", s.voice_now, "gerade aktiv"], ["boosts", "Boosts", "🚀", s.boosts, `Level ${s.boost_level}`],
    ["messages", "Nachrichten heute", "💬", s.messages_today, `${fmtNum(s.commands_today)} Commands`], ["joins", "Neue Mitglieder heute", "📈", s.joins_today, `${fmtNum(s.leaves_today)} Leaves`],
    ["tickets", "Offene Tickets", "🎫", s.open_tickets, `${fmtNum(s.open_applications)} Bewerbungen offen`], ["giveaways", "Aktive Giveaways", "🎁", s.active_giveaways, `${fmtNum(s.channels)} Channels · ${fmtNum(s.roles)} Rollen`],
  ];
  const statEls = {};
  const statGrid = h("div.grid.g4", stats.map(([k, label, icon, value, sub]) => (statEls[k] = statCard(label, typeof value === "number" ? fmtNum(value) : value, icon, sub))));

  const botCard = h("div.glass.card");
  const drawBot = (b) => {
    fill(botCard, h("h3", "🤖 Bot-Status ", pill(b.online ? "Online" : "Offline", b.online ? "green" : "red"), b.maintenance ? pill("Wartung", "yellow") : null),
      h("dl.kv", h("dt", "Uptime"), h("dd", fmtDuration(b.uptime)), h("dt", "Latenz"), h("dd", `${b.latency ?? "—"} ms`),
        h("dt", "Server"), h("dd", fmtNum(b.guilds)), h("dt", "User"), h("dd", fmtNum(b.users)), h("dt", "RAM"), h("dd", `${b.memory_mb} MB`),
        h("dt", "CPU"), h("dd", `${b.cpu}%`), h("dt", "Shards"), h("dd", b.shards)));
  };
  drawBot(d.bot);

  const casesCard = h("div.glass.card", h("div.card-head", h("h3", "🛡️ Aktuelle Moderationsaktionen"), h("a.btn.sm", { href: `/g/${state.guildId}/moderation`, "data-link": true }, "Alle")));
  const casesList = h("div.list");
  const addCase = (c, top = false) => {
    const item = h("div.item", pill(c.action, ACTION_COLOR[c.action] || ""), h("div.grow", h("div.title", `#${c.case_number} · ${c.user_name}`),
      h("div.meta", `${c.reason || "Kein Grund"} · von ${c.moderator_name}`)), h("span.muted", fmtRel(c.created_at)));
    top ? casesList.prepend(item) : casesList.append(item);
    while (casesList.children.length > 8) casesList.lastChild.remove();
  };
  d.recent_cases.forEach((c) => addCase(c));
  casesCard.append(d.recent_cases.length ? casesList : emptyState("Keine Moderationsaktionen – alles ruhig 🌙", "🛡️"));

  const msgCanvas = h("canvas"), growthCanvas = h("canvas"), joinCanvas = h("canvas"), cmdCanvas = h("canvas");
  fill(root, 
    h("div.glass.hero", g.icon ? h("img", { src: g.icon, alt: "" }) : h("span.guild-icon", g.name.slice(0, 2)),
      h("div", h("h2", g.name), h("p", `${fmtNum(s.members)} Mitglieder · ${fmtNum(s.channels)} Channels · Boost-Level ${s.boost_level}`)),
      h("div.spacer"), h("a.btn", { href: `/g/${state.guildId}/analytics`, "data-link": true }, "📊 Analytics")),
    setupCard(),
    statGrid,
    h("div.grid.g3", { style: { marginTop: "16px" } },
      h("div.glass.card.span2", h("h3", "💬 Nachrichten pro Tag ", h("span.muted", "· 30 Tage")), h("div.chart-box", msgCanvas)), botCard),
    h("div.grid.g3", { style: { marginTop: "16px" } },
      h("div.glass.card", h("h3", "📈 User-Wachstum"), h("div.chart-box.sm", growthCanvas)),
      h("div.glass.card", h("h3", "👋 Joins & Leaves"), h("div.chart-box.sm", joinCanvas)),
      h("div.glass.card", h("h3", "⌨️ Command-Nutzung ", h("span.muted", "· 7 Tage")), d.top_commands.length ? h("div.chart-box.sm", cmdCanvas) : emptyState("Noch keine Commands genutzt", "⌨️"))),
    h("div.grid.g2", { style: { marginTop: "16px" } }, casesCard,
      h("div.glass.card", h("h3", "⚡ Live-Aktivität"), h("div.list#live-feed", emptyState("Warte auf Ereignisse …", "📡")))),
  );
  const c = d.charts;
  lineChart(msgCanvas, c.labels, [{ label: "Nachrichten", data: c.series.messages }], { bar: true });
  lineChart(growthCanvas, c.labels, [{ label: "Mitglieder", data: c.series.members, color: "#5fb37a" }]);
  lineChart(joinCanvas, c.labels, [{ label: "Joins", data: c.series.joins, color: "#5fb37a" }, { label: "Leaves", data: c.series.leaves, color: "#d25a5a" }], { bar: true });
  if (d.top_commands.length) chart(cmdCanvas, { type: "doughnut", data: { labels: d.top_commands.map((x) => "/" + x.name),
    datasets: [{ data: d.top_commands.map((x) => x.count), backgroundColor: PALETTE, borderWidth: 0 }] },
    options: { responsive: true, maintainAspectRatio: false, cutout: "68%", plugins: { legend: { position: "right", labels: { boxWidth: 10 } } } } });

  // ── Live ──
  const feed = root.querySelector("#live-feed");
  const push = (icon, text) => {
    if (feed.querySelector(".empty")) clear(feed);
    feed.prepend(h("div.item", h("span", icon), h("div.grow", h("div.title", text)), h("span.muted", "jetzt")));
    while (feed.children.length > 8) feed.lastChild.remove();
  };
  const bump = (k, value) => { const el = statEls[k]; if (!el) return; el.querySelector(".value").textContent = fmtNum(value); el.classList.remove("flash"); void el.offsetWidth; el.classList.add("flash"); };
  onLive("status", (st) => {
    drawBot(st);
    if (st.guild) { bump("members", st.guild.members); if (st.guild.online !== null) bump("online", st.guild.online); bump("voice", st.guild.voice_now); bump("boosts", st.guild.boosts); }
  });
  onLive("member_join", (m) => { push("👋", `${m.name} ist beigetreten`); bump("members", m.count); });
  onLive("member_leave", (m) => { push("🚪", `${m.name} hat den Server verlassen`); bump("members", m.count); });
  onLive("mod_action", (cs) => { addCase(cs, true); push("🛡️", `${cs.action} · ${cs.user_name}`); });
  onLive("ticket", (x) => x.action === "open" && push("🎫", `Ticket #${x.number} geöffnet`));
  onLive("level", (x) => push("⭐", `Level-Up auf Level ${x.level}`));
  onLive("stream", (x) => x.action === "live" && push("🔴", `${x.name} ist live: ${x.title}`));
  onLive("giveaway", (x) => x.action === "start" && push("🎁", `Giveaway gestartet: ${x.prize}`));
  onLive("raid", (x) => x.active && push("🚨", `Raid erkannt! ${x.joins} Joins`));
}
