import { h, clear, fill, gapi, state, fmtNum, tabs, table, pill, openModal, toast, withLoading, confirmDialog, debounce, onLive } from "../core.js";
import { moduleSettings, collection, loadMeta } from "../forms.js";

const PERM_LABELS = {
  administrator: "Administrator", manage_guild: "Server verwalten", manage_roles: "Rollen verwalten", manage_channels: "Kanäle verwalten",
  kick_members: "Mitglieder kicken", ban_members: "Mitglieder bannen", moderate_members: "Timeout", manage_messages: "Nachrichten verwalten",
  mention_everyone: "@everyone erwähnen", manage_nicknames: "Nicknames verwalten", view_audit_log: "Audit-Log", send_messages: "Nachrichten senden",
  view_channel: "Kanäle sehen", connect: "Verbinden", speak: "Sprechen", move_members: "Mitglieder verschieben", mute_members: "Stummschalten",
  deafen_members: "Taub schalten", manage_events: "Events verwalten", manage_threads: "Threads verwalten", create_instant_invite: "Einladungen erstellen",
  attach_files: "Dateien anhängen", embed_links: "Links einbetten", add_reactions: "Reaktionen", use_external_emojis: "Externe Emojis",
  change_nickname: "Nickname ändern", manage_webhooks: "Webhooks verwalten", manage_expressions: "Emojis/Sticker verwalten", read_message_history: "Verlauf lesen",
};

export async function render(root) {
  const admin = state.ctx.level === "admin";
  const body = h("div");
  const items = [["roles", "🎭 Rollen"]];
  if (admin) items.push(["menus", "🧩 Self-Role-Menüs"], ["sync", "🔄 Synchronisation"]);
  const show = (k) => ({
    roles: () => roles(body),
    menus: () => collection(body, "role_menus", { intro: "Buttons oder Select-Menü – z. B. Gaming, Fortnite, Minecraft, Roblox, Benachrichtigungen, Streams, Events.",
      columns: { message_id: (r) => (r.message_id ? pill("veröffentlicht", "green") : pill("nicht gepostet", "yellow")) } }),
    sync: () => {
      const btn = h("button.btn", "🔄 Regeln jetzt auf alle Mitglieder anwenden");
      btn.addEventListener("click", () => withLoading(btn, async () => { const r = await gapi("/roles/sync", { method: "POST" }); toast(`${fmtNum(r.checked)} Mitglieder geprüft`, "success"); }));
      return moduleSettings(body, "roles", { title: "Rollen-Synchronisation", intro: "Wer Rolle A hat, bekommt automatisch Rolle B (und verliert sie optional wieder).",
        extra: h("div.row", { style: { marginBottom: "16px" } }, btn) });
    },
  })[k]();
  fill(root, h("div.page-head", h("div", h("h2", "🎭 Rollen"), h("p", "Rollen verwalten, Self-Roles per Button/Select und Synchronisationsregeln."))), tabs(items, "roles", show), body);
  show("roles");
}

async function roles(el) {
  await loadMeta();
  const d = await gapi("/roles");
  const admin = state.ctx.level === "admin";
  let q = "";
  const listBox = h("div");
  const draw = () => {
    const rows = d.roles.filter((r) => !r.default && (!q || r.name.toLowerCase().includes(q)));
    fill(listBox, table([
      { label: "Rolle", render: (r) => h("span.chip", h("i.swatch", { style: { background: r.color || "#99a" } }), r.name) },
      { label: "Mitglieder", render: (r) => fmtNum(r.members) }, { label: "Position", key: "position" },
      { label: "Eigenschaften", render: (r) => h("div.chips", r.hoist ? pill("separat") : null, r.mentionable ? pill("erwähnbar") : null, r.managed ? pill("Integration", "purple") : null,
        (BigInt(r.permissions) & 8n) ? pill("Admin", "red") : null) },
      { label: "", cls: "actions", render: (r) => admin && r.editable ? h("div.row", { style: { justifyContent: "flex-end", gap: "6px" } },
        h("button.btn.sm", { onclick: () => massDialog(r) }, "Allen geben / nehmen"),
        h("button.btn.sm", { onclick: () => editor(r) }, "Bearbeiten"),
        h("button.btn.sm.danger", { onclick: async () => { if (await confirmDialog({ title: `Rolle „${r.name}“ löschen?`, text: "Die Rolle wird auf Discord gelöscht.", typeToConfirm: r.name })) {
          await gapi(`/roles/${r.id}`, { method: "DELETE" }); toast("Rolle gelöscht", "success"); state.meta = null; roles(el); } } }, "Löschen")) : h("span.muted", r.managed ? "verwaltet" : "🔒") },
    ], rows, { onRow: admin ? (r) => r.editable && editor(r) : null }));
  };
  const editor = (r = null) => {
    const name = h("input", { type: "text", value: r?.name || "", maxlength: 100 });
    const color = h("input", { type: "color", value: r?.color || "#99aab5" });
    const hoist = h("input", { type: "checkbox", checked: !!r?.hoist });
    const ment = h("input", { type: "checkbox", checked: !!r?.mentionable });
    const pos = h("input", { type: "number", min: 1, max: d.bot_top_position - 1, value: r?.position || 1 });
    let perms = BigInt(r?.permissions || "0");
    const permGrid = h("div.chips", d.permissions.filter((p) => PERM_LABELS[p]).map((p) => {
      const bit = 1n << BigInt(PERMISSION_BITS[p] ?? 0);
      const c = h("span.chip.toggle" + ((perms & bit) ? ".on" : ""), { onclick: () => { perms ^= bit; c.classList.toggle("on"); } }, PERM_LABELS[p]);
      return c;
    }));
    const save = h("button.btn.primary", r ? "Speichern" : "Erstellen");
    const m = openModal({ title: r ? `Rolle bearbeiten · ${r.name}` : "Neue Rolle", size: "lg", body: h("div.stack",
      h("div.form-grid", h("div.field", h("label", "Name"), name), h("div.field", h("label", "Farbe"), h("div.color-input", color)),
        h("label.switch", hoist, h("span.track"), h("span", "Separat anzeigen (Hoist)")), h("label.switch", ment, h("span.track"), h("span", "Von allen erwähnbar")),
        r ? h("div.field", h("label", `Position (max. ${d.bot_top_position - 1}, unter der Bot-Rolle)`), pos) : null),
      h("div.field", h("label", "Berechtigungen"), h("div.help", "Du kannst nur Rechte vergeben, die du selbst besitzt."), permGrid)),
      footer: [h("button.btn.ghost", { onclick: () => m.close() }, "Abbrechen"), save] });
    save.addEventListener("click", () => withLoading(save, async () => {
      const body = { name: name.value, color: color.value, hoist: hoist.checked, mentionable: ment.checked, permissions: perms.toString() };
      if (r && Number(pos.value) !== r.position) body.position = Number(pos.value);
      await gapi(r ? `/roles/${r.id}` : "/roles", { method: r ? "PATCH" : "POST", body });
      toast(r ? "Rolle aktualisiert" : "Rolle erstellt", "success"); m.close(); state.meta = null; roles(el);
    }));
  };
  const search = h("input", { type: "search", placeholder: "Rolle suchen …" });
  search.addEventListener("input", debounce(() => { q = search.value.toLowerCase(); draw(); }, 150));
  fill(el, h("div.glass.card", h("div.toolbar", search, admin ? h("button.btn.primary", { onclick: () => editor() }, "＋ Neue Rolle") : null), listBox));
  draw();
}

