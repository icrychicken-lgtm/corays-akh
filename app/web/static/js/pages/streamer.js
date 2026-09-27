import { h, clear, fill, gapi, state, fmtNum, fmtDate, fmtRel, fmtDuration, tabs, table, pill, statCard, lineChart, chart, emptyState, onLive } from "../core.js";
import { moduleSettings, collection, sendPanel } from "../forms.js";

const PLATFORM = { twitch: ["🟣", "Twitch"], youtube: ["🔴", "YouTube"], kick: ["🟢", "Kick"] };

export async function render(root) {
  const admin = state.ctx.level === "admin";
  const body = h("div");
  const items = [["stats", "📈 Stream-Stats"]];
  if (admin) items.push(["streamers", "📡 Streamer verbinden"], ["settings", "⚙️ Live-Benachrichtigung"]);
  const show = (k) => ({
    stats: () => stats(body),
    streamers: () => collection(body, "streamers", { createLabel: "Streamer hinzufügen",
      intro: "API-Zugangsdaten unter Einstellungen → Integrationen hinterlegen. Twitch: Login-Name · YouTube: Channel-ID (UC…) oder @Handle · Kick: Slug.",
      columns: { platform: (r) => `${PLATFORM[r.platform][0]} ${PLATFORM[r.platform][1]}`, is_live: (r) => pill(r.is_live ? "🔴 LIVE" : "Offline", r.is_live ? "red" : ""),
        last_error: (r) => r.last_error ? h("span", { style: { color: "#fca5a5" }, title: r.last_error }, "⚠️ " + r.last_error.slice(0, 40)) : pill("OK", "green") } }),
    settings: () => {
      const btn = h("button.btn.primary", "Media-Panel senden");
      btn.addEventListener("click", () => sendPanel("media", btn));
      return moduleSettings(body, "streamer", { extra: h("div.callout.gold", h("div.row.between",
        h("div", h("b", "Medias verbinden sich selbst mit Twitch"), h("div.muted", { style: { marginTop: "4px" } },
          `Media-Rolle und Panel-Channel unten einstellen, speichern, dann Panel senden. Wichtig: In der Twitch-Developer-Console muss als Redirect-URL ${location.origin}/twitch/callback eingetragen sein.`)),
        btn)) });
    },
  })[k]();
  fill(root, h("div.page-head", h("div", h("h2", "📡 Streamer"), h("p", "Twitch, YouTube & Kick: Live-Posts, Rollen, Channel-Umbenennung und Statistiken."))), tabs(items, "stats", show), body);
  show("stats");
}

