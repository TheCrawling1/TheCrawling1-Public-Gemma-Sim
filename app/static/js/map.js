/* Map panel — navigate locations & rooms, see who's where, walk there.
 *
 * Sibling to library.js. Shares the right-rail slot with #right-panel and
 * is mutually exclusive with it (the shell's `map-mode` class swaps which
 * aside is the grid's right column). All data comes from
 * GET /api/conversations/<cid>/map; moves reuse the existing /move and
 * /follow endpoints, then hand the new leaf to chat.js via window.GemmaSimNav
 * so the chat re-renders exactly like an in-chat move.
 */
(function () {
  "use strict";

  const shell = document.querySelector(".chat-shell");
  const showMap = document.getElementById("show-map");
  const showRight = document.getElementById("show-right");
  const closeMapBtn = document.getElementById("close-map");
  const bodyEl = document.getElementById("map-body");
  const searchEl = document.getElementById("map-search");
  const scopeEl = document.getElementById("map-scope");
  if (!shell || !showMap || !bodyEl) return;

  const nav = () => window.GemmaSimNav || {};
  const cid = () => nav().conversationId;

  let scope = "scenario";
  let data = null;          // last /map payload
  let filter = "";
  const openLocs = new Set(); // location ids expanded in the panel
  let seeded = false;         // seed the current location open exactly once

  // ---- fetch helpers ------------------------------------------------------
  async function jget(url) {
    const r = await fetch(url);
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    return r.json();
  }
  async function jsend(url, body) {
    const r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    if (!r.ok) {
      const e = await r.json().catch(() => ({}));
      throw new Error(e.error || `${r.status} ${r.statusText}`);
    }
    return r.json();
  }
  const esc = (s) =>
    (s == null ? "" : String(s)).replace(/[&<>"]/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  // ---- open / close (mutually exclusive with the Library rail) ------------
  function openMap() {
    shell.classList.add("map-mode", "right-open");
    load();
  }
  function closeMap() {
    shell.classList.remove("map-mode", "right-open");
  }
  function isMapOpen() {
    return shell.classList.contains("map-mode") && shell.classList.contains("right-open");
  }

  showMap.addEventListener("click", () => {
    if (isMapOpen()) closeMap();
    else openMap();
  });
  closeMapBtn?.addEventListener("click", closeMap);

  // Clicking Library while the map is up switches to the library rather
  // than toggling it closed. Capture phase + stopImmediatePropagation so
  // chat.js's own show-right handler doesn't also fire.
  showRight?.addEventListener(
    "click",
    (e) => {
      if (shell.classList.contains("map-mode")) {
        e.stopImmediatePropagation();
        shell.classList.remove("map-mode");
        shell.classList.add("right-open");
      }
    },
    true
  );

  // ---- scope + search -----------------------------------------------------
  scopeEl?.addEventListener("click", (e) => {
    const btn = e.target.closest(".map-scope-btn");
    if (!btn) return;
    const next = btn.dataset.scope;
    if (next === scope) return;
    scope = next;
    scopeEl.querySelectorAll(".map-scope-btn").forEach((b) =>
      b.classList.toggle("active", b.dataset.scope === scope));
    load();
  });

  searchEl?.addEventListener("input", (e) => {
    filter = (e.target.value || "").trim().toLowerCase();
    render();
  });

  // ---- data ---------------------------------------------------------------
  async function load() {
    const id = cid();
    if (!id) return;
    bodyEl.innerHTML = `<p class="map-empty">Loading…</p>`;
    try {
      data = await jget(`/api/conversations/${id}/map?scope=${encodeURIComponent(scope)}`);
    } catch (err) {
      bodyEl.innerHTML = `<p class="map-empty map-error">Couldn't load map: ${esc(err.message)}</p>`;
      return;
    }
    // Seed the collapse state once: open the location the user is standing
    // in (or the first one) so the panel doesn't open fully collapsed.
    if (!seeded) {
      if (data.current_location) openLocs.add(data.current_location);
      else if ((data.locations || [])[0]) openLocs.add(data.locations[0].id);
      seeded = true;
    } else if (data.current_location) {
      openLocs.add(data.current_location); // keep the current location visible
    }
    render();
  }

  function roomMatches(room, locName) {
    if (!filter) return true;
    const hay = [room.name, locName, ...(room.tags || [])].join(" ").toLowerCase();
    return hay.includes(filter);
  }

  function distanceLabel(room) {
    if (room.is_current) return `<span class="map-here">you are here</span>`;
    if (!room.reachable) return `<span class="map-dist map-unreach" title="No walking route from your current room">no route</span>`;
    const d = room.distance;
    if (d == null) return "";
    return `<span class="map-dist" title="${d} room${d === 1 ? "" : "s"} away">${d} away</span>`;
  }

  function presentChip(p) {
    const foll = p.following
      ? ` <span class="map-foll" title="Following ${esc(p.following)}">↳</span>`
      : "";
    // Only real NPCs (not You) get a follow toggle.
    const followBtn =
      p.id === "user"
        ? ""
        : `<button class="map-followbtn ${p.following === "user" ? "on" : ""}" data-follow="${esc(p.id)}" data-following="${esc(p.following || "")}" title="${p.following === "user" ? "Stop following you" : "Have them follow you"}">${p.following === "user" ? "following you" : "follow"}</button>`;
    return `<span class="map-person${p.id === "user" ? " is-you" : ""}">${esc(p.name)}${foll}${followBtn}</span>`;
  }

  // Barred exits out of a room: a fallen portcullis / locked door you must Force
  // before the route opens. Only actionable from the room you're standing in.
  function barredRows(room) {
    const locked = room.locked || {};
    const keys = Object.keys(locked);
    if (!keys.length) return "";
    return keys.map((dest) => {
      const info = locked[dest] || {};
      const skill = info.skill === "strength" || info.skill === "str"
        ? "Strength" : (info.skill || "check").replace(/_/g, " ");
      const dc = info.dc != null ? ` DC ${esc(String(info.dc))}` : "";
      const btn = room.is_current
        ? `<button class="map-force" data-force="${esc(dest)}">Force (${esc(skill)}${dc})</button>`
        : "";
      return `<div class="map-barred" title="${esc(info.reason || "barred")}">
          <span class="map-barred-label">⛔ ${esc(info.reason || "A barred door")}</span>${btn}
        </div>`;
    }).join("");
  }

  function roomRow(room, locName) {
    const tags = (room.tags || [])
      .map((t) => `<span class="map-tag">${esc(t)}</span>`)
      .join("");
    const present = (room.present || []).map(presentChip).join("");
    const goBtn = room.is_current
      ? ""
      : `<button class="map-go" data-go="${esc(room.id)}">Go</button>`;
    return `
      <div class="map-room${room.is_current ? " current" : ""}${room.reachable ? "" : " unreachable"}" data-room="${esc(room.id)}">
        <div class="map-room-top">
          <span class="map-room-name">${esc(room.name)}</span>
          ${distanceLabel(room)}
          ${goBtn}
        </div>
        ${tags ? `<div class="map-tags">${tags}</div>` : ""}
        ${present ? `<div class="map-present">${present}</div>` : ""}
        ${barredRows(room)}
      </div>`;
  }

  function render() {
    if (!data) return;
    const locs = data.locations || [];
    const searching = !!filter; // a search force-opens every matching location
    const parts = [];
    for (const loc of locs) {
      const rooms = (loc.rooms || []).filter((r) => roomMatches(r, loc.name));
      if (!rooms.length) continue;
      const open = searching || openLocs.has(loc.id);
      const hasHere = rooms.some((r) => r.is_current);
      parts.push(`
        <section class="map-loc ${open ? "open" : "collapsed"}" data-loc="${esc(loc.id)}">
          <header class="map-loc-head" data-loctoggle="${esc(loc.id)}" role="button" tabindex="0" aria-expanded="${open}">
            <span class="map-loc-caret">${open ? "▾" : "▸"}</span>
            <span class="map-loc-name">${esc(loc.name)}</span>
            ${hasHere ? `<span class="map-loc-here" title="You are in this location">•</span>` : ""}
            <span class="map-loc-count">${rooms.length}</span>
          </header>
          ${open ? rooms.map((r) => roomRow(r, loc.name)).join("") : ""}
        </section>`);
    }
    bodyEl.innerHTML =
      parts.join("") ||
      `<p class="map-empty">${filter ? "No rooms match your search." : "No locations to show for this scenario. Try the “All” scope."}</p>`;
  }

  // ---- actions ------------------------------------------------------------
  bodyEl.addEventListener("click", async (e) => {
    // Location header → collapse / expand its rooms.
    const locToggle = e.target.closest("[data-loctoggle]");
    if (locToggle && !e.target.closest("[data-go],[data-follow]")) {
      const lid = locToggle.dataset.loctoggle;
      if (openLocs.has(lid)) openLocs.delete(lid);
      else openLocs.add(lid);
      render();
      return;
    }

    const goBtn = e.target.closest("[data-go]");
    const followBtn = e.target.closest("[data-follow]");
    const forceBtn = e.target.closest("[data-force]");
    const id = cid();
    if (!id) return;

    if (forceBtn) {
      const dest = forceBtn.dataset.force;
      forceBtn.disabled = true;
      const label = forceBtn.textContent;
      forceBtn.textContent = "…";
      try {
        const res = await jsend(`/api/conversations/${id}/pf1e/exit/force`, { dest });
        if (res && res.ok) {
          const line = res.opened
            ? `You force the way — ${res.skill} ${res.total} vs DC ${res.dc}. The door grinds open.`
            : `You strain against it — ${res.skill} ${res.total} vs DC ${res.dc}. It holds.`;
          if (nav().setStatus) nav().setStatus(line);
          if (!res.opened) { forceBtn.disabled = false; forceBtn.textContent = label; }
        } else {
          forceBtn.disabled = false;
          forceBtn.textContent = label;
        }
        await load(); // refresh: an opened door makes the far room reachable
      } catch (err) {
        forceBtn.disabled = false;
        forceBtn.textContent = label;
        alert("Force failed: " + err.message);
      }
      return;
    }

    if (goBtn) {
      const roomId = goBtn.dataset.go;
      goBtn.disabled = true;
      goBtn.textContent = "…";
      try {
        const res = await jsend(`/api/conversations/${id}/move`, {
          character_id: "user",
          room_id: roomId,
        });
        if (res.message) {
          nav().applyServerMessage?.(res.message);
          nav().reloadEntities?.();
        }
        await load(); // refresh present cast + distances from the new room
      } catch (err) {
        goBtn.disabled = false;
        goBtn.textContent = "Go";
        alert("Move failed: " + err.message);
      }
      return;
    }

    if (followBtn) {
      const charId = followBtn.dataset.follow;
      const already = followBtn.dataset.following === "user";
      followBtn.disabled = true;
      try {
        const res = await jsend(`/api/conversations/${id}/follow`, {
          character_id: charId,
          follow: already ? null : "user",
        });
        if (res.message) nav().applyServerMessage?.(res.message);
        await load();
      } catch (err) {
        followBtn.disabled = false;
        alert("Follow failed: " + err.message);
      }
    }
  });

  // Keyboard: Enter / Space toggles a focused location header.
  bodyEl.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" && e.key !== " ") return;
    const loc = e.target.closest("[data-loctoggle]");
    if (!loc) return;
    e.preventDefault();
    const lid = loc.dataset.loctoggle;
    if (openLocs.has(lid)) openLocs.delete(lid);
    else openLocs.add(lid);
    render();
  });

  // Refresh the map after a move/branch made elsewhere in the chat, so
  // "you are here" and present-cast stay in sync while the panel is open.
  window.addEventListener("gemmasim:cast-changed", () => {
    if (isMapOpen()) load();
  });
})();
