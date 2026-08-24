// Outfit-bundle editor (v2). The bundle is a named preset: an `equips`
// map (slot → clothing-piece id) plus a `signature_description`. This
// controller renders a per-slot piece picker (scoped to the owner's
// pieces + shared ones) AND, for each equipped piece, a full inline
// editor for that piece's states / per-part coverage / raw json — so an
// outfit's parts can be edited in one place. Identity / signature /
// owner / the legacy-v1 fields ride the shared `data-field` plumbing.

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
import { mountCoverageEditor } from "./coverage_editor.js";

const boot = readBootstrap();
let entity =
  boot.entity || {
    id: "",
    type: "outfit",
    name: "",
    description: "",
    tags: [],
    children: [],
    example_text: "",
    properties: { owner: "", equips: {}, signature_description: "" },
  };
const isNew = boot.isNew;
const form = document.getElementById("editor-form");

// Full clothing entities (not a slim projection) so equipped pieces can
// be edited and saved in full from here.
const CLOTHING = JSON.parse(
  document.getElementById("clothing-catalog").textContent || "[]",
);
const slotOf = (c) => (c.properties || {}).slot || "";
const ownerOf = (c) => (c.properties || {}).owner || "";

// Equips slot inventory + the underwear/panties alias (pieces use either).
const SLOTS = [
  "top", "bottom", "bra", "underwear", "pantyhose", "gloves",
  "legwear", "shoes", "head", "face", "neck", "back", "overlay",
];
const SLOT_ACCEPTS = { underwear: ["underwear", "panties"] };

// Editable clones of the equipped pieces, keyed by piece id, plus the set
// of pieces with unsaved edits.
const pieceEdits = {};
const dirty = new Set();

const catalogPiece = (id) => CLOTHING.find((c) => c.id === id);

function editablePiece(id) {
  if (!id) return null;
  if (!pieceEdits[id]) {
    const src = catalogPiece(id);
    pieceEdits[id] = src ? JSON.parse(JSON.stringify(src)) : null;
  }
  return pieceEdits[id];
}

function piecesForSlot(slot, owner) {
  const accept = SLOT_ACCEPTS[slot] || [slot];
  return CLOTHING.filter((c) => accept.includes(slotOf(c)))
    // owner's pieces + shared (owner-less); hide other characters' pieces
    .filter((c) => !ownerOf(c) || !owner || ownerOf(c) === owner)
    .sort((a, b) => {
      const ao = ownerOf(a) === owner ? 0 : 1;
      const bo = ownerOf(b) === owner ? 0 : 1;
      return ao - bo || (a.name || a.id).localeCompare(b.name || b.id);
    });
}

// Mount (or clear) the inline piece editor for one slot's current piece.
function mountPieceEditor(slot) {
  const wrap = document.querySelector(`[data-detail-slot="${slot}"]`);
  if (!wrap) return;
  const id = entity.properties.equips[slot] || "";
  if (!id) {
    wrap.innerHTML = "";
    return;
  }
  const piece = editablePiece(id);
  if (!piece) {
    wrap.innerHTML =
      `<p class="muted small">${id} — not found in catalog.</p>`;
    return;
  }
  wrap.innerHTML =
    `<div class="piece-toolbar">` +
    `<button type="button" class="ghost small" data-save-piece="${slot}">Save piece</button>` +
    `<span class="muted small piece-dirty" data-dirty="${slot}"` +
    `${dirty.has(id) ? "" : " hidden"}>unsaved edits</span>` +
    `</div><div class="cov-mount"></div>`;
  const mount = wrap.querySelector(".cov-mount");
  mountCoverageEditor(mount, piece, {
    onChange: () => {
      dirty.add(id);
      const flag = wrap.querySelector(`[data-dirty="${slot}"]`);
      if (flag) flag.hidden = false;
    },
    onError: (msg) => setStatus(msg, true),
  });
  const btn = wrap.querySelector(`[data-save-piece="${slot}"]`);
  if (btn) btn.addEventListener("click", () => savePiece(slot, id));
}

async function savePiece(slot, id) {
  const piece = pieceEdits[id];
  if (!piece) return false;
  try {
    const saved = await saveEntity(piece, false);
    pieceEdits[id] = JSON.parse(JSON.stringify(saved));
    const idx = CLOTHING.findIndex((c) => c.id === id);
    if (idx >= 0) CLOTHING[idx] = JSON.parse(JSON.stringify(saved));
    dirty.delete(id);
    setStatus(`Saved piece ${id}.`);
    mountPieceEditor(slot);
    return true;
  } catch (e) {
    setStatus(`Piece ${id} failed: ` + e.message, true);
    return false;
  }
}