async function stats(el) {
  const list = await gapi("/c/streamers");
  if (!list.items.length) return fill(el, h("div.glass.card", emptyState("Noch kein Streamer verbunden. Füge unter „Streamer verbinden“ einen Kanal hinzu.", "📡")));
  let current = list.items[0].id, range = "30d";
  const box = h("div");
  const pick = h("select", list.items.map((s) => h("option", { value: s.id }, `${PLATFORM[s.platform][0]} ${s.display_name || s.channel}`)));
  pick.addEventListener("change", () => { current = Number(pick.value); load(); });
  const rangeTabs = tabs([["24h", "24 Stunden"], ["7d", "7 Tage"], ["30d", "30 Tage"], ["90d", "90 Tage"]], range, (r) => { range = r; load(); });
  rangeTabs.style.marginBottom = "0";
  const load = async () => {
    const d = await gapi(`/streamers/${current}/stats?range=${range}`);
    const s = d.streamer, sum = d.summary;
    const vCanvas = h("canvas"), gCanvas = h("canvas"), sCanvas = h("canvas");
    fill(box, 
      h("div.glass.hero", s.avatar_url ? h("img", { src: s.avatar_url, alt: "" }) : h("span.guild-icon", PLATFORM[s.platform][0]),
        h("div", h("h2", s.display_name || s.channel), h("p", `${PLATFORM[s.platform][1]} · ${fmtNum(s.total_streams)} Streams erfasst · zuletzt geprüft ${fmtRel(s.last_checked)}`)),
        h("div.spacer"), d.current ? pill(`🔴 LIVE · ${fmtNum(d.current.live_viewers)} Zuschauer`, "red") : pill("Offline")),
      d.current ? h("div.callout", h("b", "🔴 Aktueller Stream: "), d.current.title, " · ", d.current.game, " · seit ", fmtRel(d.current.started_at), ` · Peak ${fmtNum(d.current.peak_viewers)}`) : null,
      h("div.grid.g4", statCard("Aktuelle Zuschauer", d.current ? fmtNum(d.current.live_viewers) : "—", "👀"), statCard("Peak", fmtNum(sum.peak), "🏔️"),
        statCard("Ø Zuschauer", fmtNum(sum.avg_viewers), "📊"), statCard("Streams", fmtNum(sum.streams), "🎥", `${sum.total_hours} h · Ø ${sum.avg_duration_min} min`)),
      h("div.grid.g4", { style: { marginTop: "16px" } },
        statCard("Follower", sum.followers !== null ? fmtNum(sum.followers) : "—", "❤️", sum.followers === null ? "Twitch: nur mit Broadcaster-Token" : (sum.follower_growth !== null ? `${sum.follower_growth >= 0 ? "+" : ""}${fmtNum(sum.follower_growth)} im Zeitraum` : "")),
        statCard("Subscriber", sum.subscribers !== null ? fmtNum(sum.subscribers) : "—", "⭐", sum.subscriber_growth !== null ? `${sum.subscriber_growth >= 0 ? "+" : ""}${fmtNum(sum.subscriber_growth)} im Zeitraum` : "YouTube: öffentliche Abos"),
        statCard("Check-ins", fmtNum(sum.checkins), "🙋", "„Ich bin dabei“-Klicks"), statCard("Gesamt-Streams", fmtNum(s.total_streams), "🗂️")),
      h("div.grid.g2", { style: { marginTop: "16px" } },
        h("div.glass.card", h("h3", "👀 Zuschauerverlauf"), d.viewers.length ? h("div.chart-box", vCanvas) : emptyState("Keine Live-Daten im Zeitraum", "📉")),
        h("div.glass.card", h("h3", "📈 Wachstum"), d.growth.length ? h("div.chart-box", gCanvas) : emptyState("Wachstumsdaten werden stündlich erfasst", "🌱"))),
      h("div.glass.card", { style: { marginTop: "16px" } }, h("h3", "🎥 Streams"), d.sessions.length ? h("div.chart-box.sm", { style: { marginBottom: "14px" } }, sCanvas) : null,
        table([{ label: "Start", render: (x) => fmtDate(x.started_at) }, { label: "Titel", render: (x) => (x.title || "—").slice(0, 60) }, { label: "Kategorie", key: "game" },
          { label: "Dauer", render: (x) => fmtDuration(((x.ended_at ? new Date(x.ended_at) : new Date()) - new Date(x.started_at)) / 1000) },
          { label: "Peak", render: (x) => fmtNum(x.peak_viewers) }, { label: "Ø", render: (x) => fmtNum(x.avg_viewers) }, { label: "Check-ins", render: (x) => fmtNum(x.checkins) }],
          d.sessions, { empty: "Keine Streams im Zeitraum" })));
    const fmtT = (iso) => new Date(iso).toLocaleString(state.lang, range === "24h" ? { hour: "2-digit", minute: "2-digit" } : { day: "2-digit", month: "2-digit", hour: "2-digit" });
    if (d.viewers.length) chart(vCanvas, { type: "line", data: { labels: d.viewers.map((v) => fmtT(v.t)), datasets: [{ label: "Zuschauer", data: d.viewers.map((v) => v.v),
      borderColor: "#d25a5a", backgroundColor: "#d25a5a22", fill: true, tension: .3, pointRadius: 0, borderWidth: 2 }] },
      options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { x: { ticks: { maxTicksLimit: 8 }, grid: { display: false } }, y: { beginAtZero: true } } } });
    if (d.growth.length) chart(gCanvas, { type: "line", data: { labels: d.growth.map((g) => fmtT(g.t)), datasets: [
      { label: "Follower", data: d.growth.map((g) => g.followers), borderColor: "#c8a45d", tension: .3, pointRadius: 0, spanGaps: true },
      { label: "Subscriber", data: d.growth.map((g) => g.subscribers), borderColor: "#5fb37a", tension: .3, pointRadius: 0, spanGaps: true }] },
      options: { responsive: true, maintainAspectRatio: false, scales: { x: { ticks: { maxTicksLimit: 8 }, grid: { display: false } } } } });
    if (d.sessions.length) {
      const ss = [...d.sessions].reverse();
      chart(sCanvas, { type: "bar", data: { labels: ss.map((x) => new Date(x.started_at).toLocaleDateString(state.lang, { day: "2-digit", month: "2-digit" })),
        datasets: [{ label: "Peak", data: ss.map((x) => x.peak_viewers), backgroundColor: "#c8a45dcc", borderRadius: 6 }, { label: "Ø", data: ss.map((x) => x.avg_viewers), backgroundColor: "#5fb37acc", borderRadius: 6 }] },
        options: { responsive: true, maintainAspectRatio: false, scales: { x: { grid: { display: false } }, y: { beginAtZero: true } } } });
    }
  };
  fill(el, h("div.toolbar", pick, rangeTabs), box);
  onLive("stream", (x) => x.streamer_id === current && ["live", "offline"].includes(x.action) && load());
  await load();
}
