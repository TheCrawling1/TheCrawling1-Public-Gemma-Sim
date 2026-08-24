// Character editor: glues editor.js helpers to the character-specific
// sections (personality grid, body parts table, owned outfits, portrait
// upload).

import {
  readBootstrap,
  setStatus,
  collectForm,
  fillForm,
  saveEntity,
  deleteEntity,
  wireTabs,
  wireJSONTab,
} from "./editor.js";

const boot = readBootstrap();
let entity =
  boot.entity || {
    id: "",
    type: "character",
    name: "",
    description: "",
    tags: [],
    properties: {
      current_outfit: "",
      outfits: [],
      personality: {},
      body_parts: {},
      can_talk: true,
      can_move: true,
      can_edit_world: false,
      first_message: "",
    },
    example_text: "",
    children: [],
  };
const isNew = boot.isNew;
const form = document.getElementById("editor-form");

// --- Personality -----------------------------------------------------------
function renderPersonality() {
  const grid = document.getElementById("personality-grid");
  grid.innerHTML = "";
  const stats = (entity.properties && entity.properties.personality) || {};
  for (const [key, val] of Object.entries(stats)) {
    const wrap = document.createElement("label");
    wrap.className = "personality-row";
    wrap.innerHTML = `
      <span class="trait-key">${esc(key)}</span>
      <input type="range" min="0" max="100" value="${Number(val) || 0}"
             data-personality="${esc(key)}" />
      <span class="trait-val">${Number(val) || 0}</span>
      <button type="button" class="ghost xs" data-remove-personality="${esc(key)}" title="Remove">×</button>
    `;
    grid.appendChild(wrap);
  }
  grid.querySelectorAll('input[type="range"]').forEach((sl) => {
    sl.addEventListener("input", (ev) => {
      const k = ev.target.dataset.personality;
      const v = Number(ev.target.value);
      entity.properties.personality[k] = v;
      ev.target.parentElement.querySelector(".trait-val").textContent = v;
    });
  });
  grid.querySelectorAll("[data-remove-personality]").forEach((btn) => {
    btn.addEventListener("click", () => {
      delete entity.properties.personality[btn.dataset.removePersonality];
      renderPersonality();
    });
  });
}

document.getElementById("add-personality").addEventListener("click", () => {
  const inp = document.getElementById("new-personality-key");
  const k = (inp.value || "").trim();
  if (!k) return;
  entity.properties = entity.properties || {};
  entity.properties.personality = entity.properties.personality || {};
  entity.properties.personality[k] = 50;
  inp.value = "";
  renderPersonality();
});

// --- Body parts ------------------------------------------------------------
function renderBodyParts() {
  const root = document.getElementById("bodyparts-list");
  root.innerHTML = "";
  const parts = (entity.properties && entity.properties.body_parts) || {};
  for (const [key, def] of Object.entries(parts)) {
    const card = document.createElement("div");
    card.className = "bodypart-card";
    card.innerHTML = `
      <div class="bodypart-head">
        <strong>${esc(key)}</strong>
        <button type="button" class="ghost xs" data-remove-bodypart="${esc(key)}">Remove</button>
      </div>
      <label class="stack">
        <span class="muted small">Base description (when uncovered)</span>
        <textarea rows="2" data-bp="${esc(key)}.base">${esc(def.base || "")}</textarea>
      </label>
      <label class="stack">
        <span class="muted small">Clothed description (when covered)</span>
        <textarea rows="2" data-bp="${esc(key)}.clothed_base">${esc(def.clothed_base || "")}</textarea>
      </label>
      <label class="row toggle">
        <input type="checkbox" data-bp="${esc(key)}.covered" ${def.covered ? "checked" : ""} />
        <span>Covered by default</span>
      </label>
    `;
    root.appendChild(card);
  }
  root.querySelectorAll("[data-bp]").forEach((el) => {
    el.addEventListener("input", () => {
      const [k, sub] = el.dataset.bp.split(".");
      entity.properties.body_parts = entity.properties.body_parts || {};
      entity.properties.body_parts[k] = entity.properties.body_parts[k] || {};
      entity.properties.body_parts[k][sub] = el.type === "checkbox" ? el.checked : el.value;
    });
  });
  root.querySelectorAll("[data-remove-bodypart]").forEach((btn) => {
    btn.addEventListener("click", () => {
      delete entity.properties.body_parts[btn.dataset.removeBodypart];
      renderBodyParts();
    });
  });
}

