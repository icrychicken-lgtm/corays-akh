import { h, clear, fill, gapi, state, table, pager, userCell, fmtNum, fmtDate, fmtRel, fmtDuration, pill, debounce, tabs, openModal, toast, withLoading, emptyState, confirmDialog, onLive, statCard } from "../core.js";
import { moduleSettings, picker, loadMeta } from "../forms.js";
import { ACTION_COLOR } from "./home.js";

export async function render(root) {
  const admin = state.ctx.level === "admin";
  const body = h("div");
  const items = [["cases", "📁 Cases"], ["active", "⏳ Aktive Strafen"], ["action", "⚡ Aktion ausführen"], ["staff", "👮 Staff"], ["raids", "🚨 Raids"]];
  if (admin) items.push(["audit", "📜 Audit-Log"], ["settings", "⚙️ Einstellungen"]);
  const show = (k) => ({ cases, active, action, staff, raids, audit, settings: (el) => moduleSettings(el, "moderation") })[k](body);
  const initial = new URLSearchParams(location.search).get("case") ? "cases" : "cases";
  fill(root, h("div.page-head", h("div", h("h2", "🛡️ Moderation"), h("p", "Cases, Verwarnungen, Timeouts, Staff-Aktivität und Audit-Log."))), tabs(items, initial, show), body);
  show(initial);
}

function caseModal(c, reload) {
  const reason = h("textarea"); reason.value = c.reason || "";
  const active = h("input", { type: "checkbox", checked: c.active });
  const save = h("button.btn.primary", "Speichern");
  const m = openModal({ title: `Case #${c.case_number}`, body: h("div.stack",
    h("dl.kv", h("dt", "User"), h("dd", `${c.user_name} (${c.user_id})`), h("dt", "Moderator"), h("dd", c.moderator_name), h("dt", "Aktion"), h("dd", pill(c.action, ACTION_COLOR[c.action])),
      h("dt", "Dauer"), h("dd", c.duration_seconds ? fmtDuration(c.duration_seconds) : "—"), h("dt", "Läuft ab"), h("dd", c.expires_at ? fmtDate(c.expires_at) : "—"),
      h("dt", "Quelle"), h("dd", c.source), h("dt", "Datum"), h("dd", fmtDate(c.created_at))),
    h("div.field", h("label", "Grund"), reason),
    ["warn", "timeout", "ban"].includes(c.action) ? h("label.switch", active, h("span.track"), h("span", "Aktiv (zählt für Eskalation / Temp-Ban)")) : null),
    footer: [h("a.btn.ghost", { href: `/g/${state.guildId}/members/${c.user_id}`, "data-link": true, onclick: () => m.close() }, "Profil öffnen"), save] });
  save.addEventListener("click", () => withLoading(save, async () => {
    await gapi(`/cases/${c.case_number}`, { method: "PATCH", body: { reason: reason.value, active: active.checked } });
    toast("Case aktualisiert", "success"); m.close(); reload();
  }));
}

async function cases(el) {
  let page = 1, q = new URLSearchParams(location.search).get("case") || "", action = "";
  const list = h("div");
  const load = async () => {
    const d = await gapi(`/cases?q=${encodeURIComponent(q)}&action=${action}&page=${page}`);
    fill(list, table([
      { label: "#", render: (c) => h("b", "#" + c.case_number) }, { label: "Aktion", render: (c) => pill(c.action, ACTION_COLOR[c.action] || "") },
      { label: "User", render: (c) => h("div", c.user_name, h("div.muted.mono", c.user_id)) }, { label: "Moderator", key: "moderator_name" },
      { label: "Grund", render: (c) => (c.reason || "—").slice(0, 80) }, { label: "Datum", render: (c) => fmtRel(c.created_at) },
      { label: "Status", render: (c) => ["warn", "timeout", "ban"].includes(c.action) ? pill(c.active ? "aktiv" : "inaktiv", c.active ? "yellow" : "") : "" },
    ], d.items, { onRow: (c) => caseModal(c, load), empty: "Noch keine Cases" }), pager(page, d.per_page, d.total, (p) => { page = p; load(); }));
  };
  const search = h("input", { type: "search", placeholder: "Case-Nr., User, Grund …", value: q });
  search.addEventListener("input", debounce(() => { q = search.value; page = 1; load(); }));
  const sel = h("select", h("option", { value: "" }, "Alle Aktionen"), ["warn", "timeout", "untimeout", "kick", "ban", "unban", "softban"].map((a) => h("option", { value: a }, a)));
  sel.addEventListener("change", () => { action = sel.value; page = 1; load(); });
  fill(el, h("div.glass.card", h("div.toolbar", search, sel), list));
  onLive("mod_action", () => page === 1 && load());
  await load();
}

