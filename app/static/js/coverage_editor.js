// Reusable structured editor for a v2 clothing piece's editable guts:
// its `states` list and its per-state / per-part `coverage` map
// ({covered, revealing, description}), plus the piece name/description and
// a raw-json view of the whole piece. Mutates the passed `piece` object in
// place and calls opts.onChange() after every edit.
//
// Used both by the standalone clothing-piece editor and, per equipped
// piece, by the outfit editor — so an outfit's parts can be edited in one
// place instead of only via a link out.

function esc(s) {
  return String(s == null ? "" : s).replace(
    /[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c],
  );
}

// Rebuild an object with one key renamed, preserving insertion order.
function renameKey(obj, oldKey, newKey) {
  const out = {};
  Object.keys(obj).forEach((k) => {
    out[k === oldKey ? newKey : k] = obj[k];
  });
  return out;
}

export function mountCoverageEditor(root, piece, opts = {}) {
  const onChange = opts.onChange || (() => {});
  piece.properties = piece.properties || {};
  const props = piece.properties;
  if (!Array.isArray(props.states) || !props.states.length)
    props.states = ["on", "off"];
  if (!props.coverage || typeof props.coverage !== "object")
    props.coverage = {};

  // Keep a coverage bucket for every declared state; drop buckets for
  // states that no longer exist.
  function reconcile() {
    const cov = props.coverage;
    props.states.forEach((s) => {
      if (!cov[s] || typeof cov[s] !== "object") cov[s] = {};
    });
    Object.keys(cov).forEach((s) => {
      if (!props.states.includes(s)) delete cov[s];
    });
  }

  function partRowHTML(state, part) {
    const info = props.coverage[state][part] || {};
    return (
      `<div class="cov-part">` +
      `<div class="cov-part-top">` +
      `<input class="cov-part-name" data-part-name data-state="${esc(state)}"` +
      ` value="${esc(part)}" spellcheck="false" />` +
      `<label class="row toggle"><input type="checkbox" data-cov-covered` +
      ` data-state="${esc(state)}" data-part="${esc(part)}"` +
      `${info.covered ? " checked" : ""} /> covered</label>` +
      `<label class="row toggle"><input type="checkbox" data-cov-revealing` +
      ` data-state="${esc(state)}" data-part="${esc(part)}"` +
      `${info.revealing ? " checked" : ""} /> revealing</label>` +
      `<button type="button" class="ghost small" data-del-part` +
      ` data-state="${esc(state)}" data-part="${esc(part)}"` +
      ` title="remove part">✕</button>` +
      `</div>` +
      `<textarea class="cov-desc" rows="2" data-cov-desc` +
      ` data-state="${esc(state)}" data-part="${esc(part)}"` +
      ` placeholder="what this piece does to ${esc(part)} in the ${esc(state)} state">` +
      `${esc(info.description || "")}</textarea>` +
      `</div>`
    );
  }

  function stateBlockHTML(state) {
    const parts = props.coverage[state] || {};
    const rows = Object.keys(parts).map((p) => partRowHTML(state, p)).join("");
    return (
      `<div class="cov-state">` +
      `<div class="cov-state-head"><strong class="small">${esc(state)}</strong>` +
      `${props.states[0] === state ? ` <span class="muted small">(default)</span>` : ""}` +
      `</div>` +
      (rows || `<p class="muted small">No parts covered in this state.</p>`) +
      `<div class="row gap cov-addpart">` +
      `<input type="text" class="cov-newpart" data-new-part="${esc(state)}"` +
      ` placeholder="body part (e.g. chest)" />` +
      `<button type="button" class="ghost small" data-add-part="${esc(state)}">+ part</button>` +
      `</div>` +
      `</div>`
    );
  }

  function render() {
    reconcile();
    // coverageOnly = just the per-state coverage grid (for the standalone
    // clothing editor, whose form already owns name / description /
    // states). Recomputed each render so the states field stays current.
    const meta = opts.coverageOnly
      ? ""
      : `<label class="stack"><span>Piece name</span>` +
        `<input type="text" data-piece-name value="${esc(piece.name || "")}" /></label>` +
        `<label class="stack"><span>Piece description</span>` +
        `<textarea rows="2" data-piece-desc>${esc(piece.description || "")}</textarea></label>` +
        `<label class="stack"><span>States <span class="muted small">(comma-list; first is the default)</span></span>` +
        `<input type="text" data-states-input value="${esc(props.states.join(", "))}" /></label>`;
    const rawJson = opts.coverageOnly
      ? ""
      : `<details class="piece-json"><summary class="muted small">raw json</summary>` +
        `<textarea class="json-inline" data-rawjson rows="14" spellcheck="false"></textarea>` +
        `</details>`;
    root.innerHTML =
      `<div class="cov-editor">` +
      meta +
      `<div class="cov-states">` +
      props.states.map(stateBlockHTML).join("") +
      `</div>` +
      rawJson +
      `</div>`;
    refreshRawJson();
  }

  function refreshRawJson() {
    const ta = root.querySelector("[data-rawjson]");
    if (ta && document.activeElement !== ta)
      ta.value = JSON.stringify(piece, null, 2);
  }

  // A structural change re-renders the whole grid (root.innerHTML). When
  // that's triggered from a `change`/`blur` on an input *inside* the grid,
  // replacing the DOM synchronously would yank the node mid-event (the
  // browser throws "node to be removed is no longer a child…"). Defer the
  // re-render one tick so the triggering event fully settles first.
  function scheduleRender() {
    setTimeout(render, 0);
  }

  function changed(structural) {
    if (structural) scheduleRender();
    else refreshRawJson();
    onChange();
  }

  // Event delegation — typing in a description/name textarea mutates in
  // place with no re-render (so focus/caret are never lost); only
  // structural edits (add/remove/rename part, states change, json parse)
  // re-render.
  root.addEventListener("input", (ev) => {
    const t = ev.target;
    if (t.matches("[data-piece-name]")) {
      piece.name = t.value;
      changed(false);
    } else if (t.matches("[data-piece-desc]")) {
      piece.description = t.value;
      changed(false);
    } else if (t.matches("[data-cov-desc]")) {
      const { state, part } = t.dataset;
      const entry = (props.coverage[state] ||= {});
      (entry[part] ||= { covered: false }).description = t.value;
      changed(false);
    }
  });

  root.addEventListener("change", (ev) => {
    const t = ev.target;
    if (t.matches("[data-cov-covered]")) {
      const { state, part } = t.dataset;
      (props.coverage[state][part] ||= {}).covered = t.checked;
      changed(false);
    } else if (t.matches("[data-cov-revealing]")) {
      const { state, part } = t.dataset;
      const entry = (props.coverage[state][part] ||= { covered: false });
      if (t.checked) entry.revealing = true;
      else delete entry.revealing;
      changed(false);
    } else if (t.matches("[data-part-name]")) {
      const state = t.dataset.state;
      const oldName = t.dataset.part || "";
      const newName = t.value.trim();
      if (!newName || newName === oldName) {
        scheduleRender();
        return;
      }
      props.coverage[state] = renameKey(props.coverage[state], oldName, newName);
      changed(true);
    } else if (t.matches("[data-states-input]")) {
      props.states = t.value
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean);
      if (!props.states.length) props.states = ["on", "off"];
      changed(true);
    } else if (t.matches("[data-rawjson]")) {
      if (!t.value.trim()) return;
      try {
        const parsed = JSON.parse(t.value);
        Object.keys(piece).forEach((k) => delete piece[k]);
        Object.assign(piece, parsed);
        piece.properties = piece.properties || {};
        if (!Array.isArray(piece.properties.states) || !piece.properties.states.length)
          piece.properties.states = props.states;
        if (!piece.properties.coverage || typeof piece.properties.coverage !== "object")
          piece.properties.coverage = {};
        // Re-sync the closure's `props` alias with the parsed properties.
        // Clear it first — Object.assign copies keys but never deletes, so
        // without this a key the pasted JSON dropped (e.g. garment) would
        // linger in `props` (render path) while being absent from
        // piece.properties (save path), diverging the two.
        Object.keys(props).forEach((k) => delete props[k]);
        Object.assign(props, piece.properties);
        changed(true);
      } catch (e) {
        if (opts.onError) opts.onError("Invalid JSON: " + e.message);
      }
    }
  });

  root.addEventListener("click", (ev) => {
    const t = ev.target;
    if (t.matches("[data-add-part]")) {
      const state = t.dataset.addPart;
      const input = root.querySelector(`[data-new-part="${CSS.escape(state)}"]`);
      const name = (input && input.value.trim()) || "";
      if (!name) return;
      (props.coverage[state] ||= {})[name] = { covered: true, description: "" };
      changed(true);
    } else if (t.matches("[data-del-part]")) {
      const { state, part } = t.dataset;
      if (props.coverage[state]) delete props.coverage[state][part];
      changed(true);
    }
  });

  // Refresh the raw-json view when the user opens/focuses it.
  root.addEventListener("focusin", (ev) => {
    if (ev.target.matches("[data-rawjson]")) {
      if (document.activeElement === ev.target)
        ev.target.value = JSON.stringify(piece, null, 2);
    }
  });

  render();
  return { render, refreshRawJson };
}
