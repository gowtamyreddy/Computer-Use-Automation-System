// Perception layer injected into every frame. ONE algorithm is used for both discovery
// (enumerate what is on screen for the model) and replay (resolve a recorded locator), so a
// target described at record time is resolved by exactly the same rules at replay time.
//
// It deliberately does not rely on ids, classes or test ids. It derives what an operator
// perceives: role, accessible name, the *visual* label next to a control (legacy table
// layouts put labels in a neighbouring <td>, not in <label for>), and grid coordinates
// (column header + row key) for data cells.
(() => {
  if (window.__cua) return;

  const norm = (s) => (s || "").replace(/\s+/g, " ").trim();
  const key = (s) => norm(s).replace(/[:*]+$/, "").trim().toLowerCase();
  const numericish = (s) => /^[\s$#\-+.,0-9()%]*$/.test(s);

  function visible(el) {
    if (!el.isConnected) return false;
    if (el.type === "hidden") return false;
    const st = getComputedStyle(el);
    if (st.visibility === "hidden" || st.display === "none") return false;
    return el.getClientRects().length > 0;
  }

  function role(el) {
    const explicit = el.getAttribute("role");
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    const t = (el.getAttribute("type") || "text").toLowerCase();
    if (tag === "a" && el.hasAttribute("href")) return "link";
    if (tag === "button") return "button";
    if (tag === "input") {
      if (["submit", "button", "reset", "image"].includes(t)) return "button";
      if (t === "checkbox") return "checkbox";
      if (t === "radio") return "radio";
      return "textbox";
    }
    if (tag === "select") return "combobox";
    if (tag === "textarea") return "textbox";
    if (/^h[1-6]$/.test(tag)) return "heading";
    if (tag === "th") return "columnheader";
    if (tag === "td") return "cell";
    return "text";
  }

  const INTERACTIVE = "a[href],button,input,select,textarea,[role=button],[role=link],[onclick]";
  const isControl = (el) => el.matches(INTERACTIVE) && !(el.tagName === "INPUT" && el.type === "hidden");

  function ownText(el) {
    return norm(el.innerText || el.textContent || "");
  }

  // Accessible name (simplified accname): aria-label, aria-labelledby, <label>, value/text, title.
  function accName(el) {
    const al = el.getAttribute("aria-label");
    if (al) return norm(al);
    const lb = el.getAttribute("aria-labelledby");
    if (lb) {
      const t = lb.split(/\s+/).map((id) => el.ownerDocument.getElementById(id)).filter(Boolean);
      if (t.length) return norm(t.map(ownText).join(" "));
    }
    if (el.id) {
      const l = el.ownerDocument.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) return ownText(l);
    }
    const wrap = el.closest("label");
    if (wrap) return ownText(wrap);
    const tag = el.tagName.toLowerCase();
    const t = (el.getAttribute("type") || "").toLowerCase();
    if (tag === "input" && ["submit", "button", "reset"].includes(t)) return norm(el.value);
    if (tag === "input" && t === "image") return norm(el.alt || el.title);
    if (["a", "button", "th", "td"].includes(tag) || /^h[1-6]$/.test(tag)) return ownText(el);
    return norm(el.title || el.placeholder || "");
  }

  // Visual label: the nearest non-empty text cell to the left in the same row; else the cell
  // directly above in the same column; else the preceding text node. This is how a human
  // reads "Member # [______]" in a 1998 table layout.
  function visualLabel(el) {
    const td = el.closest("td,th");
    if (td) {
      let sib = td.previousElementSibling;
      while (sib) {
        if (!sib.querySelector(INTERACTIVE)) {
          const t = ownText(sib);
          if (t) return t.replace(/[:*]+$/, "").trim();
        }
        sib = sib.previousElementSibling;
      }
      // Stacked layout (caption above the field) -- controls only; a data/caption cell never
      // inherits the caption of the row above it.
      const tr = td.parentElement;
      const prevRow = isControl(el) && tr && tr.previousElementSibling;
      if (prevRow && prevRow.cells && prevRow.cells[td.cellIndex]) {
        const t = ownText(prevRow.cells[td.cellIndex]);
        if (t && !prevRow.cells[td.cellIndex].querySelector(INTERACTIVE)) return t.replace(/[:*]+$/, "").trim();
      }
    }
    let n = el.previousSibling;
    while (n) {
      const t = norm(n.textContent);
      if (t) return t.replace(/[:*]+$/, "").trim();
      n = n.previousSibling;
    }
    return "";
  }

  // Grid coordinates for a cell: column header text + a row key (first non-numeric cell in the
  // row other than this one), e.g. {column:"Balance", row_key:"Share Savings"}.
  function gridCoords(el) {
    const td = el.closest("td,th");
    if (!td || !td.parentElement) return null;
    const tr = td.parentElement;
    const table = tr.closest("table");
    if (!table) return null;
    // Only real grids (a header row of <th> cells) get grid coordinates; label/value layouts use labels.
    const headerRow = Array.from(table.rows).find((r) => Array.from(r.cells).some((c) => c.tagName === "TH"));
    if (!headerRow || headerRow === tr || !headerRow.cells[td.cellIndex]) return null;
    const column = ownText(headerRow.cells[td.cellIndex]);
    let rowKey = "";
    for (const c of tr.cells) {
      if (c === td) continue;
      const t = ownText(c);
      if (t && !numericish(t) && !c.querySelector(INTERACTIVE)) { rowKey = t; break; }
    }
    if (!column) return null;
    return { column, row_key: rowKey };
  }

  function describe(el) {
    const tag = el.tagName.toLowerCase();
    const d = {
      tag,
      role: role(el),
      name: accName(el),
      label: isControl(el) || tag === "td" ? visualLabel(el) : "",
      input_type: tag === "input" ? (el.getAttribute("type") || "text").toLowerCase() : null,
      attrs: {},
      enabled: !el.disabled,
    };
    for (const a of ["name", "id", "href", "target"]) {
      const v = el.getAttribute(a);
      if (v) d.attrs[a] = a === "href" ? v.split("?")[0] : v; // never carry query strings (session ids)
    }
    if (tag === "input" || tag === "textarea") d.value = d.input_type === "password" ? (el.value ? "********" : "") : el.value;
    if (tag === "select") {
      d.value = el.options[el.selectedIndex] ? norm(el.options[el.selectedIndex].text) : "";
      d.options = Array.from(el.options).map((o) => norm(o.text));
    }
    if (el.form && el.form.getAttribute("action")) d.attrs.form_action = el.form.getAttribute("action").split("?")[0];
    if (!isControl(el)) {
      d.text = ownText(el).slice(0, 200);
      const g = gridCoords(el);
      if (g) d.grid = g;
      const st = getComputedStyle(el);
      d.emphasis = st.color === "rgb(204, 0, 0)" || el.classList.contains("err") ? "error" : null;
    }
    return d;
  }

  // Leaf-ish text carriers worth showing to the model / extracting from.
  function isTextLeaf(el) {
    if (isControl(el) || el.closest(INTERACTIVE)) return false;
    if (!/^(TD|TH|B|FONT|SPAN|DIV|P|PRE|LI|H[1-6]|STRONG|EM|CENTER)$/.test(el.tagName)) return false;
    const t = ownText(el);
    if (!t) return false;
    // keep the innermost element carrying this text
    for (const c of el.children) if (ownText(c) === t) return false;
    return !el.querySelector("table");
  }

  function snapshot(maxItems) {
    const out = [];
    document.querySelectorAll("[data-cua-ref]").forEach((e) => e.removeAttribute("data-cua-ref"));
    let i = 0;
    for (const el of document.body ? document.body.querySelectorAll("*") : []) {
      if (out.length >= maxItems) break;
      if (!(isControl(el) || isTextLeaf(el))) continue;
      if (!visible(el)) continue;
      const ref = String(i++);
      el.setAttribute("data-cua-ref", ref);
      out.push(Object.assign({ ref }, describe(el)));
    }
    return { title: document.title, url: location.href, items: out };
  }

  function candidates(loc) {
    const all = Array.from(document.body ? document.body.querySelectorAll("*") : []);
    const wantRole = loc.role || null;
    const roleOk = (el) => !wantRole || role(el) === wantRole;
    switch (loc.kind) {
      case "role_name":
        return all.filter((el) => roleOk(el) && (isControl(el) || isTextLeaf(el)) && key(accName(el)) === key(loc.name));
      case "label":
        return all.filter((el) => (isControl(el) || el.tagName === "TD") && roleOk(el) && key(visualLabel(el)) === key(loc.label));
      case "table_cell":
        return all.filter((el) => {
          if (el.tagName !== "TD") return false;
          const g = gridCoords(el);
          return g && key(g.column) === key(loc.column) && key(g.row_key) === key(loc.row_key);
        });
      case "text":
        return all.filter((el) => roleOk(el) && (isControl(el) || isTextLeaf(el)) && key(ownText(el)) === key(loc.text));
      case "css":
        try { return Array.from(document.querySelectorAll(loc.selector)); } catch (e) { return []; }
      default:
        return [];
    }
  }

  function resolve(loc, token) {
    document.querySelectorAll(`[data-cua-hit]`).forEach((e) => e.removeAttribute("data-cua-hit"));
    const hits = candidates(loc).filter(visible);
    hits.forEach((el) => el.setAttribute("data-cua-hit", token));
    return hits.map((el) => ({
      ref: el.getAttribute("data-cua-ref"),
      tag: el.tagName.toLowerCase(),
      input_type: el.tagName === "INPUT" ? (el.getAttribute("type") || "text").toLowerCase() : null,
      role: role(el),
    }));
  }

  function pageText() {
    return document.body ? norm(document.body.innerText) : "";
  }

  // Mark (or unmark) elements whose text matches sensitive patterns so screenshots can mask them.
  function markSensitive(patterns, labels, on) {
    const style = document.getElementById("__cua_mask");
    if (!on) { if (style) style.remove(); document.querySelectorAll("[data-cua-sensitive]").forEach((e) => e.removeAttribute("data-cua-sensitive")); return 0; }
    const res = patterns.map((p) => new RegExp(p));
    const labs = new Set(labels.map(key));
    let n = 0;
    for (const el of document.body ? document.body.querySelectorAll("*") : []) {
      if (!isTextLeaf(el) && !(el.tagName === "INPUT" && el.type !== "hidden")) continue;
      const t = el.tagName === "INPUT" ? el.value : ownText(el);
      const byLabel = el.tagName === "TD" && labs.has(key(visualLabel(el)));
      if (byLabel || res.some((r) => r.test(t))) { el.setAttribute("data-cua-sensitive", "1"); n++; }
    }
    if (!style && document.head) {
      const s = document.createElement("style");
      s.id = "__cua_mask";
      s.textContent = "[data-cua-sensitive]{background:#000!important;color:#000!important;}";
      document.head.appendChild(s);
    }
    return n;
  }

  function redactedHTML(patterns, labels) {
    markSensitive(patterns, labels, true);
    const clone = document.documentElement.cloneNode(true);
    markSensitive([], [], false);
    clone.querySelectorAll("[data-cua-sensitive]").forEach((e) => {
      if (e.tagName === "INPUT") e.setAttribute("value", "[REDACTED]"); else e.textContent = "[REDACTED]";
    });
    clone.querySelectorAll("#__cua_mask").forEach((e) => e.remove());
    clone.querySelectorAll("input[type=password]").forEach((e) => e.removeAttribute("value"));
    clone.querySelectorAll("input[type=hidden]").forEach((e) => {
      if (/sid|session|token|auth|csrf/i.test(e.getAttribute("name") || "")) e.setAttribute("value", "[REDACTED]");
    });
    return clone.outerHTML;
  }

  window.__cua = { snapshot, resolve, describe, pageText, markSensitive, redactedHTML };

  // Human-action capture: during a human-in-control window, the operator's clicks/changes are
  // described with the same perception rules and reported to the controller (redacted there).
  const report = (type, el) => {
    if (typeof window.__cuaHumanEvent !== "function") return;
    try {
      const d = describe(el);
      // values travel only to the controller, which redacts before storing (params -> {name})
      if (type !== "change") delete d.value;
      window.__cuaHumanEvent({ type, element: d, url: location.href.split("?")[0] });
    } catch (e) { /* never break the page */ }
  };
  document.addEventListener("click", (e) => { const t = e.target.closest(INTERACTIVE) || e.target; report("click", t); }, true);
  document.addEventListener("change", (e) => report("change", e.target), true);
})();
