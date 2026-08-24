// Settings: edit Ollama host/model/options + global defaults + per-model
// sampling profiles, with a connection test.

const els = {
  host: document.getElementById("ollama-host"),
  model: document.getElementById("ollama-model"),
  refresh: document.getElementById("refresh-models"),
  temp: document.getElementById("ollama-temperature"),
  topP: document.getElementById("ollama-top-p"),
  test: document.getElementById("test-connection"),
  save: document.getElementById("save-settings"),
  status: document.getElementById("settings-status"),
  ctxLimit: document.getElementById("default-context-limit"),
  narrator: document.getElementById("default-narrator-mode"),
  turn: document.getElementById("default-turn-mode"),
  locational: document.getElementById("default-locational"),
  // Profile fields
  profModel: document.getElementById("profile-model"),
  profLoad: document.getElementById("profile-load"),
  profClear: document.getElementById("profile-clear"),
  profTemp: document.getElementById("profile-temp"),
  profTopP: document.getElementById("profile-top-p"),
  profTopK: document.getElementById("profile-top-k"),
  profRepeat: document.getElementById("profile-repeat"),
  profPresence: document.getElementById("profile-presence"),
  profFrequency: document.getElementById("profile-frequency"),
  profNumCtx: document.getElementById("profile-num-ctx"),
  profNumPredict: document.getElementById("profile-num-predict"),
  profSave: document.getElementById("profile-save"),
  profStatus: document.getElementById("profile-status"),
};

let state = { ollama: {}, defaults: {}, model_profiles: {} };

function setStatus(node, msg, kind = "neutral") {
  node.textContent = msg;
  node.dataset.kind = kind;
}

