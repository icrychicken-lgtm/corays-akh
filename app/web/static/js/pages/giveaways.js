import { h, clear, fill, gapi, state, table, userCell, fmtNum, fmtDate, fmtRel, pill, tabs, openModal, toast, withLoading, emptyState, confirmDialog, onLive, statCard } from "../core.js";
import { moduleSettings, loadMeta, renderForm } from "../forms.js";

const FIELDS = [
  { key: "prize", type: "str", label: "Preis", default: "", max_len: 200, group: "Giveaway" },
  { key: "duration", type: "str", label: "Dauer (z. B. 30m, 2h, 3d)", default: "1d", max_len: 20, group: "Giveaway" },
  { key: "winners", type: "int", label: "Gewinner", default: 1, min: 1, max: 50, group: "Giveaway" },
  { key: "channel_id", type: "channel", label: "Channel", default: null, group: "Giveaway" },
  { key: "description", type: "text", label: "Beschreibung", default: "", max_len: 2000, group: "Giveaway" },
  { key: "image_url", type: "url", label: "Bild-URL", default: "", group: "Giveaway" },
  { key: "required_role", type: "role", label: "Mindestrolle", default: null, group: "Teilnahmebedingungen" },
  { key: "account_days", type: "int", label: "Mindestalter Account (Tage)", default: 0, min: 0, group: "Teilnahmebedingungen" },
  { key: "server_days", type: "int", label: "Mindestzeit auf dem Server (Tage)", default: 0, min: 0, group: "Teilnahmebedingungen" },
  { key: "extra", type: "str", label: "Zusätzliche Bedingung (Text)", default: "", max_len: 200, group: "Teilnahmebedingungen" },
  { key: "role_entries", type: "objlist", label: "Role Entries", default: [], group: "Bonus-Entries (leer = Standard aus Einstellungen)",
    fields: [{ key: "role", type: "role", label: "Rolle", default: null }, { key: "entries", type: "int", label: "Zusätzliche Entries", default: 1, min: 1, max: 100 }] },
  { key: "invite_entries", type: "int", label: "Invite Entries (pro Einladung)", default: null, min: 0, group: "Bonus-Entries (leer = Standard aus Einstellungen)" },
  { key: "activity_messages", type: "int", label: "Aktivität: ab Nachrichten", default: null, min: 0, group: "Bonus-Entries (leer = Standard aus Einstellungen)" },
  { key: "activity_entries", type: "int", label: "Aktivität: Entries", default: null, min: 0, group: "Bonus-Entries (leer = Standard aus Einstellungen)" },
  { key: "early_minutes", type: "int", label: "Early Join: erste Minuten", default: null, min: 0, group: "Bonus-Entries (leer = Standard aus Einstellungen)" },
  { key: "early_entries", type: "int", label: "Early Join: Entries", default: null, min: 0, group: "Bonus-Entries (leer = Standard aus Einstellungen)" },
];

export async function render(root) {
  const body = h("div");
  const items = [["list", "🎁 Giveaways"]];
  if (state.ctx.level === "admin") items.push(["settings", "⚙️ Einstellungen"]);
  const show = (k) => (k === "list" ? list(body) : moduleSettings(body, "giveaways"));
  fill(root, h("div.page-head", h("div", h("h2", "🎁 Giveaways"), h("p", "Mehrere Gewinner, Bedingungen, Bonus-Entries, Reroll und Historie.")),
    h("div.actions", h("button.btn.primary", { onclick: () => create(() => show("list")) }, "＋ Neues Giveaway"))), tabs(items, "list", show), body);
  show("list");
}

async function create(done) {
  await loadMeta();
  const form = renderForm(FIELDS, {});
  const btn = h("button.btn.primary", "🎉 Starten");
  const m = openModal({ title: "Neues Giveaway", body: form.el, size: "lg", footer: [h("button.btn.ghost", { onclick: () => m.close() }, "Abbrechen"), btn] });
  btn.addEventListener("click", () => withLoading(btn, async () => {
    try {
      await gapi("/giveaways", { method: "POST", body: form.getValues() });
      toast("Giveaway gestartet", "success"); m.close(); done();
    } catch (e) { form.setErrors(e.errors || {}); }
  }));
}

async function list(el) {
  const d = await gapi("/giveaways");
  const active = d.items.filter((g) => !g.ended), ended = d.items.filter((g) => g.ended);
  const act = async (g, action, btn, body = {}) => withLoading(btn, async () => {
    await gapi(`/giveaways/${g.id}/${action}`, { method: "POST", body });
    toast({ end: "Beendet – Gewinner gezogen", reroll: "Neue Gewinner gezogen", delete: "Gelöscht" }[action], "success");
    list(el);
  });
  const entries = async (g) => {
    const r = await gapi(`/giveaways/${g.id}/entries`);
    const total = r.items.reduce((s, x) => s + x.entries, 0);
    openModal({ title: `👥 Teilnehmer · ${g.prize}`, body: table([{ label: "User", render: (u) => userCell(u) }, { label: "Entries", render: (u) => h("b", u.entries) },
      { label: "Chance", render: (u) => `${total ? ((u.entries / total) * 100).toFixed(1) : 0}%` }, { label: "Beigetreten", render: (u) => fmtRel(u.joined_at) }], r.items, { empty: "Noch keine Teilnehmer" }) });
  };
  const cols = (isActive) => [
    { label: "Preis", render: (g) => h("b", g.prize) }, { label: "Gewinner", render: (g) => isActive ? `${g.winners_count}×` : h("div.chips", g.winners.length ? g.winners.map((w) => h("span.chip", w.display || w.name)) : h("span.muted", "keine")) },
    { label: "Teilnehmer", render: (g) => fmtNum(g.participants) }, { label: isActive ? "Endet" : "Beendet", render: (g) => fmtRel(g.ends_at) }, { label: "Host", key: "host_name" },
    { label: "", cls: "actions", render: (g) => h("div.row", { style: { justifyContent: "flex-end", gap: "6px" } },
      h("button.btn.sm", { onclick: () => entries(g) }, "👥"),
      isActive ? h("button.btn.sm", { onclick: async (e) => { const b = e.currentTarget; if (await confirmDialog({ title: "Jetzt beenden?", text: "Gewinner werden sofort gezogen.", danger: false })) act(g, "end", b); } }, "⏹ Beenden")
        : h("button.btn.sm", { onclick: (e) => act(g, "reroll", e.currentTarget, { count: 1 }) }, "🔁 Reroll"),
      h("button.btn.sm.danger", { onclick: async (e) => { const b = e.currentTarget; if (await confirmDialog({ title: "Giveaway löschen?", text: g.prize })) act(g, "delete", b); } }, "🗑")) },
  ];
  fill(el, h("div.grid.g3", { style: { marginBottom: "16px" } }, statCard("Aktiv", fmtNum(active.length), "🟢"), statCard("Beendet", fmtNum(ended.length), "🏁"),
    statCard("Teilnahmen gesamt", fmtNum(d.items.reduce((s, g) => s + g.participants, 0)), "👥")),
    h("div.glass.card", h("h3", "🟢 Aktive Giveaways"), table(cols(true), active, { empty: "Kein aktives Giveaway" })),
    h("div.glass.card", { style: { marginTop: "16px" } }, h("h3", "🏁 Historie"), table(cols(false), ended, { empty: "Noch keine beendeten Giveaways" })));
  onLive("giveaway", (x) => ["start", "end", "delete"].includes(x.action) && list(el));
}
