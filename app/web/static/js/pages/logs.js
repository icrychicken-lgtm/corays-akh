import { h, clear, fill, gapi, state, fmtDate, fmtNum, tabs, pill, pager, debounce, onLive, openModal, emptyState } from "../core.js";
import { moduleSettings } from "../forms.js";

const CAT = { message: ["💬", "blue"], member: ["👤", "green"], moderation: ["🛡️", "red"], automod: ["🤖", "yellow"], role: ["🎭", "purple"], channel: ["#️⃣", ""],
  voice: ["🔊", "blue"], ticket: ["🎫", "green"], giveaway: ["🎁", "purple"], security: ["🚨", "red"], stream: ["📡", "purple"], suggestion: ["💡", ""],
  application: ["📋", ""], event: ["🎉", ""] };

export async function render(root) {
  const body = h("div");
  const items = [["logs", "📝 Logs"]];
  if (state.ctx.level === "admin") items.push(["settings", "⚙️ Log-Einstellungen"]);
  const show = (k) => (k === "logs" ? logs(body) : moduleSettings(body, "logging"));
  fill(root, h("div.page-head", h("div", h("h2", "📝 Logs"), h("p", "Nachrichten, Member, Moderation, Rollen, Channels, Voice, Tickets, Giveaways – durchsuchbar und live."))),
    tabs(items, "logs", show), body);
  show("logs");
}

async function logs(el) {
  let page = 1, category = "", q = new URLSearchParams(location.search).get("q") || "";
  const listBox = h("div");
  const catSel = h("select", h("option", { value: "" }, "Alle Kategorien"));
  const row = (l) => {
    const [icon, color] = CAT[l.category] || ["•", ""];
    return h("div.item", { style: { cursor: "pointer" }, onclick: () => details(l) }, h("span", { style: { fontSize: "18px" } }, icon),
      h("div.grow", h("div.title", pill(l.category, color), " ", h("b", l.action), l.user_name ? ` · ${l.user_name}` : ""), h("div.meta", (l.content || "").slice(0, 160) || "—")),
      h("span.muted", { style: { whiteSpace: "nowrap" } }, fmtDate(l.created_at)));
  };
  const load = async () => {
    const d = await gapi(`/logs?category=${category}&q=${encodeURIComponent(q)}&page=${page}`);
    if (catSel.options.length === 1) Object.entries(d.categories).forEach(([c, n]) => catSel.append(h("option", { value: c }, `${(CAT[c] || ["•"])[0]} ${c} (${fmtNum(n)})`)));
    fill(listBox, d.items.length ? h("div.list#loglist", d.items.map(row)) : emptyState("Keine Logs gefunden", "📝"),
      pager(page, d.per_page, d.total, (p) => { page = p; load(); }));
  };
  catSel.addEventListener("change", () => { category = catSel.value; page = 1; load(); });
  const search = h("input", { type: "search", placeholder: "Inhalt, User, Aktion …", value: q });
  search.addEventListener("input", debounce(() => { q = search.value; page = 1; load(); }));
  fill(el, h("div.glass.card", h("div.toolbar", search, catSel, h("span.pill.green", "● Live")), listBox));
  onLive("log", (l) => {
    if (page !== 1 || q || (category && l.category !== category)) return;
    const list = listBox.querySelector("#loglist");
    if (!list) return load();
    list.prepend(row(l));
    while (list.children.length > 50) list.lastChild.remove();
  });
  await load();
}

function details(l) {
  openModal({ title: `${l.category} · ${l.action}`, body: h("div.stack",
    h("dl.kv", h("dt", "Zeit"), h("dd", fmtDate(l.created_at)), h("dt", "User"), h("dd", l.user_name ? `${l.user_name} (${l.user_id})` : "—"),
      h("dt", "Ziel"), h("dd", l.target_id || "—"), h("dt", "Channel"), h("dd", l.channel_id || "—")),
    l.content ? h("div.field", h("label", "Inhalt"), h("pre.code", l.content)) : null,
    Object.keys(l.details || {}).length ? h("div.field", h("label", "Details"), h("pre.code", JSON.stringify(l.details, null, 2))) : null) });
}
