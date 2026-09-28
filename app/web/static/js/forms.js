// ───────────────────────── Formular-Renderer (schemabasiert) & Collections ─────────────────────────
import { h, clear, fill, state, gapi, api, toast, openModal, confirmDialog, withLoading, table, pill, emptyState, t, fmtDate, fmtRel, debounce } from "./core.js";

// ── Metadaten (Channels, Rollen, Badges) ──
export async function loadMeta(force = false) {
  if (!state.meta || force) state.meta = await gapi("/meta");
  return state.meta;
}
const CH_TYPES = {
  channel: ["text", "news", "forum"], voice: ["voice", "stage_voice"], category: ["category"], anychannel: null,
};
const CH_ICON = { text: "#", news: "📢", forum: "💬", voice: "🔊", stage_voice: "🎙️", category: "📁" };

function optionsFor(type) {
  const meta = state.meta || { channels: [], roles: [], badges: [] };
  const base = type.replace(/s$/, "");
  if (base === "role") return meta.roles.filter((r) => !r.default).map((r) => ({ value: r.id, label: r.name, color: r.color, sub: r.managed ? "Bot" : "" }));
  if (base === "badge") return meta.badges.map((b) => ({ value: b.id, label: `${b.emoji} ${b.name}` }));
  const allowed = CH_TYPES[base === "channel" ? "channel" : base];
  return meta.channels.filter((c) => !allowed || allowed.includes(c.type))
    .map((c) => ({ value: c.id, label: `${CH_ICON[c.type] || "#"} ${c.name}`, sub: c.category || "" }));
}

// Durchsuchbarer (Mehrfach-)Picker
export function picker({ options, value, multiple = false, placeholder = "Auswählen …", remote = null }) {
  let selected = multiple ? [...(value || [])].map(String) : (value ? [String(value)] : []);
  let opts = options || [];
  const known = new Map(opts.map((o) => [o.value, o]));
  const box = h("div.picker-box");
  const input = h("input", { type: "text", placeholder });
  const menu = h("div.picker-menu", { hidden: true });
  const wrap = h("div.picker", box, menu);
  let cursor = -1;

  const labelOf = (v) => known.get(v) || { value: v, label: v };
  const render = () => {
    clear(box);
    for (const v of selected) {
      const o = labelOf(v);
      box.append(h("span.chip", o.color ? h("i.swatch", { style: { background: o.color } }) : null, o.label,
        h("button.x", { type: "button", "aria-label": "Entfernen", onclick: (e) => { e.stopPropagation(); selected = selected.filter((x) => x !== v); render(); wrap.dispatchEvent(new Event("change")); } }, "×")));
    }
    if (multiple || !selected.length) box.append(input);
    else box.append(h("button.btn.ghost.sm", { type: "button", onclick: () => { selected = []; render(); wrap.dispatchEvent(new Event("change")); } }, "ändern"));
  };
  const showMenu = async () => {
    const q = input.value.toLowerCase();
    if (remote) {
      opts = await remote(q);
      opts.forEach((o) => known.set(o.value, o));
    }
    const list = opts.filter((o) => !selected.includes(o.value) && (!q || o.label.toLowerCase().includes(q) || o.value === q)).slice(0, 80);
    clear(menu);
    cursor = -1;
    if (!list.length) menu.append(h("div.muted", { style: { cursor: "default" } }, "Keine Treffer"));
    for (const o of list) {
      menu.append(h("div", { dataset: { v: o.value }, onmousedown: (e) => { e.preventDefault(); pick(o.value); } },
        o.color ? h("i.swatch", { style: { width: "10px", height: "10px", borderRadius: "50%", background: o.color } }) : null, o.label, o.sub ? h("small", o.sub) : null));
    }
    menu.hidden = false;
  };
  const pick = (v) => {
    selected = multiple ? [...selected, v] : [v];
    input.value = "";
    menu.hidden = true;
    render();
    wrap.dispatchEvent(new Event("change"));
    if (multiple) setTimeout(() => input.focus(), 0);
  };
  input.addEventListener("focus", showMenu);
  input.addEventListener("input", remote ? debounce(showMenu, 250) : showMenu);
  input.addEventListener("blur", () => setTimeout(() => (menu.hidden = true), 120));
  input.addEventListener("keydown", (e) => {
    const items = [...menu.querySelectorAll("div[data-v]")];
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      cursor = Math.max(0, Math.min(items.length - 1, cursor + (e.key === "ArrowDown" ? 1 : -1)));
      items.forEach((it, i) => it.classList.toggle("sel", i === cursor));
    } else if (e.key === "Enter" && items[cursor]) { e.preventDefault(); pick(items[cursor].dataset.v); }
    else if (e.key === "Backspace" && !input.value && selected.length && multiple) { selected.pop(); render(); }
  });
  box.addEventListener("click", () => input.focus());
  render();
  wrap.getValue = () => (multiple ? selected : selected[0] || null);
  return wrap;
}

