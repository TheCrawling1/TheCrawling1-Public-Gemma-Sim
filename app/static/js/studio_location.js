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
    type: "location",
    name: "",
    description: "",
    tags: [],
    children: [],
    properties: { ambient_sounds: "", lighting: "", atmosphere: "" },
    example_text: "",
  };
const isNew = boot.isNew;
const form = document.getElementById("editor-form");

async function fetchRoom(id) {
  const r = await fetch(`/api/entities/${id}`);
  if (!r.ok) return null;
  return r.json();
}

async function renderRooms() {
  const list = document.getElementById("room-list");
  list.innerHTML = "";
  const ids = entity.children || [];
  const rooms = await Promise.all(ids.map(fetchRoom));
  rooms.forEach((room, idx) => {
    const id = ids[idx];
    const li = document.createElement("li");
    li.innerHTML = `
      <a href="/studio/room/${id}" target="_blank" rel="noopener">${room ? room.name || id : id + " (missing)"}</a>
      <span class="muted small">${id}</span>
      <button type="button" class="ghost xs" data-remove-room="${idx}">Unlink</button>
    `;
    list.appendChild(li);
  });
  list.querySelectorAll("[data-remove-room]").forEach((btn) => {
    btn.addEventListener("click", () => {
      entity.children.splice(Number(btn.dataset.removeRoom), 1);
      renderRooms();
    });
  });
}

document.getElementById("add-room").addEventListener("click", async () => {
  if (isNew || !entity.id) {
    setStatus("Save the location first, then add rooms.", true);
    return;
  }
  const id = (document.getElementById("new-room-id").value || "").trim();
  const name = (document.getElementById("new-room-name").value || "").trim();
  if (!id) return;
  const room = {
    id,
    type: "room",
    name: name || id,
    description: "",
    tags: [],
    children: [],
    properties: { exits: [] },
    example_text: "",
  };
  try {
    await saveEntity(room, true);
  } catch (e) {
    setStatus("Room create failed: " + e.message, true);
    return;
  }
  entity.children = entity.children || [];
  if (!entity.children.includes(id)) entity.children.push(id);
  // Persist the location too so the room moves into this folder.
  try {
    entity = await saveEntity(collectForm(form, entity), false);
  } catch (e) {
    setStatus("Location save failed: " + e.message, true);
    return;
  }
  document.getElementById("new-room-id").value = "";
  document.getElementById("new-room-name").value = "";
  renderRooms();
  setStatus(`Created ${id}.`);
});

document.getElementById("link-room").addEventListener("click", () => {
  const sel = document.getElementById("link-room-select");
  const id = sel.value;
  if (!id) return;
  entity.children = entity.children || [];
  if (!entity.children.includes(id)) entity.children.push(id);
  sel.value = "";
  renderRooms();
});

document.getElementById("save-entity").addEventListener("click", async (ev) => {
  ev.preventDefault();
  entity = collectForm(form, entity);
  entity.type = "location";
  if (!entity.id) {
    setStatus("ID is required.", true);
    return;
  }
  try {
    const saved = await saveEntity(entity, isNew);
    entity = saved;
    setStatus(`Saved ${saved.id}.`);
    if (isNew) window.location.href = `/studio/location/${saved.id}`;
  } catch (e) {
    setStatus("Save failed: " + e.message, true);
  }
});

document.getElementById("delete-entity").addEventListener("click", async () => {
  if (isNew || !entity.id) return;
  if (!confirm(`Delete location ${entity.id}? Rooms are NOT deleted.`)) return;
  await deleteEntity(entity.id);
  window.location.href = "/studio";
});

fillForm(form, entity);
renderRooms();
wireTabs(document);
wireJSONTab(document.getElementById("entity-json"), () => entity, (parsed) => {
  entity = parsed;
  fillForm(form, entity);
  renderRooms();
});