document.getElementById("add-bodypart").addEventListener("click", () => {
  const inp = document.getElementById("new-bodypart-key");
  const k = (inp.value || "").trim();
  if (!k) return;
  entity.properties = entity.properties || {};
  entity.properties.body_parts = entity.properties.body_parts || {};
  entity.properties.body_parts[k] = { base: "", clothed_base: "", covered: false };
  inp.value = "";
  renderBodyParts();
});

// --- Owned outfits ---------------------------------------------------------
function renderOutfits() {
  const list = document.getElementById("owned-outfits");
  list.innerHTML = "";
  const outfits = (entity.properties && entity.properties.outfits) || [];
  outfits.forEach((id, idx) => {
    const li = document.createElement("li");
    li.innerHTML = `
      <a href="/studio/outfit/${esc(id)}" target="_blank" rel="noopener">${esc(id)}</a>
      <button type="button" class="ghost xs" data-remove-outfit="${idx}">Remove</button>
    `;
    list.appendChild(li);
  });
  list.querySelectorAll("[data-remove-outfit]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const i = Number(btn.dataset.removeOutfit);
      entity.properties.outfits.splice(i, 1);
      renderOutfits();
    });
  });
}

document.getElementById("add-outfit-existing").addEventListener("click", () => {
  const sel = document.getElementById("add-outfit-select");
  const id = sel.value;
  if (!id) return;
  entity.properties = entity.properties || {};
  entity.properties.outfits = entity.properties.outfits || [];
  if (!entity.properties.outfits.includes(id)) entity.properties.outfits.push(id);
  sel.value = "";
  renderOutfits();
});

// --- Worn (v2 slot map) -----------------------------------------------------
const CLOTHING = JSON.parse(
  document.getElementById("clothing-catalog")?.textContent || "[]",
);
const OUTFIT_EQUIPS = JSON.parse(
  document.getElementById("outfit-equips")?.textContent || "{}",
);
const WORN_SLOTS = [
  "top", "bottom", "bra", "underwear", "pantyhose", "gloves",
  "legwear", "shoes", "head", "face", "neck", "back", "overlay",
];
const SLOT_ACCEPTS = { underwear: ["underwear", "panties"] };
const pieceById = (id) => CLOTHING.find((c) => c.id === id);
const firstState = (id) => {
  const p = pieceById(id);
  return (p && p.states && p.states[0]) || "on";
};

function wornPiecesForSlot(slot, owner) {
  const accept = SLOT_ACCEPTS[slot] || [slot];
  return CLOTHING.filter((c) => accept.includes(c.slot))
    .filter((c) => !c.owner || !owner || c.owner === owner)
    .sort((a, b) => {
      const ao = a.owner === owner ? 0 : 1;
      const bo = b.owner === owner ? 0 : 1;
      return ao - bo || (a.name || a.id).localeCompare(b.name || b.id);
    });
}

function stateOptions(pieceId, cur) {
  const p = pieceById(pieceId);
  const states = (p && p.states) || ["on", "off"];
  return states
    .map((s) => `<option value="${esc(s)}"${s === cur ? " selected" : ""}>${esc(s)}</option>`)
    .join("");
}

