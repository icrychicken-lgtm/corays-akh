import { h, fill, gapi, pill, debounce, emptyState, copy } from "../core.js";

// Übersicht aller Befehle – gruppiert, durchsuchbar, mit Hinweis ob das Modul aktiv ist
export async function render(root) {
  const d = await gapi("/features");
  let q = "";
  const list = h("div");
  const draw = () => {
    const groups = d.groups.map((g) => ({ ...g, commands: g.commands.filter((c) => !q || c.name.includes(q) || (c.description || "").toLowerCase().includes(q)) }))
      .filter((g) => g.commands.length);
    fill(list, groups.length ? groups.map((g) => h("div.glass.card", { style: { marginBottom: "16px" } },
      h("div.card-head", h("h3", g.label), h("span.muted", `${g.commands.length} Befehle`)),
      h("div.list", g.commands.map((c) => h("div.item",
        h("div.grow",
          h("div.title", h("span.mono", { style: { color: "var(--accent)", cursor: "pointer" }, title: "Kopieren", onclick: () => !c.context && copy("/" + c.name) },
            c.context ? c.name : "/" + c.name),
          c.params?.length ? h("span.muted.mono", { style: { fontSize: "12px" } }, "  " + c.params.map((p) => (p.required ? p.name : `[${p.name}]`)).join(" ")) : null),
          h("div.meta", c.description)),
        c.context ? pill("Kontextmenü") : null, c.admin ? pill("Team") : null,
        c.active ? null : pill("Modul aus", "red")))))) : emptyState("Kein Befehl gefunden", "⌕"));
  };
  const search = h("input", { type: "search", placeholder: "Befehl oder Beschreibung suchen …" });
  search.addEventListener("input", debounce(() => { q = search.value.toLowerCase().replace(/^\//, ""); draw(); }, 120));
  fill(root, h("div.page-head", h("div", h("h2", "Befehle & Funktionen"),
    h("p", `${d.total} Befehle. Klick auf einen Befehl kopiert ihn. „Team“ = nur für Mitglieder mit passenden Rechten, „Modul aus“ = unter Einstellungen → Module aktivieren.`))),
    h("div.toolbar", search), list);
  draw();
}
