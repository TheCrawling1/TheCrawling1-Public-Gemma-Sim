// Scenario editor: cast list with per-character starting state + custom
// outfits + first messages. Reads bootstrap data and writes back the
// full scenario JSON shape on save.

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
    type: "scenario",
    name: "",
    description: "",
    tags: [],
    children: [],
    properties: {},
    example_text: "",
    characters: [],
    locations: [],
    objects: [],
    lore: [],
    starting_state: {},
    character_overrides: {},
    custom_outfits: [],
    first_messages: {},
    opening_prompt: "",
    scenario_instructions: "",
    setups: [],
    turn_mode: "manual",
    narrator_mode: "auto",
    context_limit_tokens: 8000,
    personas: [],
  };
const isNew = boot.isNew;
const form = document.getElementById("editor-form");

const allCharacters = JSON.parse(document.getElementById("all-characters").textContent);
const allLocations = JSON.parse(document.getElementById("all-locations").textContent);
const allRooms = JSON.parse(document.getElementById("all-rooms").textContent);
const allOutfits = JSON.parse(document.getElementById("all-outfits").textContent);

function characterById(id) {
  return allCharacters.find((c) => c.id === id);
}
function roomsForLocation(locId) {
  const loc = allLocations.find((l) => l.id === locId);
  if (!loc) return [];
  const childIds = loc.children || [];
  return allRooms.filter((r) => childIds.includes(r.id));
}
function outfitsForCharacter(charId) {
  const ch = characterById(charId);
  const ownIds = (ch && ch.properties && ch.properties.outfits) || [];
  const own = allOutfits.filter((o) => ownIds.includes(o.id));
  const customs = (entity.custom_outfits || []).filter((o) => !o.properties || !o.properties.owner || o.properties.owner === charId);
  return [...own, ...customs];
}
function allRoomsAcrossPickedLocations() {
  const locs = entity.locations || [];
  if (!locs.length) return allRooms;
  const out = [];
  for (const l of locs) out.push(...roomsForLocation(l));
  return out;
}

function ensureStartingState() {
  entity.starting_state = entity.starting_state || {};
  entity.first_messages = entity.first_messages || {};
}

// --- Locations checklist ---------------------------------------------------
function renderLocationChecklist() {
  document.querySelectorAll("[data-scenario-location]").forEach((cb) => {
    cb.checked = (entity.locations || []).includes(cb.dataset.scenarioLocation);
    cb.addEventListener("change", () => {
      const id = cb.dataset.scenarioLocation;
      entity.locations = entity.locations || [];
      if (cb.checked) {
        if (!entity.locations.includes(id)) entity.locations.push(id);
      } else {
        entity.locations = entity.locations.filter((x) => x !== id);
      }
      renderCast(); // refresh room dropdowns
    });
  });
}

// --- Cast ------------------------------------------------------------------
function renderCast() {
  ensureStartingState();
  const root = document.getElementById("cast-list");
  root.innerHTML = "";
  const cast = entity.characters || [];
  cast.forEach((charId, idx) => {
    const ch = characterById(charId);
    const startState = entity.starting_state[charId] || {};
    const card = document.createElement("div");
    card.className = "cast-card";
    const rooms = allRoomsAcrossPickedLocations();
    const outfits = outfitsForCharacter(charId);
    card.innerHTML = `
      <div class="cast-head">
        <strong>${ch ? ch.name || charId : charId}</strong>
        <span class="muted small">${charId}</span>
        <button type="button" class="ghost xs" data-remove-cast="${idx}">Remove</button>
      </div>
      <div class="cast-grid">
        <label class="stack">
          <span class="muted small">Starting location</span>
          <select data-cast-field="${charId}.location">
            <option value="">(none)</option>
            ${allLocations.map((l) => `<option value="${l.id}" ${startState.location === l.id ? "selected" : ""}>${l.name || l.id}</option>`).join("")}
          </select>
        </label>
        <label class="stack">
          <span class="muted small">Starting room</span>
          <select data-cast-field="${charId}.room">
            <option value="">(none)</option>
            ${rooms.map((r) => `<option value="${r.id}" ${startState.room === r.id ? "selected" : ""}>${r.name || r.id}</option>`).join("")}
          </select>
        </label>
        <label class="stack">
          <span class="muted small">Starting outfit</span>
          <select data-cast-field="${charId}.outfit">
            <option value="">(default)</option>
            ${outfits.map((o) => `<option value="${o.id}" ${startState.outfit === o.id ? "selected" : ""}>${o.name || o.id}</option>`).join("")}
          </select>
        </label>
      </div>
    `;
    root.appendChild(card);
  });
  root.querySelectorAll("[data-cast-field]").forEach((el) => {
    el.addEventListener("change", () => {
      const [charId, key] = el.dataset.castField.split(".");
      entity.starting_state = entity.starting_state || {};
      entity.starting_state[charId] = entity.starting_state[charId] || {};
      entity.starting_state[charId][key] = el.value || null;
    });
  });
  root.querySelectorAll("[data-remove-cast]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const i = Number(btn.dataset.removeCast);
      const removed = entity.characters.splice(i, 1)[0];
      delete (entity.starting_state || {})[removed];
      delete (entity.first_messages || {})[removed];
      renderCast();
      renderFirstMessages();
    });
  });
}