function renderWorn() {
  const root = document.getElementById("worn-list");
  if (!root) return;
  root.innerHTML = "";
  entity.properties = entity.properties || {};
  const worn = (entity.properties.worn = entity.properties.worn || {});
  const owner = entity.id || "";
  WORN_SLOTS.forEach((slot) => {
    const cur = worn[slot] || null;
    const curPiece = (cur && cur.piece) || "";
    const curState = (cur && cur.state) || "";
    const pieces = wornPiecesForSlot(slot, owner);
    const opts =
      `<option value="">— none —</option>` +
      pieces
        .map(
          (p) =>
            `<option value="${esc(p.id)}"${p.id === curPiece ? " selected" : ""}>` +
            `${esc(p.name || p.id)}${p.owner ? "" : " (shared)"}</option>`,
        )
        .join("") +
      (curPiece && !pieces.some((p) => p.id === curPiece)
        ? `<option value="${esc(curPiece)}" selected>${esc(curPiece)} (not in list)</option>`
        : "");
    const row = document.createElement("div");
    row.className = "stack equip-row";
    row.innerHTML =
      `<span>${slot}</span>` +
      `<div class="row gap">` +
      `<select data-worn-piece="${slot}" style="flex:2">${opts}</select>` +
      `<select data-worn-state="${slot}" style="flex:1"${curPiece ? "" : " disabled"}>` +
      `${curPiece ? stateOptions(curPiece, curState) : ""}</select>` +
      `</div>`;
    root.appendChild(row);
  });
  root.querySelectorAll("[data-worn-piece]").forEach((sel) => {
    sel.addEventListener("change", () => {
      const slot = sel.dataset.wornPiece;
      if (sel.value) {
        entity.properties.worn[slot] = {
          piece: sel.value,
          state: firstState(sel.value),
        };
      } else {
        delete entity.properties.worn[slot];
      }
      renderWorn();
      schedulePreview();
    });
  });
  root.querySelectorAll("[data-worn-state]").forEach((sel) => {
    sel.addEventListener("change", () => {
      const slot = sel.dataset.wornState;
      if (entity.properties.worn[slot]) {
        entity.properties.worn[slot].state = sel.value;
      }
      schedulePreview();
    });
  });
}

function populatePresetSelect() {
  const sel = document.getElementById("apply-preset-select");
  if (!sel) return;
  const owned = new Set((entity.properties && entity.properties.outfits) || []);
  const ids = Object.keys(OUTFIT_EQUIPS).sort((a, b) => {
    const ao = owned.has(a) ? 0 : 1;
    const bo = owned.has(b) ? 0 : 1;
    return ao - bo || a.localeCompare(b);
  });
  sel.innerHTML =
    `<option value="">— choose an outfit bundle —</option>` +
    ids
      .map((id) => {
        const o = OUTFIT_EQUIPS[id];
        const own = owned.has(id) ? " ★" : "";
        return `<option value="${esc(id)}">${esc(o.name || id)}${own}</option>`;
      })
      .join("");
}

document.getElementById("apply-preset")?.addEventListener("click", () => {
  const sel = document.getElementById("apply-preset-select");
  const id = sel && sel.value;
  const note = document.getElementById("worn-preset-note");
  if (!id || !OUTFIT_EQUIPS[id]) return;
  const equips = OUTFIT_EQUIPS[id].equips || {};
  entity.properties = entity.properties || {};
  const layer = document.getElementById("apply-layer")?.checked;
  // Replace mode starts from an empty map; layer mode keeps the current
  // worn slots and only overwrites the ones this bundle defines.
  const worn = layer ? { ...(entity.properties.worn || {}) } : {};
  const slots = Object.entries(equips).map(([slot, pieceId]) => {
    worn[slot] = { piece: pieceId, state: firstState(pieceId) };
    return slot;
  });
  entity.properties.worn = worn;
  renderWorn();
  if (note)
    note.textContent = layer
      ? `Layered "${OUTFIT_EQUIPS[id].name || id}" onto ${slots.length} slot(s): ${slots.join(", ")}. Save to persist.`
      : `Applied "${OUTFIT_EQUIPS[id].name || id}" — ${Object.keys(worn).length} slot(s). Save to persist.`;
  schedulePreview();
});

document.getElementById("clear-worn")?.addEventListener("click", () => {
  entity.properties = entity.properties || {};
  entity.properties.worn = {};
  renderWorn();
  const note = document.getElementById("worn-preset-note");
  if (note) note.textContent = "Worn map cleared — character has no outfit on the v2 path. Save to persist.";
  schedulePreview();
});

// --- Live prompt preview ---------------------------------------------------
let previewTimer = null;
async function updatePreview() {
  const pre = document.getElementById("worn-preview");
  if (!pre) return;
  try {
    const r = await fetch("/api/preview/body", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ character: entity }),
    });
    const d = await r.json();
    if (!r.ok) {
      pre.textContent = "preview error: " + (d.error || r.statusText);
      return;
    }
    pre.textContent = d.appearance || d.card || "(no appearance rendered)";
  } catch (e) {
    pre.textContent = "preview failed: " + e.message;
  }
}
function schedulePreview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(updatePreview, 250);
}
document.getElementById("refresh-preview")?.addEventListener("click", updatePreview);

