/**
 * Locked image module — frontend.
 *
 * Reactive to live activation toggles: when the user enables /
 * disables the module via the left-panel modules section, the frame
 * mounts or unmounts in place without a page reload. Plumbing comes
 * from window.Modules.onActivationChange, which fires after
 * patchActiveSetupModules returns successfully.
 *
 * Image source: msg.metadata.image_pack_pick.image_url (the same
 * field the inline image-pack widget reads). The frame shows the
 * top-most visible message that has an image; scrolling up through
 * history swaps the frame to that historical message's image. The
 * inline per-message image_pack blocks are hidden via a body class
 * so the locked frame is the sole image surface.
 */
(function () {
  const MODULE_ID = "locked_image";

  if (!window.Modules) {
    console.warn("locked_image: window.Modules unavailable; dormant");
    return;
  }

  const settings = Modules.settings(MODULE_ID) || {};
  const heightPx = settings.height_px;
  const heightCss = (typeof heightPx === "number" && heightPx > 0)
    ? `${Math.max(120, Math.min(2000, heightPx))}px`
    : "75vh";

  const messagesEl = document.getElementById("messages");

  // Cached DOM elements — created lazily on first mount, kept around
  // so re-enabling the module reuses the same frame and observer.
  let frame = null;
  let img = null;
  let observer = null;
  let currentMid = null;
  const visibleMessages = new Set();
  let messageHookRegistered = false;

  function _buildFrame() {
    frame = document.createElement("div");
    frame.className = "locked-image-frame";
    frame.style.setProperty("--locked-image-height", heightCss);

    img = document.createElement("img");
    img.alt = "";
    img.loading = "eager";
    img.decoding = "async";
    img.className = "locked-image-img";
    frame.appendChild(img);
  }

  function imagePackFor(messageEl) {
    const mid = messageEl.dataset.messageId;
    if (!mid) return null;
    const msg = state?.conversation?.messages?.[mid];
    if (!msg) return null;
    const pick = msg.metadata && msg.metadata.image_pack_pick;
    if (pick && pick.image_url) return pick;
    return null;
  }

  function showImageFor(messageEl) {
    const pick = imagePackFor(messageEl);
    if (!pick) return false;
    const mid = messageEl.dataset.messageId;
    if (mid === currentMid) return true;
    currentMid = mid;
    img.style.opacity = "0";
    const onLoad = () => { img.style.opacity = ""; };
    const onErr  = () => { /* keep previous src */ };
    img.addEventListener("load",  onLoad, { once: true });
    img.addEventListener("error", onErr,  { once: true });
    img.src = pick.image_url;
    return true;
  }

  function pickTopMostWithImage() {
    if (!visibleMessages.size) return;
    const sorted = Array.from(visibleMessages).sort((a, b) => {
      const ar = a.getBoundingClientRect();
      const br = b.getBoundingClientRect();
      return ar.top - br.top;
    });
    for (const el of sorted) {
      if (showImageFor(el)) return;
    }
  }

  function _buildObserver() {
    const liveFrameHeight = () => frame.clientHeight || 200;
    observer = new IntersectionObserver((entries) => {
      for (const e of entries) {
        if (e.isIntersecting) visibleMessages.add(e.target);
        else                   visibleMessages.delete(e.target);
      }
      pickTopMostWithImage();
    }, {
      root: messagesEl,
      rootMargin: `-${liveFrameHeight()}px 0px 0px 0px`,
      threshold: [0, 0.1, 0.5, 1],
    });
  }

  function _observeExisting() {
    if (!messagesEl || !observer) return;
    const existing = messagesEl.querySelectorAll(".msg");
    for (const el of existing) observer.observe(el);
    // Initial seed: walk leaf→root for the latest message with an
    // image and show it right away.
    const msgs = state?.conversation?.messages || {};
    const leaf = state?.conversation?.active_path_leaf;
    let cur = leaf && msgs[leaf];
    const seen = new Set();
    while (cur && !seen.has(cur.id)) {
      seen.add(cur.id);
      const pick = cur.metadata?.image_pack_pick;
      if (pick && pick.image_url) {
        const el = messagesEl.querySelector(
          `.msg[data-message-id="${cur.id}"]`,
        );
        if (el) { showImageFor(el); break; }
      }
      cur = cur.parent_id ? msgs[cur.parent_id] : null;
    }
  }

  function _mount() {
    if (frame && frame.isConnected) return;  // already mounted
    if (!frame) {
      _buildFrame();
      _buildObserver();
    }
    if (messagesEl) messagesEl.classList.add("locked-image-active");
    Modules.mount("chat_above", frame);
    // Bind to any messages already in the DOM (the message hook
    // catches future renders; this catches the existing path).
    setTimeout(_observeExisting, 0);
    if (!messageHookRegistered) {
      Modules.onMessage((messageEl) => {
        // Only observe when frame is mounted — otherwise we'd be
        // tracking elements with no UI to show them on.
        if (frame && frame.isConnected && observer) {
          observer.observe(messageEl);
          if (!currentMid && imagePackFor(messageEl)) {
            showImageFor(messageEl);
          }
        }
      });
      messageHookRegistered = true;
    }
  }

  function _unmount() {
    if (frame && frame.parentNode) frame.parentNode.removeChild(frame);
    if (messagesEl) messagesEl.classList.remove("locked-image-active");
    if (observer) observer.disconnect();
    visibleMessages.clear();
    currentMid = null;
    // Keep `frame`, `img`, `observer` references so re-activation
    // skips the rebuild — the observer's `disconnect()` lets it be
    // re-used after the next observe() calls.
  }

  function _sync() {
    if (Modules.isActive(MODULE_ID)) _mount();
    else                              _unmount();
  }

  // React to live activation toggles from the left-panel.
  Modules.onActivationChange(_sync);
  // Initial state on page load.
  _sync();
})();
