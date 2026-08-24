// Shared form-builder for the right-panel layered editor.
//
// The library.js editor delegates to PanelForms.build(type, ...) when a
// builder exists for `type`. Each builder constructs a real form (no JSON
// textareas as the primary surface) under the given root, with every input
// carrying a data-path so origin tinting + collection work uniformly.
//
// Conventions:
//   - data-path="dotted.field"          → location in the entity object
//   - data-list="comma" | "lines"       → collected as a string array
//   - data-json="1"                     → collected as JSON (object/array)
//   - data-coerce="number"              → collected as a Number
//   - type="checkbox"                   → collected as boolean
//
// Builders return { rerenderDynamics, beforeSave } where:
//   - rerenderDynamics() rebuilds the dynamic widgets that don't have a
//     fixed shape (personality grid, body-part cards, etc.) so origin
//     tinting can be re-applied after structural changes.
//   - beforeSave(target) is invoked just before the form's data-paths are
//     read back; lets the builder do any last-mile work (none for now).

window.PanelForms = (function () {
  // -------------------------------------------------------------------------
  // Path / collect helpers
  // -------------------------------------------------------------------------

  function getPath(obj, path) {
    if (!path) return obj;
    return path.split(".").reduce((cur, k) => (cur == null ? undefined : cur[k]), obj);
  }
  function setPath(obj, path, value) {
    const keys = path.split(".");
    let cur = obj;
    for (let i = 0; i < keys.length - 1; i++) {
      const k = keys[i];
      if (cur[k] == null || typeof cur[k] !== "object") cur[k] = {};
      cur = cur[k];
    }
    cur[keys[keys.length - 1]] = value;
  }

  function originAt(originMap, path) {
    let cur = originMap;
    for (const k of path.split(".")) {
      if (cur == null || typeof cur !== "object") return null;
      cur = cur[k];
    }
    return typeof cur === "string" ? cur : null;
  }

  function applyOriginTints(root, origin) {
    for (const el of root.querySelectorAll("[data-path]")) {
      el.classList.remove("origin-template", "origin-scenario", "origin-instance", "origin-unset");
      const layer = originAt(origin || {}, el.dataset.path);
      if (layer) el.classList.add(`origin-${layer}`);
    }
  }

  function collectForm(root, target) {
    for (const el of root.querySelectorAll("[data-path]")) {
      const path = el.dataset.path;
      let v;
      if (el.type === "checkbox") {
        v = !!el.checked;
      } else if (el.dataset.json === "1") {
        const txt = (el.value || "").trim();
        try { v = txt ? JSON.parse(txt) : (el.dataset.empty === "array" ? [] : {}); }
        catch (e) { throw new Error(`Invalid JSON at ${path}: ${e.message}`); }
      } else if (el.dataset.list === "comma") {
        v = (el.value || "").split(",").map((s) => s.trim()).filter(Boolean);
      } else if (el.dataset.list === "lines") {
        v = (el.value || "").split("\n").map((s) => s.trim()).filter(Boolean);
      } else if (el.dataset.coerce === "number" || el.type === "range" || el.type === "number") {
        v = el.value === "" ? null : Number(el.value);
      } else {
        v = el.value;
      }
      setPath(target, path, v);
    }
  }

  // -------------------------------------------------------------------------
  // Element factories
  // -------------------------------------------------------------------------

  function mkInput(value, path, opts = {}) {
    const i = document.createElement("input");
    i.type = opts.type || "text";
    i.value = value == null ? "" : String(value);
    if (path) i.dataset.path = path;
    if (opts.placeholder) i.placeholder = opts.placeholder;
    if (opts.list) i.dataset.list = opts.list;
    if (opts.coerce) i.dataset.coerce = opts.coerce;
    if (opts.min != null) i.min = String(opts.min);
    if (opts.max != null) i.max = String(opts.max);
    if (opts.step != null) i.step = String(opts.step);
    if (opts.readonly) i.readOnly = true;
    return i;
  }
  function mkTextarea(value, path, rows = 4, opts = {}) {
    const t = document.createElement("textarea");
    t.rows = rows;
    t.spellcheck = false;
    if (Array.isArray(value)) {
      if (opts.list === "lines") t.value = value.join("\n");
      else if (opts.list === "comma") t.value = value.join(", ");
      else t.value = JSON.stringify(value);
    } else if (opts.json) {
      t.value = value == null ? "" : JSON.stringify(value, null, 2);
    } else {
      t.value = value == null ? "" : String(value);
    }
    if (path) t.dataset.path = path;
    if (opts.list) t.dataset.list = opts.list;
    if (opts.json) {
      t.dataset.json = "1";
      if (opts.empty === "array") t.dataset.empty = "array";
      t.classList.add("json-textarea");
    }
    return t;
  }
  function mkCheckbox(value, path) {
    const c = document.createElement("input");
    c.type = "checkbox";
    c.checked = !!value;
    if (path) c.dataset.path = path;
    return c;
  }
  function mkSelect(options, value, path) {
    const s = document.createElement("select");
    if (path) s.dataset.path = path;
    for (const [val, label] of options) {
      const o = document.createElement("option");
      o.value = val;
      o.textContent = label;
      if (val === (value || "")) o.selected = true;
      s.appendChild(o);
    }
    return s;
  }

  function field(label, input, hint) {
    const l = document.createElement("label");
    l.className = "stack panel-field";
    const span = document.createElement("span");
    span.className = "muted small";
    span.textContent = label;
    l.appendChild(span);
    l.appendChild(input);
    if (hint) {
      const p = document.createElement("p");
      p.className = "muted small";
      p.textContent = hint;
      l.appendChild(p);
    }
    return l;
  }
  function checkboxRow(labelText, checked, path) {
    const l = document.createElement("label");
    l.className = "row toggle";
    l.appendChild(mkCheckbox(checked, path));
    const sp = document.createElement("span");
    sp.textContent = labelText;
    l.appendChild(sp);
    return l;
  }
  function section(title) {
    const det = document.createElement("details");
    det.className = "panel-section";
    det.open = true;
    const sum = document.createElement("summary");
    const h = document.createElement("h4");
    h.textContent = title;
    sum.appendChild(h);
    det.appendChild(sum);
    return det;
  }

  // -------------------------------------------------------------------------
  // Builder registry
  // -------------------------------------------------------------------------

  const builders = {};

  function build(type, root, parsed, origin, ctx, entitiesCache, callbacks) {
    const builder = builders[type];
    if (!builder) return null;
    return builder(root, parsed, origin, ctx, entitiesCache, callbacks || {});
  }

  // -------------------------------------------------------------------------
  // Character form — the full studio sheet, in the panel
  // -------------------------------------------------------------------------

  builders.character = function buildCharacter(root, parsed, origin, ctx, entities, cb) {
    parsed.properties = parsed.properties || {};
    const props = parsed.properties;
    const allOutfits = Object.values(entities || {}).filter((e) => e.type === "outfit").sort(byName);
    const allLocations = Object.values(entities || {}).filter((e) => e.type === "location").sort(byName);
    const allRooms = Object.values(entities || {}).filter((e) => e.type === "room").sort(byName);

    // --- Identity ---
    const idSection = section("Identity");
    idSection.appendChild(field("ID (slug)", mkInput(parsed.id, "id", {
      placeholder: "e.g. iris", readonly: !!parsed.id,
    })));
    idSection.appendChild(field("Name", mkInput(parsed.name, "name")));
    idSection.appendChild(field("Tags", mkInput((parsed.tags || []).join(", "), "tags", { list: "comma" }),
      "Comma-separated."));
    idSection.appendChild(field("Description", mkTextarea(parsed.description, "description", 6)));
    idSection.appendChild(field("Example text", mkTextarea(parsed.example_text, "example_text", 3),
      "A representative one-liner — used as a stylistic primer."));
    root.appendChild(idSection);

    // --- Portrait ---
    const portraitSection = section("Portrait");
    const preview = document.createElement("div");
    preview.className = "portrait-preview";
    function refreshPreview() {
      preview.innerHTML = parsed.id
        ? `<img src="/portraits/${encodeURIComponent(parsed.id)}?t=${Date.now()}"
             alt="" onerror="this.replaceWith(document.createTextNode('no image'))" />`
        : `<div class="placeholder">save the character first to upload a portrait</div>`;
    }
    refreshPreview();
    portraitSection.appendChild(preview);
    if (parsed.id) {
      const fileInput = document.createElement("input");
      fileInput.type = "file";
      fileInput.accept = "image/*";
      fileInput.addEventListener("change", async () => {
        const f = fileInput.files[0];
        if (!f) return;
        const fd = new FormData();
        fd.append("file", f);
        try {
          const r = await fetch(`/api/entities/${parsed.id}/portrait`, { method: "POST", body: fd });
          if (!r.ok) {
            const err = await r.json().catch(() => ({}));
            throw new Error(err.error || r.statusText);
          }
          refreshPreview();
        } catch (e) { alert("Upload failed: " + e.message); }
      });
      portraitSection.appendChild(fileInput);
    }
    root.appendChild(portraitSection);

    // --- Defaults ---
    const defaultsSection = section("Defaults");
    defaultsSection.appendChild(field("Default outfit", mkSelect(
      [["", "(none)"], ...allOutfits.map((o) => [o.id, o.name || o.id])],
      props.current_outfit, "properties.current_outfit",
    )));
    defaultsSection.appendChild(field("Default location", mkSelect(
      [["", "(none)"], ...allLocations.map((l) => [l.id, l.name || l.id])],
      props.default_location, "properties.default_location",
    )));
    defaultsSection.appendChild(field("Default room", mkSelect(
      [["", "(none)"], ...allRooms.map((r) => [r.id, r.name || r.id])],
      props.default_room, "properties.default_room",
    )));
    defaultsSection.appendChild(checkboxRow("Can talk", props.can_talk !== false, "properties.can_talk"));
    defaultsSection.appendChild(checkboxRow("Can move between rooms", props.can_move !== false, "properties.can_move"));
    defaultsSection.appendChild(checkboxRow("Can edit world (narrator privileges)", !!props.can_edit_world, "properties.can_edit_world"));
    root.appendChild(defaultsSection);

    // --- Personality (dynamic sliders) ---
    const personalitySection = section("Personality");
    const personalityList = document.createElement("div");
    personalityList.className = "personality-grid";
    function rerenderPersonality() {
      personalityList.innerHTML = "";
      const stats = props.personality || {};
      for (const key of Object.keys(stats)) {
        const wrap = document.createElement("label");
        wrap.className = "personality-row";
        const keySpan = document.createElement("span");
        keySpan.className = "trait-key";
        keySpan.textContent = key;
        const slider = mkInput(Number(stats[key]) || 0, `properties.personality.${key}`,
          { type: "range", min: 0, max: 100, coerce: "number" });
        const valSpan = document.createElement("span");
        valSpan.className = "trait-val";
        valSpan.textContent = String(Number(stats[key]) || 0);
        slider.addEventListener("input", () => { valSpan.textContent = slider.value; });
        const rmBtn = document.createElement("button");
        rmBtn.type = "button";
        rmBtn.className = "ghost xs";
        rmBtn.textContent = "×";
        rmBtn.addEventListener("click", () => {
          delete props.personality[key];
          rerenderPersonality();
          applyOriginTints(root, origin);
        });
        wrap.append(keySpan, slider, valSpan, rmBtn);
        personalityList.appendChild(wrap);
      }
    }
    rerenderPersonality();
    personalitySection.appendChild(personalityList);
    const newKeyInput = document.createElement("input");
    newKeyInput.type = "text";
    newKeyInput.placeholder = "new trait (e.g. curiosity)";
    const addPerBtn = document.createElement("button");
    addPerBtn.type = "button";
    addPerBtn.className = "ghost small";
    addPerBtn.textContent = "+ Add";
    addPerBtn.addEventListener("click", () => {
      const k = newKeyInput.value.trim();
      if (!k) return;
      props.personality = props.personality || {};
      props.personality[k] = 50;
      newKeyInput.value = "";
      rerenderPersonality();
      applyOriginTints(root, origin);
    });
    const perAddRow = document.createElement("div");
    perAddRow.className = "row gap";
    perAddRow.append(newKeyInput, addPerBtn);
    personalitySection.appendChild(perAddRow);
    root.appendChild(personalitySection);

    // --- Body parts (dynamic cards) ---
    const bodySection = section("Body parts");
    const bodyList = document.createElement("div");
    bodyList.className = "bodyparts-list";
    function rerenderBodyParts() {
      bodyList.innerHTML = "";
      const parts = props.body_parts || {};
      for (const partKey of Object.keys(parts)) {
        const part = parts[partKey] || {};
        const card = document.createElement("div");
        card.className = "bodypart-card";
        const head = document.createElement("div");
        head.className = "bodypart-head";
        const strong = document.createElement("strong");
        strong.textContent = partKey;
        const rm = document.createElement("button");
        rm.type = "button";
        rm.className = "ghost xs";
        rm.textContent = "Remove";
        rm.addEventListener("click", () => {
          delete props.body_parts[partKey];
          rerenderBodyParts();
          applyOriginTints(root, origin);
        });
        head.append(strong, rm);
        card.appendChild(head);
        card.appendChild(field("Base (uncovered)",
          mkTextarea(part.base, `properties.body_parts.${partKey}.base`, 2)));
        card.appendChild(field("Clothed",
          mkTextarea(part.clothed_base, `properties.body_parts.${partKey}.clothed_base`, 2)));
        card.appendChild(checkboxRow("Covered by default",
          !!part.covered, `properties.body_parts.${partKey}.covered`));
        // State modifiers (rare; JSON for now)
        card.appendChild(field("State modifiers",
          mkTextarea(part.state_modifiers, `properties.body_parts.${partKey}.state_modifiers`, 3,
            { json: true }),
          "JSON: { state_name: \"description fragment\", … }"));
        bodyList.appendChild(card);
      }
    }
    rerenderBodyParts();
    bodySection.appendChild(bodyList);
    const newPartInput = document.createElement("input");
    newPartInput.type = "text";
    newPartInput.placeholder = "new part (e.g. tail)";
    const addPartBtn = document.createElement("button");
    addPartBtn.type = "button";
    addPartBtn.className = "ghost small";
    addPartBtn.textContent = "+ Add";
    addPartBtn.addEventListener("click", () => {
      const k = newPartInput.value.trim();
      if (!k) return;
      props.body_parts = props.body_parts || {};
      props.body_parts[k] = { base: "", clothed_base: "", covered: false };
      newPartInput.value = "";
      rerenderBodyParts();
      applyOriginTints(root, origin);
    });
    const partAddRow = document.createElement("div");
    partAddRow.className = "row gap";
    partAddRow.append(newPartInput, addPartBtn);
    bodySection.appendChild(partAddRow);
    root.appendChild(bodySection);

    // --- Owned outfits ---
    const outfitsSection = section("Owned outfits");
    const outfitsUl = document.createElement("ul");
    outfitsUl.className = "outfit-list";
    function rerenderOutfits() {
      outfitsUl.innerHTML = "";
      const list = props.outfits || [];
      list.forEach((oid, idx) => {
        const li = document.createElement("li");
        const open = document.createElement("button");
        open.type = "button";
        open.className = "link-btn";
        const o = entities[oid];
        open.textContent = o ? (o.name || o.id) : `${oid} (missing)`;
        open.addEventListener("click", () => {
          if (entities[oid] && cb.openEntity) cb.openEntity(entities[oid]);
        });
        const rm = document.createElement("button");
        rm.type = "button";
        rm.className = "ghost xs";
        rm.textContent = "Remove";
        rm.addEventListener("click", () => {
          props.outfits.splice(idx, 1);
          rerenderOutfits();
        });
        li.append(open, rm);
        outfitsUl.appendChild(li);
      });
    }
    rerenderOutfits();
    outfitsSection.appendChild(outfitsUl);
    const outfitSel = mkSelect(
      [["", "— add existing outfit —"], ...allOutfits.map((o) => [o.id, o.name || o.id])],
      "", null,
    );
    const addOutfitBtn = document.createElement("button");
    addOutfitBtn.type = "button";
    addOutfitBtn.className = "ghost small";
    addOutfitBtn.textContent = "Add";
    addOutfitBtn.addEventListener("click", () => {
      const id = outfitSel.value;
      if (!id) return;
      props.outfits = props.outfits || [];
      if (!props.outfits.includes(id)) props.outfits.push(id);
      outfitSel.value = "";
      rerenderOutfits();
    });
    const outfitAddRow = document.createElement("div");
    outfitAddRow.className = "row gap";
    outfitAddRow.append(outfitSel, addOutfitBtn);
    outfitsSection.appendChild(outfitAddRow);
    root.appendChild(outfitsSection);

    // --- Dialogue ---
    const dialogueSection = section("Dialogue");
    dialogueSection.appendChild(field("First message",
      mkTextarea(props.first_message, "properties.first_message", 5),
      "Macros: {{user}} / {{char}} / {{user.field}} expand at render time."));
    dialogueSection.appendChild(field("Dialogue examples",
      mkTextarea(props.dialogue_examples, "properties.dialogue_examples", 6, { list: "lines" }),
      "One per line. Used as primer turns."));
    root.appendChild(dialogueSection);

    // --- Relationships (kept JSON; uncommon) ---
    const relSection = section("Relationships");
    relSection.appendChild(field("Relationships",
      mkTextarea(props.relationships, "properties.relationships", 8, { json: true }),
      "JSON keyed by character id: { \"<id>\": \"how this char feels about them\", … }"));
    root.appendChild(relSection);

    // --- Image pack ---
    // Per-character image config. Two formats supported:
    //   combined : sprite_id points at a directory of layered PNGs the
    //              compositor blends from current outfit + room state
    //              (Iris, Dex). No catalog; no model call.
    //   tagged   : entries[] is a flat catalog of {caption, image_url};
    //              the chat picks one after each reply via a model call.
    // Either format participates only when the conversation's "Image"
    // toggle is on. Authoring goes through properties.images.*; the
    // legacy properties.image_pack.entries field is still read by
    // sprite_url.tagged_entries_of as a fallback for unmigrated
    // characters (legacy tagged catalog), and surfaced below when populated so existing
    // data is editable.
    const images = props.images || {};
    const legacyPack = props.image_pack || {};
    const imgSection = section("Image pack");
    imgSection.appendChild(field("Format",
      mkSelect(
        [["", "(none)"], ["combined", "Combined sprite layers"], ["tagged", "Tagged catalog"]],
        images.format || "",
        "properties.images.format",
      ),
      "Combined: server composes from current outfit + room state. Tagged: chat picks from a {caption, image_url} catalog."));
    imgSection.appendChild(field("Sprite id",
      mkInput(images.sprite_id || "", "properties.images.sprite_id",
        { placeholder: "e.g. Iris1" }),
      "Combined-format only. Names the asset directory under config.sprite_assets_dir; the compositor reads Image1.png … Image14.png from there."));
    imgSection.appendChild(field("Entries",
      mkTextarea(images.entries || [], "properties.images.entries", 10,
        { json: true, empty: "array" }),
      "Tagged-format only. JSON array of { \"caption\": \"…\", \"image_url\": \"https://…\" }. Captions become alt text and are matched against the speaker's reply at pick time."));
    if (Array.isArray(legacyPack.entries) && legacyPack.entries.length) {
      imgSection.appendChild(field("Legacy entries",
        mkTextarea(legacyPack.entries, "properties.image_pack.entries", 6,
          { json: true, empty: "array" }),
        "Read-through fallback at properties.image_pack.entries. Migrate to properties.images.entries with format=tagged when convenient."));
    }
    root.appendChild(imgSection);

    return {
      rerenderDynamics() {
        rerenderPersonality();
        rerenderBodyParts();
        rerenderOutfits();
      },
    };
  };

  function byName(a, b) {
    return (a.name || a.id || "").localeCompare(b.name || b.id || "");
  }

  // -------------------------------------------------------------------------
  // Location form — identity, atmosphere, nested rooms
  // -------------------------------------------------------------------------

  builders.location = function buildLocation(root, parsed, origin, ctx, entities, cb) {
    parsed.properties = parsed.properties || {};
    parsed.children = parsed.children || [];
    const props = parsed.properties;
    const allRooms = Object.values(entities || {}).filter((e) => e.type === "room").sort(byName);

    // --- Identity ---
    const idSection = section("Identity");
    idSection.appendChild(field("ID (slug)", mkInput(parsed.id, "id", {
      placeholder: "e.g. the_marginalia", readonly: !!parsed.id,
    })));
    idSection.appendChild(field("Name", mkInput(parsed.name, "name")));
    idSection.appendChild(field("Tags", mkInput((parsed.tags || []).join(", "), "tags", { list: "comma" }),
      "Comma-separated."));
    idSection.appendChild(field("Description", mkTextarea(parsed.description, "description", 6)));
    idSection.appendChild(field("Example text", mkTextarea(parsed.example_text, "example_text", 3)));
    root.appendChild(idSection);

    // --- Atmosphere ---
    const atmoSection = section("Atmosphere");
    atmoSection.appendChild(field("Ambient sounds", mkInput(props.ambient_sounds, "properties.ambient_sounds")));
    atmoSection.appendChild(field("Lighting", mkInput(props.lighting, "properties.lighting")));
    atmoSection.appendChild(field("Atmosphere", mkInput(props.atmosphere, "properties.atmosphere")));
    root.appendChild(atmoSection);

    // --- Rooms (children) ---
    const roomsSection = section("Rooms");
    const hint = document.createElement("p");
    hint.className = "muted small";
    hint.textContent = "Stored under data/locations/<id>/rooms/. Click a room to edit it.";
    roomsSection.appendChild(hint);
    const roomsUl = document.createElement("ul");
    roomsUl.className = "room-list";
    function rerenderRooms() {
      roomsUl.innerHTML = "";
      const ids = parsed.children || [];
      ids.forEach((rid, idx) => {
        const li = document.createElement("li");
        const open = document.createElement("button");
        open.type = "button";
        open.className = "link-btn";
        const r = entities[rid];
        open.textContent = r ? (r.name || r.id) : `${rid} (missing)`;
        open.addEventListener("click", () => {
          if (entities[rid] && cb.openEntity) cb.openEntity(entities[rid]);
        });
        const rm = document.createElement("button");
        rm.type = "button";
        rm.className = "ghost xs";
        rm.textContent = "Unlink";
        rm.addEventListener("click", () => {
          parsed.children.splice(idx, 1);
          rerenderRooms();
        });
        li.append(open, rm);
        roomsUl.appendChild(li);
      });
    }
    rerenderRooms();
    roomsSection.appendChild(roomsUl);

    // Link an existing room
    const linkSel = mkSelect(
      [["", "— link existing room —"], ...allRooms
        .filter((r) => !(parsed.children || []).includes(r.id))
        .map((r) => [r.id, r.name || r.id])],
      "", null,
    );
    const linkBtn = document.createElement("button");
    linkBtn.type = "button";
    linkBtn.className = "ghost small";
    linkBtn.textContent = "Link";
    linkBtn.addEventListener("click", () => {
      const id = linkSel.value;
      if (!id) return;
      parsed.children = parsed.children || [];
      if (!parsed.children.includes(id)) parsed.children.push(id);
      linkSel.value = "";
      rerenderRooms();
    });
    const linkRow = document.createElement("div");
    linkRow.className = "row gap";
    linkRow.append(linkSel, linkBtn);
    roomsSection.appendChild(linkRow);

    // Create a new room (writes to template via /api/entities then links).
    // Only available when this location already exists, since the new
    // room needs to land under the location's folder.
    if (parsed.id) {
      const newRoomId = document.createElement("input");
      newRoomId.type = "text";
      newRoomId.placeholder = "new room id (e.g. lobby)";
      const newRoomName = document.createElement("input");
      newRoomName.type = "text";
      newRoomName.placeholder = "name";
      const createBtn = document.createElement("button");
      createBtn.type = "button";
      createBtn.className = "ghost small";
      createBtn.textContent = "+ Create";
      createBtn.addEventListener("click", async () => {
        const id = (newRoomId.value || "").trim();
        const name = (newRoomName.value || "").trim();
        if (!id) return;
        try {
          // Pre-link in `parsed.children` so the server's _file_for routes
          // the new room under this location. Save the location first so
          // children is persisted, then create the room.
          parsed.children = parsed.children || [];
          if (!parsed.children.includes(id)) parsed.children.push(id);
          await fetch(`/api/entities/${parsed.id}`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(parsed),
          });
          await fetch(`/api/entities`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              id, type: "room", name: name || id, description: "",
              tags: [], children: [], properties: { exits: [] }, example_text: "",
            }),
          });
          newRoomId.value = ""; newRoomName.value = "";
          alert(`Created ${id}.`);
          // Refresh by re-rendering: the caller will repopulate entities.
          if (cb.refresh) cb.refresh();
        } catch (e) { alert("Create failed: " + e.message); }
      });
      const createRow = document.createElement("div");
      createRow.className = "row gap";
      createRow.append(newRoomId, newRoomName, createBtn);
      roomsSection.appendChild(createRow);
    }
    root.appendChild(roomsSection);

    return {
      rerenderDynamics() { rerenderRooms(); },
    };
  };

  // -------------------------------------------------------------------------
  // Outfit form — identity, inheritance, look, coverage map
  // -------------------------------------------------------------------------

  builders.outfit = function buildOutfit(root, parsed, origin, ctx, entities, cb) {
    parsed.properties = parsed.properties || {};
    const props = parsed.properties;
    const allOutfits = Object.values(entities || {}).filter((e) => e.type === "outfit").sort(byName);
    const allCharacters = Object.values(entities || {}).filter((e) => e.type === "character").sort(byName);

    // --- Identity ---
    const idSection = section("Identity");
    idSection.appendChild(field("ID (slug)", mkInput(parsed.id, "id", { readonly: !!parsed.id })));
    idSection.appendChild(field("Name", mkInput(parsed.name, "name")));
    idSection.appendChild(field("Tags", mkInput((parsed.tags || []).join(", "), "tags", { list: "comma" })));
    idSection.appendChild(field("Description", mkTextarea(parsed.description, "description", 4)));
    idSection.appendChild(field("Example text", mkTextarea(parsed.example_text, "example_text", 2)));
    root.appendChild(idSection);

    // --- Inheritance ---
    const inhSection = section("Inheritance");
    inhSection.appendChild(field("Extends (base outfit)", mkSelect(
      [["", "(none)"], ...allOutfits
        .filter((o) => o.id !== parsed.id)
        .map((o) => [o.id, o.name || o.id])],
      props.extends, "properties.extends",
    ), "Coverage from the base layers in; this outfit's coverage overrides per body part."));
    inhSection.appendChild(field("Owner character", mkSelect(
      [["", "(none)"], ...allCharacters.map((c) => [c.id, c.name || c.id])],
      props.owner, "properties.owner",
    )));
    inhSection.appendChild(field("Slot", mkInput(props.slot, "properties.slot",
      { placeholder: "body / head / hand / …" })));
    inhSection.appendChild(checkboxRow("Can be taken off / picked up",
      !!props.can_take, "properties.can_take"));
    root.appendChild(inhSection);

    // --- Look ---
    const lookSection = section("Look");
    lookSection.appendChild(field("Intact description",
      mkTextarea(props.intact_description, "properties.intact_description", 3)));
    lookSection.appendChild(field("Concise description",
      mkTextarea(props.concise_description, "properties.concise_description", 2)));
    const looksRow1 = document.createElement("div");
    looksRow1.className = "row gap";
    looksRow1.append(
      field("Material", mkInput(props.material, "properties.material")),
      field("Color", mkInput(props.color, "properties.color")),
      field("Fit", mkInput(props.fit, "properties.fit")),
    );
    lookSection.appendChild(looksRow1);
    const looksRow2 = document.createElement("div");
    looksRow2.className = "row gap";
    looksRow2.append(
      field("Style", mkInput(props.style, "properties.style")),
      field("Formality", mkInput(props.formality, "properties.formality")),
      field("Condition", mkInput(props.condition, "properties.condition",
        { placeholder: "intact / torn / soaked" })),
    );
    lookSection.appendChild(looksRow2);
    root.appendChild(lookSection);

    // --- Coverage map (dynamic cards) ---
    const covSection = section("Coverage");
    const covHint = document.createElement("p");
    covHint.className = "muted small";
    covHint.textContent = "One entry per body part. Leave blank to fall through to the base outfit's coverage.";
    covSection.appendChild(covHint);
    const covList = document.createElement("div");
    covList.className = "bodyparts-list";
    function rerenderCoverage() {
      covList.innerHTML = "";
      const cov = props.coverage || {};
      for (const partKey of Object.keys(cov)) {
        const c = cov[partKey] || {};
        const card = document.createElement("div");
        card.className = "bodypart-card";
        const head = document.createElement("div");
        head.className = "bodypart-head";
        const strong = document.createElement("strong");
        strong.textContent = partKey;
        const rm = document.createElement("button");
        rm.type = "button";
        rm.className = "ghost xs";
        rm.textContent = "Remove";
        rm.addEventListener("click", () => {
          delete props.coverage[partKey];
          rerenderCoverage();
          applyOriginTints(root, origin);
        });
        head.append(strong, rm);
        card.appendChild(head);
        card.appendChild(checkboxRow("Covered", !!c.covered, `properties.coverage.${partKey}.covered`));
        card.appendChild(field("Opacity", mkSelect(
          [["opaque", "opaque"], ["sheer", "sheer (body visible through fabric)"], ["transparent", "transparent (essentially see-through)"]],
          c.opacity || "opaque", `properties.coverage.${partKey}.opacity`,
        ), "Sheer/transparent compose the body's base description through the garment."));
        card.appendChild(field("Description",
          mkTextarea(c.description, `properties.coverage.${partKey}.description`, 2)));
        card.appendChild(field("Reveals (optional override)",
          mkTextarea(c.reveals, `properties.coverage.${partKey}.reveals`, 2),
          "If set, replaces the auto-composed sheer/transparent text. Use for one-off effects (\"clinging where it's wet\", etc.)."));
        covList.appendChild(card);
      }
    }
    rerenderCoverage();
    covSection.appendChild(covList);
    const newPart = document.createElement("input");
    newPart.type = "text";
    newPart.placeholder = "body part (e.g. chest)";
    const addPart = document.createElement("button");
    addPart.type = "button";
    addPart.className = "ghost small";
    addPart.textContent = "+ Add";
    addPart.addEventListener("click", () => {
      const k = newPart.value.trim();
      if (!k) return;
      props.coverage = props.coverage || {};
      if (!props.coverage[k]) props.coverage[k] = { covered: true, description: "" };
      newPart.value = "";
      rerenderCoverage();
      applyOriginTints(root, origin);
    });
    const addRow = document.createElement("div");
    addRow.className = "row gap";
    addRow.append(newPart, addPart);
    covSection.appendChild(addRow);
    root.appendChild(covSection);

    return { rerenderDynamics() { rerenderCoverage(); } };
  };

  // -------------------------------------------------------------------------
  // Room form — identity + size / lighting / exits
  // -------------------------------------------------------------------------

  builders.room = function buildRoom(root, parsed, origin, ctx, entities, cb) {
    parsed.properties = parsed.properties || {};
    const props = parsed.properties;
    const allRooms = Object.values(entities || {}).filter((e) => e.type === "room").sort(byName);

    const idSection = section("Identity");
    idSection.appendChild(field("ID (slug)", mkInput(parsed.id, "id", { readonly: !!parsed.id })));
    idSection.appendChild(field("Name", mkInput(parsed.name, "name")));
    idSection.appendChild(field("Tags", mkInput((parsed.tags || []).join(", "), "tags", { list: "comma" })));
    idSection.appendChild(field("Description", mkTextarea(parsed.description, "description", 5)));
    idSection.appendChild(field("Example text", mkTextarea(parsed.example_text, "example_text", 2)));
    root.appendChild(idSection);

    const propSection = section("Properties");
    propSection.appendChild(field("Size", mkInput(props.size, "properties.size",
      { placeholder: "small / medium / large" })));
    propSection.appendChild(field("Lighting", mkInput(props.lighting, "properties.lighting")));
    propSection.appendChild(field("Exits (room ids, comma-separated)",
      mkInput((props.exits || []).join(", "), "properties.exits", { list: "comma" }),
      "Each exit is the id of a connected room. " +
      `Valid ids: ${allRooms.map((r) => r.id).slice(0, 6).join(", ")}${allRooms.length > 6 ? ", …" : ""}`));
    root.appendChild(propSection);

    return { rerenderDynamics() {} };
  };

  // -------------------------------------------------------------------------
  // Object form — identity + slot/takeable/state
  // -------------------------------------------------------------------------

  builders.object = function buildObject(root, parsed, origin, ctx, entities, cb) {
    parsed.properties = parsed.properties || {};
    const props = parsed.properties;

    const idSection = section("Identity");
    idSection.appendChild(field("ID (slug)", mkInput(parsed.id, "id", { readonly: !!parsed.id })));
    idSection.appendChild(field("Name", mkInput(parsed.name, "name")));
    idSection.appendChild(field("Tags", mkInput((parsed.tags || []).join(", "), "tags", { list: "comma" })));
    idSection.appendChild(field("Description", mkTextarea(parsed.description, "description", 5),
      "What it is, what it looks like."));
    idSection.appendChild(field("Example text", mkTextarea(parsed.example_text, "example_text", 2)));
    root.appendChild(idSection);

    const propSection = section("Properties");
    propSection.appendChild(field("Slot",
      mkInput(props.slot, "properties.slot", { placeholder: "held / pocket / hand / wrist / waist / …" }),
      "Where this object lives when picked up or placed."));
    propSection.appendChild(checkboxRow("Takeable", !!props.takeable, "properties.takeable"));
    propSection.appendChild(checkboxRow("Consumable", !!props.consumable, "properties.consumable"));
    propSection.appendChild(field("Default state",
      mkInput(props.state, "properties.state", { placeholder: "full / empty / on / off" })));
    propSection.appendChild(field("Default owner / location",
      mkInput(props.default_owner, "properties.default_owner")));
    root.appendChild(propSection);

    // Effect + Limitations — surfaced into the character system prompt
    // under [Items in scene]. The "load-bearing" gameplay text for any
    // cursed / magical item lives here.
    const effectSection = section("Effect");
    effectSection.appendChild(field("Effect",
      mkTextarea(props.effect, "properties.effect", 6, {
        placeholder: "While worn, anyone interacting with the wearer is convinced …"
      }),
      "What this object does in scene. Rendered into the system prompt."));
    effectSection.appendChild(field("Limitations",
      mkTextarea(props.limitations, "properties.limitations", 4, {
        placeholder: "Edge cases, escape hatches, who it doesn't work on, when it ends."
      })));
    root.appendChild(effectSection);

    // Descriptive metadata — not engine-read today, but authoring
    // conventions the Trap items use; expose so they round-trip
    // through the form.
    const metaSection = section("Item metadata");
    metaSection.appendChild(field("Rarity",
      mkInput(props.rarity, "properties.rarity", { placeholder: "common / rare / very_rare / …" })));
    metaSection.appendChild(field("Curse type",
      mkInput(props.curse_type, "properties.curse_type", { placeholder: "perception_override / behavioral_compulsion / …" })));
    metaSection.appendChild(field("Duration",
      mkInput(props.duration, "properties.duration", { placeholder: "while_worn / one_session / permanent / …" })));
    metaSection.appendChild(field("Key rule",
      mkInput(props.key_rule, "properties.key_rule", { placeholder: "One-line summary of the load-bearing rule." })));
    root.appendChild(metaSection);

    return { rerenderDynamics() {} };
  };

  // -------------------------------------------------------------------------
  // Scenario form — identity, modes, locations, cast, custom outfits, opening
  // -------------------------------------------------------------------------

  builders.scenario = function buildScenario(root, parsed, origin, ctx, entities, cb) {
    parsed.characters = parsed.characters || [];
    parsed.locations = parsed.locations || [];
    parsed.starting_state = parsed.starting_state || {};
    parsed.first_messages = parsed.first_messages || {};
    parsed.custom_outfits = parsed.custom_outfits || [];
    parsed.character_overrides = parsed.character_overrides || {};

    const allCharacters = Object.values(entities || {}).filter((e) => e.type === "character").sort(byName);
    const allLocations = Object.values(entities || {}).filter((e) => e.type === "location").sort(byName);
    const allRooms = Object.values(entities || {}).filter((e) => e.type === "room").sort(byName);
    const allOutfits = Object.values(entities || {}).filter((e) => e.type === "outfit").sort(byName);

    function roomsForLoc(locId) {
      const loc = entities[locId];
      const ids = (loc && loc.children) || [];
      return allRooms.filter((r) => ids.includes(r.id));
    }
    function outfitsForChar(charId) {
      const ch = entities[charId];
      const owned = ((ch && ch.properties && ch.properties.outfits) || []);
      const own = allOutfits.filter((o) => owned.includes(o.id));
      const customs = (parsed.custom_outfits || []).filter(
        (o) => o && (!o.properties || !o.properties.owner || o.properties.owner === charId),
      );
      return [...own, ...customs];
    }
    function roomsForActiveLocations() {
      const locs = parsed.locations || [];
      if (!locs.length) return allRooms;
      const out = [];
      for (const l of locs) out.push(...roomsForLoc(l));
      return out;
    }

    // --- Identity ---
    const idSection = section("Identity");
    idSection.appendChild(field("ID (slug)", mkInput(parsed.id, "id", { readonly: !!parsed.id })));
    idSection.appendChild(field("Name", mkInput(parsed.name, "name")));
    idSection.appendChild(field("Tags", mkInput((parsed.tags || []).join(", "), "tags", { list: "comma" })));
    idSection.appendChild(field("Description", mkTextarea(parsed.description, "description", 4)));
    idSection.appendChild(field("Example text", mkTextarea(parsed.example_text, "example_text", 2)));
    root.appendChild(idSection);

    // --- Modes ---
    const modesSection = section("Modes");
    modesSection.appendChild(field("Turn mode", mkSelect(
      [["manual", "manual"], ["rotating", "rotating"], ["scenario", "scenario"]],
      parsed.turn_mode, "turn_mode",
    )));
    modesSection.appendChild(field("Narrator mode", mkSelect(
      [["auto", "auto"], ["manual", "manual"], ["off", "off"]],
      parsed.narrator_mode, "narrator_mode",
    )));
    modesSection.appendChild(field("Context limit (tokens)", mkInput(
      parsed.context_limit_tokens, "context_limit_tokens",
      { type: "number", min: 1024, step: 256, coerce: "number" },
    )));
    root.appendChild(modesSection);

    // --- Locations (checklist) ---
    const locsSection = section("Locations");
    const locHint = document.createElement("p");
    locHint.className = "muted small";
    locHint.textContent = "Locations whose rooms are available in this scenario.";
    locsSection.appendChild(locHint);
    const locUl = document.createElement("ul");
    locUl.className = "checklist";
    for (const l of allLocations) {
      const li = document.createElement("li");
      const lbl = document.createElement("label");
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = parsed.locations.includes(l.id);
      cb.addEventListener("change", () => {
        if (cb.checked) {
          if (!parsed.locations.includes(l.id)) parsed.locations.push(l.id);
        } else {
          parsed.locations = parsed.locations.filter((x) => x !== l.id);
        }
        rerenderCast();  // refresh room dropdowns
      });
      lbl.append(cb, document.createTextNode(" " + (l.name || l.id)));
      li.appendChild(lbl);
      locUl.appendChild(li);
    }
    locsSection.appendChild(locUl);
    root.appendChild(locsSection);

    // --- Cast (dynamic) ---
    const castSection = section("Cast");
    const castHint = document.createElement("p");
    castHint.className = "muted small";
    castHint.textContent = "Per-character starting location, room, outfit, and a first message override.";
    castSection.appendChild(castHint);
    const castList = document.createElement("div");
    castList.className = "cast-cards";
    function rerenderCast() {
      castList.innerHTML = "";
      const cast = parsed.characters || [];
      cast.forEach((charId) => {
        const ch = entities[charId];
        const start = parsed.starting_state[charId] || {};
        const card = document.createElement("div");
        card.className = "cast-card";
        const head = document.createElement("div");
        head.className = "cast-head";
        const strong = document.createElement("strong");
        strong.textContent = ch ? (ch.name || ch.id) : `${charId} (missing)`;
        const muted = document.createElement("span");
        muted.className = "muted small";
        muted.textContent = charId;
        const rm = document.createElement("button");
        rm.type = "button";
        rm.className = "ghost xs";
        rm.textContent = "Remove";
        rm.addEventListener("click", () => {
          parsed.characters = parsed.characters.filter((x) => x !== charId);
          delete parsed.starting_state[charId];
          delete parsed.first_messages[charId];
          delete parsed.character_overrides[charId];
          rerenderCast();
        });
        head.append(strong, muted, rm);
        card.appendChild(head);

        const grid = document.createElement("div");
        grid.className = "cast-grid";
        const locSel = mkSelect(
          [["", "(none)"], ...allLocations.map((l) => [l.id, l.name || l.id])],
          start.location || "", null,
        );
        locSel.addEventListener("change", () => {
          parsed.starting_state[charId] = parsed.starting_state[charId] || {};
          parsed.starting_state[charId].location = locSel.value || null;
        });
        const roomSel = mkSelect(
          [["", "(none)"], ...roomsForActiveLocations().map((r) => [r.id, r.name || r.id])],
          start.room || "", null,
        );
        roomSel.addEventListener("change", () => {
          parsed.starting_state[charId] = parsed.starting_state[charId] || {};
          parsed.starting_state[charId].room = roomSel.value || null;
        });
        const outfitSel = mkSelect(
          [["", "(default)"], ...outfitsForChar(charId).map((o) => [o.id, o.name || o.id])],
          start.outfit || "", null,
        );
        outfitSel.addEventListener("change", () => {
          parsed.starting_state[charId] = parsed.starting_state[charId] || {};
          parsed.starting_state[charId].outfit = outfitSel.value || null;
        });
        grid.append(
          field("Starting location", locSel),
          field("Starting room", roomSel),
          field("Starting outfit", outfitSel),
        );
        card.appendChild(grid);

        // Per-character first_message override
        const fmTa = mkTextarea(
          parsed.first_messages[charId] || "", null, 3,
        );
        fmTa.addEventListener("input", () => {
          if (fmTa.value.trim()) parsed.first_messages[charId] = fmTa.value;
          else delete parsed.first_messages[charId];
        });
        card.appendChild(field(
          "First message (override)",
          fmTa,
          "Leave blank to use the character's own first message.",
        ));

        // Open the character's card in scenario context
        const open = document.createElement("button");
        open.type = "button";
        open.className = "ghost small";
        open.textContent = "Edit character (this scenario)";
        open.addEventListener("click", () => {
          if (entities[charId] && cb.openEntity) cb.openEntity(entities[charId]);
        });
        card.appendChild(open);

        castList.appendChild(card);
      });
    }
    rerenderCast();
    castSection.appendChild(castList);
    // Add to cast
    const castSel = mkSelect(
      [["", "— add character —"], ...allCharacters
        .filter((c) => !(parsed.characters || []).includes(c.id))
        .map((c) => [c.id, c.name || c.id])],
      "", null,
    );
    const castAddBtn = document.createElement("button");
    castAddBtn.type = "button";
    castAddBtn.className = "ghost small";
    castAddBtn.textContent = "Add to cast";
    castAddBtn.addEventListener("click", () => {
      const id = castSel.value;
      if (!id) return;
      parsed.characters = parsed.characters || [];
      if (!parsed.characters.includes(id)) parsed.characters.push(id);
      castSel.value = "";
      rerenderCast();
    });
    const castAddRow = document.createElement("div");
    castAddRow.className = "row gap";
    castAddRow.append(castSel, castAddBtn);
    castSection.appendChild(castAddRow);
    root.appendChild(castSection);

    // --- Custom outfits (dynamic) ---
    const customSection = section("Custom outfits");
    const customHint = document.createElement("p");
    customHint.className = "muted small";
    customHint.textContent =
      "Outfits authored inline that exist only in this scenario instance. " +
      "Click to open the outfit editor scoped to this scenario.";
    customSection.appendChild(customHint);
    const customUl = document.createElement("ul");
    customUl.className = "custom-outfit-list";
    function rerenderCustom() {
      customUl.innerHTML = "";
      (parsed.custom_outfits || []).forEach((o, idx) => {
        const li = document.createElement("li");
        li.className = "custom-outfit-row";
        const open = document.createElement("button");
        open.type = "button";
        open.className = "link-btn";
        open.textContent = (o.name || o.id || "(unnamed)") +
          (o.properties && o.properties.owner ? ` — ${o.properties.owner}` : "");
        open.addEventListener("click", () => {
          // Open as a scenario-only entity. The panel will fetch
          // /api/effective/<id>?scenario=<sid> which falls into the
          // "scenario-only entity" branch in layers.effective_entity.
          if (cb.openEntity) cb.openEntity({ id: o.id, type: "outfit" });
        });
        const rm = document.createElement("button");
        rm.type = "button";
        rm.className = "ghost xs";
        rm.textContent = "Remove";
        rm.addEventListener("click", () => {
          parsed.custom_outfits.splice(idx, 1);
          rerenderCustom();
        });
        li.append(open, rm);
        customUl.appendChild(li);
      });
    }
    rerenderCustom();
    customSection.appendChild(customUl);
    const newCustomId = document.createElement("input");
    newCustomId.type = "text";
    newCustomId.placeholder = "id";
    const newCustomName = document.createElement("input");
    newCustomName.type = "text";
    newCustomName.placeholder = "name";
    const newCustomBase = mkSelect(
      [["", "(no base)"], ...allOutfits.map((o) => [o.id, o.name || o.id])],
      "", null,
    );
    const customAdd = document.createElement("button");
    customAdd.type = "button";
    customAdd.className = "ghost small";
    customAdd.textContent = "+ Add";
    customAdd.addEventListener("click", () => {
      const id = (newCustomId.value || "").trim();
      if (!id) return;
      parsed.custom_outfits.push({
        id, type: "outfit",
        name: (newCustomName.value || "").trim() || id,
        description: "",
        tags: [],
        children: [],
        properties: { extends: newCustomBase.value || "", owner: "", coverage: {} },
        example_text: "",
      });
      newCustomId.value = ""; newCustomName.value = ""; newCustomBase.value = "";
      rerenderCustom();
    });
    const customAddRow = document.createElement("div");
    customAddRow.className = "row gap";
    customAddRow.append(newCustomId, newCustomName, newCustomBase, customAdd);
    customSection.appendChild(customAddRow);
    root.appendChild(customSection);

    // --- Opening ---
    const openSection = section("Opening");
    openSection.appendChild(field("Opening narration",
      mkTextarea(parsed.opening_prompt, "opening_prompt", 5),
      "The narrator's root message. Set the scene before anyone speaks."));
    openSection.appendChild(field("Scenario instructions",
      mkTextarea(parsed.scenario_instructions, "scenario_instructions", 5),
      "Appended to the system block on every prompt in this scenario."));
    root.appendChild(openSection);

    return {
      rerenderDynamics() {
        rerenderCast();
        rerenderCustom();
      },
    };
  };

  return { build, applyOriginTints, collectForm, builders };
})();
