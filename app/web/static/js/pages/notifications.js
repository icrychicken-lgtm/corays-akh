import { h, clear, fill, tabs } from "../core.js";
import { moduleSettings } from "../forms.js";

export async function render(root) {
  const body = h("div");
  const show = (k) => moduleSettings(body, k);
  fill(root, h("div.page-head", h("div", h("h2", "🔔 Benachrichtigungen"), h("p", "Videos, Shorts, Giveaways, Events, Boosts, Joins – plus Welcome, Boost-Danke und Geburtstage."))),
    tabs([["notifications", "🔔 Routing"], ["welcome", "👋 Welcome & Leave"], ["boost", "💜 Server-Boosts"], ["birthday", "🎂 Geburtstage"]], "notifications", show), body);
  show("notifications");
}