// Rolle allen Mitgliedern geben oder wegnehmen – mit Live-Fortschritt
function massDialog(r) {
  const action = h("select", h("option", { value: "add" }, "Allen geben"), h("option", { value: "remove" }, "Allen wegnehmen"));
  const target = h("select", h("option", { value: "humans" }, "Nur Menschen"), h("option", { value: "all" }, "Alle (inkl. Bots)"), h("option", { value: "bots" }, "Nur Bots"));
  const only = h("select", h("option", { value: "" }, "— alle Mitglieder —"), state.meta.roles.filter((x) => !x.default && x.id !== r.id).map((x) => h("option", { value: x.id }, `nur wer „${x.name}“ hat`)));
  const status = h("div");
  const start = h("button.btn.primary", "Starten");
  const cancel = h("button.btn.danger", { hidden: true }, "Abbrechen");
  const m = openModal({ title: `Rolle „${r.name}“ an alle`, size: "sm", body: h("div.stack",
    h("div.field", h("label", "Aktion"), action), h("div.field", h("label", "Wer"), target), h("div.field", h("label", "Filter"), only), status),
    footer: [cancel, start] });
  const draw = (j) => {
    const pct = j.total ? Math.round(((j.done + j.failed) / j.total) * 100) : 100;
    fill(status, h("div.callout.gold", h("div.row.between", h("b", j.running ? "Läuft …" : "Fertig"), h("span", `${fmtNum(j.done)} / ${fmtNum(j.total)}${j.failed ? ` · ${j.failed} fehlgeschlagen` : ""}`)),
      h("div.progress", h("i", { style: { width: pct + "%" } }))));
    cancel.hidden = !j.running;
    if (!j.running) start.textContent = "Schließen";
  };
  start.addEventListener("click", async () => {
    if (start.textContent === "Schließen") return m.close();
    const ok = await confirmDialog({ title: "Wirklich starten?", danger: action.value === "remove", confirmText: "Starten",
      text: `Die Rolle „${r.name}“ wird ${action.value === "add" ? "allen passenden Mitgliedern gegeben" : "allen weggenommen"}. Bei großen Servern dauert das einige Minuten.` });
    if (!ok) return;
    withLoading(start, async () => {
      const j = await gapi(`/roles/${r.id}/mass`, { method: "POST", body: { action: action.value, target: target.value, only_with: only.value || null } });
      draw(j);
      start.disabled = true;
      onLive("mass_role", (x) => { if (x.role_id === r.id) { draw(x); if (!x.running) start.disabled = false; } });
    });
  });
  cancel.addEventListener("click", () => gapi("/roles/mass/cancel", { method: "POST" }));
}

// Discord-Permission-Bitpositionen
const PERMISSION_BITS = {
  create_instant_invite: 0, kick_members: 1, ban_members: 2, administrator: 3, manage_channels: 4, manage_guild: 5, add_reactions: 6, view_audit_log: 7,
  view_channel: 10, send_messages: 11, manage_messages: 13, embed_links: 14, attach_files: 15, read_message_history: 16, mention_everyone: 17,
  use_external_emojis: 18, connect: 20, speak: 21, mute_members: 22, deafen_members: 23, move_members: 24, change_nickname: 26, manage_nicknames: 27,
  manage_roles: 28, manage_webhooks: 29, manage_expressions: 30, manage_events: 33, manage_threads: 34, moderate_members: 40,
};
