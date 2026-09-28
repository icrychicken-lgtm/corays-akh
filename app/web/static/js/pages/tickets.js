import { h, clear, fill, gapi, state, table, pager, userCell, fmtNum, fmtDate, fmtRel, fmtDuration, pill, debounce, tabs, openModal, toast, withLoading, emptyState, confirmDialog, onLive, statCard } from "../core.js";
import { moduleSettings, collection, sendPanel, picker, loadMeta, modules, renderForm } from "../forms.js";

const PRIO = { low: ["🟢 Niedrig", "green"], normal: ["🔵 Normal", "blue"], high: ["🟠 Hoch", "yellow"], urgent: ["🔴 Dringend", "red"] };
const APP_STATUS = { open: ["Offen", "yellow"], in_review: ["In Bearbeitung", "blue"], accepted: ["Angenommen", "green"], rejected: ["Abgelehnt", "red"] };

export async function render(root, params) {
  if (params[0] && /^\d+$/.test(params[0])) return detail(root, params[0]);
  const admin = state.ctx.level === "admin";
  const body = h("div");
  const items = [["list", "Tickets"], ["apps", "Bewerbungen"]];
  if (admin) items.unshift(["panel", "Ticket-Panel"]);
  const show = (k) => ({ list: () => list(body), apps: () => apps(body), panel: () => panelEditor(body) })[k]();
  const start = admin ? "panel" : "list";
  fill(root, h("div.page-head", h("div", h("h2", "Tickets"), h("p", "Ein Panel für alles: Support, Bewerbung, Entbannungsantrag, Partnerschaft …"))),
    tabs(items, start, show), body);
  show(start);
}

// Ticket-Panel: Einstellungen + Live-Vorschau so, wie es in Discord aussieht
const DEFAULT_CATS = [
  ["Support", "🛠️", "Hilfe & Fragen"], ["Bewerbung", "📋", "Ins Team"], ["Entbannungsantrag", "🔓", "Ban aufheben"],
  ["Partnerschaft", "🤝", "Kooperation"], ["Beschwerde", "⚠️", "Etwas melden"], ["Creator", "🎥", "Für Creator"],
  ["Gewinnspiel", "🎁", "Gewinne & Giveaways"], ["Technische Probleme", "💻", "Bot & Technik"], ["Allgemeine Anfrage", "💬", "Alles andere"],
];
const PANEL_GROUPS = {
  panel_channel: "Panel", panel_title: "Panel", panel_text: "Panel", panel_button: "Panel", panel_image: "Panel",
  category: "Tickets", claimed_category: "Tickets", closed_category: "Tickets", staff_roles: "Tickets", transcript_channel: "Tickets", max_open: "Tickets", ping_staff: "Tickets", dm_transcript: "Tickets", name_format: "Tickets",
};