document.getElementById("add-cast").addEventListener("click", () => {
  const sel = document.getElementById("add-cast-select");
  const id = sel.value;
  if (!id) return;
  entity.characters = entity.characters || [];
  if (!entity.characters.includes(id)) entity.characters.push(id);
  sel.value = "";
  renderCast();
  renderFirstMessages();
});

// --- Custom outfits --------------------------------------------------------
function renderCustomOutfits() {
  const list = document.getElementById("custom-outfits");
  list.innerHTML = "";
  const customs = entity.custom_outfits || [];
  customs.forEach((o, idx) => {
    const li = document.createElement("li");
    li.className = "custom-outfit-row";
    li.innerHTML = `
      <input type="text" value="${o.id || ""}" data-custom-outfit="${idx}.id" placeholder="id" />
      <input type="text" value="${o.name || ""}" data-custom-outfit="${idx}.name" placeholder="name" />
      <select data-custom-outfit="${idx}.properties.owner">
        <option value="">(no owner)</option>
        ${allCharacters.map((c) => `<option value="${c.id}" ${o.properties && o.properties.owner === c.id ? "selected" : ""}>${c.name || c.id}</option>`).join("")}
      </select>
      <select data-custom-outfit="${idx}.properties.extends">
        <option value="">(no base)</option>
        ${allOutfits.map((b) => `<option value="${b.id}" ${o.properties && o.properties.extends === b.id ? "selected" : ""}>${b.name || b.id}</option>`).join("")}
      </select>
      <button type="button" class="ghost xs" data-remove-custom="${idx}">Remove</button>
    `;
    list.appendChild(li);
  });
  list.querySelectorAll("[data-custom-outfit]").forEach((el) => {
    el.addEventListener("change", () => {
      const path = el.dataset.customOutfit;
      const [idxStr, ...rest] = path.split(".");
      const i = Number(idxStr);
      const target = entity.custom_outfits[i];
      let cur = target;
      for (let j = 0; j < rest.length - 1; j++) {
        cur[rest[j]] = cur[rest[j]] || {};
        cur = cur[rest[j]];
      }
      cur[rest[rest.length - 1]] = el.value;
      // Keep type tag.
      target.type = "outfit";
    });
  });
  list.querySelectorAll("[data-remove-custom]").forEach((btn) => {
    btn.addEventListener("click", () => {
      entity.custom_outfits.splice(Number(btn.dataset.removeCustom), 1);
      renderCustomOutfits();
    });
  });
}

document.getElementById("add-custom-outfit").addEventListener("click", () => {
  const inp = document.getElementById("new-custom-outfit-id");
  const id = (inp.value || "").trim();
  if (!id) return;
  entity.custom_outfits = entity.custom_outfits || [];
  entity.custom_outfits.push({
    id,
    type: "outfit",
    name: id,
    description: "",
    tags: [],
    children: [],
    properties: { owner: "", extends: "", coverage: {} },
    example_text: "",
  });
  inp.value = "";
  renderCustomOutfits();
});

// --- Setups ---------------------------------------------------------------
function ensureSetups() {
  entity.setups = Array.isArray(entity.setups) ? entity.setups : [];
}
function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[c]));
}

