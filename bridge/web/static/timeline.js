/**
 * RadioDan — 3D Ortho Timeline Visualization
 *
 * Three.js orthographic renderer with SSE live data, inspect mode,
 * minimap, activity table, and adaptive camera controls.
 *
 * Ported from prototype (doc/radio_timeline_viz prototype.html),
 * connected to real SSE data from /api/timeline/events.
 */
window.initTimeline = function() {
  "use strict";

  // Dispose any previous instance (safe to call multiple times)
  if (window.__timelineCleanup) {
    window.__timelineCleanup();
    window.__timelineCleanup = null;
  }

  const $ = (s, el = document) => el.querySelector(s);

  if (!window.THREE) {
    console.error("Three.js failed to load");
    return;
  }

  // ── Color palette for dynamic lanes ──
  const LANE_COLORS = {
    music:     0x2a2094,
    presenter: 0x56d4f5,
    dong:      0x4aeabc,
    api:       0xf0a840,
    system:    0xf0a840,
    _palette: [0xf472b6, 0x38bdf8, 0xff6b6b, 0x56d4f5, 0x4aeabc],
  };

  // ── Helpers ──
  const pad2 = n => String(n).padStart(2, "0");
  const hhmmss = ts => {
    const d = new Date(ts * 1000);
    return `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
  };
  const hhmm = ts => {
    const d = new Date(ts * 1000);
    return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
  };
  const fmtDur = sec => {
    if (!sec || sec <= 0) return "\u2014";
    sec = Math.max(0, Math.floor(sec));
    return `${Math.floor(sec / 60)}:${pad2(sec % 60)}`;
  };
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const escHtml = s => String(s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" }[c]));

  // ============================================================
  // SSE State
  // ============================================================

  const sseState = {
    events: new Map(),          // event_id -> event object
    serverTimeOffset: 0,        // server_time - local_time
    currentTrackEndAt: 0,
    crossfadeDuration: 5.0,
  };

  function serverNow() {
    return Date.now() / 1000 + sseState.serverTimeOffset;
  }

  // ============================================================
  // Track / Lane management (dynamic)
  // ============================================================

  let tracks = [];          // [{id, name, color}]
  const trackIdx = new Map(); // track.id -> index
  let paletteIdx = 0;

  function getLaneColor(laneId) {
    if (LANE_COLORS[laneId] !== undefined && laneId !== "_palette") return LANE_COLORS[laneId];
    const pal = LANE_COLORS._palette;
    const c = pal[paletteIdx % pal.length];
    paletteIdx++;
    LANE_COLORS[laneId] = c;
    return c;
  }

  function ensureLane(laneId) {
    if (trackIdx.has(laneId)) return;
    // Map system -> api lane
    const effectiveId = laneId === "system" ? "api" : laneId;
    if (trackIdx.has(effectiveId)) return;

    const color = getLaneColor(effectiveId);
    const name = effectiveId.toUpperCase();

    if (effectiveId === "music") {
      // Music goes last (bottom of the 3D scene)
      tracks.push({ id: effectiveId, name, color });
    } else if (effectiveId === "api") {
      // API just above music
      const musicPos = tracks.findIndex(t => t.id === "music");
      if (musicPos !== -1) {
        tracks.splice(musicPos, 0, { id: effectiveId, name, color });
      } else {
        tracks.push({ id: effectiveId, name, color });
      }
    } else {
      // Other lanes go at top; insert before api if it exists
      const apiPos = tracks.findIndex(t => t.id === "api");
      if (apiPos !== -1) {
        tracks.splice(apiPos, 0, { id: effectiveId, name, color });
      } else {
        const musicPos = tracks.findIndex(t => t.id === "music");
        if (musicPos !== -1) {
          tracks.splice(musicPos, 0, { id: effectiveId, name, color });
        } else {
          tracks.push({ id: effectiveId, name, color });
        }
      }
    }

    // Rebuild index
    trackIdx.clear();
    tracks.forEach((t, i) => trackIdx.set(t.id, i));
  }

  function effectiveLane(ev) {
    return ev.lane === "system" ? "api" : ev.lane;
  }

  // ============================================================
  // SSE Connection
  // ============================================================

  let eventSource = null;
  let reconnectDelay = 1000;
  const sseDot = $("#sseDot");
  const sseStatusEl = $("#sseStatus");

  function setSseStatus(connected) {
    if (connected) {
      sseDot.classList.remove("disconnected");
      sseStatusEl.textContent = "Live";
    } else {
      sseDot.classList.add("disconnected");
      sseStatusEl.textContent = "Reconnecting";
    }
  }

  function connectSSE() {
    eventSource = new EventSource("/api/timeline/events");

    eventSource.addEventListener("snapshot", e => {
      const events = JSON.parse(e.data);
      sseState.events.clear();
      for (const ev of events) {
        const lane = effectiveLane(ev);
        ensureLane(lane);
        ev.lane = lane;
        sseState.events.set(ev.id, ev);
      }
      needsFullRebuild = true;
      setSseStatus(true);
      reconnectDelay = 1000;
    });

    eventSource.addEventListener("playback_state", e => {
      const data = JSON.parse(e.data);
      sseState.serverTimeOffset = data.server_time - Date.now() / 1000;
      sseState.currentTrackEndAt = data.server_time + data.remaining;
      if (data.crossfade_duration !== undefined) {
        sseState.crossfadeDuration = data.crossfade_duration;
      }
    });

    eventSource.addEventListener("event_update", e => {
      const msg = JSON.parse(e.data);
      if (!msg.event) return;
      const ev = msg.event;

      if (msg.action === "start") {
        const lane = effectiveLane(ev);
        ensureLane(lane);
        ev.lane = lane;
        sseState.events.set(ev.id, ev);
        addOrUpdateMesh(ev, true);
        renderActivity();
      } else {
        // end/update — merge into existing
        const existing = sseState.events.get(ev.id);
        if (existing) {
          Object.assign(existing, ev);
          addOrUpdateMesh(existing);
          renderActivity();
        }
      }
    });

    eventSource.onerror = () => {
      eventSource.close();
      setSseStatus(false);
      setTimeout(connectSSE, reconnectDelay);
      reconnectDelay = Math.min(reconnectDelay * 2, 30000);
    };
  }

  // ============================================================
  // DOM refs
  // ============================================================

  const clockEl = $("#clock"), nowBadge = $("#nowBadge");
  const trackListEl = $("#trackList"), actBody = $("#actBody"), actSub = $("#actSub");
  const trackLegendEls = new Map();

  // ============================================================
  // Selection state
  // ============================================================

  let selectedId = null;

  function setSelected(id, { source = "ui", centerNow = true } = {}) {
    selectedId = id;
    actBody.querySelectorAll("tr").forEach(tr =>
      tr.classList.toggle("isSelected", tr.dataset.eid === id));
    trackLegendEls.forEach(el => el.classList.remove("isSelected"));
    if (id) {
      const ev = sseState.events.get(id);
      if (ev) {
        const el = trackLegendEls.get(ev.lane);
        if (el) el.classList.add("isSelected");
      }
    }
    applySelGlow();
    if (id && source === "timeline") {
      const row = actBody.querySelector(`tr[data-eid="${id}"]`);
      if (row) row.scrollIntoView({ block: "center", behavior: s.reducedMotion ? "auto" : "smooth" });
    }
  }

  // ============================================================
  // Activity table
  // ============================================================

  function renderActivity() {
    const sorted = Array.from(sseState.events.values())
      .sort((a, b) => b.started_at - a.started_at)
      .slice(0, 50);

    actBody.innerHTML = "";
    sorted.forEach(ev => {
      const tr = document.createElement("tr");
      tr.dataset.eid = ev.id;
      const dur = ev.ended_at ? ev.ended_at - ev.started_at :
        (ev.status === "active" ? serverNow() - ev.started_at : 0);
      const sc = ev.status === "active" ? "" :
        ev.status === "failed" ? "failed" :
          ev.status === "scheduled" ? "pending" : "";
      tr.innerHTML = `
        <td class="mono" style="font-size:11px;color:rgba(235,240,255,.7)">${hhmmss(ev.started_at)}</td>
        <td class="mono" style="font-size:11px;color:rgba(235,240,255,.6)">${ev.ended_at ? hhmmss(ev.ended_at) : "\u2026"}</td>
        <td class="mono" style="font-size:11px">${escHtml(ev.lane)}</td>
        <td style="font-size:12px">${escHtml(ev.title)}</td>
        <td><span class="status ${sc}"><span class="s"></span>${escHtml(ev.status)}</span></td>
        <td class="mono" style="font-size:11px">${fmtDur(dur)}</td>`;
      tr.addEventListener("click", () => enterFocus(ev.id));
      actBody.appendChild(tr);
    });
    actSub.textContent = `linked 1:1 \u00b7 ${sorted.length}`;
    if (selectedId) setSelected(selectedId, { source: "ui", centerNow: false });
  }

  // ============================================================
  // Track legend
  // ============================================================

  function buildLegend() {
    trackListEl.innerHTML = "";
    trackLegendEls.clear();
    tracks.forEach(tr => {
      const el = document.createElement("div");
      el.className = "track";
      el.dataset.trackId = tr.id;
      const hex = "#" + tr.color.toString(16).padStart(6, "0");
      el.innerHTML = `
        <div style="display:flex;align-items:center;gap:8px;min-width:0">
          <span style="width:8px;height:8px;border-radius:99px;background:${hex};box-shadow:0 0 10px ${hex}80;flex:0 0 auto"></span>
          <div class="name">${escHtml(tr.name)}</div>
        </div>
        <div class="badge mono">${escHtml(tr.id)}</div>`;
      trackLegendEls.set(tr.id, el);
      trackListEl.appendChild(el);
    });
  }

  // ============================================================
  // Three.js scene setup
  // ============================================================

  const viewport = $("#viewport"), glCanvas = $("#gl");
  const rulerCanvas = $("#ruler"), rulerCtx = rulerCanvas.getContext("2d");

  const renderer = new THREE.WebGLRenderer({ canvas: glCanvas, antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 2));

  // Minimap
  const miniGLCanvas = $("#miniGL");
  const miniOverlay = $("#miniOverlay");
  const miniCtx = miniOverlay.getContext("2d");
  const miniRenderer = new THREE.WebGLRenderer({ canvas: miniGLCanvas, antialias: true, alpha: true });
  miniRenderer.setPixelRatio(Math.min(devicePixelRatio || 1, 2));
  miniRenderer.setClearColor(0x080a12, 0.9);
  let miniCamera = null;
  const MINI_ZOOM_FACTOR = 0.12;

  const scene = new THREE.Scene();
  scene.fog = new THREE.Fog(0x080a12, 900, 3000);

  scene.add(new THREE.AmbientLight(0xffffff, 0.80));
  const keyLight = new THREE.DirectionalLight(0xc4d4ff, 0.70);
  keyLight.position.set(400, 500, 900);
  scene.add(keyLight);
  const rimLight = new THREE.DirectionalLight(0x9b8aff, 0.45);
  rimLight.position.set(-600, 120, 650);
  scene.add(rimLight);
  const fillLight = new THREE.DirectionalLight(0x56d4f5, 0.20);
  fillLight.position.set(0, -200, 400);
  scene.add(fillLight);

  const root = new THREE.Group();
  scene.add(root);
  const lanesGroup = new THREE.Group();
  const clipsGroup = new THREE.Group();
  const gridGroup = new THREE.Group();
  const nowPlaneGroup = new THREE.Group();
  root.add(gridGroup, lanesGroup, clipsGroup, nowPlaneGroup);

  let camera = null;

  // ============================================================
  // Focus / Inspect state
  // ============================================================

  const focus = {
    active: false,
    eventId: null,
    swoopT: 0,
    drawerT: 0,
    drawerGroup: null,
    drawerMesh: null,
    connMesh: null,
    drawerH: 0,
    savedZoom: 0.70,
    savedPanY: -70,
    savedPanX: 0,
    savedTilt: 2,
    savedYaw: -78,
    savedOrbit: 260,
  };

  // ============================================================
  // Render state + damping targets
  // ============================================================

  const s = {
    timeScale: 1, zoom: 1.1, laneH: 44, boxDepth: 20,
    tilt: 14, yaw: -6, orbit: 260,
    grid: 0.30, glow: 0.10, musicZ: 200,
    reducedMotion: false,
    panY: -70,
    panX: 0,
    nowPlaneOpacity: 1.0,
  };
  const target = { ...s };

  // Inspect mode settings (separate from main view)
  const focusSettings = {
    zoom: 4.0, tilt: 16, yaw: -5, orbit: 350,
    drawerW: 120, drawerScale: 0,
  };

  // ============================================================
  // Scene objects
  // ============================================================

  let totalH = 0, gridLines = null;
  const eventMeshes = new Map();
  const pickMeshes = [];
  const unitBox = new THREE.BoxGeometry(1, 1, 1);
  let needsFullRebuild = false;

  function makeClipMat(color) {
    const base = new THREE.Color(color);
    return new THREE.MeshStandardMaterial({
      color: base, roughness: 0.28, metalness: 0.20,
      emissive: base.clone(), emissiveIntensity: 0.32,
    });
  }

  function makeShellMat() {
    return new THREE.MeshBasicMaterial({
      color: 0xffffff,
      transparent: true, opacity: 0.05, depthWrite: false,
    });
  }

  function rebuildLanesAndGrid() {
    while (lanesGroup.children.length) lanesGroup.remove(lanesGroup.children[0]);
    while (gridGroup.children.length) gridGroup.remove(gridGroup.children[0]);
    totalH = tracks.length * s.laneH;

    const planeGeo = new THREE.PlaneGeometry(1, 1);
    tracks.forEach((tr, i) => {
      const yCenter = -(i * s.laneH) - s.laneH / 2;
      const lane = new THREE.Mesh(planeGeo, new THREE.MeshBasicMaterial({
        color: 0x1a2848, transparent: true, opacity: 0.12, depthWrite: false,
      }));
      lane.scale.set(200000, s.laneH - 4, 1);
      lane.position.set(0, yCenter, -5);
      lanesGroup.add(lane);

      const sep = new THREE.Mesh(new THREE.PlaneGeometry(200000, 1.5), new THREE.MeshBasicMaterial({
        color: tr.color, transparent: true, opacity: 0.06, depthWrite: false,
      }));
      sep.position.set(0, -(i * s.laneH), -4);
      lanesGroup.add(sep);
    });

    // Grid
    const pts = [];
    const addV = (x, y0, y1) => pts.push(x, y0, -6, x, y1, -6);
    const addH = y => pts.push(-100000, y, -6, 100000, y, -6);
    for (let i = 0; i <= tracks.length; i++) addH(-(i * s.laneH));

    const effScale = s.timeScale * s.zoom;
    const major = effScale > 350 ? 2 : effScale > 200 ? 5 : effScale > 120 ? 10 : effScale > 70 ? 30 : 60;
    const minor = major / 5;
    for (let sec = -600; sec <= 600; sec += minor) addV(sec * s.timeScale, 0, -totalH);

    const geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.Float32BufferAttribute(pts, 3));
    gridLines = new THREE.LineSegments(geo, new THREE.LineBasicMaterial({
      color: 0x3a4a78, transparent: true, opacity: s.grid,
    }));
    gridGroup.add(gridLines);
    buildNowPlane();
  }

  // ── Now Plane ──
  function buildNowPlane() {
    while (nowPlaneGroup.children.length) nowPlaneGroup.remove(nowPlaneGroup.children[0]);
    const pH = totalH + 80;
    const pD = 220;

    const slab = new THREE.Mesh(
      new THREE.BoxGeometry(2.5, pH, pD),
      new THREE.MeshBasicMaterial({ color: 0x56d4f5, transparent: true, opacity: 0.032, depthWrite: false, side: THREE.DoubleSide }),
    );
    slab.position.set(0, -pH / 2 + 40, pD / 2 - 25);
    nowPlaneGroup.add(slab);

    const edge = new THREE.Mesh(
      new THREE.BoxGeometry(1.8, pH, 1.2),
      new THREE.MeshBasicMaterial({ color: 0x56d4f5, transparent: true, opacity: 0.22, depthWrite: false }),
    );
    edge.position.set(0, -pH / 2 + 40, -3);
    nowPlaneGroup.add(edge);

    const glow = new THREE.Mesh(
      new THREE.BoxGeometry(14, pH, pD),
      new THREE.MeshBasicMaterial({ color: 0x56d4f5, transparent: true, opacity: 0.010, depthWrite: false, side: THREE.DoubleSide }),
    );
    glow.position.copy(slab.position);
    nowPlaneGroup.add(glow);
  }

  // ============================================================
  // Event meshes (boxes)
  // ============================================================

  function addOrUpdateMesh(ev, forceCreate = false) {
    const lane = ev.lane;
    const idx = trackIdx.get(lane);
    if (idx === undefined) return;
    const tr = tracks[idx];

    const now = serverNow();
    const evStart = ev.started_at;
    const evDuration = ev.details?.duration_seconds;
    const evEnd = ev.ended_at ||
      (evDuration ? evStart + evDuration : (ev.status === "active" ? now : evStart + 2));

    const startRel = evStart - now;
    const endRel = evEnd - now;
    const durSec = Math.max(0.03, endRel - startRel);
    const w = durSec * s.timeScale;

    const isMusic = lane === "music";
    const h = s.laneH * (isMusic ? 0.55 : 0.38);
    const d = s.boxDepth + (isMusic ? 10 : 0);

    const xCenter = ((startRel + endRel) / 2) * s.timeScale;
    const baseY = -(idx * s.laneH) - s.laneH / 2;

    // Music Z-stagger: read from DB-stored detail, fall back to id parity for old events
    let zPos = d / 2;
    if (isMusic) {
      const zStagger = ev.details?.z_stagger ?? 0;
      if (zStagger === 1) zPos += s.musicZ;
    }

    let pair = eventMeshes.get(ev.id);
    if (!pair || forceCreate) {
      if (pair) {
        // Remove old meshes
        clipsGroup.remove(pair.mesh);
        clipsGroup.remove(pair.shell);
        const pIdx = pickMeshes.indexOf(pair.mesh);
        if (pIdx !== -1) pickMeshes.splice(pIdx, 1);
      }
      const mesh = new THREE.Mesh(unitBox, makeClipMat(tr.color));
      mesh.userData.eventId = ev.id;
      mesh.userData.baseEmissive = mesh.material.emissiveIntensity;
      const shell = new THREE.Mesh(unitBox, makeShellMat());
      clipsGroup.add(shell);
      clipsGroup.add(mesh);
      pair = { mesh, shell };
      eventMeshes.set(ev.id, pair);
      pickMeshes.push(mesh);
    }

    pair.mesh.scale.set(w, h, d);
    pair.mesh.position.set(xCenter, baseY, zPos);
    pair.shell.scale.set(w + 5, h + 4, d + 5);
    pair.shell.position.copy(pair.mesh.position);

    // Style by event status
    const status = ev.status || "active";
    const mat = pair.mesh.material;
    if (status === "scheduled") {
      mat.transparent = true;
      mat.opacity = 0.35;
      mat.emissiveIntensity = 0.15;
      mat.userData = { ...mat.userData };
      pair.mesh.userData.baseEmissive = 0.15;
    } else if (status === "skipped" || status === "cancelled") {
      mat.transparent = true;
      mat.opacity = 0.15;
      mat.emissiveIntensity = 0.08;
      pair.mesh.userData.baseEmissive = 0.08;
    } else {
      // active / completed — full opacity
      mat.transparent = false;
      mat.opacity = 1;
      mat.emissiveIntensity = 0.32;
      pair.mesh.userData.baseEmissive = 0.32;
    }
  }

  function updateAllMeshes() {
    // Remove meshes whose events no longer exist
    for (const id of eventMeshes.keys()) {
      if (!sseState.events.has(id)) {
        const pair = eventMeshes.get(id);
        clipsGroup.remove(pair.mesh);
        clipsGroup.remove(pair.shell);
        pair.mesh.material.dispose();
        pair.shell.material.dispose();
        eventMeshes.delete(id);
        const pIdx = pickMeshes.indexOf(pair.mesh);
        if (pIdx !== -1) pickMeshes.splice(pIdx, 1);
      }
    }
    for (const ev of sseState.events.values()) addOrUpdateMesh(ev);
  }

  // ============================================================
  // Selection glow
  // ============================================================

  function applySelGlow() {
    for (const [id, pair] of eventMeshes) {
      const m = pair.mesh;
      const base = m.userData.baseEmissive ?? 0.32;
      const ev = sseState.events.get(id);
      const status = ev?.status || "active";
      const isGhost = status === "scheduled" || status === "skipped" || status === "cancelled";

      if (!selectedId) {
        m.material.emissiveIntensity = base + s.glow * 0.5;
        if (!isGhost) { m.material.opacity = 1; m.material.transparent = false; }
      } else if (id === selectedId) {
        m.material.emissiveIntensity = Math.min(2.5, base + 0.65 + s.glow);
        if (!isGhost) { m.material.opacity = 1; m.material.transparent = false; }
      } else {
        m.material.emissiveIntensity = Math.max(0.05, (base + s.glow * 0.2) * 0.25);
        if (!isGhost) { m.material.transparent = true; m.material.opacity = 0.72; }
      }
    }
  }

  // ============================================================
  // Picking (hover + click)
  // ============================================================

  const raycaster = new THREE.Raycaster();
  const mouse = new THREE.Vector2();
  const tip = $("#tip"), tipTitle = $("#tipTitle"), tipMeta = $("#tipMeta");
  const tipTime = $("#tipTime"), tipDur = $("#tipDur");

  function setHover(mesh, cx, cy) {
    if (!mesh) { tip.style.opacity = "0"; return; }
    const ev = sseState.events.get(mesh.userData.eventId);
    if (!ev) { tip.style.opacity = "0"; return; }
    const dur = ev.ended_at ? ev.ended_at - ev.started_at :
      (ev.status === "active" ? serverNow() - ev.started_at : 0);
    tipTitle.textContent = ev.title;
    tipMeta.textContent = `#${ev.id} \u00b7 ${ev.lane} \u00b7 ${ev.status}`;
    tipTime.textContent = `${hhmmss(ev.started_at)} \u2192 ${ev.ended_at ? hhmmss(ev.ended_at) : "\u2026"}`;
    tipDur.textContent = `dur ${fmtDur(dur)}`;
    tip.style.opacity = "1"; tip.style.left = cx + "px"; tip.style.top = cy + "px";
  }

  const clickCapture = $("#clickCapture");

  clickCapture.addEventListener("pointermove", e => {
    if (focus.active) { clickCapture.style.cursor = "default"; return; }
    const rect = glCanvas.getBoundingClientRect();
    mouse.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
    mouse.y = -((e.clientY - rect.top) / rect.height) * 2 - 1;
    raycaster.setFromCamera(mouse, camera);
    const hits = raycaster.intersectObjects(pickMeshes, false);
    const hit = hits.length ? hits[0].object : null;
    setHover(hit, e.clientX, e.clientY);
    clickCapture.style.cursor = hit ? "pointer" : "default";
  });

  // Click via pointerdown/pointerup
  let _pDownTime = 0, _pDownX = 0, _pDownY = 0;

  clickCapture.addEventListener("pointerdown", e => {
    _pDownTime = performance.now();
    _pDownX = e.clientX;
    _pDownY = e.clientY;
  });

  clickCapture.addEventListener("pointerup", e => {
    const dt = performance.now() - _pDownTime;
    const dx = Math.abs(e.clientX - _pDownX);
    const dy = Math.abs(e.clientY - _pDownY);
    if (dt > 500 || dx > 10 || dy > 10) return;

    const rect = glCanvas.getBoundingClientRect();
    const mx = ((e.clientX - rect.left) / rect.width) * 2 - 1;
    const my = -((e.clientY - rect.top) / rect.height) * 2 - 1;
    raycaster.setFromCamera(new THREE.Vector2(mx, my), camera);
    const hits = raycaster.intersectObjects(pickMeshes, false);
    const hitId = hits.length ? hits[0].object.userData.eventId : null;

    if (focus.active) {
      if (hitId === focus.eventId) return;
      if (hitId) { enterFocus(hitId); return; }
      exitFocus();
      return;
    }

    if (hitId) enterFocus(hitId);
  });

  function onEscKey(eKey) {
    if (eKey.key === "Escape" && focus.active) exitFocus();
  }
  window.addEventListener("keydown", onEscKey);

  // ============================================================
  // Drawer (inspect mode JSON panel)
  // ============================================================

  function buildEventJson(ev) {
    // Real event data instead of mock
    return JSON.stringify({
      id: ev.id,
      lane: ev.lane,
      event_type: ev.event_type || ev.lane,
      title: ev.title,
      status: ev.status,
      started_at: hhmmss(ev.started_at),
      ended_at: ev.ended_at ? hhmmss(ev.ended_at) : null,
      duration: ev.ended_at ? fmtDur(ev.ended_at - ev.started_at) :
        (ev.status === "active" ? "active" : null),
      details: ev.details || {},
    }, null, 2);
  }

  function createDrawerTexture(jsonStr, w, h) {
    const cvs = document.createElement("canvas");
    const dpr = 2;
    cvs.width = w * dpr; cvs.height = h * dpr;
    const ctx = cvs.getContext("2d");
    ctx.scale(dpr, dpr);

    const grad = ctx.createLinearGradient(0, 0, 0, h);
    grad.addColorStop(0, "rgba(8,12,24,0.97)");
    grad.addColorStop(1, "rgba(4,6,14,0.99)");
    ctx.fillStyle = grad;
    roundRect(ctx, 0, 0, w, h, 10); ctx.fill();

    ctx.strokeStyle = "rgba(86,212,245,0.22)"; ctx.lineWidth = 1;
    roundRect(ctx, 0.5, 0.5, w - 1, h - 1, 10); ctx.stroke();

    ctx.strokeStyle = "rgba(86,212,245,0.40)"; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(12, 2); ctx.lineTo(w - 12, 2); ctx.stroke();

    ctx.fillStyle = "rgba(86,212,245,0.82)";
    ctx.font = "bold 12px 'IBM Plex Mono', monospace";
    ctx.fillText("EVENT DATA", 14, 24);
    ctx.fillStyle = "rgba(155,138,255,0.50)";
    ctx.font = "10px 'IBM Plex Mono', monospace";
    ctx.fillText("// raw payload", 120, 24);

    ctx.strokeStyle = "rgba(255,255,255,0.06)";
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(14, 32); ctx.lineTo(w - 14, 32); ctx.stroke();

    ctx.font = "10px 'IBM Plex Mono', monospace";
    const lines = jsonStr.split("\n");
    let y = 48;
    const lineH = 14;
    for (let i = 0; i < lines.length && y < h - 10; i++) {
      const line = lines[i];
      if (line.includes(":")) {
        const ci = line.indexOf(":");
        const key = line.slice(0, ci + 1);
        const val = line.slice(ci + 1);
        ctx.fillStyle = "rgba(155,138,255,0.78)";
        ctx.fillText(key, 14, y);
        const kw = ctx.measureText(key).width;
        if (val.trim().startsWith('"')) ctx.fillStyle = "rgba(74,234,188,0.72)";
        else if (val.trim().match(/^[0-9]/) || val.trim() === "null") ctx.fillStyle = "rgba(240,168,64,0.78)";
        else ctx.fillStyle = "rgba(235,240,255,0.45)";
        ctx.fillText(val, 14 + kw, y);
      } else {
        ctx.fillStyle = "rgba(235,240,255,0.35)";
        ctx.fillText(line, 14, y);
      }
      y += lineH;
    }

    const tex = new THREE.CanvasTexture(cvs);
    tex.minFilter = THREE.LinearFilter;
    tex.magFilter = THREE.LinearFilter;
    return tex;
  }

  function roundRect(ctx, x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.lineTo(x + w - r, y);
    ctx.quadraticCurveTo(x + w, y, x + w, y + r);
    ctx.lineTo(x + w, y + h - r);
    ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
    ctx.lineTo(x + r, y + h);
    ctx.quadraticCurveTo(x, y + h, x, y + h - r);
    ctx.lineTo(x, y + r);
    ctx.quadraticCurveTo(x, y, x + r, y);
    ctx.closePath();
  }

  // ============================================================
  // Enter / Exit focus (inspect mode)
  // ============================================================

  function enterFocus(eventId) {
    const ev = sseState.events.get(eventId);
    if (!ev) return;
    const pair = eventMeshes.get(eventId);
    if (!pair) return;

    const wasActive = focus.active;

    if (!wasActive) {
      focus.savedZoom = target.zoom;
      focus.savedPanY = target.panY;
      focus.savedPanX = target.panX;
      focus.savedTilt = target.tilt;
      focus.savedYaw = target.yaw;
      focus.savedOrbit = target.orbit;
    }

    focus.active = true;
    focus.eventId = eventId;

    if (wasActive) {
      focus.swoopT = 1;
      focus.drawerT = 0;
    } else {
      focus.swoopT = 0;
      focus.drawerT = 0;
    }

    setSelected(eventId, { source: "timeline", centerNow: false });
    tip.style.opacity = "0";

    destroyDrawer();
    const json = buildEventJson(ev);
    const tex = createDrawerTexture(json, 340, 480);

    const bx = pair.mesh.scale;
    // Blend between fixed width and event-proportional based on drawerScale setting
    const fixedW = focusSettings.drawerW;
    const scaledW = Math.max(bx.x * 0.85, 80);
    const drawerW = THREE.MathUtils.lerp(fixedW, scaledW, focusSettings.drawerScale);
    const drawerH = drawerW * (480 / 340);

    const dMat = new THREE.MeshBasicMaterial({
      map: tex, transparent: true, opacity: 0, side: THREE.DoubleSide, depthWrite: false,
    });
    const dMesh = new THREE.Mesh(new THREE.PlaneGeometry(drawerW, drawerH), dMat);

    const cMat = new THREE.MeshBasicMaterial({
      color: 0x56d4f5, transparent: true, opacity: 0, depthWrite: false,
    });
    const cMesh = new THREE.Mesh(new THREE.PlaneGeometry(1, 1), cMat);

    const grp = new THREE.Group();
    grp.add(dMesh); grp.add(cMesh);
    clipsGroup.add(grp);

    focus.drawerGroup = grp;
    focus.drawerMesh = dMesh;
    focus.connMesh = cMesh;
    focus.drawerH = drawerH;

    $("#focusOverlay").classList.add("active");
    $("#focusBadge").classList.add("active");
    $(".centerLine").style.opacity = "0";
    nowBadge.style.opacity = "0.3";
  }

  function exitFocus() {
    if (!focus.active) return;

    target.zoom = focus.savedZoom;
    target.panY = focus.savedPanY;
    target.panX = 0;
    target.tilt = focus.savedTilt;
    target.yaw = focus.savedYaw;
    target.orbit = focus.savedOrbit;
    target.nowPlaneOpacity = 1.0;

    focus.active = false;

    const grp = focus.drawerGroup;
    const dm = focus.drawerMesh;
    const cm = focus.connMesh;
    const startOp = dm ? dm.material.opacity : 0;
    const t0 = performance.now();
    function animClose(now) {
      const p = Math.min(1, (now - t0) / 350);
      const e = 1 - Math.pow(1 - p, 3);
      if (dm) { dm.material.opacity = startOp * (1 - e); dm.position.y += 0.15; }
      if (cm) cm.material.opacity *= (1 - e * 0.05);
      if (p < 1) requestAnimationFrame(animClose);
      else if (grp && grp.parent) grp.parent.remove(grp);
    }
    requestAnimationFrame(animClose);

    focus.drawerGroup = null;
    focus.drawerMesh = null;
    focus.connMesh = null;
    focus.eventId = null;
    focus.swoopT = 0;
    focus.drawerT = 0;

    setSelected(null, { source: "ui", centerNow: false });
    $("#focusOverlay").classList.remove("active");
    $("#focusBadge").classList.remove("active");
    $(".centerLine").style.opacity = "1";
    nowBadge.style.opacity = "1";
  }

  function destroyDrawer() {
    if (focus.drawerGroup && focus.drawerGroup.parent) focus.drawerGroup.parent.remove(focus.drawerGroup);
    focus.drawerGroup = null;
    focus.drawerMesh = null;
    focus.connMesh = null;
  }

  // ============================================================
  // Ruler
  // ============================================================

  const rangeReadout = $("#rangeReadout");
  function ndcToWorld(nx, ny) {
    const v = new THREE.Vector3(nx, ny, 0).unproject(camera);
    const dir = new THREE.Vector3(); camera.getWorldDirection(dir);
    return v.addScaledVector(dir, -v.z / dir.z);
  }

  function drawRuler() {
    const dpr = devicePixelRatio || 1;
    const W = rulerCanvas.width, H = rulerCanvas.height;
    rulerCtx.clearRect(0, 0, W, H);

    const g = rulerCtx.createLinearGradient(0, 0, 0, H);
    g.addColorStop(0, "rgba(255,255,255,0.04)"); g.addColorStop(1, "rgba(255,255,255,0.00)");
    rulerCtx.fillStyle = g; rulerCtx.fillRect(0, 0, W, H);

    const leftW = ndcToWorld(-1, 0), rightW = ndcToWorld(1, 0);
    const now = serverNow();
    const sL = leftW.x / s.timeScale, sR = rightW.x / s.timeScale;
    rangeReadout.textContent = `${hhmm(now + sL)} \u2192 ${hhmm(now + sR)}`;

    const pxPS = (W / dpr) / Math.max(1e-6, sR - sL);
    const major = pxPS > 320 ? 2 : pxPS > 200 ? 5 : pxPS > 120 ? 10 : pxPS > 70 ? 30 : 60;
    const minor = major / 5;
    const start = Math.floor(sL / minor) * minor;
    const end = Math.ceil(sR / minor) * minor;

    rulerCtx.save(); rulerCtx.scale(dpr, dpr);
    const wC = W / dpr, hC = H / dpr;
    rulerCtx.strokeStyle = "rgba(255,255,255,0.10)";
    rulerCtx.beginPath(); rulerCtx.moveTo(0, hC - 0.5); rulerCtx.lineTo(wC, hC - 0.5); rulerCtx.stroke();

    for (let t = start; t <= end; t += minor) {
      const x = ((t - sL) / (sR - sL)) * wC;
      const isM = Math.abs(t / major - Math.round(t / major)) < 1e-6;
      rulerCtx.strokeStyle = isM ? "rgba(255,255,255,0.20)" : "rgba(255,255,255,0.08)";
      rulerCtx.beginPath(); rulerCtx.moveTo(x, isM ? 8 : 16); rulerCtx.lineTo(x, hC); rulerCtx.stroke();
      if (isM) {
        rulerCtx.fillStyle = "rgba(235,240,255,0.72)";
        rulerCtx.font = "11px 'IBM Plex Mono', monospace";
        rulerCtx.fillText(hhmm(now + t), x + 5, 16);
      }
    }

    rulerCtx.fillStyle = "rgba(86,212,245,0.70)";
    rulerCtx.font = "11px 'IBM Plex Mono', monospace";
    rulerCtx.fillText(hhmm(now), wC / 2 + 8, 32);

    rulerCtx.restore();
  }

  // ============================================================
  // Camera
  // ============================================================

  function resize() {
    const rect = viewport.getBoundingClientRect();
    const w = Math.max(1, Math.floor(rect.width));
    const h = Math.max(1, Math.floor(rect.height - 42));
    renderer.setSize(w, h, false);
    rulerCanvas.width = Math.floor(rect.width * (devicePixelRatio || 1));
    rulerCanvas.height = Math.floor(42 * (devicePixelRatio || 1));
    rulerCanvas.style.width = rect.width + "px";
    rulerCanvas.style.height = "42px";
    camera = new THREE.OrthographicCamera(-w / 2, w / 2, h / 2, -h / 2, -5000, 5000);
    camera.zoom = s.zoom;
    camera.updateProjectionMatrix();

    const miniEl = $("#minimap");
    const mw = miniEl.offsetWidth, mh = miniEl.offsetHeight;
    const mdpr = Math.min(devicePixelRatio || 1, 2);
    miniRenderer.setSize(Math.floor(mw * mdpr), Math.floor(mh * mdpr), false);
    miniGLCanvas.style.width = mw + "px";
    miniGLCanvas.style.height = mh + "px";
    miniOverlay.width = Math.floor(mw * mdpr);
    miniOverlay.height = Math.floor(mh * mdpr);
    miniOverlay.style.width = mw + "px";
    miniOverlay.style.height = mh + "px";
    miniCamera = new THREE.OrthographicCamera(-mw / 2, mw / 2, mh / 2, -mh / 2, -5000, 5000);
    miniCamera.zoom = MINI_ZOOM_FACTOR;
    miniCamera.updateProjectionMatrix();
  }
  window.addEventListener("resize", resize);

  function updateCamera() {
    const rect = viewport.getBoundingClientRect();
    const w = Math.max(1, rect.width), h = Math.max(1, rect.height - 42);
    camera.left = -w / 2; camera.right = w / 2; camera.top = h / 2; camera.bottom = -h / 2;
    camera.zoom = s.zoom;
    camera.updateProjectionMatrix();

    const cx = s.panX;
    const cy = s.panY;
    const yawR = THREE.MathUtils.degToRad(s.yaw);
    const tiltR = THREE.MathUtils.degToRad(s.tilt);
    const R = s.orbit;
    camera.position.set(
      cx + Math.sin(yawR) * R * Math.cos(tiltR),
      cy + Math.sin(tiltR) * R,
      Math.cos(yawR) * R * Math.cos(tiltR),
    );
    camera.lookAt(cx, cy, 0);
  }

  function applyLook() {
    if (gridLines) { gridLines.material.opacity = s.grid; gridLines.material.needsUpdate = true; }
    glCanvas.style.filter = `drop-shadow(0 16px 40px rgba(0,0,0,.5)) drop-shadow(0 0 ${16 + s.glow * 22}px rgba(86,212,245,${0.08 + s.glow * 0.08}))`;
  }

  // ============================================================
  // Minimap
  // ============================================================

  function updateMiniCamera() {
    if (!miniCamera) return;
    const miniEl = $("#minimap");
    const mw = miniEl.offsetWidth, mh = miniEl.offsetHeight;
    miniCamera.left = -mw / 2;
    miniCamera.right = mw / 2;
    miniCamera.top = mh / 2;
    miniCamera.bottom = -mh / 2;
    miniCamera.zoom = MINI_ZOOM_FACTOR;
    miniCamera.updateProjectionMatrix();

    const cx = s.panX;
    const cy = s.panY;
    const yawR = THREE.MathUtils.degToRad(s.yaw);
    const tiltR = THREE.MathUtils.degToRad(s.tilt);
    const R = s.orbit;
    miniCamera.position.set(
      cx + Math.sin(yawR) * R * Math.cos(tiltR),
      cy + Math.sin(tiltR) * R,
      Math.cos(yawR) * R * Math.cos(tiltR),
    );
    miniCamera.lookAt(cx, cy, 0);
  }

  function drawMiniOverlay() {
    if (!miniCamera || !camera) return;
    const miniEl = $("#minimap");
    const mw = miniEl.offsetWidth, mh = miniEl.offsetHeight;
    const dpr = Math.min(devicePixelRatio || 1, 2);
    const W = miniOverlay.width, H = miniOverlay.height;
    miniCtx.clearRect(0, 0, W, H);

    function mainNdcToWorld(nx, ny) {
      const v = new THREE.Vector3(nx, ny, 0).unproject(camera);
      const dir = new THREE.Vector3(); camera.getWorldDirection(dir);
      if (Math.abs(dir.z) < 1e-6) return v;
      return v.addScaledVector(dir, -v.z / dir.z);
    }
    function worldToMiniScreen(wp) {
      const v = wp.clone().project(miniCamera);
      return {
        x: (v.x * 0.5 + 0.5) * mw * dpr,
        y: (-v.y * 0.5 + 0.5) * mh * dpr,
      };
    }

    const corners = [
      mainNdcToWorld(-1, -1),
      mainNdcToWorld(1, -1),
      mainNdcToWorld(1, 1),
      mainNdcToWorld(-1, 1),
    ];

    const screenCorners = corners.map(c => worldToMiniScreen(c));

    miniCtx.save();
    miniCtx.strokeStyle = "rgba(86,212,245,0.65)";
    miniCtx.lineWidth = 1.5 * dpr;
    miniCtx.setLineDash([4 * dpr, 3 * dpr]);
    miniCtx.beginPath();
    miniCtx.moveTo(screenCorners[0].x, screenCorners[0].y);
    for (let i = 1; i < 4; i++) miniCtx.lineTo(screenCorners[i].x, screenCorners[i].y);
    miniCtx.closePath();
    miniCtx.stroke();

    miniCtx.fillStyle = "rgba(86,212,245,0.04)";
    miniCtx.fill();

    miniCtx.fillStyle = "rgba(86,212,245,0.80)";
    screenCorners.forEach(c => {
      miniCtx.beginPath();
      miniCtx.arc(c.x, c.y, 2.5 * dpr, 0, Math.PI * 2);
      miniCtx.fill();
    });

    miniCtx.restore();
  }

  // Minimap click-to-navigate
  $("#minimap").addEventListener("click", e => {
    if (!miniCamera) return;
    const miniEl = $("#minimap");
    const rect = miniEl.getBoundingClientRect();
    const nx = ((e.clientX - rect.left) / rect.width) * 2 - 1;
    const ny = -((e.clientY - rect.top) / rect.height) * 2 - 1;

    const v = new THREE.Vector3(nx, ny, 0).unproject(miniCamera);
    const dir = new THREE.Vector3();
    miniCamera.getWorldDirection(dir);
    if (Math.abs(dir.z) > 1e-6) {
      v.addScaledVector(dir, -v.z / dir.z);
    }
    target.panY = clamp(v.y, -totalH - 180, 140);
  });

  // ============================================================
  // Controls (wheel, keyboard, sliders)
  // ============================================================

  clickCapture.addEventListener("wheel", e => {
    e.preventDefault();
    if (focus.active) return;
    const d = e.deltaY;
    if (e.ctrlKey) {
      target.zoom = clamp(target.zoom * (d > 0 ? 0.94 : 1.06), 0.25, 5);
    } else if (e.shiftKey) {
      target.panY = clamp(target.panY + (d * 0.9) / target.zoom * (s.reducedMotion ? 1 : 0.75), -totalH - 180, 140);
    }
  }, { passive: false });

  function onArrowKey(e) {
    if (focus.active) return;
    const step = 80 / target.zoom;
    if (e.key === "ArrowUp") target.panY = clamp(target.panY + step * 0.7, -totalH - 180, 140);
    if (e.key === "ArrowDown") target.panY = clamp(target.panY - step * 0.7, -totalH - 180, 140);
  }
  window.addEventListener("keydown", onArrowKey);

  function bindRange(id, key, fmt = v => v) {
    const el = $("#" + id), out = $("#" + id + "Out");
    if (!el || !out) return;
    const set = () => out.textContent = fmt(parseFloat(el.value));
    set();
    el.addEventListener("input", () => {
      target[key] = parseFloat(el.value);
      set();
      if (["laneH", "timeScale", "boxDepth", "musicZ"].includes(key)) {
        s[key] = target[key];
        rebuildLanesAndGrid();
      }
    });
  }

  bindRange("timeScale", "timeScale", v => Math.round(v) + "");
  bindRange("zoom", "zoom", v => v.toFixed(2));
  bindRange("laneH", "laneH", v => Math.round(v) + "");
  bindRange("boxDepth", "boxDepth", v => Math.round(v) + "");
  bindRange("tilt", "tilt", v => Math.round(v) + "\u00b0");
  bindRange("yaw", "yaw", v => Math.round(v) + "\u00b0");
  bindRange("orbit", "orbit", v => Math.round(v) + "");
  bindRange("grid", "grid", v => v.toFixed(2));
  bindRange("glow", "glow", v => v.toFixed(2));
  bindRange("musicZ", "musicZ", v => Math.round(v) + "px");

  // Inspect settings sliders (write to focusSettings, not target)
  function bindFocusRange(id, key, fmt = v => v) {
    const el = $("#" + id), out = $("#" + id + "Out");
    if (!el || !out) return;
    const set = () => out.textContent = fmt(parseFloat(el.value));
    set();
    el.addEventListener("input", () => {
      focusSettings[key] = parseFloat(el.value);
      set();
    });
  }

  bindFocusRange("focusZoom", "zoom", v => v.toFixed(1));
  bindFocusRange("focusTilt", "tilt", v => Math.round(v) + "\u00b0");
  bindFocusRange("focusYaw", "yaw", v => Math.round(v) + "\u00b0");
  bindFocusRange("focusOrbit", "orbit", v => Math.round(v) + "");
  bindFocusRange("drawerW", "drawerW", v => Math.round(v) + "px");
  bindFocusRange("drawerScale", "drawerScale", v => (v * 100).toFixed(0) + "%");

  const reducedEl = $("#reduced");
  if (reducedEl) reducedEl.addEventListener("change", e => target.reducedMotion = e.target.checked);

  $("#btnLive").addEventListener("click", () => {
    if (focus.active) exitFocus();
    target.panY = -(tracks.length * s.laneH) / 2 + 10;
    target.zoom = 0.70;
    target.panX = 0;
    target.nowPlaneOpacity = 1.0;
  });

  $("#btnClear").addEventListener("click", () => {
    if (focus.active) exitFocus();
    setSelected(null, { source: "ui", centerNow: false });
  });

  // ============================================================
  // Main loop
  // ============================================================

  let lastT = performance.now();
  let rafHandle = null;

  function tick(now) {
    const dt = Math.min(0.05, (now - lastT) / 1000);
    lastT = now;

    // Clock
    const realNow = serverNow();
    clockEl.textContent = hhmmss(realNow);
    nowBadge.textContent = "NOW " + hhmmss(realNow);

    // Full rebuild if needed (new snapshot / new lanes)
    if (needsFullRebuild) {
      needsFullRebuild = false;
      buildLegend();
      rebuildLanesAndGrid();
      updateAllMeshes();
      renderActivity();
      target.panY = -(tracks.length * s.laneH) / 2 + 10;
    }

    const damp = s.reducedMotion ? 0.35 : 0.13;
    s.reducedMotion = target.reducedMotion;

    // ── Focus swooping ──
    if (focus.active && focus.eventId) {
      focus.swoopT = Math.min(1, focus.swoopT + dt * 2.0);
      const e3 = 1 - Math.pow(1 - focus.swoopT, 3);

      const pair = eventMeshes.get(focus.eventId);
      if (pair) {
        const mp = pair.mesh.position;

        target.zoom = THREE.MathUtils.lerp(focus.savedZoom, focusSettings.zoom, e3);
        target.panY = THREE.MathUtils.lerp(focus.savedPanY, mp.y, e3);
        target.panX = THREE.MathUtils.lerp(focus.savedPanX, mp.x, e3);
        target.tilt = THREE.MathUtils.lerp(focus.savedTilt, focusSettings.tilt, e3);
        target.yaw = THREE.MathUtils.lerp(focus.savedYaw, focusSettings.yaw, e3);
        target.orbit = THREE.MathUtils.lerp(focus.savedOrbit, focusSettings.orbit, e3);
        target.nowPlaneOpacity = THREE.MathUtils.lerp(1.0, 0.0, e3);

        if (focus.swoopT > 0.35 && focus.drawerMesh) {
          focus.drawerT = Math.min(1, focus.drawerT + dt * 2.8);
          const de = 1 - Math.pow(1 - focus.drawerT, 3);

          const bx = pair.mesh.scale;
          const dH = focus.drawerH || bx.y * 3;

          focus.drawerMesh.material.opacity = de * 0.92;
          focus.drawerMesh.position.set(
            mp.x,
            mp.y - bx.y * 0.5 - dH * 0.5 * de - 2 * de,
            mp.z + bx.z * 0.5 + 3,
          );

          if (focus.connMesh) {
            const connH = Math.max(0.1, (dH * 0.5 * de + 2 * de));
            focus.connMesh.material.opacity = de * 0.30;
            focus.connMesh.scale.set(2, connH, 1);
            focus.connMesh.position.set(
              mp.x,
              mp.y - bx.y * 0.5 - connH * 0.5,
              mp.z + bx.z * 0.5 + 2,
            );
          }
        }
      }
    }

    // Standard damping
    s.panY += (target.panY - s.panY) * damp;
    s.panX += (target.panX - s.panX) * damp;
    s.zoom += (target.zoom - s.zoom) * (s.reducedMotion ? 0.26 : 0.15);
    s.tilt += (target.tilt - s.tilt) * 0.09;
    s.yaw += (target.yaw - s.yaw) * 0.09;
    s.orbit += (target.orbit - s.orbit) * 0.09;
    s.grid += (target.grid - s.grid) * 0.11;
    s.glow += (target.glow - s.glow) * 0.11;
    s.musicZ += (target.musicZ - s.musicZ) * 0.11;
    s.nowPlaneOpacity += (target.nowPlaneOpacity - s.nowPlaneOpacity) * 0.12;

    // Now Plane pulse
    if (nowPlaneGroup.children.length >= 3) {
      const t2 = now * 0.001;
      const npo = s.nowPlaneOpacity;
      nowPlaneGroup.children[0].material.opacity = (0.032 + Math.sin(t2 * 1.8) * 0.012) * npo;
      nowPlaneGroup.children[1].material.opacity = (0.22 + Math.sin(t2 * 2.4) * 0.07) * npo;
      nowPlaneGroup.children[2].material.opacity = (0.010 + Math.sin(t2 * 1.2) * 0.004) * npo;
    }

    updateCamera();
    updateAllMeshes();
    applyLook();
    applySelGlow();
    drawRuler();

    renderer.render(scene, camera);

    // Minimap
    updateMiniCamera();
    miniRenderer.render(scene, miniCamera);
    drawMiniOverlay();

    rafHandle = requestAnimationFrame(tick);
  }

  // ============================================================
  // Boot
  // ============================================================

  function boot() {
    resize();
    // Start with empty scene — SSE will populate
    rebuildLanesAndGrid();
    connectSSE();
    rafHandle = requestAnimationFrame(tick);
  }

  // ============================================================
  // Cleanup (for HTMX navigation away)
  // ============================================================

  window.__timelineCleanup = function() {
    if (rafHandle) { cancelAnimationFrame(rafHandle); rafHandle = null; }
    if (eventSource) { eventSource.close(); eventSource = null; }
    renderer.dispose();
    miniRenderer.dispose();
    window.removeEventListener("resize", resize);
    window.removeEventListener("keydown", onEscKey);
    window.removeEventListener("keydown", onArrowKey);
  };

  // Auto-cleanup when HTMX swaps #page-content (navigating away)
  document.body.addEventListener("htmx:beforeSwap", function _onSwap() {
    if (window.__timelineCleanup) window.__timelineCleanup();
    window.__timelineCleanup = null;
    document.body.removeEventListener("htmx:beforeSwap", _onSwap);
  });

  boot();
};

// Auto-init on first load (direct navigation / full page load)
window.initTimeline();
