import { h, clear, fill, gapi, state, fmtDate, fmtRel, tabs, pill, openModal, toast, withLoading, userCell, table } from "../core.js";
import { moduleSettings, collection } from "../forms.js";

const ST = { scheduled: ["📅 Geplant", "blue"], live: ["🔴 Läuft", "red"], ended: ["🏁 Beendet", ""], cancelled: ["✖ Abgesagt", "yellow"] };

export async function render(root) {
  const body = h("div");
  const items = [["events", "🎉 Events"]];
  if (state.ctx.level === "admin") items.push(["settings", "⚙️ Einstellungen"]);
  const show = (k) => (k === "events" ? collection(body, "events", {
    createLabel: "Event erstellen", intro: "Nach dem Erstellen „Veröffentlichen“ klicken – Countdown, RSVP-Button und Erinnerungen laufen automatisch.",
    columns: { status: (r) => pill(...(ST[r.status] || [r.status])), starts_at: (r) => h("div", fmtDate(r.starts_at), h("div.muted", fmtRel(r.starts_at))) },
    rowActions: (r) => [h("button.btn.sm", { onclick: () => participants(r) }, "👥"), r.status !== "cancelled" ? h("button.btn.sm", { onclick: () => finish(r) }, "🏆 Gewinner") : null],
  }) : moduleSettings(body, "events"));
  fill(root, h("div.page-head", h("div", h("h2", "🎉 Events"), h("p", "Server-Events mit Countdown, Teilnahme-Button, Erinnerungen und Belohnungen."))), tabs(items, "events", show), body);
  show("events");
}

async function participants(ev) {
  const d = await gapi(`/events/${ev.id}/participants`);
  openModal({ title: `👥 ${ev.name}`, body: table([{ label: "User", render: (u) => userCell(u) }, { label: "", render: (u) => u.winner ? pill("🏆 Gewinner", "green") : "" }], d.items, { empty: "Noch keine Teilnehmer" }) });
}

async function finish(ev) {
  const d = await gapi(`/events/${ev.id}/participants`);
  const chosen = new Set(d.items.filter((u) => u.winner).map((u) => u.id));
  const list = h("div.chips", d.items.map((u) => {
    const c = h("span.chip.toggle" + (chosen.has(u.id) ? ".on" : ""), { onclick: () => { chosen.has(u.id) ? chosen.delete(u.id) : chosen.add(u.id); c.classList.toggle("on"); } }, u.display || u.name);
    return c;
  }));
  const btn = h("button.btn.primary", "🏆 Event abschließen & belohnen");
  const m = openModal({ title: `Gewinner · ${ev.name}`, body: h("div.stack", h("p.muted", "Wähle die Gewinner aus den Teilnehmern. Sie erhalten die Event-Belohnung (Coins/XP), Quest-Fortschritt und Achievements."),
    d.items.length ? list : h("p.muted", "Keine Teilnehmer")), footer: [h("button.btn.ghost", { onclick: () => m.close() }, "Abbrechen"), btn] });
  btn.addEventListener("click", () => withLoading(btn, async () => {
    await gapi(`/c/events/${ev.id}/action/finish`, { method: "POST", body: { winner_ids: [...chosen] } });
    toast("Event abgeschlossen", "success"); m.close();
    history.replaceState({}, "", location.pathname); dispatchEvent(new PopStateEvent("popstate"));
  }));
}