function renderSetups() {
  ensureSetups();
  const root = document.getElementById("setups-list");
  if (!root) return;
  root.innerHTML = "";
  entity.setups.forEach((setup, idx) => {
    const card = document.createElement("div");
    card.className = "editor-card";
    card.style.marginBottom = "1rem";
    const fmRows = (entity.characters || [])
      .map((cid) => {
        const ch = characterById(cid);
        const v = (setup.first_messages || {})[cid] || "";
        return `
          <label class="stack">
            <span class="muted small"><strong>${escapeHtml(ch?.name || cid)}</strong> first message override</span>
            <textarea rows="2" data-setup-firstmsg="${idx}.${cid}" placeholder="(falls back to scenario / character default)">${escapeHtml(v)}</textarea>
          </label>`;
      })
      .join("");
    card.innerHTML = `
      <div class="row gap" style="align-items: center; justify-content: space-between;">
        <div>
          <strong>${escapeHtml(setup.name || setup.id || "(unnamed)")}</strong>
          <span class="muted small">${escapeHtml(setup.id || "")}</span>
        </div>
        <div class="row gap">
          <button type="button" class="ghost xs" data-setup-up="${idx}" ${idx === 0 ? "disabled" : ""}>↑</button>
          <button type="button" class="ghost xs" data-setup-down="${idx}" ${idx === entity.setups.length - 1 ? "disabled" : ""}>↓</button>
          <button type="button" class="ghost xs" data-setup-remove="${idx}">Remove</button>
        </div>
      </div>
      <div class="editor-grid" style="margin-top: 0.5rem;">
        <label class="stack span-1">
          <span>Setup id</span>
          <input type="text" data-setup-field="${idx}.id" value="${escapeHtml(setup.id || "")}" />
        </label>
        <label class="stack span-1">
          <span>Display name</span>
          <input type="text" data-setup-field="${idx}.name" value="${escapeHtml(setup.name || "")}" />
        </label>
        <label class="stack span-2">
          <span>Description <span class="muted small">(shown in the setup picker)</span></span>
          <input type="text" data-setup-field="${idx}.description" value="${escapeHtml(setup.description || "")}" />
        </label>

        <label class="stack span-1">
          <span>User persona name <span class="muted small">(optional override)</span></span>
          <input type="text" data-setup-userpersona="${idx}.name" value="${escapeHtml((setup.user_persona && setup.user_persona.name) || "")}" />
        </label>
        <label class="stack span-1">
          <span>User persona description</span>
          <input type="text" data-setup-userpersona="${idx}.description" value="${escapeHtml((setup.user_persona && setup.user_persona.description) || "")}" />
        </label>

        <label class="stack span-2">
          <span>Opening narration <span class="muted small">(falls back to scenario.opening_prompt)</span></span>
          <textarea rows="4" data-setup-field="${idx}.opening_prompt" placeholder="(uses scenario opening_prompt)">${escapeHtml(setup.opening_prompt || "")}</textarea>
        </label>

        <label class="stack span-2">
          <span>Scenario instructions append <span class="muted small">(appended to scenario.scenario_instructions for this setup only)</span></span>
          <textarea rows="3" data-setup-field="${idx}.scenario_instructions_append" placeholder="(none)">${escapeHtml(setup.scenario_instructions_append || "")}</textarea>
        </label>

        <label class="stack span-2">
          <span>State directives <span class="muted small">(narrator-edit grammar — applied to the instance + presence on activation)</span></span>
          <textarea rows="5" class="json-textarea" data-setup-field="${idx}.state" spellcheck="false" placeholder="[move character_id -> room_id]&#10;[outfit character_id -> outfit_id]&#10;[set character_id.mood = &quot;tired&quot;]&#10;[set user.role = &quot;producer&quot;]">${escapeHtml(setup.state || "")}</textarea>
        </label>

        ${fmRows ? `<div class="span-2" style="margin-top: 0.25rem;"><h4 style="margin: 0.5rem 0;">Per-character first message overrides</h4>${fmRows}</div>` : ""}
      </div>
    `;
    root.appendChild(card);
  });

  root.querySelectorAll("[data-setup-field]").forEach((el) => {
    el.addEventListener("input", () => {
      const [idxStr, key] = el.dataset.setupField.split(".");
      const i = Number(idxStr);
      const setup = entity.setups[i];
      if (!setup) return;
      setup[key] = el.value;
    });
  });
  root.querySelectorAll("[data-setup-userpersona]").forEach((el) => {
    el.addEventListener("input", () => {
      const [idxStr, key] = el.dataset.setupUserpersona.split(".");
      const i = Number(idxStr);
      const setup = entity.setups[i];
      if (!setup) return;
      setup.user_persona = setup.user_persona || {};
      if (el.value.trim()) setup.user_persona[key] = el.value;
      else delete setup.user_persona[key];
      if (!Object.keys(setup.user_persona).length) delete setup.user_persona;
    });
  });
  root.querySelectorAll("[data-setup-firstmsg]").forEach((el) => {
    el.addEventListener("input", () => {
      const [idxStr, ...rest] = el.dataset.setupFirstmsg.split(".");
      const cid = rest.join(".");
      const i = Number(idxStr);
      const setup = entity.setups[i];
      if (!setup) return;
      setup.first_messages = setup.first_messages || {};
      const v = el.value;
      if (v.trim()) setup.first_messages[cid] = v;
      else delete setup.first_messages[cid];
      if (!Object.keys(setup.first_messages).length) delete setup.first_messages;
    });
  });
  root.querySelectorAll("[data-setup-remove]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const i = Number(btn.dataset.setupRemove);
      if (!confirm(`Remove setup "${entity.setups[i]?.name || entity.setups[i]?.id || ""}"?`)) return;
      entity.setups.splice(i, 1);
      renderSetups();
    });
  });
  root.querySelectorAll("[data-setup-up]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const i = Number(btn.dataset.setupUp);
      if (i <= 0) return;
      [entity.setups[i - 1], entity.setups[i]] = [entity.setups[i], entity.setups[i - 1]];
      renderSetups();
    });
  });
  root.querySelectorAll("[data-setup-down]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const i = Number(btn.dataset.setupDown);
      if (i >= entity.setups.length - 1) return;
      [entity.setups[i + 1], entity.setups[i]] = [entity.setups[i], entity.setups[i + 1]];
      renderSetups();
    });
  });
}