const memberSearch = async (q) => {
  if (!q || q.length < 2) return [];
  const r = await gapi(`/members?q=${encodeURIComponent(q)}&per_page=15`, { quiet: true });
  return r.items.map((m) => ({ value: m.id, label: m.display || m.name, sub: m.name }));
};

function toLocalInput(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

// Ein einzelnes Eingabeelement je Feldtyp; gibt {el, get} zurück
export function widget(f, value) {
  const v = value === undefined ? f.default : value;
  switch (f.type) {
    case "bool": {
      const cb = h("input", { type: "checkbox", checked: !!v });
      return { el: h("label.switch", cb, h("span.track"), h("span", v ? "" : "")), get: () => cb.checked, input: cb };
    }
    case "int": case "float": {
      const i = h("input", { type: "number", value: v ?? "", step: f.type === "float" ? "0.1" : "1", min: f.min ?? "", max: f.max ?? "", placeholder: f.placeholder || "" });
      return { el: i, get: () => (i.value === "" ? null : Number(i.value)) };
    }
    case "text": {
      const i = h("textarea", { maxlength: f.max_len, placeholder: f.placeholder || "" });
      i.value = v ?? "";
      return { el: i, get: () => i.value };
    }
    case "color": case "color_opt": {
      const txt = h("input", { type: "text", value: v || "", placeholder: "#c8a45d", maxlength: 7 });
      const col = h("input", { type: "color", value: v || "#c8a45d" });
      col.addEventListener("input", () => (txt.value = col.value));
      txt.addEventListener("input", () => /^#[0-9a-f]{6}$/i.test(txt.value) && (col.value = txt.value));
      return { el: h("div.color-input", col, txt), get: () => txt.value.trim() || (f.type === "color" ? f.default : null) };
    }
    case "select": {
      const s = h("select", (f.options || []).map((o) => h("option", { value: o.value, selected: o.value === v }, o.label)));
      return { el: s, get: () => s.value };
    }
    case "multiselect": {
      let cur = new Set(v || []);
      const chips = h("div.chips", (f.options || []).map((o) => {
        const c = h("span.chip.toggle" + (cur.has(o.value) ? ".on" : ""), { role: "checkbox", tabindex: 0, onclick: () => {
          cur.has(o.value) ? cur.delete(o.value) : cur.add(o.value);
          c.classList.toggle("on");
        } }, o.label);
        return c;
      }));
      return { el: chips, get: () => [...cur] };
    }
    case "channel": case "voice": case "category": case "anychannel": case "role": case "badge": {
      const p = picker({ options: optionsFor(f.type), value: v, placeholder: "Auswählen …" });
      return { el: p, get: () => p.getValue() };
    }
    case "channels": case "roles": {
      const p = picker({ options: optionsFor(f.type), value: v, multiple: true, placeholder: "Hinzufügen …" });
      return { el: p, get: () => p.getValue() };
    }
    case "user": case "users": {
      const p = picker({ options: (Array.isArray(v) ? v : v ? [v] : []).map((x) => ({ value: String(x), label: String(x) })), value: v,
        multiple: f.type === "users", placeholder: "Name oder ID suchen …", remote: memberSearch });
      return { el: p, get: () => p.getValue() };
    }
    case "strlist": {
      const i = h("textarea", { placeholder: "Ein Eintrag pro Zeile", style: { minHeight: "110px" } });
      i.value = (v || []).join("\n");
      return { el: i, get: () => i.value.split("\n").map((x) => x.trim()).filter(Boolean) };
    }
    case "datetime": {
      const i = h("input", { type: "datetime-local", value: toLocalInput(v) });
      return { el: i, get: () => (i.value ? new Date(i.value).toISOString() : null) };
    }
    case "objlist": return objList(f, v || []);
    default: {
      const i = h("input", { type: f.type === "url" ? "url" : "text", value: v ?? "", maxlength: f.max_len, placeholder: f.placeholder || "" });
      return { el: i, get: () => i.value };
    }
  }
}

function objList(f, rows) {
  const list = h("div.stack");
  const items = [];
  const addRow = (row = {}) => {
    const sub = f.fields.map((sf) => ({ sf, w: widget(sf, row[sf.key]) }));
    const item = { sub };
    const el = h("div.form-group", { style: { marginBottom: 0, padding: "14px" } },
      h("div.form-grid", sub.map(({ sf, w }) => h("div.field", h("label", sf.label), w.el))),
      h("div.row", { style: { justifyContent: "flex-end", marginTop: "10px" } },
        h("button.btn.danger.sm", { type: "button", onclick: () => { items.splice(items.indexOf(item), 1); el.remove(); } }, "Entfernen")));
    item.el = el;
    items.push(item);
    list.append(el);
  };
  rows.forEach(addRow);
  const wrap = h("div.stack", list, h("button.btn.sm", { type: "button", style: { alignSelf: "flex-start" }, onclick: () => addRow() }, "＋ " + t("add")));
  return { el: wrap, get: () => items.map((it) => Object.fromEntries(it.sub.map(({ sf, w }) => [sf.key, w.get()]))) };
}

// Vollständiges Formular mit Gruppen. Felder mit `advanced` stehen eingeklappt unter „Mehr Optionen“.
export function renderForm(fields, values = {}) {
  const widgets = new Map();
  const groups = { basic: new Map(), advanced: new Map() };
  for (const f of fields) {
    const w = widget(f, values[f.key]);
    const errEl = h("div.err", { hidden: true });
    const wide = ["text", "objlist", "strlist", "multiselect", "channels", "roles", "users"].includes(f.type);
    const field = h("div.field" + (wide ? ".full" : ""), f.type === "bool"
      ? [h("div.row.between", h("label", { style: { fontWeight: 550 } }, f.label), w.el)]
      : [h("label", f.label), w.el], f.help ? h("div.help", f.help) : null, errEl);
    widgets.set(f.key, { w, field, errEl, advanced: !!f.advanced });
    const bucket = groups[f.advanced ? "advanced" : "basic"];
    const g = f.group || "";
    if (!bucket.has(g)) bucket.set(g, []);
    bucket.get(g).push(field);
  }
  const section = (map) => [...map.entries()].map(([g, els]) => h("div.form-group", g ? h("h4", g) : null, h("div.form-grid", els)));
  const moreCount = [...groups.advanced.values()].reduce((n, els) => n + els.length, 0);
  const more = h("div", { hidden: true }, section(groups.advanced));
  const moreBtn = h("button.btn.ghost", { type: "button", style: { width: "100%", marginBottom: "16px" } });
  const setMore = (open) => { more.hidden = !open; moreBtn.textContent = open ? "▲ Weniger anzeigen" : `⚙️ Mehr Optionen anzeigen (${moreCount})`; };
  moreBtn.addEventListener("click", () => setMore(more.hidden));
  setMore(false);
  const el = h("div", section(groups.basic), moreCount ? [moreBtn, more] : null);
  const openMore = () => setMore(true);
  return {
    el,
    getValues: () => Object.fromEntries([...widgets.entries()].map(([k, { w }]) => [k, w.get()])),
    setErrors(errors = {}) {
      for (const [k, { field, errEl }] of widgets) {
        const msg = errors[k];
        field.classList.toggle("invalid", !!msg);
        errEl.hidden = !msg;
        errEl.textContent = msg || "";
      }
      const first = Object.keys(errors)[0];
      if (first && widgets.get(first)?.advanced) openMore();  // Fehler in eingeklapptem Feld sichtbar machen
      if (first) widgets.get(first)?.field.scrollIntoView({ behavior: "smooth", block: "center" });
    },
  };
}

// ── Modul-Einstellungen (Toggle + Formular) ──
let moduleCache = null;
export async function modules(force = false) {
  if (!moduleCache || force || moduleCache.guild !== state.guildId) moduleCache = { guild: state.guildId, list: await gapi("/modules") };
  return moduleCache.list;
}

export async function moduleSettings(container, key, { title, intro, extra } = {}) {
  fill(container, h("div.skeleton", { style: { height: "300px" } }));
  await loadMeta();
  const list = await modules();
  const m = list.find((x) => x.key === key);
  if (!m) return fill(container, emptyState("Modul nicht gefunden"));
  const form = renderForm(m.fields, m.settings);
  const toggle = h("input", { type: "checkbox", checked: m.enabled });
  const saveBtn = h("button.btn.primary", t("save"));
  saveBtn.addEventListener("click", () => withLoading(saveBtn, async () => {
    try {
      const res = await gapi(`/modules/${key}`, { method: "PUT", body: { settings: form.getValues() } });
      m.settings = res.settings;
      form.setErrors({});
      toast(`${m.name}: ${t("saved")}`, "success");
    } catch (e) { form.setErrors(e.errors || {}); }
  }));
  toggle.addEventListener("change", async () => {
    try {
      const res = await gapi(`/modules/${key}`, { method: "PUT", body: { enabled: toggle.checked } });
      m.enabled = res.enabled;
      state.ctx.modules[key] = res.enabled;
      toast(`${m.name} ${res.enabled ? "aktiviert" : "deaktiviert"}`, res.enabled ? "success" : "warning");
      document.dispatchEvent(new CustomEvent("modules-changed"));
    } catch { toggle.checked = !toggle.checked; }
  });
  fill(container, 
    h("div.glass.card", { style: { marginBottom: "16px" } },
      h("div.row.between",
        h("div.row", h("div.mi", { style: { width: "40px", height: "40px", borderRadius: "12px", display: "grid", placeItems: "center", background: "rgba(var(--accent-rgb),.14)", fontSize: "19px" } }, m.icon),
          h("div", h("b", title || m.name), h("div.muted", intro || m.description))),
        m.toggleable ? h("label.switch", toggle, h("span.track"), h("span", m.enabled ? t("enabled") : t("disabled"))) : null)),
    extra || null,
    m.fields.length ? form.el : emptyState("Dieses Modul hat keine weiteren Einstellungen.", "⚙️"),
    m.fields.length ? h("div.form-actions", h("div.form-actions.floating", saveBtn)) : null,
  );
  toggle.addEventListener("change", () => toggle.parentElement.lastChild.textContent = toggle.checked ? t("enabled") : t("disabled"));
}

// ── Generische Collection (Tabelle + Modal-Formular) ──
export async function collection(container, key, { columns: colRender = {}, rowActions, onLoaded, createLabel, intro } = {}) {
  fill(container, h("div.skeleton", { style: { height: "220px" } }));
  await loadMeta();
  const data = await gapi(`/c/${key}`);
  const c = data.collection;
  let items = data.items;
  const fieldMap = Object.fromEntries(c.fields.map((f) => [f.key, f]));

  const cellValue = (row, col) => {
    if (colRender[col]) return colRender[col](row);
    const f = fieldMap[col];
    const v = row[col];
    if (typeof v === "boolean") return pill(v ? t("enabled") : t("disabled"), v ? "green" : "");
    if (v === null || v === undefined || v === "") return h("span.muted", "—");
    if (f && ["role", "channel", "category", "voice", "anychannel"].includes(f.type)) {
      const meta = state.meta;
      const o = (f.type === "role" ? meta.roles : meta.channels).find((x) => x.id === String(v));
      return o ? (f.type === "role" ? h("span.chip", h("i.swatch", { style: { background: o.color || "#99a" } }), o.name) : "#" + o.name) : h("span.muted", "gelöscht");
    }
    if (f && f.type === "select") return (f.options.find((o) => o.value === v) || {}).label || v;
    if (col.endsWith("_at") || col === "last_checked") return fmtRel(v);
    return String(v);
  };

  const openEditor = (row = null) => {
    const form = renderForm(c.fields, row || {});
    const save = h("button.btn.primary", row ? t("save") : t("create"));
    const m = openModal({ title: `${c.title} · ${row ? t("edit") : t("create")}`, body: form.el, size: "lg",
      footer: [h("button.btn.ghost", { onclick: () => m.close() }, t("cancel")), save] });
    save.addEventListener("click", () => withLoading(save, async () => {
      try {
        const res = await gapi(row ? `/c/${key}/${row.id}` : `/c/${key}`, { method: row ? "PUT" : "POST", body: form.getValues() });
        items = row ? items.map((x) => (x.id === row.id ? res : x)) : [...items, res];
        m.close();
        toast(t("saved"), "success");
        draw();
      } catch (e) { form.setErrors(e.errors || {}); }
    }));
  };

  const runAction = async (row, a, btn) => withLoading(btn, async () => {
    const res = await gapi(`/c/${key}/${row.id}/action/${a.key}`, { method: "POST", body: {} });
    if (res && res.id) items = items.map((x) => (x.id === row.id ? res : x));
    toast(`${a.label}: erledigt`, "success");
    draw();
  });

  const draw = () => {
    const cols = c.columns.map((col) => ({ key: col, label: fieldMap[col]?.label?.split(" (")[0] || { is_live: "Status", last_checked: "Geprüft", last_error: "Fehler", display_name: "Name", uses: "Nutzungen", message_id: "Gepostet", status: "Status" }[col] || col,
      render: (r) => cellValue(r, col) }));
    cols.push({ label: "", cls: "actions", render: (r) => h("div.row", { style: { justifyContent: "flex-end", gap: "6px" } },
      ...(c.actions || []).map((a) => { const b = h("button.btn.sm", { onclick: () => runAction(r, a, b) }, a.label); return b; }),
      ...(rowActions ? rowActions(r, () => draw()) : []),
      h("button.btn.sm", { onclick: () => openEditor(r) }, t("edit")),
      h("button.btn.sm.danger", { onclick: async () => {
        if (!(await confirmDialog({ title: `${c.title}: ${t("delete")}?`, text: "Dieser Eintrag wird dauerhaft entfernt." }))) return;
        await gapi(`/c/${key}/${r.id}`, { method: "DELETE" });
        items = items.filter((x) => x.id !== r.id);
        toast("Gelöscht", "success");
        draw();
      } }, t("delete"))) });
    fill(container, h("div.glass.card",
      h("div.card-head", h("div", h("h3", c.title, h("span.muted", `· ${items.length}`)), intro ? h("div.muted", { style: { fontSize: "12.5px" } }, intro) : null),
        h("button.btn.primary", { onclick: () => openEditor() }, "＋ " + (createLabel || t("create")))),
      table(cols, items, { onRow: (r) => openEditor(r), empty: "Noch keine Einträge – lege den ersten an." })));
    onLoaded && onLoaded(items);
  };
  draw();
}

export async function sendPanel(kind, btn) {
  return withLoading(btn, async () => {
    const r = await gapi(`/panels/${kind}`, { method: "POST" });
    toast("Panel gesendet", "success");
    if (r.url) window.open(r.url, "_blank", "noopener");
  });
}
