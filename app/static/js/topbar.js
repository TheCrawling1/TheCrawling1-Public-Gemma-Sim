// Topbar API bar: live status dot + model swap dropdown. Loaded on every page.

(function () {
  const bar = document.getElementById("api-bar");
  if (!bar) return;
  const dot = bar.querySelector(".dot");
  const select = document.getElementById("topbar-model");
  const testBtn = document.getElementById("topbar-test");

  let currentHost = null;
  let currentModel = null;

  function setStatus(kind, label) {
    bar.dataset.status = kind;
    bar.title = label;
  }

  async function loadSettings() {
    try {
      const r = await fetch("/api/settings");
      if (!r.ok) return null;
      const data = await r.json();
      currentHost = (data.ollama || {}).host || null;
      currentModel = (data.ollama || {}).model || null;
      return data;
    } catch (e) {
      return null;
    }
  }

  function populate(models) {
    select.innerHTML = "";
    if (!models || models.length === 0) {
      const opt = document.createElement("option");
      opt.value = currentModel || "";
      opt.textContent = currentModel ? `${currentModel} (offline)` : "(no model)";
      select.appendChild(opt);
      select.disabled = true;
      return;
    }
    select.disabled = false;
    for (const name of models) {
      const opt = document.createElement("option");
      opt.value = name;
      opt.textContent = name;
      if (name === currentModel) opt.selected = true;
      select.appendChild(opt);
    }
    if (currentModel && !models.includes(currentModel)) {
      const opt = document.createElement("option");
      opt.value = currentModel;
      opt.textContent = `${currentModel} (not pulled)`;
      opt.selected = true;
      select.prepend(opt);
    }
  }

  // Cache connection-test results in sessionStorage so flipping between
  // Dashboard / Chat / Studio doesn't re-probe Ollama every navigation.
  const CACHE_KEY = "gemmasim.ollama.test";
  const CACHE_TTL_MS = 30_000;

  function cacheGet() {
    try {
      const raw = sessionStorage.getItem(CACHE_KEY);
      if (!raw) return null;
      const obj = JSON.parse(raw);
      if (!obj || Date.now() - obj.at > CACHE_TTL_MS) return null;
      if (obj.host !== currentHost || obj.model !== currentModel) return null;
      return obj.data;
    } catch { return null; }
  }
  function cacheSet(data) {
    try {
      sessionStorage.setItem(CACHE_KEY, JSON.stringify({
        at: Date.now(), host: currentHost, model: currentModel, data,
      }));
    } catch {}
  }
  function cacheClear() {
    try { sessionStorage.removeItem(CACHE_KEY); } catch {}
  }

  function applyResult(data) {
    if (data.ok) {
      populate(data.models || []);
      if (data.model_present === false) {
        setStatus("warn", `Connected but model '${currentModel}' isn't pulled.`);
      } else {
        setStatus("ok", `Connected to ${currentHost || "Ollama"} · ${(data.models || []).length} model(s)`);
      }
    } else {
      populate([]);
      setStatus("error", data.error || "Ollama unreachable");
    }
  }

  async function refresh({ force = false } = {}) {
    if (!force) {
      const cached = cacheGet();
      if (cached) { applyResult(cached); return; }
    }
    setStatus("unknown", "Checking Ollama…");
    try {
      const r = await fetch("/api/ollama/test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ host: currentHost, model: currentModel }),
      });
      const data = await r.json();
      cacheSet(data);
      applyResult(data);
    } catch (e) {
      populate([]);
      setStatus("error", e.message);
    }
  }

  select.addEventListener("change", async () => {
    const newModel = select.value;
    if (!newModel || newModel === currentModel) return;
    setStatus("unknown", "Switching model…");
    try {
      const r = await fetch("/api/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ollama: { model: newModel } }),
      });
      if (!r.ok) throw new Error((await r.json()).error || r.statusText);
      currentModel = newModel;
      cacheClear();
      setStatus("ok", `Active model set to ${newModel}`);
    } catch (e) {
      setStatus("error", "Failed to swap model: " + e.message);
    }
  });

  testBtn?.addEventListener("click", () => refresh({ force: true }));

  const warmupBtn = document.getElementById("topbar-warmup");
  async function warmup() {
    if (!currentModel) return;
    setStatus("unknown", `Loading ${currentModel}…`);
    warmupBtn.disabled = true;
    const t0 = performance.now();
    try {
      const r = await fetch("/api/ollama/warmup", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model: currentModel }),
      });
      const data = await r.json();
      const elapsed = ((performance.now() - t0) / 1000).toFixed(1);
      if (data.ok) {
        cacheClear();
        setStatus("ok", `${currentModel} warmed in ${elapsed}s`);
      } else {
        setStatus("error", data.error || "Warmup failed");
      }
    } catch (e) {
      setStatus("error", "Warmup failed: " + e.message);
    } finally {
      warmupBtn.disabled = false;
    }
  }
  warmupBtn?.addEventListener("click", warmup);

  loadSettings().then(() => refresh());
})();
