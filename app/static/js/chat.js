// Chat client. Designed to minimize round-trips: state mutates locally on
// every action and the conversation is only re-fetched when something we
// can't reconstruct happens.

const shell = document.querySelector(".chat-shell");
const conversationId = shell.dataset.conversationId;
const messagesEl = document.getElementById("messages");
const composer = document.getElementById("composer");
const composerInput = document.getElementById("composer-input");
const composerAs = document.getElementById("composer-as-label");
const personaSelect = document.getElementById("persona-select");
const responderSelect = document.getElementById("responder-select");
const composerPersonaSelect = document.getElementById("composer-persona-select");
const composerResponderSelect = document.getElementById("composer-responder-select");
const composerSpeakSwap = document.getElementById("composer-speak-swap");
const generateOnlyBtn = document.getElementById("generate-only");
const sendBtn = composer.querySelector("button[type=submit]");
const tokensEl = document.getElementById("composer-tokens");
const quickEditsEl = document.getElementById("quick-edits");
const toggleLeft = document.getElementById("toggle-left");
const showLeft = document.getElementById("show-left");
const showRight = document.getElementById("show-right");
const closeRight = document.getElementById("close-right");
const locationalToggle = document.getElementById("locational-toggle");
const thinkingToggle = document.getElementById("thinking-toggle");
const mentionToggle = document.getElementById("mention-toggle");
const autoApplyEditsToggle = document.getElementById("auto-apply-edits-toggle");
const narratorMode = document.getElementById("narrator-mode");
const confirmDialog = document.getElementById("confirm-dialog");
const confirmText = document.getElementById("confirm-text");
const editsDialog = document.getElementById("edits-dialog");
const editsText = document.getElementById("edits-text");

// Layout metrics. Publishes runtime sizes the CSS reads via custom
// properties so segments stay in sync without duplicating numbers.
//   --composer-h : measured composer height (so .messages can reserve
//                  space and avoid hiding the last message under it).
//   --app-top    : visualViewport.offsetTop — the fixed chat layout's top,
//                  pushed down by a top browser toolbar.
//   --app-height : visualViewport.height — the fixed chat layout's height,
//                  the ACTUALLY-visible rect (shrinks for a bottom toolbar or
//                  the on-screen keyboard). Replaces the old 100dvh/--kb-inset
//                  approach that left a dead strip on Firefox Android.
const docStyle = document.documentElement.style;

// Tracks whether the user is currently scrolled to the bottom of the
// message list. We only auto-scroll on composer growth when they are —
// otherwise we'd yank them out of scrollback.
const isAtBottom = () => {
  if (!messagesEl) return true;
  return messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight < 24;
};

// Remembered pin state: true while the user is parked at the bottom. Updated
// only on real scroll events, so it reflects intent from BEFORE any
// programmatic content append (a fresh isAtBottom() read taken right after an
// append would be falsely false). scrollToBottomSoon() consults this so edits,
// branch switches, and state updates never yank a user who has scrolled up
// into history. User-initiated sends/generations re-pin it to true.
let autoScrollPinned = true;
messagesEl?.addEventListener(
  "scroll",
  () => { autoScrollPinned = isAtBottom(); },
  { passive: true }
);

// Top chrome (nav bar + in-chat header) is an OVERLAY over the message list (see
// CSS) — showing/hiding it never resizes the messages, so nothing shifts. This
// just toggles .nav-collapsed to slide it up/down. Mobile only.
//
// The toggle is a short, BINARY throw driven by pointer DIRECTION, not the
// list's scrollTop — so it still works when the list is pinned at its very top
// or bottom (where 'scroll' events stop firing but touchmove/wheel do not):
// swipe up / wheel down (reading toward newer) hides the top; swipe down / wheel
// up (toward history) shows it. A small directional move flips it fully.
(() => {
  if (!messagesEl) return;
  const isMobile = () => window.matchMedia("(max-width: 800px)").matches;
  const body = document.body;

  // State model:
  //  - scrollCollapsed: the swipe toggle's own hide/show (user gesture).
  //  - locks: reasons the chrome is FORCE-hidden and the swipe toggle is
  //    disabled — the on-screen keyboard, a full-screen module overlay (pf1e
  //    grid), etc. A lock marked {revealable:true} gets a pull-down tab so the
  //    user can still drop the chrome down; `peek` is that manual override.
  // The chrome is hidden when locked && !peek, else when scrollCollapsed.
  let scrollCollapsed = false;
  let peek = false;
  const locks = new Map(); // reason -> { revealable }

  // Pull-down reveal tab (core-provided so ANY revealable lock gets it for free).
  // Manual toggle: tap to drop the chrome down, tap again to re-hide. It's
  // anchored to the chat-head (top:100% in CSS), so it rides the chrome's
  // transform for free — a pull-DOWN handle at the top when the chrome is hidden,
  // a pull-UP handle just below the header when it's shown.
  const chatHead = document.querySelector(".chat-main > .chat-head");
  const tab = document.createElement("button");
  tab.type = "button";
  tab.id = "chrome-reveal-tab";
  tab.setAttribute("aria-label", "Show / hide the top bar");
  tab.hidden = true;
  tab.addEventListener("click", () => { peek = !peek; apply(); });
  (chatHead || body).appendChild(tab);

  function apply() {
    const locked = locks.size > 0;
    const revealable = [...locks.values()].some((l) => l.revealable);
    const hidden = locked ? !peek : scrollCollapsed;
    body.classList.toggle("nav-collapsed", hidden);
    body.classList.toggle("nav-locked", locked);
    // Tab appears only while a revealable lock is actually hiding the chrome.
    tab.hidden = !(locked && revealable);
    tab.classList.toggle("peeked", peek);
  }

  const setScrollCollapsed = (v) => {
    if (locks.size > 0 || v === scrollCollapsed) return;
    scrollCollapsed = v;
    apply();
  };

  // Swipe toggle: a short, BINARY throw driven by pointer DIRECTION, not the
  // list's scrollTop — so it works even when the list is pinned at its top or
  // bottom (where 'scroll' events stop firing but touchmove/wheel do not). lastY
  // only advances when a throw commits, so sub-threshold jitter accumulates
  // instead of flip-flopping, and reversing direction flips it the other way.
  const THROW = 14;
  let lastY = 0;
  messagesEl.addEventListener("touchstart", (e) => {
    lastY = e.touches[0].clientY;
  }, { passive: true });
  messagesEl.addEventListener("touchmove", (e) => {
    if (locks.size > 0 || !isMobile()) return;
    const dy = e.touches[0].clientY - lastY;
    if (dy <= -THROW) { setScrollCollapsed(true); lastY = e.touches[0].clientY; }        // finger up -> hide
    else if (dy >= THROW) { setScrollCollapsed(false); lastY = e.touches[0].clientY; }   // finger down -> show
  }, { passive: true });
  messagesEl.addEventListener("wheel", (e) => {
    if (locks.size > 0 || !isMobile()) return;
    if (e.deltaY > 0) setScrollCollapsed(true);                 // scroll down -> hide
    else if (e.deltaY < 0) setScrollCollapsed(false);           // scroll up -> show
  }, { passive: true });

  // Public API. Any module can force the top chrome out of the way for a
  // full-screen surface (e.g. pf1e's tactical grid) and get the reveal tab:
  //   window.ChatChrome.lock("pf1e-grid", { revealable: true });
  //   window.ChatChrome.unlock("pf1e-grid");
  window.ChatChrome = {
    lock(reason, opts) {
      if (!reason) return;
      locks.set(reason, { revealable: !!(opts && opts.revealable) });
      peek = false;
      apply();
    },
    unlock(reason) {
      locks.delete(reason);
      if (locks.size === 0) peek = false;
      apply();
    },
    togglePeek() { peek = !peek; apply(); },
    isLocked: () => locks.size > 0,
  };

  // Typing (on-screen keyboard) is just another lock — NOT revealable, since
  // dismissing the keyboard restores the chrome automatically. Detect via
  // visualViewport height (a blur may never fire on mobile — dismissing the
  // keyboard often keeps the field focused), gated on an editable element being
  // focused + a substantial (>120px) delta so a browser toolbar / rounding
  // can't trigger it.
  if (window.visualViewport) {
    const vv = window.visualViewport;
    let kbOpen = false;
    const updateKb = () => {
      const ae = document.activeElement;
      const editable = !!ae && (ae.tagName === "TEXTAREA" || ae.tagName === "INPUT" || ae.isContentEditable);
      const delta = Math.max(0, window.innerHeight - vv.height - vv.offsetTop);
      const open = isMobile() && editable && delta > 120;
      if (open !== kbOpen) {
        kbOpen = open;
        if (open) window.ChatChrome.lock("keyboard");
        else window.ChatChrome.unlock("keyboard");
      }
    };
    vv.addEventListener("resize", updateKb);
    vv.addEventListener("scroll", updateKb);
    document.addEventListener("focusin", updateKb);
    document.addEventListener("focusout", updateKb);
  }

  apply();
})();

if (typeof ResizeObserver !== "undefined") {
  let lastH = composer.offsetHeight;
  const ro = new ResizeObserver(() => {
    const h = composer.offsetHeight;
    docStyle.setProperty("--composer-h", `${h}px`);
    // When the composer grows (e.g. narrator-edit composer opens), the
    // messages row shrinks. If the user was at the bottom, keep them
    // there so the last message doesn't slip off-screen.
    if (h > lastH && autoScrollPinned && messagesEl) {
      messagesEl.scrollTop = messagesEl.scrollHeight;
    }
    lastH = h;
  });
  ro.observe(composer);
} else {
  // No ResizeObserver: set once on load and after window resizes.
  const sync = () => docStyle.setProperty("--composer-h", `${composer.offsetHeight}px`);
  window.addEventListener("resize", sync);
  sync();
}

if (window.visualViewport) {
  const vv = window.visualViewport;
  // Pin the fixed chat layout (body.chat on mobile) to the ACTUALLY-visible
  // viewport rectangle rather than 100dvh/inset:0.
  //
  // Why not just 100dvh: this page is position:fixed with touch-action:none,
  // so the document never scrolls — only the message list does. Firefox
  // Android only collapses its dynamic toolbar on a document scroll, which
  // never happens here, so its layout viewport (and 100dvh) stayed the
  // toolbar-hidden TALL height. The composer, anchored to the bottom of that
  // too-tall layout, sat below the visible area — a reserved dead strip the
  // toolbar's height that no amount of scrolling could shrink.
  //
  // visualViewport reports the real visible rect: .offsetTop (a TOP toolbar
  // pushes it down) and .height (a BOTTOM toolbar or the on-screen keyboard
  // shrink it). Writing those to --app-top/--app-height sizes the fixed
  // layout to exactly what's on screen, live, and reconciles the instant the
  // toolbar or keyboard changes — no document scroll required. It also lifts
  // the composer above the keyboard for free (height shrinks when it opens),
  // which is why the separate --kb-inset padding is gone.
  const syncApp = () => {
    docStyle.setProperty("--app-top", `${vv.offsetTop}px`);
    docStyle.setProperty("--app-height", `${vv.height}px`);
  };
  vv.addEventListener("resize", syncApp);
  vv.addEventListener("scroll", syncApp);
  // Focus transitions can precede the viewport resize when the keyboard
  // opens/closes; re-sync so the layout tracks it without a visible lag.
  document.addEventListener("focusin", syncApp);
  document.addEventListener("focusout", syncApp);
  syncApp();
}

let state = {
  conversation: window.GEMMASIM_INITIAL.conversation,
  entities: window.GEMMASIM_INITIAL.entities,
  // Branch-scoped cast for the active path. Server-computed from the
  // path's cast_add / cast_remove edits (see app/effective.py); kept
  // here as a Set so renderCastList + the dropdowns can filter the
  // shared instance pool down to what's actually in this branch.
  effectiveCastChars: new Set(
    (window.GEMMASIM_INITIAL.effective_cast?.characters) || []
  ),
  effectiveCastObjects: new Set(
    (window.GEMMASIM_INITIAL.effective_cast?.objects) || []
  ),
  // GM-view toggle: reveal cast members flagged hidden_from_player (monsters the
  // scenario stashes in rooms you haven't reached). A player preference, sticky
  // across conversations; default off (hidden stays hidden). See castHiddenFromPlayer.
  showHiddenCast: (() => { try { return localStorage.getItem("gemmasim_show_hidden_cast") === "1"; } catch (_) { return false; } })(),
  // Birth-time cast of the conversation (the instance scenario's
  // characters[]/objects[]). Used as the baseline that the client
  // replays cast_add / cast_remove against to derive the cast for
  // any path — without a server roundtrip, so branch switches
  // update dropdowns synchronously instead of after the debounced
  // /active-leaf POST returns.
  scenarioBaselineCast: {
    characters: new Set(
      (window.GEMMASIM_INITIAL.scenario_baseline_cast?.characters) || []
    ),
    objects: new Set(
      (window.GEMMASIM_INITIAL.scenario_baseline_cast?.objects) || []
    ),
  },
  // Setup-root id for the active branch. The Reply-as sticky pick
  // is keyed off this — switching to a sibling branch with a
  // different setup root reads its own remembered responder.
  activeSetupRootId: (window.GEMMASIM_INITIAL.active_setup_root_id || ""),
  // Path-replayed user persona at the active leaf. Source of truth
  // for the side-panel persona/role inputs at page load — staging
  // emits `[set user.X = ...]` edits and those land here via
  // effective_user_persona on the server side. Refreshable from
  // /user-persona-effective (or any endpoint that returns the
  // path-replayed result) when the active branch switches.
  effectiveUserPersona: window.GEMMASIM_INITIAL.effective_user_persona || {},
  generating: false,
  abortController: null,
  // Transient client-side UI state per message id. Drives below-body
  // attachments like the inline narrator-edit composer (which only
  // exists while it's open — never persisted to msg.metadata).
  openComposers: new Set(),
};

// ---------------------------------------------------------------------------
// window.Prefabs — public API for self-contained drop-in prefab JS files.
//
// A prefab kind's staging-panel renderer lives under
// data/prefabs/<id>/prefab.js and is loaded by chat.html AFTER chat.js,
// so this namespace exists when prefab init code runs. A drop-in
// registers its renderer once at load:
//
//   Prefabs.registerKind("my_kind", function (pid, prefab, target, ctx) {
//     // ctx.makeCollapsible(title), ctx.picks, ctx.refreshHooks,
//     // ctx.pickedCharacterIds(), ctx.prefabs — build a section and
//     // store pick state under ctx.picks.prefabs[pid].
//   });
//
// The staging panel dispatches each prefab to its registered renderer;
// the four engine builtins keep their inline renderers as a fallback
// when no kind is registered here, so existing scenarios are untouched.
// ---------------------------------------------------------------------------
window.Prefabs = window.Prefabs || (function () {
  const _kinds = {};
  return {
    registerKind(kind, renderFn) {
      if (typeof kind === "string" && kind && typeof renderFn === "function") {
        _kinds[kind] = renderFn;
      }
    },
    getKind(kind) { return _kinds[kind] || null; },
    kinds() { return Object.keys(_kinds); },
  };
})();

// ---------------------------------------------------------------------------
// window.Modules — public API for self-contained drop-in module JS files.
//
// Module JS lives under data/modules/<id>/<id>.js and is loaded by
// chat.html AFTER chat.js, so this namespace exists when module init
// code runs. Modules call into the public API ONLY — never into
// state internals — so engine refactors stay free as long as the
// surface below holds.
//
// Surface:
//   Modules.mount(region, element, opts) drop content into a named region
//                                        (opts.owner tags it for unmount)
//   Modules.region(name)                 resolve a region name to its DOM node
//   Modules.unmount(owner)               remove every element mounted by owner
//   Modules.onMessage(callback)          fire after each new message renders
//   Modules.onCompose(callback)          fire on Send; mutate pendingMessage
//   Modules.onNavigate(callback)         fire on branch/leaf navigation
//   Modules.onBeforeGenerate(callback)   awaited hook before a reply generates
//   Modules.claimMain(owner, handlers)   take over the main chat surface
//   Modules.releaseMain(owner)           hand the main surface back
//   Modules.registerAttachment(spec)     per-message above/below-body blocks
//   Modules.host                         engine command surface (reload, toast,
//                                        navigateTo, getState, conversationId)
//   Modules.isActive(moduleId)           is the module on for this branch?
//   Modules.settings(moduleId)           module settings on the active branch
//   Modules.staticUrl(moduleId, file)    URL builder for module assets
//   Modules.api(moduleId, path, opts)    POST/GET against module routes
// ---------------------------------------------------------------------------
window.Modules = (function () {
  const SLOTS = Object.freeze({
    chat_above:      "slot-chat-above",
    chat_toolbar:    "autoplay-toggle-wrap",  // legacy slot id (Autoplay uses)
    composer_left:   "slot-composer-left",
    composer_right:  "slot-composer-right",
  });

  // Per-event listener arrays. Modules push into these via the
  // onMessage / onCompose / onActivationChange subscribers below.
  const _onMessageCallbacks = [];
  const _onComposeCallbacks = [];
  const _onActivationChangeCallbacks = [];
  const _onNavigateCallbacks = [];
  const _onBeforeGenerateCallbacks = [];

  // Ownership-tracked mounts. Modules that pass {owner} into mount()
  // get their elements auto-removed on unmount(owner) and on
  // deactivation (see _fireActivationChange).
  const _mounted = [];  // {owner, region, element}
  // The single full-surface takeover, if any: {owner, element, handlers}.
  let _mainSurface = null;

  // Page-load snapshot of the active-module-ids set, used as a
  // fallback when state hasn't been populated yet. The live check
  // in isActive() prefers the conversation's active setup root
  // metadata so toggling a module via the left-panel updates
  // isActive() without a page reload.
  const initialActiveModuleIds = new Set(
    (window.GEMMASIM_INITIAL?.active_module_ids) || []
  );

  // Resolve a region name to its DOM anchor. New engine anchors use
  // data-region="..."; legacy callers pass a SLOTS name (chat_above,
  // chat_toolbar, composer_left, composer_right) or a raw element id.
  function region(name) {
    const byData = document.querySelector('[data-region="' + name + '"]');
    if (byData) return byData;
    if (SLOTS[name]) return document.getElementById(SLOTS[name]);
    return document.getElementById(name);
  }

  function mount(regionName, element, opts) {
    const target = region(regionName);
    if (!target) {
      console.warn(`Modules.mount: unknown region "${regionName}"`);
      return false;
    }
    target.appendChild(element);
    if (opts && opts.owner) {
      _mounted.push({ owner: opts.owner, region: regionName, element });
    }
    return true;
  }

  // Remove every DOM element mounted by `owner`. Safe if none.
  function unmount(owner) {
    for (let i = _mounted.length - 1; i >= 0; i--) {
      if (_mounted[i].owner === owner) {
        try { _mounted[i].element.remove(); } catch (_e) { /* detached */ }
        _mounted.splice(i, 1);
      }
    }
  }

  function onMessage(callback) {
    if (typeof callback === "function") _onMessageCallbacks.push(callback);
  }

  function _fireMessage(messageEl, message) {
    for (const cb of _onMessageCallbacks) {
      try { cb(messageEl, message); }
      catch (e) { console.warn("module onMessage error:", e); }
    }
  }

  function onCompose(callback) {
    if (typeof callback === "function") _onComposeCallbacks.push(callback);
  }

  function _fireCompose(pendingMessage) {
    for (const cb of _onComposeCallbacks) {
      try { cb(pendingMessage); }
      catch (e) { console.warn("module onCompose error:", e); }
    }
    return pendingMessage;
  }

  function isActive(moduleId) {
    // Live check: prefer the active setup root's metadata.modules
    // list so a left-panel toggle of the module updates this
    // without a page reload. Falls back to the page-load snapshot
    // before state is initialized.
    try {
      const rootId = state && state.activeSetupRootId;
      if (rootId) {
        const root = state.conversation?.messages?.[rootId];
        const live = root?.metadata?.modules;
        if (Array.isArray(live)) return live.includes(moduleId);
      }
    } catch (_e) { /* fall through */ }
    return initialActiveModuleIds.has(moduleId);
  }

  function onActivationChange(callback) {
    if (typeof callback === "function") _onActivationChangeCallbacks.push(callback);
  }

  function _fireActivationChange() {
    for (const cb of _onActivationChangeCallbacks) {
      try { cb(); }
      catch (e) { console.warn("module onActivationChange error:", e); }
    }
    // Auto-clean surfaces owned by modules that are now off so a
    // toggled-off module's mounts / main takeover don't linger.
    try {
      const owners = new Set(_mounted.map((m) => m.owner));
      if (_mainSurface) owners.add(_mainSurface.owner);
      for (const owner of owners) {
        if (!isActive(owner)) {
          try { unmount(owner); } catch (_e) { /* defensive */ }
          try { releaseMain(owner); } catch (_e) { /* defensive */ }
        }
      }
    } catch (e) { console.warn("module surface auto-unmount error:", e); }
  }

  function onNavigate(cb) {
    if (typeof cb === "function") _onNavigateCallbacks.push(cb);
  }

  function _fireNavigate(leafId) {
    for (const cb of _onNavigateCallbacks) {
      try { cb(leafId); }
      catch (e) { console.warn("module onNavigate error:", e); }
    }
  }

  function onBeforeGenerate(cb) {
    if (typeof cb === "function") _onBeforeGenerateCallbacks.push(cb);
  }

  async function _fireBeforeGenerate(ctx) {
    for (const cb of _onBeforeGenerateCallbacks) {
      try { await cb(ctx); }
      catch (e) { console.warn("module onBeforeGenerate error:", e); }
    }
  }

  // Full-surface takeover: hand a module a fresh <div> inside the main
  // chat column and flag the shell so CSS can hide the default chat UI.
  // Only one owner holds the surface at a time; claiming while another
  // owner holds it releases the previous first.
  function claimMain(owner, handlers) {
    if (_mainSurface && _mainSurface.owner !== owner) {
      releaseMain(_mainSurface.owner);
    }
    const mainHost = document.querySelector("section.chat-main")
      || (document.getElementById("messages")
          && document.getElementById("messages").parentElement);
    if (!mainHost) {
      console.warn("Modules.claimMain: no main surface found");
      return null;
    }
    const el = document.createElement("div");
    el.className = "module-main-surface";
    el.setAttribute("data-module", owner);
    mainHost.appendChild(el);
    const shellEl = document.querySelector(".chat-shell");
    if (shellEl) {
      shellEl.classList.add("module-surface-claimed");
      shellEl.setAttribute("data-surface-owner", owner);
    }
    _mainSurface = { owner, element: el, handlers: handlers || null };
    try {
      if (handlers && handlers.onEnter) handlers.onEnter(el);
    } catch (e) { console.warn("module claimMain onEnter error:", e); }
    return el;
  }

  function releaseMain(owner) {
    if (!_mainSurface || _mainSurface.owner !== owner) return;
    const { element, handlers } = _mainSurface;
    try { if (element) element.remove(); } catch (_e) { /* detached */ }
    const shellEl = document.querySelector(".chat-shell");
    if (shellEl) {
      shellEl.classList.remove("module-surface-claimed");
      shellEl.removeAttribute("data-surface-owner");
    }
    _mainSurface = null;
    try {
      if (handlers && handlers.onExit) handlers.onExit();
    } catch (e) { console.warn("module releaseMain onExit error:", e); }
  }

  // Small engine command surface for modules: reload the conversation,
  // toast a neutral message, navigate to a leaf, or read a state snapshot.
  const host = {
    get conversationId() { return conversationId; },
    reload() { return window.reloadConversation && window.reloadConversation(); },
    toast(msg) { return (window.flashInfo || function () {})(msg); },
    navigateTo(leafId) { return setActiveLeaf(leafId); },
    getState() {
      return {
        conversation: state.conversation,
        entities: state.entities,
        activeLeaf: state.conversation && state.conversation.active_path_leaf,
      };
    },
  };

  function settings(moduleId) {
    // Branch-scoped: live setup root's module_settings.<id>.
    const root = state.conversation?.messages?.[state.activeSetupRootId];
    const all = (root && root.metadata && root.metadata.module_settings) || {};
    return all[moduleId] || {};
  }

  function staticUrl(moduleId, filename) {
    return `/modules/${encodeURIComponent(moduleId)}/static/${filename}`;
  }

  async function api(moduleId, path, opts = {}) {
    const p = path.startsWith("/") ? path : `/${path}`;
    return jfetch(`/modules/${encodeURIComponent(moduleId)}${p}`, opts);
  }

  return {
    mount, region, unmount, onMessage, _fireMessage,
    onCompose, _fireCompose,
    onNavigate, _fireNavigate,
    onBeforeGenerate, _fireBeforeGenerate,
    onActivationChange, _fireActivationChange,
    claimMain, releaseMain,
    registerAttachment: (spec) => registerAttachment(spec),
    host,
    isActive, settings, staticUrl, api,
    setResponder: moduleSetResponder,
    SLOTS,
  };
})();

// Modules API: programmatically steer who replies next (Reply-as). Used by AAM
// routing — a world-query action (e.g. a scry) is answered by the narrator, not
// the NPC the player was talking to, then handed back. Guarded: only sets a value
// that is actually a valid responder option, and never persists it as the user's
// sticky pick (setResponderProgrammatically). Returns whether it took effect.
function moduleSetResponder(value) {
  if (!value || !responderSelect) return false;
  if (![...responderSelect.options].some((o) => o.value === value)) return false;
  setResponderProgrammatically(value);
  return true;
}

// Walk the path leaf→root and replay cast_add / cast_remove edits
// onto the conversation's birth-time baseline cast. Mirrors
// effective.effective_cast_at server-side so branch switches don't
// have to wait on the debounced /active-leaf POST to know what the
// cast looks like on the new path. Also honors scene_staging_picks
// on the active setup root as an exclusive whitelist — old staging
// branches don't have cast_remove edits, only the picks metadata.
function computeEffectiveCastForLeaf(leafId) {
  const msgs = (state.conversation && state.conversation.messages) || {};
  const chain = [];
  let cur = msgs[leafId];
  const seen = new Set();
  while (cur && !seen.has(cur.id)) {
    seen.add(cur.id);
    chain.push(cur);
    if (!cur.parent_id) break;
    cur = msgs[cur.parent_id];
  }
  // Most-recent setup root on the path. chain is leaf→root; pick the
  // first message in that order whose metadata.setup is a dict.
  let setupRoot = null;
  for (const m of chain) {
    const meta = m.metadata || {};
    if (meta.setup && typeof meta.setup === "object") {
      setupRoot = m;
      break;
    }
  }
  chain.reverse();
  const chars = new Set(state.scenarioBaselineCast.characters);
  // Objects have no present-baseline (scenario objects[] is just the
  // staging pool): an object is in the scene only via a path-replayed
  // cast_add. Mirrors effective.effective_cast_at server-side.
  const objs = new Set();
  chars.add("user");
  // Apply scene_staging_picks before edits so a later cast_add can
  // still extend the cast (e.g., narrator-add on a staging branch).
  const picks = (setupRoot && setupRoot.metadata
    && setupRoot.metadata.scene_staging_picks
    && setupRoot.metadata.scene_staging_picks.characters) || null;
  if (Array.isArray(picks)) {
    const allow = new Set(picks);
    allow.add("user");
    for (const c of [...chars]) if (!allow.has(c)) chars.delete(c);
  }
  for (const m of chain) {
    const log = (m.metadata && m.metadata.applied_edits) || [];
    for (const e of log) {
      if (!e || e.ok === false) continue;
      if (e.kind === "cast_add" && e.id) {
        const etype = (state.entities[e.id] || {}).type;
        if (etype === "object") objs.add(e.id);
        else chars.add(e.id);
      } else if (e.kind === "cast_remove" && e.id && e.id !== "user") {
        chars.delete(e.id);
        objs.delete(e.id);
      }
    }
  }
  return { characters: chars, objects: objs };
}

function applyEffectiveCastForLeaf(leafId) {
  const cast = computeEffectiveCastForLeaf(leafId);
  state.effectiveCastChars = cast.characters;
  state.effectiveCastObjects = cast.objects;
}

// A cast member the player shouldn't see in the roster yet — either GM-hidden
// (properties.hidden_from_player) or a monster lying in wait (properties.stealthed,
// which the engine clears when your Perception beats its Stealth). Purely a
// client-side roster/dropdown filter: the entity is still in the cast, still in its
// room, still seeds onto the grid, and the narrator still sees it when you're
// present. The "Show hidden cast" toggle (state.showHiddenCast) reveals them all.
function castHiddenFromPlayer(e) {
  if (state.showHiddenCast) return false;
  const props = (e && e.properties) || {};
  return !!(props.hidden_from_player || props.stealthed);
}

// Walk the path leaf→root and return the most recent character
// speaker. Mirrors effective.default_responder_for_path so a branch
// switch can pick the right Reply-as default without waiting on the
// /active-leaf POST.
function clientDefaultResponderForLeaf(leafId) {
  const msgs = (state.conversation && state.conversation.messages) || {};
  let cur = msgs[leafId];
  const seen = new Set();
  while (cur && !seen.has(cur.id)) {
    seen.add(cur.id);
    const sid = cur.speaker_id;
    const persona = cur.persona;
    if (sid && sid !== "user" && persona !== "narrator" && persona !== "user" && persona !== "system") {
      return sid;
    }
    if (!cur.parent_id) break;
    cur = msgs[cur.parent_id];
  }
  return "";
}

// "Speak as" the user persona: a user message is labeled and avatared
// from settings.user_persona, falling back to "You" + the generic user
// placeholder. When card_id points at a user-tagged character template,
// we use that character's portrait route too — so picking Alex in the
// persona dialog makes messages render as Alex.
function userPersonaName() {
  return (state.conversation.settings?.user_persona?.name || "").trim() || "You";
}
function userPersonaPortraitId() {
  return state.conversation.settings?.user_persona?.card_id || null;
}

// ---------------------------------------------------------------------------
// Formatting + helpers
// ---------------------------------------------------------------------------

function escapeHtml(s) {
  return (s || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}
function sanitizeImageUrl(raw) {
  const u = (raw || "").trim();
  if (!u) return null;
  const lower = u.toLowerCase();
  if (lower.startsWith("javascript:") || lower.startsWith("vbscript:")) return null;
  if (/^https?:\/\//i.test(u)) return u;
  if (/^data:image\//i.test(u)) return u;
  if (u.startsWith("/")) return u;
  // Project-relative paths to known asset roots.
  if (/^(data|static|instances)\//.test(u)) return "/file/" + u;
  return null;
}

function applyMacros(text, charName) {
  if (!text) return "";
  const persona = state.conversation.settings?.user_persona || {};
  const userName = (persona.name || "").trim() || "User";
  const c = (charName || "").trim();
  return String(text)
    .replace(/\{\{user\.([a-z0-9_]+)\}\}/gi, (_, k) => String(persona[k.toLowerCase()] ?? ""))
    .replace(/\{\{(?:user|user_name)\}\}/gi, userName)
    .replace(/\{\{(?:char|char_name)\}\}/gi, c);
}

function formatBody(text, charName) {
  const expanded = applyMacros(text, charName);
  const placeholders = [];
  let work = expanded.replace(/!\[([^\]\n]*)\]\(([^)\n]+)\)/g, (m, alt, url) => {
    const safe = sanitizeImageUrl(url);
    if (!safe) return m;
    const tag = `<img alt="${escapeHtml(alt)}" src="${escapeHtml(safe)}" class="msg-img" loading="lazy" />`;
    placeholders.push(tag);
    return ` IMG${placeholders.length - 1} `;
  });
  let html = escapeHtml(work);
  html = html.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/\*([^*\n]+)\*/g, "<em>$1</em>");
  html = html.replace(/"([^"\n]+)"/g, '<span class="quoted">&ldquo;$1&rdquo;</span>');
  html = html.replace(/ IMG(\d+) /g, (_, i) => placeholders[Number(i)] || "");
  return html.replace(/\n/g, "<br>");
}
function entityName(id) {
  const e = state.entities[id];
  return (e && e.name) || id;
}
function speakerLabel(msg) {
  if (msg.persona === "narrator") return "Narrator";
  if (msg.persona === "user") return userPersonaName();
  if (msg.speaker_id) return entityName(msg.speaker_id);
  return msg.persona;
}
function portraitUrl(speakerId) {
  // Always return a candidate URL — the /portraits route 404s for missing
  // files, and the <img> elements use onerror to swap in the placeholder.
  // This avoids requiring every character JSON to declare
  // `properties.portrait`; dropping a portrait.png in the character's
  // data dir is enough.
  if (!speakerId) return null;
  const e = state.entities[speakerId];
  if (!e) return null;
  return `/portraits/${encodeURIComponent(speakerId)}`;
}
function fmtTime(ts) {
  if (!ts) return "";
  try { return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); }
  catch { return ""; }
}

// rAF-throttled scroll helper avoids forcing layout on every token.
let scrollPending = false;
function scrollToBottomSoon() {
  // Respect a user who has scrolled up: only auto-scroll when they were parked
  // at the bottom. User-initiated sends/generations re-pin (autoScrollPinned)
  // before calling, so replies still follow; passive re-renders (edits, branch
  // switches, state updates) no longer yank the viewport.
  if (!autoScrollPinned) return;
  if (scrollPending) return;
  scrollPending = true;
  requestAnimationFrame(() => {
    scrollPending = false;
    messagesEl.scrollTop = messagesEl.scrollHeight;
    autoScrollPinned = true;
  });
}

// ---------------------------------------------------------------------------
// Tree helpers
// ---------------------------------------------------------------------------

function pathToLeaf(leafId) {
  const msgs = state.conversation.messages;
  const path = [];
  let cur = msgs[leafId];
  const seen = new Set();
  while (cur && !seen.has(cur.id)) {
    path.push(cur);
    seen.add(cur.id);
    if (!cur.parent_id) break;
    cur = msgs[cur.parent_id];
  }
  return path.reverse();
}
function siblingsOf(msg) {
  // Oldest-first by creation time so "Branch 1" is always the oldest and the
  // newest is Branch N. created_at is second-resolution; tie-break on id for a
  // stable order. (Object.values follows insertion order, which drifts.)
  return Object.values(state.conversation.messages)
    .filter((m) => m.parent_id === msg.parent_id)
    .sort(
      (a, b) =>
        (a.created_at || 0) - (b.created_at || 0) ||
        (a.id < b.id ? -1 : a.id > b.id ? 1 : 0)
    );
}


// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------

function placeholderAvatar(persona, speakerId, opts = {}) {
  const span = document.createElement("span");
  span.className = "avatar placeholder " + (persona || "");
  if (opts.small) span.classList.add("small");
  const label = persona === "narrator" ? "N"
              : persona === "user" ? userPersonaName()[0]
              : (speakerId ? entityName(speakerId)[0] : "?");
  span.textContent = (label || "?").toUpperCase();
  return span;
}
function avatarFor(persona, speakerId) {
  // For user messages, route through the persona card's portrait when
  // one is set; otherwise fall back to the user placeholder.
  let portraitId = speakerId;
  if (persona === "user") portraitId = userPersonaPortraitId();
  const portrait = portraitId ? portraitUrl(portraitId) : null;
  if (portrait) {
    const img = document.createElement("img");
    img.className = "avatar";
    img.src = portrait;
    img.alt = persona === "user" ? userPersonaName() : entityName(speakerId);
    img.loading = "lazy";
    img.addEventListener("error", () => {
      img.replaceWith(placeholderAvatar(persona, speakerId));
    });
    return img;
  }
  return placeholderAvatar(persona, speakerId);
}

// ---------------------------------------------------------------------------
// Message attachments framework
//
// An "attachment" is any panel anchored to a single message — thinking
// trace, narrator-edit metadata, applied-edits chips, phrase-hits chips,
// the siblings chip, etc. They all share the same lifecycle: read a key
// from msg.metadata, render a DOM block, slot it inside .msg-content so
// it inherits the message card's width / padding / responsive behaviour.
//
// Spec:
//   id     — metadata key the attachment owns (e.g. "thinking")
//   slot   — "above-body" | "below-body" | "header-actions"
//   order  — sort within slot (lower = earlier); default 0
//   show   — (msg) => boolean; default = !!msg.metadata?.[id]
//   render — (msg) => Element; returns the panel DOM
//
// renderMessage walks the registry per slot and appends whatever opts
// in. Adding a new panel = registerAttachment({...}); no edit to
// renderMessage. Streaming UIs can later route SSE deltas to a specific
// attachment via its id without forking the render path.
// ---------------------------------------------------------------------------
const ATTACHMENTS = [];

function registerAttachment(spec) {
  const a = {
    order: 0,
    show: (msg) => !!(msg.metadata && msg.metadata[spec.id]),
    ...spec,
  };
  if (!a.id || !a.slot || typeof a.render !== "function") {
    console.warn("registerAttachment: spec missing id/slot/render", spec);
    return;
  }
  ATTACHMENTS.push(a);
}

function attachmentsForSlot(slot) {
  return ATTACHMENTS
    .filter((a) => a.slot === slot)
    .sort((a, b) => a.order - b.order);
}

function renderAttachmentSlot(msg, slot, host) {
  for (const a of attachmentsForSlot(slot)) {
    if (!a.show(msg)) continue;
    const node = a.render(msg);
    if (node) host.appendChild(node);
  }
}

function renderMessage(msg) {
  const wrap = document.createElement("article");
  wrap.className = `msg msg-${msg.persona}`;
  wrap.dataset.messageId = msg.id;

  const content = document.createElement("div");
  content.className = "msg-content";

  // Action row: full-width strip at the top of the box. Always visible
  // (no hover-reveal) so it's tappable on touch devices.
  // Root narrator gets the same button set as descendants (Regen
  // re-rolls the opening, Continue extends it, Narrator can rewrite).
  // Continue is hidden for user posts since "extending" a user message
  // doesn't make sense.
  const actions = document.createElement("div");
  actions.className = "msg-actions";
  actions.appendChild(actionBtn("Edit", () => editMessage(msg),
    "Edit (creates a new branch)"));
  actions.appendChild(actionBtn("Raw", () => editMessage(msg, { inPlace: true }),
    "Raw edit — fix this message in place without forking history"));
  // Regen re-rolls an AI turn; on a user message it generated "as the user"
  // (a no-op) and only force-scrolled. Hide it there, same as Continue below.
  if (msg.persona !== "user") {
    actions.appendChild(actionBtn("Regen", () => regenerate(msg),
      msg.parent_id ? "Regenerate just this message" : "Re-roll the opening"));
  }
  if (msg.metadata && msg.metadata.multi_response) {
    actions.appendChild(actionBtn("Regen group", () => regenerateGroup(msg),
      "Re-roll the whole multi-response group from the lead"));
  }
  actions.appendChild(actionBtn("Narrator", () => narratorEditMessage(msg),
    "Rewrite this message via a narrator directive (also applies state changes like outfit swaps)"));
  if (msg.persona !== "user") {
    actions.appendChild(actionBtn("Continue", () => continueMessage(msg), "Extend this response"));
  }
  actions.appendChild(actionBtn("Delete", () => deleteMessage(msg),
    msg.parent_id ? "Delete this message and all descendants" : "Delete the root + everything below"));
  if (msg.metadata?.pending_edits?.length) {
    actions.appendChild(actionBtn("Edits ●", () => reviewEdits(msg), "Review proposed world edits"));
  }
  content.appendChild(actions);

  // Header: name + timestamp. Avatar sits below in its own row.
  const header = document.createElement("header");
  header.className = "msg-header";
  const meta = document.createElement("div");
  meta.className = "msg-meta";
  meta.innerHTML = `<strong>${escapeHtml(speakerLabel(msg))}</strong>` +
    (msg.created_at ? `<span class="muted small">· ${fmtTime(msg.created_at)}</span>` : "");
  header.appendChild(meta);
  content.appendChild(header);

  // Avatar inside the box, beneath the name.
  const av = avatarFor(msg.persona, msg.speaker_id);
  av.classList.add("msg-avatar");
  content.appendChild(av);

  renderAttachmentSlot(msg, "above-body", content);

  // Life Sim layout fix-up: if both the stat-bars block and the image-
  // pack figure are present in above-body, wrap them in a flex row so
  // the bars sit vertically next to the image. When only bars exist,
  // they already render full-width horizontally per their CSS.
  const _bars = content.querySelector(":scope > .msg-stat-bars");
  const _img = content.querySelector(":scope > .msg-image-pack");
  if (_bars && _img) {
    const row = document.createElement("div");
    row.className = "msg-stat-image-row";
    content.insertBefore(row, _bars);
    row.appendChild(_bars);
    row.appendChild(_img);
    _bars.classList.add("vertical");
  } else if (_bars) {
    _bars.classList.add("horizontal");
  }

  const body = document.createElement("div");
  body.className = "msg-body";
  body.innerHTML = formatBody(msg.content, speakerLabel(msg));
  content.appendChild(body);

  renderAttachmentSlot(msg, "below-body", content);

  wrap.appendChild(content);
  // Modules: fire per-message hooks. Modules' JS can read msg
  // metadata and decorate the element (CSS classes, appended
  // children, sprite refreshes). Late so the message is fully
  // constructed when modules see it.
  if (window.Modules && Modules._fireMessage) Modules._fireMessage(wrap, msg);
  return wrap;
}

function buildSiblingsRow(msg) {
  const sibs = siblingsOf(msg);
  const idx = sibs.findIndex((m) => m.id === msg.id);
  const sib = document.createElement("div");
  sib.className = "siblings";
  const prev = actionBtn("◀", () => switchToSibling(sibs, idx - 1), "Previous branch");
  const next = actionBtn("▶", () => switchToSibling(sibs, idx + 1), "Next branch");
  const label = document.createElement("span");
  label.className = "muted small";
  // For root sibling rows (parent_id == null) where every sibling carries a
  // setup, render the row as a setup picker instead of "Branch 1/N" so the
  // user sees what scene each root represents.
  const allSetups =
    !msg.parent_id &&
    sibs.every((s) => s?.metadata?.setup?.id);
  if (allSetups) {
    const setup = msg?.metadata?.setup || {};
    label.textContent = `Setup: ${setup.name || setup.id} (${idx + 1} / ${sibs.length})`;
    if (setup.description) label.title = setup.description;
  } else {
    label.textContent = `Branch ${idx + 1} / ${sibs.length}`;
  }
  sib.append(prev, label, next);
  return sib;
}
registerAttachment({
  id: "siblings",
  slot: "below-body",
  // After applied_edits (10) and phrase_hits (20), so the branch
  // chip sits at the bottom of the card like before.
  order: 100,
  // Computed from the message tree, not from msg.metadata, so override
  // show with a sibling-count check.
  show: (msg) => siblingsOf(msg).length > 1,
  render: buildSiblingsRow,
});

// ---------------------------------------------------------------------------
// Image pack
//
// Two-call flow per character response:
//   1. The text response is generated (existing streaming pipeline).
//   2. After streaming completes, if image_pack_pick is enabled and the
//      speaker has a non-empty properties.image_pack.entries catalog, we POST to
//      /api/conversations/<cid>/messages/<mid>/image_pick. The server
//      asks the model to pick an entry id, validates it exists in the
//      catalog (retry once on failure), and returns the chosen image.
// Picks live in client memory only — they don't persist to msg.metadata,
// so a page reload re-picks. (User asked: "each message will have new
// image call".) The "loading" state is rendered as a small placeholder
// block so the message doesn't jump when the image arrives.
// ---------------------------------------------------------------------------

// Transient per-message overlay state. The persisted source of truth is
// msg.metadata.image_pack_pick, written by the server when a pick
// succeeds; this map only tracks the in-flight "loading" placeholder
// while the model call is still outstanding.
state.imagePackPicks = state.imagePackPicks || {};

// Resolve the effective image-pack state for a message: persisted
// metadata wins, then any transient "loading" sentinel, otherwise null.
function imagePackFor(msg) {
  const persisted = msg?.metadata?.image_pack_pick;
  if (persisted) return persisted;
  return state.imagePackPicks[msg?.id] || null;
}

function imagePackEnabledForConv() {
  // Per-conversation override wins; otherwise fall back to the global
  // default surfaced in window.GEMMASIM_INITIAL.config.defaults.
  const s = state.conversation.settings || {};
  if (s.image_pack_pick === true) return true;
  if (s.image_pack_pick === false) return false;
  const cfgDef = window.GEMMASIM_INITIAL?.config?.defaults?.image_pack_pick;
  return !!cfgDef;
}

function autoStateEnabledForConv() {
  const s = state.conversation.settings || {};
  if (s.auto_state_changes === true) return true;
  if (s.auto_state_changes === false) return false;
  const cfgDef = window.GEMMASIM_INITIAL?.config?.defaults?.auto_state_changes;
  return !!cfgDef;
}

function autoStateAspectEnabledForConv(key) {
  // Per-aspect Auto State toggle (auto_state_transparency / _location).
  const s = state.conversation.settings || {};
  if (s[key] === true) return true;
  if (s[key] === false) return false;
  return !!(window.GEMMASIM_INITIAL?.config?.defaults || {})[key];
}

function autoStateAnyEnabledForConv() {
  // The /auto_state route runs whichever aspect passes are on, so the
  // character-turn pass fires if ANY aspect (clothing/transparency/
  // location) is enabled.
  return autoStateEnabledForConv()
    || autoStateAspectEnabledForConv("auto_state_transparency")
    || autoStateAspectEnabledForConv("auto_state_location");
}

function autoStateOnUserEnabledForConv() {
  // Sub-toggle: also run auto-state on USER messages. Off by default
  // and silently no-op when the parent `auto_state_changes` toggle
  // is off — the server enforces this too, this client-side check
  // saves an unnecessary round-trip.
  if (!autoStateEnabledForConv()) return false;
  const s = state.conversation.settings || {};
  if (s.auto_state_on_user_messages === true) return true;
  if (s.auto_state_on_user_messages === false) return false;
  const cfgDef = window.GEMMASIM_INITIAL?.config?.defaults?.auto_state_on_user_messages;
  return !!cfgDef;
}

function autoStateOnNarratorEnabledForConv() {
  // Sub-toggle: run a FULL narrator-add pass on user-typed narrator
  // messages (cast_add / move / outfit / set, not the wardrobe-only
  // auto-state prompt). Same precedence chain as the user variant.
  if (!autoStateEnabledForConv()) return false;
  const s = state.conversation.settings || {};
  if (s.auto_state_on_narrator_messages === true) return true;
  if (s.auto_state_on_narrator_messages === false) return false;
  const cfgDef = window.GEMMASIM_INITIAL?.config?.defaults?.auto_state_on_narrator_messages;
  return !!cfgDef;
}

function speakerHasImagePack(speakerId) {
  if (!speakerId) return false;
  const e = state.entities?.[speakerId];
  const props = e?.properties || {};
  // New unified schema. Combined: server composes from outfit+room
  // state, no catalog required — fire the picker as long as a sprite_id
  // is configured. Tagged: a non-empty {caption, image_url} catalog
  // gates the picker.
  const images = props.images;
  if (images && typeof images === "object") {
    const fmt = (images.format || "").toLowerCase();
    if (fmt === "combined") return !!(images.sprite_id || "").trim();
    if (fmt === "tagged") {
      return (images.entries || []).some((x) => x && x.image_url);
    }
  }
  // Legacy fallback for characters not yet migrated (legacy tagged characters still on
  // the bare `image_pack` field; any older sprite_id placement).
  if ((props.sprite_id || "").trim()) return true;
  const pack = props.image_pack;
  if (!pack) return false;
  return (pack.entries || []).some((x) => x && x.image_url);
}

function buildImagePackBlock(pick) {
  const block = document.createElement("figure");
  block.className = "msg-image-pack";
  if (pick === "loading") {
    // Compact pill — no big empty box reserving 4:3 of the column width.
    block.classList.add("loading");
    block.append(document.createTextNode("Picking image…"));
    return block;
  }
  const img = document.createElement("img");
  img.loading = "lazy";
  img.decoding = "async";
  img.alt = pick.caption || "";
  // Hold the image hidden until it has loaded, then reveal in the same
  // frame as the natural dimensions arrive — collapses the placeholder
  // → final-size transition into one layout step.
  img.style.opacity = "0";
  img.addEventListener("load", () => { img.style.opacity = ""; }, { once: true });
  // If the URL fails to load, drop the whole block silently — better
  // than a broken-image icon stranded in the middle of the chat.
  img.addEventListener("error", () => block.remove());
  img.src = pick.image_url;
  block.appendChild(img);
  return block;
}

// Collapsible "Reasoning trace" for the image chooser, mirroring the
// thinking attachment's <details> shape. Renders the prompt sent to the
// pick model and its raw reply so the user can see how the choice was
// made before reading the response.
function buildImageTraceBlock(pick) {
  const trace = pick?.trace || {};
  const det = document.createElement("details");
  det.className = "msg-thinking";
  const sum = document.createElement("summary");
  sum.className = "thinking-summary";
  const label = document.createElement("span");
  label.className = "thinking-label";
  const reply = trace.reply || "";
  // Lead with the chosen id (or the failure mode) so the summary is
  // useful at a glance — character count is secondary.
  const chosen = pick && pick.id != null ? `#${pick.id}` : "no pick";
  label.textContent = `Image pick · ${chosen} · ${reply.length}ch`;
  sum.appendChild(label);
  det.appendChild(sum);
  const tb = document.createElement("div");
  tb.className = "msg-thinking-body";
  tb.textContent =
    (trace.prompt ? `[Prompt]\n${trace.prompt}\n\n` : "") +
    `[Reply]\n${reply}`;
  det.appendChild(tb);
  return det;
}

registerAttachment({
  id: "image_pack_trace",
  slot: "above-body",
  // Between thinking (10) and image (50) so the chooser's reasoning sits
  // immediately under the response's own thinking trace, before the image.
  order: 40,
  show: (msg) => {
    const p = imagePackFor(msg);
    return p && p !== "loading" && p.trace;
  },
  render: (msg) => buildImageTraceBlock(imagePackFor(msg)),
});

// ---------------------------------------------------------------------------
// Scenario staging panel
//
// Rendered on a *staging* setup root — a special narrator message
// with `metadata.staging` = true that the seed code creates instead
// of a fully-populated narrator opener. The panel is the visible UI:
// dropdowns to swap partner / add items, chips for the items in cast
// (each with a × to remove), and a Start button that POSTs
// /scenario-prep/start. The server appends the opening narrator
// prose as a child of the staging root and hangs the first_message
// greetings off that — so the staging IS its own message in the
// branch, the narrator response is a CHILD message.
//
// Pre-start reroll-partner swaps in place (no wipe); post-start
// reroll requires confirmation since it resets the conversation.
// The panel hides itself once the staging root has any children —
// "started" is just "has descendants."
// ---------------------------------------------------------------------------

// Staging panel attachment is computed at render time and gets
// closure access to its own state (selected dropdown values, etc).
// The msg is the staging root narrator message.
function buildScenarioStagingPanel(msg) {
  const meta = msg.metadata || {};
  const picks = meta.random_picks || {};
  const scenarioId = state.conversation.scenario_id;
  const scenario = state.entities?.[scenarioId] || {};
  // Source of truth for the partner is THIS BRANCH'S cast (not the
  // shared instance pool) — a sibling branch may have a different
  // partner picked, and reading from state.entities alone would
  // surface partners from other branches. Filter by the branch's
  // effective cast set.
  const pool = scenario.random_character_pool || [];
  const inCastPool = Object.values(state.entities || {})
    .filter((e) => (
      e && e.type === "character" && e.id !== "user"
      && pool.includes(e.id)
      && state.effectiveCastChars.has(e.id)
    ))
    .map((e) => e.id);
  const partnerId = (picks.partner && inCastPool.includes(picks.partner))
    ? picks.partner
    : (inCastPool[0] || picks.partner);

  // Pools come from the scenario entity itself. We pull the master
  // entity (instance copy) so the partner pool reflects whatever pool
  // the scenario author declared — and a future scenario with
  // different pool names just works.
  const partnerPool = scenario.random_character_pool || [];
  const itemPool = scenario.random_item_pool || [];

  // Items in this branch's cast (NOT the full shared pool). Without
  // the effectiveCastObjects filter, items removed on this branch
  // still re-render with their − button after the panel rebuilds
  // because the instance file stays around for sibling branches.
  const inCastItems = Object.values(state.entities || {})
    .filter((e) => e && e.type === "object" && state.effectiveCastObjects.has(e.id));
  const inCastIds = new Set(inCastItems.map((e) => e.id));
  const itemPoolNotInCast = itemPool.filter((id) => !inCastIds.has(id));

  const wrap = document.createElement("div");
  wrap.className = "scenario-staging";

  const title = document.createElement("div");
  title.className = "scenario-staging-title muted small";
  title.textContent = "Scene staging — pick setup, partner + items, then press Start";
  wrap.appendChild(title);

  // -------- Setup row (only when 2+ sibling staging roots exist) --------
  // Each setup gets its own staging root as a sibling under None;
  // the dropdown is just navigation between them. The user could also
  // click the existing root branch arrows, but having an explicit
  // "Setup: Roommates ▾ / First Date" picker on the panel is more
  // discoverable than the tiny ◀ / ▶ chips.
  const siblingRoots = Object.values(state.conversation.messages || {})
    .filter((m) => m.parent_id === null && m.metadata && m.metadata.staging && m.metadata.setup)
    .sort((a, b) => (a.created_at || 0) - (b.created_at || 0));
  if (siblingRoots.length > 1) {
    const setupRow = document.createElement("div");
    setupRow.className = "scenario-staging-row";
    const setupLabel = document.createElement("span");
    setupLabel.className = "muted small scenario-staging-label";
    setupLabel.textContent = "Setup:";
    setupRow.appendChild(setupLabel);
    const setupSel = document.createElement("select");
    setupSel.className = "scenario-staging-select";
    for (const r of siblingRoots) {
      const opt = document.createElement("option");
      opt.value = r.id;
      const setup = r.metadata && r.metadata.setup;
      opt.textContent = (setup && (setup.name || setup.id)) || r.id;
      if (r.id === msg.id) opt.selected = true;
      setupSel.appendChild(opt);
    }
    setupSel.addEventListener("change", () => {
      const target = siblingRoots.find((r) => r.id === setupSel.value);
      if (target) setActiveLeaf(deepestActiveLeaf(target.id));
    });
    setupRow.appendChild(setupSel);
    wrap.appendChild(setupRow);
  }

  // -------- Partner row --------
  const partnerRow = document.createElement("div");
  partnerRow.className = "scenario-staging-row";
  const partnerLabel = document.createElement("span");
  partnerLabel.className = "muted small scenario-staging-label";
  partnerLabel.textContent = "Partner:";
  partnerRow.appendChild(partnerLabel);
  const partnerSel = document.createElement("select");
  partnerSel.className = "scenario-staging-select";
  for (const pid of partnerPool) {
    const opt = document.createElement("option");
    opt.value = pid;
    const ent = state.entities[pid];
    opt.textContent = (ent?.name) || pid;
    if (pid === partnerId) opt.selected = true;
    partnerSel.appendChild(opt);
  }
  partnerSel.addEventListener("change", () => {
    if (partnerSel.value === partnerId) return;
    swapStagingPartner(msg, partnerSel.value);
  });
  partnerRow.appendChild(partnerSel);
  wrap.appendChild(partnerRow);

  // -------- Items row: dropdown + add --------
  if (itemPool.length > 0) {
    const itemAddRow = document.createElement("div");
    itemAddRow.className = "scenario-staging-row";
    const itemLabel = document.createElement("span");
    itemLabel.className = "muted small scenario-staging-label";
    itemLabel.textContent = "Add item:";
    itemAddRow.appendChild(itemLabel);

    const itemSel = document.createElement("select");
    itemSel.className = "scenario-staging-select";
    if (itemPoolNotInCast.length === 0) {
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = "— pool empty —";
      itemSel.appendChild(opt);
      itemSel.disabled = true;
    } else {
      const placeholder = document.createElement("option");
      placeholder.value = "";
      placeholder.textContent = "— pick an item —";
      itemSel.appendChild(placeholder);
      for (const iid of itemPoolNotInCast) {
        const opt = document.createElement("option");
        opt.value = iid;
        const ent = state.entities[iid];
        opt.textContent = (ent?.name) || iid;
        itemSel.appendChild(opt);
      }
    }
    itemAddRow.appendChild(itemSel);

    const addBtn = document.createElement("button");
    addBtn.type = "button";
    addBtn.className = "ghost xs";
    addBtn.textContent = "Add";
    addBtn.disabled = itemSel.disabled;
    addBtn.addEventListener("click", async () => {
      const id = itemSel.value;
      if (!id) return;
      addBtn.disabled = true;
      await addStagingItem(msg, id);
    });
    itemAddRow.appendChild(addBtn);
    wrap.appendChild(itemAddRow);
  }

  // -------- Items row: chips for items in cast --------
  if (inCastItems.length > 0) {
    const itemListRow = document.createElement("div");
    itemListRow.className = "scenario-staging-row";
    const itemListLabel = document.createElement("span");
    itemListLabel.className = "muted small scenario-staging-label";
    itemListLabel.textContent = "In scene:";
    itemListRow.appendChild(itemListLabel);
    for (const item of inCastItems) {
      const chip = document.createElement("span");
      chip.className = "scenario-staging-chip";
      chip.textContent = (item.name || item.id);
      const x = document.createElement("button");
      x.type = "button";
      x.className = "scenario-staging-chip-rm";
      x.textContent = "×";
      x.title = "Remove from scene";
      x.addEventListener("click", () => removeStagingItem(msg, item.id));
      chip.appendChild(x);
      itemListRow.appendChild(chip);
    }
    wrap.appendChild(itemListRow);
  }

  // -------- Start buttons --------
  const startRow = document.createElement("div");
  startRow.className = "scenario-staging-row scenario-staging-actions";

  const npcStartBtn = document.createElement("button");
  npcStartBtn.type = "button";
  npcStartBtn.className = "ghost";
  npcStartBtn.textContent = "NPC starts ▸";
  npcStartBtn.title = "Lock the staging in AND have the partner write the first message after the narrator opening";
  npcStartBtn.addEventListener("click", () => startStaging(msg, { npcStarts: true }));
  startRow.appendChild(npcStartBtn);

  const startBtn = document.createElement("button");
  startBtn.type = "button";
  startBtn.className = "primary";
  startBtn.textContent = "Start ▸";
  startBtn.title = "Lock the staging in and write the opening scene; the user takes the first turn";
  startBtn.addEventListener("click", () => startStaging(msg, { npcStarts: false }));
  startRow.appendChild(startBtn);

  wrap.appendChild(startRow);

  return wrap;
}

async function swapStagingPartner(msg, newPartnerId) {
  // Pre-start swap is non-destructive — the server updates the active
  // root's metadata + presence in place.
  const userTurns = Object.values(state.conversation.messages || {})
    .filter((m) => m.persona === "user").length;
  if (userTurns > 0) {
    const ok = await confirmAction(
      `Swap to a different partner? Conversation has ${userTurns} user turn${userTurns === 1 ? "" : "s"}; reroll wipes them and resets every setup root.`
    );
    if (!ok) {
      // Revert dropdown (re-render via reload of conversation state).
      try { await reloadConversation(); } catch (_) {}
      return;
    }
  }
  try {
    await jfetch(`/api/conversations/${conversationId}/scenario-prep/reroll-partner`, {
      method: "POST",
      body: JSON.stringify({ partner_id: newPartnerId }),
    });
  } catch (e) {
    flashError("Swap failed: " + e.message);
    return;
  }
  // Reload reflects the new partner / presence / panel state.
  if (userTurns > 0) {
    window.location.reload();
  } else {
    // Pre-start path: just refresh the conversation + entities and
    // re-render so the panel shows the new selection without a full
    // page reload.
    try {
      await reloadConversation();
      const r = await jfetch(`/api/conversations/${conversationId}/entities`);
      state.entities = r.entities || {};
    } catch (_) {}
    renderCastList();
    fullRender();
    // Active leaf doesn't change on a pre-start swap (we're still on
    // the same staging root), so loadActiveSetup's leaf-keyed cache
    // would skip the refetch. Force it so the sidebar's [Scenario]
    // text + the staging panel's metadata reflect the new partner.
    try {
      if (typeof loadActiveSetup === "function") await loadActiveSetup({ force: true });
    } catch (_) {}
  }
}

// Mirror a cast +/- edit onto the active leaf's metadata.applied_edits
// in the client's state.conversation.messages copy. The server already
// persisted it; without this mirror, future computeEffectiveCastForLeaf
// walks (e.g. branch-switch and return) replay the path from stale
// client memory and the removal/addition disappears until refresh.
function appendAppliedEditOnActiveLeaf(entry) {
  const leafId = state.conversation.active_path_leaf;
  const leaf = leafId && state.conversation.messages
    ? state.conversation.messages[leafId]
    : null;
  if (!leaf) return;
  const meta = leaf.metadata = leaf.metadata || {};
  const log = meta.applied_edits = meta.applied_edits || [];
  if (entry.kind === "cast_add" || entry.kind === "cast_remove") {
    // Only suppress when the LAST edit for this id is already the
    // same kind — a no-op duplicate (e.g. double-click Add). When a
    // cast_remove sits between the previous cast_add and this new
    // one, the path-replay needs to see all three so the id ends up
    // re-included. The earlier blanket dedupe was dropping the
    // re-add edit and the character vanished on the next branch
    // walk despite being in state.effectiveCastChars.
    let lastForId = null;
    for (let i = log.length - 1; i >= 0; i--) {
      const e = log[i];
      if (!e) continue;
      if ((e.kind === "cast_add" || e.kind === "cast_remove") && e.id === entry.id) {
        lastForId = e;
        break;
      }
    }
    if (lastForId && lastForId.kind === entry.kind) return;
  }
  log.push(entry);
}

async function addStagingItem(msg, itemId) {
  try {
    await jfetch(`/api/conversations/${conversationId}/cast/${itemId}`, {
      method: "POST",
      body: JSON.stringify({}),
    });
  } catch (e) {
    flashError("Add failed: " + e.message);
    return;
  }
  // The shared instance pool may have grown (file freshly pulled)
  // and the active leaf now carries a cast_add edit. Refresh the
  // entities mirror, mark the new id as in-cast for this branch,
  // and rebuild the dropdowns so the new char shows up everywhere.
  try {
    const r = await jfetch(`/api/conversations/${conversationId}/entities`);
    state.entities = r.entities || {};
  } catch (_) {}
  const etype = (state.entities[itemId] || {}).type;
  if (etype === "object") state.effectiveCastObjects.add(itemId);
  else state.effectiveCastChars.add(itemId);
  appendAppliedEditOnActiveLeaf({ kind: "cast_add", ok: true, id: itemId });
  renderCastList();
  renderPersonaResponderDropdowns();
  rerenderMessage(msg);
}

async function removeStagingItem(msg, itemId) {
  try {
    await jfetch(`/api/conversations/${conversationId}/cast/${itemId}`, {
      method: "DELETE",
    });
  } catch (e) {
    flashError("Remove failed: " + e.message);
    return;
  }
  // Branch-scoped removal: server emits a cast_remove edit on the
  // active leaf and never deletes the instance file. Drop the id
  // from the local cast set so the widget hides them on this branch
  // without nuking the shared entities mirror — sibling branches
  // that still have them are unaffected.
  state.effectiveCastChars.delete(itemId);
  state.effectiveCastObjects.delete(itemId);
  appendAppliedEditOnActiveLeaf({ kind: "cast_remove", ok: true, id: itemId });
  renderCastList();
  renderPersonaResponderDropdowns();
  rerenderMessage(msg);
}

async function startStaging(msg, { npcStarts = false } = {}) {
  // When `npcStarts` is true, the staging "Start" creates the
  // narrator opening as today AND the partner takes the first
  // in-character turn — server doesn't generate it (no model call
  // belongs in a sync POST), so we stash the partner id for the
  // post-reload page to pick up and fire streamGenerate against.
  // Storage key is conversation-scoped so two open chats on
  // different conversations don't cross-pollinate.
  let partnerForFirstTurn = null;
  if (npcStarts) {
    // Resolve partner from staging metadata first; fall back to the
    // cast-derived value the staging panel uses, so a Library-swap
    // user gets the same answer the server used to substitute the
    // opening prose.
    const meta = msg.metadata || {};
    const picks = meta.random_picks || {};
    partnerForFirstTurn = picks.partner;
    if (!partnerForFirstTurn) {
      const scenarioId = state.conversation.scenario_id;
      const scenario = state.entities?.[scenarioId] || {};
      const pool = scenario.random_character_pool || [];
      const inCastPool = Object.values(state.entities || {})
        .filter((e) => e && e.type === "character" && e.id !== "user" && pool.includes(e.id));
      if (inCastPool.length) partnerForFirstTurn = inCastPool[0].id;
    }
  }

  try {
    await jfetch(`/api/conversations/${conversationId}/scenario-prep/start`, {
      method: "POST",
      body: JSON.stringify({}),
    });
  } catch (e) {
    flashError("Start failed: " + e.message);
    return;
  }
  if (partnerForFirstTurn) {
    sessionStorage.setItem(
      `pending_npc_first_turn:${conversationId}`,
      partnerForFirstTurn,
    );
  }
  // Reload to pick up the new narrator child + first_message chain.
  // After the reload the page-load handler at the bottom of chat.js
  // checks the sessionStorage flag and fires streamGenerate for the
  // partner if set.
  window.location.reload();
}

registerAttachment({
  id: "scenario_staging",
  slot: "below-body",
  // Above siblings (100), below applied_edits (10) and phrase_hits (20).
  order: 30,
  show: (msg) => {
    const meta = msg.metadata || {};
    if (!(meta.opening && meta.staging && meta.setup)) return false;
    // "Started" = the staging root has at least one child. Once the
    // narrator response + first_messages have been seeded as children,
    // hide the panel — staging is locked in.
    const hasChildren = Object.values(state.conversation.messages || {})
      .some((m) => m.parent_id === msg.id);
    return !hasChildren;
  },
  render: buildScenarioStagingPanel,
});

// ---------------------------------------------------------------------------
// Scene staging panel
//
// Per-setup gate: a setup with `scene_staging_fields` on the scenario
// JSON gets its root flagged metadata.scene_staging at conversation
// creation. Panel renders below the root and lets the user pick
// characters (multi-select), per-character outfit, location + room,
// and a free-text prompt. Each Start spawns a fresh child branch off
// this root — the panel stays visible after children exist so the
// user can re-stage as many times as they want, each click yielding
// a sibling branch chain.
// ---------------------------------------------------------------------------

const sceneStagingOptionsCache = new Map();

function currentUserCardId() {
  const p = state.conversation?.settings?.user_persona;
  const v = p && typeof p.card_id === "string" ? p.card_id : "";
  return v || "";
}

async function loadSceneStagingOptions(scenarioId, setupId) {
  const cardId = currentUserCardId();
  // Cache key includes the card id so the per-card user-outfit list
  // (Alex's wardrobe vs the default generic_blonde_guy set) doesn't
  // bleed across user-persona switches in the same page session.
  const key = `${scenarioId}::${setupId}::${cardId}`;
  if (sceneStagingOptionsCache.has(key)) {
    return sceneStagingOptionsCache.get(key);
  }
  const q = cardId ? `?user_card_id=${encodeURIComponent(cardId)}` : "";
  const r = await jfetch(`/api/scenarios/${scenarioId}/scene-staging/${setupId}/options${q}`);
  sceneStagingOptionsCache.set(key, r);
  return r;
}

function buildSceneStagingPanel(msg) {
  const meta = msg.metadata || {};
  const setup = meta.setup || {};
  const setupId = setup.id;
  const scenarioId = state.conversation.scenario_id;

  const wrap = document.createElement("div");
  wrap.className = "scenario-staging";

  const title = document.createElement("div");
  title.className = "scenario-staging-title muted small";
  title.textContent = "Scene staging — pick characters, location, outfits, and prompt; each Start creates a new branch";
  wrap.appendChild(title);

  const loading = document.createElement("div");
  loading.className = "muted small";
  loading.textContent = "Loading options…";
  wrap.appendChild(loading);

  // Panel state lives in this closure.
  const picks = {
    characters: new Set(),
    outfits: {},                  // char_id → outfit_id (primary)
    slot_states: {},              // char_id → {slot: 1|2|3}
    // Optional per-character placement override. char_id → location/room id;
    // absent = use the scene-wide "Location (cast)" pick. Lets individual NPCs
    // start somewhere other than the batch location.
    castLocations: {},
    castRooms: {},
    // Per-instance description overrides for location / room entities.
    // Keyed by entity id so switching the location dropdown doesn't
    // lose an in-progress edit on the previous one. On submit only
    // entries that differ from the template description are sent.
    location_descriptions: {},    // location_id → description override
    room_descriptions: {},        // room_id → description override
    // Conversation-scoped new rooms added at staging time. Each entry
    // is {tmp_id, name, description, location_id}; the server resolves
    // tmp_id to a real entity id, writes the room file into the
    // conversation's instance dir, and the cast's `room` pick is
    // mapped from tmp_id to the real id before the [move] edits land.
    new_rooms: [],
    // Mix-and-match accessories per character — outfit-shape entities
    // with `is_accessory: true` that compose on top of the primary.
    // Server emits `[set <char>.properties.accessories = [...]]` patches
    // from this. Accessories that aren't equipped on a character are
    // absent from this map.
    accessories: {},              // char_id → [<accessory_id>, ...]
    // Per-character outfit-templating overlay. Color, material, fit,
    // style each override the primary outfit's own field at render
    // time via personas._apply_outfit_template. Lets bikini_generic
    // render as "gold" / "pink" / etc. without spinning up a new
    // outfit entity. Server emits
    // `[set <char>.properties.outfit_overrides.<key> = <value>]`.
    outfit_overrides: {},         // char_id → {color, material, fit, style}
    location: null,
    room: null,
    prompt: "",                   // user's typed seed for the location prompt
    scenario_instructions: null,  // base; pre-filled from scenario, then editable
    setup_append: "",
    narrator_edits: "",
    location_prompt: "",
    npc_starter: null,
    // Picked user persona: {name, description, preset_id?}. null
    // leaves the parent root's persona untouched.
    user_persona: null,
    // Modules: id -> bool active, plus settings the user tweaked on
    // the auto-generated form. The POST flattens active to a list
    // and ships settings as {id: {key: value}}.
    modules: {},          // id -> bool
    module_settings: {},  // id -> {settingKey: value}
    // Prefabs: per-prefab pick state, keyed by prefab id. Shape per
    // kind:
    //   object_picker       → { objects: Set<id>, equipped: {id: char_id} }
    //   per_character_toggle → { characters: Set<char_id> }
    // Both `objects` and `generic_objects` route through here. Legacy
    // top-level `objects` / `equipped` aliases are derived at POST
    // time from prefabs.objects for backwards-compat with any cached
    // server build.
    prefabs: {},
    // Life Sim staging: per character, per stat, the user's overrides.
    // Each inner dict carries only the fields the user touched (value
    // for an existing stat; full {value, label, min, max} for a newly-
    // added stat). The scene-stage POST sends these as
    // stats_edits: {char_id: {stat_id: {...}}} and the route emits a
    // single patch per character.
    stats_edits: {},
    // Stats the user removed from a character at staging time. Maps
    // char_id -> Set(stat_id). Backend translates to [unset
    // char.properties.stats.<id>] edits. Removing a stat the user
    // also has in stats_edits cancels the edit (so order doesn't
    // matter); removing a brand-new stat the user just added is
    // handled as a pure UI deletion (never sent to the server).
    stats_removed: {},
    // User staging: split cleanly into three picks plus a location.
    //   user_card_id  — base character template (Alex, Nadia, …)
    //   user_persona  — { role / role_description } overlay (existing)
    //   outfits.user  — clothing (character-agnostic, in picks.outfits)
    //   user_location / user_room — where the user starts
    user_card_id: null,
    user_location: null,
    user_room: null,
  };

  loadSceneStagingOptions(scenarioId, setupId).then((opts) => {
    loading.remove();
    // Author-supplied pre-selections (scene_staging_fields.defaults
    // on the setup, filtered to valid ids server-side). Seed the
    // simple `picks` fields here so the per-widget init in
    // renderSceneStagingBody can read them as the starting value.
    // Only applied on a fresh panel — if picks already has anything
    // set (re-render), skip the seed so we don't clobber the user.
    //
    // Characters and per-character outfits are NOT seeded here —
    // they need to flow through addCharacterRow (which lives inside
    // renderSceneStagingBody) to actually materialise the UI rows.
    // The character pre-add happens at the end of the cast section
    // setup below.
    const defaults = opts.defaults || {};
    if (picks.characters.size === 0 && !picks.location && !picks.user_card_id) {
      if (typeof defaults.location === "string") {
        picks.location = defaults.location;
      }
      if (typeof defaults.room === "string") {
        picks.room = defaults.room;
      }
      if (typeof defaults.user_card_id === "string") {
        picks.user_card_id = defaults.user_card_id;
      }
      if (typeof defaults.user_outfit === "string") {
        picks.outfits.user = defaults.user_outfit;
      }
      // Default user persona/role — surfaces the user into the scene as
      // e.g. "Literature Club member" the same way defaults.characters
      // pre-adds the default cast. Seeds picks.user_persona from the
      // matching preset; the "You" section pre-fills its fields to match.
      if (typeof defaults.user_persona === "string" && Array.isArray(opts.user_personas)) {
        const p = opts.user_personas.find((x) => x && x.id === defaults.user_persona);
        if (p) {
          picks.user_persona = opts.user_personas_are_roles
            ? { role: p.name || "", role_description: p.description || "", preset_id: p.id }
            : { name: p.name || "", description: p.description || "", preset_id: p.id };
        }
      }
    }
    renderSceneStagingBody(wrap, msg, opts, picks);
  }).catch((e) => {
    loading.textContent = `Failed to load options: ${e.message}`;
  });

  return wrap;
}

const SCENE_SLOT_ORDER = ["top", "bottom", "bra", "panties", "pantyhose", "gloves", "legwear", "shoes"];
const SCENE_SLOT_LABEL = { 1: "On", 2: "Half off", 3: "Off" };
const SCENE_SLOT_CYCLE = { 1: 2, 2: 3, 3: 1 };

// ---------------------------------------------------------------------------
// Outfit list+search widget — shared between the per-character outfit
// blocks (Outfit / Worn under / Accessories) and the user clothing block.
//
// `mode: "single"` — click row to set the selection; only one row is
//   .active at a time. The active row stays highlighted via the
//   `.active` class once `isSelected(id)` returns true.
// `mode: "multi"`  — click row to toggle; multi-select via the
//   data-checked="true" attribute on each row.
//
// All state lives in the caller — the widget never owns picks. It just
// reads them via `isSelected(id)` and writes them via `onPick(id)` /
// `onToggle(id, willBeSelected)`. The returned `refresh()` re-renders
// from the current pick state (useful when external state changes,
// e.g. the user equips an item via a different control).
//
// Items: array of {id, name, generic?, owner?}. The widget renders a
// "generic" tag on the right of any row with `generic: true`; the
// `owner` field is shown as "(<owner>)" when present and non-empty.
// ---------------------------------------------------------------------------
function buildOutfitListWidget(label, items, opts) {
  const wrap = document.createElement("div");
  wrap.className = "scenario-staging-list-section";

  const headRow = document.createElement("div");
  headRow.style.display = "flex";
  headRow.style.alignItems = "center";
  headRow.style.gap = "8px";
  const lbl = document.createElement("span");
  lbl.className = "scenario-staging-list-label";
  lbl.textContent = label;
  headRow.appendChild(lbl);

  // Optional "Clear" link — only rendered when opts.onClear is supplied.
  // Used by the user-clothing block to express "don't change" since
  // single-select otherwise has no way to reach the empty state.
  if (typeof opts.onClear === "function") {
    const clearBtn = document.createElement("button");
    clearBtn.type = "button";
    clearBtn.className = "scenario-staging-list-clear";
    clearBtn.textContent = opts.clearLabel || "Clear";
    clearBtn.addEventListener("click", () => {
      opts.onClear();
      render();
    });
    headRow.appendChild(clearBtn);
  }
  wrap.appendChild(headRow);

  const search = document.createElement("input");
  search.type = "search";
  search.className = "scenario-staging-list-search";
  search.placeholder = `Search ${items.length} items…`;
  // Hide the search box if the visible catalog is too small to bother.
  if (items.length <= 5) search.style.display = "none";

  const list = document.createElement("div");
  list.className = "scenario-staging-list";

  wrap.appendChild(search);
  wrap.appendChild(list);

  let query = "";
  const render = () => {
    list.innerHTML = "";
    const q = query.trim().toLowerCase();
    const skipId = typeof opts.skipId === "function" ? opts.skipId() : null;
    const filtered = items.filter((o) => {
      if (skipId && o.id === skipId) return false;
      if (!q) return true;
      return (`${o.name} ${o.id}`).toLowerCase().includes(q);
    });
    if (!filtered.length) {
      const empty = document.createElement("div");
      empty.className = "scenario-staging-list-empty";
      empty.textContent = q ? "(no matches)" : (opts.emptyText || "(none)");
      list.appendChild(empty);
      return;
    }
    for (const o of filtered) {
      const row = document.createElement("div");
      row.className = "scenario-staging-list-row";
      row.dataset.id = o.id;
      const selected = opts.isSelected(o.id);
      if (opts.mode === "single") {
        if (selected) row.classList.add("active");
      } else {
        row.dataset.checked = selected ? "true" : "false";
      }
      const name = document.createElement("span");
      name.textContent = o.name;
      row.appendChild(name);
      // Right-side tag: "generic" if generic, else "(owner)" when an
      // owner string is provided. The user-clothing block uses owner
      // tags ("alex", "samus") to hint where each piece comes from.
      let tagText = "";
      if (o.generic) tagText = "generic";
      else if (o.owner) tagText = o.owner;
      if (tagText) {
        const tag = document.createElement("span");
        tag.className = "row-tag";
        tag.textContent = tagText;
        row.appendChild(tag);
      }
      row.addEventListener("click", () => {
        if (opts.mode === "single") {
          if (opts.isSelected(o.id)) return;
          opts.onPick(o.id);
        } else {
          opts.onToggle(o.id, !opts.isSelected(o.id));
        }
      });
      list.appendChild(row);
    }
  };
  search.addEventListener("input", () => { query = search.value; render(); });
  render();
  return { wrap, refresh: render };
}

// ---------------------------------------------------------------------------
// Module settings renderer (shared between scene-staging panel and the
// live left-panel modules section).
//
// `target` is the mutable dict the control writes into on change —
// staging-panel passes `picks.module_settings[mid]`, live panel passes
// a closure-local dict it later POSTs. `onChange` (optional) fires after
// the value lands in `target` so the caller can persist immediately
// (the live-panel use case).
// ---------------------------------------------------------------------------
function renderModuleSettingControl(manifest, schema, target, charactersById, onChange) {
  if (!schema || !schema.id) return null;
  const sid = schema.id;
  const row = document.createElement("label");
  row.className = "module-setting-row";
  row.style.display = "flex";
  row.style.gap = "6px";
  row.style.alignItems = "center";
  const label = document.createElement("span");
  label.className = "muted small";
  label.textContent = schema.label || sid;
  if (schema.help) row.title = schema.help;

  const fire = () => { if (typeof onChange === "function") onChange(sid, target[sid]); };

  if (schema.type === "bool") {
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = !!target[sid];
    input.addEventListener("change", () => {
      target[sid] = input.checked;
      fire();
    });
    row.appendChild(input);
    row.appendChild(label);
    return row;
  }

  if (schema.type === "int") {
    row.appendChild(label);
    const input = document.createElement("input");
    input.type = "number";
    input.value = target[sid] != null ? target[sid] : (schema.default ?? 0);
    if (Number.isInteger(schema.min)) input.min = schema.min;
    if (Number.isInteger(schema.max)) input.max = schema.max;
    input.style.width = "80px";
    input.addEventListener("change", () => {
      const n = parseInt(input.value, 10);
      target[sid] = Number.isFinite(n) ? n : (schema.default ?? 0);
      fire();
    });
    row.appendChild(input);
    return row;
  }

  if (schema.type === "enum") {
    row.appendChild(label);
    const sel = document.createElement("select");
    for (const opt of schema.options || []) {
      const o = document.createElement("option");
      o.value = opt;
      o.textContent = opt;
      sel.appendChild(o);
    }
    sel.value = target[sid] != null ? target[sid] : (schema.default ?? "");
    sel.addEventListener("change", () => {
      target[sid] = sel.value;
      fire();
    });
    row.appendChild(sel);
    return row;
  }

  if (schema.type === "char_ref") {
    row.appendChild(label);
    const sel = document.createElement("select");
    const noneOpt = document.createElement("option");
    noneOpt.value = "";
    noneOpt.textContent = "— auto —";
    sel.appendChild(noneOpt);
    const charList = charactersById && typeof charactersById.entries === "function"
      ? Array.from(charactersById.entries())
      : Object.entries((state.conversation && state.conversation.entities) || {})
          .filter(([, e]) => e && e.type === "character");
    for (const [cid, c] of charList) {
      const o = document.createElement("option");
      o.value = cid;
      o.textContent = (c && c.name) || cid;
      sel.appendChild(o);
    }
    sel.value = target[sid] || "";
    sel.addEventListener("change", () => {
      target[sid] = sel.value || null;
      fire();
    });
    row.appendChild(sel);
    return row;
  }

  // Fallback: string input.
  row.appendChild(label);
  const input = document.createElement("input");
  input.type = "text";
  input.value = target[sid] != null ? String(target[sid]) : "";
  input.addEventListener("change", () => {
    target[sid] = input.value;
    fire();
  });
  row.appendChild(input);
  return row;
}

function renderSceneStagingBody(wrap, msg, opts, picks) {
  const charactersById = new Map((opts.characters || []).map((c) => [c.id, c]));
  const locations = opts.locations || [];
  const locationById = (id) => locations.find((l) => l.id === id) || null;

  // Shared cast-placement state so the per-character "Location" picker (in
  // each Cast card) and the "Locations" overview section stay in sync — both
  // read/write picks.castLocations / picks.castRooms through setCastLocation,
  // and any change re-renders every registered view.
  const castPlacementRefreshers = [];
  let castLocSection = null; // the "Location (cast)" collapsible; the per-character
                             // move overview is appended into its body.
  const refreshCastPlacement = () => {
    for (const fn of castPlacementRefreshers) {
      try { fn(); } catch (_) {}
    }
  };
  const setCastLocation = (cid, locId, roomId) => {
    if (locId) {
      picks.castLocations[cid] = locId;
      picks.castRooms[cid] =
        roomId || (locationById(locId)?.rooms?.[0] || {}).id || null;
    } else {
      delete picks.castLocations[cid];
      delete picks.castRooms[cid];
    }
    refreshCastPlacement();
  };

  // Life Sim ties: the stats sub-row per character has to react to the
  // user (un)checking life_sim in the modules section. Track each
  // character's sub-row here so the modules-toggle handler can iterate
  // and show/hide them in one pass. Visibility is driven entirely by
  // picks.modules.life_sim — life_sim unchecked = no stats UI for any
  // character, even if the character ships with declared stats.
  const statsBlocksByChar = new Map();  // char_id -> statsBlock element
  const refreshStatsVisibility = () => {
    const on = !!picks.modules.life_sim;
    for (const block of statsBlocksByChar.values()) {
      block.hidden = !on;
    }
  };

  // Collapsible-section helper. Every visible piece of the staging
  // panel goes inside one of these so the user can fold any section
  // away. Returns {section: <details>, body: <div>} — caller appends
  // controls to body and the section to wrap.
  const makeCollapsible = (title, { open = true } = {}) => {
    const section = document.createElement("details");
    section.className = "scenario-staging-section";
    section.open = open;
    const sum = document.createElement("summary");
    sum.textContent = title;
    section.appendChild(sum);
    const body = document.createElement("div");
    body.className = "scenario-staging-section-body";
    section.appendChild(body);
    return { section, body };
  };

  if (locations.length && !picks.location) {
    picks.location = locations[0].id;
    picks.room = (locations[0].rooms[0] || {}).id || null;
  }

  // -------- Cast section --------
  const castSection = makeCollapsible("Cast");
  const addRow = document.createElement("div");
  addRow.className = "scenario-staging-row";
  const addLabel = document.createElement("span");
  addLabel.className = "muted small scenario-staging-label";
  addLabel.textContent = "Add character:";
  addRow.appendChild(addLabel);

  const addSel = document.createElement("select");
  addSel.className = "scenario-staging-select";
  addRow.appendChild(addSel);

  const addBtn = document.createElement("button");
  addBtn.type = "button";
  addBtn.className = "ghost xs";
  addBtn.textContent = "Add";
  addRow.appendChild(addBtn);
  castSection.body.appendChild(addRow);

  const charList = document.createElement("div");
  charList.className = "scenario-staging-charlist";
  charList.style.display = "flex";
  charList.style.flexDirection = "column";
  charList.style.gap = "6px";
  castSection.body.appendChild(charList);
  wrap.appendChild(castSection.section);

  // `let` (not const) so the objects-prefab section below can wrap
  // this with equip-dropdown sync logic.
  let refreshAddDropdown = () => {
    addSel.innerHTML = "";
    const remaining = (opts.characters || []).filter((c) => !picks.characters.has(c.id));
    if (!remaining.length) {
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = "— all picked —";
      addSel.appendChild(opt);
      addSel.disabled = true;
      addBtn.disabled = true;
      return;
    }
    addSel.disabled = false;
    addBtn.disabled = false;
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = "— pick a character —";
    addSel.appendChild(placeholder);
    for (const c of remaining) {
      const opt = document.createElement("option");
      opt.value = c.id;
      opt.textContent = c.name;
      addSel.appendChild(opt);
    }
  };

  const buildSlotButtons = (charId, slotsContainer, currentSlots, partialLabel) => {
    slotsContainer.innerHTML = "";
    picks.slot_states[charId] = picks.slot_states[charId] || {};
    // Always render the 8 slots regardless of what the outfit
    // declares — the user can toggle any of them, and the picker
    // fills in defaults from the outfit's clothing_slots when it
    // has them (else "On"). State-2 label is whatever the outfit
    // calls its partial state (e.g., "Ripped" for the Zero Suit);
    // falls back to the global "Half off".
    const labelFor = (n) => {
      if (n === 2 && partialLabel) return partialLabel;
      return SCENE_SLOT_LABEL[n] || "On";
    };
    for (const slot of SCENE_SLOT_ORDER) {
      const def = (currentSlots[slot] | 0) || 1;
      let cur = picks.slot_states[charId][slot];
      if (cur == null) cur = def;
      picks.slot_states[charId][slot] = cur;
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "ghost xs scenario-staging-slot-btn";
      const renderLabel = () => {
        btn.textContent = `${slot}: ${labelFor(cur)}`;
      };
      renderLabel();
      btn.addEventListener("click", () => {
        cur = SCENE_SLOT_CYCLE[cur] || 1;
        picks.slot_states[charId][slot] = cur;
        renderLabel();
      });
      slotsContainer.appendChild(btn);
    }
  };

  const addCharacterRow = (charId, outfitOverride = null) => {
    const char = charactersById.get(charId);
    if (!char || picks.characters.has(charId)) return;
    picks.characters.add(charId);
    picks.outfits[charId] = outfitOverride
      || char.current_outfit
      || (char.outfits[0] || {}).id
      || null;
    picks.slot_states[charId] = {};

    // Each character is its own collapsible card so the user can
    // fold a configured character away. <details>/<summary> gives
    // the native disclosure widget; the × remove button lives inside
    // the summary but stops propagation so clicking it doesn't also
    // toggle open/closed.
    const block = document.createElement("details");
    block.className = "scenario-staging-char";
    block.open = true;

    const head = document.createElement("summary");
    head.className = "scenario-staging-char-head";

    const nameSpan = document.createElement("span");
    nameSpan.className = "muted small";
    nameSpan.style.fontWeight = "600";
    nameSpan.textContent = char.name;
    head.appendChild(nameSpan);

    const spacer = document.createElement("span");
    spacer.style.flex = "1 1 auto";
    head.appendChild(spacer);

    const rmBtn = document.createElement("button");
    rmBtn.type = "button";
    rmBtn.className = "scenario-staging-chip-rm";
    rmBtn.textContent = "×";
    rmBtn.title = "Remove from scene";
    // Stop the click from bubbling into <summary>'s default toggle.
    rmBtn.addEventListener("mousedown", (e) => e.stopPropagation());
    head.appendChild(rmBtn);

    block.appendChild(head);

    // Reuse the label:control row shape we use in the User block.
    const charSubRow = (label) => {
      const row = document.createElement("div");
      row.style.display = "flex";
      row.style.alignItems = "center";
      row.style.gap = "8px";
      const lbl = document.createElement("span");
      lbl.className = "muted small";
      lbl.style.minWidth = "78px";
      lbl.textContent = label;
      row.appendChild(lbl);
      return row;
    };

    // Wardrobe model — three zones the user can think about separately:
    //   1. Outfit         — single primary pick from full outfits only.
    //   2. Clothing       — per-slot toggle buttons (top/bra/etc.) for
    //                       the easy "shirt rolled up" single-part tweak.
    //   3. Worn under     — hidden under-layers (e.g. bikini under a
    //                       uniform).
    //   4. Accessories    — visible overlays (cat ears, gloves, tail,
    //                       tattoos, decals).
    // Worn under + Accessories both write to picks.accessories — the
    // composer reads each item's own `under` flag at render time for
    // under-vs-over semantics. The UI just splits the visual presentation
    // so the user isn't confronted with one big mixed checkbox row.
    // Wardrobe model — four zones the user can think about separately:
    //   1. Outfit         — single primary pick from full outfits only
    //                       (list + search; click row to equip).
    //   2. Clothing       — per-slot toggle buttons (top/bra/etc.) for
    //                       the easy "shirt rolled up" single-part tweak.
    //   3. Worn under     — hidden under-layers (bikini under a uniform;
    //                       list + search, multi-select).
    //   4. Accessories    — visible overlays (cat ears, gloves, tail,
    //                       tattoos, decals; list + search, multi-select).
    // Worn under + Accessories both write to picks.accessories — the
    // composer reads each item's own `under` flag at render time for
    // under-vs-over semantics. The UI just splits the visual presentation
    // so the user isn't confronted with one big mixed list.
    const catalog = char.outfits || [];
    const primaryCatalog = catalog.filter((o) => !o.is_accessory);
    const underCatalog = catalog.filter((o) => o.under && !o.is_accessory);
    const accessoryCatalog = catalog.filter((o) => !!o.is_accessory);

    // Searchable list section. Single shape for Outfit / Worn under /
    // Accessories. `mode`: "single" for click-to-equip, "multi" for
    // click-to-toggle (accessory list). Returns { wrap, refresh } —
    // refresh re-renders the visible rows from current pick state.
    // ---- Outfit section (primary pick — list + search, single select) ----
    const outfitSection = buildOutfitListWidget("Outfit", primaryCatalog, {
      mode: "single",
      isSelected: (id) => picks.outfits[charId] === id,
      onPick: (id) => setPrimaryOutfit(id),
      emptyText: "(no outfits available)",
    });
    block.appendChild(outfitSection.wrap);

    // ---- Per-character starting location (optional override) ----
    // Defaults to the scene-wide "Location (cast)" pick; setting it here
    // places just this NPC somewhere else. Mirrors the user block's
    // "with the cast / own location" idiom. Held and appended at the end of
    // the card so the section order reads Outfit → Clothing → Location.
    let heldLocationRow = null;
    if (locations.length) {
      const locRow = charSubRow("Location:");
      const cLocSel = document.createElement("select");
      cLocSel.className = "scenario-staging-select";
      const defOpt = document.createElement("option");
      defOpt.value = "";
      defOpt.textContent = "— scene default —";
      cLocSel.appendChild(defOpt);
      for (const l of locations) {
        const o = document.createElement("option");
        o.value = l.id;
        o.textContent = l.name || l.id;
        cLocSel.appendChild(o);
      }
      const cRoomSel = document.createElement("select");
      cRoomSel.className = "scenario-staging-select";
      const fillRooms = () => {
        cRoomSel.innerHTML = "";
        const lid = cLocSel.value;
        if (!lid) {
          const o = document.createElement("option");
          o.value = ""; o.textContent = "—";
          cRoomSel.appendChild(o);
          cRoomSel.disabled = true;
          return;
        }
        cRoomSel.disabled = false;
        const loc = locations.find((l) => l.id === lid);
        for (const r of (loc?.rooms || [])) {
          const o = document.createElement("option");
          o.value = r.id;
          o.textContent = r.name || r.id;
          cRoomSel.appendChild(o);
        }
      };
      // Reflect the shared picks state into the two selects (called on first
      // render and whenever the Locations overview moves this character).
      const syncFromPicks = () => {
        cLocSel.value = picks.castLocations[charId] || "";
        fillRooms();
        if (picks.castRooms[charId]) cRoomSel.value = picks.castRooms[charId];
      };
      syncFromPicks();
      castPlacementRefreshers.push(syncFromPicks);
      cLocSel.addEventListener("change", () =>
        setCastLocation(charId, cLocSel.value || null, null)
      );
      cRoomSel.addEventListener("change", () =>
        setCastLocation(charId, cLocSel.value || null, cRoomSel.value || null)
      );
      const cClear = document.createElement("button");
      cClear.type = "button";
      cClear.className = "scenario-staging-chip-rm";
      cClear.textContent = "−";
      cClear.title = "Clear — use the scene default location";
      cClear.addEventListener("click", () => setCastLocation(charId, null, null));
      locRow.appendChild(cLocSel);
      locRow.appendChild(cRoomSel);
      locRow.appendChild(cClear);
      locRow.classList.add("scenario-staging-cardsep");
      heldLocationRow = locRow;
    }

    // ---- Clothing row (per-slot toggles — always surfaced) ----
    const slotsRow = charSubRow("Clothing:");
    slotsRow.classList.add("scenario-staging-cardsep");
    const slotsBox = document.createElement("span");
    slotsBox.className = "scenario-staging-slots";
    slotsBox.style.display = "inline-flex";
    slotsBox.style.flexWrap = "wrap";
    slotsBox.style.gap = "4px";
    slotsBox.style.flex = "1 1 auto";
    slotsRow.appendChild(slotsBox);
    block.appendChild(slotsRow);

    const outfitSlotMap = new Map(catalog.map((o) => [o.id, o.clothing_slots || {}]));
    const outfitPartialLabelMap = new Map(catalog.map((o) => [o.id, o.partial_label || null]));
    const initialSlots = outfitSlotMap.get(picks.outfits[charId]) || char.current_slots || {};
    const initialPartialLabel = outfitPartialLabelMap.get(picks.outfits[charId]) || null;
    buildSlotButtons(charId, slotsBox, initialSlots, initialPartialLabel);

    // Clothing minus: clear per-slot tweaks back to the outfit's defaults.
    const clothingClear = document.createElement("button");
    clothingClear.type = "button";
    clothingClear.className = "scenario-staging-chip-rm";
    clothingClear.textContent = "−";
    clothingClear.title = "Clear clothing tweaks (back to the outfit's defaults)";
    clothingClear.addEventListener("click", () => {
      picks.slot_states[charId] = {};
      const oid = picks.outfits[charId];
      buildSlotButtons(
        charId, slotsBox,
        outfitSlotMap.get(oid) || {}, outfitPartialLabelMap.get(oid) || null
      );
    });
    slotsRow.appendChild(clothingClear);

    // Common toggle handler for Worn under + Accessories sections.
    const toggleLayered = (id, willBeSelected) => {
      const cur = new Set(picks.accessories[charId] || []);
      if (willBeSelected) cur.add(id);
      else cur.delete(id);
      if (cur.size) picks.accessories[charId] = Array.from(cur);
      else delete picks.accessories[charId];
      refreshColorRowVisibility();
    };

    // ---- Worn under section ----
    let underSection = null;
    if (underCatalog.length) {
      underSection = buildOutfitListWidget("Worn under", underCatalog, {
        mode: "multi",
        isSelected: (id) => (picks.accessories[charId] || []).includes(id),
        onToggle: (id, sel) => { toggleLayered(id, sel); underSection.refresh(); },
        skipId: () => picks.outfits[charId] || null,
        emptyText: "(no under-layers available)",
      });
      block.appendChild(underSection.wrap);
    }

    // ---- Accessories section ----
    let accessorySection = null;
    if (accessoryCatalog.length) {
      accessorySection = buildOutfitListWidget("Accessories", accessoryCatalog, {
        mode: "multi",
        isSelected: (id) => (picks.accessories[charId] || []).includes(id),
        onToggle: (id, sel) => { toggleLayered(id, sel); accessorySection.refresh(); },
        skipId: () => picks.outfits[charId] || null,
        emptyText: "(no accessories available)",
      });
      block.appendChild(accessorySection.wrap);
    }

    // ---- Color overlay row ----
    // Feeds the {color} placeholder via outfit_overrides; only renders
    // when at least one of the equipped outfits (primary + accessories)
    // is templated, since overriding when nothing reads the placeholder
    // would be a no-op.
    const colorRow = charSubRow("Color:");
    const colorInput = document.createElement("input");
    colorInput.type = "text";
    colorInput.className = "scenario-staging-input";
    colorInput.style.flex = "1 1 auto";
    colorInput.placeholder = "(outfit's default)";
    colorInput.value = (picks.outfit_overrides[charId] || {}).color || "";
    colorInput.addEventListener("input", () => {
      const v = colorInput.value.trim();
      if (!picks.outfit_overrides[charId]) picks.outfit_overrides[charId] = {};
      if (v) picks.outfit_overrides[charId].color = v;
      else delete picks.outfit_overrides[charId].color;
      if (!Object.keys(picks.outfit_overrides[charId]).length) {
        delete picks.outfit_overrides[charId];
      }
    });
    colorRow.appendChild(colorInput);
    block.appendChild(colorRow);

    const refreshColorRowVisibility = () => {
      const primaryId = picks.outfits[charId] || "";
      const equippedIds = new Set([primaryId, ...(picks.accessories[charId] || [])]);
      let anyTemplated = false;
      for (const o of catalog) {
        if (o.templated && equippedIds.has(o.id)) { anyTemplated = true; break; }
      }
      colorRow.hidden = !anyTemplated;
    };

    // Equip a primary outfit and resync every dependent control:
    // active-row highlight in the Outfit list, slot toggle defaults,
    // layered lists (so the newly-equipped item drops out of them),
    // and Color row visibility.
    const setPrimaryOutfit = (outfitId) => {
      picks.outfits[charId] = outfitId || null;
      outfitSection.refresh();
      // Reset slot picks so the new outfit's defaults apply.
      picks.slot_states[charId] = {};
      const slots = outfitSlotMap.get(outfitId) || {};
      const partialLabel = outfitPartialLabelMap.get(outfitId) || null;
      buildSlotButtons(charId, slotsBox, slots, partialLabel);
      // Drop the newly-picked primary from the layered set if it was
      // also checked there.
      const accList = picks.accessories[charId] || [];
      const filtered = accList.filter((a) => a !== outfitId);
      if (filtered.length !== accList.length) {
        if (filtered.length) picks.accessories[charId] = filtered;
        else delete picks.accessories[charId];
      }
      if (underSection) underSection.refresh();
      if (accessorySection) accessorySection.refresh();
      refreshColorRowVisibility();
    };

    refreshColorRowVisibility();

    // Outfit minus: reset the primary outfit to the character's default.
    const outfitLabelEl = outfitSection.wrap.querySelector(".scenario-staging-list-label");
    if (outfitLabelEl) {
      const oClear = document.createElement("button");
      oClear.type = "button";
      oClear.className = "scenario-staging-chip-rm";
      oClear.textContent = "−";
      oClear.title = "Reset to the default outfit";
      oClear.addEventListener("click", () =>
        setPrimaryOutfit(char.current_outfit || (char.outfits[0] || {}).id || null)
      );
      outfitLabelEl.appendChild(oClear);
    }

    rmBtn.addEventListener("click", () => {
      picks.characters.delete(charId);
      delete picks.outfits[charId];
      delete picks.slot_states[charId];
      delete picks.accessories[charId];
      delete picks.outfit_overrides[charId];
      delete picks.stats_edits[charId];
      delete picks.stats_removed[charId];
      delete picks.castLocations[charId];
      delete picks.castRooms[charId];
      block.remove();
      statsBlocksByChar.delete(charId);
      refreshAddDropdown();
      refreshNpcStartsDropdown();
      refreshCastPlacement();  // drop this character from the Locations overview
    });

    charList.appendChild(block);

    // -------- Life Sim stats sub-row --------
    // Always rendered into the DOM (nested INSIDE this character's
    // block now so it visually belongs to the character); visibility
    // is driven by the life_sim module checkbox via
    // refreshStatsVisibility. life_sim off = stats sub-row hidden,
    // picks.stats_edits / stats_removed not sent on Start, server-
    // side also skipped. Modules off = zero scenario change.
    const declaredStats = Array.isArray(char.stats) ? char.stats : [];
    const statsBlock = document.createElement("div");
    statsBlock.className = "scenario-staging-stats";
    statsBlock.hidden = !picks.modules.life_sim;
    statsBlock.style.marginTop = "2px";

    const statsHeader = charSubRow("Stats:");
    statsBlock.appendChild(statsHeader);

    const statsChipsRow = document.createElement("div");
    statsChipsRow.style.display = "flex";
    statsChipsRow.style.flexWrap = "wrap";
    statsChipsRow.style.alignItems = "center";
    statsChipsRow.style.gap = "6px";
    statsChipsRow.style.flex = "1 1 auto";
    statsHeader.appendChild(statsChipsRow);
    {

      const statsRoster = new Map();  // stat_id -> {label, value, min, max}
      for (const s of declaredStats) {
        statsRoster.set(s.id, {
          label: s.label || s.id,
          value: s.value,
          min: s.min,
          max: s.max,
          isNew: false,
        });
      }

      const addStatBtn = document.createElement("button");
      addStatBtn.type = "button";
      addStatBtn.className = "ghost xs";
      addStatBtn.textContent = "+ stat";
      addStatBtn.title = "Define a new stat for this branch only";
      // Append the button up-front so renderStatChip's insertBefore
      // has a valid reference node when the declared stats render.
      statsChipsRow.appendChild(addStatBtn);

      const renderStatChip = (sid, body) => {
        const chip = document.createElement("span");
        chip.className = "scenario-staging-chip scenario-staging-stat-chip";
        chip.title = `${body.label} (min ${body.min}, max ${body.max})`;

        const label = document.createElement("span");
        label.textContent = `${body.label}: `;
        chip.appendChild(label);

        const valInput = document.createElement("input");
        valInput.type = "number";
        valInput.value = body.value;
        valInput.min = body.min;
        valInput.max = body.max;
        valInput.style.width = "52px";
        valInput.addEventListener("change", () => {
          let n = parseInt(valInput.value, 10);
          if (!Number.isFinite(n)) n = body.value;
          n = Math.max(body.min, Math.min(body.max, n));
          valInput.value = n;
          body.value = n;
          // Track this as an edit only if the value differs from the
          // template default (for declared stats) or always (for new).
          const edits = (picks.stats_edits[charId] ||= {});
          if (body.isNew) {
            edits[sid] = {
              value: n,
              label: body.label,
              min: body.min,
              max: body.max,
            };
          } else {
            edits[sid] = { value: n };
          }
        });
        chip.appendChild(valInput);

        if (body.isNew) {
          const minMaxWrap = document.createElement("span");
          minMaxWrap.className = "muted small";
          minMaxWrap.style.marginLeft = "4px";
          minMaxWrap.textContent = ` / ${body.max}`;
          chip.appendChild(minMaxWrap);
        }

        // × removes the stat from this branch. For a brand-new stat
        // it's a pure UI delete (nothing to undo on the server). For
        // a declared stat we also stamp picks.stats_removed[charId]
        // so the scene-stage POST emits an [unset] edit and the new
        // branch starts without it.
        const rmStat = document.createElement("button");
        rmStat.type = "button";
        rmStat.className = "scenario-staging-chip-rm";
        rmStat.textContent = "×";
        rmStat.title = body.isNew ? "Drop this new stat" : "Drop this stat for this branch";
        rmStat.addEventListener("click", () => {
          statsRoster.delete(sid);
          const edits = picks.stats_edits[charId];
          if (edits) {
            delete edits[sid];
            if (!Object.keys(edits).length) delete picks.stats_edits[charId];
          }
          if (!body.isNew) {
            (picks.stats_removed[charId] ||= new Set()).add(sid);
          }
          chip.remove();
        });
        chip.appendChild(rmStat);

        statsChipsRow.insertBefore(chip, addStatBtn);
      };

      for (const [sid, body] of statsRoster.entries()) renderStatChip(sid, body);

      addStatBtn.addEventListener("click", () => {
        const id = prompt("Stat id (snake_case, e.g. mood):");
        if (!id) return;
        const norm = String(id).trim().toLowerCase().replace(/[^a-z0-9_]/g, "_");
        // Re-adding the same id the user just removed: clear the
        // removal flag so the unset doesn't fight the new patch.
        const removedSet = picks.stats_removed[charId];
        if (removedSet) removedSet.delete(norm);
        if (!norm || statsRoster.has(norm)) {
          flashError("Stat id must be unique and snake_case.");
          return;
        }
        const labelIn = (prompt("Display label:", norm) || norm).trim() || norm;
        const maxIn = parseInt(prompt("Max (e.g. 100):", "100") || "100", 10);
        const max = Number.isFinite(maxIn) && maxIn > 0 ? maxIn : 100;
        const valIn = parseInt(prompt(`Starting value (0..${max}):`, String(max)) || String(max), 10);
        const value = Number.isFinite(valIn) ? Math.max(0, Math.min(max, valIn)) : max;
        const body = { label: labelIn, value, min: 0, max, isNew: true };
        statsRoster.set(norm, body);
        renderStatChip(norm, body);
        // Seed the edits entry right away so even an untouched new
        // stat lands on the new branch.
        (picks.stats_edits[charId] ||= {})[norm] = {
          value, label: labelIn, min: 0, max,
        };
      });
      // (addStatBtn was appended up-front so renderStatChip's
      // insertBefore had a valid reference node.)
    }

    statsBlocksByChar.set(charId, statsBlock);
    block.appendChild(statsBlock);

    // Location last so the card reads Outfit → Clothing → Location.
    if (heldLocationRow) block.appendChild(heldLocationRow);

    refreshAddDropdown();
    refreshNpcStartsDropdown();
    refreshCastPlacement();  // update the Locations overview with this character
  };

  addBtn.addEventListener("click", () => {
    if (!addSel.value) return;
    addCharacterRow(addSel.value);
    addSel.value = "";
  });

  // -------- Cast Location section --------
  if (locations.length) {
    const locSection = (castLocSection = makeCollapsible("Location (cast)"));
    const locRow = document.createElement("div");
    locRow.className = "scenario-staging-row";
    const locLabel = document.createElement("span");
    locLabel.className = "muted small scenario-staging-label";
    locLabel.textContent = "Location:";
    locRow.appendChild(locLabel);

    const locSel = document.createElement("select");
    locSel.className = "scenario-staging-select";
    for (const loc of locations) {
      const opt = document.createElement("option");
      opt.value = loc.id;
      opt.textContent = loc.name;
      locSel.appendChild(opt);
    }
    // Honor the staging setup's `defaults.location` pre-seed (set in
    // buildSceneStagingPanel before this render runs). Without this,
    // locSel.value defaults to the first <option> and the refreshRooms
    // call below would overwrite picks.location with that.
    if (picks.location && [...locSel.options].some((o) => o.value === picks.location)) {
      locSel.value = picks.location;
    }
    locRow.appendChild(locSel);

    const roomSel = document.createElement("select");
    roomSel.className = "scenario-staging-select";
    locRow.appendChild(roomSel);

    // Description editors for the currently-selected location and
    // room. Pre-filled from the template; the user can edit to
    // override per-instance. Storage is keyed by entity id so
    // flipping the dropdown back and forth preserves any in-progress
    // edit; only entries that diverge from the template description
    // are shipped on submit. The originals map (stamped at panel
    // render time) is the diff baseline read by startSceneStaging.
    picks._loc_originals = {};
    picks._room_originals = {};
    for (const loc of locations) {
      picks._loc_originals[loc.id] = loc.description || "";
      for (const r of (loc.rooms || [])) {
        picks._room_originals[r.id] = r.description || "";
      }
    }
    const locDescLabel = document.createElement("div");
    locDescLabel.className = "muted small";
    locDescLabel.style.marginTop = "8px";
    locDescLabel.textContent = "Location description:";
    const locDescArea = document.createElement("textarea");
    locDescArea.rows = 4;
    locDescArea.style.width = "100%";
    locDescArea.placeholder = "Override the location description for this instance.";
    locDescArea.addEventListener("input", () => {
      const lid = locSel.value;
      if (lid) picks.location_descriptions[lid] = locDescArea.value;
    });

    const roomDescLabel = document.createElement("div");
    roomDescLabel.className = "muted small";
    roomDescLabel.style.marginTop = "8px";
    roomDescLabel.textContent = "Room description:";
    const roomDescArea = document.createElement("textarea");
    roomDescArea.rows = 4;
    roomDescArea.style.width = "100%";
    roomDescArea.placeholder = "Override the room description for this instance.";
    roomDescArea.addEventListener("input", () => {
      const rid = roomSel.value;
      if (!rid) return;
      // For pending new rooms, route the edit into the new_rooms
      // entry so the server stamps it as the room's initial
      // description rather than as an override patch against a
      // tmp_id that doesn't exist as an entity yet.
      const newEntry = picks.new_rooms.find((nr) => nr.tmp_id === rid);
      if (newEntry) {
        newEntry.description = roomDescArea.value;
        const loc = locations.find((l) => l.id === locSel.value);
        const r = (loc && (loc.rooms || []).find((x) => x.id === rid));
        if (r) r.description = roomDescArea.value;
        return;
      }
      picks.room_descriptions[rid] = roomDescArea.value;
    });

    const syncRoomDesc = () => {
      const loc = locations.find((l) => l.id === locSel.value);
      const room = (loc && (loc.rooms || []).find((r) => r.id === roomSel.value)) || null;
      const rid = room ? room.id : "";
      if (!rid) {
        roomDescArea.value = "";
        roomDescArea.disabled = true;
        return;
      }
      roomDescArea.disabled = false;
      roomDescArea.value = (rid in picks.room_descriptions)
        ? picks.room_descriptions[rid]
        : (room.description || "");
    };

    const syncLocDesc = () => {
      const lid = locSel.value;
      if (!lid) {
        locDescArea.value = "";
        locDescArea.disabled = true;
        return;
      }
      locDescArea.disabled = false;
      const loc = locations.find((l) => l.id === lid);
      locDescArea.value = (lid in picks.location_descriptions)
        ? picks.location_descriptions[lid]
        : ((loc && loc.description) || "");
    };

    const refreshRooms = () => {
      const loc = locations.find((l) => l.id === locSel.value);
      roomSel.innerHTML = "";
      const rooms = (loc && loc.rooms) || [];
      for (const r of rooms) {
        const opt = document.createElement("option");
        opt.value = r.id;
        // Custom rooms added in this session carry an `_isNew` flag
        // so we can tag them visually and the server can resolve the
        // tmp_id back to a fresh entity id at submission time.
        opt.textContent = r._isNew ? `${r.name} (new)` : r.name;
        roomSel.appendChild(opt);
      }
      // Preserve picks.room when it's a valid option in the new room
      // set (e.g. defaults.room seeded by buildSceneStagingPanel,
      // or the user previously picked a room and is re-rendering).
      // Otherwise fall through to the first option.
      if (picks.room && [...roomSel.options].some((o) => o.value === picks.room)) {
        roomSel.value = picks.room;
      }
      picks.location = locSel.value;
      picks.room = roomSel.value || null;
      syncLocDesc();
      syncRoomDesc();
    };
    locSel.addEventListener("change", refreshRooms);
    roomSel.addEventListener("change", () => {
      picks.room = roomSel.value || null;
      syncRoomDesc();
    });
    refreshRooms();

    // -------- "Add custom room" affordance --------
    // Mirrors the user-persona Custom… flow: a small inline form
    // captures a name + description, then the new room is stamped
    // into the room dropdown and gets created in the conversation's
    // instance dir on submit. The server assigns a real entity id;
    // until then the dropdown carries a client tmp_id.
    let newRoomCounter = 0;
    const newRoomToggleRow = document.createElement("div");
    newRoomToggleRow.style.marginTop = "8px";
    const newRoomBtn = document.createElement("button");
    newRoomBtn.type = "button";
    newRoomBtn.className = "ghost xs";
    newRoomBtn.textContent = "+ Add custom room";
    newRoomToggleRow.appendChild(newRoomBtn);

    const newRoomForm = document.createElement("div");
    newRoomForm.hidden = true;
    newRoomForm.style.display = "flex";
    newRoomForm.style.flexDirection = "column";
    newRoomForm.style.gap = "6px";
    newRoomForm.style.marginTop = "6px";
    newRoomForm.style.padding = "8px";
    newRoomForm.style.border = "1px solid var(--border)";
    newRoomForm.style.borderRadius = "var(--radius-sm)";

    const newRoomName = document.createElement("input");
    newRoomName.type = "text";
    newRoomName.placeholder = "Room name (e.g., Garden gazebo)";
    newRoomName.style.width = "100%";
    newRoomForm.appendChild(newRoomName);

    const newRoomDesc = document.createElement("textarea");
    newRoomDesc.rows = 4;
    newRoomDesc.placeholder = "Room description for the LLM (what it looks like, what's in it).";
    newRoomDesc.style.width = "100%";
    newRoomForm.appendChild(newRoomDesc);

    const newRoomActions = document.createElement("div");
    newRoomActions.style.display = "flex";
    newRoomActions.style.gap = "6px";
    newRoomActions.style.justifyContent = "flex-end";
    const newRoomCancel = document.createElement("button");
    newRoomCancel.type = "button";
    newRoomCancel.className = "ghost xs";
    newRoomCancel.textContent = "Cancel";
    const newRoomAdd = document.createElement("button");
    newRoomAdd.type = "button";
    newRoomAdd.className = "primary xs";
    newRoomAdd.textContent = "Add";
    newRoomActions.appendChild(newRoomCancel);
    newRoomActions.appendChild(newRoomAdd);
    newRoomForm.appendChild(newRoomActions);

    const closeNewRoomForm = () => {
      newRoomForm.hidden = true;
      newRoomBtn.hidden = false;
      newRoomName.value = "";
      newRoomDesc.value = "";
    };
    newRoomBtn.addEventListener("click", () => {
      newRoomForm.hidden = false;
      newRoomBtn.hidden = true;
      newRoomName.focus();
    });
    newRoomCancel.addEventListener("click", closeNewRoomForm);
    newRoomAdd.addEventListener("click", () => {
      const name = newRoomName.value.trim();
      if (!name) {
        newRoomName.focus();
        return;
      }
      const locId = locSel.value;
      const loc = locations.find((l) => l.id === locId);
      if (!loc) return;
      newRoomCounter += 1;
      const tmpId = `__new_room_${newRoomCounter}__`;
      const desc = newRoomDesc.value;
      const roomEntry = { id: tmpId, name, description: desc, _isNew: true };
      loc.rooms = loc.rooms || [];
      loc.rooms.push(roomEntry);
      picks.new_rooms.push({
        tmp_id: tmpId,
        name,
        description: desc,
        location_id: locId,
      });
      // Refresh the dropdown and auto-select the freshly-added room.
      refreshRooms();
      roomSel.value = tmpId;
      picks.room = tmpId;
      syncRoomDesc();
      closeNewRoomForm();
    });

    locSection.body.appendChild(locRow);
    locSection.body.appendChild(locDescLabel);
    locSection.body.appendChild(locDescArea);
    locSection.body.appendChild(roomDescLabel);
    locSection.body.appendChild(roomDescArea);
    locSection.body.appendChild(newRoomToggleRow);
    locSection.body.appendChild(newRoomForm);
    wrap.appendChild(locSection.section);
  }

  // -------- Locations overview (move characters between locations) --------
  // Part of the "Location (cast)" section (not a separate one): lists every
  // cast member grouped by where they start, with a dropdown + minus to move
  // each one. Stays in sync with each character's own Location picker — both
  // write picks.castLocations via setCastLocation.
  if (locations.length && castLocSection) {
    const overHead = document.createElement("div");
    overHead.className = "scenario-staging-locgroup-head muted small scenario-staging-cardsep";
    overHead.textContent = "Locations";
    castLocSection.body.appendChild(overHead);
    const overList = document.createElement("div");
    overList.className = "scenario-staging-locoverview";
    castLocSection.body.appendChild(overList);

    const renderOverview = () => {
      overList.innerHTML = "";
      const byLoc = new Map(); // location id ("" = scene default) -> [char ids]
      const ensure = (k) => {
        if (!byLoc.has(k)) byLoc.set(k, []);
        return byLoc.get(k);
      };
      ensure(""); // always show the scene-default bucket
      for (const cid of picks.characters) {
        ensure(picks.castLocations[cid] || "").push(cid);
      }
      if (picks.characters.size === 0) {
        const empty = document.createElement("div");
        empty.className = "muted small";
        empty.textContent = "(no characters in the scene yet)";
        overList.appendChild(empty);
        return;
      }
      for (const [locId, ids] of byLoc) {
        if (locId !== "" && ids.length === 0) continue; // hide empty real locations
        const grp = document.createElement("div");
        grp.className = "scenario-staging-locgroup";
        const h = document.createElement("div");
        h.className = "scenario-staging-locgroup-head muted small";
        h.textContent =
          locId === "" ? "Scene default" : locationById(locId)?.name || locId;
        grp.appendChild(h);
        if (ids.length === 0) {
          const none = document.createElement("div");
          none.className = "muted small";
          none.textContent = "(no one)";
          grp.appendChild(none);
        }
        for (const cid of ids) {
          const row = document.createElement("div");
          row.className = "scenario-staging-locchar";
          const nm = document.createElement("span");
          nm.style.flex = "1 1 auto";
          nm.textContent = charactersById.get(cid)?.name || cid;
          row.appendChild(nm);
          const moveSel = document.createElement("select");
          moveSel.className = "scenario-staging-select";
          const def = document.createElement("option");
          def.value = "";
          def.textContent = "Scene default";
          moveSel.appendChild(def);
          for (const l of locations) {
            const o = document.createElement("option");
            o.value = l.id;
            o.textContent = l.name || l.id;
            moveSel.appendChild(o);
          }
          moveSel.value = locId;
          moveSel.addEventListener("change", () =>
            setCastLocation(cid, moveSel.value || null, null)
          );
          row.appendChild(moveSel);
          // Minus: only meaningful when this character has an override —
          // clears them back to the scene default.
          if (locId !== "") {
            const rm = document.createElement("button");
            rm.type = "button";
            rm.className = "scenario-staging-chip-rm";
            rm.textContent = "−";
            rm.title = "Move back to the scene default location";
            rm.addEventListener("click", () => setCastLocation(cid, null, null));
            row.appendChild(rm);
          }
          grp.appendChild(row);
        }
        overList.appendChild(grp);
      }
    };

    renderOverview();
    castPlacementRefreshers.push(renderOverview);
  }

  // -------- User section --------
  // Four cleanly-stacked picks: Character (who you are), Persona
  // (your role / relation to the cast), Clothing (any outfit, no
  // character filter), and Location (where the user starts; can
  // differ from the cast's room). Renders inside a single
  // .scenario-staging-user block so the visual grouping is obvious.
  const personaPresets = Array.isArray(opts.user_personas) ? opts.user_personas : [];
  const personasAreRoles = !!opts.user_personas_are_roles;
  const userCards = Array.isArray(opts.user_cards) ? opts.user_cards : [];
  const userOutfits = Array.isArray(opts.user_outfits) ? opts.user_outfits : [];
  const userSection = makeCollapsible("You");
  const userBlock = userSection.body;
  userBlock.style.display = "flex";
  userBlock.style.flexDirection = "column";
  userBlock.style.gap = "6px";

  // helper: a labeled row with one control on the right.
  const userRow = (label) => {
    const row = document.createElement("div");
    row.style.display = "flex";
    row.style.alignItems = "center";
    row.style.gap = "8px";
    const lbl = document.createElement("span");
    lbl.className = "muted small";
    lbl.style.minWidth = "78px";
    lbl.textContent = label;
    row.appendChild(lbl);
    return row;
  };

  // 1. Character — base identity card (Alex, Nadia, etc.).
  const charRow = userRow("Character:");
  const charSel = document.createElement("select");
  charSel.className = "scenario-staging-select";
  charSel.style.flex = "1 1 auto";
  const charNone = document.createElement("option");
  charNone.value = "";
  charNone.textContent = "— default —";
  charSel.appendChild(charNone);
  for (const c of userCards) {
    const opt = document.createElement("option");
    opt.value = c.id;
    opt.textContent = c.name;
    if (c.description) opt.title = c.description;
    charSel.appendChild(opt);
  }
  // Pre-select: conversation's current card wins; then the staging
  // setup's `defaults.user_card_id` (already seeded into picks); then
  // the "— default —" option. The picks.user_card_id seed may have
  // been set by the pre-render defaults block in buildSceneStagingPanel.
  const currentCardId = currentUserCardId();
  const initialCardId = currentCardId || picks.user_card_id || "";
  if (initialCardId && [...charSel.options].some((o) => o.value === initialCardId)) {
    charSel.value = initialCardId;
  }
  picks.user_card_id = charSel.value || null;
  charSel.addEventListener("change", () => {
    picks.user_card_id = charSel.value || null;
  });
  charRow.appendChild(charSel);
  userBlock.appendChild(charRow);

  // 2. Persona — role label + description overlay. Preset dropdown
  // fills both fields; Custom… clears them for free-text.
  const personaRow = document.createElement("div");
  personaRow.style.display = "flex";
  personaRow.style.flexDirection = "column";
  personaRow.style.gap = "4px";

  const personaHeader = document.createElement("div");
  personaHeader.style.display = "flex";
  personaHeader.style.alignItems = "center";
  personaHeader.style.gap = "8px";
  const personaLbl = document.createElement("span");
  personaLbl.className = "muted small";
  personaLbl.style.minWidth = "78px";
  personaLbl.textContent = "Persona:";
  personaHeader.appendChild(personaLbl);

  let personaPresetSel = null;
  if (personaPresets.length) {
    personaPresetSel = document.createElement("select");
    personaPresetSel.className = "scenario-staging-select";
    personaPresetSel.style.flex = "1 1 auto";
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = "— none —";
    personaPresetSel.appendChild(placeholder);
    for (const p of personaPresets) {
      const opt = document.createElement("option");
      opt.value = p.id;
      opt.textContent = (p.label || p.name);
      personaPresetSel.appendChild(opt);
    }
    const customOpt = document.createElement("option");
    customOpt.value = "__custom__";
    customOpt.textContent = "Custom…";
    personaPresetSel.appendChild(customOpt);
    personaHeader.appendChild(personaPresetSel);
  }
  personaRow.appendChild(personaHeader);

  const personaNameInput = document.createElement("input");
  personaNameInput.type = "text";
  personaNameInput.placeholder = personasAreRoles
    ? "Role label (e.g., Federation liaison)"
    : "Name (e.g., Producer-san)";
  personaNameInput.style.width = "100%";
  personaRow.appendChild(personaNameInput);

  const personaDescArea = document.createElement("textarea");
  personaDescArea.rows = 2;
  personaDescArea.placeholder = personasAreRoles
    ? "Your job / relation to the cast in this scene."
    : "Description shown to the cast.";
  personaDescArea.style.width = "100%";
  personaRow.appendChild(personaDescArea);

  const syncPersonaPick = () => {
    const labelOrName = personaNameInput.value.trim();
    if (!labelOrName) {
      picks.user_persona = null;
      return;
    }
    if (personasAreRoles) {
      picks.user_persona = {
        role: labelOrName,
        role_description: personaDescArea.value.trim(),
        preset_id: personaPresetSel ? personaPresetSel.value || null : null,
      };
    } else {
      picks.user_persona = {
        name: labelOrName,
        description: personaDescArea.value.trim(),
        preset_id: personaPresetSel ? personaPresetSel.value || null : null,
      };
    }
  };
  // Reflect a seeded persona (e.g. scene_staging defaults.user_persona)
  // into the visible fields + dropdown so the user opens already in-role.
  if (picks.user_persona) {
    const up = picks.user_persona;
    const seededName = personasAreRoles ? up.role : up.name;
    const seededDesc = personasAreRoles ? up.role_description : up.description;
    if (seededName) personaNameInput.value = seededName;
    if (seededDesc) personaDescArea.value = seededDesc;
    if (personaPresetSel && up.preset_id) personaPresetSel.value = up.preset_id;
  }
  personaNameInput.addEventListener("input", syncPersonaPick);
  personaDescArea.addEventListener("input", syncPersonaPick);
  if (personaPresetSel) {
    personaPresetSel.addEventListener("change", () => {
      const val = personaPresetSel.value;
      if (!val || val === "__custom__") {
        if (val !== "__custom__") {
          personaNameInput.value = "";
          personaDescArea.value = "";
        }
        syncPersonaPick();
        return;
      }
      const preset = personaPresets.find((p) => p.id === val);
      if (preset) {
        personaNameInput.value = preset.name || "";
        personaDescArea.value = preset.description || "";
      }
      syncPersonaPick();
    });
  }
  userBlock.appendChild(personaRow);

  // 3. Clothing — character-agnostic outfit pool. Picks land on
  // picks.outfits.user just like before; what changed is the source
  // pool (every outfit in the library, not just the user-card's
  // owned ones).
  if (userOutfits.length) {
    // List + search, single-select — same widget shape the per-character
    // Outfit / Worn under / Accessories blocks use. The "Clear" link at
    // the top of the section expresses the "don't change" state the old
    // dropdown surfaced via its empty-string option.
    const userClothingSection = buildOutfitListWidget("Clothing", userOutfits, {
      mode: "single",
      isSelected: (id) => picks.outfits.user === id,
      onPick: (id) => { picks.outfits.user = id; userClothingSection.refresh(); },
      onClear: () => { delete picks.outfits.user; },
      clearLabel: "Don't change",
      emptyText: "(no user outfits available)",
    });
    userBlock.appendChild(userClothingSection.wrap);
  }

  // 4. Location — where the user starts. Independent of the cast's
  // room so the user can walk in from elsewhere; defaults to the
  // scene's main location/room (so an unset pick means "with the
  // cast"). Builds off the same locations[] the cast picker uses.
  if (locations.length) {
    const locRow = userRow("Location:");
    const userLocSel = document.createElement("select");
    userLocSel.className = "scenario-staging-select";
    userLocSel.style.flex = "1 1 auto";
    const userRoomSel = document.createElement("select");
    userRoomSel.className = "scenario-staging-select";
    userRoomSel.style.flex = "1 1 auto";

    const defaultOpt = document.createElement("option");
    defaultOpt.value = "";
    defaultOpt.textContent = "— with the cast —";
    userLocSel.appendChild(defaultOpt);
    for (const loc of locations) {
      const opt = document.createElement("option");
      opt.value = loc.id;
      opt.textContent = loc.name;
      userLocSel.appendChild(opt);
    }

    const refreshUserRooms = () => {
      userRoomSel.innerHTML = "";
      if (!userLocSel.value) {
        const opt = document.createElement("option");
        opt.value = "";
        opt.textContent = "—";
        userRoomSel.appendChild(opt);
        userRoomSel.disabled = true;
        picks.user_location = null;
        picks.user_room = null;
        return;
      }
      userRoomSel.disabled = false;
      const loc = locations.find((l) => l.id === userLocSel.value);
      const rooms = (loc && loc.rooms) || [];
      for (const r of rooms) {
        const opt = document.createElement("option");
        opt.value = r.id;
        opt.textContent = r.name;
        userRoomSel.appendChild(opt);
      }
      picks.user_location = userLocSel.value;
      picks.user_room = userRoomSel.value || null;
    };
    userLocSel.addEventListener("change", refreshUserRooms);
    userRoomSel.addEventListener("change", () => {
      picks.user_room = userRoomSel.value || null;
    });
    refreshUserRooms();

    locRow.appendChild(userLocSel);
    locRow.appendChild(userRoomSel);
    userBlock.appendChild(locRow);
  }

  wrap.appendChild(userSection.section);

  // -------- Prefabs --------
  // Each scenario-declared prefab contributes a section to this panel.
  // Dispatch by manifest.properties.staging_ui.kind so new kinds drop
  // in without growing the surrounding code path. Three kinds today:
  //   object_picker         — pool dropdown + chip row with optional
  //                           equip-to dropdown per chip.
  //   per_character_toggle  — one chip per picked character; clicking
  //                           toggles whether the prefab's edits apply
  //                           to that character on Start.
  //   prefab_holder         — container; renders other prefabs inside
  //                           one collapsible so the panel doesn't
  //                           sprawl. The composed children skip their
  //                           own top-level section and render inside
  //                           the holder's body instead.
  const prefabs = (opts && opts.prefabs) || {};
  // Per-prefab refresh hooks: when the picked-character set changes the
  // wrap function for refreshAddDropdown calls each of these so equip
  // selects and per-character toggle rows can re-render.
  const prefabRefreshHooks = [];

  // Render context handed to drop-in prefab renderers registered on
  // window.Prefabs. Builtin renderers use the enclosing closure
  // directly and ignore this; external kinds get everything they need
  // through it (no closure access).
  const prefabCtx = {
    picks,
    makeCollapsible,
    refreshHooks: prefabRefreshHooks,
    pickedCharacterIds: () => ["user", ...Array.from(picks.characters)],
    // Resolve a display name for a picked id (matches the Cast tab).
    characterName: (id) =>
      id === "user" ? "You" : ((charactersById.get(id) || {}).name || id),
    prefabs,
  };
  // Dispatch one prefab to its renderer: a kind registered on
  // window.Prefabs wins; otherwise fall back to the builtin renderers.
  function dispatchPrefab(pid, prefab, kind, target) {
    const ext = (window.Prefabs && window.Prefabs.getKind)
      ? window.Prefabs.getKind(kind) : null;
    if (ext) {
      try { ext(pid, prefab, target, prefabCtx); }
      catch (e) { console.warn("prefab kind", kind, "render failed", e); }
      return;
    }
    if (kind === "object_picker") renderObjectPickerPrefab(pid, prefab, target);
    else if (kind === "per_character_toggle") renderPerCharacterTogglePrefab(pid, prefab, target);
    else if (kind === "scenario_freeform_text") renderScenarioFreeformTextPrefab(pid, prefab, target);
    else if (kind === "prefab_holder") renderPrefabHolder(pid, prefab, target);
    // Unknown + unregistered kinds are silently skipped.
  }

  // First pass — find any prefab_holder prefabs and reserve their
  // composed children. Reserved ids skip top-level rendering and get
  // rendered inside the holder's collapsible body instead.
  const consumedByHolder = new Set();
  for (const [pid, prefab] of Object.entries(prefabs)) {
    if (!prefab) continue;
    const props = (prefab.manifest || {}).properties || {};
    const kind = prefab.kind || (props.staging_ui && props.staging_ui.kind) || "";
    if (kind === "prefab_holder") {
      const composes = props.composes || [];
      for (const cid of composes) {
        if (typeof cid === "string") consumedByHolder.add(cid);
      }
    }
  }

  for (const [pid, prefab] of Object.entries(prefabs)) {
    if (!prefab) continue;
    if (consumedByHolder.has(pid)) continue;  // rendered inside a holder
    const manifest = prefab.manifest || {};
    const props = manifest.properties || {};
    const kind = prefab.kind || (props.staging_ui && props.staging_ui.kind) || "";
    dispatchPrefab(pid, prefab, kind, wrap);
  }

  function renderPrefabHolder(pid, prefab, target) {
    const props = (prefab.manifest || {}).properties || {};
    const ui = (props.staging_ui) || {};
    const composes = props.composes || [];
    const title = (prefab.manifest && prefab.manifest.name) || pid;
    const collapsible = makeCollapsible(title, { open: !ui.default_collapsed });
    const body = collapsible.body;
    body.style.display = "flex";
    body.style.flexDirection = "column";
    body.style.gap = "6px";

    if (prefab.manifest && prefab.manifest.description) {
      const hint = document.createElement("div");
      hint.className = "muted small";
      hint.textContent = prefab.manifest.description;
      body.appendChild(hint);
    }

    // Render each composed child INTO the holder's body. The child's
    // own collapsible chevron stays — gives the user nested fold
    // control inside the box.
    for (const childId of composes) {
      const child = prefabs[childId];
      if (!child) continue;
      const childProps = (child.manifest || {}).properties || {};
      const childKind = child.kind || (childProps.staging_ui && childProps.staging_ui.kind) || "";
      // Nested holders render inline too — no recursion limit because
      // consumedByHolder is scoped to top-level dispatch, not this walk.
      dispatchPrefab(childId, child, childKind, body);
    }

    target.appendChild(collapsible.section);
  }

  function renderObjectPickerPrefab(pid, prefab, target) {
    if (!Array.isArray(prefab.pool) || !prefab.pool.length) return;
    const cfg = prefab.config || {};
    const allowEquip = cfg.allow_equip !== false;
    const defaultEquipTarget = (typeof cfg.default_equip_target === "string")
      ? cfg.default_equip_target : null;
    const poolById = new Map(prefab.pool.map((o) => [o.id, o]));
    // Per-prefab pick state lives under picks.prefabs[pid] so multiple
    // object_picker prefabs (e.g. scenario-declared + generic) coexist
    // cleanly without colliding on a shared `objects`/`equipped`.
    const state_ = picks.prefabs[pid] = { objects: new Set(), equipped: {} };

    const title = (prefab.manifest && prefab.manifest.name) || "Objects";
    const objectsCollapsible = makeCollapsible(title);
    const section = objectsCollapsible.body;
    section.style.display = "flex";
    section.style.flexDirection = "column";
    section.style.gap = "6px";

    const addRow = document.createElement("div");
    addRow.className = "scenario-staging-row";
    const addLabel = document.createElement("span");
    addLabel.className = "muted small scenario-staging-label";
    addLabel.textContent = "Add object:";
    addRow.appendChild(addLabel);

    const addSel = document.createElement("select");
    addSel.className = "scenario-staging-select";
    addRow.appendChild(addSel);

    const addBtn = document.createElement("button");
    addBtn.type = "button";
    addBtn.className = "ghost xs";
    addBtn.textContent = "Add";
    addRow.appendChild(addBtn);
    section.appendChild(addRow);

    const chipsRow = document.createElement("div");
    chipsRow.className = "scenario-staging-row";
    chipsRow.style.flexWrap = "wrap";
    const chipsLabel = document.createElement("span");
    chipsLabel.className = "muted small scenario-staging-label";
    chipsLabel.textContent = "In scene:";
    chipsRow.appendChild(chipsLabel);
    section.appendChild(chipsRow);

    const refreshAddSelect = () => {
      addSel.innerHTML = "";
      const remaining = prefab.pool.filter((o) => !state_.objects.has(o.id));
      if (!remaining.length) {
        const opt = document.createElement("option");
        opt.value = "";
        opt.textContent = "— all picked —";
        addSel.appendChild(opt);
        addSel.disabled = true;
        addBtn.disabled = true;
        return;
      }
      addSel.disabled = false;
      addBtn.disabled = false;
      const placeholder = document.createElement("option");
      placeholder.value = "";
      placeholder.textContent = "— pick an object —";
      addSel.appendChild(placeholder);
      for (const obj of remaining) {
        const opt = document.createElement("option");
        opt.value = obj.id;
        opt.textContent = obj.name;
        if (obj.description) opt.title = obj.description;
        addSel.appendChild(opt);
      }
    };

    const buildEquipOptions = (sel, currentValue) => {
      sel.innerHTML = "";
      const none = document.createElement("option");
      none.value = "";
      none.textContent = "in room";
      sel.appendChild(none);
      const pickedIds = ["user", ...Array.from(picks.characters)];
      for (const cid of pickedIds) {
        const ent = state.entities[cid];
        if (!ent && cid !== "user") continue;
        const opt = document.createElement("option");
        opt.value = cid;
        opt.textContent = (cid === "user") ? "User" : ((ent && ent.name) || cid);
        sel.appendChild(opt);
      }
      if (currentValue && [...sel.options].some((o) => o.value === currentValue)) {
        sel.value = currentValue;
      }
    };

    const equipChipSelects = [];

    const removeObjectChip = (objectId, chip) => {
      state_.objects.delete(objectId);
      delete state_.equipped[objectId];
      if (chip && chip.parentNode) chip.remove();
      const idx = equipChipSelects.findIndex((e) => e.object_id === objectId);
      if (idx >= 0) equipChipSelects.splice(idx, 1);
      refreshAddSelect();
    };

    const addObjectChip = (objectId) => {
      const obj = poolById.get(objectId);
      if (!obj) return;
      state_.objects.add(objectId);

      const chip = document.createElement("span");
      chip.className = "scenario-staging-chip";
      chip.title = obj.description || "";

      const name = document.createElement("strong");
      name.textContent = obj.name;
      chip.appendChild(name);

      if (allowEquip) {
        const sep = document.createElement("span");
        sep.className = "muted small";
        sep.textContent = " · ";
        chip.appendChild(sep);
        const equipSel = document.createElement("select");
        equipSel.className = "scenario-staging-chip-equip";
        let initial = "";
        if (defaultEquipTarget) {
          const pickedIds = new Set(["user", ...Array.from(picks.characters)]);
          if (pickedIds.has(defaultEquipTarget)) initial = defaultEquipTarget;
        }
        buildEquipOptions(equipSel, initial);
        if (equipSel.value) state_.equipped[objectId] = equipSel.value;
        equipSel.addEventListener("change", () => {
          const v = equipSel.value;
          if (v) state_.equipped[objectId] = v;
          else delete state_.equipped[objectId];
        });
        chip.appendChild(equipSel);
        equipChipSelects.push({ object_id: objectId, sel: equipSel });
      }

      const x = document.createElement("button");
      x.type = "button";
      x.className = "scenario-staging-chip-rm";
      x.textContent = "×";
      x.title = "Remove from scene";
      x.addEventListener("click", () => removeObjectChip(objectId, chip));
      chip.appendChild(x);

      chipsRow.appendChild(chip);
      refreshAddSelect();
    };

    addBtn.addEventListener("click", () => {
      const id = addSel.value;
      if (!id) return;
      addObjectChip(id);
    });

    prefabRefreshHooks.push(() => {
      for (const { object_id, sel } of equipChipSelects) {
        buildEquipOptions(sel, state_.equipped[object_id] || "");
        if (state_.equipped[object_id] && ![...sel.options].some((o) => o.value === state_.equipped[object_id])) {
          delete state_.equipped[object_id];
        }
      }
    });

    refreshAddSelect();
    target.appendChild(objectsCollapsible.section);
  }

  function renderPerCharacterTogglePrefab(pid, prefab, target) {
    const ui = prefab.ui || {};
    const label = ui.label || (prefab.manifest && prefab.manifest.name) || pid;
    const tooltip = ui.tooltip || "";
    const includeUser = ui.include_user !== false;  // default include user
    const state_ = picks.prefabs[pid] = { characters: new Set() };

    const title = (prefab.manifest && prefab.manifest.name) || label;
    const collapsible = makeCollapsible(title);
    const section = collapsible.body;
    section.style.display = "flex";
    section.style.flexDirection = "column";
    section.style.gap = "6px";

    if (prefab.manifest && prefab.manifest.description) {
      const hint = document.createElement("div");
      hint.className = "muted small";
      hint.textContent = prefab.manifest.description;
      section.appendChild(hint);
    }

    const row = document.createElement("div");
    row.className = "scenario-staging-row";
    row.style.flexWrap = "wrap";
    const rowLabel = document.createElement("span");
    rowLabel.className = "muted small scenario-staging-label";
    rowLabel.textContent = `${label}:`;
    row.appendChild(rowLabel);
    section.appendChild(row);

    const renderChips = () => {
      // Clear chips but keep the label at the front.
      while (row.children.length > 1) row.removeChild(row.lastChild);
      const ids = [...Array.from(picks.characters)];
      if (includeUser) ids.unshift("user");
      if (!ids.length) {
        const empty = document.createElement("span");
        empty.className = "muted small";
        empty.textContent = "— no characters in cast —";
        row.appendChild(empty);
        return;
      }
      for (const cid of ids) {
        const ent = state.entities[cid];
        const displayName = (cid === "user") ? "User" : ((ent && ent.name) || cid);
        const chip = document.createElement("button");
        chip.type = "button";
        chip.className = "scenario-staging-chip scenario-staging-chip-toggle";
        chip.textContent = displayName;
        if (tooltip) chip.title = tooltip;
        const on = state_.characters.has(cid);
        chip.dataset.active = on ? "1" : "0";
        chip.style.opacity = on ? "1" : "0.55";
        chip.style.cursor = "pointer";
        chip.addEventListener("click", () => {
          if (state_.characters.has(cid)) {
            state_.characters.delete(cid);
          } else {
            state_.characters.add(cid);
          }
          renderChips();
        });
        row.appendChild(chip);
      }
    };

    renderChips();
    prefabRefreshHooks.push(() => {
      // Prune toggled-off characters who are no longer in the cast.
      for (const cid of Array.from(state_.characters)) {
        if (cid === "user" && includeUser) continue;
        if (!picks.characters.has(cid)) state_.characters.delete(cid);
      }
      renderChips();
    });

    target.appendChild(collapsible.section);
  }

  function renderScenarioFreeformTextPrefab(pid, prefab, target) {
    const ui = prefab.ui || {};
    const label = ui.label || (prefab.manifest && prefab.manifest.name) || pid;
    const placeholder = ui.placeholder || "";
    const rows = Number(ui.rows) || 4;
    const defaultText = ui.default_text || "";
    const appliesTo = ui.applies_to || "all_picked_characters";
    const showShared = appliesTo !== "per_character";
    const showPer = appliesTo === "per_character" || appliesTo === "shared_and_per_character";
    // text = cast-wide note; texts = { charId: per-character note }.
    const state_ = picks.prefabs[pid] = { text: defaultText, texts: {} };

    const title = (prefab.manifest && prefab.manifest.name) || label;
    const collapsible = makeCollapsible(title);
    const body = collapsible.body;
    body.style.display = "flex";
    body.style.flexDirection = "column";
    body.style.gap = "6px";

    if (prefab.manifest && prefab.manifest.description) {
      const hint = document.createElement("div");
      hint.className = "muted small";
      hint.textContent = prefab.manifest.description;
      body.appendChild(hint);
    }

    if (showShared) {
      const area = document.createElement("textarea");
      area.rows = rows;
      area.style.width = "100%";
      if (placeholder) area.placeholder = placeholder;
      area.value = defaultText;
      area.addEventListener("input", () => { state_.text = area.value; });
      body.appendChild(area);
    }

    // Per-character sub-panel: a note that applies to just one NPC,
    // appended below the cast-wide note for that character.
    if (showPer) {
      const sub = makeCollapsible(ui.per_character_label || "Per-character notes", { open: false });
      sub.body.style.display = "flex";
      sub.body.style.flexDirection = "column";
      sub.body.style.gap = "8px";
      const perPlaceholder = ui.per_character_placeholder || "";

      const renderPerRows = () => {
        const cast = Array.from(picks.characters); // NPCs only (no user)
        for (const cid of Object.keys(state_.texts)) {
          if (!picks.characters.has(cid)) delete state_.texts[cid];
        }
        sub.body.innerHTML = "";
        if (!cast.length) {
          const empty = document.createElement("div");
          empty.className = "muted small";
          empty.textContent = "Add characters above to give them individual notes.";
          sub.body.appendChild(empty);
          return;
        }
        for (const cid of cast) {
          const wrap = document.createElement("div");
          wrap.className = "scenario-staging-list-section";
          const lab = document.createElement("span");
          lab.className = "scenario-staging-list-label";
          lab.textContent = cid;
          wrap.appendChild(lab);
          const ta = document.createElement("textarea");
          ta.rows = 2;
          ta.style.width = "100%";
          if (perPlaceholder) ta.placeholder = perPlaceholder;
          ta.value = state_.texts[cid] || "";
          ta.addEventListener("input", () => {
            if (ta.value.trim()) state_.texts[cid] = ta.value;
            else delete state_.texts[cid];
          });
          wrap.appendChild(ta);
          sub.body.appendChild(wrap);
        }
      };
      renderPerRows();
      prefabRefreshHooks.push(renderPerRows);
      body.appendChild(sub.section);
    }

    target.appendChild(collapsible.section);
  }

  // Wrap the character-pool refresh so every prefab's per-character
  // dropdowns / chip rows re-render when the cast changes.
  if (prefabRefreshHooks.length) {
    const _origRefresh = refreshAddDropdown;
    refreshAddDropdown = function() {
      _origRefresh();
      for (const fn of prefabRefreshHooks) {
        try { fn(); } catch (e) { /* keep panel alive on one prefab's failure */ }
      }
    };
  }

  // -------- Prompt sections (4 textareas) --------
  // Each section maps to a backend field; the panel just collects
  // text and forwards on Start. Labels match the user's mental
  // model: scenario instructions (the base rules), setup append
  // (per-stage tweak that gets concatenated), narrator edits (free
  // text → narrator model emits state edits), location prompt
  // (free text → narrator model emits the opening narrator prose).
  // Scene text textareas: each gets its own collapsible. Authors can
  // fold the noisy "Narrator edits" / "Location prompt" textareas
  // away while keeping "Scenario instructions" open, etc.
  function addTextSection(label, key, opts) {
    opts = opts || {};
    const sec = makeCollapsible(label, { open: opts.open !== false });
    const area = document.createElement("textarea");
    area.rows = opts.rows || 3;
    area.style.width = "100%";
    if (opts.mono) area.style.fontFamily = "monospace";
    if (opts.placeholder) area.placeholder = opts.placeholder;
    if (opts.value != null) area.value = opts.value;
    area.addEventListener("input", () => { picks[key] = area.value; });
    sec.body.appendChild(area);
    wrap.appendChild(sec.section);
    return area;
  }

  // 1. Scenario instructions — pre-fill from scenario root so the
  //    user edits the base rather than retyping.
  const baseInst = (opts.scenario_instructions || "").toString();
  picks.scenario_instructions = baseInst;
  addTextSection("Scenario instructions:", "scenario_instructions", {
    rows: 4,
    value: baseInst,
    placeholder: "Base scenario_instructions inherited from the scenario.",
  });

  // 2. Setup append — empty by default.
  addTextSection("Setup append:", "setup_append", {
    rows: 3,
    placeholder: "Extra scenario_instructions for this staged scene only.",
  });

  // 3. Narrator edits — free text routed through the narrator model;
  //    emitted edits modify character defs before the scene starts.
  addTextSection("Narrator edits:", "narrator_edits", {
    rows: 3,
    placeholder: 'Nadia has a fresh coffee stain on her sleeve.',
  });

  // 4. Location prompt — free text routed through the narrator;
  //    the rewritten body becomes the opening narrator prose.
  addTextSection("Location prompt:", "location_prompt", {
    rows: 4,
    placeholder: "Nadia in the reading nook, curled in an armchair, casual clothes…",
  });

  // -------- Modules --------
  // Scenario-declared `available_modules` rendered as a multi-select
  // with per-module auto-generated settings form. Each manifest's
  // `settings` schema drives the controls; live_editable settings get
  // an immediate toggle/input in the panel. The user's picks ride to
  // the new branch's root metadata via the scene-stage POST.
  const moduleManifests = Array.isArray(opts.modules) ? opts.modules : [];
  const defaultModules = Array.isArray(opts.default_modules) ? opts.default_modules : [];
  if (moduleManifests.length) {
    const modulesSection = makeCollapsible("Modules");
    const modulesWrap = modulesSection.body;
    modulesWrap.style.display = "flex";
    modulesWrap.style.flexDirection = "column";
    modulesWrap.style.gap = "6px";

    for (const manifest of moduleManifests) {
      const mid = manifest.id;
      if (!mid) continue;
      const isDefault = defaultModules.includes(mid);
      picks.modules[mid] = isDefault;
      // Seed settings with manifest defaults so the POST always sends
      // a complete settings object even if the user touches nothing.
      const seed = {};
      for (const s of manifest.settings || []) {
        if (s && s.id) seed[s.id] = s.default;
      }
      picks.module_settings[mid] = seed;

      const card = document.createElement("div");
      card.className = "scene-staging-module";
      card.style.border = "1px solid var(--border, #444)";
      card.style.borderRadius = "6px";
      card.style.padding = "6px 8px";
      card.style.marginTop = "6px";

      const headRow = document.createElement("label");
      headRow.style.display = "flex";
      headRow.style.gap = "8px";
      headRow.style.alignItems = "baseline";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = isDefault;
      headRow.appendChild(cb);
      const nameSpan = document.createElement("strong");
      nameSpan.textContent = manifest.name || mid;
      headRow.appendChild(nameSpan);
      if (manifest.description) {
        const desc = document.createElement("span");
        desc.className = "muted small";
        desc.style.marginLeft = "auto";
        desc.textContent = manifest.description;
        headRow.appendChild(desc);
      }
      card.appendChild(headRow);

      const settingsBlock = document.createElement("div");
      settingsBlock.style.display = isDefault ? "flex" : "none";
      settingsBlock.style.flexDirection = "column";
      settingsBlock.style.gap = "4px";
      settingsBlock.style.marginTop = "6px";
      settingsBlock.style.paddingLeft = "20px";

      for (const s of manifest.settings || []) {
        if (!s || !s.id) continue;
        const row = renderModuleSettingControl(manifest, s, picks.module_settings[mid], charactersById);
        if (row) settingsBlock.appendChild(row);
      }
      card.appendChild(settingsBlock);

      cb.addEventListener("change", () => {
        picks.modules[mid] = cb.checked;
        settingsBlock.style.display = cb.checked ? "flex" : "none";
        // Life Sim controls the stats sub-row per character — keep
        // them in lockstep with the module checkbox.
        if (mid === "life_sim") refreshStatsVisibility();
      });

      modulesWrap.appendChild(card);
    }
    wrap.appendChild(modulesSection.section);
  }

  // -------- Start section: NPC-starter pick + Start buttons --------
  const startSection = makeCollapsible("Start");

  const npcRow = document.createElement("div");
  npcRow.className = "scenario-staging-row";
  const npcLabel = document.createElement("span");
  npcLabel.className = "muted small scenario-staging-label";
  npcLabel.textContent = "Who starts:";
  npcRow.appendChild(npcLabel);
  const npcSel = document.createElement("select");
  npcSel.className = "scenario-staging-select";
  npcRow.appendChild(npcSel);
  startSection.body.appendChild(npcRow);

  function refreshNpcStartsDropdown() {
    npcSel.innerHTML = "";
    const picked = Array.from(picks.characters);
    if (!picked.length) {
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = "— add a character first —";
      npcSel.appendChild(opt);
      npcSel.disabled = true;
      picks.npc_starter = null;
      return;
    }
    npcSel.disabled = false;
    for (const cid of picked) {
      const c = charactersById.get(cid);
      const opt = document.createElement("option");
      opt.value = cid;
      opt.textContent = (c && c.name) || cid;
      npcSel.appendChild(opt);
    }
    picks.npc_starter = npcSel.value || picked[0];
  }
  npcSel.addEventListener("change", () => { picks.npc_starter = npcSel.value || null; });
  refreshNpcStartsDropdown();

  // Start buttons live inside the same "Start" collapsible.
  const startRow = document.createElement("div");
  startRow.className = "scenario-staging-row scenario-staging-actions";

  const npcStartBtn = document.createElement("button");
  npcStartBtn.type = "button";
  npcStartBtn.className = "ghost";
  npcStartBtn.textContent = "NPC starts ▸";
  npcStartBtn.title = "Spawn a new branch and have the picked NPC write the first turn";
  npcStartBtn.addEventListener("click", () => startSceneStaging(msg, picks, npcStartBtn, { npcStarts: true }));
  startRow.appendChild(npcStartBtn);

  const startBtn = document.createElement("button");
  startBtn.type = "button";
  startBtn.className = "primary";
  startBtn.textContent = "Start ▸";
  startBtn.title = "Spawn a new branch with the picked scene; the user takes the next turn";
  startBtn.addEventListener("click", () => startSceneStaging(msg, picks, startBtn, { npcStarts: false }));
  startRow.appendChild(startBtn);

  startSection.body.appendChild(startRow);
  wrap.appendChild(startSection.section);

  refreshAddDropdown();

  // Apply scene_staging_fields.defaults.characters AFTER every panel
  // section is rendered. addCharacterRow's tail calls into
  // refreshNpcStartsDropdown, which closes over `npcSel` (created in
  // the Start section a few dozen lines up); pre-adding chars before
  // that section runs hits a temporal-dead-zone ReferenceError that
  // bails the whole render after the first defaults char lands.
  //
  // The pre-add routes through addCharacterRow (not a direct
  // picks.characters.add) so the row materialises with its outfit
  // widget, slot states, and accessory mounts. The user can still
  // un-check / remove them — these are pre-selections, not
  // enforcement. Outfit override: defaults.outfits[<cid>] wins over
  // the char's current_outfit / first-outfit fallback.
  const defaultCharIds = Array.isArray((opts.defaults || {}).characters)
    ? opts.defaults.characters : [];
  const defaultOutfits = (opts.defaults || {}).outfits || {};
  for (const cid of defaultCharIds) {
    addCharacterRow(cid, defaultOutfits[cid] || null);
  }
}

async function startSceneStaging(msg, picks, btn, { npcStarts = false } = {}) {
  if (picks.characters.size === 0) {
    flashError("Pick at least one character.");
    return;
  }
  if (!picks.location || !picks.room) {
    flashError("Pick a location and a room.");
    return;
  }
  const setupId = (msg.metadata && msg.metadata.setup && msg.metadata.setup.id) || null;
  if (!setupId) {
    flashError("Staging root has no setup id.");
    return;
  }
  if (npcStarts && !picks.npc_starter) {
    flashError("Pick who starts.");
    return;
  }
  if (btn) btn.disabled = true;
  // Pre-stash the NPC-starter so the post-reload page handler picks
  // it up and fires streamGenerate against the new sibling root.
  // sessionStorage key matches the trap staging convention.
  if (npcStarts) {
    sessionStorage.setItem(
      `pending_npc_first_turn:${conversationId}`,
      picks.npc_starter,
    );
  }
  try {
    // Flatten module activation map -> list of active ids; ship only
    // settings for active modules so the server's validator doesn't
    // have to filter twice.
    const activeModules = Object.keys(picks.modules || {}).filter((m) => picks.modules[m]);
    const moduleSettings = {};
    for (const mid of activeModules) {
      moduleSettings[mid] = picks.module_settings[mid] || {};
    }
    await jfetch(`/api/conversations/${conversationId}/scenario-prep/scene-stage`, {
      method: "POST",
      body: JSON.stringify({
        setup_id: setupId,
        characters: Array.from(picks.characters),
        outfits: { ...picks.outfits },
        slot_states: { ...picks.slot_states },
        accessories: { ...picks.accessories },
        outfit_overrides: { ...picks.outfit_overrides },
        location: picks.location,
        room: picks.room,
        // Per-NPC placement overrides (char_id → location/room id). The server
        // falls back to the batch location/room above for any character absent
        // from these maps, so omitting them reproduces the old batch behaviour.
        cast_locations: { ...picks.castLocations },
        cast_rooms: { ...picks.castRooms },
        // Only send description overrides that diverge from the
        // template baseline captured at panel render. Empty objects
        // are fine; the server treats missing/empty as "no overrides".
        location_descriptions: (function () {
          const out = {};
          const orig = picks._loc_originals || {};
          for (const [id, val] of Object.entries(picks.location_descriptions || {})) {
            if (typeof val !== "string") continue;
            if (val !== (orig[id] || "")) out[id] = val;
          }
          return out;
        })(),
        room_descriptions: (function () {
          const out = {};
          const orig = picks._room_originals || {};
          for (const [id, val] of Object.entries(picks.room_descriptions || {})) {
            if (typeof val !== "string") continue;
            if (val !== (orig[id] || "")) out[id] = val;
          }
          return out;
        })(),
        // New rooms created via the "+ Add custom room" form. Server
        // generates a real entity id per entry, writes the room into
        // the conversation's instance dir, and remaps any tmp_id
        // referenced by `room` (cast pick) / `user_room` to the real
        // id before emitting [move] edits.
        new_rooms: (picks.new_rooms || []).map((nr) => ({
          tmp_id: nr.tmp_id,
          name: nr.name,
          description: nr.description,
          location_id: nr.location_id,
        })),
        prompt: picks.prompt || "",
        scenario_instructions: picks.scenario_instructions == null
          ? null : picks.scenario_instructions,
        setup_append: picks.setup_append || "",
        narrator_edits: picks.narrator_edits || "",
        location_prompt: picks.location_prompt || "",
        user_persona: picks.user_persona || null,
        modules: activeModules,
        module_settings: moduleSettings,
        // Prefabs: per-prefab pick payload keyed by prefab id. The
        // backend dispatches each one by manifest.staging_ui.kind.
        // Object-picker entries carry { objects, equipped }; per-
        // character toggles carry { characters }. The shape below
        // normalizes Sets → Arrays so JSON.stringify ships them.
        prefabs: (function () {
          const out = {};
          for (const [pid, pf] of Object.entries(picks.prefabs || {})) {
            if (!pf) continue;
            if (pf.objects instanceof Set) {
              out[pid] = {
                objects: Array.from(pf.objects),
                equipped: { ...(pf.equipped || {}) },
              };
            } else if (pf.characters instanceof Set) {
              out[pid] = { characters: Array.from(pf.characters) };
            } else if (typeof pf.text === "string") {
              out[pid] = { text: pf.text };
              if (pf.texts && typeof pf.texts === "object" && Object.keys(pf.texts).length) {
                out[pid].texts = { ...pf.texts };
              }
            } else {
              // Generic drop-in kinds: ship their pick state as-is,
              // recursively converting any nested Sets to Arrays so
              // JSON.stringify handles them. Lets a new kind store an
              // arbitrary JSON-serialisable shape (e.g. { selections,
              // texts }) without the engine knowing its fields.
              out[pid] = (function normalize(v) {
                if (v instanceof Set) return Array.from(v).map(normalize);
                if (Array.isArray(v)) return v.map(normalize);
                if (v && typeof v === "object") {
                  const o = {};
                  for (const [k, val] of Object.entries(v)) o[k] = normalize(val);
                  return o;
                }
                return v;
              })(pf);
            }
          }
          return out;
        })(),
        // Legacy top-level aliases for the `objects` prefab — kept so
        // a stale server build that hasn't picked up the dispatcher
        // still works for the canonical scenario-declared pool case.
        objects: (function () {
          const pf = (picks.prefabs && picks.prefabs.objects) || null;
          return pf && pf.objects instanceof Set ? Array.from(pf.objects) : [];
        })(),
        equipped: (function () {
          const pf = (picks.prefabs && picks.prefabs.objects) || null;
          return pf && pf.equipped ? { ...pf.equipped } : {};
        })(),
        // Stats payload only ships when life_sim is checked in the
        // modules picker. Server gates on the same condition (modules
        // off should change nothing); the client drops here too so
        // an inert stats_edits dict doesn't even hit the wire.
        stats_edits: picks.modules.life_sim ? { ...picks.stats_edits } : {},
        stats_removed: picks.modules.life_sim
          ? Object.fromEntries(
              Object.entries(picks.stats_removed)
                .map(([cid, s]) => [cid, Array.from(s)])
                .filter(([, arr]) => arr.length),
            )
          : {},
        // User-section picks: base Character card, optional separate
        // Location/Room for the user. Persona overlay rides in
        // user_persona; clothing rides in outfits.user.
        user_card_id: picks.user_card_id || null,
        user_location: picks.user_location || null,
        user_room: picks.user_room || null,
      }),
    });
  } catch (e) {
    flashError("Start failed: " + e.message);
    if (btn) btn.disabled = false;
    if (npcStarts) sessionStorage.removeItem(`pending_npc_first_turn:${conversationId}`);
    return;
  }
  // Reload to pick up the new sibling root as active.
  window.location.reload();
}

registerAttachment({
  id: "scene_staging",
  slot: "below-body",
  order: 30,
  // Always show on the staging root regardless of children — each
  // Start spawns a new sibling branch chain off this root, so keeping
  // the panel visible lets the user re-stage without having to dig
  // through the message tree.
  show: (msg) => {
    const meta = msg.metadata || {};
    return !!(meta.opening && meta.scene_staging && meta.setup);
  },
  render: buildSceneStagingPanel,
});

// ---------------------------------------------------------------------------
// Scene staging origin banner
//
// Passive confirmation: a new sibling root spawned by Scene staging
// carries metadata.scene_staging_origin = true and metadata.applied_edits.
// Render a banner under the root listing what was applied, with an
// Undo button (deletes this root + navigates back to the source
// staging root) and a Dismiss button (frontend-only acknowledgement
// stored in sessionStorage so reloads remember the choice).
// ---------------------------------------------------------------------------

function sceneStagingBannerDismissed(rootId) {
  try {
    return sessionStorage.getItem(`scene_staging_banner_dismissed:${rootId}`) === "1";
  } catch (_) {
    return false;
  }
}

function dismissSceneStagingBanner(rootId) {
  try {
    sessionStorage.setItem(`scene_staging_banner_dismissed:${rootId}`, "1");
  } catch (_) { /* ignore */ }
}

function buildSceneStagingBanner(msg) {
  const meta = msg.metadata || {};
  const edits = Array.isArray(meta.applied_edits) ? meta.applied_edits : [];
  const sourceSetupId = meta.scene_staging_source_setup_id || null;

  const wrap = document.createElement("div");
  wrap.className = "scene-staging-banner";
  wrap.style.padding = "8px 10px";
  wrap.style.border = "1px solid var(--border, #444)";
  wrap.style.borderRadius = "6px";
  wrap.style.margin = "6px 0";
  wrap.style.background = "var(--surface-2, rgba(255,255,255,0.04))";

  const title = document.createElement("div");
  title.className = "muted small";
  title.style.marginBottom = "4px";
  title.textContent = `Scene staging applied ${edits.length} edit${edits.length === 1 ? "" : "s"}.`;
  wrap.appendChild(title);

  if (edits.length) {
    const list = document.createElement("ul");
    list.style.margin = "4px 0 8px";
    list.style.paddingLeft = "18px";
    list.style.fontFamily = "monospace";
    list.style.fontSize = "0.85em";
    for (const e of edits) {
      const li = document.createElement("li");
      li.textContent = formatSceneStagingEdit(e);
      list.appendChild(li);
    }
    wrap.appendChild(list);
  }

  const actions = document.createElement("div");
  actions.style.display = "flex";
  actions.style.gap = "6px";

  const dismissBtn = document.createElement("button");
  dismissBtn.type = "button";
  dismissBtn.className = "ghost xs";
  dismissBtn.textContent = "Dismiss";
  dismissBtn.addEventListener("click", () => {
    dismissSceneStagingBanner(msg.id);
    rerenderMessage(msg);
  });
  actions.appendChild(dismissBtn);

  const undoBtn = document.createElement("button");
  undoBtn.type = "button";
  undoBtn.className = "ghost xs";
  undoBtn.textContent = "Undo";
  undoBtn.title = "Delete this branch and go back to the staging root";
  undoBtn.addEventListener("click", () => undoSceneStaging(msg, sourceSetupId, undoBtn));
  actions.appendChild(undoBtn);

  wrap.appendChild(actions);
  return wrap;
}

function formatSceneStagingEdit(e) {
  if (!e || typeof e !== "object") return String(e);
  switch (e.kind) {
    case "move": {
      const loc = e.location ? `${e.location}:${e.room}` : e.room;
      return `[move ${e.character_id} -> ${loc}]`;
    }
    case "outfit":
      return `[outfit ${e.character_id} -> ${e.outfit_id}]`;
    case "patch": {
      const dataStr = JSON.stringify(e.data);
      return `[set ${e.id} ← ${dataStr}]`;
    }
    case "unset":
      return `[unset ${e.id}.${(e.path || []).join(".")}]`;
    case "replace":
      return `[replace ${e.id}]`;
    default:
      return JSON.stringify(e);
  }
}

async function undoSceneStaging(rootMsg, sourceSetupId, btn) {
  const ok = await confirmAction("Undo this staged branch? It will be deleted.");
  if (!ok) return;
  if (btn) btn.disabled = true;
  // Find the source staging root so we can hop back to it after delete.
  let targetLeaf = null;
  for (const m of Object.values(state.conversation.messages || {})) {
    const meta = m.metadata || {};
    if (meta.scene_staging && !m.parent_id && meta.setup && meta.setup.id === sourceSetupId) {
      targetLeaf = m.id;
      break;
    }
  }
  try {
    await jfetch(`/api/conversations/${conversationId}/messages/${rootMsg.id}`, {
      method: "DELETE",
    });
  } catch (e) {
    flashError("Undo failed: " + e.message);
    if (btn) btn.disabled = false;
    return;
  }
  if (targetLeaf) {
    try {
      await jfetch(`/api/conversations/${conversationId}/active-leaf`, {
        method: "POST",
        body: JSON.stringify({ leaf_id: targetLeaf }),
      });
    } catch (_) { /* best effort; reload still recovers */ }
  }
  window.location.reload();
}

registerAttachment({
  id: "scene_staging_banner",
  slot: "below-body",
  // Sit above siblings (100) and the staging panel (30); banner is
  // small and informational, render it first below the body.
  order: 25,
  show: (msg) => {
    const meta = msg.metadata || {};
    if (!meta.scene_staging_origin) return false;
    return !sceneStagingBannerDismissed(msg.id);
  },
  render: buildSceneStagingBanner,
});

registerAttachment({
  id: "image_pack",
  slot: "above-body",
  // After thinking (default 0) so the trace stays on top, before the body.
  order: 50,
  show: (msg) => imagePackFor(msg) != null,
  render: (msg) => buildImagePackBlock(imagePackFor(msg)),
});

// ---------------------------------------------------------------------------
// Life Sim — stat bars on NPC messages
//
// Rendered as an above-body attachment so it sits in the same row as
// the image-pack figure when one exists; renderMessage wraps the two
// in a flex container post-attachment to get bars-left-of-image. When
// there's no image, bars render full-width as a horizontal strip.
// ---------------------------------------------------------------------------
function lifeSimActiveOnBranch() {
  if (typeof isModuleActive !== "function") return false;
  if (!isModuleActive("life_sim")) return false;
  const s = (typeof moduleSettingsFor === "function") ? moduleSettingsFor("life_sim") : {};
  if (s.enabled === false) return false;
  if (s.show_bars === false) return false;
  return true;
}

function characterStatsFor(charId) {
  const e = (state.entities || {})[charId];
  if (!e || e.type !== "character") return null;
  const stats = (e.properties || {}).stats;
  if (!stats || typeof stats !== "object") return null;
  return stats;
}

function buildStatBars(msg) {
  const stats = characterStatsFor(msg.speaker_id);
  if (!stats) return null;
  const ids = Object.keys(stats);
  if (!ids.length) return null;

  const wrap = document.createElement("div");
  wrap.className = "msg-stat-bars";

  for (const sid of ids) {
    const schema = stats[sid] || {};
    let value = Number(schema.value);
    if (!Number.isFinite(value)) value = 0;
    const max = Number.isFinite(Number(schema.max)) ? Number(schema.max) : 100;
    const min = Number.isFinite(Number(schema.min)) ? Number(schema.min) : 0;
    const span = max - min;
    const pct = span > 0 ? Math.max(0, Math.min(100, ((value - min) / span) * 100)) : 0;

    const bar = document.createElement("div");
    bar.className = "msg-stat-bar";
    const rounded = Math.round(value);
    bar.title = `${schema.label || sid}: ${rounded} / ${max}`;

    const fill = document.createElement("div");
    fill.className = "msg-stat-bar-fill";
    fill.style.setProperty("--fill", `${pct}%`);
    if (pct < 30) fill.classList.add("low");
    else if (pct < 60) fill.classList.add("mid");
    bar.appendChild(fill);

    const label = document.createElement("span");
    label.className = "msg-stat-bar-label";
    label.textContent = `${schema.label || sid} ${rounded}`;
    bar.appendChild(label);

    // Vertical bars are too narrow for text inside; render the value
    // as a tiny readout below the bar instead.
    const numChip = document.createElement("span");
    numChip.className = "msg-stat-bar-num";
    numChip.textContent = String(rounded);
    bar.appendChild(numChip);

    wrap.appendChild(bar);
  }
  return wrap;
}

registerAttachment({
  id: "life_sim_bars",
  slot: "above-body",
  // Just before image_pack (50) so the bars node sits adjacent to the
  // image in the DOM, letting renderMessage wrap them in a flex row.
  order: 49,
  show: (msg) => {
    if (msg.persona === "user" || msg.persona === "narrator") return false;
    if (!msg.speaker_id) return false;
    if (!lifeSimActiveOnBranch()) return false;
    return characterStatsFor(msg.speaker_id) != null;
  },
  render: (msg) => buildStatBars(msg),
});

async function maybeAutoStateChanges(msg) {
  // Fire the auto-state-changes side call on a freshly-streamed
  // character message. Lives in a separate POST so the SSE response
  // doesn't hold open across a second model round-trip — that was the
  // hang the user reported when the pass was inline in stream.py.
  // Server returns { ran, edits, applied_log, image_pack_pick }; we
  // mirror the new image_pack_pick (if any) onto the local message so
  // the picture refreshes without a reload.
  //
  // Tracked on state.autoStateController so streamGenerate() can abort
  // it when the user starts a new turn — Ollama serializes per-model,
  // and a regen fired while this is in flight otherwise queues 10–40s
  // behind the in-flight call.
  if (!msg || !msg.id) return;
  if (msg.persona === "narrator") {
    // Narrator messages opt in via the narrator sub-toggle. When on,
    // run a full narrator-add pass via the STREAMING /narrator-edit
    // endpoint — same shape the per-message Narrator button uses —
    // instead of the blocking /auto_state POST. The user sees tokens
    // stream into a new sibling bubble live, the cast widget +
    // quick-edits refresh on cast_add via patchBranchCastFromMessage,
    // and the original beat is preserved as a peer branch.
    if (!autoStateOnNarratorEnabledForConv()) return;
    const directive = (msg.content || "").trim();
    if (!directive) return;
    if (msg.metadata?.auto_state_changes) return;  // already ran
    // Stamp the breadcrumb up front so a re-mount doesn't re-fire
    // (runNarratorEdit may take 5–10s and the user might cause a
    // re-render before the new sibling lands).
    msg.metadata = msg.metadata || {};
    msg.metadata.auto_state_changes = { edits: [], mode: "narrator_full" };
    try {
      await runNarratorEdit(msg, directive);
    } catch (e) {
      console.warn("auto-state narrator stream failed:", e);
    }
    return;
  } else if (msg.persona === "user") {
    if (!autoStateOnUserEnabledForConv()) return;
  } else {
    if (!msg.speaker_id) return;
    if (!autoStateAnyEnabledForConv()) return;
  }
  if (msg.metadata?.auto_state_changes) return;  // already ran for this msg

  const controller = new AbortController();
  state.autoStateController = controller;
  // Tag the controller so streamGenerate's "abort previous auto-state
  // before starting a new turn" logic can keep a NARRATOR full pass
  // alive — those do real state work the next character turn depends
  // on. Wardrobe-only auto-state on a stale character msg is still
  // killed (the stale comment about Ollama serialization still
  // applies for that case).
  controller._autoStatePersona = msg.persona;
  controller._autoStateMsgId = msg.id;
  try {
    const res = await jfetch(
      `/api/conversations/${conversationId}/messages/${msg.id}/auto_state`,
      { method: "POST", signal: controller.signal }
    );
    if (!res?.ran) return;
    // Narrator full-pass mode branches: server appended a new sibling
    // of the user-typed narrator beat carrying the auto-state edits.
    // The original message keeps a tiny breadcrumb (so the client
    // doesn't re-fire); the NEW sibling carries the real
    // auto_state_changes metadata + applied_edits + (rewritten) body.
    // Fold the new message into local state and shift the active
    // leaf to it.
    let target = state.conversation.messages[msg.id] || msg;
    if (res.mode === "narrator_full" && res.new_message_id) {
      const newId = res.new_message_id;
      // Fetch the new message body from the server's payload (we
      // already have its applied_log + edits + new_body) and synth
      // a minimal in-memory message. The next reloadConversation /
      // reloadInstanceEntities call below will pull the canonical
      // version.
      const newMsg = {
        id: newId,
        parent_id: msg.parent_id,
        persona: "narrator",
        speaker_id: null,
        content: res.new_body || msg.content,
        created_at: Math.floor(Date.now() / 1000),
        metadata: {
          auto_state_changes: {
            edits: res.edits || [],
            applied_log: res.applied_log || [],
            mode: "narrator_full",
          },
          applied_edits: res.applied_log || [],
        },
      };
      state.conversation.messages[newId] = newMsg;
      // Breadcrumb on the original.
      msg.metadata = msg.metadata || {};
      msg.metadata.auto_state_changes = {
        edits: [], mode: "narrator_full", branched_to: newId,
      };
      // Shift active leaf so subsequent generation reads from the
      // sibling's post-edit presence_snapshot. fullRender below picks
      // it up via setActiveLeaf.
      if (res.active_path_leaf) {
        state.conversation.active_path_leaf = res.active_path_leaf;
      }
      target = newMsg;
    } else {
      target.metadata = target.metadata || {};
      target.metadata.auto_state_changes = {
        edits: res.edits || [],
        mode: res.mode || "wardrobe",
      };
      if (res.applied_log?.length) {
        target.metadata.applied_edits = (target.metadata.applied_edits || []).concat(res.applied_log);
      }
      if (res.image_pack_pick) {
        target.metadata.image_pack_pick = res.image_pack_pick;
      }
      if (res.new_body && target.persona === "narrator") {
        target.content = res.new_body;
      }
    }
    // Keep `stored` as the legacy variable name for downstream code
    // below — points at whichever message (new sibling or original)
    // actually carries the auto-state edits.
    const stored = target;
    // If the pass emitted cast_add / cast_remove edits, the cast
    // membership for this branch changed. Refresh state.entities +
    // effectiveCastChars + the cast / quick-edits widgets so the user
    // sees the new chars without manually reloading the page.
    const castEditsLanded = (res.applied_log || []).some(
      (e) => e?.kind === "cast_add" || e?.kind === "cast_remove",
    );
    if (castEditsLanded) {
      for (const e of res.applied_log || []) {
        if (!e?.id) continue;
        if (e.kind === "cast_add") state.effectiveCastChars.add(e.id);
        else if (e.kind === "cast_remove") state.effectiveCastChars.delete(e.id);
      }
      // Pull the freshly-materialized entities (the narrator may have
      // instanced new ids from generic templates) before re-rendering.
      reloadInstanceEntities().catch(() => {});
    }
    // Multi-response: when auto-state lands edits on a group member,
    // the server re-runs the GROUP composite (not the solo) and
    // stamps every member with the new pick — so all bubbles in the
    // row stay consistent instead of one going solo-bikini while the
    // others keep the stale composite. Mirror that here: stamp the
    // returned image_pack_pick onto every sibling whose id is in the
    // group-update list, then re-render each.
    const groupUpdatedIds = Array.isArray(res.group_image_updated_message_ids)
      ? res.group_image_updated_message_ids
      : [];
    for (const sibId of groupUpdatedIds) {
      if (sibId === msg.id) continue;
      const sib = state.conversation.messages[sibId];
      if (!sib) continue;
      sib.metadata = sib.metadata || {};
      if (res.image_pack_pick) {
        sib.metadata.image_pack_pick = res.image_pack_pick;
      }
      rerenderMessage(sib);
    }
    if (res.applied_log?.length) {
      rerenderMessage(stored);
    }
    // Narrator branch: the new sibling needs to be rendered + the
    // active path needs to redraw so the branch chip shows up under
    // the parent. fullRender redraws the message column from
    // active_path_leaf so this handles both.
    if (res.mode === "narrator_full" && res.new_message_id) {
      if (typeof fullRender === "function") fullRender();
      if (res.active_path_leaf
          && typeof setActiveLeaf === "function") {
        setActiveLeaf(res.active_path_leaf);
      }
    }
  } catch (e) {
    /* network/timeout/aborted — drop silently. */
  } finally {
    if (state.autoStateController === controller) {
      state.autoStateController = null;
    }
  }
}

async function maybeLifeSimUpdate(msg) {
  // Fire the life_sim stats + goals side-call against a fresh NPC
  // message. Mirrors maybeAutoStateChanges shape: separate POST so the
  // SSE response doesn't block, server hard-skips when the module is
  // inactive for this branch / disabled / the focal character has no
  // declared stats, so no need to re-check those here.
  if (!msg || !msg.id) return;
  if (msg.persona === "user" || msg.persona === "narrator") return;
  if (!msg.speaker_id) return;
  if (msg.metadata?.life_sim_update) return;  // already ran for this msg

  try {
    const res = await jfetch(
      `/api/conversations/${conversationId}/messages/${msg.id}/life_sim_update`,
      { method: "POST" }
    );
    if (!res?.ran) return;
    const stored = state.conversation.messages[msg.id] || msg;
    stored.metadata = stored.metadata || {};
    stored.metadata.life_sim_update = { edits: res.edits || [] };
    if (res.applied_log?.length) {
      stored.metadata.applied_edits = (stored.metadata.applied_edits || []).concat(res.applied_log);
      // Mirror patch edits into the client-side entity cache so the
      // stat bars and any other live readouts pick up the new values
      // without a full /entities reload. Server is authoritative; this
      // is purely local view-state.
      for (const entry of res.applied_log) {
        if (!entry || entry.ok === false) continue;
        if (entry.kind === "patch" && entry.id && entry.data) {
          const ent = state.entities[entry.id];
          if (ent) clientDeepMerge(ent, entry.data);
        } else if (entry.kind === "unset" && entry.id && Array.isArray(entry.path)) {
          const ent = state.entities[entry.id];
          if (ent) clientDeepUnset(ent, entry.path);
        }
      }
    }
    // Stat values changed → re-render this single message so the
    // attached bars reflect the new values immediately.
    rerenderMessage(stored);
  } catch (e) {
    /* network/timeout/aborted — drop silently. */
  }
}

// Tiny client-side deep-merge mirroring app/merge.py — used to keep
// state.entities in sync with side-call applied edits without a full
// /entities reload. Lists are replaced wholesale; dicts merge by key.
function clientDeepMerge(target, patch) {
  if (!target || typeof target !== "object") return;
  for (const [k, v] of Object.entries(patch || {})) {
    if (v && typeof v === "object" && !Array.isArray(v) &&
        target[k] && typeof target[k] === "object" && !Array.isArray(target[k])) {
      clientDeepMerge(target[k], v);
    } else {
      target[k] = v;
    }
  }
}
function clientDeepUnset(target, path) {
  let cur = target;
  for (let i = 0; i < path.length - 1; i++) {
    if (!cur || typeof cur !== "object") return;
    cur = cur[path[i]];
  }
  if (cur && typeof cur === "object") {
    const last = path[path.length - 1];
    if (Array.isArray(cur)) {
      const idx = parseInt(last, 10);
      if (Number.isInteger(idx) && idx >= 0 && idx < cur.length) cur.splice(idx, 1);
    } else {
      delete cur[last];
    }
  }
}

async function maybePickImagePack(msg) {
  if (!msg || !msg.id) return;
  if (msg.persona === "user" || msg.persona === "narrator") return;
  if (!imagePackEnabledForConv()) return;
  if (!speakerHasImagePack(msg.speaker_id)) return;
  // Already persisted from a prior call (this message was loaded with
  // metadata.image_pack_pick on it) or already in flight.
  if (msg.metadata?.image_pack_pick) return;
  if (state.imagePackPicks[msg.id]) return;

  state.imagePackPicks[msg.id] = "loading";
  rerenderMessage(msg);
  try {
    const res = await jfetch(
      `/api/conversations/${conversationId}/messages/${msg.id}/image_pick`,
      { method: "POST" }
    );
    if (res?.picked) {
      // Mirror the server-side persistence onto the in-memory message so
      // the next render sees the pick in metadata, not just transient state.
      const stored = state.conversation.messages[msg.id] || msg;
      stored.metadata = stored.metadata || {};
      stored.metadata.image_pack_pick = res.picked;
    }
  } catch (e) {
    /* network/timeout — drop the loading state, no persisted pick. */
  }
  delete state.imagePackPicks[msg.id];
  rerenderMessage(msg);
}

function summarizeEdit(e) {
  if (!e || typeof e !== "object") return JSON.stringify(e);
  // Failed edits are recorded WRAPPED: {kind, ok:false, error, edit:{...the
  // real edit...}}. The real id/kind live on the inner `edit`, so reading
  // them off the wrapper gives `undefined` (the "undefined ← replace" bug).
  // Summarize the inner edit and flag the failure instead.
  if (e.ok === false) {
    const inner = e.edit && typeof e.edit === "object" ? e.edit : e;
    return `⚠ ${describeEdit(inner)} — FAILED`;
  }
  return describeEdit(e);
}

function describeEdit(e) {
  if (!e || !e.kind) return JSON.stringify(e);
  if (e.kind === "move") {
    const dest = e.location ? `${e.location}:${e.room}` : e.room;
    return `${e.character_id} → ${dest}`;
  }
  if (e.kind === "outfit") return `${e.character_id} wears ${e.outfit_id}`;
  if (e.kind === "patch") {
    // Surface the deepest leaf path = value as the summary.
    const leaves = [];
    const walk = (obj, path) => {
      if (obj && typeof obj === "object" && !Array.isArray(obj)) {
        for (const [k, v] of Object.entries(obj)) walk(v, path.concat(k));
      } else {
        leaves.push({ path, value: obj });
      }
    };
    walk(e.data || {}, []);
    if (leaves.length === 1) {
      const { path, value } = leaves[0];
      return `${e.id}.${path.join(".")} = ${JSON.stringify(value)}`;
    }
    return `${e.id} ← patch (${leaves.length} fields)`;
  }
  if (e.kind === "replace") return `${e.id} ← replace`;
  if (e.kind === "unset") return `unset ${e.id}.${(e.path || []).join(".")}`;
  return e.kind;
}

function buildAppliedEditsRow(msg) {
  const wrap = document.createElement("div");
  wrap.className = "applied-edits";
  const list = msg.metadata.applied_edits || [];
  list.forEach((entry, idx) => {
    const chip = document.createElement("span");
    chip.className = "edit-chip";
    if (entry.ok === false) chip.classList.add("failed");
    if (entry.reverted_at) chip.classList.add("reverted");
    chip.title = JSON.stringify(entry, null, 2);

    const label = document.createElement("span");
    label.className = "edit-chip-label";
    label.textContent = summarizeEdit(entry);
    chip.appendChild(label);

    // Full edit, surfaced IN the block (not just the hover tooltip): the
    // label toggles a detail panel with the complete edit JSON. For a
    // failed edit that includes the error + the rejected `edit` payload,
    // so it's clear what the model emitted and why it didn't apply.
    const detail = document.createElement("pre");
    detail.className = "edit-chip-detail";
    detail.textContent = JSON.stringify(entry, null, 2);
    label.style.cursor = "pointer";
    label.title = "Show/hide the full edit";
    label.addEventListener("click", () => detail.classList.toggle("open"));

    // Failed edits: show the reason inline so it reads without expanding.
    if (entry.ok === false && entry.error) {
      const err = document.createElement("span");
      err.className = "edit-chip-error";
      err.textContent = entry.error;
      chip.appendChild(err);
    }

    if (entry.ok !== false && !entry.reverted_at) {
      const undo = document.createElement("button");
      undo.type = "button";
      undo.className = "edit-chip-undo";
      undo.title = "Revert this change";
      undo.textContent = "×";
      undo.addEventListener("click", async () => {
        undo.disabled = true;
        try {
          const r = await jfetch(
            `/api/conversations/${conversationId}/messages/${msg.id}/revert-edit`,
            { method: "POST", body: JSON.stringify({ index: idx }) }
          );
          if (r.message) {
            msg.metadata = r.message.metadata || msg.metadata;
            // Re-render this message in place.
            const old = document.querySelector(`[data-message-id="${msg.id}"]`);
            if (old) old.replaceWith(renderMessage(msg));
          }
          if (r.affected_entities) {
            for (const [id, e] of Object.entries(r.affected_entities)) {
              state.entities[id] = e;
            }
          }
          flashInfo("Reverted.");
          renderQuickEdits();
        } catch (e) {
          flashError("Revert failed: " + e.message);
          undo.disabled = false;
        }
      });
      chip.appendChild(undo);
    } else if (entry.reverted_at) {
      const tag = document.createElement("span");
      tag.className = "muted small";
      tag.textContent = "(reverted)";
      chip.appendChild(tag);
    }

    wrap.appendChild(chip);
    wrap.appendChild(detail);
  });
  return wrap;
}

// The renderable narrator_state items on a message (a move/outfit/clothing
// change may be a single object or a list). Non-empty ⟺ a narrator_state
// block will render.
function narratorStateItems(msg) {
  const ns = msg.metadata?.narrator_state;
  if (!ns) return [];
  return (Array.isArray(ns) ? ns : [ns]).filter((x) => x && typeof x === "object");
}

// Which block "owns" the applied-edit chips for this message, so the same
// edits never render as two stacked narrator blocks:
//   "edit"       — fold into the narrator_edit block (a narrator rewrite)
//   "state"      — fold into the narrator_state block (a move/outfit change)
//   "standalone" — no other narrator block, render the chips as their own
//   null         — no applied edits to show
// narrator_edit wins over narrator_state so a message carrying both hosts
// the chips exactly once.
function appliedEditsHost(msg) {
  if (!(msg.metadata?.applied_edits?.length)) return null;
  if (msg.metadata?.narrator_edit) return "edit";
  if (narratorStateItems(msg).length) return "state";
  return "standalone";
}

// The applied-edit chips used to render as a free-floating row BELOW the
// message, where a long label (an outfit id, a description patch) stretched
// the page. They now live inside a narrator block ABOVE the body — same
// chips, same undo affordance, but contained by the block's width guards and
// grouped with the other narrator state/edit blocks. When the message
// already has a narrator_state / narrator_edit block, the chips are folded
// INTO that block (see appliedEditsHost) rather than rendered here.
function buildAppliedEditsBlock(msg) {
  const det = document.createElement("details");
  det.className = "msg-narrator-edit msg-applied-edits";
  det.open = true;
  det.dataset.messageId = msg.id;

  const sum = document.createElement("summary");
  sum.className = "narrator-edit-summary";
  const label = document.createElement("span");
  label.className = "narrator-edit-label";
  const n = (msg.metadata.applied_edits || []).length;
  label.textContent = `Narrator edit · ${n} change${n === 1 ? "" : "s"}`;
  sum.appendChild(label);
  det.appendChild(sum);

  const inner = document.createElement("div");
  inner.className = "msg-narrator-edit-body";
  inner.appendChild(buildAppliedEditsRow(msg));
  det.appendChild(inner);
  return det;
}
registerAttachment({
  id: "applied_edits",
  slot: "above-body",
  order: 6, // just after narrator_state (5), grouped with the narrator blocks
  // Only render standalone when no narrator_state / narrator_edit block will
  // host the chips; otherwise the chips are folded into that block.
  show: (msg) => appliedEditsHost(msg) === "standalone",
  render: buildAppliedEditsBlock,
});

function buildPhraseHitsRow(msg) {
  const wrap = document.createElement("div");
  wrap.className = "phrase-hits";
  const label = document.createElement("span");
  label.className = "phrase-hits-label";
  label.textContent = "banned phrase used:";
  wrap.appendChild(label);
  for (const p of msg.metadata.phrase_hits) {
    const chip = document.createElement("span");
    chip.className = "phrase-hit-chip";
    chip.textContent = p;
    wrap.appendChild(chip);
  }
  return wrap;
}
registerAttachment({
  id: "phrase_hits",
  slot: "below-body",
  order: 20,
  show: (msg) => !!(msg.metadata?.phrase_hits?.length),
  render: buildPhraseHitsRow,
});

// Move / outfit state changes are stored as structured `narrator_state`
// (no prose) by the /move and /outfit endpoints. Render them as a collapsed
// chip ABOVE the thinking block (order 5 < thinking's 10) instead of a verbose
// "*Iris moves from X to Y.*" narrator bubble. They're excluded from the model
// prompt server-side (empty content), so this is purely a UI affordance.
function buildNarratorStateBlock(msg) {
  const ns = msg.metadata?.narrator_state;
  if (!ns) return null;
  // narrator_state may be a single object (move/outfit) or a LIST (so a
  // clothing block can coexist with an outfit/move block on one message).
  const items = (Array.isArray(ns) ? ns : [ns]).filter((x) => x && typeof x === "object");
  const built = items.map((it) => buildOneNarratorState(msg, it)).filter(Boolean);
  if (!built.length) return null;
  // Fold the applied-edit chips (with their undo affordance) into the last
  // state block's body when this block owns them, so an outfit/move change
  // shows one narrator block instead of a state block + a separate chip block.
  if (appliedEditsHost(msg) === "state") {
    const host = built[built.length - 1];
    const body = host.querySelector(".msg-narrator-edit-body");
    if (body) body.appendChild(buildAppliedEditsRow(msg));
    host.open = true; // keep the change chips visible, as the old tag row was
  }
  if (built.length === 1) return built[0];
  const group = document.createElement("div");
  group.className = "msg-narrator-state-group";
  built.forEach((b) => group.appendChild(b));
  return group;
}

function buildOneNarratorState(msg, ns) {
  const det = document.createElement("details");
  det.className = "msg-narrator-edit msg-narrator-state";
  det.dataset.messageId = msg.id;

  const sum = document.createElement("summary");
  sum.className = "narrator-edit-summary";
  const label = document.createElement("span");
  label.className = "narrator-edit-label";
  const who = ns.character_name || ns.character || "Someone";
  const clothingParts = () =>
    Object.entries(ns.slots || {})
      .map(([s, v]) => `${s} ${(SCENE_SLOT_LABEL[v] || v).toLowerCase()}`);
  if (ns.kind === "move") {
    label.textContent = ns.to
      ? `Narrator edit · ${who} → ${ns.to}`
      : `Narrator edit · ${who} left the scene`;
  } else if (ns.kind === "outfit") {
    label.textContent = `Narrator edit · ${who}: ${ns.outfit}`;
  } else if (ns.kind === "clothing") {
    const parts = clothingParts();
    label.textContent = `Narrator edit · ${who}: ${parts.length ? parts.join(", ") : "clothing"}`;
  } else {
    label.textContent = `Narrator edit · ${who}`;
  }
  sum.appendChild(label);
  det.appendChild(sum);

  const inner = document.createElement("div");
  inner.className = "msg-narrator-edit-body";
  if (ns.kind === "move") {
    inner.textContent = ns.from && ns.to
      ? `Moved from the ${ns.from} to the ${ns.to}.`
      : ns.to ? `Moved to the ${ns.to}.`
      : ns.from ? `Stepped out of the ${ns.from}.` : `Stepped out of view.`;
  } else if (ns.kind === "outfit") {
    inner.textContent = `Changed into ${ns.outfit}.`;
  } else if (ns.kind === "clothing") {
    const parts = clothingParts();
    inner.textContent = parts.length ? `${who} — ${parts.join(", ")}.` : `Adjusted clothing.`;
  }
  det.appendChild(inner);
  return det;
}

// Mirror the server's clothing narrator block locally so it shows the
// instant a slot is toggled (server persists the same in
// _upsert_clothing_narrator_state). Accumulates onto one clothing entry.
function mirrorClothingNarratorState(eid, slot, value) {
  const leafId = state.conversation.active_path_leaf;
  const leaf = leafId ? state.conversation.messages[leafId] : null;
  if (!leaf) return;
  const meta = leaf.metadata = leaf.metadata || {};
  let ns = meta.narrator_state;
  const items = Array.isArray(ns) ? ns : (ns && typeof ns === "object" ? [ns] : []);
  let entry = items.find((it) => it && it.kind === "clothing" && it.character === eid);
  if (!entry) {
    const name = (state.entities[eid] || {}).name || eid;
    entry = { kind: "clothing", character: eid, character_name: name, slots: {} };
    items.push(entry);
  }
  entry.slots = entry.slots || {};
  entry.slots[String(slot).toLowerCase()] = value;
  meta.narrator_state = items;
}
registerAttachment({
  id: "narrator_state",
  slot: "above-body",
  order: 5, // above the thinking block (order 10)
  render: buildNarratorStateBlock,
});

function buildNarratorEditBlock(msg) {
  const ne = msg.metadata?.narrator_edit || {};
  const det = document.createElement("details");
  det.className = "msg-narrator-edit";
  det.dataset.messageId = msg.id;

  const sum = document.createElement("summary");
  sum.className = "narrator-edit-summary";
  const label = document.createElement("span");
  label.className = "narrator-edit-label";
  const editsCount = (ne.edits || []).length;
  const appliedCount = (ne.applied || []).filter((a) => a && a.ok !== false).length;
  label.textContent = `Narrator edit · ${editsCount} directive${editsCount === 1 ? "" : "s"}` +
    (editsCount ? ` (${appliedCount} applied)` : "");
  sum.appendChild(label);
  det.appendChild(sum);

  const inner = document.createElement("div");
  inner.className = "msg-narrator-edit-body";

  if (ne.directive) {
    const d = document.createElement("div");
    d.className = "narrator-edit-directive";
    const dh = document.createElement("strong");
    dh.textContent = "Directive: ";
    d.appendChild(dh);
    d.appendChild(document.createTextNode(ne.directive));
    inner.appendChild(d);
  }

  if (appliedEditsHost(msg) === "edit") {
    // Fold in the applied-edit chips: friendly, undoable, and each chip
    // carries the raw edit JSON in its tooltip — so this replaces the raw
    // directive list below whenever edits actually applied.
    inner.appendChild(buildAppliedEditsRow(msg));
    det.open = true; // keep the change chips visible, as the old tag row was
  } else {
    const edits = ne.edits || [];
    if (edits.length) {
      const list = document.createElement("ul");
      list.className = "narrator-edit-list";
      for (const e of edits) {
        const li = document.createElement("li");
        li.textContent = JSON.stringify(e);
        list.appendChild(li);
      }
      inner.appendChild(list);
    }
  }

  const trace = (ne.thinking_trace || "").trim();
  if (trace) {
    const traceDet = document.createElement("details");
    traceDet.className = "narrator-edit-trace";
    const traceSum = document.createElement("summary");
    traceSum.textContent = `Reasoning trace · ${trace.length}ch`;
    traceDet.appendChild(traceSum);
    const traceBody = document.createElement("div");
    traceBody.className = "msg-thinking-body";
    traceBody.textContent = trace;
    traceDet.appendChild(traceBody);
    inner.appendChild(traceDet);
  }

  // The original message lives as a real sibling under the same parent,
  // so the standard sibling chip flips between them — no inline "view
  // original" details needed.

  det.appendChild(inner);
  return det;
}
registerAttachment({
  id: "narrator_edit",
  slot: "above-body",
  order: 20,
  render: buildNarratorEditBlock,
});

function buildThinkingBlock(msg) {
  const trace = msg.metadata?.thinking || "";
  const det = document.createElement("details");
  det.className = "msg-thinking";
  det.dataset.messageId = msg.id;

  const sum = document.createElement("summary");
  sum.className = "thinking-summary";
  const label = document.createElement("span");
  label.className = "thinking-label";
  label.textContent = `Reasoning trace · ${trace.length}ch · ≈${Math.ceil(trace.length / 4)} tok`;
  sum.appendChild(label);
  const editBtn = document.createElement("button");
  editBtn.type = "button";
  editBtn.className = "ghost xs thinking-edit-btn";
  editBtn.textContent = "Edit";
  editBtn.title = "Edit the reasoning trace in place";
  editBtn.addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    det.open = true;
    enterThinkingEdit(det, msg);
  });
  sum.appendChild(editBtn);
  det.appendChild(sum);

  const tb = document.createElement("div");
  tb.className = "msg-thinking-body";
  tb.textContent = trace;
  det.appendChild(tb);
  return det;
}
registerAttachment({
  id: "thinking",
  slot: "above-body",
  order: 10,
  render: buildThinkingBlock,
});

// ---------------------------------------------------------------------------
// Auto-state changes block
// ---------------------------------------------------------------------------
// Surface when the auto-state side call landed edits on a message —
// otherwise the second model round-trip is invisible to the user, who
// just sees the sprite quietly update with no explanation. The data
// already lives at msg.metadata.auto_state_changes (set by api.py:
// auto_state_pass + mirrored by maybeAutoStateChanges). Renders as a
// collapsed <details> above the body, between the thinking trace
// (order 10) and the image pick (order 50).
//
// Returns null when the metadata wasn't populated (auto-state didn't
// run, or ran but emitted no edits), so messages without auto-state
// changes show nothing.
function _autoStateEditSummary(edit) {
  // Human-readable one-liner per applied-edit entry. Mirrors the
  // shapes auto_state.py emits (outfit + properties.clothing_overrides
  // patches only) plus a generic fallback for anything else that
  // surfaces here (e.g. future expansion).
  if (!edit || typeof edit !== "object") return "?";
  const kind = edit.kind;
  if (kind === "outfit") {
    const cid = edit.character_id || edit.id || "?";
    return `${cid}: outfit → ${edit.outfit_id || "?"}`;
  }
  if (kind === "patch") {
    const cid = edit.id || "?";
    const overrides = (((edit.data || {}).properties || {})
      .clothing_overrides) || null;
    if (overrides && typeof overrides === "object") {
      // 1 = on, 2 = displaced/partial, 3 = removed/off
      const labels = { 1: "on", 2: "partial", 3: "off" };
      const parts = Object.entries(overrides).map(
        ([slot, val]) => `${slot}: ${labels[val] || val}`,
      );
      return `${cid}: ${parts.join(", ")}`;
    }
    // Other patches (e.g. notes.status from a future widened auto-
    // state) — just show the top-level keys touched.
    const keys = Object.keys(edit.data || {}).join(", ") || "(no data)";
    return `${cid}: ${keys}`;
  }
  if (kind === "move") {
    const cid = edit.character_id || "?";
    const target = [edit.location, edit.room].filter(Boolean).join(":");
    return `${cid}: move → ${target || "?"}`;
  }
  return `${kind || "?"}: ${edit.id || edit.character_id || "?"}`;
}

function buildAutoStateBlock(msg) {
  const meta = msg.metadata?.auto_state_changes;
  if (!meta) return null;
  const edits = Array.isArray(meta.edits) ? meta.edits : [];
  if (!edits.length) return null;  // ran but emitted nothing — no block.

  const det = document.createElement("details");
  det.className = "msg-thinking msg-auto-state";  // reuse thinking CSS
  det.dataset.messageId = msg.id;

  const sum = document.createElement("summary");
  sum.className = "thinking-summary";
  const label = document.createElement("span");
  label.className = "thinking-label";
  label.textContent = `Auto-state · ${edits.length} edit${edits.length === 1 ? "" : "s"}`;
  sum.appendChild(label);
  det.appendChild(sum);

  const body = document.createElement("div");
  body.className = "msg-thinking-body";
  const lines = edits.map(_autoStateEditSummary);
  // One edit per line, monospace-friendly via the inherited
  // msg-thinking-body styling.
  body.textContent = lines.join("\n");
  det.appendChild(body);
  return det;
}
registerAttachment({
  id: "auto_state_changes",
  slot: "above-body",
  // Between thinking (10) and image_pack (50). Sits below the
  // reasoning trace, above the image — same vertical order as the
  // pipeline ran them in.
  order: 20,
  render: buildAutoStateBlock,
});

function enterThinkingEdit(det, msg) {
  if (det.classList.contains("editing")) return;
  det.classList.add("editing");
  const tb = det.querySelector(".msg-thinking-body");
  const original = tb.textContent;
  const ta = document.createElement("textarea");
  ta.className = "msg-thinking-edit";
  ta.value = original;
  ta.style.minHeight = Math.max(120, tb.offsetHeight) + "px";
  tb.style.display = "none";
  tb.after(ta);

  const actions = document.createElement("div");
  actions.className = "msg-edit-actions";
  const saveBtn = document.createElement("button");
  saveBtn.type = "button";
  saveBtn.className = "primary xs";
  saveBtn.textContent = "Save";
  const cancelBtn = document.createElement("button");
  cancelBtn.type = "button";
  cancelBtn.className = "ghost xs";
  cancelBtn.textContent = "Cancel";
  actions.append(saveBtn, cancelBtn);
  ta.after(actions);
  ta.focus();

  function exit() {
    ta.remove();
    actions.remove();
    tb.style.display = "";
    det.classList.remove("editing");
  }

  cancelBtn.addEventListener("click", (e) => { e.preventDefault(); exit(); });
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Escape") exit();
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") saveBtn.click();
  });

  saveBtn.addEventListener("click", async () => {
    const next = ta.value;
    saveBtn.disabled = true;
    try {
      const updated = await jfetch(`/api/conversations/${conversationId}/messages/${msg.id}`, {
        method: "PATCH",
        body: JSON.stringify({ thinking: next }),
      });
      state.conversation.messages[updated.id] = updated;
      // Replace this whole thinking block with a fresh one so the summary
      // counts are up to date.
      const fresh = buildThinkingBlock(updated);
      det.replaceWith(fresh);
    } catch (e) {
      saveBtn.disabled = false;
      flashError("Save failed: " + e.message);
    }
  });
}

function actionBtn(label, fn, title = "") {
  const b = document.createElement("button");
  b.className = "ghost xs";
  b.textContent = label;
  if (title) b.title = title;
  b.addEventListener("click", fn);
  return b;
}

function switchToSibling(siblings, idx) {
  const target = siblings[(idx + siblings.length) % siblings.length];
  if (!target) return;
  setActiveLeaf(deepestActiveLeaf(target.id));
}

// Walk down from `nodeId` to whichever leaf was last active inside its
// subtree, using conversation.branch_choices as a memo. When no memo
// exists for a fork, fall back to the most recently created child so a
// fresh branch still descends to its tip. Returns nodeId itself when it
// has no children.
function deepestActiveLeaf(nodeId) {
  const msgs = state.conversation.messages;
  const choices = state.conversation.branch_choices || {};
  const seen = new Set();
  let cur = nodeId;
  while (cur && !seen.has(cur)) {
    seen.add(cur);
    const kids = Object.values(msgs).filter((m) => m.parent_id === cur);
    if (!kids.length) return cur;
    const memo = choices[cur];
    const next = (memo && kids.find((k) => k.id === memo))
      || kids.reduce((a, b) => (a.created_at >= b.created_at ? a : b));
    cur = next.id;
  }
  return cur;
}

function fullRender() {
  const path = pathToLeaf(state.conversation.active_path_leaf);
  // Preserve module-slot children (e.g. the chat_above slot used by
  // the locked_image module) through the clear — they live INSIDE
  // the messages scroll container so position: sticky anchors to
  // the right scroll context, but the path-rebuild would otherwise
  // wipe them.
  const preserved = Array.from(messagesEl.children).filter(
    (c) => c.classList && c.classList.contains("module-slot"),
  );
  messagesEl.replaceChildren(...preserved);
  for (const msg of path) messagesEl.appendChild(renderMessage(msg));
  // Branch-aware side panels need to follow the active leaf — the cast
  // row's location/outfit comboboxes and the quick-edits panel both
  // resolve their displayed values from the leaf's presence_snapshot,
  // so stale rows after a sibling switch were possible without this.
  renderCastList();
  renderQuickEdits();
  // The composer's "as <name>" label is derived from
  // userPersonaName() — re-derive it here so a sub-scenario switch
  // (which patches settings.user_persona) flips the label without
  // waiting for a manual persona-dropdown change.
  if (typeof updateAsLabel === "function") updateAsLabel();
  if (typeof loadActiveSetup === "function") loadActiveSetup();
  scrollToBottomSoon();
}

function appendMessage(msg) {
  state.conversation.messages[msg.id] = msg;
  state.conversation.active_path_leaf = msg.id;
  recordBranchChoicePath(msg.id);
  patchBranchCastFromMessage(msg);
  messagesEl.appendChild(renderMessage(msg));
  scrollToBottomSoon();
  renderQuickEdits();
}

// Pull cast_add / cast_remove entries off a freshly committed
// message's applied_edits log into the client's branch cast set.
// Without this, narrator auto-instance (a [move] against an
// off-cast char) would put the new character in state.entities
// via the entities refetch — but never in this branch's cast set,
// so renderCastList would still hide them.
function patchBranchCastFromMessage(msg) {
  const log = (msg && msg.metadata && msg.metadata.applied_edits) || [];
  let changed = false;
  let needsEntityRefresh = false;
  for (const e of log) {
    if (!e || e.ok === false) continue;
    if (e.kind === "cast_add" && e.id) {
      const etype = (state.entities[e.id] || {}).type;
      if (etype === "object") state.effectiveCastObjects.add(e.id);
      else state.effectiveCastChars.add(e.id);
      changed = true;
      // Narrator auto-instance just landed a brand-new char on disk
      // that the client mirror doesn't know about yet. Without this
      // fetch the cast list would have the id in effectiveCastChars
      // but no entity to render against.
      if (!state.entities[e.id]) needsEntityRefresh = true;
    } else if (e.kind === "cast_remove" && e.id && e.id !== "user") {
      state.effectiveCastChars.delete(e.id);
      state.effectiveCastObjects.delete(e.id);
      changed = true;
    }
  }
  if (needsEntityRefresh) {
    jfetch(`/api/conversations/${conversationId}/entities`)
      .then((r) => {
        state.entities = r.entities || state.entities;
        renderCastList();
        renderQuickEdits();  // keep Quick edits side panel in sync too
        renderPersonaResponderDropdowns();
      })
      .catch(() => {});
  } else if (changed) {
    // Even when no new entity needs fetching (e.g. a cast_remove on
    // an existing char), the rendered cast widgets need a redraw so
    // they re-filter against the updated effectiveCastChars.
    if (typeof renderCastList === "function") renderCastList();
    if (typeof renderQuickEdits === "function") renderQuickEdits();
    if (typeof renderPersonaResponderDropdowns === "function") {
      renderPersonaResponderDropdowns();
    }
  }
}

// Re-render a single message in place. Used by attachments whose
// visibility depends on transient client-side state (e.g. opening or
// closing the narrator-edit composer) — toggles the state, then asks
// the renderer to rebuild the message DOM so the registry's slot walk
// re-evaluates show().
function rerenderMessage(msg) {
  const old = messagesEl.querySelector(`[data-message-id="${msg.id}"]`);
  if (old) old.replaceWith(renderMessage(msg));
}

function openNarratorEditComposer(msg) {
  state.openComposers.add(msg.id);
  rerenderMessage(msg);
}
function closeNarratorEditComposer(msg) {
  state.openComposers.delete(msg.id);
  rerenderMessage(msg);
}

// ---------------------------------------------------------------------------
// API
// ---------------------------------------------------------------------------

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

async function reloadConversation() {
  const fresh = await jfetch(`/api/conversations/${conversationId}`);
  state.conversation = fresh;
  await reloadInstanceEntities();
  fullRender();
  renderQuickEdits();
}

async function reloadInstanceEntities() {
  try {
    const r = await jfetch(`/api/conversations/${conversationId}/entities`);
    state.entities = r.entities || {};
    renderCastList();
    // The quick-edits side panel also iterates state.entities — keep
    // it in sync so narrator-added characters show up without a page
    // refresh. (Symptom before: user adds Jake via narrator pass, the
    // top-of-side-panel cast widget refreshes via renderCastList but
    // the Quick edits section under it still shows the old roster.)
    renderQuickEdits();
  } catch (e) {
    flashError("Failed to refresh cast: " + e.message);
  }
}

// Re-render the left-panel cast list from state.entities, then re-seat
// the Speak-as / Reply-as dropdown options so they mirror the same
// branch-scoped cast set. Anywhere that updates the cast list also
// gets the dropdown rebuild for free — keeping them in lockstep is
// what the user expects ("dropdowns just mirror the branch pool").
function removeObjectFromScene(itemId) {
  jfetch(`/api/conversations/${conversationId}/cast/${itemId}`, { method: "DELETE" })
    .then(() => {
      // Branch-scoped removal, same contract as removeStagingItem: the
      // server emits a cast_remove edit on the active leaf and leaves the
      // instance file intact for sibling branches.
      state.effectiveCastObjects.delete(itemId);
      appendAppliedEditOnActiveLeaf({ kind: "cast_remove", ok: true, id: itemId });
      renderObjectsList();
      renderCastList();
    })
    .catch((e) => flashError("Remove failed: " + e.message));
}

// ---------------------------------------------------------------------------
// Per-character Images control (cast list)
//
// Characters that declare named image packs (properties.image_packs) get
// an inline "Images" collapsible in their cast row: one checkbox per
// pack, plus All / Default. Ticking writes the explicit
// properties.enabled_image_packs list as a branch-scoped studio patch
// (POST /entities/<id>/patch appends a kind=patch overlay on the active
// leaf), so packs swap per-branch exactly like cast membership — the
// global card is never mutated. "Default" clears the list (revert to
// default_enabled). Packs currently forced on by a scene tag
// (expose_tags ∩ object/room/outfit tags) carry a "scene" badge; the
// checkbox tracks only the explicit set, because tag exposure is
// additive on top of it server-side (sprite_url.enabled_image_packs_of).
// ---------------------------------------------------------------------------

// Client mirror of the image-pick endpoint's scene-tag gather: tags of
// every object present on this branch + the character's current room +
// their worn outfit. Entities missing from the local cache (e.g. a
// global outfit that was never instanced) just contribute nothing.
function sceneTagsFor(charId) {
  const tags = new Set();
  const addTags = (entity) => {
    for (const t of entity?.tags || []) {
      if (typeof t === "string") tags.add(t.toLowerCase());
    }
  };
  for (const oid of state.effectiveCastObjects) addTags(state.entities[oid]);
  const roomId = currentRoomFor(charId);
  if (roomId) addTags(state.entities[roomId]);
  const outfitId = currentOutfitFor(charId);
  if (outfitId) addTags(state.entities[outfitId]);
  return tags;
}

function buildCastImagesControl(eid, e) {
  const props = e.properties || {};
  const images = props.images || {};
  const rawPacks = props.image_packs;
  const packs = rawPacks && typeof rawPacks === "object" ? rawPacks : {};
  const packIds = Object.keys(packs).filter(
    (pid) => packs[pid] && typeof packs[pid] === "object"
  );
  // The base catalog ("Default images") is toggleable too, so the
  // section renders for base-only characters as well — that's where
  // turning images off entirely matters most.
  const baseEntries = Array.isArray(props.images?.entries)
    ? props.images.entries
    : (Array.isArray(props.image_pack?.entries) ? props.image_pack.entries : []);
  const hasBase = baseEntries.length > 0;
  // Outfit-profile "image states" (grouped tri-state pools). When present they
  // subsume the standalone "Default images" base toggle — each section gets
  // its own Default variant — so that row is hidden for profile characters.
  const profilesObj = props.images?.outfit_profiles || props.image_pack?.outfit_profiles;
  const hasProfiles =
    profilesObj && typeof profilesObj === "object" && Object.keys(profilesObj).length > 0;
  // Composed-sprite capability: a combined-format character (sprite_id
  // set / format=combined) has no packs or base catalog, but still gets
  // the section so its render-mode toggle is available.
  const hasComposed =
    !!((images.sprite_id || "").trim()) ||
    (images.format || "").toLowerCase() === "combined";
  if (!packIds.length && !hasBase && !hasProfiles && !hasComposed) return null;

  const explicit = props.enabled_image_packs;
  const hasExplicit = Array.isArray(explicit);
  const sceneTags = sceneTagsFor(eid);
  const exposedIds = new Set(packIds.filter((pid) =>
    (packs[pid].expose_tags || []).some(
      (t) => sceneTags.has(String(t).toLowerCase())
    )
  ));

  // Inline collapsible (same shape as Clothing): the summary reads out
  // the active selections; opening it expands the checkboxes in
  // document flow (no popup). All / Default sit at the top.
  const { details, body, readout } = buildCastSection(`${eid}:images`, "Images");
  details.title = "Image selections active for this character on this branch";

  // Render-mode toggle: swap this character between the composed sprite
  // (combined) and their image pack (tagged) for this branch. Writes
  // properties.images.format as a branch-scoped patch, so the image-pick
  // / sprite renderer sources from the compositor or the pack catalog.
  const curFmt = () =>
    ((state.entities[eid]?.properties?.images || {}).format || images.format || "")
      .toLowerCase();
  const modeWrap = document.createElement("div");
  modeWrap.className = "cast-images-mode";
  const modeLabel = document.createElement("span");
  modeLabel.className = "muted small";
  modeLabel.textContent = "Render:";
  modeWrap.appendChild(modeLabel);
  const mkMode = (label, value, title) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "ghost xs mode-btn";
    btn.textContent = label;
    btn.title = title;
    if (curFmt() === value) btn.classList.add("active");
    btn.addEventListener("click", () => {
      if (curFmt() === value) return;
      const cur = state.entities[eid]?.properties?.images || images || {};
      patchCastImages(eid, { images: { ...cur, format: value } }, { rerender: true });
    });
    return btn;
  };
  modeWrap.append(
    mkMode("Composed", "combined", "Render this character from the sprite compositor"),
    mkMode("Pack", "tagged", "Render this character from their image pack / tagged catalog"),
  );
  body.appendChild(modeWrap);

  const list = document.createElement("ul");
  list.className = "cast-images-list";

  // State-driven (no checkbox DOM): packs render as category toggles below.
  const liveProps = () => state.entities[eid]?.properties || {};
  const packEnabled = (pid) => {
    const ex = liveProps().enabled_image_packs;
    return Array.isArray(ex) ? ex.includes(pid) : !!packs[pid].default_enabled;
  };
  const refreshReadout = () => {
    const on = [];
    if (hasBase && !hasProfiles && liveProps().base_images_enabled !== false) {
      on.push("Default");
    }
    for (const pid of packIds) {
      const name = packs[pid].name || pid;
      if (packEnabled(pid)) on.push(name);
      else if (exposedIds.has(pid)) on.push(`${name} (scene)`);
    }
    readout.textContent = on.length ? on.join(", ") : "None";
  };

  // All / Default controls. "All" ticks everything (Default images +
  // every pack); "Default" unsets both overrides so the character falls
  // back to its card state (base on + default_enabled packs).
  const controls = document.createElement("div");
  controls.className = "cast-images-controls";
  const allBtn = document.createElement("button");
  allBtn.type = "button";
  allBtn.className = "ghost xs";
  allBtn.textContent = "All";
  allBtn.title = "Enable the default images and every pack";
  allBtn.addEventListener("click", () => {
    patchCastImages(eid, {
      enabled_image_packs: [...packIds],
      base_images_enabled: "__unset__",  // base default is on
    }, { rerender: true });
  });
  const defBtn = document.createElement("button");
  defBtn.type = "button";
  defBtn.className = "ghost xs";
  defBtn.textContent = "Default";
  defBtn.title = "Revert to this character's card defaults";
  defBtn.addEventListener("click", () => {
    // Unset both overrides; re-render re-ticks from the card defaults.
    patchCastImages(eid, {
      enabled_image_packs: "__unset__",
      base_images_enabled: "__unset__",
    }, { rerender: true });
  });
  controls.append(allBtn, defBtn);
  body.appendChild(controls);

  // "Default images" row — the always-on base catalog, now optional. Hidden
  // when the character has grouped image-state profiles (each section carries
  // its own Default variant, so a standalone base toggle is redundant).
  if (hasBase && !hasProfiles) {
    const row = document.createElement("li");
    const label = document.createElement("label");
    label.className = "cast-images-pack";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.dataset.baseImages = "1";
    cb.checked = props.base_images_enabled !== false;
    cb.addEventListener("change", () => {
      refreshReadout();
      patchCastImages(eid, {
        // Off is an explicit false; on returns to the clean default
        // (key absent), matching tagged_entries_of's `is not False`.
        base_images_enabled: cb.checked ? "__unset__" : false,
      });
    });
    label.appendChild(cb);
    const name = document.createElement("span");
    name.textContent = `Default images (${baseEntries.length})`;
    label.appendChild(name);
    row.appendChild(label);
    list.appendChild(row);
  }

  // (Additive packs — futa / mushroom / etc. — render as toggle categories in
  // the unified states UI below, not as separate checkboxes, so every
  // character's Images tab follows the same format.)

  // Image states — outfit_profiles grouped by outfit (section), each variant a
  // TRI-STATE pool toggle: On (allowed) → Off (don't pull from this pool) →
  // Excluded (remove this pool's images from every pool). Rendered grouped:
  //     Winter coat:  [On Default] [On Snowy]
  //     Swimsuit:     [On Default] [Off Poolside] [Excluded Beach]
  // An image can sit in several pools (via its tags), so Off only drops images
  // unique to that pool while Excluded removes them everywhere. State lives in
  // branch-scoped properties.image_pool_states (default On = absent).
  if (hasProfiles || packIds.length) {
    const profWrap = document.createElement("div");
    profWrap.className = "cast-images-profiles";

    const groups = new Map(); // group label -> [[pid, prof], ...]
    for (const [pid, prof] of Object.entries(profilesObj || {})) {
      if (!prof || typeof prof !== "object") continue;
      const g = prof.group || prof.name || pid;
      if (!groups.has(g)) groups.set(g, []);
      groups.get(g).push([pid, prof]);
    }

    const STATES = ["on", "off", "excluded"];
    const STATE_LABEL = { on: "On", off: "Off", excluded: "Excluded" };
    const statesNow = () => ({
      ...(state.entities[eid]?.properties?.image_pool_states || {}),
    });
    const stateOf = (pid) => statesNow()[pid] || "on";

    const cycle = (pid) => {
      const next = STATES[(STATES.indexOf(stateOf(pid)) + 1) % STATES.length];
      const map = statesNow();
      if (next === "on") delete map[pid];
      else map[pid] = next;
      // Optimistic local update so the button reflects the new state before
      // the async patch resolves.
      const entity = state.entities[eid];
      if (entity) {
        entity.properties = entity.properties || {};
        if (Object.keys(map).length) entity.properties.image_pool_states = map;
        else delete entity.properties.image_pool_states;
      }
      // Branch-scoped patch (same path as the pack checkboxes); empty map
      // clears the override entirely.
      patchCastImages(
        eid,
        { image_pool_states: Object.keys(map).length ? map : "__unset__" }
      );
      renderProfiles();
    };

    // Additive packs (futa / mushroom / …) — enable/disable, rendered in the
    // same category/toggle format so every character's Images tab matches.
    const togglePack = (pid) => {
      const cur = new Set(packIds.filter(packEnabled));
      if (cur.has(pid)) cur.delete(pid);
      else cur.add(pid);
      const arr = [...cur];
      const entity = state.entities[eid];
      if (entity) {
        entity.properties = entity.properties || {};
        entity.properties.enabled_image_packs = arr;
      }
      patchCastImages(eid, { enabled_image_packs: arr });
      renderProfiles();
      refreshReadout();
    };

    const makeCat = (catKey, title) => {
      const cat = document.createElement("details");
      cat.className = "cast-images-cat";
      cat.open = !state.castImgCatClosed.has(catKey);
      cat.addEventListener("toggle", () => {
        if (cat.open) state.castImgCatClosed.delete(catKey);
        else state.castImgCatClosed.add(catKey);
      });
      const sum = document.createElement("summary");
      sum.className = "cast-images-cat-summary";
      sum.textContent = title;
      cat.appendChild(sum);
      const row = document.createElement("div");
      row.className = "cast-images-cat-variants";
      cat.appendChild(row);
      return { cat, row };
    };

    const renderProfiles = () => {
      profWrap.innerHTML = "";
      // Each category is its own collapsible (name on its own line) with the
      // variant toggles inline below — so options aren't pushed right by a
      // fixed-width label gutter, and sections can be folded away.
      for (const [g, items] of groups) {
        const { cat, row } = makeCat(`${eid}:imgcat:${g}`, g);
        for (const [pid, prof] of items) {
          const st = stateOf(pid);
          const btn = document.createElement("button");
          btn.type = "button";
          btn.className = "cast-images-pvariant state-" + st;
          btn.textContent = `${STATE_LABEL[st]} · ${prof.name || pid}`;
          btn.title =
            "On = pull from this pool · Off = don't pull · Excluded = remove these from every pool. Click to cycle.";
          btn.addEventListener("click", () => cycle(pid));
          row.appendChild(btn);
        }
        profWrap.appendChild(cat);
      }
      // Additive packs as their own toggle category (same look).
      if (packIds.length) {
        const { cat, row } = makeCat(`${eid}:imgcat:__packs`, "Extras");
        for (const pid of packIds) {
          const on = packEnabled(pid);
          const exposed = exposedIds.has(pid);
          const btn = document.createElement("button");
          btn.type = "button";
          btn.className = "cast-images-pvariant state-" + (on ? "on" : "off");
          btn.textContent =
            `${on ? "On" : "Off"} · ${packs[pid].name || pid}` + (exposed ? " (scene)" : "");
          btn.title = exposed
            ? "Auto-enabled by a scene tag (object / room / outfit)."
            : "Toggle this image pack on / off.";
          btn.addEventListener("click", () => togglePack(pid));
          row.appendChild(btn);
        }
        profWrap.appendChild(cat);
      }
    };

    renderProfiles();
    body.appendChild(profWrap);
  }

  body.appendChild(list);
  refreshReadout();
  return details;
}

// Branch-scoped image-selection patch. `propsPatch` maps property names
// (enabled_image_packs / base_images_enabled) to a value or the
// "__unset__" deep-merge marker (= remove the key, reverting that
// property to the card default). Mirrors locally so branch walks from
// client memory keep the change (same idiom as the cast +/− patches).
async function patchCastImages(eid, propsPatch, opts = {}) {
  const patch = { properties: propsPatch };
  try {
    await jfetch(`/api/conversations/${conversationId}/entities/${eid}/patch`, {
      method: "POST",
      body: JSON.stringify(patch),
    });
    const entity = state.entities[eid];
    if (entity) {
      entity.properties = entity.properties || {};
      for (const [k, v] of Object.entries(propsPatch)) {
        if (v === "__unset__") delete entity.properties[k];  // marker = key removal
        else entity.properties[k] = v;                        // lists replace, not merge
      }
    }
    appendAppliedEditOnActiveLeaf({
      kind: "patch", ok: true, id: eid, data: patch, origin: "studio",
    });
    if (opts.rerender) renderCastList();  // re-tick checkboxes from defaults
  } catch (err) {
    flashError("Image selection change failed: " + err.message);
    renderCastList(); // revert the checkboxes to the persisted state
  }
}

// Side-panel Objects block: lists the objects present in the scene on
// this branch (state.effectiveCastObjects), each with a − to remove it,
// plus an "Add object…" combobox over the rest of the catalog (instance
// + global templates). Objects are strictly opt-in: nothing is present
// until added here / in staging / by the narrator, and adds land as
// branch-scoped cast_add edits. Mirrors the cast list and is called
// from renderCastList so it always tracks cast/object changes.
function renderObjectsList() {
  const ul = document.querySelector(".objects-list");
  if (!ul) return;
  ul.innerHTML = "";
  const objs = Object.values(state.entities || {})
    .filter((e) => e && e.type === "object" && state.effectiveCastObjects.has(e.id))
    .sort((a, b) => (a.name || a.id).localeCompare(b.name || b.id));
  const empty = document.getElementById("objects-empty");
  if (empty) empty.hidden = objs.length > 0;
  for (const obj of objs) {
    const li = document.createElement("li");
    const top = document.createElement("div");
    top.className = "cast-row-top";
    const name = document.createElement("span");
    name.className = "cast-name";
    name.textContent = obj.name || obj.id;
    if (obj.description) name.title = obj.description;
    top.appendChild(name);
    const rm = document.createElement("button");
    rm.type = "button";
    rm.className = "ghost icon cast-rm";
    rm.textContent = "−";
    rm.title = "Remove from scene";
    rm.addEventListener("click", () => removeObjectFromScene(obj.id));
    top.appendChild(rm);
    li.appendChild(top);
    ul.appendChild(li);
  }

  // Add picker: every known object (conversation instances + global
  // templates) not already in the scene on this branch.
  const known = new Map();
  for (const o of window.GEMMASIM_INITIAL?.global_objects || []) {
    if (o?.id) known.set(o.id, o.name || o.id);
  }
  for (const e of Object.values(state.entities || {})) {
    if (e && e.type === "object") known.set(e.id, e.name || e.id);
  }
  const addable = [...known.entries()]
    .filter(([id]) => !state.effectiveCastObjects.has(id))
    .map(([id, label]) => ({ id, label }))
    .sort((a, b) => a.label.localeCompare(b.label));
  if (addable.length) {
    const li = document.createElement("li");
    li.className = "objects-add-row";
    li.appendChild(
      buildCastCombobox(addable, "", "Add object…", (id) => addObjectToScene(id))
    );
    ul.appendChild(li);
  }
}

// Branch-scoped object add, the inverse of removeObjectFromScene: the
// server pulls the template into the instance dir (if needed) and
// appends a cast_add edit on the active leaf; mirror both locally so
// path replays from client memory keep it.
async function addObjectToScene(objectId) {
  try {
    await jfetch(`/api/conversations/${conversationId}/cast/${objectId}`, {
      method: "POST",
      body: JSON.stringify({}),
    });
  } catch (e) {
    flashError("Add failed: " + e.message);
    renderObjectsList();
    return;
  }
  if (!state.entities[objectId]) {
    try {
      const r = await jfetch(`/api/conversations/${conversationId}/entities`);
      state.entities = r.entities || {};
    } catch (_) {}
  }
  state.effectiveCastObjects.add(objectId);
  appendAppliedEditOnActiveLeaf({ kind: "cast_add", ok: true, id: objectId });
  renderCastList();
}

function renderCastList() {
  renderPersonaResponderDropdowns();
  renderObjectsList();
  const ul = document.querySelector(".cast-list");
  if (!ul) return;
  ul.innerHTML = "";

  // The `user` instance entity is rendered inline below as a regular
  // cast row (with a "(you)" badge), so no separate placeholder is
  // needed. If the conversation predates the user-as-instance work
  // and has no `user` entity, fall back to the legacy placeholder.
  const userPersona = state.conversation.settings?.user_persona || {};
  const userCardId = userPersona.card_id || null;
  if (!state.entities?.user) {
    const userName = (userPersona.name || "").trim() || "You";
    const userLi = document.createElement("li");
    userLi.dataset.userCard = "1";
    const userOpen = document.createElement("button");
    userOpen.type = "button";
    userOpen.className = "cast-open";
    userOpen.title = "Edit your persona (name + description shown to characters)";
    const userAv = document.createElement("span");
    userAv.className = "avatar small placeholder user";
    userAv.textContent = userName[0].toUpperCase();
    userOpen.appendChild(userAv);
    const userLabel = document.createElement("span");
    userLabel.className = "cast-name";
    userLabel.textContent = userName;
    userOpen.appendChild(userLabel);
    userOpen.addEventListener("click", openUserPersonaEditor);
    userLi.appendChild(userOpen);
    ul.appendChild(userLi);
  }

  // Render-scoped lookups. Rooms and outfits both come from the FULL
  // library (global templates ∪ instance entities, instance name
  // winning) so a character can be swapped to any of either — the
  // /outfit endpoint auto-copies outfit templates on first use, and
  // moveCastCharacter instances a global-only room through the cast
  // endpoint before committing the move.
  const roomById = new Map();
  for (const r of window.GEMMASIM_INITIAL?.global_rooms || []) {
    if (r?.id) roomById.set(r.id, r.name || r.id);
  }
  for (const r of Object.values(state.entities)) {
    if (r && r.type === "room") roomById.set(r.id, r.name || r.id);
  }
  const allRooms = [...roomById.entries()]
    .map(([id, name]) => ({ id, name }))
    .sort((a, b) => a.name.localeCompare(b.name));
  const instanceOutfits = Object.fromEntries(
    Object.values(state.entities)
      .filter((x) => x.type === "outfit")
      .map((o) => [o.id, o])
  );
  // Enriched catalog (staging shape: is_accessory / under / slots /
  // partial_label / owner). Instance name overrides the template's so
  // a customized outfit shows its own label.
  const allOutfits = (window.GEMMASIM_INITIAL?.global_outfits || []).map((o) => {
    const inst = instanceOutfits[o.id];
    return inst ? { ...o, name: inst.name || inst.id } : o;
  });
  const outfitById = new Map(allOutfits.map((o) => [o.id, o]));

  for (const [eid, e] of Object.entries(state.entities)) {
    if (e.type !== "character") continue;
    // Branch-scoped cast: hide characters that aren't in this
    // branch's effective cast. The shared instance pool may contain
    // chars added on a sibling branch; they don't belong here.
    // The `user` persona row is always shown (it's the player).
    if (eid !== "user" && !state.effectiveCastChars.has(eid)) continue;
    // Hidden-from-player cast members drop out of the roster unless the GM-view
    // "Show hidden cast" toggle is on (then they show with a "hidden" badge).
    if (eid !== "user" && castHiddenFromPlayer(e)) continue;
    const isPersona = eid === "user" || eid === userCardId;
    const revealedHidden = eid !== "user" && !!(e.properties && (e.properties.hidden_from_player || e.properties.stealthed));
    const li = document.createElement("li");
    li.dataset.characterId = eid;
    if (isPersona) li.dataset.userPersona = "1";
    if (revealedHidden) li.dataset.hiddenCast = "1";

    // Top strip: [− collapse] [avatar/name button] [× remove].
    // The minus minimizes this character's control sections (Location /
    // Clothing / Images); the remove moved to the right end of the row.
    const top = document.createElement("div");
    top.className = "cast-row-top";

    const collapsed = state.castCharCollapsed.has(eid);
    const collapse = document.createElement("button");
    collapse.type = "button";
    collapse.className = "ghost xs cast-collapse";
    collapse.textContent = collapsed ? "+" : "−";
    collapse.title = collapsed
      ? "Show this character's controls"
      : "Minimize this character's controls";
    top.appendChild(collapse);

    const open = document.createElement("button");
    open.type = "button";
    open.className = "cast-open";
    open.dataset.castOpen = eid;
    open.title = `Edit ${e.name || eid} (this conversation only)`;
    const img = document.createElement("img");
    img.className = "avatar small";
    img.src = `/portraits/${encodeURIComponent(eid)}`;
    img.alt = e.name || eid;
    img.loading = "lazy";
    img.addEventListener("error", () => {
      const span = document.createElement("span");
      span.className = "avatar small placeholder";
      span.textContent = (e.name || eid)[0].toUpperCase();
      img.replaceWith(span);
    });
    open.appendChild(img);
    const name = document.createElement("span");
    name.className = "cast-name";
    name.textContent = (e.name || eid) + (isPersona ? " (you)" : "");
    open.appendChild(name);
    if (revealedHidden) {
      const badge = document.createElement("span");
      badge.className = "cast-hidden-badge muted small";
      badge.textContent = "hidden";
      badge.title = "Hidden from the player — revealed by the Show hidden cast toggle.";
      open.appendChild(badge);
    }
    top.appendChild(open);

    const rm = document.createElement("button");
    rm.type = "button";
    rm.className = "ghost xs cast-rm";
    rm.dataset.castRm = eid;
    rm.title = "Remove from this conversation (their messages stay)";
    rm.textContent = "×";
    top.appendChild(rm);
    li.appendChild(top);

    // Control sections live in one wrapper so the minus can fold them
    // all away; collapsed state survives renderCastList rebuilds.
    const charBody = document.createElement("div");
    charBody.className = "cast-char-body";
    charBody.hidden = collapsed;
    collapse.addEventListener("click", () => {
      const isCollapsed = state.castCharCollapsed.has(eid);
      if (isCollapsed) state.castCharCollapsed.delete(eid);
      else state.castCharCollapsed.add(eid);
      charBody.hidden = !isCollapsed;
      collapse.textContent = isCollapsed ? "−" : "+";
      collapse.title = isCollapsed
        ? "Minimize this character's controls"
        : "Show this character's controls";
    });

    // Location: same inline-collapsible shape as Clothing — full panel
    // width, searchable room list (any room; global-only rooms are
    // instanced on first move).
    charBody.appendChild(buildCastLocationSection(eid, allRooms));

    // Clothing: a staging-style collapsible — primary-outfit list,
    // per-slot toggles, and (when any exist) an accessories
    // multi-select. Expands inline; no popups.
    charBody.appendChild(buildCastClothingSection(eid, e, outfitById));

    // Images: per-pack enable checkboxes (+ All / Default), only for
    // characters that declare image packs.
    const imagesSection = buildCastImagesControl(eid, e);
    if (imagesSection) charBody.appendChild(imagesSection);

    li.appendChild(charBody);
    ul.appendChild(li);
  }
}

// Collapsed-character memory for the cast list's per-character minus.
state.castCharCollapsed = state.castCharCollapsed || new Set();

// Location as an inline collapsible (same shape as Clothing): the
// summary reads out the current room; the body is the searchable
// full-width room list. "None" parks the character off-scene.
function buildCastLocationSection(eid, allRooms) {
  const branchRoom = currentRoomFor(eid);
  const { details, body, readout } = buildCastSection(`${eid}:location`, "Location");
  readout.textContent =
    allRooms.find((r) => r.id === branchRoom)?.name || branchRoom || "None";
  const items = [
    { id: "__null__", name: "— None (off-scene) —" },
    ...allRooms,
  ];
  const widget = buildOutfitListWidget("Room", items, {
    mode: "single",
    isSelected: (id) => (currentRoomFor(eid) || "__null__") === id,
    onPick: (id) => {
      if (id !== (currentRoomFor(eid) || "__null__")) moveCastCharacter(eid, id);
    },
    emptyText: "(no rooms)",
  });
  body.appendChild(widget.wrap);
  return details;
}

// A left-panel inline collapsible used by the Clothing + Images cast
// controls. Returns { details, body, summary } — the caller fills the
// body (which expands in document flow, not as a popup) and updates the
// summary read-out. Open-state survives renderCastList rebuilds via the
// per-(char,kind) key in state.castSectionOpen.
state.castSectionOpen = state.castSectionOpen || new Set();
// Image-state categories default OPEN; this remembers the ones the user folded.
state.castImgCatClosed = state.castImgCatClosed || new Set();
function buildCastSection(openKey, title) {
  const details = document.createElement("details");
  details.className = "cast-section";
  if (state.castSectionOpen.has(openKey)) details.open = true;
  details.addEventListener("toggle", () => {
    if (details.open) state.castSectionOpen.add(openKey);
    else state.castSectionOpen.delete(openKey);
  });
  const summary = document.createElement("summary");
  summary.className = "cast-section-summary";
  const titleSpan = document.createElement("span");
  titleSpan.className = "cast-section-title";
  titleSpan.textContent = title;
  const readout = document.createElement("span");
  readout.className = "cast-section-readout";
  summary.append(titleSpan, readout);
  details.appendChild(summary);
  const body = document.createElement("div");
  body.className = "cast-section-body";
  details.appendChild(body);
  return { details, body, readout };
}

// Build the per-character outfit catalog from the enriched global list:
// outfits the character owns (explicit properties.outfits, then
// owner-match) first, then every generic outfit. Mirrors
// setups.outfits_for server-side so the cast Clothing list matches what
// the staging panel would show for the same character.
function outfitCatalogFor(charEntity, outfitById) {
  const props = charEntity.properties || {};
  const ownerIds = new Set(
    [charEntity.id, charEntity._template_id]
      .filter((x) => typeof x === "string" && x)
      .map((s) => s.toLowerCase())
  );
  const out = [];
  const seen = new Set();
  for (const oid of props.outfits || []) {
    const o = outfitById.get(oid);
    if (o && !seen.has(oid)) { out.push(o); seen.add(oid); }
  }
  for (const o of outfitById.values()) {
    if (seen.has(o.id)) continue;
    if (ownerIds.has((o.owner || "").toLowerCase())) { out.push(o); seen.add(o.id); }
  }
  for (const o of outfitById.values()) {
    if (seen.has(o.id)) continue;
    if (o.generic) { out.push(o); seen.add(o.id); }
  }
  return out;
}

function buildCastClothingSection(eid, e, outfitById) {
  const branchOutfit = currentOutfitFor(eid);
  const catalog = outfitCatalogFor(e, outfitById);
  const primary = catalog.filter((o) => !o.is_accessory);
  const accessories = catalog.filter((o) => o.is_accessory);

  const { details, body, readout } = buildCastSection(`${eid}:clothing`, "Clothing");
  const curName = outfitById.get(branchOutfit)?.name || branchOutfit || "—";
  readout.textContent = curName;

  // ---- Primary outfit (single-select list + search) ----
  const outfitWidget = buildOutfitListWidget("Outfit", primary, {
    mode: "single",
    isSelected: (id) => id === currentOutfitFor(eid),
    onPick: (id) => { if (id !== currentOutfitFor(eid)) changeCastOutfit(eid, id); },
    emptyText: "(no outfits available)",
  });
  body.appendChild(outfitWidget.wrap);

  // ---- Clothing slots (per-slot On / Half off / Off cycle) ----
  // Defaults: the branch's clothing_overrides, else the current
  // outfit's clothing_slots, else On. Each toggle commits a
  // branch-scoped clothing_overrides patch (translated to v2 worn
  // states at render via apply_v1_overrides_to_worn).
  const slotsLabel = document.createElement("div");
  slotsLabel.className = "scenario-staging-list-label";
  slotsLabel.textContent = "Clothing";
  body.appendChild(slotsLabel);
  const slotsBox = document.createElement("div");
  slotsBox.className = "cast-slot-btns";
  body.appendChild(slotsBox);
  const overrides = (e.properties || {}).clothing_overrides || {};
  const outfitSlots = outfitById.get(branchOutfit)?.clothing_slots || {};
  const partialLabel = outfitById.get(branchOutfit)?.partial_label || null;
  const labelFor = (n) => (n === 2 && partialLabel ? partialLabel : (SCENE_SLOT_LABEL[n] || "On"));
  for (const slot of SCENE_SLOT_ORDER) {
    let cur = overrides[slot] ?? ((outfitSlots[slot] | 0) || 1);
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "ghost xs scenario-staging-slot-btn";
    const render = () => { btn.textContent = `${slot}: ${labelFor(cur)}`; };
    render();
    btn.addEventListener("click", () => {
      cur = SCENE_SLOT_CYCLE[cur] || 1;
      render();
      changeCastSlot(eid, slot, cur);
    });
    slotsBox.appendChild(btn);
  }

  // ---- Accessories (multi-select; only when any exist) ----
  if (accessories.length) {
    const accWidget = buildOutfitListWidget("Accessories", accessories, {
      mode: "multi",
      isSelected: (id) => ((e.properties || {}).accessories || []).includes(id),
      onToggle: (id, sel) => {
        const cur = new Set((e.properties || {}).accessories || []);
        if (sel) cur.add(id); else cur.delete(id);
        changeCastAccessories(eid, [...cur]);
        accWidget.refresh();
      },
      skipId: () => currentOutfitFor(eid) || null,
      emptyText: "(no accessories available)",
    });
    body.appendChild(accWidget.wrap);
  }

  return details;
}

// Re-pick the composed sprite for the newest character message on the
// active path after a branch-scoped wardrobe change (per-slot clothing
// or accessories). The studio patch lands as a kind=patch overlay on the
// active leaf (see _append_studio_edit_to_leaf) and _resolve_sprite_state
// computes effective state AT the message id — so re-picking the
// active-leaf character message reflects the change immediately. Without
// this, a slot toggle updated state silently and the on-screen image
// never moved until the next generated turn. Restricted to combined-format
// speakers so a wardrobe toggle never spends a model call on a tagged
// (catalog) character. Clears the existing pick first because
// maybePickImagePack early-returns when one is already present.
async function repickSprite(msg) {
  if (!msg || msg.persona === "user" || msg.persona === "narrator") return;
  const e = state.entities?.[msg.speaker_id];
  const fmt = ((e?.properties?.images || {}).format || "").toLowerCase();
  if (fmt !== "combined") return;
  if (msg.metadata) delete msg.metadata.image_pack_pick;
  delete state.imagePackPicks[msg.id];
  try { await maybePickImagePack(msg); }
  catch (err) { console.warn("sprite re-pick failed:", err); }
}

// Walk the active path from the leaf upward to the newest character
// message spoken by `eid`. Used to refresh a composed sprite after a
// baseline wardrobe mutation (outfit swap) whose change isn't tied to a
// single leaf edit.
function newestCharMessageOnPath(eid) {
  const msgs = state.conversation.messages || {};
  let id = state.conversation.active_path_leaf;
  while (id) {
    const m = msgs[id];
    if (!m) break;
    if (m.speaker_id === eid && m.persona !== "user" && m.persona !== "narrator") return m;
    id = m.parent_id;
  }
  return null;
}

// Per-slot clothing / accessories land as a kind=patch overlay on the
// active leaf, so re-pick the leaf itself when it's this character's turn.
function repickSpriteForWardrobeChange(eid) {
  const leafId = state.conversation.active_path_leaf;
  const msg = leafId ? state.conversation.messages[leafId] : null;
  if (msg && msg.speaker_id === eid) return repickSprite(msg);
}

// Branch-scoped per-slot clothing override (top/bra/etc → 1/2/3). Same
// studio-patch path as the image-pack toggle: appends a kind=patch
// overlay on the active leaf and mirrors it locally.
function changeCastSlot(eid, slot, value) {
  const patch = { properties: { clothing_overrides: { [slot]: value } } };
  jfetch(`/api/conversations/${conversationId}/entities/${eid}/patch`, {
    method: "POST",
    body: JSON.stringify(patch),
  }).then(() => {
    const entity = state.entities[eid];
    if (entity) clientDeepMerge(entity, patch);
    appendAppliedEditOnActiveLeaf({
      kind: "patch", ok: true, id: eid, data: patch, origin: "studio",
    });
    mirrorClothingNarratorState(eid, slot, value);
    const leafId = state.conversation.active_path_leaf;
    const leaf = leafId ? state.conversation.messages[leafId] : null;
    if (leaf) rerenderMessage(leaf);
    repickSpriteForWardrobeChange(eid);
  }).catch((err) => {
    flashError("Clothing change failed: " + err.message);
    renderCastList();
  });
}

// Branch-scoped accessories list (properties.accessories). Lists
// replace wholesale under deep-merge, so the full set is sent each time.
function changeCastAccessories(eid, ids) {
  const patch = { properties: { accessories: ids } };
  jfetch(`/api/conversations/${conversationId}/entities/${eid}/patch`, {
    method: "POST",
    body: JSON.stringify(patch),
  }).then(() => {
    const entity = state.entities[eid];
    if (entity) {
      entity.properties = entity.properties || {};
      entity.properties.accessories = ids;  // list replace, not merge
    }
    appendAppliedEditOnActiveLeaf({
      kind: "patch", ok: true, id: eid, data: patch, origin: "studio",
    });
    repickSpriteForWardrobeChange(eid);
  }).catch((err) => {
    flashError("Accessories change failed: " + err.message);
    renderCastList();
  });
}

// Inline persona-card picker, lives at the top of the Cast section.
// On change: POST /user-persona with the chosen card_id (or null for
// "default User"). The server auto-casts the picked character into the
// instance and stamps name/description into settings.user_persona; we
// mirror the response into local state and re-render the cast list so
// the new persona character shows up immediately.
// "Show hidden cast" — GM-view toggle to reveal hidden_from_player cast members.
const showHiddenCastToggle = document.getElementById("show-hidden-cast");
if (showHiddenCastToggle) {
  showHiddenCastToggle.checked = state.showHiddenCast;
  showHiddenCastToggle.addEventListener("change", () => {
    state.showHiddenCast = showHiddenCastToggle.checked;
    try { localStorage.setItem("gemmasim_show_hidden_cast", state.showHiddenCast ? "1" : "0"); } catch (_) {}
    renderCastList();
  });
}

const personaCardPicker = document.getElementById("persona-card-picker");
personaCardPicker?.addEventListener("change", async () => {
  const cardId = personaCardPicker.value || null;
  personaCardPicker.disabled = true;
  try {
    const res = await jfetch(
      `/api/conversations/${conversationId}/user-persona`,
      { method: "POST", body: JSON.stringify({ card_id: cardId }) }
    );
    if (res?.settings) {
      state.conversation.settings = res.settings;
    }
    if (res?.entities) {
      // Replace state.entities so the new persona character lands in
      // the cast list and dropdowns; existing rows whose data was
      // also re-saved server-side stay current.
      state.entities = res.entities;
    }
    fullRender();
    flashInfo(cardId
      ? `Persona set to ${state.conversation.settings.user_persona.name}.`
      : `Persona cleared.`);
  } catch (e) {
    flashError("Persona change failed: " + e.message);
    // Revert the dropdown to whatever the server thinks is correct.
    personaCardPicker.value = state.conversation.settings?.user_persona?.card_id || "";
  } finally {
    personaCardPicker.disabled = false;
  }
});

// Legacy persona dialog opener — still used by the cast row's user
// card click for users who want to tweak name/description manually.
function openUserPersonaEditor() {
  const dlg = document.getElementById("user-persona-dialog");
  if (!dlg) return;
  const persona = state.conversation.settings?.user_persona || {};
  const nameEl = document.getElementById("user-persona-name");
  const descEl = document.getElementById("user-persona-description");
  nameEl.value = persona.name || "";
  descEl.value = persona.description || "";
  dlg.returnValue = "";
  dlg.showModal();
  dlg.addEventListener("close", function onClose() {
    dlg.removeEventListener("close", onClose);
    if (dlg.returnValue !== "save") return;
    // Preserve card_id — the inline persona picker owns that field.
    const next = {
      ...persona,
      name: nameEl.value.trim() || "User",
      description: descEl.value,
    };
    state.conversation.settings = state.conversation.settings || {};
    state.conversation.settings.user_persona = next;
    renderCastList();
    jfetch(`/api/conversations/${conversationId}/settings`, {
      method: "PUT",
      body: JSON.stringify({ user_persona: next }),
    }).catch((e) => flashError("Save failed: " + e.message));
  });
}

// Listen for +/- events from the right-panel cast buttons.
window.addEventListener("gemmasim:cast-changed", () => {
  reloadInstanceEntities();
});

// Minimal navigation surface for sibling scripts (the map panel in
// map.js). Exposes the conversation id and a jump-to-leaf so a move made
// from the map re-renders the chat the same way an in-chat move does.
window.GemmaSimNav = {
  conversationId,
  goToLeaf: (leafId) => { if (leafId) setActiveLeaf(leafId); },
  reloadEntities: () => { try { reloadInstanceEntities(); } catch (_e) {} },
  // Insert a server-returned message into the local store BEFORE jumping to
  // it. Without this, setActiveLeaf points at a leaf the client hasn't
  // mirrored yet and the path walk renders an empty chat until reload —
  // the bug the map's follow/move buttons hit. Mirrors moveCastCharacter.
  applyServerMessage: (msg) => {
    if (!msg || !msg.id) return;
    state.conversation.messages[msg.id] = msg;
    setActiveLeaf(msg.id);
  },
};

// "Edit scenario" button in the left panel: opens the conversation's
// scenario in the right panel. Auto-enters scenario context so any
// subsequent +/- in the right-panel character/location lists targets
// the scenario template.
document.getElementById("edit-scenario-btn")?.addEventListener("click", (ev) => {
  const sid = ev.currentTarget.dataset.scenarioId;
  if (!sid) return;
  if (window.GemmaSimPanel?.openEntityById) {
    window.GemmaSimPanel.openEntityById(sid);
  }
});

// "Reset to scenario": wipes messages + running summary, reseeds root
// narrator + greetings from the scenario template. Instance edits are
// preserved. Useful when you've deleted things mid-chat and want a
// clean slate without losing your character tweaks.
// "+ Setup from directive": opens a dialog where the user types a
// natural-language directive ("Today is feature day, Iris in casual,
// I'm from the local paper"). Streams the response from the narrator,
// parses the [outfit] / [move] / [set] grammar + opening prose, and
// seeds a new sibling root setup. The user can then navigate to it
// via the existing root branch arrows.
document.getElementById("setup-from-directive-btn")?.addEventListener("click", () => {
  const dlg = document.getElementById("setup-directive-dialog");
  const ta = document.getElementById("setup-directive-text");
  const status = document.getElementById("setup-directive-status");
  const submit = document.getElementById("setup-directive-submit");
  if (!dlg || !ta || !status || !submit) return;
  ta.value = "";
  status.textContent = "";
  submit.disabled = false;
  dlg.showModal();
  // Submit handler is rebound per open so we can resolve cleanly.
  const onSubmit = async (ev) => {
    if (ev.submitter && ev.submitter.value === "cancel") return;
    ev.preventDefault();
    const directive = (ta.value || "").trim();
    if (!directive) {
      status.textContent = "Type something first.";
      return;
    }
    submit.disabled = true;
    status.textContent = "Streaming…";
    try {
      const resp = await fetch(
        `/api/conversations/${conversationId}/setups/from-directive`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ directive }),
        }
      );
      if (!resp.ok || !resp.body) {
        status.textContent = `Failed: HTTP ${resp.status}`;
        submit.disabled = false;
        return;
      }
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      let chars = 0;
      let result = null;
      let errored = null;
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf("\n\n")) !== -1) {
          const block = buf.slice(0, idx).trim();
          buf = buf.slice(idx + 2);
          if (!block || block.startsWith(":")) continue;
          const line = block.split("\n").find((l) => l.startsWith("data:"));
          if (!line) continue;
          let ev;
          try { ev = JSON.parse(line.slice(5).trim()); } catch { continue; }
          if (ev.type === "delta") {
            chars += (ev.content || "").length;
            status.textContent = `Streaming… ${chars} chars`;
          } else if (ev.type === "done") {
            result = ev;
          } else if (ev.type === "error") {
            errored = ev.error || "stream error";
          }
        }
      }
      if (errored) {
        status.textContent = "Error: " + errored;
        submit.disabled = false;
        return;
      }
      if (!result || !result.message) {
        status.textContent = "Narrator returned nothing usable.";
        submit.disabled = false;
        return;
      }
      // Pull the freshly-saved conversation so we have the new root +
      // any presence_snapshot / settings the route persisted.
      const fresh = await jfetch(`/api/conversations/${conversationId}`);
      state.conversation = fresh;
      // Switch to the new setup root so the user lands on it
      // immediately (their explicit ask was "I want to start with…").
      const newRootId = result.message.id;
      try {
        await jfetch(`/api/conversations/${conversationId}/active-leaf`, {
          method: "POST",
          body: JSON.stringify({ leaf_id: newRootId }),
        });
        // Re-fetch to pick up server-side setup-active flips + settings.
        state.conversation = await jfetch(`/api/conversations/${conversationId}`);
      } catch {}
      await reloadInstanceEntities();
      fullRender();
      renderQuickEdits();
      flashInfo(`New setup: ${result.name || "directive"}.`);
      dlg.close();
    } catch (e) {
      status.textContent = "Failed: " + e.message;
      submit.disabled = false;
    }
  };
  // Wire once per open so we don't stack handlers.
  const form = dlg.querySelector("form");
  form.addEventListener("submit", onSubmit, { once: true });
});

document.getElementById("reset-scene-btn")?.addEventListener("click", async () => {
  if (!(await confirmAction(
    "Reset to scenario? This wipes every message, the running summary, " +
    "and re-creates the opening narration + greetings from the scenario. " +
    "Per-conversation edits to characters / outfits stay intact."
  ))) return;
  try {
    const fresh = await jfetch(`/api/conversations/${conversationId}/reset-scene`, {
      method: "POST",
    });
    state.conversation = fresh;
    await reloadInstanceEntities();
    fullRender();
    renderQuickEdits();
    flashInfo("Scene reset.");
  } catch (e) { flashError("Reset failed: " + e.message); }
});

// Delegated click on the left-panel cast list: tapping the avatar/name
// opens the character's instanced card in the right panel. − removes
// the character from this conversation's instance (DELETE /cast/<id>)
// without touching the scenario or template. Past messages they sent
// stay; they just disappear from presence + the cast list.
document.addEventListener("click", (ev) => {
  const open = ev.target.closest("[data-cast-open]");
  if (!open) return;
  const id = open.dataset.castOpen;
  if (window.GemmaSimPanel?.openEntityById) {
    window.GemmaSimPanel.openEntityById(id);
  }
});

document.addEventListener("click", async (ev) => {
  const btn = ev.target.closest("[data-cast-rm]");
  if (!btn) return;
  const charId = btn.dataset.castRm;
  const e = state.entities[charId];
  const name = (e && e.name) || charId;
  if (!confirm(`Remove ${name} from this conversation? Their messages stay.`)) return;
  btn.disabled = true;
  try {
    await jfetch(`/api/conversations/${conversationId}/cast/${charId}`, { method: "DELETE" });
    // Branch-scoped removal: keep the shared entities mirror so a
    // sibling branch (where the char wasn't removed) still sees them
    // after a switch. Just drop them from this branch's cast set,
    // and mirror the cast_remove edit on the active leaf so future
    // path replays (sibling switch + return) keep the removal.
    state.effectiveCastChars.delete(charId);
    state.effectiveCastObjects.delete(charId);
    appendAppliedEditOnActiveLeaf({ kind: "cast_remove", ok: true, id: charId });
    renderCastList();
    renderPersonaResponderDropdowns();
    renderQuickEdits();
    flashInfo(`${name} left the scene.`);
  } catch (e) {
    flashError("Remove failed: " + e.message);
    btn.disabled = false;
  }
});

// Searchable combobox built on <input list> + <datalist>. Native
// <select> has type-to-jump but no substring filter; this gives the
// user a free-text typeahead. We map between option id and label
// ourselves so callers commit by id. Returns a wrapper <span> so the
// associated <datalist> sits next to its input as a sibling.
let _castComboCounter = 0;
function buildCastCombobox(options, currentId, placeholder, onCommit) {
  const wrap = document.createElement("span");
  wrap.className = "cast-state-wrap";
  const input = document.createElement("input");
  input.type = "text";
  input.className = "cast-state-select";
  input.placeholder = placeholder;
  input.title = placeholder;
  const dlId = `cast-combo-${++_castComboCounter}`;
  input.setAttribute("list", dlId);
  const dl = document.createElement("datalist");
  dl.id = dlId;
  for (const o of options) {
    dl.appendChild(new Option(o.label));
  }
  const labelById = (id) => options.find((o) => o.id === id)?.label || "";
  const initial = labelById(currentId);
  input.value = initial;
  input.addEventListener("change", () => {
    const v = input.value.trim();
    const match = options.find(
      (o) => o.label.toLowerCase() === v.toLowerCase() || o.id === v
    );
    if (match) {
      onCommit(match.id);
    } else {
      input.value = initial;  // silently revert unknown text
    }
  });
  // Clicking the field selects all so the user can immediately type to
  // filter the suggestions list without manually clearing.
  input.addEventListener("focus", () => input.select());
  wrap.append(input, dl);
  // Forward the disabled property the renderer might set after build.
  Object.defineProperty(wrap, "disabled", {
    set(v) { input.disabled = !!v; },
    get() { return input.disabled; },
  });
  return wrap;
}

// Cast-row inline state changes. Each posts a narrator message that
// updates the active leaf with a new presence_snapshot — same pipeline
// as a narrator-edit's [move ...] / [outfit ...] directive — and then
// hands the new leaf id to setActiveLeaf so the chat re-renders.
async function moveCastCharacter(charId, roomValue) {
  const roomId = roomValue === "__null__" ? null : roomValue || null;
  // Discover the owning location for the chosen room so the snapshot
  // carries both fields (prompts read presence.location for the
  // "surroundings" block). Global-only rooms aren't in state.entities
  // yet — the /move endpoint instances them server-side and fills in
  // the owner itself, so a null here is fine.
  let locationId = null;
  if (roomId) {
    const room = state.entities[roomId];
    locationId = room?.location || null;
    if (!locationId) {
      const owner = Object.values(state.entities).find(
        (e) => e.type === "location" && (e.children || []).includes(roomId)
      );
      locationId = owner ? owner.id : null;
    }
  }
  try {
    const res = await jfetch(`/api/conversations/${conversationId}/move`, {
      method: "POST",
      body: JSON.stringify({ character_id: charId, room_id: roomId, location_id: locationId }),
    });
    if (res?.message) {
      state.conversation.messages[res.message.id] = res.message;
      // A global-only room was instanced server-side during the move —
      // pull it into the local mirror so the surroundings/right panel
      // can resolve it without a page reload.
      if (roomId && !state.entities[roomId]) reloadInstanceEntities().catch(() => {});
      setActiveLeaf(res.message.id);
    }
  } catch (e) {
    flashError("Move failed: " + e.message);
    renderCastList();  // revert the dropdown to the persisted state
  }
}

async function changeCastOutfit(charId, outfitId) {
  if (!outfitId) return;
  try {
    const res = await jfetch(`/api/conversations/${conversationId}/outfit`, {
      method: "POST",
      body: JSON.stringify({ character_id: charId, outfit_id: outfitId }),
    });
    if (res?.message) {
      // The instance entity got mutated server-side; mirror locally so
      // the dropdown's selected value persists across renders. An
      // outfit swap is fresh-slate — path replay drops clothing_overrides
      // / clothing_transparency (effective._replay_edit), so clear the
      // local mirror too or the slot buttons would show stale flips.
      const e = state.entities[charId];
      if (e) {
        e.properties = e.properties || {};
        e.properties.current_outfit = outfitId;
        delete e.properties.clothing_overrides;
        delete e.properties.clothing_transparency;
      }
      // The swap now annotates the newest response IN PLACE (no new
      // branch): the server appended the outfit applied-edits + a
      // narrator_state block to the active leaf. Mirror it locally, then
      // re-render (shows the narrator block) and re-pick its composed
      // sprite so the image reflects the new outfit immediately.
      state.conversation.messages[res.message.id] = res.message;
      renderCastList();
      rerenderMessage(res.message);
      repickSprite(res.message);
    }
  } catch (e) {
    flashError("Outfit change failed: " + e.message);
    renderCastList();
  }
}

// Debounce active-leaf so rapid branch navigation only triggers one POST.
let activeLeafTimer = null;
function setActiveLeaf(leafId) {
  state.conversation.active_path_leaf = leafId;
  if (window.Modules && Modules._fireNavigate) Modules._fireNavigate(leafId);
  recordBranchChoicePath(leafId);
  // Recompute branch cast synchronously so renderCastList +
  // renderPersonaResponderDropdowns inside fullRender see the new
  // branch's set immediately. The debounced /active-leaf POST below
  // still runs (server-side mirroring + sanity), but the UI no
  // longer waits for it.
  applyEffectiveCastForLeaf(leafId);
  renderPersonaResponderDropdowns();
  // Resolve Reply-as for the new branch: sticky pick first, then
  // last-character-on-path (best-effort client-side derivation),
  // then narrator. The server's authoritative default lands later
  // via the POST response and only takes effect if no sticky.
  const localDefault = clientDefaultResponderForLeaf(leafId);
  setResponderProgrammatically(chooseResponderDefault({ serverDefault: localDefault }));
  fullRender();
  clearTimeout(activeLeafTimer);
  activeLeafTimer = setTimeout(async () => {
    try {
      const res = await jfetch(
        `/api/conversations/${conversationId}/active-leaf`,
        {
          method: "POST",
          body: JSON.stringify({ leaf_id: leafId }),
        }
      );
      // The server re-mirrors the active setup's user_persona and
      // scenario_instructions into settings whenever the new leaf
      // crosses a setup root. Patch the cached settings in place so
      // persona chips, labels, and the active-setup-keyed sticky
      // responder all reflect the new sub-scenario. Bail if the
      // user has since switched to yet another leaf — only the most
      // recent selection matters.
      if (res && state.conversation.active_path_leaf === leafId) {
        if (res.settings) {
          state.conversation.settings = {
            ...(state.conversation.settings || {}),
            ...res.settings,
          };
        }
        // Reseat the branch's setup-root id (the localStorage key
        // for sticky responder is keyed off this) and re-resolve
        // the responder against the server's authoritative default.
        // The synchronous path replay already set effectiveCast +
        // dropdowns; only re-pick the responder if our local guess
        // differs from the server's, AND the user hasn't manually
        // changed the dropdown since this POST went out.
        const newSetupRootId = res.active_setup_root_id || "";
        const setupChanged = newSetupRootId !== state.activeSetupRootId;
        state.activeSetupRootId = newSetupRootId;
        if (setupChanged) {
          // Crossed a setup root — re-resolve sticky from the new
          // branch's bucket, falling back to the server's default.
          const serverDefault = (res.default_responder || "").trim();
          setResponderProgrammatically(chooseResponderDefault({ serverDefault }));
          // The new branch may have a different module mix; refresh
          // toolbar + left-panel sections and drop any pending
          // autoplay countdown so it doesn't fire on a sibling.
          if (typeof refreshModulesUI === "function") {
            cancelAutoplayCountdown();
            refreshModulesUI();
          }
        }
        responderSelect.dataset.defaultResponder = (res.default_responder || "").trim();
      }
    } catch (e) {
      console.warn("active-leaf save failed", e);
    }
  }, 350);
}

// Mirrors the server's record_branch_choice_path: walks leaf → root and
// stores {parent_id: child_on_path} for each fork. Keeps the local memo
// in sync between the optimistic UI update and the debounced POST.
function recordBranchChoicePath(leafId) {
  const msgs = state.conversation.messages;
  const choices = (state.conversation.branch_choices ||= {});
  const seen = new Set();
  let cur = leafId;
  while (cur && !seen.has(cur)) {
    seen.add(cur);
    const m = msgs[cur];
    if (!m) break;
    if (m.parent_id) choices[m.parent_id] = cur;
    cur = m.parent_id;
  }
}

// ---------------------------------------------------------------------------
// Persona/responder
// ---------------------------------------------------------------------------

function characterIds() {
  // Branch-scoped: only chars that are actually in this branch's cast.
  // The shared instance pool may carry sibling-branch additions; those
  // shouldn't show up in Speak-as / Reply-as.
  return Object.entries(state.entities)
    .filter(([id, e]) => e.type === "character" && (id === "user" || state.effectiveCastChars.has(id)))
    .map(([id]) => id);
}

// Rebuild the Speak-as + Reply-as dropdown options from the current
// effective cast. Server-rendered initially via Jinja; called again on
// branch switch so the options track the new branch's cast. Preserves
// the user's current selection when possible (or falls back to the
// branch's default responder).
let _personaAutoDefaulted = false;   // one-shot: default Speak-as to the player role
function renderPersonaResponderDropdowns() {
  const personaSel = document.getElementById("persona-select");
  const responderSel = document.getElementById("responder-select");
  if (!personaSel || !responderSel) return;
  const charsInCast = Object.entries(state.entities)
    .filter(([id, e]) => e.type === "character" && id !== "user"
      && state.effectiveCastChars.has(id) && !castHiddenFromPlayer(e))
    .map(([id, e]) => ({ id, name: e.name || id, player: (e.tags || []).includes("player") }));
  charsInCast.sort((a, b) => a.name.localeCompare(b.name));
  // A "player" character is a role the human plays (speak-as), not an AI voice —
  // keep them out of the Reply-as list so the model never generates their turns.
  const respondersInCast = charsInCast.filter((c) => !c.player);

  const prevPersona = personaSel.value;
  personaSel.innerHTML = "";
  personaSel.appendChild(new Option("User (you)", "user"));
  personaSel.appendChild(new Option("Narrator", "narrator"));
  // Life Sim: surface the "Goal" pseudo-persona when the module is
  // active for this branch. Sending a message as Goal doesn't post a
  // chat turn — it appends the text to the focal character's
  // properties.goals list via /life_sim/goal.
  if (typeof lifeSimActiveOnBranch === "function" && lifeSimActiveOnBranch()) {
    personaSel.appendChild(new Option("Goal", "goal"));
  }
  for (const c of charsInCast) personaSel.appendChild(new Option(c.name, c.id));
  // Default Speak-as to a player-role character when the scenario has one (the
  // human plays them) — but only ONCE, on first population, so a player-tagged
  // cast member (e.g. Alex the rogue) is the human's voice out of the box and
  // composed actions roll on that character's sheet without a manual switch.
  // After that we preserve the user's sticky pick across branch-switch rebuilds.
  const playerChar = charsInCast.find((c) => c.player);
  if (!_personaAutoDefaulted && playerChar) {
    personaSel.value = playerChar.id;
    _personaAutoDefaulted = true;
  } else if ([...personaSel.options].some((o) => o.value === prevPersona)) {
    personaSel.value = prevPersona;
  } else {
    personaSel.value = playerChar ? playerChar.id : "user";
  }

  const prevResponder = responderSel.value;
  responderSel.innerHTML = "";
  responderSel.appendChild(new Option("Narrator", "narrator"));
  for (const c of respondersInCast) responderSel.appendChild(new Option(c.name, c.id));
  // Re-seat the dropdown without firing a change-handler that would
  // overwrite the user's sticky pick: rebuilding options counts as
  // programmatic, not user intent.
  if ([...responderSel.options].some((o) => o.value === prevResponder)) {
    setResponderProgrammatically(prevResponder);
  } else {
    setResponderProgrammatically("narrator");
  }
  syncComposerSpeak();
}

// Composer "Speak as / to" mirror controls. The side-panel persona-select
// (speak-as) and responder-select (reply-as) stay the source of truth; the
// composer dropdowns mirror their options + value and drive them on change,
// and the ⇄ button swaps the two (guarding for options that aren't valid in
// the other select — e.g. "User"/"Goal" aren't reply-as options).
function syncComposerSpeak() {
  const clone = (dst, src) => {
    if (!dst || !src) return;
    dst.innerHTML = "";
    for (const o of src.options) dst.appendChild(new Option(o.text, o.value));
    dst.value = src.value;
  };
  clone(composerPersonaSelect, personaSelect);
  clone(composerResponderSelect, responderSelect);
}
if (composerPersonaSelect) {
  composerPersonaSelect.addEventListener("change", () => {
    personaSelect.value = composerPersonaSelect.value;
    personaSelect.dispatchEvent(new Event("change"));
    syncComposerSpeak();
  });
}
if (composerResponderSelect) {
  composerResponderSelect.addEventListener("change", () => {
    responderSelect.value = composerResponderSelect.value;
    responderSelect.dispatchEvent(new Event("change"));
    syncComposerSpeak();
  });
}
if (composerSpeakSwap) {
  composerSpeakSwap.addEventListener("click", () => {
    const oldAs = personaSelect.value;
    const oldTo = responderSelect.value;
    // New speak-as = old reply-to (narrator/char — always a valid persona).
    if ([...personaSelect.options].some((o) => o.value === oldTo)) {
      personaSelect.value = oldTo;
    }
    // New reply-to = old speak-as if it's a valid responder, else a default.
    if ([...responderSelect.options].some((o) => o.value === oldAs)) {
      responderSelect.value = oldAs;
    } else {
      responderSelect.value = pickDefaultResponder();
    }
    personaSelect.dispatchEvent(new Event("change"));
    responderSelect.dispatchEvent(new Event("change"));
    syncComposerSpeak();
  });
}
function pickDefaultResponder() {
  const chars = characterIds();
  const me = personaSelect.value;
  const pool = chars.filter((c) => c !== me);
  if (pool.length) return pool[0];
  if (chars.length) return chars[0];
  return "narrator";
}
function updateAsLabel() {
  const v = personaSelect.value;
  composerAs.textContent = v === "user" ? `as ${userPersonaName()}`
    : v === "narrator" ? "as Narrator"
    : `as ${entityName(v)}`;
}
personaSelect.addEventListener("change", () => {
  updateAsLabel();
  if (responderSelect.value === personaSelect.value && personaSelect.value !== "user") {
    responderSelect.value = pickDefaultResponder();
  }
});
// Per-branch sticky Reply-as: a manual pick wins over the path-
// derived default until the user picks again. Keyed by setup root id
// so different branches remember their own. Stored in localStorage
// so it survives page refreshes (the path-derived default would
// otherwise overwrite a manual pick on every reload).
function responderStickyKey(setupRootId) {
  const sid = setupRootId || state.activeSetupRootId || "";
  return `gemmasim:responder:${conversationId}:${sid}`;
}
function readStickyResponder(setupRootId) {
  try { return localStorage.getItem(responderStickyKey(setupRootId)) || ""; }
  catch (_) { return ""; }
}
function writeStickyResponder(value, setupRootId) {
  try {
    if (value) localStorage.setItem(responderStickyKey(setupRootId), value);
    else localStorage.removeItem(responderStickyKey(setupRootId));
  } catch (_) {}
}

function chooseResponderDefault({ serverDefault } = {}) {
  // Sticky manual pick beats path-derived default. Falls back to the
  // server-computed last-character-on-path, then to pickDefaultResponder
  // for branches with no character history.
  const sticky = readStickyResponder();
  const candidates = [sticky, serverDefault, pickDefaultResponder()].filter(Boolean);
  for (const c of candidates) {
    if ([...responderSelect.options].some((o) => o.value === c)) return c;
  }
  return responderSelect.options[0]?.value || "narrator";
}

function initResponder() {
  const serverDefault = (
    responderSelect.dataset.defaultResponder
    || window.GEMMASIM_INITIAL?.default_responder
    || ""
  ).trim();
  setResponderProgrammatically(chooseResponderDefault({ serverDefault }));
}

// Programmatic responder updates (init, branch switch, dropdown
// rebuild) should not be persisted as the user's sticky pick. Wrap
// the assignment so genuine user `change` events are the only thing
// that hits localStorage.
let _responderProgrammatic = 0;
function setResponderProgrammatically(value) {
  _responderProgrammatic++;
  try { responderSelect.value = value; }
  finally { _responderProgrammatic--; }
}
responderSelect.addEventListener("change", () => {
  if (_responderProgrammatic > 0) return;
  writeStickyResponder(responderSelect.value);
  // Symmetric collision-breaker — mirrors the personaSelect change
  // handler earlier in this file. When the user manually changes
  // Reply-as to match the currently-selected Speak-as (and that
  // Speak-as is a character, not "user"), bounce Speak-as back to
  // "user" so the next submit fires a fresh NPC reaction instead
  // of asking the just-spoken character to continue itself. Without
  // this, the prompt ends with two consecutive assistant turns by
  // the same speaker → model EOSes → empty completion → bubble
  // disappears → "no response at all" symptom.
  if (responderSelect.value === personaSelect.value && personaSelect.value !== "user") {
    personaSelect.value = "user";
    if (typeof updateAsLabel === "function") updateAsLabel();
  }
});

// ---------------------------------------------------------------------------
// Composer
// ---------------------------------------------------------------------------

composerInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    composer.requestSubmit();
  }
});

function setSendButtonStop(isStreaming) {
  if (isStreaming) {
    sendBtn.dataset.mode = "stop";
    sendBtn.textContent = "Stop";
    sendBtn.classList.remove("primary");
    sendBtn.classList.add("danger");
  } else {
    sendBtn.dataset.mode = "send";
    sendBtn.textContent = "Send";
    sendBtn.classList.add("primary");
    sendBtn.classList.remove("danger");
  }
}

async function handleSlash(content) {
  // Returns true if the input was a slash command (and was handled).
  const m = content.match(/^\/(\w+)\s*(.*)$/);
  if (!m) return false;
  const [, cmd, arg] = m;
  switch (cmd.toLowerCase()) {
    case "help":
      flashInfo(
        "Slash commands:\n" +
        "  /help — show this list\n" +
        "  /sys <text> — append to per-conversation system instructions\n" +
        "  /oc <text> — post an out-of-character note as the user\n" +
        "  /retry — regenerate the last assistant message\n" +
        "  /continue — extend the last assistant message\n" +
        "  /checkpoint [label] — set the Return-by-Death save point here\n" +
        "  /die [text] — Return by Death: rewind to the save point\n" +
        "  /reveal [text] — try to tell someone about the loop (the Witch won't allow it)\n" +
        "  /help [text] — cry out for help; who hears is a Perception check (pf1e)"
      );
      composerInput.value = "";
      return true;
    case "sys":
      if (!arg.trim()) return flashError("/sys needs text"), composerInput.value = "", true;
      try {
        await jfetch(`/api/conversations/${conversationId}/settings`, {
          method: "PUT",
          body: JSON.stringify({
            dev_panel_instructions:
              ((state.conversation.settings.dev_panel_instructions || "") + "\n" + arg).trim(),
          }),
        });
        state.conversation.settings.dev_panel_instructions =
          ((state.conversation.settings.dev_panel_instructions || "") + "\n" + arg).trim();
        flashInfo("System instruction added.");
      } catch (e) { flashError("Failed: " + e.message); }
      composerInput.value = "";
      return true;
    case "oc": {
      const note = `((OOC: ${arg.trim()}))`;
      composerInput.value = note;
      return false; // fall through, post normally
    }
    case "checkpoint": {
      // Return by Death: mark the current moment as the save point.
      try {
        const res = await jfetch(`/api/conversations/${conversationId}/rbd/checkpoint`, {
          method: "POST",
          body: JSON.stringify({ label: arg.trim() }),
        });
        const lbl = (res?.checkpoint && res.checkpoint.label) || "Save point";
        flashInfo(`Save point set: ${lbl}.`);
      } catch (e) {
        flashError("Failed: " + (e.message || e) + " (is Return by Death active for this scene?)");
      }
      composerInput.value = "";
      return true;
    }
    case "die": {
      // Return by Death: rewind to the save point; the world resets, you
      // keep what you learned. The tree branches, so reload the view.
      try {
        await jfetch(`/api/conversations/${conversationId}/rbd/die`, {
          method: "POST",
          body: JSON.stringify({ narrator_text: arg.trim() }),
        });
        composerInput.value = "";
        if (typeof reloadConversation === "function") await reloadConversation();
        flashInfo("Return by Death — the world resets; you remember.");
      } catch (e) {
        flashError("Failed: " + (e.message || e) + " (is Return by Death active for this scene?)");
        composerInput.value = "";
      }
      return true;
    }
    case "help": {
      // Cry out for help. The rules (pf1e) decide who's in earshot via a
      // Perception check and who comes; the tree may change, so reload.
      try {
        const res = await jfetch(`/api/conversations/${conversationId}/pf1e/help`, {
          method: "POST",
          body: JSON.stringify({ text: arg.trim() }),
        });
        composerInput.value = "";
        if (typeof reloadConversation === "function") await reloadConversation();
        if (res && res.answered) {
          flashInfo("Help heard you — someone is coming.");
        } else if (res) {
          flashInfo("Your cry goes unanswered.");
        }
      } catch (e) {
        flashError("Failed: " + (e.message || e) + " (is Pathfinder active for this scene?)");
        composerInput.value = "";
      }
      return true;
    }
    case "reveal": {
      // Try to tell someone present about the loop. Under the Witch's
      // constraint the rules block it and the words never reach them.
      try {
        const res = await jfetch(`/api/conversations/${conversationId}/rbd/reveal`, {
          method: "POST",
          body: JSON.stringify({ text: arg.trim() }),
        });
        composerInput.value = "";
        if (typeof reloadConversation === "function") await reloadConversation();
        if (res && res.blocked) {
          flashInfo("The words die in your throat. No one else can know.");
        }
      } catch (e) {
        flashError("Failed: " + (e.message || e) + " (is Return by Death active for this scene?)");
        composerInput.value = "";
      }
      return true;
    }
    case "retry": {
      // Find last assistant message in the active path; regen it.
      const path = pathToLeaf(state.conversation.active_path_leaf);
      for (let i = path.length - 1; i >= 0; i--) {
        if (path[i].persona !== "user") {
          composerInput.value = "";
          await regenerate(path[i]);
          return true;
        }
      }
      flashError("No assistant message to retry.");
      composerInput.value = "";
      return true;
    }
    case "continue": {
      const path = pathToLeaf(state.conversation.active_path_leaf);
      for (let i = path.length - 1; i >= 0; i--) {
        if (path[i].persona !== "user") {
          composerInput.value = "";
          await continueMessage(path[i]);
          return true;
        }
      }
      flashError("No assistant message to continue.");
      composerInput.value = "";
      return true;
    }
    default:
      flashError(`Unknown command: /${cmd}. Type /help for the list.`);
      composerInput.value = "";
      return true;
  }
}

composer.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (state.generating) {
    if (state.abortController) state.abortController.abort();
    return;
  }
  const rawContent = composerInput.value;
  if (await handleSlash(rawContent)) return;

  const content = composerInput.value.trim();
  const persona = personaSelect.value;

  // Life Sim "Goal" pseudo-persona: don't post a chat turn. Append
  // the typed text to the focal NPC's goals list via /life_sim/goal.
  // Focal = Reply-as dropdown selection (must be a same-room NPC,
  // which the Speak-as gate enforces by only listing the option
  // when life_sim is active for this branch).
  if (persona === "goal") {
    if (!content) return;
    const focal = responderSelect.value;
    if (!focal || focal === "narrator") {
      flashError("Pick a character in Reply-as to receive the goal.");
      return;
    }
    try {
      const res = await jfetch(
        `/api/conversations/${conversationId}/life_sim/goal`,
        {
          method: "POST",
          body: JSON.stringify({ character_id: focal, goal: content }),
        },
      );
      if (res?.message) {
        appendMessage(res.message);
      }
      composerInput.value = "";
      updateTokenEstimate();
    } catch (err) {
      flashError("Failed to add goal: " + err.message);
    }
    return;
  }

  let responder = responderSelect.value || pickDefaultResponder();
  if (content) {
    const mentioned = pickResponderFromMention(content);
    if (mentioned) {
      // Respect explicit user picks. When the user has manually set
      // the responder via the dropdown (the sticky-pick mechanism
      // records this in localStorage), the @-mention picker should
      // NOT override — otherwise typing "Hi Iris" as Kevin while
      // the user explicitly picked Dex as responder still pulls
      // Iris in, which is the "only Iris ever responds" bug
      // the user reported.
      //
      // The sticky pick wins over auto-mention; auto-mention only
      // overrides when the responder is at its DEFAULT (no manual
      // pick on this branch). User can clear the sticky by either
      // not picking on this branch or by picking Narrator (which
      // writeStickyResponder clears for sentinel values too — but
      // typing the dropdown change writes a sticky no matter what).
      const sticky = readStickyResponder();
      const userExplicitlyPicked = sticky && sticky === responder;
      if (userExplicitlyPicked) {
        // Keep user's pick. The mention is informational.
      } else {
        responder = mentioned;
        if ([...responderSelect.options].some((o) => o.value === mentioned)) {
          // Programmatic to avoid writing a new sticky from the
          // mention override (would lock the user into the
          // mentioned target on future turns).
          if (typeof setResponderProgrammatically === "function") {
            setResponderProgrammatically(mentioned);
          } else {
            responderSelect.value = mentioned;
          }
        }
      }
    }
  }
  const speaker_id = persona === "user" || persona === "narrator" ? null : persona;

  // Final safety net for the persona/responder collision case (see the
  // responderSelect change-handler for the symmetric collision-breaker).
  // If the responder ended up matching the user's just-typed persona
  // through some other path (sticky restore on reload, programmatic
  // setResponderProgrammatically race, server-pushed next_responder
  // pointing at the same character), pick a different responder. Asking
  // the same character to respond to its own user-typed turn lands
  // chat-instruct prompts in a two-consecutive-assistant-turns state
  // that EOSes most models → empty completion → bubble removed.
  if (persona !== "user" && persona !== "narrator" && responder === persona) {
    responder = pickDefaultResponder();
  }

  // Module compose hook: each Modules.onCompose subscriber gets a
  // shot at the pending message before it's POSTed. The texting
  // module's hook stamps metadata.modules.texting = {to: <char_id>}
  // when the user composed a text instead of a regular message; pf1e's
  // hook stamps a composed-action request and, for a covert action,
  // rewrites content to "" so the player's words never reach the LLM.
  // Engine doesn't know what shape modules write; the prompt-side
  // filters read their own metadata back at assembly time. Fire it even
  // with an empty composer so an action-only turn still carries its
  // request; each subscriber no-ops unless the user staged something.
  const pending = { content, persona, speaker_id, metadata: {} };
  if (window.Modules && Modules._fireCompose) Modules._fireCompose(pending);
  // A module may route THIS turn to a specific responder via
  // pending.metadata.route_to (stamped by its onCompose hook) — e.g. a pf1e
  // world-query (scry) goes to the narrator, not the NPC you were talking to.
  // Runs after the compose hook and before generation, so it redirects the
  // current turn rather than the next one.
  const _routeTo = pending.metadata && pending.metadata.route_to;
  if (_routeTo && _routeTo !== responder &&
      [...responderSelect.options].some((o) => o.value === _routeTo)) {
    responder = _routeTo;
    if (typeof setResponderProgrammatically === "function") setResponderProgrammatically(_routeTo);
  }
  const _hasMeta = pending.metadata && Object.keys(pending.metadata).length;
  if (pending.content || _hasMeta) {
    const postBody = { content: pending.content, persona, speaker_id };
    if (_hasMeta) postBody.metadata = pending.metadata;
    let userMsg;
    try {
      userMsg = await jfetch(`/api/conversations/${conversationId}/messages`, {
        method: "POST",
        body: JSON.stringify(postBody),
      });
    } catch (err) {
      flashError("Failed to post message: " + err.message);
      return;
    }
    composerInput.value = "";
    updateTokenEstimate();
    // The user just sent — pin to bottom so their message (and the reply)
    // land in view even if they'd scrolled up.
    autoScrollPinned = true;
    appendMessage(userMsg);
    // Auto-state on the just-appended message. Two modes:
    //
    // 1. Narrator full pass (persona=narrator + sub-toggle on).
    //    AWAIT it before generating — the narrator pass emits
    //    cast_add / move / outfit / set edits that the upcoming
    //    character generation needs to read at prompt assembly.
    //    Skipping the await means the character sees pre-edit
    //    state (e.g. "two guys walk in" → character responds as if
    //    the guys aren't in the room because cast_add hasn't
    //    landed yet).
    //
    // 2. Wardrobe-only mode (persona=user with sub-toggle, or
    //    legacy persona=character flow). Fire in parallel — the
    //    character can read user prose directly and the engine
    //    state catches up for subsequent turns.
    //
    // Caught errors so a failing side call doesn't block generation.
    if (userMsg.persona === "narrator") {
      try { await maybeAutoStateChanges(userMsg); }
      catch (e) { console.warn("narrator auto-state failed:", e); }
    } else {
      maybeAutoStateChanges(userMsg).catch(() => {});
    }
  }
  await streamGenerate({ persona: responder });
});

generateOnlyBtn.addEventListener("click", () => {
  if (state.generating) return;
  const responder = responderSelect.value || pickDefaultResponder();
  streamGenerate({ persona: responder });
});

// ---------------------------------------------------------------------------
// Streaming (incremental, with visible status)
// ---------------------------------------------------------------------------

function buildStreamPlaceholder(persona) {
  const wrap = document.createElement("article");
  wrap.className = `msg msg-${persona === "narrator" ? "narrator" : "character"} streaming`;

  const content = document.createElement("div");
  content.className = "msg-content";

  // Action row at the top — only the Stop button while streaming.
  const actions = document.createElement("div");
  actions.className = "msg-actions";
  const stopBtn = document.createElement("button");
  stopBtn.type = "button";
  stopBtn.className = "danger xs stream-stop";
  stopBtn.textContent = "Stop";
  stopBtn.title = "Abort this generation";
  stopBtn.addEventListener("click", () => {
    if (state.abortController) state.abortController.abort();
  });
  actions.appendChild(stopBtn);
  content.appendChild(actions);

  const header = document.createElement("header");
  header.className = "msg-header";
  const meta = document.createElement("div");
  meta.className = "msg-meta";
  meta.innerHTML = `<strong>${escapeHtml(persona === "narrator" ? "Narrator" : entityName(persona))}</strong>`;
  header.appendChild(meta);
  const status = document.createElement("span");
  status.className = "muted small stream-status";
  status.textContent = "Connecting…";
  header.appendChild(status);
  content.appendChild(header);

  const av = avatarFor(persona === "narrator" ? "narrator" : "character", persona === "narrator" ? null : persona);
  av.classList.add("msg-avatar");
  content.appendChild(av);

  // Body has to exist before the thinking block so we can insertBefore() it.
  const body = document.createElement("div");
  body.className = "msg-body";
  const textNode = document.createTextNode("");
  body.appendChild(textNode);
  content.appendChild(body);

  // Thinking sub-section: lazily created on the first thinking chunk so
  // requests that don't generate any thinking don't get an empty block.
  let thinkingDetails = null;
  let thinkingTextNode = null;
  let thinkingSummary = null;
  let thinkingBody = null;
  let thinkingChars = 0;
  let thinkingStartedAt = null;

  function appendThinking(text) {
    if (!thinkingDetails) {
      thinkingDetails = document.createElement("details");
      thinkingDetails.className = "msg-thinking streaming";
      thinkingDetails.open = true;
      thinkingSummary = document.createElement("summary");
      thinkingSummary.className = "thinking-summary";
      thinkingSummary.textContent = "Thinking…";
      thinkingDetails.appendChild(thinkingSummary);
      thinkingBody = document.createElement("div");
      thinkingBody.className = "msg-thinking-body";
      thinkingTextNode = document.createTextNode("");
      thinkingBody.appendChild(thinkingTextNode);
      thinkingDetails.appendChild(thinkingBody);
      // Insert ABOVE the body so the trace appears as a sub-section
      // immediately under the header.
      content.insertBefore(thinkingDetails, body);
      thinkingStartedAt = performance.now();
    }
    thinkingTextNode.appendData(text);
    thinkingChars += text.length;
    const elapsed = ((performance.now() - thinkingStartedAt) / 1000).toFixed(1);
    thinkingSummary.textContent = `Thinking · ${thinkingChars}ch · ${elapsed}s`;
    // Keep the latest thinking visible inside its own scroll region.
    thinkingBody.scrollTop = thinkingBody.scrollHeight;
  }

  function finalizeThinking() {
    if (!thinkingDetails) return;
    thinkingDetails.classList.remove("streaming");
    thinkingDetails.open = false;
    const elapsed = ((performance.now() - thinkingStartedAt) / 1000).toFixed(1);
    thinkingSummary.textContent = `Reasoning trace · ${thinkingChars}ch · ${elapsed}s`;
  }

  wrap.appendChild(content);
  return { wrap, status, body, textNode, appendThinking, finalizeThinking };
}

// (pf1e's pre-narration roll used to live here — _pf1eRollTargetId /
// maybePathfinderRoll. It moved into data/modules/pf1e/pf1e.js, registered
// on Modules.onBeforeGenerate, so the engine no longer knows pf1e's turn
// flow. See the _fireBeforeGenerate call in streamGenerate.)

async function streamGenerate({ persona, parent_id, disable_multi = false, carry_chain_from = null, carry_cast_from = null }) {
  state.generating = true;
  // User-initiated generation: pin to the bottom so the reply streams into
  // view even if they were reading scrollback.
  autoScrollPinned = true;
  generateOnlyBtn.disabled = true;
  setSendButtonStop(true);

  // Cancel any pending active-leaf POST. Without this, a regen that runs
  // faster than the 350ms debounce can have its server-side active leaf
  // (the new generated message) overwritten by the late active-leaf POST
  // pointing at the parent — losing the regen branch on reload.
  clearTimeout(activeLeafTimer);
  activeLeafTimer = null;

  // Cancel any in-flight auto-state side call for a previous turn.
  // Auto-state shares the Ollama model with main-gen, and Ollama
  // serializes per-model — without this, a regen fired during an
  // in-flight auto-state POST waits 10–40s behind it before its first
  // token arrives, looking like a hang. Aborting frees the model.
  //
  // EXCEPTION: a narrator full-pass auto-state (mode=narrator_full)
  // does real state work — cast_add / move / outfit — that the about-
  // to-start character generation will read at prompt-assembly time.
  // Killing it leaves state stale and the character reads pre-edit
  // baseline. For those, we don't abort; we let it run to completion.
  // The send-message handler awaits maybeAutoStateChanges before
  // calling streamGenerate when the message was a narrator, so this
  // branch normally doesn't fire there — but if a user somehow
  // double-clicks Send before the auto-state finishes, this guard
  // prevents the abort from killing valuable state work.
  if (state.autoStateController) {
    const c = state.autoStateController;
    if (c._autoStatePersona === "narrator") {
      // Keep it alive. The character generation will serialize behind
      // it on Ollama, which is the correct semantics.
    } else {
      try { c.abort(); } catch (_) {}
      state.autoStateController = null;
    }
  }

  const controller = new AbortController();
  state.abortController = controller;

  const { wrap, status, body, textNode, appendThinking, finalizeThinking } = buildStreamPlaceholder(persona);
  messagesEl.appendChild(wrap);
  scrollToBottomSoon();

  // Module pre-generation hook: a module can run async work before the
  // narration prompt assembles — e.g. pf1e resolves the pending action's d20
  // and stamps the result on the turn (see data/modules/pf1e/pf1e.js). No-op
  // when no module registered one. The reply placeholder is already appended
  // above, so a regen shows the "waiting" slot in place instead of a gap while
  // this round-trips.
  if (window.Modules && Modules._fireBeforeGenerate) {
    await Modules._fireBeforeGenerate({ persona, conversationId });
  }

  let raw = "";
  let firstChunk = false;
  let finalMsg = null;
  let pendingEdits = [];
  let finalLeafId = null;  // multi-response: real leaf may be a partner, not finalMsg
  // Multi-response: speaker_id -> stream-placeholder bundle. The lead's
  // existing wrap is registered up front so tagged deltas with the
  // lead's speaker_id route here; partner bubbles get added when the
  // multi_placeholders event arrives.
  const speakerPlaceholders = new Map();
  speakerPlaceholders.set(persona, { wrap, status, body, textNode, appendThinking, finalizeThinking, isLead: true });
  const multiPersistedMessages = [];  // partner messages from multi_message events
  const startedAt = performance.now();

  // Periodic elapsed-time tick so the user can see progress while we wait
  // on a cold-loaded model. Clears as soon as the first token arrives.
  const tick = setInterval(() => {
    if (firstChunk) return;
    const elapsed = ((performance.now() - startedAt) / 1000).toFixed(1);
    status.textContent = elapsed > 4
      ? `Waiting on Ollama (model loading) · ${elapsed}s`
      : `Waiting on Ollama · ${elapsed}s`;
  }, 500);

  try {
    const r = await fetch(`/api/conversations/${conversationId}/generate`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify({
        persona,
        speaker_id: persona === "narrator" ? null : persona,
        parent_id: parent_id || null,
        disable_multi: !!disable_multi,
        carry_chain_from: carry_chain_from || null,
        carry_cast_from: carry_cast_from || null,
      }),
      signal: controller.signal,
    });
    if (!r.ok || !r.body) {
      const err = await r.json().catch(() => ({}));
      throw new Error(err.error || `HTTP ${r.status}`);
    }

    const ct = r.headers.get("content-type") || "";
    if (!ct.includes("event-stream")) {
      const txt = await r.text();
      throw new Error(`unexpected ${ct}: ${txt.slice(0, 200)}`);
    }

    const reader = r.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buffer.indexOf("\n\n")) >= 0) {
        const line = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        const trimmed = line.trim();
        if (!trimmed.startsWith("data:")) continue;
        let payload;
        try { payload = JSON.parse(trimmed.slice(5).trim()); }
        catch { continue; }
        if (payload.type === "start") {
          status.textContent = payload.model ? `Streaming · ${payload.model}` : "Streaming…";
        } else if (payload.type === "thinking") {
          if (!firstChunk) firstChunk = true;
          status.textContent = "Thinking…";
          appendThinking(payload.content);
          scrollToBottomSoon();
        } else if (payload.type === "delta") {
          if (!firstChunk) firstChunk = true;
          status.textContent = "";
          // Auto-collapse the trace the first time real content arrives.
          finalizeThinking();
          // Multi-response routes deltas by speaker_id into per-partner
          // bubbles. Non-multi calls have no speaker_id; they all go to
          // the lead's textNode as before.
          const sid = payload.speaker_id;
          const ph = sid ? speakerPlaceholders.get(sid) : null;
          if (ph && !ph.isLead) {
            ph.textNode.appendData(payload.content);
            ph.status.textContent = "";
          } else {
            raw += payload.content;
            textNode.appendData(payload.content);
          }
          scrollToBottomSoon();
        } else if (payload.type === "multi_placeholders") {
          // Pre-create empty bubbles for each partner so subsequent
          // tagged deltas paint into the right one. The lead's wrap is
          // already registered.
          for (const p of (payload.partners || [])) {
            if (!p || !p.speaker_id || speakerPlaceholders.has(p.speaker_id)) continue;
            const ph = buildStreamPlaceholder(p.speaker_id);
            messagesEl.appendChild(ph.wrap);
            speakerPlaceholders.set(p.speaker_id, { ...ph, isLead: false });
          }
          scrollToBottomSoon();
        } else if (payload.type === "error") {
          status.textContent = "Error";
          status.dataset.kind = "error";
          raw += `\n[${payload.error}]`;
          textNode.appendData(`\n[${payload.error}]`);
        } else if (payload.type === "multi_message") {
          // Server has persisted a partner message. Replace its live
          // streaming placeholder with the fully-rendered message so
          // actions / sibling chips / metadata attachments come online.
          if (payload.message) {
            const m = payload.message;
            state.conversation.messages[m.id] = m;
            state.conversation.active_path_leaf = m.id;
            recordBranchChoicePath(m.id);
            patchBranchCastFromMessage(m);
            multiPersistedMessages.push(m);
            const ph = m.speaker_id ? speakerPlaceholders.get(m.speaker_id) : null;
            if (ph && !ph.isLead && ph.wrap.parentNode) {
              ph.wrap.dataset.messageId = m.id;
              const fresh = renderMessage(m);
              ph.wrap.replaceWith(fresh);
              speakerPlaceholders.delete(m.speaker_id);
            } else {
              // Placeholder absent (multi_placeholders never arrived) —
              // fall back to appending so the message is still visible.
              appendMessage(m);
            }
            scrollToBottomSoon();
          }
        } else if (payload.type === "done") {
          finalMsg = payload.message;
          finalLeafId = payload.active_path_leaf || (finalMsg && finalMsg.id);
          pendingEdits = payload.pending_edits || [];
          if (payload.next_responder) {
            // Special: `next_responder === "user"` means hand the next
            // turn back to the user. Park a flag so autoplay scheduling
            // skips the countdown for this completion; the dropdown
            // stays put (user isn't an option there).
            if (payload.next_responder === "user") {
              finalMsg && (finalMsg._next_is_user = true);
            } else if ([...responderSelect.options].some((o) => o.value === payload.next_responder)) {
              responderSelect.value = payload.next_responder;
            }
          }
        }
      }
    }
  } catch (e) {
    if (e.name === "AbortError") {
      // The server's GeneratorExit handler persists whatever it had as a
      // normal message; reload below picks it up.
      status.textContent = "Stopped";
    } else {
      raw += `\n[stream failed: ${e.message}]`;
      textNode.appendData(`\n[stream failed: ${e.message}]`);
      status.textContent = "Failed";
      status.dataset.kind = "error";
    }
  } finally {
    clearInterval(tick);
    finalizeThinking();
    state.generating = false;
    state.abortController = null;
    generateOnlyBtn.disabled = false;
    setSendButtonStop(false);
    wrap.classList.remove("streaming");
  }

  // If the stream ended without a 'done' event (Stop / network drop), the
  // server saved the partial as a normal message. Reload to pick it up
  // and let the regular renderer replace the orphaned placeholder. The
  // server persists from inside its GeneratorExit handler, which races
  // the client's abort propagation, so we may need a small delay + retry.
  if (!finalMsg) {
    const placeholderText = raw;
    wrap.remove();
    // Multi-response: clean up any pre-created partner placeholders
    // — abort happens before _dispatch_multi_response, so no
    // multi_message events fired and these are still empty.
    for (const [, ph] of speakerPlaceholders) {
      if (!ph.isLead && ph.wrap.parentNode) ph.wrap.remove();
    }
    speakerPlaceholders.clear();
    const beforeIds = new Set(Object.keys(state.conversation.messages));
    let recovered = false;
    for (const wait of [80, 200, 400, 800]) {
      await new Promise((res) => setTimeout(res, wait));
      try {
        await reloadConversation();
      } catch (e) {
        flashError("Couldn't refresh after stop: " + e.message);
        return;
      }
      const newIds = Object.keys(state.conversation.messages).filter((id) => !beforeIds.has(id));
      if (newIds.length) { recovered = true; break; }
    }
    if (!recovered && placeholderText.trim()) {
      // Server didn't end up persisting (e.g. completely empty output).
      // Surface a small note so the user knows what happened.
      flashError("Stop was instant — nothing to keep.");
    }
    return;
  }

  // Replace the placeholder text node with formatted HTML now that we're done,
  // and wire the message into local state.
  body.innerHTML = formatBody(raw, persona === "narrator" ? "Narrator" : entityName(persona));
  if (finalMsg) {
    wrap.dataset.messageId = finalMsg.id;
    state.conversation.messages[finalMsg.id] = finalMsg;
    // Multi-response: leaf may have advanced past the lead onto a
    // partner message. Trust the server's reported leaf when present.
    state.conversation.active_path_leaf = finalLeafId || finalMsg.id;
    recordBranchChoicePath(state.conversation.active_path_leaf);
    patchBranchCastFromMessage(finalMsg);
    // Re-render this single message to attach actions/sibling controls.
    const fresh = renderMessage(finalMsg);
    wrap.replaceWith(fresh);
    if (pendingEdits.length) reviewEdits(finalMsg);
    // Multi-response: clean up any pre-created partner placeholder that
    // never received a multi_message (the partner stayed silent).
    for (const [, ph] of speakerPlaceholders) {
      if (!ph.isLead && ph.wrap.parentNode) ph.wrap.remove();
    }
    speakerPlaceholders.clear();
    // Sequence: auto-state first (may emit clothing directives that
    // change which image the picker should show), then image pick.
    // For multi-response, run the side-call pair sequentially per
    // message — Ollama serializes per-model anyway, parallelizing
    // would just queue them.
    const sideCallTargets = [finalMsg, ...multiPersistedMessages];
    (async () => {
      for (const m of sideCallTargets) {
        try { await maybeAutoStateChanges(m); }
        catch (e) { console.warn("auto-state failed:", e); }
        try { await maybeLifeSimUpdate(m); }
        catch (e) { console.warn("life-sim update failed:", e); }
        try { await maybePickImagePack(m); }
        catch (e) { console.warn("image-pick failed:", e); }
      }
      // Autoplay scheduling runs after side-calls so the timer starts
      // from a fully-settled UI. The last persisted message governs
      // the chain decision (narrator → countdown, character → chain
      // narrator immediately).
      const lastSettled = sideCallTargets[sideCallTargets.length - 1] || finalMsg;
      try { maybeScheduleAutoplay(lastSettled); }
      catch (e) { console.warn("autoplay schedule failed:", e); }
    })();
  }
  status.textContent = `${(raw.length).toLocaleString()} chars · ${((performance.now() - startedAt) / 1000).toFixed(1)}s`;
}

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

function flashError(msg) {
  const el = document.createElement("div");
  el.className = "chat-error";
  el.textContent = msg;
  messagesEl.appendChild(el);
  setTimeout(() => el.remove(), 8000);
  scrollToBottomSoon();
}

function flashInfo(msg) {
  const el = document.createElement("div");
  el.className = "chat-info";
  el.textContent = msg;
  messagesEl.appendChild(el);
  setTimeout(() => el.remove(), 6000);
  scrollToBottomSoon();
}

// ---------------------------------------------------------------------------
// Quick edits panel: outfit swap with live PATCH
// ---------------------------------------------------------------------------

function currentRoomFor(charId) {
  // Walk the active path back from the leaf to find this character's last
  // recorded room.
  const msgs = state.conversation.messages;
  let cur = msgs[state.conversation.active_path_leaf];
  while (cur) {
    const p = cur.presence_snapshot?.presence?.[charId];
    if (p && p.room) return p.room;
    if (!cur.parent_id) break;
    cur = msgs[cur.parent_id];
  }
  return null;
}

function currentOutfitFor(charId) {
  // Branch-aware analog of currentRoomFor for outfit. Walks the active
  // path's snapshots first, falls back to the entity's properties
  // .current_outfit only if no snapshot on this branch ever recorded
  // an outfit for the character — so flipping to a sibling that
  // predates an outfit swap shows the correct pre-swap outfit.
  const msgs = state.conversation.messages;
  let cur = msgs[state.conversation.active_path_leaf];
  while (cur) {
    const p = cur.presence_snapshot?.presence?.[charId];
    if (p && p.outfit) return p.outfit;
    if (!cur.parent_id) break;
    cur = msgs[cur.parent_id];
  }
  return state.entities[charId]?.properties?.current_outfit || null;
}

function renderQuickEdits() {
  if (!quickEditsEl) return;
  quickEditsEl.innerHTML = "";
  // Branch-scoped: same gate as renderCastList, so quick-edits stays
  // in sync with what's actually in this branch's scene.
  //
  // One compact row per character: name + Edit JSON. The old per-char
  // Outfit / Move selects were dropped — the Cast rows' Clothing /
  // Location comboboxes are their replacement and are strictly more
  // capable (full global outfit library; any room, auto-instanced).
  const characters = Object.values(state.entities).filter(
    (e) => e.type === "character"
      && (e.id === "user" || state.effectiveCastChars.has(e.id)),
  );
  if (!characters.length) {
    quickEditsEl.innerHTML = `<p class="muted small">No characters in this scene.</p>`;
    return;
  }
  for (const char of characters) {
    const row = document.createElement("div");
    row.className = "quick-char";
    const name = document.createElement("strong");
    name.className = "cast-name";
    name.textContent = char.name || char.id;
    row.appendChild(name);
    const editJson = document.createElement("button");
    editJson.type = "button";
    editJson.className = "ghost xs";
    editJson.textContent = "Edit JSON";
    editJson.title = "Edit this character's instance JSON for this conversation";
    editJson.addEventListener("click", () => openInstanceEditor(char));
    row.appendChild(editJson);
    quickEditsEl.appendChild(row);
  }
}

// In-conversation JSON editor for any instance entity (character, room,
// outfit, etc.). PUT /api/conversations/<cid>/entities/<eid> replaces
// the file in instances/<cid>/entities/ without touching the templates.
function openInstanceEditor(entity) {
  const dlg = document.getElementById("instance-editor-dialog");
  const ta = document.getElementById("instance-editor-text");
  const title = document.getElementById("instance-editor-title");
  const status = document.getElementById("instance-editor-status");
  if (!dlg || !ta) return;
  title.textContent = `Edit ${entity.type}: ${entity.name || entity.id}`;
  ta.value = JSON.stringify(entity, null, 2);
  status.textContent = "";
  dlg.returnValue = "";
  dlg.showModal();
  dlg.addEventListener(
    "close",
    async () => {
      if (dlg.returnValue !== "save") return;
      let parsed;
      try { parsed = JSON.parse(ta.value); }
      catch (e) { flashError("Invalid JSON: " + e.message); return; }
      try {
        const saved = await jfetch(
          `/api/conversations/${conversationId}/entities/${entity.id}`,
          { method: "PUT", body: JSON.stringify(parsed) }
        );
        state.entities[saved.id] = saved;
        renderQuickEdits();
        flashInfo(`Saved ${saved.id} (this conversation only).`);
      } catch (e) {
        flashError("Save failed: " + e.message);
      }
    },
    { once: true }
  );
}

// ---------------------------------------------------------------------------
// Token estimator (rough: 1 token ≈ 4 chars)
// ---------------------------------------------------------------------------

function updateTokenEstimate() {
  if (!tokensEl) return;
  // Approximate the active prompt size by summing system + history chars
  // we can predict client-side: persona, surroundings, dev panel, history.
  const path = pathToLeaf(state.conversation.active_path_leaf);
  let chars = composerInput.value.length;
  for (const m of path) chars += (m.content || "").length + 12;
  const persona = personaSelect.value;
  if (persona !== "user" && persona !== "narrator") {
    const c = state.entities[persona];
    if (c) chars += (c.description || "").length + 200;
  }
  const tokens = Math.ceil(chars / 4);
  // Pull num_ctx from settings or the global default; cheap to infer.
  const numCtx =
    (state.conversation.settings.sampling || {}).num_ctx ||
    (window.GEMMASIM_INITIAL?.config?.num_ctx) ||
    8192;
  tokensEl.textContent = `≈${tokens.toLocaleString()} / ${numCtx.toLocaleString()} tok`;
  tokensEl.dataset.kind = tokens > numCtx * 0.9 ? "warn" : "";
}

composerInput.addEventListener("input", updateTokenEstimate);

// ---------------------------------------------------------------------------
// Message actions
// ---------------------------------------------------------------------------

function editMessage(msg, opts = {}) {
  const inPlace = !!opts.inPlace;
  const wrap = messagesEl.querySelector(`[data-message-id="${msg.id}"]`);
  if (!wrap) return;
  if (wrap.classList.contains("editing")) {
    // If the editor textarea is actually present, an edit is genuinely open —
    // no-op. Otherwise the row is STUCK: an external in-place re-render
    // orphaned the editor (removed the textarea) but left the `.editing`
    // flag set, which previously dead-locked editing until a page refresh.
    // Self-heal by rebuilding a clean row and re-opening on it.
    if (wrap.querySelector(".msg-edit")) return;
    rerenderMessage(msg);
    return editMessage(msg, opts);
  }
  wrap.classList.add("editing");
  const content = wrap.querySelector(".msg-content");
  const body = content.querySelector(".msg-body");
  const sib = content.querySelector(".siblings");
  const actions = content.querySelector(".msg-actions");

  const original = msg.content || "";
  const ta = document.createElement("textarea");
  ta.className = "msg-edit";
  ta.value = original;
  // Match height to current body so it doesn't jump.
  ta.style.minHeight = Math.max(80, body.offsetHeight) + "px";
  body.style.display = "none";
  if (sib) sib.style.display = "none";
  body.after(ta);

  const editActions = document.createElement("div");
  editActions.className = "msg-edit-actions";
  const saveBtn = document.createElement("button");
  saveBtn.className = "primary xs";
  saveBtn.textContent = "Save";
  const cancelBtn = document.createElement("button");
  cancelBtn.className = "ghost xs";
  cancelBtn.textContent = "Cancel";
  editActions.append(saveBtn, cancelBtn);
  ta.after(editActions);

  // Stash + hide the row's normal action buttons. Detach the NODES (not
  // innerHTML) so their click listeners survive — restoring via innerHTML
  // re-parses the HTML into fresh, listener-less buttons, which left Edit/Raw
  // dead after a Cancel until the row next re-rendered.
  const prevActionNodes = Array.from(actions.childNodes);
  actions.replaceChildren();

  ta.focus();
  ta.setSelectionRange(ta.value.length, ta.value.length);
  // Editing a message near the bottom of the scroll can leave the Save/Cancel
  // row below the fold — under the composer — so the first click lands on the
  // composer, not the button (it looked like "have to press Save/Cancel twice").
  // Keep the controls in view on every layout change: typing, and — for pf1e —
  // an action chip appearing above the text (and its dice preview arriving via
  // an async fetch a moment later), each of which grows the box back past the
  // fold. A ResizeObserver on the edit content catches them all, sync or async.
  const keepEditActionsVisible = () => requestAnimationFrame(() => {
    try {
      const mr = messagesEl.getBoundingClientRect();
      const er = editActions.getBoundingClientRect();
      let target = messagesEl.scrollTop;
      if (er.bottom > mr.bottom) target += (er.bottom - mr.bottom) + 8;
      else if (er.top < mr.top) target -= (mr.top - er.top) + 8;
      else return;
      // Force an instant jump: the container has `scroll-behavior: smooth`, so a
      // plain scrollTop write animates, and a follow-up growth re-measures mid-
      // animation and never converges — leaving the buttons short of the fold.
      messagesEl.scrollTo({ top: target, behavior: "instant" });
    } catch (_) {}
  });
  keepEditActionsVisible();
  let editResizeObs = null;
  try {
    editResizeObs = new ResizeObserver(keepEditActionsVisible);
    editResizeObs.observe(content);
  } catch (_) {}

  function exitEdit() {
    if (editResizeObs) { try { editResizeObs.disconnect(); } catch (_) {} }
    ta.remove();
    editActions.remove();
    body.style.display = "";
    if (sib) sib.style.display = "";
    actions.replaceChildren(...prevActionNodes);   // re-attach with listeners intact
    wrap.classList.remove("editing");
  }

  cancelBtn.addEventListener("click", exitEdit);
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Escape") exitEdit();
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") saveBtn.click();
  });

  saveBtn.addEventListener("click", async () => {
    const next = ta.value;
    if (next === original) return exitEdit();
    saveBtn.disabled = true;
    let updated;
    try {
      updated = await jfetch(`/api/conversations/${conversationId}/messages/${msg.id}`, {
        method: "PUT",
        body: JSON.stringify(
          inPlace ? { content: next, mode: "in_place" } : { content: next }
        ),
      });
    } catch (e) {
      saveBtn.disabled = false;
      return flashError("Edit failed: " + e.message);
    }
    if (inPlace) {
      // Server mutated this message in place — same id, same parent,
      // descendants intact. Just refresh the row's content; no leaf
      // change, no full re-render needed.
      Object.assign(state.conversation.messages[msg.id] || msg, updated);
      const fresh = renderMessage(state.conversation.messages[msg.id] || msg);
      wrap.replaceWith(fresh);
      return;
    }
    // Branching path: mirror server state, set the new sibling as the
    // active leaf, and re-render the whole path so any descendants from
    // the original branch drop out of the DOM (still in state, just no
    // longer on the active path — sibling chip on the new row navigates
    // back to the original).
    state.conversation.messages[updated.id] = updated;
    setActiveLeaf(updated.id);
  });
}

async function regenerate(msg) {
  // "Regen" always re-rolls just this one message. For group members
  // we force disable_multi so the new sibling is a plain single-
  // character reply (re-rolling the lead would otherwise fire the
  // joint pass again and spawn a fresh group — that's "Regen group"),
  // and we pass carry_chain_from so the server clones the original's
  // downstream group members under the new sibling. The old chain
  // stays addressable on the original branch.
  if (!msg.parent_id) return;
  // Defensive: regen targets an AI turn. On a user message it would generate
  // "as the user" (no-op) and force-scroll — the button is hidden, but guard.
  if (msg.persona === "user") return;
  setActiveLeaf(msg.parent_id);
  const persona = msg.persona === "narrator" ? "narrator" : msg.speaker_id || msg.persona;
  const inGroup = !!(msg.metadata && msg.metadata.multi_response);
  await streamGenerate({
    persona,
    parent_id: msg.parent_id,
    disable_multi: inGroup,
    carry_chain_from: inGroup ? msg.id : null,
    // Keep a cast add/remove made on this turn sticky across the regen — the new
    // sibling would otherwise drop it and a removed character would reappear.
    carry_cast_from: msg.id,
  });
}

async function regenerateGroup(msg) {
  // Walk to the lead via group_id and re-roll the whole chain off the
  // lead's parent. The old chain stays addressable as a sibling
  // branch off the same parent.
  if (!msg.parent_id) return;
  const grp = msg.metadata && msg.metadata.multi_response;
  if (!grp || !grp.group_id) return;
  const lead = state.conversation.messages[grp.group_id] || msg;
  if (!lead.parent_id) return;
  setActiveLeaf(lead.parent_id);
  const persona = lead.persona === "narrator" ? "narrator" : lead.speaker_id || lead.persona;
  await streamGenerate({ persona, parent_id: lead.parent_id });
}

// ---------------------------------------------------------------------------
// Narrator-edit: rewrite a message via a directive, also applying state
// changes (outfit swaps, moves, etc.) the directive implies.
//
// The rewrite is appended as a NEW sibling under the same parent — same
// branching shape as Edit/Regen — so the original stays addressable via
// the normal sibling chip. The directive is typed into an inline
// composer slotted under the target message (Enter sends, Esc cancels);
// the response streams into a fresh message wrapper just like a normal
// generation.
// ---------------------------------------------------------------------------
function narratorEditMessage(msg) {
  if (state.generating) return;
  if (state.openComposers.has(msg.id)) {
    // Already open — refocus the textarea.
    const ta = messagesEl.querySelector(
      `[data-message-id="${msg.id}"] .narrator-edit-composer textarea`
    );
    ta?.focus();
    return;
  }
  openNarratorEditComposer(msg);
}

function buildNarratorEditComposer(msg) {
  const c = document.createElement("div");
  c.className = "narrator-edit-composer";
  const ta = document.createElement("textarea");
  ta.rows = 2;
  ta.placeholder = "Narrator directive — e.g. Swap Iris to a green cardigan. Move both to the reading nook.";
  c.appendChild(ta);

  const actions = document.createElement("div");
  actions.className = "narrator-edit-composer-actions";
  const send = document.createElement("button");
  send.type = "button";
  send.className = "primary xs";
  send.textContent = "Rewrite";
  send.title = "Send directive (Enter)";
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.className = "ghost xs";
  cancel.textContent = "Cancel";
  cancel.title = "Cancel (Esc)";
  actions.appendChild(send);
  actions.appendChild(cancel);
  c.appendChild(actions);

  const close = () => closeNarratorEditComposer(msg);
  const submit = () => {
    const directive = ta.value.trim();
    if (!directive) { ta.focus(); return; }
    state.openComposers.delete(msg.id);
    c.remove();
    runNarratorEdit(msg, directive);
  };
  send.addEventListener("click", submit);
  cancel.addEventListener("click", close);
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); }
    else if (e.key === "Escape") { e.preventDefault(); close(); }
  });
  // Defer focus to the next tick so the click event finishes before we
  // try to take focus (otherwise the click that opened us steals it back).
  setTimeout(() => ta.focus(), 0);
  return c;
}
registerAttachment({
  id: "narrator_edit_composer",
  slot: "below-body",
  // After applied_edits (10) and phrase_hits (20), before siblings (100)
  // so the composer sits between the message body's metadata chips and
  // the branch chip.
  order: 50,
  show: (msg) => state.openComposers.has(msg.id),
  render: buildNarratorEditComposer,
});

async function runNarratorEdit(msg, directive) {
  if (state.generating) return;
  state.generating = true;
  generateOnlyBtn.disabled = true;
  setSendButtonStop(true);
  clearTimeout(activeLeafTimer);
  activeLeafTimer = null;
  const controller = new AbortController();
  state.abortController = controller;

  const persona = msg.persona === "narrator" ? "narrator" : (msg.speaker_id || msg.persona);
  const { wrap, status, body, textNode, appendThinking, finalizeThinking } = buildStreamPlaceholder(persona);
  wrap.classList.add("narrator-edit-streaming");

  // Drop the placeholder right after the target so the rewrite appears as
  // a sibling in the visual flow. Narrator edits stream into a position
  // that's typically off-screen (sibling of an older message), so we
  // intentionally do NOT force-scroll here — yanking the user to the
  // bottom while tokens are written far above is jarring and the new
  // content isn't even where the scroll would land.
  const targetWrap = messagesEl.querySelector(`[data-message-id="${msg.id}"]`);
  if (targetWrap && targetWrap.parentNode === messagesEl) {
    messagesEl.insertBefore(wrap, targetWrap.nextSibling);
  } else {
    messagesEl.appendChild(wrap);
  }

  let raw = "";
  let firstChunk = false;
  let finalMsg = null;
  const startedAt = performance.now();

  const tick = setInterval(() => {
    if (firstChunk) return;
    const elapsed = ((performance.now() - startedAt) / 1000).toFixed(1);
    status.textContent = elapsed > 4
      ? `Waiting on Ollama (model loading) · ${elapsed}s`
      : `Waiting on Ollama · ${elapsed}s`;
  }, 500);

  try {
    const r = await fetch(`/api/conversations/${conversationId}/narrator-edit/${msg.id}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify({ directive }),
      signal: controller.signal,
    });
    if (!r.ok || !r.body) {
      const err = await r.json().catch(() => ({}));
      throw new Error(err.error || `HTTP ${r.status}`);
    }
    const reader = r.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buffer.indexOf("\n\n")) >= 0) {
        const line = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        const trimmed = line.trim();
        if (!trimmed.startsWith("data:")) continue;
        let payload;
        try { payload = JSON.parse(trimmed.slice(5).trim()); } catch { continue; }
        if (payload.type === "start") {
          status.textContent = payload.model ? `Rewriting · ${payload.model}` : "Rewriting…";
        } else if (payload.type === "thinking") {
          if (!firstChunk) firstChunk = true;
          status.textContent = "Thinking…";
          appendThinking(payload.content);
          // Narrator edits stay where the user left them — no auto-scroll.
        } else if (payload.type === "delta") {
          if (!firstChunk) firstChunk = true;
          status.textContent = "";
          raw += payload.content;
          textNode.appendData(payload.content);
          finalizeThinking();
          // Narrator edits stay where the user left them — no auto-scroll.
        } else if (payload.type === "error") {
          throw new Error(payload.error || "narrator-edit error");
        } else if (payload.type === "done") {
          finalMsg = payload.message;
        }
      }
    }
  } catch (e) {
    if (e.name === "AbortError") {
      status.textContent = "Stopped";
    } else {
      status.textContent = "Failed";
      status.dataset.kind = "error";
      flashError("Narrator edit failed: " + e.message);
    }
  } finally {
    clearInterval(tick);
    finalizeThinking();
    state.generating = false;
    state.abortController = null;
    generateOnlyBtn.disabled = false;
    setSendButtonStop(false);
    wrap.classList.remove("streaming");
  }

  if (!finalMsg) {
    wrap.remove();
    return;
  }

  // Wire the new sibling into local state and re-render the path so the
  // parent's sibling chip reflects the new branch. fullRender reads
  // active_path_leaf, so set it first.
  state.conversation.messages[finalMsg.id] = finalMsg;
  patchBranchCastFromMessage(finalMsg);
  setActiveLeaf(finalMsg.id);
  // Run the image-pack pick on the rewritten message — the streamGenerate
  // pipeline does this for normal responses, narrator-edit needs the same
  // hook so a rewrite under an image-enabled conversation still gets an
  // image attached.
  maybePickImagePack(finalMsg);
}

async function continueMessage(msg) {
  if (state.generating) return;
  state.generating = true;
  generateOnlyBtn.disabled = true;
  setSendButtonStop(true);
  state.abortController = new AbortController();

  const wrap = messagesEl.querySelector(`[data-message-id="${msg.id}"]`);
  if (!wrap) { state.generating = false; return; }
  const content = wrap.querySelector(".msg-content");
  const body = wrap.querySelector(".msg-body");
  let raw = msg.content || "";

  // Append a marker text node we can write into.
  const tail = document.createTextNode("");
  body.appendChild(tail);
  wrap.classList.add("streaming");

  // Inline Stop button in this message's header for the duration of the
  // continuation; removed in finally.
  const cHeader = wrap.querySelector(".msg-header");
  const cStop = document.createElement("button");
  cStop.type = "button";
  cStop.className = "danger xs stream-stop";
  cStop.textContent = "Stop";
  cStop.title = "Abort this continuation";
  cStop.addEventListener("click", () => {
    if (state.abortController) state.abortController.abort();
  });
  cHeader?.appendChild(cStop);

  // Continuation thinking: re-use the existing trace block if there is
  // one, otherwise build a new sub-section above the body.
  let cThinking = wrap.querySelector(".msg-thinking");
  let cThinkingBody = cThinking ? cThinking.querySelector(".msg-thinking-body") : null;
  let cThinkingSummary = cThinking ? cThinking.querySelector("summary") : null;
  let cThinkingChars = cThinkingBody ? cThinkingBody.textContent.length : 0;
  let cThinkingStartedAt = null;
  function appendContinueThinking(text) {
    if (!cThinking) {
      cThinking = document.createElement("details");
      cThinking.className = "msg-thinking streaming";
      cThinking.open = true;
      cThinkingSummary = document.createElement("summary");
      cThinkingSummary.textContent = "Thinking (continuation)…";
      cThinking.appendChild(cThinkingSummary);
      cThinkingBody = document.createElement("div");
      cThinkingBody.className = "msg-thinking-body";
      cThinking.appendChild(cThinkingBody);
      content.insertBefore(cThinking, body);
      cThinkingStartedAt = performance.now();
    } else {
      cThinking.classList.add("streaming");
      cThinking.open = true;
      if (cThinkingStartedAt === null) cThinkingStartedAt = performance.now();
    }
    cThinkingBody.appendChild(document.createTextNode(text));
    cThinkingChars += text.length;
    const elapsed = ((performance.now() - cThinkingStartedAt) / 1000).toFixed(1);
    cThinkingSummary.textContent = `Thinking · ${cThinkingChars}ch · +${elapsed}s`;
    cThinkingBody.scrollTop = cThinkingBody.scrollHeight;
  }
  function finalizeContinueThinking() {
    if (!cThinking) return;
    cThinking.classList.remove("streaming");
    cThinking.open = false;
    cThinkingSummary.textContent = `Reasoning trace · ${cThinkingChars}ch`;
  }

  try {
    const r = await fetch(`/api/conversations/${conversationId}/messages/${msg.id}/continue`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: "{}",
      signal: state.abortController.signal,
    });
    if (!r.ok || !r.body) {
      const err = await r.json().catch(() => ({}));
      throw new Error(err.error || `HTTP ${r.status}`);
    }
    const reader = r.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let updated = null;
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buffer.indexOf("\n\n")) >= 0) {
        const line = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        const trimmed = line.trim();
        if (!trimmed.startsWith("data:")) continue;
        let payload;
        try { payload = JSON.parse(trimmed.slice(5).trim()); }
        catch { continue; }
        if (payload.type === "thinking") {
          appendContinueThinking(payload.content);
        } else if (payload.type === "delta") {
          raw += payload.content;
          tail.appendData(payload.content);
          finalizeContinueThinking();
          scrollToBottomSoon();
        } else if (payload.type === "error") {
          tail.appendData(`\n[${payload.error}]`);
        } else if (payload.type === "done") {
          updated = payload.message;
        }
      }
    }
    wrap.classList.remove("streaming");
    if (updated) {
      state.conversation.messages[updated.id] = updated;
      const fresh = renderMessage(updated);
      wrap.replaceWith(fresh);
    } else {
      // Fall back to formatting whatever we accumulated.
      body.innerHTML = formatBody(raw, speakerLabel(msg));
    }
  } catch (e) {
    wrap.classList.remove("streaming");
    if (e.name === "AbortError") {
      flashInfo("Continue stopped.");
    } else {
      flashError("Continue failed: " + e.message);
    }
  } finally {
    finalizeContinueThinking();
    cStop.remove();
    state.generating = false;
    state.abortController = null;
    generateOnlyBtn.disabled = false;
    setSendButtonStop(false);
  }
  // Whether we got a 'done' event or aborted, the server has the latest
  // version of the message. Reload so the in-place edit reflects it.
  try {
    await reloadConversation();
  } catch (e) {
    /* non-fatal */
  }
}

async function deleteMessage(msg) {
  const hasSummary = !!(state.conversation.settings.summary || "").trim();
  const note = hasSummary
    ? "Delete this message and all its descendants?\n\nNote: a running summary still exists and may reference this content. Use the Conversation summary block in the sidebar to Clear it."
    : "Delete this message and all its descendants?";
  if (!(await confirmAction(note))) return;
  try {
    await jfetch(`/api/conversations/${conversationId}/messages/${msg.id}`, {
      method: "DELETE",
    });
  } catch (e) {
    return flashError("Delete failed: " + e.message);
  }
  // Drop subtree locally.
  const toDrop = collectDescendants(msg.id);
  toDrop.add(msg.id);
  for (const id of toDrop) delete state.conversation.messages[id];
  if (toDrop.has(state.conversation.active_path_leaf)) {
    state.conversation.active_path_leaf = msg.parent_id;
  }
  // Drop branch_choices entries that point at removed messages so the
  // next descent doesn't try to follow a stale memo into thin air.
  const choices = state.conversation.branch_choices || {};
  for (const [parent, child] of Object.entries(choices)) {
    if (toDrop.has(parent) || toDrop.has(child)) delete choices[parent];
  }
  fullRender();
}
function collectDescendants(id) {
  const out = new Set();
  const stack = [id];
  while (stack.length) {
    const cur = stack.pop();
    for (const m of Object.values(state.conversation.messages)) {
      if (m.parent_id === cur && !out.has(m.id)) {
        out.add(m.id);
        stack.push(m.id);
      }
    }
  }
  return out;
}

function confirmAction(text) {
  return new Promise((resolve) => {
    confirmText.textContent = text;
    confirmDialog.returnValue = "";
    confirmDialog.showModal();
    confirmDialog.addEventListener(
      "close",
      () => resolve(confirmDialog.returnValue === "ok"),
      { once: true }
    );
  });
}

async function reviewEdits(msg) {
  const edits = msg.metadata?.pending_edits || [];
  if (!edits.length) return;
  editsText.textContent = JSON.stringify(edits, null, 2);
  editsDialog.returnValue = "";
  editsDialog.showModal();
  editsDialog.addEventListener(
    "close",
    async () => {
      if (editsDialog.returnValue !== "accept") return;
      for (const edit of edits) {
        try {
          if (edit.kind === "patch") {
            await jfetch(
              `/api/conversations/${conversationId}/entities/${edit.id}/patch`,
              { method: "POST", body: JSON.stringify(edit.data || {}) }
            );
          } else if (edit.kind === "replace") {
            await jfetch(
              `/api/conversations/${conversationId}/entities/${edit.id}`,
              { method: "PUT", body: JSON.stringify(edit.data || {}) }
            );
          }
        } catch (e) {
          flashError(`Edit failed for ${edit.id}: ${e.message}`);
        }
      }
    },
    { once: true }
  );
}

// ---------------------------------------------------------------------------
// Side panels
// ---------------------------------------------------------------------------

// Drawer state. On desktop both rails can be visible at once; on
// mobile they're overlay drawers and only one can be open at a time
// so they don't fight over the backdrop or stack on top of each
// other. openDrawer() enforces the mutex; the bare class toggles
// would let both drawers open simultaneously.
const mobileMQ = window.matchMedia("(max-width: 800px)");

const openLeft = () => {
  shell.classList.remove("left-collapsed");
  if (mobileMQ.matches) shell.classList.remove("right-open");
};
const closeLeft = () => shell.classList.add("left-collapsed");
const openRight = () => {
  shell.classList.add("right-open");
  if (mobileMQ.matches) shell.classList.add("left-collapsed");
};
const closeRight_ = () => shell.classList.remove("right-open");
const closeAllDrawers = () => {
  shell.classList.add("left-collapsed");
  shell.classList.remove("right-open");
};

toggleLeft.addEventListener("click", closeLeft);
showLeft.addEventListener("click", openLeft);
showRight.addEventListener("click", () => {
  if (shell.classList.contains("right-open")) closeRight_();
  else openRight();
});
closeRight.addEventListener("click", closeRight_);

// Mobile: tap the backdrop to close whichever drawer is open.
const backdrop = document.getElementById("drawer-backdrop");
backdrop?.addEventListener("click", closeAllDrawers);

// Escape closes any open drawer on mobile (matches the backdrop tap).
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape" || !mobileMQ.matches) return;
  if (!shell.classList.contains("left-collapsed") || shell.classList.contains("right-open")) {
    closeAllDrawers();
  }
});

// On narrow screens, default both drawers closed so the chat takes
// the whole viewport. Re-applied whenever the viewport crosses into
// mobile so a desktop-open right rail doesn't strand on rotate.
const applyMobileDefaults = () => {
  if (mobileMQ.matches) closeAllDrawers();
};
applyMobileDefaults();
mobileMQ.addEventListener?.("change", applyMobileDefaults);

locationalToggle?.addEventListener("change", () => {
  jfetch(`/api/conversations/${conversationId}/settings`, {
    method: "PUT",
    body: JSON.stringify({ locational_memory: locationalToggle.checked }),
  }).catch((e) => flashError("Save failed: " + e.message));
});
thinkingToggle?.addEventListener("change", () => {
  jfetch(`/api/conversations/${conversationId}/settings`, {
    method: "PUT",
    body: JSON.stringify({ enable_thinking: thinkingToggle.checked }),
  }).catch((e) => flashError("Save failed: " + e.message));
});
const imagePackToggle = document.getElementById("image-pack-toggle");
imagePackToggle?.addEventListener("change", () => {
  state.conversation.settings = state.conversation.settings || {};
  state.conversation.settings.image_pack_pick = imagePackToggle.checked;
  jfetch(`/api/conversations/${conversationId}/settings`, {
    method: "PUT",
    body: JSON.stringify({ image_pack_pick: imagePackToggle.checked }),
  }).catch((e) => flashError("Save failed: " + e.message));
});
const multiResponseToggle = document.getElementById("multi-response-toggle");
multiResponseToggle?.addEventListener("change", () => {
  state.conversation.settings = state.conversation.settings || {};
  state.conversation.settings.multi_response = multiResponseToggle.checked;
  jfetch(`/api/conversations/${conversationId}/settings`, {
    method: "PUT",
    body: JSON.stringify({ multi_response: multiResponseToggle.checked }),
  }).catch((e) => flashError("Save failed: " + e.message));
});

// ---------------------------------------------------------------------------
// Composer multi panel — "as X ⇄ to Y  [multi ▾]"
//
// Picks WHICH NPCs react on a multi-response turn and in WHAT order.
// Mechanics map 1:1 onto what the backend already reads:
//   - respond checkbox  → settings.multi_response_excluded (unchecked =
//     excluded; partners_for_lead skips them)
//   - ↑ / ↓ order       → settings.turn_order (partners_for_lead emits
//     partners in turn_order; also drives rotating turn mode)
// The reply-to character always speaks first (they're the lead) and the
// same-room filter still applies on top of these picks.
// ---------------------------------------------------------------------------
const composerMultiBtn = document.getElementById("composer-multi-btn");
const composerMultiPanel = document.getElementById("composer-multi-panel");

function _multiOrderedCastIds() {
  // settings.turn_order first (filtered to in-cast characters), then any
  // in-cast character the order list doesn't know yet, appended.
  const s = state.conversation.settings || {};
  const inCast = (id) =>
    id !== "user"
    && state.effectiveCastChars.has(id)
    && (state.entities[id] || {}).type === "character";
  const ordered = (s.turn_order || []).filter(inCast);
  for (const id of Object.keys(state.entities)) {
    if (inCast(id) && !ordered.includes(id)) ordered.push(id);
  }
  return ordered;
}

function _saveMultiSettings(patch) {
  state.conversation.settings = { ...(state.conversation.settings || {}), ...patch };
  jfetch(`/api/conversations/${conversationId}/settings`, {
    method: "PUT",
    body: JSON.stringify(patch),
  }).catch((e) => flashError("Save failed: " + e.message));
}

function renderComposerMultiPanel() {
  if (!composerMultiPanel) return;
  composerMultiPanel.innerHTML = "";
  const s = state.conversation.settings || {};
  const excluded = new Set(s.multi_response_excluded || []);
  const ordered = _multiOrderedCastIds();

  // Master toggle — same setting as the composer-actions "Multi" pill;
  // flipping here drives that checkbox so its save handler stays the
  // single writer.
  const head = document.createElement("label");
  head.className = "composer-multi-head";
  const master = document.createElement("input");
  master.type = "checkbox";
  master.checked = !!s.multi_response;
  master.addEventListener("change", () => {
    if (multiResponseToggle) {
      multiResponseToggle.checked = master.checked;
      multiResponseToggle.dispatchEvent(new Event("change"));
    } else {
      _saveMultiSettings({ multi_response: master.checked });
    }
  });
  head.appendChild(master);
  head.appendChild(document.createTextNode(" Multi responses"));
  composerMultiPanel.appendChild(head);

  const hint = document.createElement("p");
  hint.className = "muted small composer-multi-hint";
  hint.textContent = "Reply-to speaks first; checked NPCs in the same room react in this order.";
  composerMultiPanel.appendChild(hint);

  for (const id of ordered) {
    const row = document.createElement("div");
    row.className = "composer-multi-row";

    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = !excluded.has(id);
    cb.title = "React on multi-response turns";
    cb.addEventListener("change", () => {
      const ex = new Set((state.conversation.settings || {}).multi_response_excluded || []);
      if (cb.checked) ex.delete(id);
      else ex.add(id);
      _saveMultiSettings({ multi_response_excluded: [...ex] });
    });
    row.appendChild(cb);

    const name = document.createElement("span");
    name.className = "composer-multi-name";
    name.textContent = entityName(id);
    row.appendChild(name);

    const move = (delta) => {
      const order = _multiOrderedCastIds();
      const i = order.indexOf(id);
      const j = i + delta;
      if (i < 0 || j < 0 || j >= order.length) return;
      [order[i], order[j]] = [order[j], order[i]];
      _saveMultiSettings({ turn_order: order });
      renderComposerMultiPanel();
    };
    const up = document.createElement("button");
    up.type = "button";
    up.className = "ghost xs";
    up.textContent = "↑";
    up.title = "React earlier";
    up.addEventListener("click", () => move(-1));
    const down = document.createElement("button");
    down.type = "button";
    down.className = "ghost xs";
    down.textContent = "↓";
    down.title = "React later";
    down.addEventListener("click", () => move(1));
    row.appendChild(up);
    row.appendChild(down);
    composerMultiPanel.appendChild(row);
  }
  if (!ordered.length) {
    const empty = document.createElement("p");
    empty.className = "muted small";
    empty.textContent = "No NPCs in the cast.";
    composerMultiPanel.appendChild(empty);
  }
}

composerMultiBtn?.addEventListener("click", () => {
  if (!composerMultiPanel) return;
  if (composerMultiPanel.hidden) {
    renderComposerMultiPanel();
    composerMultiPanel.hidden = false;
  } else {
    composerMultiPanel.hidden = true;
  }
});
// Light-dismiss: click anywhere outside the panel/button closes it.
document.addEventListener("click", (ev) => {
  if (!composerMultiPanel || composerMultiPanel.hidden) return;
  if (composerMultiPanel.contains(ev.target) || composerMultiBtn?.contains(ev.target)) return;
  composerMultiPanel.hidden = true;
});

// ---------------------------------------------------------------------------
// Modules: live state, toolbar autoplay toggle + countdown, left-panel section
// ---------------------------------------------------------------------------
// The active list and per-module settings live on the active setup
// root's metadata (server-side: routes/api.py update_active_setup_modules).
// The client reads them off `state.conversation.messages[setupRoot].metadata`
// on every refresh and renders the toolbar / left panel accordingly.
const MODULE_MANIFESTS = (window.GEMMASIM_INITIAL.module_manifests || []).reduce(
  (acc, m) => { if (m && m.id) acc[m.id] = m; return acc; }, {}
);

function activeSetupRootMessage() {
  const id = state.activeSetupRootId;
  if (!id) return null;
  return state.conversation?.messages?.[id] || null;
}

function activeModuleIds() {
  const root = activeSetupRootMessage();
  const list = root?.metadata?.modules;
  return Array.isArray(list) ? list : [];
}

function moduleSettingsFor(mid) {
  const root = activeSetupRootMessage();
  const all = root?.metadata?.module_settings;
  if (!all || typeof all !== "object") return {};
  return all[mid] || {};
}

function isModuleActive(mid) {
  return activeModuleIds().includes(mid);
}

async function patchActiveSetupModules(payload) {
  const root = activeSetupRootMessage();
  if (!root) throw new Error("No active setup root.");
  const res = await jfetch(
    `/api/conversations/${conversationId}/active-setup/modules`,
    { method: "PUT", body: JSON.stringify(payload) },
  );
  // Mirror the canonical server response into local state so
  // subsequent reads (toolbar visibility, countdown) see fresh data
  // without waiting on a full conversation reload.
  const meta = root.metadata || (root.metadata = {});
  if (res && Array.isArray(res.modules)) meta.modules = res.modules;
  if (res && res.module_settings && typeof res.module_settings === "object") {
    meta.module_settings = res.module_settings;
  }
  // Notify drop-in modules so they can mount / unmount UI without
  // a page reload. The locked_image frame uses this; future modules
  // that gate UI on activation should too.
  if (window.Modules && Modules._fireActivationChange) Modules._fireActivationChange();
  return res;
}

// ---- Autoplay --------------------------------------------------------------
const autoplayToggleWrap = document.getElementById("autoplay-toggle-wrap");
const autoplayToggle = document.getElementById("autoplay-toggle");
const autoplayLabel = document.getElementById("autoplay-label");

let autoplayTimerId = null;
let autoplayDeadline = 0;
let autoplayTickId = null;

function setAutoplayLabel(text, opts = {}) {
  if (!autoplayLabel) return;
  autoplayLabel.textContent = text;
  if (opts.cancelable && autoplayToggleWrap) {
    autoplayToggleWrap.title = "Auto Play counting down — click the toggle to cancel.";
  } else if (autoplayToggleWrap) {
    autoplayToggleWrap.title = "Auto Play: after the narrator finishes a turn, automatically advance the lead character on a countdown timer.";
  }
}

function cancelAutoplayCountdown() {
  if (autoplayTimerId) { clearTimeout(autoplayTimerId); autoplayTimerId = null; }
  if (autoplayTickId) { clearInterval(autoplayTickId); autoplayTickId = null; }
  setAutoplayLabel("Auto");
}

function startAutoplayCountdown(leadPersona, seconds) {
  cancelAutoplayCountdown();
  const ms = Math.max(1, seconds | 0) * 1000;
  autoplayDeadline = performance.now() + ms;
  const renderTick = () => {
    const remaining = Math.max(0, autoplayDeadline - performance.now()) / 1000;
    setAutoplayLabel(`Auto · ${remaining.toFixed(1)}s`, { cancelable: true });
  };
  renderTick();
  autoplayTickId = setInterval(renderTick, 100);
  autoplayTimerId = setTimeout(() => {
    cancelAutoplayCountdown();
    if (!isAutoplayEnabled()) return;
    fireAutoplayTurn(leadPersona);
  }, ms);
}

// Generate the next turn for autoplay. Routes through the same
// streamGenerate call the Generate ↻ button uses, scheduled on the
// microtask queue so any in-flight cleanup from the previous turn
// (state.generating finally, IIFE side calls) has settled before we
// open a new SSE stream. A fresh POST → fresh placeholder bubble →
// fresh persisted message; never an in-place edit.
function fireAutoplayTurn(persona) {
  const responder = persona || autoplayResponder();
  if (!responder) {
    console.warn("autoplay: no responder available to fire");
    return;
  }
  if (state.generating) {
    console.debug("autoplay: defer fire — generation in flight");
    return;
  }
  console.debug(`autoplay: fire → ${responder}`);
  Promise.resolve().then(() => {
    if (state.generating) return;
    streamGenerate({ persona: responder }).catch((e) => {
      console.warn("autoplay turn failed:", e);
    });
  });
}

function isAutoplayEnabled() {
  if (!isModuleActive("autoplay")) return false;
  const s = moduleSettingsFor("autoplay");
  return !!s.enabled;
}

function autoplayResponder() {
  const s = moduleSettingsFor("autoplay");
  // In `turn_mode=auto`, the Reply-as dropdown is the source of
  // truth — the server parks the auto-picked next-responder there on
  // each `done` event, and the user can override by changing the
  // dropdown before the countdown fires. `lead_character` becomes a
  // fallback rather than the top-priority pick so the user-override
  // path works.
  const turnMode = state.conversation?.settings?.turn_mode || "manual";
  if (turnMode === "auto") {
    const r = (responderSelect && responderSelect.value) || "";
    if (r && r !== "narrator") return r;
    if (s.lead_character) return s.lead_character;
    if (typeof pickDefaultResponder === "function") {
      const d = pickDefaultResponder();
      if (d) return d;
    }
    for (const cid of state.effectiveCastChars) {
      if (cid !== "user") return cid;
    }
    return null;
  }
  // Legacy priority for `manual` / `rotating`: lead_character wins
  // when set, otherwise the dropdown, otherwise the default.
  if (s.lead_character) return s.lead_character;
  const r = (responderSelect && responderSelect.value) || "";
  if (r) return r;
  if (typeof pickDefaultResponder === "function") {
    const d = pickDefaultResponder();
    if (d) return d;
  }
  for (const cid of state.effectiveCastChars) {
    if (cid !== "user") return cid;
  }
  return null;
}

function maybeScheduleAutoplay(finishedMessage) {
  if (!finishedMessage) return;
  if (!isAutoplayEnabled()) return;
  if (state.generating) {
    console.debug("autoplay: skip — already generating");
    return;
  }
  // User turns don't trigger autoplay (the user is driving); every
  // other turn (character or narrator) triggers a wait + Generate.
  if (finishedMessage.persona === "user") return;
  // `turn_mode=auto` can explicitly hand the next turn to the user
  // (via `[next: user]` in the just-completed reply). The server flags
  // that on the done event; respect it by skipping the countdown so
  // the user can take the turn themselves.
  if (finishedMessage._next_is_user) {
    console.debug("autoplay: skip — next_responder is user");
    return;
  }
  const responder = autoplayResponder();
  if (!responder) {
    console.warn("autoplay: no responder available");
    return;
  }
  const settings = moduleSettingsFor("autoplay");
  const delay = Math.max(1, parseInt(settings.delay_seconds, 10) || 10);
  console.debug(
    `autoplay: ${finishedMessage.persona} done → wait ${delay}s → ${responder}`
  );
  startAutoplayCountdown(responder, delay);
}

function refreshAutoplayToolbarVisibility() {
  if (!autoplayToggleWrap || !autoplayToggle) return;
  const active = isModuleActive("autoplay");
  autoplayToggleWrap.hidden = !active;
  if (!active) {
    autoplayToggle.checked = false;
    cancelAutoplayCountdown();
    return;
  }
  const s = moduleSettingsFor("autoplay");
  autoplayToggle.checked = !!s.enabled;
  if (!s.enabled) cancelAutoplayCountdown();
}

autoplayToggle?.addEventListener("change", async () => {
  const cur = moduleSettingsFor("autoplay");
  try {
    await patchActiveSetupModules({
      module_settings: { autoplay: { ...cur, enabled: autoplayToggle.checked } },
    });
  } catch (e) {
    flashError("Auto Play save failed: " + e.message);
    autoplayToggle.checked = !autoplayToggle.checked;
    return;
  }
  if (!autoplayToggle.checked) {
    cancelAutoplayCountdown();
    return;
  }
  // Kick the first turn immediately on enable so the user doesn't
  // have to press Send/Generate manually to start the loop. If a
  // generation is in flight (rare — the toggle is in the toolbar),
  // skip — the post-completion hook will schedule the next one.
  fireAutoplayTurn();
});

// Any user-initiated action that means "I'm taking the turn" cancels
// the pending auto-advance.
composerInput?.addEventListener("input", cancelAutoplayCountdown);
composerInput?.addEventListener("focus", cancelAutoplayCountdown);
generateOnlyBtn?.addEventListener("click", cancelAutoplayCountdown);
composer?.addEventListener("submit", cancelAutoplayCountdown);

// ---- Left-panel Modules section -------------------------------------------
const modulesSection = document.getElementById("modules-section");
const modulesList = document.getElementById("modules-list");

function renderLeftPanelModules() {
  if (!modulesSection || !modulesList) return;
  const root = activeSetupRootMessage();
  // Module on/off + settings live on the active setup root's metadata, so a
  // branch with no setup root (legacy conversation) has nowhere to persist a
  // toggle — hide the section, as before.
  if (!root) {
    modulesSection.hidden = true;
    modulesList.innerHTML = "";
    return;
  }
  const meta = root.metadata || {};
  // Modules are universally available (mirrors the backend's
  // modules.list_for_scenario): list EVERY registered manifest, not just
  // the scenario's `available_modules`. That field is now only an ordering
  // hint — declared ids float to the top, the rest follow alphabetically.
  const allIds = Object.keys(MODULE_MANIFESTS);
  if (!allIds.length) {
    modulesSection.hidden = true;
    modulesList.innerHTML = "";
    return;
  }
  const declared = (Array.isArray(meta.setup?.available_modules) ? meta.setup.available_modules : [])
    .filter((m) => MODULE_MANIFESTS[m]);
  const seen = new Set(declared);
  const ordered = [...declared, ...allIds.filter((m) => !seen.has(m)).sort()];
  modulesSection.hidden = false;
  const active = new Set(Array.isArray(meta.modules) ? meta.modules : []);
  modulesList.innerHTML = "";
  for (const mid of ordered) {
    const manifest = MODULE_MANIFESTS[mid];
    if (!manifest) continue;
    const card = document.createElement("div");
    card.className = "module-card";
    card.style.border = "1px solid var(--border, #444)";
    card.style.borderRadius = "6px";
    card.style.padding = "6px 8px";
    card.style.marginTop = "6px";

    const head = document.createElement("label");
    head.style.display = "flex";
    head.style.gap = "8px";
    head.style.alignItems = "baseline";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = active.has(mid);
    head.appendChild(cb);
    const nameSpan = document.createElement("strong");
    nameSpan.textContent = manifest.name || mid;
    head.appendChild(nameSpan);
    card.appendChild(head);

    if (manifest.description) {
      const desc = document.createElement("p");
      desc.className = "muted small";
      desc.style.margin = "4px 0 0 20px";
      desc.textContent = manifest.description;
      card.appendChild(desc);
    }

    const settingsBlock = document.createElement("div");
    settingsBlock.style.display = cb.checked ? "flex" : "none";
    settingsBlock.style.flexDirection = "column";
    settingsBlock.style.gap = "4px";
    settingsBlock.style.marginTop = "6px";
    settingsBlock.style.paddingLeft = "20px";

    const liveSettings = { ...moduleSettingsFor(mid) };
    const onSettingChange = async (sid, value) => {
      try {
        await patchActiveSetupModules({
          module_settings: { [mid]: { ...liveSettings } },
        });
      } catch (e) {
        flashError("Save failed: " + e.message);
        return;
      }
      // Surface dependent UI immediately (e.g., the autoplay toolbar
      // enabled state mirrors module_settings.autoplay.enabled).
      if (mid === "autoplay") refreshAutoplayToolbarVisibility();
    };
    for (const s of manifest.settings || []) {
      if (!s || !s.live_editable) continue;
      const row = renderModuleSettingControl(manifest, s, liveSettings, null, onSettingChange);
      if (row) settingsBlock.appendChild(row);
    }
    card.appendChild(settingsBlock);

    cb.addEventListener("change", async () => {
      const next = new Set(active);
      if (cb.checked) next.add(mid); else next.delete(mid);
      try {
        await patchActiveSetupModules({ modules: Array.from(next) });
      } catch (e) {
        flashError("Save failed: " + e.message);
        cb.checked = !cb.checked;
        return;
      }
      settingsBlock.style.display = cb.checked ? "flex" : "none";
      refreshAutoplayToolbarVisibility();
    });

    modulesList.appendChild(card);
  }
}

function refreshModulesUI() {
  refreshAutoplayToolbarVisibility();
  renderLeftPanelModules();
}

// Wire up the initial refresh + every active-leaf change so module
// state stays in sync as the user navigates branches.
refreshModulesUI();
const autoStateToggle = document.getElementById("auto-state-toggle");
const autoStateOnUserToggle = document.getElementById("auto-state-on-user-toggle");
const autoStateOnNarratorToggle = document.getElementById("auto-state-on-narrator-toggle");

// Per-aspect Auto State toggles (Transparency, Location). Each saves its
// own settings key; the /auto_state route runs whichever are enabled.
function _wireAutoStateAspect(elId, key) {
  const el = document.getElementById(elId);
  el?.addEventListener("change", () => {
    state.conversation.settings = state.conversation.settings || {};
    state.conversation.settings[key] = el.checked;
    jfetch(`/api/conversations/${conversationId}/settings`, {
      method: "PUT",
      body: JSON.stringify({ [key]: el.checked }),
    }).catch((e) => flashError("Save failed: " + e.message));
  });
}
_wireAutoStateAspect("auto-state-transparency-toggle", "auto_state_transparency");
_wireAutoStateAspect("auto-state-location-toggle", "auto_state_location");
function _syncAutoStateSubToggleAvailability() {
  // Sub-toggles are only meaningful when the parent is on. Disable +
  // visually grey them out when parent is off; user can still see
  // they exist but can't flip until they enable the parent.
  const parentOn = !!autoStateToggle?.checked;
  for (const sub of [autoStateOnUserToggle, autoStateOnNarratorToggle]) {
    if (!sub) continue;
    sub.disabled = !parentOn;
    const wrap = sub.closest("label");
    if (wrap) wrap.style.opacity = parentOn ? "" : "0.55";
  }
}
_syncAutoStateSubToggleAvailability();
autoStateToggle?.addEventListener("change", () => {
  state.conversation.settings = state.conversation.settings || {};
  state.conversation.settings.auto_state_changes = autoStateToggle.checked;
  jfetch(`/api/conversations/${conversationId}/settings`, {
    method: "PUT",
    body: JSON.stringify({ auto_state_changes: autoStateToggle.checked }),
  }).catch((e) => flashError("Save failed: " + e.message));
  _syncAutoStateSubToggleAvailability();
});
autoStateOnUserToggle?.addEventListener("change", () => {
  state.conversation.settings = state.conversation.settings || {};
  state.conversation.settings.auto_state_on_user_messages = autoStateOnUserToggle.checked;
  jfetch(`/api/conversations/${conversationId}/settings`, {
    method: "PUT",
    body: JSON.stringify({ auto_state_on_user_messages: autoStateOnUserToggle.checked }),
  }).catch((e) => flashError("Save failed: " + e.message));
});
autoStateOnNarratorToggle?.addEventListener("change", () => {
  state.conversation.settings = state.conversation.settings || {};
  state.conversation.settings.auto_state_on_narrator_messages = autoStateOnNarratorToggle.checked;
  jfetch(`/api/conversations/${conversationId}/settings`, {
    method: "PUT",
    body: JSON.stringify({ auto_state_on_narrator_messages: autoStateOnNarratorToggle.checked }),
  }).catch((e) => flashError("Save failed: " + e.message));
});
mentionToggle?.addEventListener("change", () => {
  jfetch(`/api/conversations/${conversationId}/settings`, {
    method: "PUT",
    body: JSON.stringify({ auto_responder_by_mention: mentionToggle.checked }),
  }).catch((e) => flashError("Save failed: " + e.message));
});

autoApplyEditsToggle?.addEventListener("change", () => {
  jfetch(`/api/conversations/${conversationId}/settings`, {
    method: "PUT",
    body: JSON.stringify({ auto_apply_narrator_edits: autoApplyEditsToggle.checked }),
  }).catch((e) => flashError("Save failed: " + e.message));
});

// ---------------------------------------------------------------------------
// Narrator additions panel
// ---------------------------------------------------------------------------
// ---------------------------------------------------------------------------
// Narrator controls (per-conversation settings)
// ---------------------------------------------------------------------------
// Five controls that gate what the narrator can see / pick when it
// runs (narrator-add + narrator-edit + the post-message narrator
// button). Stored under conv.settings.narrator_controls; consumed by
// narrator_add._build_add_world_summary + narrator_apply (default
// materialize template).
const narratorCtrlPreferGeneric = document.getElementById("narrator-control-prefer-generic");
const narratorCtrlAllowOffCast = document.getElementById("narrator-control-allow-off-cast");
const narratorCtrlWhitelist = document.getElementById("narrator-control-whitelist");
const narratorCtrlCustomData = document.getElementById("narrator-control-custom-data");
const narratorCtrlDefaultTemplate = document.getElementById("narrator-control-default-template");
// Automation → Narrator toggles. "Make characters" maps to
// character_creation_mode (on = "full" = derive + custom); "Make pairs"
// maps to edit_pairs. Both persist under conv.settings.narrator_controls.
const narratorMakeCharacters = document.getElementById("narrator-make-characters");
const narratorMakePairs = document.getElementById("narrator-make-pairs");
const narratorControlsStatus = document.getElementById("narrator-controls-status");

function _loadNarratorControlsFromSettings() {
  const s = (state.conversation?.settings || {}).narrator_controls || {};
  if (narratorCtrlPreferGeneric) {
    // Default true when undefined — prefer generic off-cast templates.
    narratorCtrlPreferGeneric.checked = s.prefer_generic !== false;
  }
  if (narratorCtrlAllowOffCast) {
    // Default false when undefined — off-cast pool opt-in.
    narratorCtrlAllowOffCast.checked = s.allow_off_cast === true;
  }
  if (narratorCtrlWhitelist) {
    const wl = Array.isArray(s.off_cast_whitelist) ? s.off_cast_whitelist : [];
    narratorCtrlWhitelist.value = wl.join(", ");
  }
  if (narratorCtrlCustomData) {
    narratorCtrlCustomData.value = s.custom_character_data || "";
  }
  if (narratorCtrlDefaultTemplate) {
    narratorCtrlDefaultTemplate.value = s.default_materialize_template || "";
  }
  if (narratorMakeCharacters) {
    // On when the creation mode is anything but "off".
    narratorMakeCharacters.checked = !!s.character_creation_mode && s.character_creation_mode !== "off";
  }
  if (narratorMakePairs) {
    narratorMakePairs.checked = s.edit_pairs === true;
  }
}
_loadNarratorControlsFromSettings();

function _saveNarratorControls() {
  const wlRaw = (narratorCtrlWhitelist?.value || "").trim();
  const whitelist = wlRaw
    ? wlRaw.split(",").map((s) => s.trim()).filter(Boolean)
    : null;
  const payload = {
    narrator_controls: {
      prefer_generic: !!narratorCtrlPreferGeneric?.checked,
      allow_off_cast: !!narratorCtrlAllowOffCast?.checked,
      off_cast_whitelist: whitelist,
      custom_character_data: narratorCtrlCustomData?.value || "",
      default_materialize_template: narratorCtrlDefaultTemplate?.value || "",
      character_creation_mode: narratorMakeCharacters?.checked ? "full" : "off",
      edit_pairs: !!narratorMakePairs?.checked,
    },
  };
  state.conversation.settings = state.conversation.settings || {};
  state.conversation.settings.narrator_controls = payload.narrator_controls;
  if (narratorControlsStatus) narratorControlsStatus.textContent = "Saving…";
  jfetch(`/api/conversations/${conversationId}/settings`, {
    method: "PUT",
    body: JSON.stringify(payload),
  })
    .then(() => {
      if (narratorControlsStatus) narratorControlsStatus.textContent = "Saved.";
    })
    .catch((e) => {
      if (narratorControlsStatus) {
        narratorControlsStatus.textContent = "Save failed: " + e.message;
      }
    });
}

narratorCtrlPreferGeneric?.addEventListener("change", _saveNarratorControls);
narratorCtrlAllowOffCast?.addEventListener("change", _saveNarratorControls);
narratorCtrlWhitelist?.addEventListener("change", _saveNarratorControls);
narratorCtrlCustomData?.addEventListener("change", _saveNarratorControls);
narratorCtrlDefaultTemplate?.addEventListener("change", _saveNarratorControls);
narratorMakeCharacters?.addEventListener("change", _saveNarratorControls);
narratorMakePairs?.addEventListener("change", _saveNarratorControls);

// Side-channel directive entry that operates on the active leaf. Same
// prompt + plumbing as the per-message Narrator button — this just lets
// you type a directive without picking a specific message first.
const narratorAdditionsDirective = document.getElementById("narrator-additions-directive");
const sendNarratorAdditionBtn = document.getElementById("send-narrator-addition");
const narratorAdditionsStatus = document.getElementById("narrator-additions-status");

sendNarratorAdditionBtn?.addEventListener("click", async () => {
  if (state.generating) return;
  const directive = (narratorAdditionsDirective?.value || "").trim();
  if (!directive) {
    narratorAdditionsDirective?.focus();
    return;
  }
  const leafId = state.conversation?.active_path_leaf;
  if (!leafId) {
    narratorAdditionsStatus.textContent = "No active message to attach the rewrite to.";
    return;
  }
  sendNarratorAdditionBtn.disabled = true;
  narratorAdditionsStatus.textContent = "Sending…";
  state.generating = true;
  setSendButtonStop(true);

  try {
    const r = await fetch(
      `/api/conversations/${conversationId}/narrator-add/${leafId}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
        body: JSON.stringify({ directive }),
      },
    );
    if (!r.ok || !r.body) {
      const err = await r.json().catch(() => ({}));
      throw new Error(err.error || `HTTP ${r.status}`);
    }
    // Drain the SSE stream — we don't render deltas inline (no per-message
    // composer here), but we still need to consume the body so the server
    // runs the persist() callback. Once we see the "done" event, reload.
    const reader = r.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let editsApplied = 0;
    let elapsedDeltaCount = 0;
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let nl;
      while ((nl = buffer.indexOf("\n\n")) !== -1) {
        const event = buffer.slice(0, nl);
        buffer = buffer.slice(nl + 2);
        for (const line of event.split("\n")) {
          if (!line.startsWith("data: ")) continue;
          let payload;
          try { payload = JSON.parse(line.slice(6)); } catch { continue; }
          if (payload.type === "delta") {
            elapsedDeltaCount += 1;
            narratorAdditionsStatus.textContent = `Streaming… ${elapsedDeltaCount} chunks`;
          } else if (payload.type === "done") {
            editsApplied = (payload.applied || []).length;
          } else if (payload.type === "error") {
            throw new Error(payload.error || "narrator-add failed");
          }
        }
      }
    }
    narratorAdditionsStatus.textContent =
      editsApplied > 0
        ? `Applied ${editsApplied} edit${editsApplied === 1 ? "" : "s"}.`
        : "Sent — no edits emitted.";
    narratorAdditionsDirective.value = "";
    await reloadConversation();
  } catch (e) {
    narratorAdditionsStatus.textContent = "Failed: " + e.message;
  } finally {
    state.generating = false;
    setSendButtonStop(false);
    sendNarratorAdditionBtn.disabled = false;
  }
});

// Conversation summary
const summaryText = document.getElementById("summary-text");
const saveSummaryBtn = document.getElementById("save-summary");
const summaryStatus = document.getElementById("summary-status");
saveSummaryBtn?.addEventListener("click", async () => {
  try {
    await jfetch(`/api/conversations/${conversationId}/settings`, {
      method: "PUT",
      body: JSON.stringify({ summary: summaryText.value }),
    });
    state.conversation.settings.summary = summaryText.value;
    summaryStatus.textContent = "Saved.";
    summaryStatus.dataset.kind = "ok";
  } catch (e) {
    summaryStatus.textContent = "Failed: " + e.message;
    summaryStatus.dataset.kind = "error";
  }
});

const clearSummaryBtn = document.getElementById("clear-summary");
clearSummaryBtn?.addEventListener("click", async () => {
  if (!summaryText.value && !state.conversation.settings.summary) return;
  if (!confirm("Wipe the running summary? The model will lose its long-term memory of the conversation.")) return;
  try {
    await jfetch(`/api/conversations/${conversationId}/settings`, {
      method: "PUT",
      body: JSON.stringify({ summary: "", summary_anchor_ids: [] }),
    });
    summaryText.value = "";
    state.conversation.settings.summary = "";
    state.conversation.settings.summary_anchor_ids = [];
    summaryStatus.textContent = "Cleared.";
    summaryStatus.dataset.kind = "ok";
  } catch (e) {
    summaryStatus.textContent = "Failed: " + e.message;
    summaryStatus.dataset.kind = "error";
  }
});

function pickResponderFromMention(text) {
  if (!mentionToggle?.checked) return null;
  const lc = text.toLowerCase();
  const persona = personaSelect.value;
  // Match each character's name, longest first to avoid prefix collisions.
  const candidates = Object.entries(state.entities)
    .filter(([id, e]) => e.type === "character" && id !== persona)
    .map(([id, e]) => ({ id, name: (e.name || "").trim() }))
    .filter((c) => c.name)
    .sort((a, b) => b.name.length - a.name.length);
  for (const c of candidates) {
    const re = new RegExp(`\\b${c.name.replace(/[.*+?^${}()|[\\]\\\\]/g, "\\$&")}\\b`, "i");
    if (re.test(text)) return c.id;
    // Also match the first word of the name.
    const first = c.name.split(/\s+/)[0];
    if (first && first.length >= 3) {
      const reF = new RegExp(`\\b${first.replace(/[.*+?^${}()|[\\]\\\\]/g, "\\$&")}\\b`, "i");
      if (reF.test(text)) return c.id;
    }
  }
  return null;
}
narratorMode?.addEventListener("change", () => {
  jfetch(`/api/conversations/${conversationId}/settings`, {
    method: "PUT",
    body: JSON.stringify({ narrator_mode: narratorMode.value }),
  }).catch((e) => flashError("Save failed: " + e.message));
});

const turnMode = document.getElementById("turn-mode");
turnMode?.addEventListener("change", () => {
  jfetch(`/api/conversations/${conversationId}/settings`, {
    method: "PUT",
    body: JSON.stringify({ turn_mode: turnMode.value }),
  }).catch((e) => flashError("Save failed: " + e.message));
});

// ---------------------------------------------------------------------------
// Active-setup panel: Scenario instructions / Scenario edits / Applied edits
//
// The active sub-scenario's prompt fields and edit log live on the active
// root message's metadata.setup. We fetch them via /active-setup, which
// follows the active path so a sibling-root switch reloads the panel.
// ---------------------------------------------------------------------------
const scInstrBase = document.getElementById("scenario-instructions-base");
const scInstrAppend = document.getElementById("scenario-instructions-append");
const saveScInstrBtn = document.getElementById("save-scenario-instructions");
const scInstrStatus = document.getElementById("scenario-instructions-status");
const scStateText = document.getElementById("scenario-state");
const saveScStateBtn = document.getElementById("save-scenario-state");
const scStateStatus = document.getElementById("scenario-state-status");
const appliedTimeline = document.getElementById("applied-edits-timeline");

let activeSetupCache = { setup: {}, applied_edits_timeline: [], setup_root_id: null };
// Remembers whether each Applied-edits list (Active / All) is collapsed,
// so a repaint (after an edit / revert) doesn't reset the user's choice.
const timelineOpenState = {};
let activeSetupLeafLoaded = null;
let activeSetupInflight = null;

async function loadActiveSetup({ force = false } = {}) {
  // fullRender() can run dozens of times per turn (streaming chunks,
  // sibling switches, regen, edits). Skip the round-trip when the
  // active leaf hasn't actually changed; the panel content is a pure
  // function of the path. `force=true` lets save handlers refresh
  // after they mutate the active root.
  const leaf = state?.conversation?.active_path_leaf || "";
  if (!force && leaf === activeSetupLeafLoaded) return;
  if (activeSetupInflight) return activeSetupInflight;
  activeSetupInflight = (async () => {
    try {
      activeSetupCache = await jfetch(
        `/api/conversations/${conversationId}/active-setup`
      );
      activeSetupLeafLoaded = leaf;
    } catch (e) {
      activeSetupCache = { setup: {}, applied_edits_timeline: [], setup_root_id: null };
    } finally {
      activeSetupInflight = null;
    }
    paintActiveSetupPanel();
  })();
  return activeSetupInflight;
}

function paintActiveSetupPanel() {
  const setup = activeSetupCache.setup || {};
  if (scInstrBase) scInstrBase.value = setup.scenario_instructions_base || "";
  if (scInstrAppend) scInstrAppend.value = setup.scenario_instructions_append || "";
  if (scStateText) scStateText.value = setup.state || "";
  // Pre-fill the prompt textareas (system prompt / author note) from the
  // setup if the conversation hasn't already overridden them via settings.
  // The conversation creation already copied scenario fields into settings,
  // so this is a passive refresh — only fills empty textareas.
  if (sysCharacter && !sysCharacter.value && setup.system_prompt_character) {
    sysCharacter.value = setup.system_prompt_character;
  }
  if (sysNarrator && !sysNarrator.value && setup.system_prompt_narrator) {
    sysNarrator.value = setup.system_prompt_narrator;
  }
  if (authorNote && !authorNote.value && setup.author_note) {
    authorNote.value = setup.author_note;
  }
  paintAppliedEditsTimeline();
}

// Short clock time for an edit, e.g. "14:07". Full date/time in the title.
function fmtEditTime(sec) {
  if (!sec && sec !== 0) return "";
  const d = new Date(sec * 1000);
  if (isNaN(d.getTime())) return "";
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

// Scroll the transcript to the message that produced an edit, and flash it.
function jumpToEditMessage(mid) {
  const el = document.querySelector(`[data-message-id="${mid}"]`);
  if (!el) return;
  el.scrollIntoView({ behavior: "smooth", block: "center" });
  el.classList.add("edit-jump-flash");
  setTimeout(() => el.classList.remove("edit-jump-flash"), 1200);
}

function buildTimelineRow(entry) {
  const row = document.createElement("div");
  row.className = "applied-edit-row";
  if (entry.ok === false) row.classList.add("failed");
  if (entry.reverted_at) row.classList.add("reverted");
  if (!entry._active) row.classList.add("inactive");

  const tag = document.createElement("span");
  tag.className = "edit-origin-tag";
  tag.textContent = entry._origin || "narrator";
  tag.title = `from ${entry._persona || "narrator"} (${entry._message_id})`;
  row.appendChild(tag);

  const label = document.createElement("span");
  label.className = "edit-summary";
  label.textContent = summarizeEdit(entry);
  label.title = "Click to jump to the message that made this edit";
  label.style.cursor = "pointer";
  label.addEventListener("click", () => jumpToEditMessage(entry._message_id));
  row.appendChild(label);

  const when = fmtEditTime(entry._made_at);
  if (when) {
    const t = document.createElement("span");
    t.className = "edit-time";
    t.textContent = when;
    t.title = new Date(entry._made_at * 1000).toLocaleString();
    row.appendChild(t);
  }

  if (entry.ok === false && entry.error) {
    const err = document.createElement("span");
    err.className = "edit-chip-error";
    err.textContent = entry.error;
    row.appendChild(err);
  }

  if (entry.ok !== false && !entry.reverted_at) {
    const undo = document.createElement("button");
    undo.type = "button";
    undo.className = "edit-chip-undo";
    undo.title = "Revert this change";
    undo.textContent = "×";
    undo.addEventListener("click", async () => {
      undo.disabled = true;
      try {
        const r = await jfetch(
          `/api/conversations/${conversationId}/messages/${entry._message_id}/revert-edit`,
          { method: "POST", body: JSON.stringify({ index: entry._index }) }
        );
        if (r.message) {
          state.conversation.messages[r.message.id] = r.message;
          const old = document.querySelector(`[data-message-id="${r.message.id}"]`);
          if (old) old.replaceWith(renderMessage(r.message));
        }
        if (r.affected_entities) {
          for (const [id, e] of Object.entries(r.affected_entities)) {
            state.entities[id] = e;
          }
        }
        flashInfo("Reverted.");
        renderQuickEdits();
        loadActiveSetup({ force: true });
      } catch (e) {
        flashError("Revert failed: " + e.message);
        undo.disabled = false;
      }
    });
    row.appendChild(undo);
  } else if (entry.reverted_at) {
    const tag2 = document.createElement("span");
    tag2.className = "muted small";
    tag2.textContent = "(reverted)";
    row.appendChild(tag2);
  }
  return row;
}

function paintAppliedEditsTimeline() {
  if (!appliedTimeline) return;
  appliedTimeline.innerHTML = "";
  const entries = (activeSetupCache.applied_edits_timeline || []).slice().reverse();
  if (!entries.length) {
    const empty = document.createElement("p");
    empty.className = "muted small";
    empty.textContent = "(no edits applied on this path)";
    appliedTimeline.appendChild(empty);
    return;
  }
  // Two lists: what's currently in effect on this branch, and the full log
  // (reverted / superseded included, marked). Active edits are the ones the
  // backend flags `_active` — effect still present in the current state.
  const active = entries.filter((e) => e._active);
  const section = (title, rows, key) => {
    // Each list is a collapsible <details> (minusable) with its own scroll
    // area. Remember open/closed per list across repaints.
    const det = document.createElement("details");
    det.className = "timeline-section";
    const stored = timelineOpenState[key];
    det.open = stored === undefined ? true : stored;
    det.addEventListener("toggle", () => { timelineOpenState[key] = det.open; });
    const sum = document.createElement("summary");
    sum.className = "timeline-heading";
    sum.textContent = `${title} (${rows.length})`;
    det.appendChild(sum);
    const body = document.createElement("div");
    body.className = "timeline-rows";
    if (!rows.length) {
      const p = document.createElement("p");
      p.className = "muted small";
      p.textContent = "(none)";
      body.appendChild(p);
    } else {
      for (const e of rows) body.appendChild(buildTimelineRow(e));
    }
    det.appendChild(body);
    appliedTimeline.appendChild(det);
  };
  section("Active", active, "active");
  section("All on this branch", entries, "all");
}

saveScInstrBtn?.addEventListener("click", async () => {
  try {
    await jfetch(`/api/conversations/${conversationId}/active-setup`, {
      method: "PUT",
      body: JSON.stringify({
        scenario_instructions_base: scInstrBase.value,
        scenario_instructions_append: scInstrAppend.value,
      }),
    });
    scInstrStatus.textContent = "Saved.";
    scInstrStatus.dataset.kind = "ok";
    loadActiveSetup({ force: true });
  } catch (e) {
    scInstrStatus.textContent = "Failed: " + e.message;
    scInstrStatus.dataset.kind = "error";
  }
});

saveScStateBtn?.addEventListener("click", async () => {
  try {
    await jfetch(`/api/conversations/${conversationId}/active-setup`, {
      method: "PUT",
      body: JSON.stringify({ state: scStateText.value }),
    });
    scStateStatus.textContent = "Saved (re-seed to apply).";
    scStateStatus.dataset.kind = "ok";
    loadActiveSetup({ force: true });
  } catch (e) {
    scStateStatus.textContent = "Failed: " + e.message;
    scStateStatus.dataset.kind = "error";
  }
});

// System prompt template editor
const sysCharacter = document.getElementById("sys-character");
const sysNarrator = document.getElementById("sys-narrator");
const saveSysBtn = document.getElementById("save-sys-prompts");
const resetSysBtn = document.getElementById("reset-sys-prompts");
const resetSysScenarioBtn = document.getElementById("reset-sys-prompts-scenario");
const sysStatus = document.getElementById("sys-prompts-status");
let sysDefaults = null;
async function loadSysDefaults() {
  if (sysDefaults) return sysDefaults;
  try {
    sysDefaults = await jfetch("/api/prompt-defaults");
  } catch (e) {
    sysDefaults = { system_prompt_character: "", system_prompt_narrator: "" };
  }
  if (sysCharacter && !sysCharacter.value) sysCharacter.placeholder = sysDefaults.system_prompt_character;
  if (sysNarrator && !sysNarrator.value) sysNarrator.placeholder = sysDefaults.system_prompt_narrator;
  return sysDefaults;
}
loadSysDefaults();

saveSysBtn?.addEventListener("click", async () => {
  try {
    await jfetch(`/api/conversations/${conversationId}/settings`, {
      method: "PUT",
      body: JSON.stringify({
        system_prompt_character: sysCharacter.value,
        system_prompt_narrator: sysNarrator.value,
      }),
    });
    sysStatus.textContent = "Saved.";
    sysStatus.dataset.kind = "ok";
  } catch (e) {
    sysStatus.textContent = "Failed: " + e.message;
    sysStatus.dataset.kind = "error";
  }
});

resetSysBtn?.addEventListener("click", async () => {
  const d = await loadSysDefaults();
  sysCharacter.value = d.system_prompt_character || "";
  sysNarrator.value = d.system_prompt_narrator || "";
  saveSysBtn.click();
});

resetSysScenarioBtn?.addEventListener("click", async () => {
  const setup = activeSetupCache.setup || {};
  sysCharacter.value = setup.system_prompt_character || "";
  sysNarrator.value = setup.system_prompt_narrator || "";
  saveSysBtn.click();
});

// Author's note + post-history persistence
const authorNote = document.getElementById("author-note");
const authorNoteDepth = document.getElementById("author-note-depth");
const saveAuthorBtn = document.getElementById("save-author-note");
const authorStatus = document.getElementById("author-note-status");

// Per-character author's notes: a live textarea per in-scene character,
// edited here (not at staging). Re-rendered whenever the section opens
// so it reflects the current cast.
const authorNotePerCharWrap = document.getElementById("author-note-per-character");
const authorNotePerCharInputs = {};
function renderAuthorNotePerCharacter() {
  if (!authorNotePerCharWrap) return;
  authorNotePerCharWrap.innerHTML = "";
  for (const k in authorNotePerCharInputs) delete authorNotePerCharInputs[k];
  const saved = (state.conversation?.settings || {}).author_note_per_character || {};
  // Follow the current branch's effective cast (not the base scenario
  // roster) — same source the cast / quick-edits widgets filter against.
  const castIds = (state.effectiveCastChars instanceof Set)
    ? [...state.effectiveCastChars].filter((id) => id !== "user")
    : Object.values(state.entities || {})
        .filter((e) => e && e.type === "character" && e.id !== "user")
        .map((e) => e.id);
  if (!castIds.length) {
    const m = document.createElement("div");
    m.className = "muted small";
    m.textContent = "No characters in scene yet.";
    authorNotePerCharWrap.appendChild(m);
    return;
  }
  for (const cid of castIds) {
    const name = (state.entities[cid] || {}).name || cid;
    const lab = document.createElement("label");
    lab.className = "stack";
    const sp = document.createElement("span");
    sp.className = "muted small";
    sp.textContent = name;
    const ta = document.createElement("textarea");
    ta.rows = 2;
    ta.placeholder = "Note for " + name + " only";
    ta.value = saved[cid] || "";
    lab.appendChild(sp);
    lab.appendChild(ta);
    authorNotePerCharWrap.appendChild(lab);
    authorNotePerCharInputs[cid] = ta;
  }
}
function collectAuthorNotePerCharacter() {
  const out = {};
  for (const [cid, ta] of Object.entries(authorNotePerCharInputs)) {
    if (ta.value.trim()) out[cid] = ta.value;
  }
  return out;
}
renderAuthorNotePerCharacter();
document.getElementById("author-note-section")?.addEventListener("toggle", (e) => {
  if (e.target.open) renderAuthorNotePerCharacter();
});

saveAuthorBtn?.addEventListener("click", async () => {
  try {
    const perChar = collectAuthorNotePerCharacter();
    await jfetch(`/api/conversations/${conversationId}/settings`, {
      method: "PUT",
      body: JSON.stringify({
        author_note: authorNote.value,
        author_note_depth: parseInt(authorNoteDepth.value, 10) || 1,
        author_note_per_character: perChar,
      }),
    });
    if (state.conversation) {
      state.conversation.settings = state.conversation.settings || {};
      state.conversation.settings.author_note_per_character = perChar;
    }
    authorStatus.textContent = "Saved.";
    authorStatus.dataset.kind = "ok";
  } catch (e) {
    authorStatus.textContent = "Failed: " + e.message;
    authorStatus.dataset.kind = "error";
  }
});

const resetAuthorScenarioBtn = document.getElementById("reset-author-note-scenario");
resetAuthorScenarioBtn?.addEventListener("click", () => {
  const setup = activeSetupCache.setup || {};
  authorNote.value = setup.author_note || "";
  if (setup.author_note_depth != null) {
    authorNoteDepth.value = setup.author_note_depth;
  }
  saveAuthorBtn.click();
});

const postHistory = document.getElementById("post-history");
const savePostBtn = document.getElementById("save-post-history");
const postStatus = document.getElementById("post-history-status");
savePostBtn?.addEventListener("click", async () => {
  try {
    await jfetch(`/api/conversations/${conversationId}/settings`, {
      method: "PUT",
      body: JSON.stringify({ post_history_instructions: postHistory.value }),
    });
    postStatus.textContent = "Saved.";
    postStatus.dataset.kind = "ok";
  } catch (e) {
    postStatus.textContent = "Failed: " + e.message;
    postStatus.dataset.kind = "error";
  }
});

// Banned phrases — global setting, but the editor lives next to the
// per-conversation system prompt because that's where prompt-related
// controls cluster. Loads from /api/settings, saves a deep-merged
// fragment back to it.
const bannedPhrasesEl = document.getElementById("sidebar-banned-phrases");
const saveBannedBtn = document.getElementById("save-banned-phrases");
const bannedStatus = document.getElementById("banned-phrases-status");
async function loadBannedPhrases() {
  if (!bannedPhrasesEl) return;
  try {
    const s = await jfetch("/api/settings");
    const list = (((s.defaults || {}).style_discipline || {}).banned_phrases) || [];
    bannedPhrasesEl.value = list.join("\n");
  } catch (e) {
    bannedStatus.textContent = "Couldn't load list: " + e.message;
    bannedStatus.dataset.kind = "error";
  }
}
saveBannedBtn?.addEventListener("click", async () => {
  const phrases = bannedPhrasesEl.value
    .split("\n").map((s) => s.trim()).filter(Boolean);
  bannedStatus.textContent = "Saving…";
  bannedStatus.dataset.kind = "neutral";
  try {
    await jfetch("/api/settings", {
      method: "PUT",
      body: JSON.stringify({
        defaults: { style_discipline: { banned_phrases: phrases } },
      }),
    });
    bannedStatus.textContent = `Saved · ${phrases.length} phrase${phrases.length === 1 ? "" : "s"}.`;
    bannedStatus.dataset.kind = "ok";
  } catch (e) {
    bannedStatus.textContent = "Failed: " + e.message;
    bannedStatus.dataset.kind = "error";
  }
});
loadBannedPhrases();

// Persona persistence
const personaName = document.getElementById("persona-name");
const personaDesc = document.getElementById("persona-desc");
const personaTags = document.getElementById("persona-tags");
const savePersonaBtn = document.getElementById("save-persona");
const personaStatus = document.getElementById("persona-status");
savePersonaBtn?.addEventListener("click", async () => {
  // Parse the comma-separated persona tags (lowercased, deduped). These
  // drive the `user_tag` dialogue-pair selector, so the user can tag who
  // they're playing and have matching context pairs surface for the cast.
  const tagsList = [...new Set(
    (personaTags?.value || "")
      .split(",")
      .map((t) => t.trim().toLowerCase())
      .filter(Boolean),
  )];
  try {
    const res = await jfetch(`/api/conversations/${conversationId}/user/persona-fields`, {
      method: "POST",
      body: JSON.stringify({
        name: personaName.value.trim(),
        description: personaDesc.value,
        tags: tagsList,
      }),
    });
    // Update local state — both the settings mirror (for macro
    // expansion + legacy reads) and the effective-persona state
    // (for next side-panel reload).
    if (state.conversation?.settings?.user_persona) {
      state.conversation.settings.user_persona.name = personaName.value.trim();
      state.conversation.settings.user_persona.description = personaDesc.value;
      state.conversation.settings.user_persona.tags = tagsList;
    }
    if (state.effectiveUserPersona) {
      state.effectiveUserPersona.name = personaName.value.trim();
      state.effectiveUserPersona.description = personaDesc.value;
      state.effectiveUserPersona.tags = tagsList;
    }
    // Append the narrator marker the endpoint returned so the change
    // shows up in the timeline like a role change does.
    if (res?.message) appendMessage(res.message);
    personaStatus.textContent = "Saved.";
    personaStatus.dataset.kind = "ok";
  } catch (e) {
    personaStatus.textContent = "Failed: " + e.message;
    personaStatus.dataset.kind = "error";
  }
});

// ---------------------------------------------------------------------------
// Role section (life sim / role-mode scenarios)
//
// Reveals iff the scenario has user_personas_are_roles = true AND a
// non-empty user_personas list. Preset dropdown sources from the
// scenario entity; manual label + description boxes track the preset
// pick or accept free-text. Save → POST /user/role emits patch edits
// on the user entity; clear → POST with {clear:true} emits unsets.
// Both routes go through the standard narrator_apply pipeline so the
// change rides path-replay and shows up in the Applied edits panel.
// ---------------------------------------------------------------------------
const roleSection = document.getElementById("role-section");
const rolePresetSel = document.getElementById("role-preset");
const roleLabelInput = document.getElementById("role-label");
const roleDescArea = document.getElementById("role-desc");
const saveRoleBtn = document.getElementById("save-role");
const clearRoleBtn = document.getElementById("clear-role");
const roleStatus = document.getElementById("role-status");

function initRoleSection() {
  if (!roleSection) return;
  const sid = state.conversation?.scenario_id || "";
  const scenario = state.entities?.[sid] || {};
  const presets = Array.isArray(scenario.user_personas) ? scenario.user_personas : [];
  const inRoleMode = !!scenario.user_personas_are_roles && presets.length > 0;
  if (!inRoleMode) {
    roleSection.hidden = true;
    return;
  }
  roleSection.hidden = false;

  // Populate preset dropdown (preserve placeholder + Custom option).
  if (rolePresetSel) {
    // Drop any previously-added preset options, leave the placeholder.
    while (rolePresetSel.options.length > 1) rolePresetSel.remove(1);
    for (const p of presets) {
      const opt = document.createElement("option");
      opt.value = p.id;
      opt.textContent = p.label || p.name || p.id;
      rolePresetSel.appendChild(opt);
    }
    const custom = document.createElement("option");
    custom.value = "__custom__";
    custom.textContent = "Custom…";
    rolePresetSel.appendChild(custom);
  }

  // Pre-fill from the path-replayed user persona (the effective
  // user state at the active leaf — staging edits land here). Falls
  // back to the on-disk entity baseline if effectiveUserPersona is
  // missing for any reason, then to empty. This covers the case
  // where the staging panel set a role via `[set user.role = ...]`
  // and we need to surface it in the side panel on load.
  const effPersona = state.effectiveUserPersona || {};
  const userEnt = state.entities?.user || {};
  if (roleLabelInput) roleLabelInput.value = effPersona.role || userEnt.role || "";
  if (roleDescArea) roleDescArea.value = effPersona.role_description || userEnt.role_description || "";
  // Match the preset dropdown to the active role (label or name) if
  // one of the presets matches — surfaces the staging pick in the
  // dropdown visually, not just in the text inputs.
  if (rolePresetSel) {
    const currentLabel = (roleLabelInput?.value || "").trim();
    const matchingPreset = currentLabel
      ? presets.find((p) => (p.name || p.label || p.id).toLowerCase() === currentLabel.toLowerCase())
      : null;
    if (matchingPreset) rolePresetSel.value = matchingPreset.id;
  }

  rolePresetSel?.addEventListener("change", () => {
    const val = rolePresetSel.value;
    if (!val || val === "__custom__") {
      if (val !== "__custom__") {
        if (roleLabelInput) roleLabelInput.value = "";
        if (roleDescArea) roleDescArea.value = "";
      }
      return;
    }
    const preset = presets.find((p) => p.id === val);
    if (!preset) return;
    if (roleLabelInput) roleLabelInput.value = preset.name || "";
    if (roleDescArea) roleDescArea.value = preset.description || "";
  });

  saveRoleBtn?.addEventListener("click", async () => {
    const label = (roleLabelInput?.value || "").trim();
    if (!label) {
      roleStatus.textContent = "Pick a preset or type a role label.";
      roleStatus.dataset.kind = "error";
      return;
    }
    try {
      const res = await jfetch(`/api/conversations/${conversationId}/user/role`, {
        method: "POST",
        body: JSON.stringify({
          role: label,
          role_description: (roleDescArea?.value || "").trim(),
        }),
      });
      if (res?.message) appendMessage(res.message);
      // Mirror onto local entity cache so other UI surfaces (prompt
      // tab, etc.) pick up the change without a reload.
      const u = state.entities?.user;
      if (u) {
        u.role = label;
        u.role_description = (roleDescArea?.value || "").trim();
      }
      roleStatus.textContent = "Saved.";
      roleStatus.dataset.kind = "ok";
    } catch (e) {
      roleStatus.textContent = "Failed: " + e.message;
      roleStatus.dataset.kind = "error";
    }
  });

  clearRoleBtn?.addEventListener("click", async () => {
    try {
      const res = await jfetch(`/api/conversations/${conversationId}/user/role`, {
        method: "POST",
        body: JSON.stringify({ clear: true }),
      });
      if (res?.message) appendMessage(res.message);
      const u = state.entities?.user;
      if (u) { delete u.role; delete u.role_description; }
      if (roleLabelInput) roleLabelInput.value = "";
      if (roleDescArea) roleDescArea.value = "";
      if (rolePresetSel) rolePresetSel.value = "";
      roleStatus.textContent = "Cleared.";
      roleStatus.dataset.kind = "ok";
    } catch (e) {
      roleStatus.textContent = "Failed: " + e.message;
      roleStatus.dataset.kind = "error";
    }
  });
}
initRoleSection();

// Sampling persistence
const samplingFields = {
  temperature: document.getElementById("samp-temp"),
  top_p: document.getElementById("samp-top-p"),
  top_k: document.getElementById("samp-top-k"),
  repeat_penalty: document.getElementById("samp-repeat"),
  num_predict: document.getElementById("samp-num-predict"),
};
const saveSamplingBtn = document.getElementById("save-sampling");
const samplingStatus = document.getElementById("sampling-status");
saveSamplingBtn?.addEventListener("click", async () => {
  const sampling = {};
  for (const [k, el] of Object.entries(samplingFields)) {
    const v = el.value.trim();
    if (v === "") continue;
    const n = Number(v);
    if (!Number.isNaN(n)) sampling[k] = n;
  }
  try {
    await jfetch(`/api/conversations/${conversationId}/settings`, {
      method: "PUT",
      body: JSON.stringify({ sampling }),
    });
    samplingStatus.textContent = "Saved. Empty fields fall back to model defaults.";
    samplingStatus.dataset.kind = "ok";
  } catch (e) {
    samplingStatus.textContent = "Failed: " + e.message;
    samplingStatus.dataset.kind = "error";
  }
});

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Display: per-conversation text colors via CSS variables
// ---------------------------------------------------------------------------

const DEFAULT_COLORS = { body: "#9b9b96", dialog: "#ffffff", action: "#e18a24" };
const colorBody = document.getElementById("color-body");
const colorDialog = document.getElementById("color-dialog");
const colorAction = document.getElementById("color-action");
const resetColorsBtn = document.getElementById("reset-colors");

function applyColors(colors) {
  shell.style.setProperty("--chat-body-color", colors.body || DEFAULT_COLORS.body);
  shell.style.setProperty("--chat-dialog-color", colors.dialog || DEFAULT_COLORS.dialog);
  shell.style.setProperty("--chat-action-color", colors.action || DEFAULT_COLORS.action);
}

let colorSaveTimer = null;
function persistColors(colors) {
  clearTimeout(colorSaveTimer);
  colorSaveTimer = setTimeout(() => {
    jfetch(`/api/conversations/${conversationId}/settings`, {
      method: "PUT",
      body: JSON.stringify({ colors }),
    }).catch((e) => flashError("Color save failed: " + e.message));
  }, 300);
}

function readColorsFromInputs() {
  return {
    body: colorBody.value,
    dialog: colorDialog.value,
    action: colorAction.value,
  };
}

[colorBody, colorDialog, colorAction].forEach((el) =>
  el?.addEventListener("input", () => {
    const c = readColorsFromInputs();
    applyColors(c);
    persistColors(c);
  })
);

resetColorsBtn?.addEventListener("click", () => {
  colorBody.value = DEFAULT_COLORS.body;
  colorDialog.value = DEFAULT_COLORS.dialog;
  colorAction.value = DEFAULT_COLORS.action;
  applyColors(DEFAULT_COLORS);
  persistColors(DEFAULT_COLORS);
});

// Apply whatever the saved settings had (or fall through to defaults).
applyColors({ ...DEFAULT_COLORS, ...(state.conversation.settings.colors || {}) });

initResponder();
updateAsLabel();
renderQuickEdits();
updateTokenEstimate();
// Default the right rail open on desktop only — on mobile it would
// cover the chat and force the user to dismiss it on every load.
if (!mobileMQ.matches) shell.classList.add("right-open");
fullRender();

// Module JS files are loaded as <script> tags AFTER chat.js (see
// chat.html), so any Modules.onMessage hooks they register — e.g. the
// texting bubble styler — run too late to decorate the messages that
// the fullRender() above already painted. That's why module message
// decorations (texting bubbles, etc.) vanished on a refresh: the only
// render that mattered happened before the hooks existed. Defer one
// macrotask (setTimeout 0) so every module <script> has executed and
// registered, then re-fire the per-message hooks against the
// already-rendered nodes. First-time decoration for these nodes (the
// initial render fired with zero hooks registered), so this doesn't
// double-decorate.
setTimeout(() => {
  if (!(window.Modules && Modules._fireMessage)) return;
  for (const el of messagesEl.querySelectorAll(".msg[data-message-id]")) {
    const msg = state.conversation?.messages?.[el.dataset.messageId];
    if (msg) Modules._fireMessage(el, msg);
  }
}, 0);

// "NPC starts" handoff: when the staging panel's "NPC starts ▸"
// button kicked the page reload, sessionStorage carries the partner
// id forward. After the chat finishes its first paint, fire a
// streamGenerate for the partner so they take the first in-character
// turn after the narrator opening.
(function maybeStartNPCFirstTurn() {
  const key = `pending_npc_first_turn:${conversationId}`;
  const partnerId = sessionStorage.getItem(key);
  if (!partnerId) return;
  sessionStorage.removeItem(key);
  // Only fire if we're actually freshly post-Start: the active leaf
  // should be a narrator opening or a first_message under one. If the
  // user has already played turns the flag is stale; bail.
  const leaf = state.conversation?.messages?.[state.conversation.active_path_leaf];
  if (!leaf) return;
  const userTurns = Object.values(state.conversation.messages || {})
    .filter((m) => m.persona === "user").length;
  if (userTurns > 0) return;
  // Defer one tick so any in-flight rendering settles before we
  // start a stream.
  setTimeout(() => {
    streamGenerate({ persona: partnerId, parent_id: leaf.id });
  }, 0);
})();