function renderEquips() {
  const root = document.getElementById("equips-list");
  root.innerHTML = "";
  entity.properties = entity.properties || {};
  entity.properties.equips = entity.properties.equips || {};
  const equips = entity.properties.equips;
  const owner = entity.properties.owner || "";
  SLOTS.forEach((slot) => {
    const pieces = piecesForSlot(slot, owner);
    const cur = equips[slot] || "";
    const row = document.createElement("div");
    row.className = "stack equip-row";
    const options =
      `<option value="">— none —</option>` +
      pieces
        .map(
          (p) =>
            `<option value="${p.id}"${p.id === cur ? " selected" : ""}>` +
            `${p.name || p.id}${ownerOf(p) ? "" : " (shared)"}</option>`,
        )
        .join("") +
      // keep a stale/other-owner id visible so it isn't silently dropped
      (cur && !pieces.some((p) => p.id === cur)
        ? `<option value="${cur}" selected>${cur} (not in list)</option>`
        : "");
    row.innerHTML =
      `<span>${slot}</span>` +
      `<div class="row gap">` +
      `<select data-equip-slot="${slot}" style="flex:1">${options}</select>` +
      // Access to the equipped piece's own standalone editor.
      `<a class="ghost small edit-piece" data-edit-slot="${slot}"` +
      ` href="${cur ? "/studio/clothing/" + cur : "#"}" target="_blank"` +
      ` rel="noopener" style="${cur ? "" : "visibility:hidden"}">edit ↗</a>` +
      `</div>` +
      // The piece's editable content (states / coverage / raw json).
      `<div class="piece-detail-wrap" data-detail-slot="${slot}"></div>`;
    root.appendChild(row);
  });
  root.querySelectorAll("[data-equip-slot]").forEach((sel) => {
    sel.addEventListener("change", () => {
      const slot = sel.dataset.equipSlot;
      if (sel.value) entity.properties.equips[slot] = sel.value;
      else delete entity.properties.equips[slot];
      const link = root.querySelector(`.edit-piece[data-edit-slot="${slot}"]`);
      if (link) {
        if (sel.value) {
          link.href = "/studio/clothing/" + sel.value;
          link.style.visibility = "visible";
        } else {
          link.style.visibility = "hidden";
        }
      }
      mountPieceEditor(slot);
    });
  });
  SLOTS.forEach(mountPieceEditor);
}

// Re-scope the piece pickers when the owner changes.
const ownerSel = form.querySelector('[data-field="properties.owner"]');
if (ownerSel) {
  ownerSel.addEventListener("change", () => {
    entity.properties = entity.properties || {};
    entity.properties.owner = ownerSel.value;
    renderEquips();
  });
}

document.getElementById("save-entity").addEventListener("click", async (ev) => {
  ev.preventDefault();
  entity = collectForm(form, entity); // preserves equips (on entity) via deep clone
  entity.type = "outfit";
  if (!entity.id) {
    setStatus("ID is required.", true);
    return;
  }
  try {
    const saved = await saveEntity(entity, isNew);
    entity = saved;
  } catch (e) {
    setStatus("Save failed: " + e.message, true);
    return;
  }
  // Persist any edited equipped pieces too.
  const ids = [...dirty];
  const failed = [];
  for (const id of ids) {
    const slot = Object.keys(entity.properties.equips || {}).find(
      (s) => entity.properties.equips[s] === id,
    );
    const ok = await savePiece(slot, id);
    if (!ok) failed.push(id);
  }
  if (failed.length) {
    setStatus(`Saved outfit; ${failed.length} piece(s) failed.`, true);
  } else if (ids.length) {
    setStatus(`Saved ${entity.id} + ${ids.length} piece(s).`);
  } else {
    setStatus(`Saved ${entity.id}.`);
  }
  if (isNew) window.location.href = `/studio/outfit/${entity.id}`;
});

document.getElementById("delete-entity").addEventListener("click", async () => {
  if (isNew || !entity.id) return;
  if (!confirm(`Delete ${entity.id}? This cannot be undone.`)) return;
  await deleteEntity(entity.id);
  window.location.href = "/studio";
});

fillForm(form, entity);
renderEquips();
wireTabs(document);
wireJSONTab(
  document.getElementById("entity-json"),
  () => entity,
  (parsed) => {
    entity = parsed;
    fillForm(form, entity);
    renderEquips();
  },
);