// --- Images tab -------------------------------------------------------------
function esc(s) {
  return String(s == null ? "" : s).replace(
    /[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c],
  );
}

// Only allow http(s), root/relative, or data:image URLs in hrefs — an
// image_url pasted via the raw-JSON tab could otherwise be a
// javascript: URL that runs when the "open full image" link is clicked.
function safeUrl(u) {
  u = String(u == null ? "" : u).trim();
  return /^(https?:\/\/|\/|\.\/|data:image\/)/i.test(u) ? u : "#";
}

// Persist local (in-memory) edits before an upload/delete: those server
// routes read the entity from DISK, so without this any unsaved edit
// (worn map, captions, pack metadata, personality, body parts) would be
// clobbered when we replace `entity` with the route's response. Returns
// false if the save failed — callers MUST abort so they don't proceed
// and overwrite the in-memory state.
async function persistBeforeImageOp() {
  if (isNew || !entity.id) return true;
  try {
    entity = await saveEntity(entity, false);
    return true;
  } catch (e) {
    setStatus("Couldn't save before image op: " + e.message, true);
    return false;
  }
}

async function uploadImage(fileInput, caption, pack) {
  if (isNew || !entity.id) {
    setStatus("Save the character first, then add images.", true);
    return;
  }
  const file = fileInput.files && fileInput.files[0];
  if (!file) return;
  if (!(await persistBeforeImageOp())) return;
  const fd = new FormData();
  fd.append("file", file);
  if (caption) fd.append("caption", caption);
  if (pack) fd.append("pack", pack);
  setStatus("Uploading…");
  try {
    const r = await fetch(`/api/entities/${entity.id}/images`, { method: "POST", body: fd });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || r.statusText);
    entity = d.entity;
    fileInput.value = "";
    setStatus("Image uploaded.");
    renderImagesTab();
  } catch (e) {
    setStatus("Upload failed: " + e.message, true);
  }
}

async function deleteImage(url) {
  if (!confirm("Remove this image? The file is deleted if nothing else uses it.")) return;
  if (!(await persistBeforeImageOp())) return;
  try {
    const r = await fetch(`/api/entities/${entity.id}/images/delete`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ image_url: url }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || r.statusText);
    entity = d.entity;
    setStatus("Image removed.");
    renderImagesTab();
  } catch (e) {
    setStatus("Delete failed: " + e.message, true);
  }
}

// Options for the per-image "pack" selector: base catalog + every pack
// + a "new pack…" escape hatch. `currentLoc` ("" = base, else a pack id)
// is marked selected.
function packOptionsHtml(currentLoc) {
  const packs = (entity.properties || {}).image_packs || {};
  let html = `<option value=""${currentLoc ? "" : " selected"}>base catalog</option>`;
  Object.keys(packs).forEach((pid) => {
    html += `<option value="${esc(pid)}"${pid === currentLoc ? " selected" : ""}>` +
      `${esc((packs[pid] || {}).name || pid)}</option>`;
  });
  html += `<option value="__new__">+ new pack…</option>`;
  return html;
}

// Move an image entry between the base catalog and a pack (in place, so
// it persists on Save). Creates the destination pack if needed.
function moveEntry(entry, fromLoc, toLoc) {
  const props = (entity.properties = entity.properties || {});
  const from = fromLoc
    ? ((props.image_packs || {})[fromLoc] || {}).entries
    : (props.images || {}).entries;
  if (Array.isArray(from)) {
    const i = from.indexOf(entry);
    if (i >= 0) from.splice(i, 1);
  }
  let to;
  if (toLoc) {
    props.image_packs = props.image_packs || {};
    const pk = (props.image_packs[toLoc] = props.image_packs[toLoc] ||
      { name: toLoc, default_enabled: false, expose_tags: [], entries: [] });
    to = pk.entries = pk.entries || [];
  } else {
    props.images = props.images || {};
    if (!props.images.format) props.images.format = "tagged";
    to = props.images.entries = props.images.entries || [];
  }
  to.push(entry);
}