async function active(el) {
  const d = await gapi("/moderation/active");
  const untimeout = async (u) => {
    if (!(await confirmDialog({ title: "Timeout aufheben?", text: u.name, danger: false }))) return;
    await gapi("/moderation/action", { method: "POST", body: { action: "untimeout", user_id: u.id, reason: "Über Dashboard aufgehoben" } });
    toast("Timeout aufgehoben", "success"); active(el);
  };
  const unban = async (c) => {
    if (!(await confirmDialog({ title: "Entbannen?", text: c.user_name, danger: false }))) return;
    await gapi("/moderation/action", { method: "POST", body: { action: "unban", user_id: c.user_id, reason: "Über Dashboard entbannt" } });
    toast("Entbannt", "success"); active(el);
  };
  fill(el, h("div.grid.g3",
    statCard("Aktive Timeouts", fmtNum(d.timeouts.length), "⏳"), statCard("Aktive Bans (Cases)", fmtNum(d.bans.length), "🔨"), statCard("User mit Verwarnungen", fmtNum(d.warnings.length), "⚠️")),
    h("div.grid.g2", { style: { marginTop: "16px" } },
      h("div.glass.card", h("h3", "⏳ Timeouts"), table([{ label: "User", render: (u) => userCell(u) }, { label: "Bis", render: (u) => fmtDate(u.until) },
        { label: "", cls: "actions", render: (u) => h("button.btn.sm", { onclick: () => untimeout(u) }, "Aufheben") }], d.timeouts, { empty: "Keine aktiven Timeouts" })),
      h("div.glass.card", h("h3", "⚠️ Verwarnungen"), table([{ label: "User", key: "user_name" }, { label: "Aktiv", render: (w) => pill(`${w.count}×`, w.count >= 3 ? "red" : "yellow") },
        { label: "", cls: "actions", render: (w) => h("a.btn.sm", { href: `/g/${state.guildId}/members/${w.user_id}`, "data-link": true }, "Profil") }], d.warnings, { empty: "Keine aktiven Verwarnungen" }))),
    h("div.glass.card", { style: { marginTop: "16px" } }, h("h3", "🔨 Bans"), table([{ label: "#", render: (c) => "#" + c.case_number }, { label: "User", key: "user_name" },
      { label: "Grund", render: (c) => c.reason || "—" }, { label: "Läuft ab", render: (c) => c.expires_at ? fmtDate(c.expires_at) : pill("permanent", "red") },
      { label: "", cls: "actions", render: (c) => h("button.btn.sm", { onclick: () => unban(c) }, "Entbannen") }], d.bans, { empty: "Keine aktiven Bans" })));
}

