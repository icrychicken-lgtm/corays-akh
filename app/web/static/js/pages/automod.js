import { h, clear, fill, tabs } from "../core.js";
import { moduleSettings, sendPanel } from "../forms.js";

export async function render(root) {
  const body = h("div");
  const panelBtn = h("button.btn", "📨 Verifizierungs-Panel senden");
  panelBtn.addEventListener("click", () => sendPanel("verification", panelBtn));
  const show = (k) => ({
    automod: () => moduleSettings(body, "automod", { intro: "Jeder Filter hat eigene Aktionen. Admins und Whitelist sind immer ausgenommen." }),
    antiraid: () => moduleSettings(body, "antiraid", { intro: "Erkennt ungewöhnliche Join-Wellen (z. B. 30 User in 20 Sekunden) und reagiert automatisch." }),
    verification: () => moduleSettings(body, "verification", { extra: h("div.row", { style: { marginBottom: "16px" } }, panelBtn) }),
  })[k]();
  fill(root, h("div.page-head", h("div", h("h2", "🤖 Automod & Sicherheit"), h("p", "Spam, Links, Scam, Bad Words, Mentions – plus Anti-Raid und Verifizierung."))),
    tabs([["automod", "🤖 Automod-Filter"], ["antiraid", "🚨 Anti-Raid"], ["verification", "✅ Verifizierung"]], "automod", show), body);
  show("automod");
}
