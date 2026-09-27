import { h, clear, fill, gapi, state, fmtNum, tabs, toast, withLoading, emptyState, onLive, progressBar, pill } from "../core.js";
import { moduleSettings } from "../forms.js";

const fmt = (s) => { if (!s) return "Live"; const m = Math.floor(s / 60), x = s % 60; return `${m}:${String(x).padStart(2, "0")}`; };

export async function render(root) {
  const body = h("div");
  const items = [["player", "🎵 Player"]];
  if (state.ctx.level === "admin") items.push(["settings", "⚙️ Einstellungen"]);
  const show = (k) => (k === "player" ? player(body) : moduleSettings(body, "music"));
  fill(root, h("div.page-head", h("div", h("h2", "🎵 Musik"), h("p", "Queue, Lautstärke, Loop, Shuffle – live gesteuert aus dem Dashboard."))), tabs(items, "player", show), body);
  show("player");
}

async function player(el) {
  const d = await gapi("/music");
  let st = d.state;
  const npBox = h("div.glass.card");
  const queueBox = h("div.glass.card");
  const ctl = (action, value) => gapi("/music/control", { method: "POST", body: { action, value } }).then((r) => { st = r.state; draw(); });

  const chSel = h("select", d.voice_channels.map((c) => h("option", { value: c.id, selected: c.id === st.channel_id }, `🔊 ${c.name} (${c.members})`)));
  const query = h("input", { type: "text", placeholder: "Song, Link oder Spotify-URL …" });
  const addBtn = h("button.btn.primary", "▶ Abspielen / Hinzufügen");
  const add = () => withLoading(addBtn, async () => {
    if (!query.value.trim()) return;
    const r = await gapi("/music/control", { method: "POST", body: { action: "play", query: query.value, channel_id: chSel.value } });
    st = r.state; query.value = ""; toast(`${r.added} Song(s) hinzugefügt`, "success"); draw();
  });
  addBtn.addEventListener("click", add);
  query.addEventListener("keydown", (e) => e.key === "Enter" && add());

  let posTimer;
  const draw = () => {
    clearInterval(posTimer);
    const c = st.current;
    const vol = h("input", { type: "range", min: 1, max: 150, value: st.volume || 60, style: { width: "160px" } });
    vol.addEventListener("change", () => ctl("volume", Number(vol.value)));
    const bar = h("div", progressBar(st.position || 0, c?.duration || 1));
    const time = h("span.muted.mono", c ? `${fmt(st.position || 0)} / ${fmt(c.duration)}` : "");
    fill(npBox, h("h3", "🎧 Now Playing ", st.connected ? pill(`🔊 ${st.channel}`, "green") : pill("Nicht verbunden")),
      c ? h("div.np", c.thumbnail ? h("img", { src: c.thumbnail, alt: "" }) : null, h("div.grow", { style: { flex: 1, minWidth: 0 } },
        h("p.t", h("a", { href: c.url, target: "_blank", rel: "noopener" }, c.title)), h("div.muted", `${st.listeners} Zuhörer im Channel`),
        h("div", { style: { marginTop: "10px" } }, bar), h("div.row.between", { style: { marginTop: "4px" } }, time, h("span.muted", `Loop: ${{ off: "aus", track: "Track", queue: "Queue" }[st.loop]}`))))
        : emptyState(st.connected ? "Queue ist leer" : "Der Bot spielt gerade nichts", "🎵"),
      st.connected ? h("div.player-controls",
        h("button.btn", { onclick: () => ctl("toggle") }, st.paused ? "▶ Fortsetzen" : "⏸ Pause"), h("button.btn", { onclick: () => ctl("skip") }, "⏭ Skip"),
        h("button.btn", { onclick: () => ctl("shuffle") }, "🔀 Shuffle"), h("button.btn", { onclick: () => ctl("loop") }, "🔁 Loop"),
        h("button.btn.danger", { onclick: () => ctl("stop") }, "⏹ Stop"), h("label.row", { style: { marginLeft: "auto" } }, "🔊", vol, `${st.volume}%`)) : null,
      h("div.hr"), h("div.form-grid", h("div.field", h("label", "Voice-Channel"), chSel), h("div.field", h("label", "Song / Link"), query)),
      h("div.row", { style: { justifyContent: "flex-end", marginTop: "12px" } }, addBtn));
    fill(queueBox, h("div.card-head", h("h3", "📜 Warteschlange ", h("span.muted", `· ${st.queue_length || 0}`)),
      st.queue?.length ? h("button.btn.sm.danger", { onclick: () => ctl("clear") }, "Leeren") : null),
      st.queue?.length ? h("div.list", st.queue.map((t, i) => h("div.item", h("span.muted.mono", String(i + 1).padStart(2, "0")),
        t.thumbnail ? h("img", { src: t.thumbnail, alt: "", style: { width: "48px", height: "36px", objectFit: "cover", borderRadius: "6px" } }) : null,
        h("div.grow", h("div.title", t.title), h("div.meta", fmt(t.duration))),
        i > 0 ? h("button.btn.ghost.sm", { title: "Nach oben", onclick: () => ctl("move", [i + 1, i]) }, "↑") : null,
        h("button.btn.ghost.sm", { title: "Entfernen", onclick: () => ctl("remove", i + 1) }, "✕")))) : emptyState("Nichts in der Warteschlange", "📭"));
    if (c && st.playing && c.duration) {
      let pos = st.position || 0;
      posTimer = setInterval(() => { pos++; bar.firstChild.firstChild.style.width = Math.min(100, (pos / c.duration) * 100) + "%"; time.textContent = `${fmt(pos)} / ${fmt(c.duration)}`; }, 1000);
    }
  };
  state.cleanup.push(() => clearInterval(posTimer));
  fill(el, h("div.grid.g2", npBox, queueBox));
  draw();
  onLive("music", (s) => { st = { ...st, ...s }; draw(); });
}
