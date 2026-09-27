import { h, clear, fill, gapi, state, table, pager, userCell, fmtNum, fmtMinutes, pill, tabs, progressBar, statCard } from "../core.js";
import { moduleSettings, collection } from "../forms.js";

export async function render(root) {
  const admin = state.ctx.level === "admin";
  const body = h("div");
  const items = [["board", "🏆 Leaderboard"], ["achievements", "🎖️ Achievements"]];
  if (admin) items.push(["rewards", "🎁 Level-Belohnungen"], ["badges", "🏅 Badges"], ["settings", "⚙️ Level-Einstellungen"], ["profiles", "⚙️ Profile & Achievements"]);
  const show = (k) => ({
    board: async () => { const box = h("div"); fill(body, explainer(), box); await leaderboard(box, "xp"); }, achievements: () => achievements(body),
    rewards: () => collection(body, "level_rewards", { intro: "Rollen werden beim Erreichen des Levels automatisch vergeben (z. B. 5, 10, 25, 50, 100)." }),
    badges: () => collection(body, "badges", { intro: "Badges erscheinen im /profile. Automatisch per Regel oder manuell im Mitgliederprofil.",
      columns: { emoji: (r) => h("span", { style: { fontSize: "18px" } }, r.emoji) } }),
    settings: () => moduleSettings(body, "levels"), profiles: () => moduleSettings(body, "profiles"),
  })[k]();
  fill(root, h("div.page-head", h("div", h("h2", "⭐ Levelsystem"), h("p", "XP für Nachrichten, Voice, Reaktionen & Streams – mit Anti-Spam und Belohnungen."))),
    tabs(items, "board", show), body);
  show("board");
}

export async function leaderboard(el, by = "xp", withTabs = true) {
  let page = 1;
  const wrap = h("div");
  const load = async () => {
    const d = await gapi(`/leaderboard?by=${by}&page=${page}`);
    fill(wrap, table([
      { label: "#", render: (r) => h("b", r.rank <= 3 ? ["🥇", "🥈", "🥉"][r.rank - 1] : "#" + r.rank) },
      { label: "User", render: (r) => userCell(r.user) },
      { label: "Level", render: (r) => h("div", { style: { minWidth: "120px" } }, `Lv. ${r.level}`) },
      { label: "XP", render: (r) => fmtNum(r.xp) }, { label: "Coins", render: (r) => fmtNum(r.coins) },
      { label: "Voice", render: (r) => fmtMinutes(r.voice_minutes) }, { label: "Nachrichten", render: (r) => fmtNum(r.messages) },
    ], d.items, { onRow: (r) => { history.pushState({}, "", `/g/${state.guildId}/members/${r.user.id}`); dispatchEvent(new PopStateEvent("popstate")); }, empty: "Noch keine Daten" }),
    pager(page, 25, d.total, (p) => { page = p; load(); }));
  };
  const sel = h("select", [["xp", "⭐ XP"], ["coins", "💰 Coins"], ["voice_minutes", "🎙️ Voice"], ["messages", "💬 Nachrichten"], ["invites", "✉️ Einladungen"]]
    .map(([v, l]) => h("option", { value: v, selected: v === by }, l)));
  sel.addEventListener("change", () => { by = sel.value; page = 1; load(); });
  fill(el, h("div.glass.card", withTabs ? h("div.toolbar", sel) : null, wrap));
  await load();
}

// Verständliche Erklärung: wofür gibt es XP, wie viel braucht man, welche Belohnungen
function explainer() {
  const card = h("div.glass.card", { style: { marginBottom: "16px" } }, h("div.skeleton", { style: { height: "120px" } }));
  gapi("/levels/info").then((d) => {
    fill(card,
      h("div.card-head", h("h3", "So funktioniert XP"), h("span.muted", { style: { fontSize: "12.5px" } }, "Mitglieder sehen das gleiche mit /guide in Discord")),
      h("p.muted", { style: { margin: "0 0 16px" } }, "XP sind Punkte für Aktivität. Wer genug XP sammelt, steigt ein Level auf. Bestimmte Level schalten automatisch Rollen frei."),
      h("div.xp-steps",
        h("div", h("span", "Nachricht schreiben"), h("b", `${d.xp_min}–${d.xp_max} XP`), h("span", `max. alle ${d.cooldown} s (Anti-Spam)`)),
        h("div", h("span", "Voice-Chat"), h("b", `${d.voice_xp} XP / Min.`), h("span", d.voice_needs_others ? "nur mit anderen, nicht stumm" : "auch allein")),
        h("div", h("span", "Reaktion"), h("b", `${d.reaction_xp} XP`), h("span", "max. alle 15 s")),
        h("div", h("span", "Stream-Check-in"), h("b", `${d.stream_xp} XP`), h("span", "Button „Ich bin dabei“"))),
      h("div.grid.g2", { style: { marginTop: "18px" } },
        h("div", h("div.muted", { style: { fontSize: "12px", marginBottom: "8px" } }, "BENÖTIGTE XP (GESAMT)"),
          h("div.chips", d.table.map((r) => h("span.chip", `Level ${r.level}: `, h("b", fmtNum(r.xp)))))),
        h("div", h("div.muted", { style: { fontSize: "12px", marginBottom: "8px" } }, "BELOHNUNGEN"),
          d.rewards.length ? h("div.chips", d.rewards.map((r) => h("span.chip", h("i.swatch", { style: { background: r.color || "#6c6c74" } }), `Level ${r.level} → ${r.role}`)))
            : h("span.muted", "Noch keine – unter „Level-Belohnungen“ anlegen"))));
  }).catch(() => card.remove());
  return card;
}

async function achievements(el) {
  const d = await gapi("/achievements");
  fill(el, h("div.grid.g-auto", d.items.map((a) => h("div.glass.stat",
    h("div.ico", a.emoji), h("div.label", a.name), h("div.value", `${fmtNum(a.unlocked)}`), h("div.sub", a.description),
    h("div", { style: { marginTop: "10px" } }, progressBar(a.unlocked, d.members || 1)),
    h("div.sub", `${d.members ? ((a.unlocked / d.members) * 100).toFixed(1) : 0}% der Mitglieder`)))));
}