// Each image renders as a row: a click-to-open preview + an editable
// caption (the danbooru-style tags the picker scores against) + a pack
// selector (assign it to base or a pack) + delete. Caption edits and
// pack moves mutate the entity in place (entries is a live reference),
// so they persist on Save — and on any upload/delete, which saves first.
function renderGalleryInto(root, entries, currentLoc) {
  currentLoc = currentLoc || "";
  root.innerHTML = "";
  if (!entries || !entries.length) {
    root.innerHTML = '<p class="muted small">No images.</p>';
    return;
  }
  entries.forEach((en) => {
    if (!en || !en.image_url) return;
    const row = document.createElement("div");
    row.className = "image-row";
    row.innerHTML =
      `<a class="image-row-thumb" href="${esc(safeUrl(en.image_url))}" target="_blank" rel="noopener" title="Open full image">` +
      `<img src="${esc(safeUrl(en.image_url))}" alt="" loading="lazy" /></a>` +
      `<div class="image-row-meta">` +
      `<textarea class="image-caption" rows="2" placeholder="tags / caption — comma-separated (e.g. Casual, Sitting, Facing forward)">${esc(en.caption || "")}</textarea>` +
      `<div class="row gap image-row-actions">` +
      `<label class="muted small">pack: <select class="image-pack-sel">${packOptionsHtml(currentLoc)}</select></label>` +
      `<span class="muted small image-url-hint">${esc(en.image_url)}</span>` +
      `<button type="button" class="ghost xs image-del">Remove</button>` +
      `</div></div>`;
    row.querySelector(".image-caption").addEventListener("input", (e) => {
      en.caption = e.target.value;
    });
    row.querySelector(".image-pack-sel").addEventListener("change", (e) => {
      let target = e.target.value;
      if (target === "__new__") {
        const raw = (prompt("New pack id:") || "").trim().replace(/[^a-zA-Z0-9_-]+/g, "_");
        if (!raw) { renderImagesTab(); return; }
        target = raw;
      }
      if (target === currentLoc) return;
      moveEntry(en, currentLoc, target);
      setStatus(`Moved image to ${target || "base catalog"}. Save to persist.`);
      renderBaseCatalog();
      renderPacks();
    });
    row.querySelector(".image-del").addEventListener("click", () => deleteImage(en.image_url));
    root.appendChild(row);
  });
}

