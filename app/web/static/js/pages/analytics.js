import { h, clear, fill, gapi, fmtNum, tabs, statCard, lineChart, state } from "../core.js";

const CHARTS = [
  ["members", "📈 Mitgliederwachstum", "#5fb37a"], ["joins,leaves", "👋 Neue Mitglieder & Leaves", null, true], ["messages", "💬 Nachrichten", "#c8a45d", true],
  ["voice_minutes", "🎙️ Voice-Aktivität (Minuten)", "#7a8ba8", true], ["xp", "⭐ Vergebene XP", "#d9a441", true], ["coins", "🪙 Verdiente Coins", "#b89b5e", true],
  ["tickets_opened,tickets_closed", "🎫 Tickets", null, true], ["mod_actions,automod", "🛡️ Moderation & Automod", null, true],
  ["giveaways", "🎁 Giveaways", "#b58ab8", true], ["streams,stream_checkins", "📡 Streams & Check-ins", null, true],
  ["commands", "⌨️ Commands", "#6fa8a0", true], ["online", "🟢 Online-Mitglieder (Peak)", "#5fb37a"],
];
const LABELS = { joins: "Joins", leaves: "Leaves", tickets_opened: "Geöffnet", tickets_closed: "Geschlossen", mod_actions: "Mod-Aktionen", automod: "Automod",
  streams: "Streams", stream_checkins: "Check-ins" };

export async function render(root) {
  let range = localStorage.getItem("nova:range") || "7d";
  const body = h("div");
  const load = async () => {
    localStorage.setItem("nova:range", range);
    const d = await gapi(`/analytics?range=${range}`);
    const tt = d.totals;
    const canvases = CHARTS.map(() => h("canvas"));
    fill(body, 
      h("div.grid.g4", statCard("Mitglieder (max.)", fmtNum(tt.members), "👥"), statCard("Joins / Leaves", `${fmtNum(tt.joins)} / ${fmtNum(tt.leaves)}`, "👋", `Netto ${tt.joins - tt.leaves >= 0 ? "+" : ""}${fmtNum(tt.joins - tt.leaves)}`),
        statCard("Nachrichten", fmtNum(tt.messages), "💬"), statCard("Voice", `${fmtNum(Math.round(tt.voice_minutes / 60))} h`, "🎙️"),
        statCard("Tickets", fmtNum(tt.tickets_opened), "🎫", `${fmtNum(tt.tickets_closed)} geschlossen`), statCard("Moderation", fmtNum(tt.mod_actions), "🛡️", `${fmtNum(tt.automod)} Automod`),
        statCard("XP / Coins", `${fmtNum(tt.xp)} / ${fmtNum(tt.coins)}`, "⭐"), statCard("Streams", fmtNum(tt.streams), "📡", `Peak ${fmtNum(tt.stream_viewers)} Zuschauer`)),
      h("div.grid.g2", { style: { marginTop: "16px" } }, CHARTS.map(([_m, title], i) => h("div.glass.card", h("h3", title), h("div.chart-box", canvases[i])))));
    CHARTS.forEach(([metrics, _t, color, bar], i) => {
      const keys = metrics.split(",");
      lineChart(canvases[i], d.labels, keys.map((k) => ({ label: LABELS[k] || k, data: d.series[k], ...(color ? { color } : {}) })), { hourly: d.hourly, bar });
    });
  };
  fill(root, h("div.page-head", h("div", h("h2", "📊 Analytics"), h("p", "Wachstum, Aktivität, Moderation und Streams im Zeitverlauf."))),
    tabs([["24h", "24h"], ["7d", "7 Tage"], ["30d", "30 Tage"], ["90d", "90 Tage"], ["all", "All Time"]], range, (r) => { range = r; load(); }), body);
  await load();
}
