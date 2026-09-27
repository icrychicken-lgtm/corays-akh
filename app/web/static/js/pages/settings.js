import { h, clear, fill, gapi, state, fmtNum, fmtDate, fmtRel, tabs, pill, table, openModal, toast, withLoading, emptyState, confirmDialog, setAccent, debounce } from "../core.js";
import { moduleSettings, modules, collection, loadMeta, picker, renderForm } from "../forms.js";

export async function render(root, params) {
  const body = h("div");
  const items = [["modules", "🧩 Module"], ["general", "⚙️ Allgemein"], ["commands", "⌨️ Commands"], ["custom", "🧩 Custom Commands"], ["autoresponder", "💬 Autoresponder"],
    ["features", "✨ Features"], ["integrations", "🔌 Integrationen"], ["backups", "💾 Backups"], ["errors", "🐞 Fehler"]];
  const initial = items.some(([k]) => k === params[0]) ? params[0] : "modules";
  const show = (k) => ({
    modules: () => moduleGrid(body), general: () => general(body), commands: () => commands(body),
    custom: () => collection(body, "custom_commands", { createLabel: "Command erstellen", intro: "Wird als echter Slash-Command registriert (z. B. /socials). Änderungen sind nach wenigen Sekunden in Discord sichtbar.",
      columns: { name: (r) => h("b.mono", "/" + r.name) } }),
    autoresponder: () => collection(body, "autoresponders", { createLabel: "Autoresponder erstellen", intro: "Trigger wie „discord“, „socials“, „stream“ oder „youtube“ – der Bot antwortet automatisch." }),
    features: () => features(body), integrations: () => integrations(body), backups: () => backups(body), errors: () => errors(body),
  })[k]();
  fill(root, h("div.page-head", h("div", h("h2", "⚙️ Einstellungen"), h("p", "Module, Berechtigungen, Integrationen, Backups – alles ohne Code konfigurierbar."))), tabs(items, initial, show), body);
  show(initial);
}

async function moduleGrid(el) {
  const list = await modules(true);
  const cats = { core: "Kern", moderation: "Moderation", security: "Sicherheit", support: "Support", community: "Community", streamer: "Streamer" };
  fill(el, ...Object.entries(cats).map(([cat, label]) => {
    const mods = list.filter((m) => m.category === cat);
    if (!mods.length) return null;
    return h("div", { style: { marginBottom: "22px" } }, h("h3", { style: { margin: "0 0 12px", fontSize: "14px", color: "var(--text-2)" } }, label),
      h("div.grid.g-auto", mods.map((m) => {
        const cb = h("input", { type: "checkbox", checked: m.enabled, disabled: !m.toggleable });
        const card = h("div.glass.module-card" + (m.enabled ? "" : ".off"), h("div.mi", m.icon), h("div.grow", h("b", m.name), h("p", m.description)),
          h("label.switch", cb, h("span.track")));
        cb.addEventListener("change", async () => {
          try {
            const r = await gapi(`/modules/${m.key}`, { method: "PUT", body: { enabled: cb.checked } });
            m.enabled = r.enabled; state.ctx.modules[m.key] = r.enabled;
            card.classList.toggle("off", !r.enabled);
            toast(`${m.name}: ${r.enabled ? "🟢 Aktiv" : "🔴 Deaktiviert"}`, r.enabled ? "success" : "warning");
            document.dispatchEvent(new CustomEvent("modules-changed"));
          } catch { cb.checked = !cb.checked; }
        });
        return card;
      })));
  }));
}