async function renderComposedOutfits() {
  const root = document.getElementById("images-outfits");
  if (!root) return;
  if (isNew || !entity.id) {
    root.innerHTML = '<p class="muted small">Save the character first to preview composed outfits.</p>';
    return;
  }
  root.innerHTML = '<p class="muted small">Loading…</p>';
  try {
    const r = await fetch(`/api/entities/${entity.id}/outfit-sprites`);
    // A non-JSON body means the /outfit-sprites route isn't loaded (the
    // app server predates it) — Flask returns an HTML 404/500 page.
    const ct = r.headers.get("content-type") || "";
    if (!ct.includes("application/json")) {
      root.innerHTML =
        `<p class="muted small">Composed preview unavailable (HTTP ${r.status}). ` +
        `If you just updated the code, restart the app server so the ` +
        `<code>/outfit-sprites</code> route loads.</p>`;
      return;
    }
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || r.statusText);
    root.innerHTML = "";
    if (!d.sprite_id) {
      root.insertAdjacentHTML("beforeend",
        '<p class="muted small">Not a sprite character — no composed image. Set <code>images.format = combined</code> and a <code>sprite_id</code> (with wardrobe assets) to render per-outfit sprites. The outfit stats still show below.</p>');
    }
    if (!d.outfits.length) {
      root.insertAdjacentHTML("beforeend", '<p class="muted small">No outfits owned by this character.</p>');
      return;
    }
    const grid = document.createElement("div");
    grid.className = "outfit-sprite-grid";
    d.outfits.forEach((o) => {
      // Live worn map for this card, seeded from the pieces' default
      // (first) states. Swapping a state recomposes the sprite.
      const worn = {};
      (o.pieces || []).forEach((p) => {
        worn[p.slot] = { piece: p.piece_id, state: (p.states && p.states[0]) || "on" };
      });
      const card = document.createElement("div");
      card.className = "outfit-sprite-card wide";
      const rows = (o.pieces || []).map((p) => {
        const stateOpts = (p.states || ["on", "off"])
          .map((s, i) => `<option value="${esc(s)}"${i === 0 ? " selected" : ""}>${esc(s)}</option>`)
          .join("");
        const rawJson = esc(JSON.stringify(p.piece || { id: p.piece_id }, null, 2));
        return (
          `<div class="sprite-piece-row">` +
          `<span class="sprite-piece-name muted small">${esc(p.slot)} · ${esc(p.name)}` +
          `${p.sprite_slot ? "" : " <em>(prose-only, no sprite layer)</em>"}</span>` +
          `<select class="sprite-piece-state" data-slot="${esc(p.slot)}">${stateOpts}</select>` +
          `<a class="ghost xs" href="/studio/clothing/${esc(p.piece_id)}" target="_blank" rel="noopener">edit ↗</a>` +
          `<details class="piece-json"><summary class="muted small">json (editable)</summary>` +
          `<textarea class="piece-json-edit json-inline" data-piece="${esc(p.piece_id)}" rows="12" spellcheck="false">${rawJson}</textarea>` +
          `<button type="button" class="ghost xs" data-save-piece="${esc(p.piece_id)}">Save piece</button>` +
          `</details>` +
          `</div>`
        );
      }).join("");
      card.innerHTML =
        `<div class="sprite-img-wrap">` +
        (o.url
          ? `<img class="sprite-img" src="${esc(safeUrl(o.url))}" alt="" loading="lazy" />`
          : `<div class="placeholder">no composed image</div>`) +
        `</div>` +
        `<div class="outfit-sprite-meta">` +
        `<div class="row gap"><strong>${esc(o.name)}</strong>` +
        `${o.is_current ? '<span class="tag">current</span>' : ""}` +
        `<a class="ghost xs" href="/studio/outfit/${esc(o.outfit_id)}" target="_blank" rel="noopener">edit outfit ↗</a></div>` +
        `<div class="sprite-piece-controls">${rows || '<p class="muted small">no equipped pieces</p>'}</div>` +
        `</div>`;
      // Wire state pickers → recompose this card's sprite live.
      const img = card.querySelector(".sprite-img");
      card.querySelectorAll(".sprite-piece-state").forEach((sel) => {
        sel.addEventListener("change", async () => {
          const slot = sel.dataset.slot;
          if (worn[slot]) worn[slot].state = sel.value;
          if (!img) return;
          img.style.opacity = "0.5";
          try {
            const cr = await fetch(`/api/entities/${entity.id}/compose-url`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ worn }),
            });
            const ct = cr.headers.get("content-type") || "";
            if (!ct.includes("application/json")) {
              setStatus(`Live sprite unavailable (HTTP ${cr.status}) — restart the app server so the /compose-url route loads.`, true);
              return;
            }
            const cd = await cr.json();
            if (!cr.ok) {
              setStatus("Compose failed: " + (cd.error || cr.statusText), true);
            } else if (cd.url) {
              img.src = cd.url;
              setStatus(`${o.name}: ${slot} → ${sel.value}`);
            }
          } catch (e) {
            setStatus("Compose failed: " + e.message, true);
          } finally {
            img.style.opacity = "";
          }
        });
      });
      // Wire per-piece JSON editing → save the piece entity + rebuild.
      card.querySelectorAll("[data-save-piece]").forEach((btn) => {
        btn.addEventListener("click", async () => {
          const pid = btn.dataset.savePiece;
          const ta = card.querySelector(`.piece-json-edit[data-piece="${CSS.escape(pid)}"]`);
          if (!ta) return;
          let parsed;
          try {
            parsed = JSON.parse(ta.value);
          } catch (e) {
            setStatus("Invalid piece JSON: " + e.message, true);
            return;
          }
          parsed.id = pid;
          parsed.type = "clothing";
          setStatus("Saving piece…");
          try {
            await saveEntity(parsed, false);
            setStatus(`Saved piece ${pid}.`);
            renderComposedOutfits(); // rebuild with fresh states + sprite
          } catch (e) {
            setStatus("Piece save failed: " + e.message, true);
          }
        });
      });
      grid.appendChild(card);
    });
    root.appendChild(grid);
  } catch (e) {
    root.innerHTML = `<p class="muted small">preview failed: ${esc(e.message)}</p>`;
  }
}

