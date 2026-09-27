import { h, clear, fill, gapi, state, fmtNum, tabs, statCard, table, userCell, confirmDialog, toast } from "../core.js";
import { moduleSettings, collection } from "../forms.js";
import { leaderboard } from "./levels.js";

export async function render(root) {
  const admin = state.ctx.level === "admin";
  const body = h("div");
  const items = [["overview", "Übersicht"], ["gangs", "Gangs"]];
  if (admin) items.push(["shop", "Shop"], ["quests", "Quests"], ["settings", "Economy & Hood-Economy"], ["quest_settings", "Quest-Einstellungen"], ["gang_settings", "Gang-Einstellungen"]);
  const show = (k) => ({
    overview: () => overview(body), gangs: () => gangs(body), gang_settings: () => moduleSettings(body, "gangs"),
    shop: () => collection(body, "shop_items", { intro: "Rollen, Farbrollen, Badges, kosmetische & Event-Items. Keine echten Geldtransaktionen.",
      columns: { emoji: (r) => h("span", { style: { fontSize: "18px" } }, r.emoji), price: (r) => fmtNum(r.price), stock: (r) => (r.stock < 0 ? "∞" : fmtNum(r.stock)) } }),
    quests: () => collection(body, "quests", { intro: "Tägliche & wöchentliche Aufgaben, z. B. „Schreibe 50 Nachrichten“ oder „Sei 60 Minuten im Voice“." }),
    settings: () => moduleSettings(body, "economy"), quest_settings: () => moduleSettings(body, "quests"),
  })[k]();
  fill(root, h("div.page-head", h("div", h("h2", "💰 Economy"), h("p", "Community-Währung, Shop, Daily/Weekly mit Streaks und Quests."))), tabs(items, "overview", show), body);
  show("overview");
}

async function gangs(el) {
  const d = await gapi("/gangs");
  const admin = state.ctx.level === "admin";
  fill(el, h("div.glass.card", h("div.card-head", h("h3", "Gang-Rangliste"), h("span.muted", { style: { fontSize: "12.5px" } }, "Power = XP aller Mitglieder + Gang-Kasse")),
    table([
      { label: "#", render: (g) => h("b", "#" + (d.items.indexOf(g) + 1)) },
      { label: "Gang", render: (g) => h("div", h("b", `${g.emoji} ${g.name}`), " ", h("span.muted.mono", `[${g.tag}]`)) },
      { label: "Leader", render: (g) => userCell(g.leader) }, { label: "Mitglieder", key: "members" },
      { label: "Power", render: (g) => fmtNum(g.power) }, { label: "Kasse", render: (g) => fmtNum(g.bank) },
      { label: "", cls: "actions", render: (g) => admin ? h("button.btn.sm.danger", { onclick: async () => {
        if (!(await confirmDialog({ title: `Gang „${g.name}“ auflösen?`, text: "Die Gang und ihre Kasse werden gelöscht.", typeToConfirm: g.name }))) return;
        await gapi(`/gangs/${g.id}`, { method: "DELETE" }); toast("Gang aufgelöst", "success"); gangs(el);
      } }, "Auflösen") : "" },
    ], d.items, { empty: "Noch keine Gangs – Mitglieder gründen sie mit /gang create" })));
}

async function overview(el) {
  const d = await gapi("/leaderboard?by=coins");
  const box = h("div");
  fill(el, h("div.grid.g3", { style: { marginBottom: "16px" } },
    statCard("Coins im Umlauf", fmtNum(d.economy.total_coins), "🪙"), statCard("XP gesamt", fmtNum(d.economy.total_xp), "⭐"), statCard("Profile", fmtNum(d.economy.profiles), "👥")), box);
  await leaderboard(box, "coins");
}
