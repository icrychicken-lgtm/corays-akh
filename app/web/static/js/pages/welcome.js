import { h, fill, gapi, tabs, toast, withLoading } from "../core.js";
import { moduleSettings } from "../forms.js";

export async function render(root) {
  const body = h("div");
  const test = h("button.btn", "Test-Nachricht senden");
  test.addEventListener("click", () => withLoading(test, async () => {
    await gapi("/welcome/test", { method: "POST" });
    toast("Test-Begrüßung gesendet – schau in den Willkommens-Channel", "success");
  }));
  const show = (k) => ({
    welcome: () => moduleSettings(body, "welcome", { title: "Willkommen, Auto-Rollen & Abschied",
      intro: "Begrüßung im Channel, private Nachricht, automatische Rollen für neue Mitglieder und Bots, Abschiedsnachricht. Platzhalter: {user} {username} {server} {count}",
      extra: h("div.callout", h("div.row.between", h("span", "Erst speichern, dann testen – der Bot schickt die Begrüßung so, als wärst du gerade beigetreten."), test)) }),
    boost: () => moduleSettings(body, "boost"),
    verification: () => moduleSettings(body, "verification"),
  })[k]();
  fill(root, h("div.page-head", h("div", h("h2", "Willkommen"), h("p", "Alles, was neue Mitglieder beim Beitritt erleben – auch per /welcome und /autorole in Discord einstellbar."))),
    tabs([["welcome", "Begrüßung & Auto-Rollen"], ["boost", "Server-Boosts"], ["verification", "Verifizierung"]], "welcome", show), body);
  show("welcome");
}