async function general(el) {
  await moduleSettings(el, "general", { title: "Allgemein & Design", intro: "Sprache, Farben der Embeds, Dashboard-Zugriff für Admin- und Staff-Rollen." });
  const accent = h("input", { type: "color", value: getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#c8a45d" });
  accent.addEventListener("input", () => { setAccent(accent.value); localStorage.setItem("nova:accent", accent.value); });
  el.prepend(h("div.glass.card", { style: { marginBottom: "16px" } }, h("div.row.between", h("div", h("b", "🎨 Dashboard-Akzentfarbe (nur für dich)"),
    h("div.muted", "Standard ist die Akzentfarbe des Servers.")), h("div.row", accent, h("button.btn.sm", { onclick: () => { localStorage.removeItem("nova:accent"); setAccent("#c8a45d"); } }, "Zurücksetzen")))));
}

async function features(el) {
  const body = h("div");
  const statsBtn = h("button.btn", "📈 Stats-Channels automatisch erstellen");
  statsBtn.addEventListener("click", () => withLoading(statsBtn, async () => { await gapi("/stats-channels/create", { method: "POST" }); toast("Stats-Channels erstellt", "success"); show("stats_channels"); }));
  const show = (k) => moduleSettings(body, k, k === "stats_channels" ? { extra: h("div.row", { style: { marginBottom: "16px" } }, statsBtn) } : {});
  fill(el, tabs([["afk", "💤 AFK"], ["starboard", "⭐ Starboard"], ["reminders", "⏰ Reminders"], ["tempvoice", "🔊 Temp-Channels"],
    ["stats_channels", "📈 Stats-Channels"], ["fun", "🎲 Fun"], ["custom_commands", "🧩 Custom Commands"], ["autoresponder", "💬 Autoresponder"]], "afk", show), body);
  show("afk");
}

async function commands(el) {
  await loadMeta();
  const d = await gapi("/commands");
  let q = "";
  const box = h("div");
  const edit = (c) => {
    const fields = [
      { key: "enabled", type: "bool", label: "Command aktiv", default: true },
      { key: "allowed_role_ids", type: "roles", label: "Erlaubte Rollen (leer = Discord-Standard)", default: [], group: "Rollen" },
      { key: "denied_role_ids", type: "roles", label: "Gesperrte Rollen", default: [], group: "Rollen" },
      { key: "allowed_user_ids", type: "users", label: "Zusätzlich erlaubte User", default: [], group: "User" },
      { key: "allowed_channel_ids", type: "channels", label: "Nur in diesen Channels (leer = überall)", default: [], group: "Channels" },
      { key: "denied_channel_ids", type: "channels", label: "Nicht in diesen Channels", default: [], group: "Channels" },
    ];
    const form = renderForm(fields, c);
    const save = h("button.btn.primary", "Speichern");
    const m = openModal({ title: `/${c.name} · Berechtigungen`, size: "lg", body: h("div.stack",
      h("div.callout", "Admins (Server verwalten) umgehen Rollen-/Channel-Beschränkungen immer. Discord-Standardrechte: ", c.default_permissions.length ? c.default_permissions.join(", ") : "keine"),
      c.subcommands.length ? h("div.muted", "Subcommands: " + c.subcommands.map((s) => "/" + s).join(", ")) : null, form.el),
      footer: [h("button.btn.ghost", { onclick: () => m.close() }, "Abbrechen"), save] });
    save.addEventListener("click", () => withLoading(save, async () => {
      try {
        await gapi(`/commands/${c.name}`, { method: "PUT", body: form.getValues() });
        Object.assign(c, form.getValues()); toast("Berechtigungen gespeichert", "success"); m.close(); draw();
      } catch (e) { form.setErrors(e.errors || {}); }
    }));
  };
  const draw = () => {
    const rows = d.items.filter((c) => !q || c.name.includes(q) || c.module.includes(q));
    fill(box, table([
      { label: "Command", render: (c) => h("div", h("b.mono", (c.type === "context" ? "☰ " : "/") + c.name), h("div.muted", c.description)) },
      { label: "Modul", render: (c) => pill(c.module) },
      { label: "Status", render: (c) => pill(c.enabled ? "aktiv" : "deaktiviert", c.enabled ? "green" : "red") },
      { label: "Rollen", render: (c) => c.allowed_role_ids.length ? `${c.allowed_role_ids.length} erlaubt` : h("span.muted", "Standard") },
      { label: "Channels", render: (c) => c.allowed_channel_ids.length || c.denied_channel_ids.length ? `${c.allowed_channel_ids.length}+ / ${c.denied_channel_ids.length}−` : h("span.muted", "überall") },
      { label: "", cls: "actions", render: (c) => h("button.btn.sm", { onclick: () => edit(c) }, "Berechtigungen") },
    ], rows, { onRow: edit }));
  };
  const search = h("input", { type: "search", placeholder: "Command oder Modul …" });
  search.addEventListener("input", debounce(() => { q = search.value.toLowerCase(); draw(); }, 150));
  fill(el, h("div.glass.card", h("div.toolbar", search, h("span.muted", `${d.items.length} Commands`)), box));
  draw();
}

async function integrations(el) {
  const d = await gapi("/integrations");
  const draw = (items) => fill(el, h("div.callout", "🔐 API-Keys werden verschlüsselt gespeichert und niemals im Klartext an das Dashboard zurückgegeben. Leere Felder behalten den bisherigen Wert."),
    h("div.grid.g2", items.map((p) => {
      const inputs = p.fields.map((f) => ({ f, i: h("input", { type: f.key.includes("secret") || f.key.includes("key") ? "password" : "text", autocomplete: "off",
        placeholder: f.configured ? (f.preview || "gesetzt") + (f.source === "global" ? " (global aus .env)" : "") : "nicht gesetzt" }) }));
      const save = h("button.btn.primary.sm", "Speichern");
      save.addEventListener("click", () => withLoading(save, async () => {
        const body = Object.fromEntries(inputs.map(({ f, i }) => [f.key, i.value]));
        const r = await gapi(`/integrations/${p.provider}`, { method: "PUT", body });
        toast(`${p.name} gespeichert`, "success"); draw(r.items);
      }));
      const clearBtn = h("button.btn.sm.ghost", "Server-Werte entfernen");
      clearBtn.addEventListener("click", async () => {
        if (!(await confirmDialog({ title: `${p.name}-Zugangsdaten entfernen?`, text: "Danach werden (falls vorhanden) die globalen Werte aus der .env genutzt." }))) return;
        const r = await gapi(`/integrations/${p.provider}`, { method: "PUT", body: Object.fromEntries(p.fields.map((f) => [f.key, "__clear__"])) });
        draw(r.items);
      });
      return h("div.glass.card", h("div.row.between", h("h3", { style: { margin: 0 } }, { twitch: "🟣", youtube: "🔴", kick: "🟢", spotify: "🎧" }[p.provider] + " " + p.name),
        pill(p.configured ? "verbunden" : "nicht konfiguriert", p.configured ? "green" : "yellow")), h("p.muted", { style: { fontSize: "12.5px" } }, p.help),
        h("div.stack", inputs.map(({ f, i }) => h("div.field", h("label", f.key), i))), h("div.row", { style: { justifyContent: "flex-end", marginTop: "12px" } }, clearBtn, save));
    })));
  draw(d.items);
}

async function backups(el) {
  const d = await gapi("/backups");
  const create = h("button.btn.primary", "💾 Backup jetzt erstellen");
  create.addEventListener("click", () => withLoading(create, async () => { await gapi("/backups", { method: "POST" }); toast("Backup erstellt", "success"); backups(el); }));
  const restore = async (b) => {
    if (!(await confirmDialog({ title: "Backup wiederherstellen?", text: `Alle Bot-Daten dieses Servers (Einstellungen, Level, Economy, Tickets, Cases …) werden auf den Stand vom ${fmtDate(b.created_at)} zurückgesetzt. Vorher wird automatisch ein Sicherungs-Backup erstellt.`, typeToConfirm: "RESTORE" }))) return;
    const r = await gapi(`/backups/${b.id}/restore`, { method: "POST", body: { confirm: "RESTORE" } });
    toast(`Wiederhergestellt (${fmtNum(Object.values(r.rows).reduce((a, x) => a + x, 0))} Datensätze)`, "success");
    backups(el);
  };
  fill(el, h("div.callout", "Automatische Backups laufen laut BACKUP_INTERVAL_HOURS. Backups enthalten alle Daten dieses Servers (ohne Log-Verlauf)."),
    h("div.glass.card", h("div.card-head", h("h3", "💾 Backups"), create), table([
      { label: "Erstellt", render: (b) => h("div", fmtDate(b.created_at), h("div.muted", fmtRel(b.created_at))) }, { label: "Datei", render: (b) => h("span.mono", b.filename) },
      { label: "Größe", render: (b) => `${(b.size / 1024).toFixed(1)} KB` }, { label: "Typ", render: (b) => pill(b.auto ? "automatisch" : "manuell", b.auto ? "" : "purple") },
      { label: "", cls: "actions", render: (b) => h("div.row", { style: { justifyContent: "flex-end", gap: "6px" } },
        h("a.btn.sm", { href: `/api/g/${state.guildId}/backups/${b.id}/download` }, "⬇ Download"), h("button.btn.sm", { onclick: () => restore(b) }, "♻ Wiederherstellen"),
        h("button.btn.sm.danger", { onclick: async () => { if (await confirmDialog({ title: "Backup löschen?" })) { await gapi(`/backups/${b.id}`, { method: "DELETE" }); backups(el); } } }, "🗑")) },
    ], d.items, { empty: "Noch keine Backups" })));
}

async function errors(el) {
  let resolved = false;
  const load = async () => {
    const d = await gapi(`/errors?resolved=${resolved}`);
    fill(box, table([
      { label: "ID", render: (e) => h("b.mono", e.id) }, { label: "Command", render: (e) => h("span.mono", e.command || "—") },
      { label: "Fehler", render: (e) => h("div", h("b", e.error_type), h("div.muted", (e.message || "").slice(0, 100))) },
      { label: "User", render: (e) => e.user_id ? h("span.mono", e.user_id) : "—" }, { label: "Zeit", render: (e) => fmtRel(e.created_at) },
      { label: "", cls: "actions", render: (e) => !e.resolved ? h("button.btn.sm.success", { onclick: async () => { await gapi(`/errors/${e.id}/resolve`, { method: "POST" }); load(); } }, "✔ Gelöst") : pill("gelöst", "green") },
    ], d.items, { onRow: (e) => openModal({ title: `Fehler ${e.id}`, size: "lg", body: h("div.stack", h("dl.kv", h("dt", "Command"), h("dd", e.command || "—"), h("dt", "Zeit"), h("dd", fmtDate(e.created_at))),
      h("pre.code", e.traceback)) }), empty: "Keine Fehler 🎉" }));
  };
  const box = h("div");
  const sel = h("select", h("option", { value: "false" }, "Offen"), h("option", { value: "true" }, "Gelöst"));
  sel.addEventListener("change", () => { resolved = sel.value === "true"; load(); });
  fill(el, h("div.glass.card", h("div.toolbar", sel), box));
  await load();
}