async function panelEditor(el) {
  await loadMeta();
  const mods = await modules(true);
  const m = mods.find((x) => x.key === "tickets");
  let cats = [];
  const form = renderForm(m.fields.filter((f) => PANEL_GROUPS[f.key]).map((f) => ({ ...f, group: PANEL_GROUPS[f.key], advanced: false })), m.settings);
  const catBox = h("div");
  const addDefaults = h("button.btn", "Standard-Kategorien hinzufügen");
  addDefaults.addEventListener("click", () => withLoading(addDefaults, async () => {
    const r = await gapi("/tickets/ensure-categories", { method: "POST", body: { missing: true } });
    toast(r.created ? `${r.created} Kategorien hinzugefügt (mit passenden Fragen)` : "Alle Standard-Kategorien sind da – fehlende Fragen wurden ergänzt", "success");
    loadCats();
  }));
  const loadCats = () => collection(catBox, "ticket_categories", {
    createLabel: "Kategorie", intro: "Alles, was nach „Ticket öffnen“ zur Auswahl steht – z. B. Support, Bewerbung, Entbannungsantrag. Reihenfolge über das Feld „Reihenfolge“.",
    columns: { emoji: (r) => h("span", { style: { fontSize: "18px" } }, r.emoji) },
    onLoaded: (items) => { cats = items.filter((c) => c.enabled).sort((a, b) => a.position - b.position); draw(); },
  });
  const preview = h("div");
  const plain = (s) => (s || "").replace(/\*\*(.+?)\*\*/g, "$1");
  const draw = () => {
    const v = form.getValues();
    const ch = state.meta.channels.find((c) => c.id === v.panel_channel);
    fill(preview,
      h("div.muted", { style: { fontSize: "12px", marginBottom: "8px" } }, ch ? `So sieht es in #${ch.name} aus` : "Noch kein Channel gewählt"),
      h("div.dc", h("div.dc-embed",
        h("div.dc-title", plain(v.panel_title) || "Support"), h("div.dc-text", plain(v.panel_text)),
        cats.length ? h("div.dc-field", h("b", "Kategorien"), cats.map((c) => h("div", `${c.emoji} ${c.label}${c.description ? " — " + c.description : ""}`))) : null,
        v.panel_image ? h("img.dc-img", { src: v.panel_image, alt: "" }) : null),
        h("div.dc-btn", "🎫 " + (v.panel_button || "Ticket öffnen"))),
      h("div.muted", { style: { fontSize: "12px", margin: "16px 0 8px" } }, "Nach dem Klick sieht nur der User diese Auswahl:"),
      h("div.dc", h("div.dc-select", h("div.dc-select-head", "Kategorie wählen …"),
        cats.length ? cats.map((c) => h("div.dc-option", h("span", c.emoji), h("div", h("b", c.label), c.description ? h("small", c.description) : null)))
          : h("div.dc-option", h("span.muted", "Noch keine Kategorien – bitte anlegen")))),
      h("div.muted", { style: { fontSize: "12px", marginTop: "8px" } }, "→ danach fragt ein Fenster „Was ist dein Anliegen?“ und das Ticket wird erstellt."));
  };
  form.el.addEventListener("input", draw);
  form.el.addEventListener("change", draw);
  const save = async () => {
    const r = await gapi("/modules/tickets", { method: "PUT", body: { settings: { ...m.settings, ...form.getValues() } } });
    m.settings = r.settings;
    form.setErrors({});
  };
  const saveBtn = h("button.btn", "Speichern");
  saveBtn.addEventListener("click", () => withLoading(saveBtn, async () => { try { await save(); toast("Gespeichert", "success"); } catch (e) { form.setErrors(e.errors || {}); } }));
  const sendBtn = h("button.btn.primary", "Speichern & Panel senden");
  sendBtn.addEventListener("click", () => withLoading(sendBtn, async () => {
    try { await save(); } catch (e) { form.setErrors(e.errors || {}); return; }
    const r = await gapi("/panels/tickets", { method: "POST" });
    toast("Ticket-Panel gesendet", "success");
    if (r.url) window.open(r.url, "_blank", "noopener");
  }));
  fill(el,
    !m.enabled ? h("div.callout.warn", "Das Ticket-Modul ist deaktiviert – unter Einstellungen → Module einschalten.") : null,
    h("div.grid.g2",
      h("div", form.el, h("div.row", { style: { justifyContent: "flex-end", marginTop: "4px" } }, saveBtn, sendBtn)),
      h("div.glass.card", { style: { alignSelf: "start", position: "sticky", top: "80px" } }, h("h3", "Vorschau"), preview)),
    h("div", { style: { marginTop: "16px" } }, h("div.row", { style: { justifyContent: "flex-end", marginBottom: "10px" } }, addDefaults), catBox),
    h("div.callout", { style: { marginTop: "16px" } }, "Nach Änderungen an den Kategorien einfach nochmal „Speichern & Panel senden“ – dann ist das neue Panel im Channel."));
  draw();
  const auto = await gapi("/tickets/ensure-categories", { method: "POST", quiet: true }).catch(() => ({ created: 0 }));
  if (auto.created) toast(`${auto.created} Standard-Kategorien automatisch angelegt`, "success");
  await loadCats();
}

async function list(el) {
  let page = 1, status = "open", q = "", priority = "";
  const wrap = h("div");
  const load = async () => {
    const d = await gapi(`/tickets?status=${status}&q=${encodeURIComponent(q)}&priority=${priority}&page=${page}`);
    fill(wrap, h("div.grid.g3", { style: { marginBottom: "16px" } }, statCard("Offen", fmtNum(d.counts.open || 0), "🟢"), statCard("Geschlossen", fmtNum(d.counts.closed || 0), "🔒"),
      statCard("Gesamt", fmtNum((d.counts.open || 0) + (d.counts.closed || 0)), "🎫")),
      h("div.glass.card", toolbar, table([
        { label: "#", render: (t) => h("b", "#" + String(t.number).padStart(4, "0")) }, { label: "Kategorie", key: "category_label" },
        { label: "Von", key: "opener_name" }, { label: "Anliegen", render: (t) => (t.subject || "").slice(0, 70) },
        { label: "Priorität", render: (t) => pill(...(PRIO[t.priority] || [t.priority])) },
        { label: "Übernommen", render: (t) => t.claimed_name || h("span.muted", "—") },
        { label: "Status", render: (t) => pill(t.status === "open" ? "Offen" : "Geschlossen", t.status === "open" ? "green" : "") },
        { label: "Erstellt", render: (t) => fmtRel(t.created_at) },
      ], d.items, { onRow: (t) => { history.pushState({}, "", `/g/${state.guildId}/tickets/${t.id}`); dispatchEvent(new PopStateEvent("popstate")); }, empty: "Keine Tickets gefunden" }),
      pager(page, d.per_page, d.total, (p) => { page = p; load(); })));
  };
  const statusSel = h("select", h("option", { value: "open" }, "Offene"), h("option", { value: "closed" }, "Geschlossene"), h("option", { value: "" }, "Alle"));
  statusSel.addEventListener("change", () => { status = statusSel.value; page = 1; load(); });
  const prioSel = h("select", h("option", { value: "" }, "Jede Priorität"), Object.entries(PRIO).map(([k, [l]]) => h("option", { value: k }, l)));
  prioSel.addEventListener("change", () => { priority = prioSel.value; page = 1; load(); });
  const search = h("input", { type: "search", placeholder: "Nummer, User, Anliegen …" });
  search.addEventListener("input", debounce(() => { q = search.value; page = 1; load(); }));
  const toolbar = h("div.toolbar", search, statusSel, prioSel);
  fill(el, wrap);
  onLive("ticket", () => load());
  await load();
}

