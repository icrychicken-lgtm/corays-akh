import { h, clear, fill, gapi, state, table, pager, userCell, fmtNum, fmtDate, fmtRel, fmtMinutes, pill, debounce, progressBar, emptyState, openModal, toast, withLoading, confirmDialog } from "../core.js";
import { loadMeta, picker } from "../forms.js";
import { ACTION_COLOR } from "./home.js";

export async function render(root, params) {
  if (params[0]) return profile(root, params[0]);
  await loadMeta();
  let page = 1, q = "", role = "", sort = "joined";
  const list = h("div");
  const load = async () => {
    const d = await gapi(`/members?q=${encodeURIComponent(q)}&role=${role}&sort=${sort}&page=${page}&per_page=25`);
    fill(list, table([
      { label: "User", render: (m) => userCell(m, m.name) },
      { label: "Rolle", render: (m) => m.top_role ? h("span.chip", h("i.swatch", { style: { background: m.top_role_color || "#99a" } }), m.top_role) : h("span.muted", "—") },
      { label: "Level", render: (m) => `Lv. ${m.level}` }, { label: "Nachrichten", render: (m) => fmtNum(m.messages) },
      { label: "Beigetreten", render: (m) => fmtRel(m.joined_at) },
      { label: "Status", render: (m) => m.timed_out ? pill("Timeout", "blue") : m.bot ? pill("Bot", "purple") : pill(m.status, m.status === "offline" ? "" : "green") },
    ], d.items, { onRow: (m) => go(`/g/${state.guildId}/members/${m.id}`) }), pager(page, d.per_page, d.total, (p) => { page = p; load(); }));
  };
  const roleSel = h("select", h("option", { value: "" }, "Alle Rollen"), state.meta.roles.filter((r) => !r.default).map((r) => h("option", { value: r.id }, r.name)));
  roleSel.addEventListener("change", () => { role = roleSel.value; page = 1; load(); });
  const sortSel = h("select", h("option", { value: "joined" }, "Neueste zuerst"), h("option", { value: "name" }, "Name"), h("option", { value: "created" }, "Account-Alter"));
  sortSel.addEventListener("change", () => { sort = sortSel.value; load(); });
  const search = h("input", { type: "search", placeholder: "Name oder ID …" });
  search.addEventListener("input", debounce(() => { q = search.value.trim(); page = 1; load(); }));
  fill(root, h("div.page-head", h("div", h("h2", "👥 Mitglieder"), h("p", "Suche User, öffne Profile, Historie und Notizen."))),
    h("div.glass.card", h("div.toolbar", search, roleSel, sortSel), list));
  await load();
}

const go = (p) => { history.pushState({}, "", p); dispatchEvent(new PopStateEvent("popstate")); };