async function action(el) {
  await loadMeta();
  const user = picker({ options: [], placeholder: "User suchen (Name oder ID) …", remote: async (q) => {
    if (/^\d{15,21}$/.test(q)) return [{ value: q, label: `ID ${q}` }];
    if (q.length < 2) return [];
    const r = await gapi(`/members?q=${encodeURIComponent(q)}&per_page=15`, { quiet: true });
    return r.items.map((m) => ({ value: m.id, label: m.display, sub: m.name }));
  } });
  const act = h("select", ["warn", "timeout", "untimeout", "kick", "ban", "unban", "softban"].map((a) => h("option", { value: a }, a)));
  const reason = h("textarea", { placeholder: "Grund" });
  const duration = h("input", { type: "text", placeholder: "z. B. 30m, 2h, 7d (Timeout / Temp-Ban)" });
  const del = h("select", [0, 1, 2, 3, 7].map((d) => h("option", { value: d }, d ? `Nachrichten der letzten ${d} Tage löschen` : "Keine Nachrichten löschen")));
  const btn = h("button.btn.primary", "⚡ Aktion ausführen");
  btn.addEventListener("click", async () => {
    const uid = user.getValue();
    if (!uid) return toast("Bitte User wählen", "warning");
    if (["kick", "ban", "softban"].includes(act.value) && !(await confirmDialog({ title: `${act.value} ausführen?`, text: "Diese Aktion wird sofort auf Discord ausgeführt." }))) return;
    withLoading(btn, async () => {
      const c = await gapi("/moderation/action", { method: "POST", body: { action: act.value, user_id: uid, reason: reason.value, duration: duration.value || null, delete_days: Number(del.value) } });
      toast(`Case #${c.case_number} erstellt`, "success");
      reason.value = ""; duration.value = "";
    });
  });
  fill(el, h("div.glass.card", { style: { maxWidth: "720px" } }, h("h3", "⚡ Moderationsaktion"),
    h("div.callout", "Aktionen werden mit deinem Discord-Account als Moderator protokolliert. Hierarchie und Rechte werden serverseitig geprüft."),
    h("div.form-grid", h("div.field.full", h("label", "User"), user), h("div.field", h("label", "Aktion"), act), h("div.field", h("label", "Dauer"), duration),
      h("div.field.full", h("label", "Grund"), reason), h("div.field.full", h("label", "Bei Ban/Softban"), del)),
    h("div.form-actions", btn)));
}

async function staff(el) {
  let days = 30;
  const load = async () => {
    const d = await gapi(`/staff?days=${days}`);
    const s = d.summary;
    const noteIn = h("textarea", { placeholder: "Nachricht an das Team (intern) …", style: { minHeight: "70px" } });
    const pin = h("input", { type: "checkbox" });
    const post = h("button.btn.primary.sm", "Posten");
    post.addEventListener("click", () => withLoading(post, async () => {
      if (!noteIn.value.trim()) return;
      await gapi("/staff/notes", { method: "POST", body: { content: noteIn.value, pinned: pin.checked } });
      load();
    }));
    const rangeSel = h("select", [7, 30, 90].map((x) => h("option", { value: x, selected: x === days }, `Letzte ${x} Tage`)));
    rangeSel.addEventListener("change", () => { days = Number(rangeSel.value); load(); });
    fill(el, h("div.toolbar", rangeSel),
      h("div.grid.g4", statCard("Offene Tickets", fmtNum(s.open_tickets), "🎫"), statCard("Ø Ticket-Dauer", s.avg_ticket_minutes ? fmtDuration(s.avg_ticket_minutes * 60) : "—", "⏱️", `${s.closed_period} geschlossen`),
        statCard("Mod-Aktionen", fmtNum(s.mod_actions), "🛡️", `${s.active_moderators} aktive Moderatoren`), statCard("Verwarnungen", fmtNum(s.warnings), "⚠️")),
      h("div.grid.g3", { style: { marginTop: "16px" } },
        h("div.glass.card.span2", h("h3", "📊 Staff-Aktivität"), table([
          { label: "Teammitglied", render: (p) => userCell(p.user) }, { label: "Mod-Aktionen", render: (p) => fmtNum(p.mod_total) },
          { label: "Tickets übernommen", render: (p) => fmtNum(p.tickets_claimed) }, { label: "Tickets geschlossen", render: (p) => fmtNum(p.tickets_closed) },
          { label: "Ø Erstantwort", render: (p) => p.avg_response_min !== null ? `${p.avg_response_min} min` : "—" },
          { label: "Details", render: (p) => h("div.chips", Object.entries(p.actions).map(([a, n]) => h("span.chip", `${a} ${n}`))) },
        ], d.people, { empty: "Keine Staff-Aktivität im Zeitraum" })),
        h("div.glass.card", h("h3", "👮 Team ", h("span.muted", `· ${d.team.length}`)), h("div.list", d.team.map((m) => h("div.item", userCell(m, m.top_role), h("span.grow"),
          pill(m.status, m.status === "offline" ? "" : "green")))))),
      h("div.glass.card", { style: { marginTop: "16px" } }, h("h3", "🗒️ Staff-Board"), h("div.stack", noteIn, h("div.row.between", h("label.switch", pin, h("span.track"), h("span", "Anpinnen")), post)),
        h("div", { style: { marginTop: "14px" } }, d.notes.length ? d.notes.map((n) => h("div.note" + (n.pinned ? ".pinned" : ""),
          h("div.meta", n.pinned ? "📌" : null, h("b", n.author_name), fmtRel(n.created_at), h("span", { style: { marginLeft: "auto" } },
            h("button.btn.ghost.sm", { onclick: async () => { await gapi(`/staff/notes/${n.id}`, { method: "PATCH", body: { pinned: !n.pinned } }); load(); } }, n.pinned ? "Lösen" : "Pinnen"),
            h("button.btn.ghost.sm", { onclick: async () => { if (await confirmDialog({ title: "Notiz löschen?" })) { await gapi(`/staff/notes/${n.id}`, { method: "DELETE" }); load(); } } }, "🗑"))),
          n.content)) : emptyState("Noch keine Team-Notizen", "🗒️"))));
  };
  await load();
}