async function detail(root, id) {
  await loadMeta();
  const t = await gapi(`/tickets/${id}`);
  const reload = () => detail(root, id);
  const act = async (action, extra = {}, btn) => {
    const run = async () => { await gapi(`/tickets/${id}/action`, { method: "POST", body: { action, ...extra } }); toast("Erledigt", "success"); reload(); };
    btn ? withLoading(btn, run) : run();
  };
  const closeBtn = h("button.btn.danger", "🔒 Schließen");
  closeBtn.addEventListener("click", () => {
    const reason = h("textarea", { placeholder: "Grund (optional)" });
    const ok = h("button.btn.danger", "Ticket schließen");
    const m = openModal({ title: `Ticket #${t.number} schließen`, size: "sm", body: h("div.field", h("label", "Grund"), reason),
      footer: [h("button.btn.ghost", { onclick: () => m.close() }, "Abbrechen"), ok] });
    ok.addEventListener("click", () => withLoading(ok, async () => { await act("close", { reason: reason.value }); m.close(); }));
  });
  const prio = h("select", Object.entries(PRIO).map(([k, [l]]) => h("option", { value: k, selected: k === t.priority }, l)));
  prio.addEventListener("change", () => act("priority", { priority: prio.value }));
  const memberPick = (label, action) => {
    const p = picker({ options: [], placeholder: label, remote: async (q) => q.length < 2 ? [] : (await gapi(`/members?q=${encodeURIComponent(q)}&per_page=10`, { quiet: true })).items.map((m) => ({ value: m.id, label: m.display, sub: m.name })) });
    p.addEventListener("change", () => p.getValue() && act(action, { user_id: p.getValue() }));
    return p;
  };
  const noteIn = h("textarea", { placeholder: "Interne Notiz (nur für Staff sichtbar)", style: { minHeight: "70px" } });
  const noteBtn = h("button.btn.primary.sm", "Notiz hinzufügen");
  noteBtn.addEventListener("click", () => noteIn.value.trim() && act("note", { content: noteIn.value }, noteBtn));
  const frame = h("iframe", { src: `/api/g/${state.guildId}/tickets/${id}/transcript`, style: { width: "100%", height: "560px", border: "1px solid var(--border)", borderRadius: "12px", background: "#0b0b10" },
    sandbox: "allow-popups allow-popups-to-escape-sandbox", title: "Transcript", loading: "lazy" });
  const dur = t.closed_at ? (new Date(t.closed_at) - new Date(t.created_at)) / 1000 : (Date.now() - new Date(t.created_at)) / 1000;
  fill(root, 
    h("div.page-head", h("div", h("a.btn.ghost.sm", { href: `/g/${state.guildId}/tickets`, "data-link": true }, "‹ Tickets"),
      h("h2", { style: { marginTop: "8px" } }, `🎫 Ticket #${String(t.number).padStart(4, "0")} · ${t.category_label}`),
      h("p", pill(t.status === "open" ? "Offen" : "Geschlossen", t.status === "open" ? "green" : ""), " ", pill(...PRIO[t.priority]), t.locked ? pill("🔐 Gesperrt", "yellow") : null)),
      h("div.actions", t.channel_url ? h("a.btn", { href: t.channel_url, target: "_blank", rel: "noopener" }, "In Discord öffnen") : null,
        t.status === "open" ? [h("button.btn", { onclick: (e) => act("claim", {}, e.currentTarget) }, "🙋 Übernehmen"),
          h("button.btn", { onclick: (e) => act("lock", { locked: !t.locked }, e.currentTarget) }, t.locked ? "🔓 Entsperren" : "🔐 Sperren"), closeBtn]
          : h("button.btn.success", { onclick: (e) => act("reopen", {}, e.currentTarget) }, "🔓 Wieder öffnen"),
        state.ctx.level === "admin" && t.channel_id ? h("button.btn.danger", { onclick: async () => { if (await confirmDialog({ title: "Channel löschen?", text: "Der Ticket-Channel wird gelöscht. Das Transcript bleibt im Dashboard." })) act("delete"); } }, "🗑️ Channel löschen") : null)),
    h("div.grid.g3",
      h("div.glass.card.span2", h("h3", "💬 Verlauf / Transcript"), t.has_transcript || t.channel_id ? frame : emptyState("Kein Verlauf verfügbar", "📄")),
      h("div.stack",
        h("div.glass.card", h("h3", "📋 Details"), h("dl.kv", h("dt", "Von"), h("dd", userCell(t.opener)), h("dt", "Übernommen"), h("dd", t.claimed_name || "—"),
          h("dt", "Erstellt"), h("dd", fmtDate(t.created_at)), h("dt", "Erste Antwort"), h("dd", t.first_response_at ? fmtRel(t.first_response_at) : "—"),
          h("dt", t.closed_at ? "Dauer" : "Offen seit"), h("dd", fmtDuration(dur)), t.closed_at ? [h("dt", "Geschlossen von"), h("dd", t.closed_by_name || "—"), h("dt", "Grund"), h("dd", t.close_reason || "—")] : null),
          h("div.field", { style: { marginTop: "12px" } }, h("label", "Anliegen"), h("div.note", t.subject))),
        t.status === "open" ? h("div.glass.card", h("h3", "⚙️ Verwalten"), h("div.stack", h("div.field", h("label", "Priorität"), prio),
          h("div.field", h("label", "User hinzufügen"), memberPick("User suchen …", "add_user")),
          h("div.field", h("label", "Übertragen an"), memberPick("Teammitglied suchen …", "transfer")),
          t.added_users.length ? h("div.field", h("label", "Hinzugefügte User"), h("div.chips", t.added_users.map((u) => h("span.chip", u.display || u.name,
            h("button.x", { onclick: () => act("remove_user", { user_id: u.id }) }, "×"))))) : null)) : null,
        h("div.glass.card", h("h3", "🗒️ Staff-Notizen"), h("div.stack", noteIn, h("div.row", { style: { justifyContent: "flex-end" } }, noteBtn)),
          h("div", { style: { marginTop: "10px" } }, t.notes.map((n) => h("div.note", h("div.meta", h("b", n.author_name), fmtRel(n.created_at)), n.content)))))));
}