function renderBaseCatalog() {
  const root = document.getElementById("images-base");
  if (!root) return;
  const entries = (((entity.properties || {}).images) || {}).entries || [];
  renderGalleryInto(root, entries, "");
}

function renderPacks() {
  const root = document.getElementById("images-packs");
  if (!root) return;
  root.innerHTML = "";
  entity.properties = entity.properties || {};
  const packs = entity.properties.image_packs || {};
  const ids = Object.keys(packs);
  if (!ids.length) {
    root.innerHTML = '<p class="muted small">No packs yet. Add one below to group images gated by a tag.</p>';
    return;
  }
  ids.forEach((pid) => {
    const pk = packs[pid] || {};
    const wrap = document.createElement("div");
    wrap.className = "image-pack editor-card";
    wrap.innerHTML =
      `<div class="row gap image-pack-head">` +
      `<strong>${esc(pid)}</strong>` +
      `<input type="text" class="pk-name" placeholder="display name" value="${esc(pk.name || "")}" style="flex:1" />` +
      `<label class="row toggle"><input type="checkbox" class="pk-default"${pk.default_enabled ? " checked" : ""} /> <span class="muted small">default-on</span></label>` +
      `<label class="ghost xs as-button">upload<input type="file" class="pk-upload" accept="image/*" hidden /></label>` +
      `<button type="button" class="ghost xs pk-del">delete pack</button>` +
      `</div>` +
      `<label class="stack"><span class="muted small">Expose tags (comma) — the pack's images become available when a scene tag matches</span>` +
      `<input type="text" class="pk-tags" value="${esc((pk.expose_tags || []).join(", "))}" /></label>` +
      `<div class="image-gallery pk-gallery"></div>`;
    renderGalleryInto(wrap.querySelector(".pk-gallery"), pk.entries || [], pid);
    wrap.querySelector(".pk-name").addEventListener("input", (e) => { pk.name = e.target.value; });
    wrap.querySelector(".pk-default").addEventListener("change", (e) => { pk.default_enabled = e.target.checked; });
    wrap.querySelector(".pk-tags").addEventListener("input", (e) => {
      pk.expose_tags = e.target.value.split(",").map((s) => s.trim()).filter(Boolean);
    });
    wrap.querySelector(".pk-upload").addEventListener("change", (e) => uploadImage(e.target, "", pid));
    wrap.querySelector(".pk-del").addEventListener("click", () => {
      if (confirm(`Delete pack "${pid}"? (its image files stay on disk)`)) {
        delete entity.properties.image_packs[pid];
        renderPacks();
        renderBaseCatalog(); // drop the removed pack from the image selectors
      }
    });
    root.appendChild(wrap);
  });
}

function renderImagesTab() {
  renderComposedOutfits();
  renderBaseCatalog();
  renderPacks();
}

document.getElementById("images-base-upload")?.addEventListener("change", (e) => {
  const cap = document.getElementById("images-base-caption");
  uploadImage(e.target, cap ? cap.value.trim() : "", "");
  if (cap) cap.value = "";
});
let newOutfitFiles = null;
document.getElementById("new-outfit-files")?.addEventListener("change", (e) => {
  newOutfitFiles = e.target.files;
  const c = document.getElementById("new-outfit-files-count");
  if (c) c.textContent = newOutfitFiles && newOutfitFiles.length
    ? `${newOutfitFiles.length} image(s) selected` : "";
});
document.getElementById("create-outfit-images")?.addEventListener("click", async () => {
  if (isNew || !entity.id) {
    setStatus("Save the character first, then create an outfit.", true);
    return;
  }
  const nameEl = document.getElementById("new-outfit-name");
  const name = (nameEl.value || "").trim();
  if (!name) { setStatus("Outfit name required.", true); return; }
  if (!newOutfitFiles || !newOutfitFiles.length) {
    setStatus("Choose at least one image.", true);
    return;
  }
  if (!(await persistBeforeImageOp())) return;
  const fd = new FormData();
  fd.append("name", name);
  for (const f of newOutfitFiles) fd.append("files", f);
  setStatus("Creating outfit…");
  try {
    const r = await fetch(`/api/entities/${entity.id}/outfit-from-images`, { method: "POST", body: fd });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || r.statusText);
    entity = d.entity;
    newOutfitFiles = null;
    nameEl.value = "";
    const c = document.getElementById("new-outfit-files-count");
    if (c) c.textContent = "";
    setStatus(`Created outfit "${d.outfit_id}" (${d.images_added} images).`);
    renderImagesTab();
    renderOutfits();
  } catch (e) {
    setStatus("Create failed: " + e.message, true);
  }
});

