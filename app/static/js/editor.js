// Shared studio editor plumbing.
//
// Per-entity page templates render a form whose inputs use data-field="x.y.z"
// dotted paths into the entity object. On save we read every [data-field],
// poke its value into a clone of the entity, and PUT/POST the result.

const ENTITY_DATA_TAG = "entity-data";
const ENTITY_TYPE_TAG = "entity-type";
const IS_NEW_TAG = "entity-is-new";

export function readBootstrap() {
  const data = JSON.parse(document.getElementById(ENTITY_DATA_TAG).textContent || "null");
  const type = document.getElementById(ENTITY_TYPE_TAG).textContent.trim();
  const isNew = document.getElementById(IS_NEW_TAG).textContent.trim() === "true";
  return { entity: data, type, isNew };
}

export function setStatus(msg, isError) {
  const el = document.getElementById("editor-status");
  if (!el) return;
  el.textContent = msg || "";
  el.style.color = isError ? "var(--danger)" : "";
}

// Read/write nested fields by dotted path. Empty string is treated as unset
// so the entity doesn't end up with a flood of "" leaves.
export function getPath(obj, path) {
  return path.split(".").reduce((cur, k) => (cur == null ? undefined : cur[k]), obj);
}

export function setPath(obj, path, value) {
  const keys = path.split(".");
  let cur = obj;
  for (let i = 0; i < keys.length - 1; i++) {
    const k = keys[i];
    if (cur[k] == null || typeof cur[k] !== "object") cur[k] = {};
    cur = cur[k];
  }
  cur[keys[keys.length - 1]] = value;
}

// Collect every [data-field] under `root` into a fresh object built on top of
// `base`. Inputs of type checkbox return booleans; comma-list textareas/inputs
// (data-list="comma") return string arrays; others return their string value.
export function collectForm(root, base) {
  const out = JSON.parse(JSON.stringify(base || {}));
  root.querySelectorAll("[data-field]").forEach((el) => {
    const path = el.dataset.field;
    let v;
    if (el.type === "checkbox") {
      v = !!el.checked;
    } else if (el.dataset.list === "comma") {
      v = (el.value || "")
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean);
    } else if (el.dataset.list === "lines") {
      v = (el.value || "")
        .split("\n")
        .map((s) => s.trim())
        .filter(Boolean);
    } else if (el.type === "number" || el.dataset.coerce === "number") {
      v = el.value === "" ? null : Number(el.value);
    } else {
      v = el.value;
    }
    setPath(out, path, v);
  });
  return out;
}

// Populate every [data-field] from `entity`.
export function fillForm(root, entity) {
  root.querySelectorAll("[data-field]").forEach((el) => {
    const v = getPath(entity, el.dataset.field);
    if (el.type === "checkbox") {
      el.checked = !!v;
    } else if (el.dataset.list === "comma") {
      el.value = Array.isArray(v) ? v.join(", ") : "";
    } else if (el.dataset.list === "lines") {
      el.value = Array.isArray(v) ? v.join("\n") : "";
    } else {
      el.value = v == null ? "" : String(v);
    }
  });
}

export async function saveEntity(entity, isNew) {
  const url = isNew ? "/api/entities" : `/api/entities/${entity.id}`;
  const method = isNew ? "POST" : "PUT";
  const r = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(entity),
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    throw new Error(err.error || r.statusText);
  }
  return r.json();
}

export async function deleteEntity(entityId) {
  const r = await fetch(`/api/entities/${entityId}`, { method: "DELETE" });
  if (!r.ok) throw new Error("delete failed");
  return r.json();
}

// Tabs: links with data-tab="X" toggle .panel[data-panel="X"] visibility.
export function wireTabs(root) {
  const tabs = root.querySelectorAll("[data-tab]");
  const panels = root.querySelectorAll("[data-panel]");
  tabs.forEach((tab) => {
    tab.addEventListener("click", (ev) => {
      ev.preventDefault();
      const target = tab.dataset.tab;
      tabs.forEach((t) => t.classList.toggle("active", t.dataset.tab === target));
      panels.forEach((p) => (p.style.display = p.dataset.panel === target ? "" : "none"));
    });
  });
}

// JSON tab: textarea bound to a getter/setter on the live entity.
export function wireJSONTab(textarea, getEntity, setEntity) {
  const refresh = () => {
    textarea.value = JSON.stringify(getEntity(), null, 2);
  };
  textarea.addEventListener("focus", refresh);
  document.querySelectorAll('[data-tab="json"]').forEach((t) => t.addEventListener("click", refresh));
  textarea.addEventListener("blur", () => {
    if (!textarea.value.trim()) return;
    try {
      setEntity(JSON.parse(textarea.value));
      setStatus("JSON parsed.");
    } catch (e) {
      setStatus("Invalid JSON: " + e.message, true);
    }
  });
  refresh();
}
