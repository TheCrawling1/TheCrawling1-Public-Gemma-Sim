// Clothing-piece editor (v2 `type: clothing`). Phase 0: identity + the
// core Piece fields (slot / states / garment / owner) via the shared
// form plumbing, with the coverage map still edited in the JSON tab.
// A structured per-state per-part coverage grid is the Phase-2 upgrade.
//
// Seeds a VALID skeleton for a new piece (non-empty slot + states +
// per-state coverage map) so a fresh piece passes `_validate_clothing`
// on the first save instead of 400ing.

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
    type: "clothing",
    name: "",
    description: "",
    tags: [],
    children: [],
    example_text: "",
    properties: {
      slot: "top",
      states: ["on", "off"],
      garment: "default",
      coverage: { on: {}, off: {} },
    },
  };
const isNew = boot.isNew;
const form = document.getElementById("editor-form");

document.getElementById("save-entity").addEventListener("click", async (ev) => {
  ev.preventDefault();
  entity = collectForm(form, entity);
  entity.type = "clothing";
  if (!entity.id) {
    setStatus("ID is required.", true);
    return;
  }
  try {
    const saved = await saveEntity(entity, isNew);
    entity = saved; // new object reference
    setStatus(`Saved ${saved.id}.`);
    if (isNew) window.location.href = `/studio/clothing/${saved.id}`;
    else remountCoverage(); // re-bind the grid to the saved entity
  } catch (e) {
    setStatus("Save failed: " + e.message, true);
  }
});

document.getElementById("delete-entity").addEventListener("click", async () => {
  if (isNew || !entity.id) return;
  if (!confirm(`Delete ${entity.id}? This cannot be undone.`)) return;
  await deleteEntity(entity.id);
  window.location.href = "/studio";
});

fillForm(form, entity);

// Structured per-state / per-part coverage grid, editing `entity` in place.
// The form's own Name / Description / States inputs are the source of
// truth for those, so the grid is coverage-only; when the States field
// changes we re-render the grid so new/removed states get buckets.
const covMount = document.getElementById("coverage-mount");
let covApi = null;
function remountCoverage() {
  if (!covMount) return;
  covApi = mountCoverageEditor(covMount, entity, {
    coverageOnly: true,
    onError: (msg) => setStatus(msg, true),
  });
}
remountCoverage();

const statesInput = form.querySelector('[data-field="properties.states"]');
if (statesInput) {
  statesInput.addEventListener("change", () => {
    entity.properties = entity.properties || {};
    entity.properties.states = statesInput.value
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    if (covApi) covApi.render();
  });
}

wireTabs(document);
wireJSONTab(
  document.getElementById("entity-json"),
  () => entity,
  (parsed) => {
    entity = parsed; // new object reference — re-bind the grid to it
    fillForm(form, entity);
    remountCoverage();
  },
);
