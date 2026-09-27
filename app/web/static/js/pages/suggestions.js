import { h, clear, fill, gapi, state, fmtNum, fmtRel, tabs, pill, openModal, toast, withLoading, emptyState, onLive, userCell, statCard, debounce, progressBar } from "../core.js";
import { moduleSettings, sendPanel } from "../forms.js";

const ST = { pending: ["🟡 Offen", "yellow"], accepted: ["🟢 Angenommen", "green"], denied: ["🔴 Abgelehnt", "red"], considered: ["🤔 Wird überdacht", "blue"] };

export async function render(root) {
  const body = h("div");
  const items = [["list", "💡 Suggestions"], ["polls", "📊 Umfragen"]];
  if (state.ctx.level === "admin") items.push(["settings", "⚙️ Suggestion-Einstellungen"], ["poll_settings", "⚙️ Umfrage-Einstellungen"]);
  const panel = h("button.btn", "📨 Suggestion-Panel senden");
  panel.addEventListener("click", () => sendPanel("suggestions", panel));
  const show = (k) => ({ list: () => list(body), polls: () => polls(body),
    settings: () => moduleSettings(body, "suggestions", { extra: h("div.row", { style: { marginBottom: "16px" } }, panel) }),
    poll_settings: () => moduleSettings(body, "polls") })[k]();
  fill(root, h("div.page-head", h("div", h("h2", "💡 Suggestions & Umfragen"), h("p", "Community-Vorschläge mit Voting und Staff-Entscheidungen."))), tabs(items, "list", show), body);
  show("list");
}

async function list(el) {
  let status = "", q = "";
  const wrap = h("div");
  const load = async () => {
    const d = await gapi(`/suggestions?status=${status}&q=${encodeURIComponent(q)}`);
    const c = d.counts;
    fill(wrap, h("div.grid.g4", { style: { marginBottom: "16px" } }, ...Object.entries(ST).map(([k, [l]]) => statCard(l, fmtNum(c[k] || 0), l.split(" ")[0]))),
      d.items.length ? h("div.grid.g2", d.items.map((s) => {
        const total = s.upvotes + s.downvotes;
        return h("div.glass.card", h("div.row.between", h("b", `#${s.number}`), pill(...ST[s.status])),
          h("p", { style: { whiteSpace: "pre-wrap", margin: "10px 0" } }, s.content),
          h("div.row", h("span", `👍 ${s.upvotes}`), h("span", `👎 ${s.downvotes}`), h("div", { style: { flex: 1 } }, progressBar(s.upvotes, total || 1))),
          s.staff_reason ? h("div.note", { style: { marginTop: "10px" } }, h("div.meta", s.staff_name), s.staff_reason) : null,
          h("div.row.between", { style: { marginTop: "12px" } }, userCell(s.user, fmtRel(s.created_at)),
            h("div.row", s.url ? h("a.btn.sm.ghost", { href: s.url, target: "_blank", rel: "noopener" }, "Discord") : null, h("button.btn.sm", { onclick: () => decide(s, load) }, "Entscheiden"))));
      })) : h("div.glass.card", emptyState("Keine Vorschläge", "💡")));
  };
  const sel = h("select", h("option", { value: "" }, "Alle"), Object.entries(ST).map(([k, [l]]) => h("option", { value: k }, l)));
  sel.addEventListener("change", () => { status = sel.value; load(); });
  const search = h("input", { type: "search", placeholder: "Suchen …" });
  search.addEventListener("input", debounce(() => { q = search.value; load(); }));
  fill(el, h("div.toolbar", search, sel), wrap);
  onLive("suggestion", () => load());
  await load();
}

function decide(s, reload) {
  const reason = h("textarea", { placeholder: "Begründung (optional, wird im Embed angezeigt)" });
  const go = (st) => (e) => withLoading(e.currentTarget, async () => {
    await gapi(`/suggestions/${s.id}/status`, { method: "POST", body: { status: st, reason: reason.value } });
    toast("Status aktualisiert", "success"); m.close(); reload();
  });
  const m = openModal({ title: `Suggestion #${s.number}`, size: "sm", body: h("div.stack", h("div.note", s.content), h("div.field", h("label", "Begründung"), reason)),
    footer: [h("button.btn", { onclick: go("considered") }, "🤔 Überdenken"), h("button.btn.danger", { onclick: go("denied") }, "👎 Ablehnen"), h("button.btn.success", { onclick: go("accepted") }, "👍 Annehmen")] });
}

async function polls(el) {
  const d = await gapi("/polls");
  fill(el, d.items.length ? h("div.grid.g2", d.items.map((p) => {
    const total = p.results.reduce((a, b) => a + b, 0);
    return h("div.glass.card", h("div.row.between", h("b", p.question), pill(p.ended ? "beendet" : "läuft", p.ended ? "" : "green")),
      h("div.stack", { style: { marginTop: "12px" } }, p.options.map((o, i) => h("div", h("div.row.between", h("span", o), h("span.muted", `${p.results[i]} · ${total ? Math.round((p.results[i] / total) * 100) : 0}%`)), progressBar(p.results[i], total || 1)))),
      h("div.muted", { style: { marginTop: "10px", fontSize: "12px" } }, `${p.multiple ? "Mehrfachauswahl · " : ""}${p.anonymous ? "anonym · " : ""}${p.ends_at ? (p.ended ? "endete " : "endet ") + fmtRel(p.ends_at) : ""}`));
  })) : h("div.glass.card", emptyState("Noch keine Umfragen – erstelle eine mit /poll in Discord.", "📊")));
}
