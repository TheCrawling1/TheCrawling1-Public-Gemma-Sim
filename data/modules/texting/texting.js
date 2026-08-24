/**
 * Texting module — frontend.
 *
 * Self-contained drop-in. Mounts a "Text" button in the composer-right
 * region (owned by this module, so the engine auto-removes it when the
 * module is toggled off — see Modules.mount({owner}) / _fireActivationChange),
 * picks a target character from the in-cast list, stamps
 * metadata.modules.texting = {to: <char_id>} on the next user message
 * via Modules.onCompose, and decorates messages with the configured
 * bubble style via Modules.onMessage.
 *
 * Loaded only when the module's .js file exists on disk
 * (data/modules/texting/texting.js). Activation is checked at init
 * via Modules.isActive("texting") — inactive = the module is inert,
 * no UI mounted, no compose / message hooks register.
 */
(function () {
  const MODULE_ID = "texting";

  if (!window.Modules) {
    console.warn("texting: window.Modules unavailable; module dormant");
    return;
  }
  if (!Modules.isActive(MODULE_ID)) {
    return;  // module not active for this branch
  }

  // Internal state. `currentTarget` is the character id the next user
  // message gets stamped against. Reset to null after each send so
  // texting is single-turn unless the user re-picks before the next
  // message.
  let currentTarget = null;

  // ---- Compose hook: stamp metadata on the outgoing user message ----
  Modules.onCompose((pending) => {
    if (pending.persona !== "user") return;
    if (!currentTarget) return;
    pending.metadata = pending.metadata || {};
    pending.metadata.modules = pending.metadata.modules || {};
    pending.metadata.modules[MODULE_ID] = { to: currentTarget };
    // Single-turn: clear the target after stamping so the next
    // message is a regular in-person turn unless the user clicks
    // Text again.
    currentTarget = null;
    _refreshButtonLabel();
  });

  // URL override for the render style — lets the user A/B
  // sms_bubble / tinted / inline_badge without re-staging. Visit
  // any chat page with ?texting_style=tinted (or ...=inline_badge,
  // or ...=sms_bubble) to override the saved setting for THIS
  // page load only. Leave the param off to use the staged setting.
  const _urlStyleOverride = (() => {
    try {
      const v = new URLSearchParams(location.search).get("texting_style");
      if (v === "sms_bubble" || v === "tinted" || v === "inline_badge") return v;
    } catch (_e) { /* no-op */ }
    return null;
  })();

  // ---- Message hook: apply bubble CSS to texting turns ----
  Modules.onMessage((messageEl, message) => {
    const settings = Modules.settings(MODULE_ID) || {};
    const style = _urlStyleOverride || settings.render_style || "sms_bubble";
    // Primary path: the message carries its own persisted texting
    // marker. The user's outgoing text has {to: <char>}; a reply that
    // was answering a text is stamped {to: <char>, reply: true} by the
    // backend message annotator — so both re-render correctly on reload
    // straight from branch-persisted node metadata, no derivation.
    const own = (message?.metadata?.modules || {})[MODULE_ID];
    if (own) {
      messageEl.classList.add("texting-message");
      messageEl.classList.add(`texting-style-${style}`);
      if (own.reply) {
        messageEl.classList.add("texting-reply");
      } else {
        messageEl.dataset.textingTo = own.to || "";
      }
      return;
    }
    // Fallback for replies persisted before the marker existed (or any
    // the annotator didn't reach): walk the FULL ancestry to the nearest
    // user message and inherit its marker. Full-ancestry (not just the
    // immediate parent) so multi-response chains and intervening
    // narrator / auto-state nodes still resolve.
    const msgs = (typeof state !== "undefined" && state) ? state.conversation?.messages : null;
    if (!msgs) return;
    let cur = msgs[message?.parent_id];
    let guard = 0;
    while (cur && guard++ < 200) {
      if (cur.persona === "user") {
        const pm = (cur.metadata?.modules || {})[MODULE_ID];
        const toMe = pm && (message?.speaker_id ? pm.to === message.speaker_id : true);
        if (toMe) {
          messageEl.classList.add("texting-message", "texting-reply");
          messageEl.classList.add(`texting-style-${style}`);
        }
        return;  // nearest user message decides; stop either way
      }
      cur = msgs[cur.parent_id];
    }
  });

  // ---- UI: Text button + target picker ----
  const wrap = document.createElement("span");
  wrap.className = "texting-composer-wrap";

  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "ghost";
  btn.dataset.textingBtn = "1";
  btn.title = "Compose a text to a specific character. Their reply " +
              "strips environmental prompt blocks (Surroundings, etc).";
  wrap.appendChild(btn);

  function _refreshButtonLabel() {
    if (!currentTarget) {
      btn.textContent = "Text";
      btn.classList.remove("texting-armed");
      return;
    }
    const ent = state?.entities?.[currentTarget];
    const name = (ent && ent.name) || currentTarget;
    btn.textContent = `→ ${name}`;
    btn.classList.add("texting-armed");
  }

  function _eligibleCharacters() {
    // In-cast NPCs from the effective_cast set, excluding user. The
    // engine's state.effectiveCastChars carries the live branch cast.
    const out = [];
    const cast = state?.effectiveCastChars;
    if (!cast) return out;
    for (const cid of cast) {
      if (cid === "user") continue;
      const e = state.entities && state.entities[cid];
      if (!e || e.type !== "character") continue;
      out.push({ id: cid, name: e.name || cid });
    }
    out.sort((a, b) => a.name.localeCompare(b.name));
    return out;
  }

  function _showPicker() {
    // Close any existing picker first.
    const old = document.querySelector(".texting-picker");
    if (old) { old.remove(); return; }

    const choices = _eligibleCharacters();
    if (!choices.length) {
      // Nothing to text. Surface gently — don't crash.
      btn.classList.add("texting-empty");
      setTimeout(() => btn.classList.remove("texting-empty"), 1200);
      return;
    }

    const menu = document.createElement("div");
    menu.className = "texting-picker";
    for (const c of choices) {
      const opt = document.createElement("button");
      opt.type = "button";
      opt.className = "ghost texting-picker-row";
      opt.textContent = c.name;
      opt.addEventListener("click", (e) => {
        e.preventDefault();
        currentTarget = c.id;
        _refreshButtonLabel();
        menu.remove();
      });
      menu.appendChild(opt);
    }
    // Cancel row clears the armed target.
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "ghost texting-picker-cancel";
    cancel.textContent = "Cancel";
    cancel.addEventListener("click", (e) => {
      e.preventDefault();
      currentTarget = null;
      _refreshButtonLabel();
      menu.remove();
    });
    menu.appendChild(cancel);

    wrap.appendChild(menu);
    // Close on outside-click (next tick so this click doesn't catch).
    setTimeout(() => {
      const off = (ev) => {
        if (!menu.contains(ev.target) && ev.target !== btn) {
          menu.remove();
          document.removeEventListener("click", off);
        }
      };
      document.addEventListener("click", off);
    }, 0);
  }

  btn.addEventListener("click", (e) => {
    e.preventDefault();
    if (currentTarget) {
      // Click an armed button to clear.
      currentTarget = null;
      _refreshButtonLabel();
      return;
    }
    _showPicker();
  });

  _refreshButtonLabel();
  // Owned mount: the engine tracks this element against the module id and
  // auto-unmounts it when texting is toggled off mid-conversation (fixes the
  // button lingering after deactivation). "composer-right" resolves to the
  // engine's [data-region] anchor; the legacy "composer_right" SLOTS name
  // would also work via region()'s fallback.
  Modules.mount("composer-right", wrap, { owner: MODULE_ID });
})();
