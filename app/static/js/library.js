// Right-panel library: tabs for Chats / entity types / Prompt / Dev.
// Caches list data so flipping between tabs is instant. Caches invalidate on
// writes (save / delete / new conversation).

(function () {
  const tabsEl = document.getElementById("right-tabs");
  const bodyEl = document.getElementById("tab-body");
  if (!tabsEl || !bodyEl) return;

  const ENTITY_TABS = {
    scenarios: { type: "scenario", label: "Scenarios", canStart: true },
    characters: { type: "character", label: "Characters" },
    locations: { type: "location", label: "Locations" },
    rooms: { type: "room", label: "Rooms" },
    objects: { type: "object", label: "Objects" },
    outfits: { type: "outfit", label: "Outfits" },
    lore: { type: "lore", label: "Lore" },
  };

  // ------------------------------------------------------------------------
  // Caches: avoid hammering the server when the user just clicks tabs.
  // ------------------------------------------------------------------------
  const cache = {
    entities: null,        // dict keyed by id
    conversations: null,   // array
  };
  function invalidateEntities() { cache.entities = null; }
  function invalidateConversations() { cache.conversations = null; }
  async function getEntities() {
    if (cache.entities) return cache.entities;
    const data = await jget("/api/entities");
    cache.entities = {};
    for (const e of data.entities || []) cache.entities[e.id] = e;
    return cache.entities;
  }
  async function getConversations() {
    if (cache.conversations) return cache.conversations;
    const data = await jget("/api/conversations");
    cache.conversations = data.conversations || [];
    return cache.conversations;
  }

  // panelContext: drives the layered editor + cast +/-/click semantics.
  //   conversation_id  set when the panel is rendered in a chat
  //   scenario_id      set when the user opens a scenario for context-edit
  // Editing target layer defaults to the most-specific available layer.
  const state = {
    activeTab: "chats",
    editing: null,
    scenarioContext: null,    // scenario id the user is editing in context
  };

  function panelContext() {
    const cid = document.querySelector(".chat-shell")?.dataset.conversationId || null;
    const sid = state.scenarioContext || null;
    return { conversation_id: cid, scenario_id: sid };
  }

  function defaultLayerForContext(ctx) {
    if (ctx.conversation_id) return "instance";
    if (ctx.scenario_id) return "scenario";
    return "template";
  }

  function effectiveQuery(ctx) {
    const params = new URLSearchParams();
    if (ctx.scenario_id) params.set("scenario", ctx.scenario_id);
    if (ctx.conversation_id) params.set("conversation", ctx.conversation_id);
    const s = params.toString();
    return s ? `?${s}` : "";
  }

  // Walk an entity's _origin map to a leaf path; returns the layer name
  // ("template" | "scenario" | "instance" | "unset" | null).
  function originAt(originMap, path) {
    let cur = originMap;
    for (const k of path) {
      if (cur == null || typeof cur !== "object") return null;
      cur = cur[k];
    }
    return typeof cur === "string" ? cur : null;
  }

  // ------------------------------------------------------------------------
  // Helpers
  // ------------------------------------------------------------------------

  async function jget(url) {
    const r = await fetch(url);
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    return r.json();
  }
  async function jsend(url, method, body) {
    const r = await fetch(url, {
      method,
      headers: { "Content-Type": "application/json" },
      body: body == null ? undefined : JSON.stringify(body),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      throw new Error(err.error || `${r.status} ${r.statusText}`);
    }
    return r.json();
  }

  function el(html) {
    const tpl = document.createElement("template");
    tpl.innerHTML = html.trim();
    return tpl.content.firstChild;
  }
  function escapeHtml(s) {
    return (s || "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
  }
  function emptyEntity(type) {
    const base = { id: "", type, name: "", description: "", tags: [], properties: {}, example_text: "", children: [] };
    if (type === "scenario") {
      base.characters = []; base.locations = []; base.objects = []; base.lore = [];
      base.starting_state = {}; base.opening_prompt = "";
    }
    if (type === "lore") {
      base.properties = { triggers: [], position: "after_char", depth: 0, always_active: false };
    }
    return base;
  }

  // ------------------------------------------------------------------------
  // Tab switching
  // ------------------------------------------------------------------------

  tabsEl.addEventListener("click", (e) => {
    const btn = e.target.closest(".tab");
    if (!btn) return;
    setActive(btn.dataset.tab);
  });

  function setActive(tab) {
    state.activeTab = tab;
    state.editing = null;
    for (const b of tabsEl.querySelectorAll(".tab")) {
      b.classList.toggle("active", b.dataset.tab === tab);
    }
    renderTab(tab);
  }

  function renderTab(tab) {
    if (state.editing) return renderEditor();
    if (tab === "chats") return renderChats();
    if (tab === "prompt") return renderPrompt();
    if (tab === "dev") return renderDev();
    if (ENTITY_TABS[tab]) return renderEntityList(tab);
    bodyEl.innerHTML = "";
  }

  function setError(msg) {
    bodyEl.innerHTML = `<p class="status-line pad" data-kind="error">${escapeHtml(msg)}</p>`;
  }

  // -- Chats ----------------------------------------------------------------

  async function renderChats() {
    let convs;
    try { convs = await getConversations(); }
    catch (e) { return setError(e.message); }
    bodyEl.innerHTML = "";
    bodyEl.appendChild(el(`
      <div class="lib-toolbar">
        <a class="ghost small" href="/">+ New from scenario…</a>
        <button class="ghost small" id="lib-refresh-convs" title="Refresh">↻</button>
      </div>`));
    bodyEl.querySelector("#lib-refresh-convs").addEventListener("click", () => {
      invalidateConversations(); renderChats();
    });
    if (!convs.length) {
      bodyEl.appendChild(el(`<p class="muted small pad">No conversations yet.</p>`));
      return;
    }
    const ul = document.createElement("ul");
    ul.className = "lib-list";
    const currentId = document.querySelector(".chat-shell")?.dataset.conversationId;
    for (const c of convs) {
      const li = el(`
        <li class="lib-row ${c.id === currentId ? "active" : ""}">
          <a class="lib-link" href="/chat/${c.id}">
            <strong>${escapeHtml(c.title)}</strong>
            <span class="muted small">${c.message_count} message${c.message_count === 1 ? "" : "s"}</span>
          </a>
          <button class="ghost xs" data-del="${c.id}">Del</button>
        </li>`);
      li.querySelector("[data-del]").addEventListener("click", async (e) => {
        e.stopPropagation(); e.preventDefault();
        if (!confirm("Delete this conversation and all its messages?")) return;
        try { await jsend(`/api/conversations/${c.id}`, "DELETE"); }
        catch (err) { return alert(err.message); }
        invalidateConversations();
        if (c.id === currentId) { window.location.href = "/"; return; }
        renderChats();
      });
      ul.appendChild(li);
    }
    bodyEl.appendChild(ul);
  }

  // -- Entity list ----------------------------------------------------------

  async function renderEntityList(tab) {
    const cfg = ENTITY_TABS[tab];
    const ctx = panelContext();
    let entityMap;
    try { entityMap = await getEntities(); }
    catch (e) { return setError(e.message); }
    const items = Object.values(entityMap)
      .filter((x) => x.type === cfg.type)
      .sort((a, b) => (a.name || a.id || "").localeCompare(b.name || b.id || ""));

    // Pull current cast from context so + / − reflect membership.
    // Scenario context wins when both are set: opening a scenario for
    // editing while in chat means the user wants +/- to target the
    // scenario template, not the conversation instance.
    let cast = new Set();
    // The +/- "in scene" axis applies to characters and to objects —
    // both are first-class scene members (the scenario tracks them in
    // `characters[]` / `objects[]`, the conversation instance pulls
    // each in as a deep-copied entity). Other tabs (locations, rooms,
    // outfits, lore) don't have a cast concept.
    if (cfg.type === "character" || cfg.type === "object") {
      const field = cfg.type === "character" ? "characters" : "objects";
      if (ctx.scenario_id) {
        const scen = entityMap[ctx.scenario_id];
        for (const id of (scen?.[field] || [])) cast.add(id);
      } else if (ctx.conversation_id) {
        try {
          const r = await jget(`/api/conversations/${ctx.conversation_id}/entities`);
          for (const id of Object.keys(r.entities || {})) {
            if ((r.entities[id] || {}).type === cfg.type) cast.add(id);
          }
        } catch {}
      }
    }

    bodyEl.innerHTML = "";

    if (ctx.scenario_id || ctx.conversation_id) {
      const what = ctx.scenario_id
        ? `scenario ${ctx.scenario_id}` + (ctx.conversation_id ? " (chat suspended)" : "")
        : "this chat";
      const banner = el(`
        <div class="ctx-banner">
          <span>Editing in context: <strong>${escapeHtml(what)}</strong></span>
          ${ctx.scenario_id
            ? `<button class="ghost xs" id="ctx-clear">Exit scenario context</button>` : ""}
        </div>`);
      bodyEl.appendChild(banner);
      banner.querySelector("#ctx-clear")?.addEventListener("click", () => {
        state.scenarioContext = null;
        renderEntityList(tab);
      });
    }

    const tools = el(`
      <div class="lib-toolbar">
        <input type="search" id="lib-filter" placeholder="Filter…" class="full" />
        <button class="primary small" id="lib-new">+ New</button>
        <button class="ghost small" id="lib-refresh" title="Refresh">↻</button>
      </div>`);
    bodyEl.appendChild(tools);
    tools.querySelector("#lib-new").addEventListener("click", () => openEditor(emptyEntity(cfg.type)));
    tools.querySelector("#lib-refresh").addEventListener("click", () => {
      invalidateEntities(); renderEntityList(tab);
    });
    if (!items.length) {
      bodyEl.appendChild(el(`<p class="muted small pad">No ${cfg.label.toLowerCase()} yet.</p>`));
      return;
    }

    const ul = document.createElement("ul");
    ul.className = "lib-list";
    for (const it of items) {
      const portrait = it.type === "character" && it.properties?.portrait
        ? `<img class="avatar small" src="/portraits/${encodeURIComponent(it.id)}" alt="" loading="lazy" />`
        : "";
      const tags = (it.tags || []).slice(0, 3).map((t) => `<span class="tag">${escapeHtml(t)}</span>`).join("");

      let castBtn = "";
      if ((cfg.type === "character" || cfg.type === "object")
          && (ctx.conversation_id || ctx.scenario_id)) {
        castBtn = cast.has(it.id)
          ? `<button class="ghost xs cast-btn cast-rm" data-rm="${it.id}" title="Remove from cast">−</button>`
          : `<button class="ghost xs cast-btn cast-add" data-add="${it.id}" title="Add to cast">+</button>`;
      }
      const ctxEditBtn = cfg.type === "scenario"
        ? `<button class="ghost xs" data-ctx="${it.id}" title="Open scenario context (lists +/− apply to this scenario)">Use as context</button>`
        : "";
      const li = el(`
        <li class="lib-row">
          ${portrait}
          <button class="lib-link link-btn" data-edit="${it.id}">
            <strong>${escapeHtml(it.name || it.id)}</strong>
            <span class="muted small ent-tags">${tags}</span>
          </button>
          ${castBtn}
          ${ctxEditBtn}
          ${cfg.canStart ? `<button class="primary xs" data-start="${it.id}">Start</button>` : ""}
        </li>`);
      li.querySelector("[data-edit]").addEventListener("click", () => openEditor(it));

      li.querySelector("[data-add]")?.addEventListener("click", async (ev) => {
        ev.stopPropagation();
        // Scenario context wins over conversation: if the user opted in
        // by opening a scenario (or "Use as context"), +/- targets the
        // scenario template, not the chat instance.
        try {
          if (ctx.scenario_id) {
            await jsend(`/api/scenarios/${ctx.scenario_id}/cast/${it.id}`, "POST", null);
          } else if (ctx.conversation_id) {
            await jsend(`/api/conversations/${ctx.conversation_id}/cast/${it.id}`, "POST", {});
          }
          invalidateEntities();
          renderEntityList(tab);
          window.dispatchEvent(new CustomEvent("gemmasim:cast-changed"));
        } catch (e) { alert("Add failed: " + e.message); }
      });
      li.querySelector("[data-rm]")?.addEventListener("click", async (ev) => {
        ev.stopPropagation();
        if (!confirm(`Remove ${it.name || it.id} from cast?`)) return;
        try {
          if (ctx.scenario_id) {
            await jsend(`/api/scenarios/${ctx.scenario_id}/cast/${it.id}`, "DELETE");
          } else if (ctx.conversation_id) {
            await jsend(`/api/conversations/${ctx.conversation_id}/cast/${it.id}`, "DELETE");
          }
          invalidateEntities();
          renderEntityList(tab);
          window.dispatchEvent(new CustomEvent("gemmasim:cast-changed"));
        } catch (e) { alert("Remove failed: " + e.message); }
      });
      li.querySelector("[data-ctx]")?.addEventListener("click", (ev) => {
        ev.stopPropagation();
        state.scenarioContext = it.id;
        renderEntityList(tab);
      });

      const startBtn = li.querySelector("[data-start]");
      if (startBtn) {
        startBtn.addEventListener("click", async () => {
          startBtn.disabled = true;
          startBtn.textContent = "…";
          try {
            const conv = await jsend("/api/conversations", "POST", { scenario_id: it.id });
            invalidateConversations();
            window.location.href = `/chat/${conv.id}`;
          } catch (e) {
            alert("Failed to start: " + e.message);
            startBtn.disabled = false; startBtn.textContent = "Start";
          }
        });
      }
      ul.appendChild(li);
    }
    bodyEl.appendChild(ul);
    bodyEl.querySelector("#lib-filter")?.addEventListener("input", (e) => {
      const q = e.target.value.toLowerCase();
      for (const row of ul.querySelectorAll(".lib-row")) {
        row.style.display = row.textContent.toLowerCase().includes(q) ? "" : "none";
      }
    });
  }

  // -- Editor ---------------------------------------------------------------

  async function openEditor(entity) {
    // Opening a scenario means the user wants to edit it: enter scenario
    // context automatically so any subsequent +/- in the Characters /
    // Locations / Outfits tabs targets this scenario instead of the
    // current conversation. Exit via the "Exit scenario context" banner.
    if (entity.type === "scenario" && entity.id) {
      state.scenarioContext = entity.id;
    }
    const ctx = panelContext();
    let effective = null;
    if (entity.id) {
      try {
        effective = await jget(`/api/effective/${entity.id}${effectiveQuery(ctx)}`);
      } catch {
        effective = null; // fall back to bare entity
      }
    }
    const baseValue = effective || entity;
    state.editing = {
      id: entity.id || null,
      type: entity.type,
      origin: (effective && effective._origin) || {},
      layers_present: (effective && effective._layers_present) || ["template"],
      ctx,
      layer: defaultLayerForContext(ctx),
      json: JSON.stringify(stripMeta(baseValue), null, 2),
    };
    renderEditor();
  }

  function stripMeta(o) {
    const c = { ...o };
    delete c._origin; delete c._layers_present; delete c._id;
    return c;
  }

  async function renderEditor() {
    const { id, type, json, origin, layers_present, ctx } = state.editing;
    let parsed;
    try { parsed = JSON.parse(json); } catch { parsed = {}; }
    bodyEl.innerHTML = "";

    // Determine which layers are saveable for this context.
    const layerOptions = ["template"];
    if (ctx.scenario_id) layerOptions.push("scenario");
    if (ctx.conversation_id) layerOptions.push("instance");
    if (!layerOptions.includes(state.editing.layer)) state.editing.layer = layerOptions[layerOptions.length - 1];

    const layerSelectorHtml = layerOptions.length > 1
      ? `<label class="muted small">Save to:
           <select id="lib-layer">
             ${layerOptions.map((l) =>
               `<option value="${l}" ${l === state.editing.layer ? "selected" : ""}>${l}</option>`
             ).join("")}
           </select>
         </label>` : "";
    const layersBadge = (layers_present || []).map((l) =>
      `<span class="layer-badge layer-${l}">${l}</span>`
    ).join("");

    const head = el(`
      <div class="lib-toolbar editor-head">
        <button class="ghost small" id="lib-back">‹ Back</button>
        <span class="muted small">${id ? "Edit" : "New"} · ${escapeHtml(type)}${id ? " · " + escapeHtml(id) : ""}</span>
        <span class="layers">${layersBadge}</span>
      </div>`);
    bodyEl.appendChild(head);

    // Status + actions row.
    const actions = el(`
      <div class="editor-actions">
        ${layerSelectorHtml}
        <button class="primary" id="lib-save">Save</button>
        <button class="ghost" id="lib-delete" ${id ? "" : "disabled"}>Delete</button>
        <span class="status-line small" id="lib-edit-status"></span>
      </div>`);
    actions.querySelector("#lib-layer")?.addEventListener("change", (e) => {
      state.editing.layer = e.target.value;
    });
    const setStatus = (msg, kind = "neutral") => {
      const s = actions.querySelector("#lib-edit-status");
      s.textContent = msg; s.dataset.kind = kind;
    };

    head.querySelector("#lib-back").addEventListener("click", () => {
      state.editing = null;
      renderTab(state.activeTab);
    });

    // -- Layered form path (PanelForms) -------------------------------------
    // If panel_forms.js has a builder for this type, use it: a single
    // scrolling form with real inputs + path-tagged data-path for full
    // origin tinting. Falls through to the legacy subtab editor below for
    // any type without a builder yet.
    if (window.PanelForms && window.PanelForms.builders[type]) {
      const formEl = document.createElement("form");
      formEl.className = "panel-form";
      formEl.setAttribute("autocomplete", "off");
      bodyEl.appendChild(formEl);
      bodyEl.appendChild(actions);

      // Builder needs the entity cache for outfit/location/room dropdowns.
      // In scenario or chat context, also include scenario-only custom
      // outfits + per-conversation instance edits so dropdowns reflect
      // the values the editor is bound to (otherwise a value like
      // "iris_thin_shirt" set by a scenario override would have no
      // matching <option> and the dropdown would render as "(none)").
      let entitiesCache = {};
      try { entitiesCache = { ...(await getEntities()) }; } catch {}
      try {
        if (ctx.conversation_id) {
          const r = await jget(`/api/conversations/${ctx.conversation_id}/entities`);
          for (const [eid, e] of Object.entries(r.entities || {})) {
            entitiesCache[eid] = e;
          }
        } else if (ctx.scenario_id) {
          const scen = await jget(`/api/entities/${ctx.scenario_id}`);
          for (const o of scen.custom_outfits || []) {
            if (o && o.id) entitiesCache[o.id] = o;
          }
        }
      } catch {}

      window.PanelForms.build(type, formEl, parsed, origin, ctx, entitiesCache, {
        openEntity: (e) => openEditor(e),
        refresh: () => { invalidateEntities(); openEditor(parsed); },
      });
      window.PanelForms.applyOriginTints(formEl, origin);

      // Save: collect form data-paths into parsed, then route by layer.
      actions.querySelector("#lib-save").addEventListener("click", async () => {
        try { window.PanelForms.collectForm(formEl, parsed); }
        catch (e) { setStatus(e.message, "error"); return; }
        const targetLayer = state.editing.layer;
        try {
          let saved;
          if (!id || targetLayer === "template") {
            saved = id
              ? await jsend(`/api/entities/${id}`, "PUT", parsed)
              : await jsend(`/api/entities`, "POST", parsed);
          } else {
            const params = new URLSearchParams({ layer: targetLayer });
            if (state.editing.ctx.scenario_id) params.set("scenario", state.editing.ctx.scenario_id);
            if (state.editing.ctx.conversation_id) params.set("conversation", state.editing.ctx.conversation_id);
            saved = await jsend(
              `/api/effective/${parsed.id || id}?${params.toString()}`,
              "PUT", parsed,
            );
          }
          invalidateEntities();
          state.editing.id = saved.id || saved._id || id;
          state.editing.origin = saved._origin || state.editing.origin;
          state.editing.layers_present = saved._layers_present || state.editing.layers_present;
          state.editing.json = JSON.stringify(stripMeta(saved), null, 2);
          setStatus(`Saved ${state.editing.id} to ${targetLayer}.`, "ok");
          renderEditor();
        } catch (e) {
          setStatus("Save failed: " + e.message, "error");
        }
      });

      actions.querySelector("#lib-delete").addEventListener("click", async () => {
        if (!id) return;
        if (!confirm(`Delete ${id}? Removes the template (instances unaffected).`)) return;
        try {
          await jsend(`/api/entities/${id}`, "DELETE");
          invalidateEntities();
          state.editing = null;
          renderTab(state.activeTab);
        } catch (e) {
          setStatus("Delete failed: " + e.message, "error");
        }
      });
      return;
    }
    // -- Legacy subtab editor (other types) ---------------------------------

    // Subtab renderers. Each section reads/writes a slice of `parsed`.
    let activeSub = "identity";
    const subBody = el(`<div class="lib-subbody"></div>`);

    function rebuildJson() {
      // Pull from any active sub-editor before saving.
      flushActiveSub();
    }
    let flushActiveSub = () => {};

    function tabBtn(key, label) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "tab-sub" + (key === activeSub ? " active" : "");
      b.dataset.key = key;
      b.textContent = label;
      b.addEventListener("click", () => {
        if (key === activeSub) return;
        flushActiveSub();
        activeSub = key;
        for (const x of subBar.querySelectorAll(".tab-sub")) {
          x.classList.toggle("active", x.dataset.key === key);
        }
        renderSub();
      });
      return b;
    }

    const subBar = el(`<div class="tabs-sub"></div>`);
    if (type === "character") {
      for (const [k, lbl] of [
        ["identity", "Identity"],
        ["body", "Body"],
        ["personality", "Personality"],
        ["outfits", "Outfits"],
        ["dialogue", "Dialogue"],
        ["relationships", "Relationships"],
        ["other", "Other"],
        ["raw", "Raw JSON"],
      ]) subBar.appendChild(tabBtn(k, lbl));
    } else {
      for (const [k, lbl] of [["identity", "Identity"], ["properties", "Properties"], ["raw", "Raw JSON"]])
        subBar.appendChild(tabBtn(k, lbl));
      activeSub = "identity";
    }
    bodyEl.appendChild(subBar);
    bodyEl.appendChild(subBody);
    bodyEl.appendChild(actions);
    // (back-button handler is wired earlier, before the layered branch)

    function tintFromOrigin(el, path) {
      const layer = originAt(state.editing.origin || {}, path);
      if (layer) el.classList.add(`origin-${layer}`);
    }

    function makeTextarea(value, rows = 4) {
      const t = document.createElement("textarea");
      t.className = "lib-editor";
      t.rows = rows;
      t.spellcheck = false;
      t.value = value || "";
      return t;
    }
    function makeInput(value) {
      const i = document.createElement("input");
      i.type = "text";
      i.value = value || "";
      return i;
    }
    function jsonField(label, value, rows = 8) {
      const wrap = document.createElement("label");
      wrap.className = "stack";
      wrap.innerHTML = `<span class="muted small">${label} (JSON)</span>`;
      const ta = makeTextarea(JSON.stringify(value ?? {}, null, 2), rows);
      ta.dataset.json = "1";
      wrap.appendChild(ta);
      return { wrap, ta };
    }

    function renderSub() {
      subBody.innerHTML = "";
      flushActiveSub = () => {};
      if (activeSub === "raw" || (type !== "character" && activeSub === "properties")) {
        const ta = makeTextarea(JSON.stringify(parsed, null, 2), 22);
        subBody.appendChild(ta);
        flushActiveSub = () => {
          try { parsed = JSON.parse(ta.value); }
          catch (e) { setStatus("Invalid JSON in Raw tab: " + e.message, "error"); throw e; }
        };
        return;
      }
      if (activeSub === "identity") {
        const id_ = makeInput(parsed.id || "");
        const name = makeInput(parsed.name || "");
        const desc = makeTextarea(parsed.description || "", 7);
        const tagsIn = makeInput((parsed.tags || []).join(", "));
        const exTxt = makeTextarea(parsed.example_text || "", 3);
        // Tint inputs by which layer their value came from.
        tintFromOrigin(name, ["name"]);
        tintFromOrigin(desc, ["description"]);
        tintFromOrigin(tagsIn, ["tags"]);
        tintFromOrigin(exTxt, ["example_text"]);
        const wrap = (label, child) => {
          const l = document.createElement("label");
          l.className = "stack";
          l.innerHTML = `<span class="muted small">${label}</span>`;
          l.appendChild(child);
          return l;
        };
        subBody.append(
          wrap("ID (slug)", id_),
          wrap("Name", name),
          wrap("Description", desc),
          wrap("Tags (comma-separated)", tagsIn),
          wrap("Example text", exTxt),
        );
        flushActiveSub = () => {
          parsed.id = id_.value.trim();
          parsed.name = name.value;
          parsed.description = desc.value;
          parsed.tags = tagsIn.value.split(",").map(s => s.trim()).filter(Boolean);
          parsed.example_text = exTxt.value;
        };
        return;
      }
      const props = parsed.properties = parsed.properties || {};
      if (activeSub === "body") {
        const f = jsonField("body_parts", props.body_parts || {}, 22);
        subBody.appendChild(f.wrap);
        flushActiveSub = () => {
          try { props.body_parts = JSON.parse(f.ta.value); }
          catch (e) { setStatus("Invalid JSON in body_parts: " + e.message, "error"); throw e; }
        };
        return;
      }
      if (activeSub === "personality") {
        const f = jsonField("personality", props.personality || {}, 12);
        subBody.appendChild(f.wrap);
        flushActiveSub = () => {
          try { props.personality = JSON.parse(f.ta.value); }
          catch (e) { setStatus("Invalid JSON in personality: " + e.message, "error"); throw e; }
        };
        return;
      }
      if (activeSub === "outfits") {
        const cur = makeInput(props.current_outfit || "");
        const list = makeInput((props.outfits || []).join(", "));
        const wrap = (label, child) => {
          const l = document.createElement("label");
          l.className = "stack";
          l.innerHTML = `<span class="muted small">${label}</span>`;
          l.appendChild(child);
          return l;
        };
        subBody.append(
          wrap("current_outfit (id)", cur),
          wrap("outfits (comma-separated ids)", list),
        );
        flushActiveSub = () => {
          props.current_outfit = cur.value.trim() || undefined;
          props.outfits = list.value.split(",").map(s => s.trim()).filter(Boolean);
        };
        return;
      }
      if (activeSub === "dialogue") {
        const first = makeTextarea(props.first_message || "", 6);
        const examples = makeTextarea((props.dialogue_examples || []).join("\n"), 8);
        const wrap = (label, child, hint) => {
          const l = document.createElement("label");
          l.className = "stack";
          l.innerHTML = `<span class="muted small">${label}</span>`;
          l.appendChild(child);
          if (hint) {
            const p = document.createElement("p");
            p.className = "muted small";
            p.textContent = hint;
            l.appendChild(p);
          }
          return l;
        };
        subBody.append(
          wrap("First message", first, "Macros: {{user}}, {{char}} expand at render time."),
          wrap("Dialogue examples", examples, "One per line. Used as primer chat turns."),
        );
        flushActiveSub = () => {
          props.first_message = first.value;
          props.dialogue_examples = examples.value.split("\n").map(s => s.trim()).filter(Boolean);
        };
        return;
      }
      if (activeSub === "relationships") {
        const f = jsonField("relationships", props.relationships || {}, 12);
        subBody.appendChild(f.wrap);
        flushActiveSub = () => {
          try { props.relationships = JSON.parse(f.ta.value); }
          catch (e) { setStatus("Invalid JSON in relationships: " + e.message, "error"); throw e; }
        };
        return;
      }
      if (activeSub === "other") {
        // Everything in `properties` that isn't covered by another tab.
        const KNOWN = new Set(["body_parts", "personality", "current_outfit", "outfits",
                               "first_message", "dialogue_examples", "relationships"]);
        const other = {};
        for (const [k, v] of Object.entries(props)) if (!KNOWN.has(k)) other[k] = v;
        const f = jsonField("Other properties", other, 18);
        subBody.appendChild(f.wrap);
        flushActiveSub = () => {
          let extra;
          try { extra = JSON.parse(f.ta.value); }
          catch (e) { setStatus("Invalid JSON in Other: " + e.message, "error"); throw e; }
          // Strip everything not-known from props, then write extras back.
          for (const k of Object.keys(props)) if (!KNOWN.has(k)) delete props[k];
          Object.assign(props, extra || {});
        };
        return;
      }
    }
    renderSub();

    actions.querySelector("#lib-save").addEventListener("click", async () => {
      try { rebuildJson(); } catch { return; }
      const targetLayer = state.editing.layer;
      try {
        let saved;
        if (!id || targetLayer === "template") {
          // New entity, or template save → goes through plain entities API
          // (this also handles "first-time create at template layer").
          saved = id
            ? await jsend(`/api/entities/${id}`, "PUT", parsed)
            : await jsend(`/api/entities`, "POST", parsed);
        } else {
          // Save at scenario or instance via the layered endpoint.
          const params = new URLSearchParams({ layer: targetLayer });
          if (state.editing.ctx.scenario_id) params.set("scenario", state.editing.ctx.scenario_id);
          if (state.editing.ctx.conversation_id) params.set("conversation", state.editing.ctx.conversation_id);
          saved = await jsend(
            `/api/effective/${parsed.id || id}?${params.toString()}`,
            "PUT",
            parsed,
          );
        }
        invalidateEntities();
        state.editing.id = saved.id || saved._id || id;
        state.editing.origin = saved._origin || state.editing.origin;
        state.editing.layers_present = saved._layers_present || state.editing.layers_present;
        state.editing.json = JSON.stringify(stripMeta(saved), null, 2);
        parsed = stripMeta(saved);
        setStatus(`Saved ${state.editing.id} to ${targetLayer}.`, "ok");
        renderEditor();
      } catch (e) {
        setStatus("Save failed: " + e.message, "error");
      }
    });

    actions.querySelector("#lib-delete").addEventListener("click", async () => {
      if (!id) return;
      if (!confirm(`Delete ${id}? This removes the template (instances are unaffected).`)) return;
      try {
        await jsend(`/api/entities/${id}`, "DELETE");
        invalidateEntities();
        state.editing = null;
        renderTab(state.activeTab);
      } catch (e) {
        setStatus("Delete failed: " + e.message, "error");
      }
    });
  }


  // -- Prompt viewer --------------------------------------------------------

  async function renderPrompt() {
    bodyEl.innerHTML = `<p class="muted small pad">Loading prompt…</p>`;
    const conv = document.querySelector(".chat-shell")?.dataset.conversationId;
    if (!conv) return setError("No active conversation.");
    const persona = document.getElementById("persona-select").value === "user"
      ? document.getElementById("responder-select").value
      : document.getElementById("persona-select").value;
    const speaker = persona === "narrator" || persona === "user" ? "" : persona;
    const url = `/api/conversations/${conv}/prompt?persona=${encodeURIComponent(persona)}${speaker ? `&speaker_id=${encodeURIComponent(speaker)}` : ""}`;
    let data;
    try { data = await jget(url); }
    catch (e) { return setError(e.message); }
    bodyEl.innerHTML = `<div class="lib-toolbar"><button class="ghost small" id="prompt-refresh">Refresh</button></div>`;
    for (const piece of data.pieces || []) {
      const det = el(`<details class="prompt-piece" open><summary>${escapeHtml(piece.label)}</summary><pre></pre></details>`);
      det.querySelector("pre").textContent = piece.content;
      bodyEl.appendChild(det);
    }
    bodyEl.querySelector("#prompt-refresh").addEventListener("click", renderPrompt);
  }

  // -- Dev panel ------------------------------------------------------------

  function renderDev() {
    const conv = document.querySelector(".chat-shell")?.dataset.conversationId;
    if (!conv) return setError("No active conversation.");
    bodyEl.innerHTML = "";
    const wrap = el(`
      <div class="dev-wrap">
        <h4>System instructions</h4>
        <p class="muted small">Injected into every AI call for this conversation.</p>
        <textarea id="dev-text" rows="14"></textarea>
        <div class="editor-actions">
          <button class="primary" id="dev-save">Save instructions</button>
        </div>
        <p class="status-line small" id="dev-status"></p>
      </div>`);
    bodyEl.appendChild(wrap);
    const conversation = window.GEMMASIM_INITIAL?.conversation;
    wrap.querySelector("#dev-text").value = conversation?.settings?.dev_panel_instructions || "";
    wrap.querySelector("#dev-save").addEventListener("click", async () => {
      const status = wrap.querySelector("#dev-status");
      try {
        await jsend(`/api/conversations/${conv}/settings`, "PUT", {
          dev_panel_instructions: wrap.querySelector("#dev-text").value,
        });
        status.textContent = "Saved.";
        status.dataset.kind = "ok";
      } catch (e) {
        status.textContent = "Failed: " + e.message;
        status.dataset.kind = "error";
      }
    });
  }

  // ------------------------------------------------------------------------
  // External hook: let chat.js (or anything else) ask the panel to open
  // an entity for editing. Used by the left-panel cast list "click to
  // edit" affordance.
  // ------------------------------------------------------------------------

  window.GemmaSimPanel = {
    openEntityById: async (id) => {
      try {
        const data = await jget(`/api/entities/${id}`);
        const targetTab = ENTITY_TABS_BY_TYPE[data.type] || "characters";
        // Make sure the right panel is open + on the relevant tab so the
        // editor renders in a visible spot.
        document.getElementById("right-panel")?.classList.add("open");
        setActive(targetTab);
        // setActive resets state.editing; openEditor will populate it.
        openEditor(data);
      } catch (e) { alert("Open failed: " + e.message); }
    },
    setScenarioContext: (sid) => {
      state.scenarioContext = sid || null;
      if (state.activeTab && ENTITY_TABS[state.activeTab]) renderTab(state.activeTab);
    },
  };

  const ENTITY_TABS_BY_TYPE = {
    character: "characters",
    location: "locations",
    room: "rooms",
    object: "objects",
    outfit: "outfits",
    scenario: "scenarios",
    lore: "lore",
  };

  // ------------------------------------------------------------------------
  // First render
  // ------------------------------------------------------------------------

  setActive("chats");
})();
