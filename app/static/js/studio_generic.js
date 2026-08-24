// Generic studio editor: form fields + JSON source tab. Used for room,
// outfit, and object pages where there's no extra structured editor.

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
const defaultType = window.STUDIO_DEFAULT_TYPE || boot.type;
let entity =
  boot.entity || {
    id: "",
    type: defaultType,
    name: "",
    description: "",
    tags: [],
    children: [],
    properties: {},
    example_text: "",
  };
const isNew = boot.isNew;
const form = document.getElementById("editor-form");

document.getElementById("save-entity").addEventListener("click", async (ev) => {
  ev.preventDefault();
  entity = collectForm(form, entity);
  entity.type = entity.type || defaultType;
  if (!entity.id) {
    setStatus("ID is required.", true);
    return;
  }
  try {
    const saved = await saveEntity(entity, isNew);
    entity = saved;
    setStatus(`Saved ${saved.id}.`);
    if (isNew) window.location.href = `/studio/${entity.type}/${saved.id}`;
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
wireTabs(document);
wireJSONTab(document.getElementById("entity-json"), () => entity, (parsed) => {
  entity = parsed;
  fillForm(form, entity);
});
