// Dashboard: scenario start, conversation delete, live Ollama status pill.
//
// One-click start. The server-side random roll (random_character_pool /
// random_item_pool) and the boolean start_toggles are still available
// — POSTs without those fields just take the defaults (random partner,
// no item picked, all toggles off). Anything the user wants to change
// after the conversation starts is handled in the chat side panel +
// the right-side library cast list.

document.querySelectorAll("[data-start-scenario]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const scenarioId = btn.dataset.startScenario;
    btn.disabled = true;
    btn.textContent = "Starting…";
    try {
      const r = await fetch("/api/conversations", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ scenario_id: scenarioId }),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        alert("Failed to start: " + (err.error || r.statusText));
        return;
      }
      const conv = await r.json();
      window.location.href = `/chat/${conv.id}`;
    } finally {
      btn.disabled = false;
      btn.textContent = "Start →";
    }
  });
});

document.querySelectorAll("[data-delete-conversation]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const cid = btn.dataset.deleteConversation;
    if (!confirm("Delete this conversation and all its messages?")) return;
    const r = await fetch(`/api/conversations/${cid}`, { method: "DELETE" });
    if (r.ok) window.location.reload();
  });
});