async function apps(el) {
  let status = "open";
  const wrap = h("div");
  const load = async () => {
    const d = await gapi(`/applications?status=${status}`);
    fill(wrap, table([
      { label: "#", render: (a) => "#" + a.id }, { label: "User", render: (a) => userCell(a.user) }, { label: "Position", key: "position" },
      { label: "Status", render: (a) => pill(...(APP_STATUS[a.status] || [a.status])) }, { label: "Reviewer", render: (a) => a.reviewer_name || "—" },
      { label: "Eingereicht", render: (a) => fmtRel(a.created_at) },
    ], d.items, { onRow: (a) => openApp(a, load), empty: "Keine Bewerbungen" }));
  };
  const sel = h("select", h("option", { value: "open" }, "Offen"), h("option", { value: "in_review" }, "In Bearbeitung"), h("option", { value: "accepted" }, "Angenommen"),
    h("option", { value: "rejected" }, "Abgelehnt"), h("option", { value: "" }, "Alle"));
  sel.addEventListener("change", () => { status = sel.value; load(); });
  fill(el, h("div.glass.card", h("div.toolbar", sel), wrap));
  onLive("application", () => load());
  await load();
}

function openApp(a, reload) {
  const reason = h("textarea", { placeholder: "Begründung (wird dem User per DM geschickt)" });
  const decide = (status) => async (e) => withLoading(e.currentTarget, async () => {
    await gapi(`/applications/${a.id}/decide`, { method: "POST", body: { status, reason: reason.value } });
    toast("Status aktualisiert – User wird benachrichtigt", "success"); m.close(); reload();
  });
  const m = openModal({ title: `Bewerbung #${a.id} · ${a.position || "Team"}`, size: "lg", body: h("div.stack",
    h("div.row.between", userCell(a.user, fmtDate(a.created_at)), pill(...(APP_STATUS[a.status] || [a.status]))),
    ...a.answers.map((qa) => h("div.field", h("label", qa.q), h("div.note", qa.a || "—"))),
    a.decision_reason ? h("div.callout", `Entscheidung von ${a.reviewer_name}: ${a.decision_reason}`) : null,
    h("div.field", h("label", "Begründung"), reason)),
    footer: [h("button.btn", { onclick: decide("in_review") }, "🔎 In Bearbeitung"), h("button.btn.danger", { onclick: decide("rejected") }, "⛔ Ablehnen"),
      h("button.btn.success", { onclick: decide("accepted") }, "✅ Annehmen")] });
}