async function jfetch(url, opts = {}) {
  const r = await fetch(url, {
    ...opts,
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    throw new Error(err.error || r.statusText);
  }
  return r.json();
}

function fillFromState() {
  const o = state.ollama || {};
  els.host.value = o.host || "";
  const opts = o.default_options || {};
  els.temp.value = opts.temperature ?? "";
  els.topP.value = opts.top_p ?? "";
  populateModelOptions(els.model, o.model);
  populateModelOptions(els.profModel, o.model);

  const d = state.defaults || {};
  els.ctxLimit.value = d.context_limit_tokens ?? 8000;
  els.narrator.value = d.narrator_mode || "auto";
  els.turn.value = d.turn_mode || "manual";
  els.locational.checked = d.locational_memory !== false;

  // Network access — IP allowlist + trust_proxy + read-only bind display.
  const net = state.network || {};
  const bindEl = document.getElementById("network-current-bind");
  if (bindEl) bindEl.textContent = `${state.host || "?"}:${state.port || "?"}`;
  const ipsEl = document.getElementById("allowed-ips");
  if (ipsEl) ipsEl.value = (net.allowed_ips || []).join("\n");
  const proxyEl = document.getElementById("trust-proxy");
  if (proxyEl) proxyEl.checked = !!net.trust_proxy;

  loadProfile(els.profModel.value);
}

async function saveNetwork() {
  const status = document.getElementById("network-status");
  const ips = document.getElementById("allowed-ips").value
    .split("\n").map((s) => s.trim()).filter(Boolean);
  const trustProxy = document.getElementById("trust-proxy").checked;
  setStatus(status, "Saving…", "neutral");
  try {
    const merged = await jfetch("/api/settings", {
      method: "PUT",
      body: JSON.stringify({
        network: { allowed_ips: ips, trust_proxy: trustProxy },
      }),
    });
    state = { ...state, ...merged };
    const note = ips.length
      ? `Saved · ${ips.length} entr${ips.length === 1 ? "y" : "ies"} on the allowlist.`
      : "Saved · open mode (no IP restriction).";
    setStatus(status, note, "ok");
  } catch (e) {
    setStatus(status, "Failed: " + e.message, "error");
  }
}
document.getElementById("network-save")?.addEventListener("click", saveNetwork);

function populateModelOptions(target, currentModel, fetched = null) {
  const list = fetched || [];
  const had = target.value;
  target.innerHTML = "";
  if (list.length === 0) {
    const opt = document.createElement("option");
    opt.value = currentModel || "";
    opt.textContent = currentModel ? `${currentModel} (not verified)` : "(no models — test connection first)";
    target.appendChild(opt);
    return;
  }
  for (const name of list) {
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = name;
    if (name === (had || currentModel)) opt.selected = true;
    target.appendChild(opt);
  }
  if (currentModel && !list.includes(currentModel)) {
    const opt = document.createElement("option");
    opt.value = currentModel;
    opt.textContent = `${currentModel} (not pulled)`;
    if (!had) opt.selected = true;
    target.prepend(opt);
  }
}

async function loadSettings() {
  state = await jfetch("/api/settings");
  fillFromState();
  testConnection({ silent: true });
}

async function testConnection({ silent = false } = {}) {
  setStatus(els.status, "Testing connection…", "neutral");
  const result = await jfetch("/api/ollama/test", {
    method: "POST",
    body: JSON.stringify({
      host: els.host.value || null,
      model: els.model.value || null,
    }),
  }).catch((e) => ({ ok: false, error: e.message }));
  if (result.ok) {
    populateModelOptions(els.model, els.model.value, result.models || []);
    populateModelOptions(els.profModel, els.profModel.value, result.models || []);
    const count = (result.models || []).length;
    const present = result.model_present;
    let msg = `Connected. ${count} model${count === 1 ? "" : "s"} available.`;
    if (els.model.value && present === false) {
      msg += ` ⚠ ${els.model.value} is not pulled.`;
    }
    setStatus(els.status, msg, present === false ? "warn" : "ok");
    loadProfile(els.profModel.value);
  } else {
    if (!silent) setStatus(els.status, `Failed: ${result.error || "unknown error"}`, "error");
    else setStatus(els.status, "Ollama is unreachable. Edit the host and click Test connection.", "warn");
  }
}

async function saveSettings() {
  setStatus(els.status, "Saving…", "neutral");
  const payload = {
    ollama: {
      host: els.host.value.trim(),
      model: els.model.value || null,
      default_options: {
        temperature: parseFloat(els.temp.value) || 0,
        top_p: parseFloat(els.topP.value) || 0,
      },
    },
    defaults: {
      context_limit_tokens: parseInt(els.ctxLimit.value, 10) || 8000,
      narrator_mode: els.narrator.value,
      turn_mode: els.turn.value,
      locational_memory: els.locational.checked,
    },
  };
  try {
    state = await jfetch("/api/settings", {
      method: "PUT",
      body: JSON.stringify(payload),
    });
    setStatus(els.status, "Saved.", "ok");
    fillFromState();
  } catch (e) {
    setStatus(els.status, `Save failed: ${e.message}`, "error");
  }
}

// -- Profiles ----------------------------------------------------------------

function loadProfile(model) {
  if (!model) return;
  const prof = (state.model_profiles || {})[model] || {};
  els.profTemp.value = prof.temperature ?? "";
  els.profTopP.value = prof.top_p ?? "";
  els.profTopK.value = prof.top_k ?? "";
  els.profRepeat.value = prof.repeat_penalty ?? "";
  els.profPresence.value = prof.presence_penalty ?? "";
  els.profFrequency.value = prof.frequency_penalty ?? "";
  els.profNumCtx.value = prof.num_ctx ?? "";
  els.profNumPredict.value = prof.num_predict ?? "";
  setStatus(els.profStatus, prof && Object.keys(prof).length ? `Loaded profile for ${model}.` : `No profile for ${model} yet.`, "neutral");
}

async function saveProfile() {
  const model = els.profModel.value;
  if (!model) return setStatus(els.profStatus, "Pick a model first.", "warn");
  const body = {};
  for (const [key, el] of [
    ["temperature", els.profTemp],
    ["top_p", els.profTopP],
    ["top_k", els.profTopK],
    ["repeat_penalty", els.profRepeat],
    ["presence_penalty", els.profPresence],
    ["frequency_penalty", els.profFrequency],
    ["num_ctx", els.profNumCtx],
    ["num_predict", els.profNumPredict],
  ]) {
    const v = el.value.trim();
    if (v === "") continue;
    body[key] = Number(v);
  }
  try {
    const result = await jfetch(`/api/profiles/${encodeURIComponent(model)}`, {
      method: "PUT",
      body: JSON.stringify(body),
    });
    state.model_profiles = result.model_profiles || {};
    setStatus(els.profStatus, Object.keys(body).length ? `Saved profile for ${model}.` : `Cleared profile for ${model}.`, "ok");
  } catch (e) {
    setStatus(els.profStatus, "Save failed: " + e.message, "error");
  }
}

function clearProfile() {
  for (const el of [els.profTemp, els.profTopP, els.profTopK, els.profRepeat, els.profPresence, els.profFrequency, els.profNumCtx, els.profNumPredict]) {
    el.value = "";
  }
}

// -- Wiring ------------------------------------------------------------------

els.test.addEventListener("click", () => testConnection());
els.refresh.addEventListener("click", () => testConnection());
els.save.addEventListener("click", saveSettings);
els.profModel.addEventListener("change", () => loadProfile(els.profModel.value));
els.profLoad.addEventListener("click", () => loadProfile(els.profModel.value));
els.profClear.addEventListener("click", clearProfile);
els.profSave.addEventListener("click", saveProfile);

// Layered editor color cues — saved to localStorage and applied as CSS vars
// next time the page loads. Used by the right-panel layered editor.
const LAYER_COLOR_DEFAULTS = {
  template: "#dcdcd2",
  scenario: "#f8d300",
  instance: "#bce7cf",
  unset:    "#9b9b96",
};
function loadLayerColors() {
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem("layer_colors") || "{}"); } catch {}
  const merged = { ...LAYER_COLOR_DEFAULTS, ...saved };
  document.getElementById("color-template").value = merged.template;
  document.getElementById("color-scenario").value = merged.scenario;
  document.getElementById("color-instance").value = merged.instance;
  document.getElementById("color-unset").value = merged.unset;
}
function saveLayerColors() {
  const cur = {
    template: document.getElementById("color-template").value,
    scenario: document.getElementById("color-scenario").value,
    instance: document.getElementById("color-instance").value,
    unset:    document.getElementById("color-unset").value,
  };
  localStorage.setItem("layer_colors", JSON.stringify(cur));
  setStatus(document.getElementById("layer-colors-status"), "Saved. Reload chat to see the change.", "ok");
}
document.getElementById("layer-colors-save")?.addEventListener("click", saveLayerColors);
document.getElementById("layer-colors-reset")?.addEventListener("click", () => {
  localStorage.removeItem("layer_colors");
  loadLayerColors();
  setStatus(document.getElementById("layer-colors-status"), "Reset.", "ok");
});
loadLayerColors();

loadSettings();