async function raids(el) {
  const d = await gapi("/raids");
  const end = h("button.btn.danger", "Lockdown beenden");
  end.addEventListener("click", () => withLoading(end, async () => { await gapi("/raids/end", { method: "POST" }); toast("Lockdown beendet", "success"); raids(el); }));
  fill(el, 
    d.active ? h("div.callout.err", h("div.row.between", h("b", "🚨 Anti-Raid-Lockdown ist aktiv!"), state.ctx.level === "admin" ? end : null)) : h("div.callout", "✅ Kein aktiver Raid. Anti-Raid überwacht Join-Wellen automatisch (Einstellungen → Sicherheit)."),
    h("div.glass.card", h("h3", "🚨 Raid-Historie"), table([
      { label: "Start", render: (r) => fmtDate(r.started_at) }, { label: "Joins", render: (r) => h("b", fmtNum(r.join_count)) },
      { label: "Aktionen", render: (r) => h("div.chips", (r.actions || []).map((a) => h("span.chip", a))) },
      { label: "Ende", render: (r) => r.ended_at ? fmtDate(r.ended_at) : pill("läuft", "red") },
    ], d.items, { empty: "Noch keine Raids erkannt" })));
  onLive("raid", () => raids(el));
}

async function audit(el) {
  let page = 1, q = "";
  const list = h("div");
  const load = async () => {
    const d = await gapi(`/audit?q=${encodeURIComponent(q)}&page=${page}`);
    const entries = [
      ...d.items.map((a) => ({ time: a.created_at, who: a.actor_name, what: a.action, target: a.target, src: a.source })),
      ...d.cases.map((c) => ({ time: c.created_at, who: c.moderator_name, what: `mod.${c.action}`, target: `${c.user_name} → Case #${c.case_number}`, src: c.source })),
    ].sort((a, b) => new Date(b.time) - new Date(a.time));
    fill(list, h("div.list", entries.length ? entries.map((e) => h("div.item",
      h("span.mono.muted", new Date(e.time).toLocaleTimeString(state.lang, { hour: "2-digit", minute: "2-digit" })),
      h("div.grow", h("div.title", h("b", e.who), " → ", e.what), h("div.meta", e.target || "—", " · ", fmtDate(e.time))), pill(e.src))) : emptyState("Keine Einträge")),
      pager(page, d.per_page, d.total, (p) => { page = p; load(); }));
  };
  const search = h("input", { type: "search", placeholder: "Aktion, Person, Ziel …" });
  search.addEventListener("input", debounce(() => { q = search.value; page = 1; load(); }));
  fill(el, h("div.glass.card", h("div.toolbar", search), list));
  onLive("audit", () => page === 1 && !q && load());
  await load();
}