async function profile(root, uid) {
  await loadMeta();
  const d = await gapi(`/members/${uid}`);
  const u = d.user || { id: uid, name: uid };
  const s = d.stats;
  const admin = state.ctx.level === "admin";

  const act = (action) => {
    const reason = h("input", { type: "text", placeholder: "Grund (optional)" });
    const dur = h("input", { type: "text", placeholder: "z. B. 30m, 1d" });
    const needDur = action === "timeout" || action === "ban";
    const btn = h("button.btn." + (["kick", "ban", "softban"].includes(action) ? "danger" : "primary"), "Ausführen");
    const m = openModal({ title: `${action} · ${u.display || u.name}`, size: "sm", body: h("div.stack", h("div.field", h("label", "Grund"), reason),
      needDur ? h("div.field", h("label", action === "ban" ? "Dauer (leer = permanent)" : "Dauer"), dur) : null),
      footer: [h("button.btn.ghost", { onclick: () => m.close() }, "Abbrechen"), btn] });
    btn.addEventListener("click", () => withLoading(btn, async () => {
      const c = await gapi("/moderation/action", { method: "POST", body: { action, user_id: uid, reason: reason.value, duration: dur.value || null } });
      m.close(); toast(`Case #${c.case_number} erstellt`, "success"); profile(root, uid);
    }));
  };
  const note = h("textarea", { placeholder: "Interne Notiz für das Team …", style: { minHeight: "70px" } });
  const addNote = h("button.btn.primary.sm", "Notiz speichern");
  addNote.addEventListener("click", () => withLoading(addNote, async () => {
    if (!note.value.trim()) return;
    await gapi(`/members/${uid}/notes`, { method: "POST", body: { content: note.value } });
    toast("Notiz gespeichert", "success"); profile(root, uid);
  }));

  const ecoEdit = (field) => {
    const val = h("input", { type: "number", value: field === "xp" ? s.xp : s.coins });
    const mode = h("select", h("option", { value: "set" }, "Setzen auf"), h("option", { value: "add" }, "Hinzufügen / Abziehen"));
    const btn = h("button.btn.primary", "Speichern");
    const m = openModal({ title: field === "xp" ? "XP anpassen" : "Coins anpassen", size: "sm", body: h("div.stack", h("div.field", h("label", "Modus"), mode), h("div.field", h("label", "Wert"), val)),
      footer: [h("button.btn.ghost", { onclick: () => m.close() }, "Abbrechen"), btn] });
    btn.addEventListener("click", () => withLoading(btn, async () => {
      await gapi(`/members/${uid}/economy`, { method: "POST", body: { field, mode: mode.value, value: Number(val.value) } });
      m.close(); toast("Aktualisiert", "success"); profile(root, uid);
    }));
  };

  const roleAdd = picker({ options: state.meta.roles.filter((r) => r.editable).map((r) => ({ value: r.id, label: r.name, color: r.color })), placeholder: "Rolle hinzufügen …" });
  roleAdd.addEventListener("change", async () => {
    const rid = roleAdd.getValue(); if (!rid) return;
    await gapi(`/members/${uid}/roles`, { method: "POST", body: { role_id: rid, add: true } });
    toast("Rolle vergeben", "success"); profile(root, uid);
  });

  const badgeSel = h("select", h("option", { value: "" }, "Badge vergeben …"), d.all_badges.filter((b) => !d.manual_badges.includes(b.id)).map((b) => h("option", { value: b.id }, `${b.emoji} ${b.name}`)));
  badgeSel.addEventListener("change", async () => {
    if (!badgeSel.value) return;
    await gapi(`/members/${uid}/badges`, { method: "POST", body: { badge_id: badgeSel.value, grant: true } });
    toast("Badge vergeben", "success"); profile(root, uid);
  });

  fill(root, 
    h("div.page-head", h("div", h("a.btn.ghost.sm", { href: `/g/${state.guildId}/members`, "data-link": true }, "‹ Mitglieder"))),
    h("div.glass.hero", u.avatar ? h("img.avatar-lg", { src: u.avatar, alt: "" }) : null,
      h("div", h("h2", u.display || u.name), h("p", `${u.name} · `, h("span.mono", u.id)),
        h("div.row", { style: { marginTop: "8px" } }, d.in_guild ? pill("Auf dem Server", "green") : pill("Nicht mehr auf dem Server", "red"),
          d.timed_out_until ? pill(`Timeout bis ${fmtDate(d.timed_out_until)}`, "blue") : null, d.premium_since ? pill("💜 Booster", "purple") : null,
          d.warnings_active ? pill(`⚠️ ${d.warnings_active} aktive Verwarnungen`, "yellow") : null, d.afk ? pill(`💤 AFK: ${d.afk}`) : null)),
      h("div.spacer"),
      h("div.row", ...["warn", "timeout", ...(d.timed_out_until ? ["untimeout"] : []), "kick", "ban"].map((a) => h("button.btn.sm" + (["kick", "ban"].includes(a) ? ".danger" : ""), { onclick: () => act(a) }, a)))),
    h("div.grid.g4",
      h("div.glass.stat", h("div.ico", "⭐"), h("div.label", "Level · Rang"), h("div.value", `${s.level} · #${s.rank}`), progressBar(s.xp_into, s.xp_need), h("div.sub", `${fmtNum(s.xp)} XP`)),
      h("div.glass.stat", h("div.ico", "💰"), h("div.label", "Coins"), h("div.value", fmtNum(s.coins)), h("div.sub", `${fmtNum(s.coins_earned)} verdient · 🔥 ${s.daily_streak}`)),
      h("div.glass.stat", h("div.ico", "💬"), h("div.label", "Nachrichten"), h("div.value", fmtNum(s.messages)), h("div.sub", `${fmtNum(s.reactions)} Reaktionen`)),
      h("div.glass.stat", h("div.ico", "🎙️"), h("div.label", "Voice-Zeit"), h("div.value", fmtMinutes(s.voice_minutes)), h("div.sub", `${fmtNum(s.invites)} Einladungen`))),
    h("div.grid.g3", { style: { marginTop: "16px" } },
      h("div.glass.card", h("h3", "📋 Details"), h("dl.kv",
        h("dt", "Account erstellt"), h("dd", fmtDate(d.created_at, false)), h("dt", "Beigetreten"), h("dd", fmtDate(d.joined_at, false)),
        h("dt", "Erster Beitritt"), h("dd", fmtDate(d.first_joined_at, false)), h("dt", "Eingeladen von"), h("dd", d.invited_by ? h("span.mono", d.invited_by) : "—"),
        h("dt", "Giveaways gewonnen"), h("dd", s.giveaways_won), h("dt", "Events gewonnen"), h("dd", s.events_won), h("dt", "Stream-Check-ins"), h("dd", s.stream_checkins),
        h("dt", "Geburtstag"), h("dd", d.has_birthday ? "hinterlegt (privat)" : "—")),
        admin ? h("div.row", { style: { marginTop: "14px" } }, h("button.btn.sm", { onclick: () => ecoEdit("xp") }, "XP anpassen"), h("button.btn.sm", { onclick: () => ecoEdit("coins") }, "Coins anpassen")) : null),
      h("div.glass.card", h("h3", "🎭 Rollen"), h("div.chips", d.roles.map((r) => h("span.chip", h("i.swatch", { style: { background: r.color || "#99a" } }), r.name,
        admin && r.editable ? h("button.x", { onclick: async () => { await gapi(`/members/${uid}/roles`, { method: "POST", body: { role_id: r.id, add: false } }); toast("Rolle entfernt", "success"); profile(root, uid); } }, "×") : null))),
        admin && d.in_guild ? h("div", { style: { marginTop: "12px" } }, roleAdd) : null),
      h("div.glass.card", h("h3", "🎖️ Badges & Items"), d.badges.length ? h("div.chips", d.badges.map((b) => h("span.chip", `${b.emoji} ${b.name}`,
        admin && d.manual_badges.includes(String(b.id)) ? h("button.x", { onclick: async () => { await gapi(`/members/${uid}/badges`, { method: "POST", body: { badge_id: b.id, grant: false } }); profile(root, uid); } }, "×") : null))) : h("p.muted", "Keine Badges"),
        admin && d.all_badges.length ? h("div", { style: { marginTop: "10px" } }, badgeSel) : null,
        d.inventory.length ? h("div", { style: { marginTop: "12px" } }, h("div.muted", "Inventar"), h("div.chips", d.inventory.map((i) => h("span.chip", `${i.emoji} ${i.name} ×${i.quantity}`)))) : null,
        Object.keys(d.games).length ? h("div", { style: { marginTop: "12px" } }, h("div.muted", "🎮 Games"), h("div.chips", Object.entries(d.games).slice(0, 8).map(([g]) => h("span.chip", g)))) : null)),
    h("div.grid.g2", { style: { marginTop: "16px" } },
      h("div.glass.card", h("h3", "🛡️ Moderationshistorie ", h("span.muted", `· ${d.cases.length}`)),
        d.cases.length ? h("div.list", d.cases.map((c) => h("div.item", pill(c.action, ACTION_COLOR[c.action] || ""), h("div.grow", h("div.title", `#${c.case_number} · ${c.reason || "Kein Grund"}`),
          h("div.meta", `${c.moderator_name} · ${fmtDate(c.created_at)}${c.duration_seconds ? " · " + Math.round(c.duration_seconds / 60) + " Min." : ""}`)), c.active && ["warn", "timeout", "ban"].includes(c.action) ? pill("aktiv", "yellow") : null))) : h("p.muted", "Keine Einträge – sauberer Record ✨")),
      h("div.glass.card", h("h3", "🗒️ Interne Staff-Notizen"), h("div.stack", note, h("div.row", { style: { justifyContent: "flex-end" } }, addNote)),
        h("div", { style: { marginTop: "12px" } }, d.notes.map((n) => h("div.note", h("div.meta", h("b", n.author_name), fmtRel(n.created_at),
          h("button.btn.ghost.sm", { style: { marginLeft: "auto" }, onclick: async () => { if (await confirmDialog({ title: "Notiz löschen?" })) { await gapi(`/members/${uid}/notes/${n.id}`, { method: "DELETE" }); profile(root, uid); } } }, "🗑")), n.content))))),
    h("div.grid.g2", { style: { marginTop: "16px" } },
      h("div.glass.card", h("h3", "🎫 Tickets & Bewerbungen"), d.tickets.length || d.applications.length ? h("div.list",
        d.tickets.map((x) => h("a.item", { href: `/g/${state.guildId}/tickets/${x.id}`, "data-link": true, style: { color: "inherit", textDecoration: "none" } }, h("span", "🎫"), h("div.grow", h("div.title", `#${x.number} · ${x.subject}`), h("div.meta", fmtDate(x.created_at))), pill(x.status, x.status === "open" ? "green" : ""))),
        d.applications.map((a) => h("div.item", h("span", "📋"), h("div.grow", h("div.title", a.position || "Bewerbung"), h("div.meta", fmtDate(a.created_at))), pill(a.status)))) : h("p.muted", "Keine")),
      h("div.glass.card", h("h3", "🏆 Achievements"), h("div.list", d.achievements.map((a) => h("div.item", h("span", a.unlocked ? a.emoji : "🔒"),
        h("div.grow", h("div.title", a.key.replace(/_/g, " ")), progressBar(a.value, a.target)), h("span.muted", `${fmtNum(a.value)}/${fmtNum(a.target)}`)))))),
  );
}