document.getElementById("add-pack")?.addEventListener("click", () => {
  const inp = document.getElementById("new-pack-id");
  const id = (inp.value || "").trim().replace(/[^a-zA-Z0-9_-]+/g, "_");
  if (!id) return;
  entity.properties = entity.properties || {};
  entity.properties.image_packs = entity.properties.image_packs || {};
  if (!entity.properties.image_packs[id]) {
    entity.properties.image_packs[id] = { name: id, default_enabled: false, expose_tags: [], entries: [] };
  }
  inp.value = "";
  renderPacks();
  renderBaseCatalog(); // refresh the per-image "pack" selectors with the new pack
});

// --- Portrait upload -------------------------------------------------------
document.getElementById("portrait-file").addEventListener("change", async (ev) => {
  if (isNew || !entity.id) {
    setStatus("Save the character first, then upload a portrait.", true);
    return;
  }
  const file = ev.target.files[0];
  if (!file) return;
  // The portrait route reads the entity from disk and returns it; persist
  // in-memory edits first (and abort on failure) so `entity = data.entity`
  // below doesn't clobber unsaved worn/personality/image edits.
  if (!(await persistBeforeImageOp())) return;
  const fd = new FormData();
  fd.append("file", file);
  setStatus("Uploading…");
  const r = await fetch(`/api/entities/${entity.id}/portrait`, { method: "POST", body: fd });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    setStatus("Upload failed: " + (err.error || r.statusText), true);
    return;
  }
  const data = await r.json();
  entity = data.entity;
  // Cache-bust the preview.
  document.getElementById("portrait-preview").innerHTML =
    `<img src="/portraits/${entity.id}?t=${Date.now()}" alt="" />`;
  setStatus("Portrait uploaded.");
});

// --- Save / Delete ---------------------------------------------------------
document.getElementById("save-entity").addEventListener("click", async (ev) => {
  ev.preventDefault();
  // Pull form fields back into the entity, preserving stuff the form doesn't
  // expose (children, _template_id, custom properties, etc.).
  entity = collectForm(form, entity);
  entity.type = "character";
  if (!entity.id) {
    setStatus("ID is required.", true);
    return;
  }
  try {
    const saved = await saveEntity(entity, isNew);
    entity = saved;
    setStatus(`Saved ${saved.id}.`);
    if (isNew) {
      window.location.href = `/studio/character/${saved.id}`;
    }
  } catch (e) {
    setStatus("Save failed: " + e.message, true);
  }
});

document.getElementById("delete-entity").addEventListener("click", async () => {
  if (isNew || !entity.id) return;
  if (!confirm(`Delete character ${entity.id}? This cannot be undone.`)) return;
  await deleteEntity(entity.id);
  window.location.href = "/studio";
});

// --- Boot ------------------------------------------------------------------
fillForm(form, entity);
renderPersonality();
renderBodyParts();
renderOutfits();
renderWorn();
populatePresetSelect();
schedulePreview();
renderImagesTab();
wireTabs(document);
wireJSONTab(document.getElementById("entity-json"), () => entity, (parsed) => {
  entity = parsed;
  fillForm(form, entity);
  renderPersonality();
  renderBodyParts();
  renderOutfits();
  renderWorn();
  populatePresetSelect();
  schedulePreview();
  renderImagesTab();
});