const addSetupBtn = document.getElementById("add-setup");
if (addSetupBtn) {
  addSetupBtn.addEventListener("click", () => {
    ensureSetups();
    const inp = document.getElementById("new-setup-id");
    const id = (inp.value || "").trim();
    if (!id) return;
    if (entity.setups.some((s) => s.id === id)) {
      alert(`A setup with id "${id}" already exists.`);
      return;
    }
    entity.setups.push({
      id,
      name: id,
      description: "",
      opening_prompt: "",
      scenario_instructions_append: "",
      state: "",
      first_messages: {},
    });
    inp.value = "";
    renderSetups();
  });
}

// --- Per-character first messages -----------------------------------------
function renderFirstMessages() {
  const root = document.getElementById("first-messages-list");
  root.innerHTML = "";
  const cast = entity.characters || [];
  cast.forEach((charId) => {
    const ch = characterById(charId);
    const val = (entity.first_messages || {})[charId] || "";
    const wrap = document.createElement("label");
    wrap.className = "stack";
    wrap.innerHTML = `
      <span><strong>${ch ? ch.name || charId : charId}</strong> <span class="muted small">${charId}</span></span>
      <textarea rows="3" data-firstmsg="${charId}" placeholder="(uses character's default first_message)">${val.replace(/</g, "&lt;")}</textarea>
    `;
    root.appendChild(wrap);
  });
  root.querySelectorAll("[data-firstmsg]").forEach((el) => {
    el.addEventListener("input", () => {
      entity.first_messages = entity.first_messages || {};
      const id = el.dataset.firstmsg;
      const v = el.value.trim();
      if (v) entity.first_messages[id] = el.value;
      else delete entity.first_messages[id];
    });
  });
}

// --- Save / Delete ---------------------------------------------------------
document.getElementById("save-entity").addEventListener("click", async (ev) => {
  ev.preventDefault();
  // Pull static form fields back; the cast / custom outfits / first messages
  // are already mutated directly on `entity`.
  const merged = collectForm(form, entity);
  // collectForm clobbers nested arrays/objects we manage manually — restore them.
  merged.characters = entity.characters || [];
  merged.locations = entity.locations || [];
  merged.starting_state = entity.starting_state || {};
  merged.first_messages = entity.first_messages || {};
  merged.custom_outfits = entity.custom_outfits || [];
  merged.character_overrides = entity.character_overrides || {};
  merged.setups = entity.setups || [];
  merged.type = "scenario";
  if (!merged.id) {
    setStatus("ID is required.", true);
    return;
  }
  try {
    const saved = await saveEntity(merged, isNew);
    entity = saved;
    setStatus(`Saved ${saved.id}.`);
    if (isNew) window.location.href = `/studio/scenario/${saved.id}`;
  } catch (e) {
    setStatus("Save failed: " + e.message, true);
  }
});

document.getElementById("delete-entity").addEventListener("click", async () => {
  if (isNew || !entity.id) return;
  if (!confirm(`Delete scenario ${entity.id}?`)) return;
  await deleteEntity(entity.id);
  window.location.href = "/studio";
});

// --- Boot ------------------------------------------------------------------
fillForm(form, entity);
renderLocationChecklist();
renderCast();
renderCustomOutfits();
renderFirstMessages();
renderSetups();
wireTabs(document);
wireJSONTab(document.getElementById("entity-json"), () => entity, (parsed) => {
  entity = parsed;
  fillForm(form, entity);
  renderLocationChecklist();
  renderCast();
  renderCustomOutfits();
  renderFirstMessages();
  renderSetups();
});
