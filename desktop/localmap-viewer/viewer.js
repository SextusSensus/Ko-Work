/* K1 Local Map — warehouse-grade Three.js viewer
 * Aesthetic: Tesla × SpaceX × Apple (near-black, white type, cyan #32D4FF).
 * Booster K1 proxy envelope: ~0.95 m H × 0.40 m W × 0.18 m D.
 * Textures: Poly Haven CC0 (assets/ATTRIBUTION.md).
 */
(function () {
  'use strict';

  var ACCENT = 0x32d4ff;
  var DANGER = 0xe31937;
  var BG = 0x000000;
  var RACK = 0x3a424c;
  var SAFETY = 0xc9a227;
  var OCC_BASE = 0x6a737d;
  var K1_WHITE = 0xf2f2f7;
  var K1_DARK = 0x1c1c1e;
  var K1_H = 0.95, K1_W = 0.40, K1_D = 0.18;
  var MAX_TRAIL = 280;
  var DATA_ROOT = '../localmap-data';
  var DOMAINS_INDEX = DATA_ROOT + '/domains.json';

  var viewport = document.getElementById('viewport');
  var statsEl = document.getElementById('stats');
  var errEl = document.getElementById('err');
  var domainBar = document.getElementById('domain-bar');
  var domainTitle = document.getElementById('domain-title');
  var modal = document.getElementById('new-domain-modal');
  var newNameInput = document.getElementById('new-domain-name');
  var poseXEl = document.getElementById('pose-x');
  var poseYEl = document.getElementById('pose-y');
  var poseYawEl = document.getElementById('pose-yaw');
  var poseTrailEl = document.getElementById('pose-trail');
  var poseVxEl = document.getElementById('pose-vx');
  var poseVyEl = document.getElementById('pose-vy');
  var poseWzEl = document.getElementById('pose-wz');
  var liveDotEl = document.getElementById('live-dot');
  var liveStateEl = document.getElementById('live-state');
  var liveDetailEl = document.getElementById('live-detail');

  var layers = { env: true, occ: true, robot: true, trail: true, follow: true };

  var scene = new THREE.Scene();
  scene.background = new THREE.Color(BG);
  scene.fog = new THREE.FogExp2(BG, 0.022);

  var camera = new THREE.PerspectiveCamera(42, 1, 0.05, 160);
  var renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: 'high-performance' });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setSize(viewport.clientWidth || 800, viewport.clientHeight || 600);
  if (renderer.toneMapping !== undefined) {
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 1.08;
  }
  if (renderer.outputColorSpace !== undefined && THREE.SRGBColorSpace) {
    renderer.outputColorSpace = THREE.SRGBColorSpace;
  } else if (renderer.outputEncoding !== undefined && THREE.sRGBEncoding) {
    renderer.outputEncoding = THREE.sRGBEncoding;
  }
  if (renderer.shadowMap) {
    renderer.shadowMap.enabled = true;
    renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  }
  viewport.appendChild(renderer.domElement);

  scene.add(new THREE.HemisphereLight(0xc5d4e8, 0x121214, 0.42));
  scene.add(new THREE.AmbientLight(0xffffff, 0.16));
  var key = new THREE.DirectionalLight(0xfff4e8, 0.95);
  key.position.set(7, 14, 5);
  key.castShadow = true;
  if (key.shadow) {
    key.shadow.mapSize.set(2048, 2048);
    key.shadow.camera.near = 0.5;
    key.shadow.camera.far = 50;
    key.shadow.camera.left = -16;
    key.shadow.camera.right = 16;
    key.shadow.camera.top = 16;
    key.shadow.camera.bottom = -16;
    key.shadow.bias = -0.0002;
  }
  scene.add(key);
  var rim = new THREE.DirectionalLight(ACCENT, 0.18);
  rim.position.set(-5, 4, -7);
  scene.add(rim);
  var fill = new THREE.DirectionalLight(0xffffff, 0.22);
  fill.position.set(-8, 6, 3);
  scene.add(fill);

  var envGroup = new THREE.Group();
  var cellGroup = new THREE.Group();
  var robotGroup = new THREE.Group();
  var trailGroup = new THREE.Group();
  scene.add(envGroup);
  scene.add(cellGroup);
  scene.add(robotGroup);
  scene.add(trailGroup);

  var trailPoints = [];
  // Default elevated 3/4 framing (OrbitControls owns target + spherical state)
  var DEFAULT_RADIUS = 11;
  var DEFAULT_THETA = 0.85;   // azimuth
  var DEFAULT_PHI = 1.05;     // polar ~60° from zenith → 3/4 view
  var orbitTarget = new THREE.Vector3(0, 0.4, 0);

  var registry = { active: null, domains: [] };
  var currentMap = null;
  var activeDomainId = null;
  var floorTex = null, metalTex = null, plasterTex = null, concreteTex = null, concreteRough = null;
  var woodTex = null, cardboardTex = null, shutterTex = null, antiSlipTex = null;
  var gltfCache = {};
  var texLoader = new THREE.TextureLoader();
  var gltfLoader = (typeof THREE !== 'undefined' && THREE.GLTFLoader) ? new THREE.GLTFLoader() : null;

  // Realtime telemetry (WebSocket) — buffer latest odom; apply in rAF
  var lastOdom = null;
  var pendingOdom = null;
  var trailDirty = false;
  var trailRebuildCooldown = 0;
  var telem = {
    enabled: false,
    url: null,
    ws: null,
    state: 'offline', // offline | connecting | live | reconnecting
    retries: 0,
    timer: null,
    lastMsgAt: 0,
    status: null
  };
  var clock = typeof THREE.Clock === 'function' ? new THREE.Clock() : null;
  var ledPulse = 0;

  function loadTex(url) {
    return new Promise(function (resolve) {
      texLoader.load(url, function (t) {
        t.wrapS = t.wrapT = THREE.RepeatWrapping;
        if (THREE.SRGBColorSpace) t.colorSpace = THREE.SRGBColorSpace;
        else if (THREE.sRGBEncoding !== undefined) t.encoding = THREE.sRGBEncoding;
        t.anisotropy = Math.min(8, (renderer.capabilities && renderer.capabilities.getMaxAnisotropy)
          ? renderer.capabilities.getMaxAnisotropy() : 1);
        resolve(t);
      }, undefined, function () { resolve(null); });
    });
  }

  function texRepeat(base, rx, ry) {
    if (!base) return null;
    var t = base.clone();
    t.needsUpdate = true;
    t.wrapS = t.wrapT = THREE.RepeatWrapping;
    t.repeat.set(rx, ry);
    if (THREE.SRGBColorSpace && base.colorSpace) t.colorSpace = base.colorSpace;
    else if (THREE.sRGBEncoding !== undefined && base.encoding !== undefined) t.encoding = base.encoding;
    return t;
  }

  // OrbitControls: full azimuth + polar orbit, zoom, pan (WebView2/iframe safe)
  var controls = null;
  if (typeof THREE.OrbitControls === 'function') {
    controls = new THREE.OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.enablePan = true;
    controls.enableZoom = true;
    controls.enableRotate = true;
    controls.screenSpacePanning = true;
    controls.minDistance = 2.4;
    controls.maxDistance = 36;
    // Full polar orbit (near zenith ↔ near nadir); azimuth unrestricted = 360°
    controls.minPolarAngle = 0.05;
    controls.maxPolarAngle = Math.PI - 0.05;
    controls.minAzimuthAngle = -Infinity;
    controls.maxAzimuthAngle = Infinity;
    controls.rotateSpeed = 0.9;
    controls.zoomSpeed = 1.0;
    controls.panSpeed = 0.85;
    controls.target.copy(orbitTarget);
  }

  function frameCamera(radius, theta, phi, lookAt) {
    var r = radius != null ? radius : DEFAULT_RADIUS;
    var th = theta != null ? theta : DEFAULT_THETA;
    var ph = phi != null ? phi : DEFAULT_PHI;
    if (lookAt) orbitTarget.copy(lookAt);
    camera.position.set(
      orbitTarget.x + r * Math.sin(ph) * Math.sin(th),
      orbitTarget.y + r * Math.cos(ph),
      orbitTarget.z + r * Math.sin(ph) * Math.cos(th)
    );
    if (controls) {
      controls.target.copy(orbitTarget);
      controls.update();
    } else {
      camera.lookAt(orbitTarget);
    }
  }
  frameCamera(DEFAULT_RADIUS, DEFAULT_THETA, DEFAULT_PHI);

  function onResize() {
    var w = viewport.clientWidth || window.innerWidth;
    var h = viewport.clientHeight || window.innerHeight;
    camera.aspect = w / Math.max(h, 1);
    camera.updateProjectionMatrix();
    renderer.setSize(w, h);
  }
  window.addEventListener('resize', onResize);
  onResize();

  // Prevent parent scroll / gesture stealing inside WebView2 / iframe embeds
  var canvasEl = renderer.domElement;
  canvasEl.style.touchAction = 'none';
  canvasEl.style.userSelect = 'none';
  canvasEl.style.webkitUserSelect = 'none';
  canvasEl.tabIndex = 0; // allow focus so wheel stays local
  function isMapPointerTarget(t) {
    return t === canvasEl || t === viewport || (canvasEl.contains && canvasEl.contains(t));
  }
  // capture-phase preventDefault only — do NOT stopPropagation (OrbitControls must receive events)
  document.addEventListener('wheel', function (e) {
    if (isMapPointerTarget(e.target)) e.preventDefault();
  }, { passive: false, capture: true });
  document.addEventListener('touchmove', function (e) {
    if (isMapPointerTarget(e.target) && e.cancelable) e.preventDefault();
  }, { passive: false, capture: true });
  canvasEl.addEventListener('pointerdown', function () {
    try { canvasEl.focus({ preventScroll: true }); } catch (err) { try { canvasEl.focus(); } catch (err2) {} }
  }, true);
  canvasEl.addEventListener('contextmenu', function (e) { e.preventDefault(); });

  function stdMat(color, opts) {
    opts = opts || {};
    return new THREE.MeshStandardMaterial({
      color: color,
      metalness: opts.metalness != null ? opts.metalness : 0.18,
      roughness: opts.roughness != null ? opts.roughness : 0.62,
      emissive: opts.emissive || 0x000000,
      emissiveIntensity: opts.emissiveIntensity || 0,
      transparent: !!opts.transparent,
      opacity: opts.opacity != null ? opts.opacity : 1,
      map: opts.map || null
    });
  }

  function makeBox(w, h, d, material, x, y, z) {
    var m = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), material);
    m.position.set(x || 0, y || 0, z || 0);
    m.castShadow = true;
    m.receiveShadow = true;
    return m;
  }

  function clearGroup(g) {
    while (g.children.length) {
      var c = g.children[0];
      g.remove(c);
      c.traverse(function (obj) {
        if (obj.geometry) obj.geometry.dispose();
        if (obj.material) {
          if (Array.isArray(obj.material)) obj.material.forEach(function (mm) { mm.dispose(); });
          else obj.material.dispose();
        }
      });
    }
  }

  function showErr(msg) {
    if (!msg) { errEl.style.display = 'none'; errEl.textContent = ''; return; }
    errEl.style.display = 'block';
    errEl.textContent = String(msg);
  }

  function loadJson(url) {
    return fetch(url, { cache: 'no-store' }).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status + ' ' + url);
      return r.json();
    });
  }

  function formatUpdated(iso) {
    if (!iso) return '—';
    try {
      var d = new Date(iso);
      if (isNaN(d.getTime())) return iso;
      return d.toISOString().slice(0, 16).replace('T', ' ') + 'Z';
    } catch (e) { return iso; }
  }

  function findDomain(id) {
    for (var i = 0; i < registry.domains.length; i++) {
      if (registry.domains[i].id === id) return registry.domains[i];
    }
    return null;
  }

  // ---- Realtime telemetry (WebSocket) -------------------------------------
  function setLiveHud(state, detail) {
    telem.state = state || 'offline';
    if (liveStateEl) liveStateEl.textContent = String(state || 'OFFLINE').toUpperCase();
    if (liveDetailEl) liveDetailEl.textContent = detail || '—';
    if (!liveDotEl) return;
    liveDotEl.className = '';
    if (state === 'live') liveDotEl.classList.add('on');
    else if (state === 'connecting' || state === 'reconnecting') liveDotEl.classList.add('warn');
    else liveDotEl.classList.add('off');
  }

  function defaultWsUrl() {
    var q = new URLSearchParams(window.location.search);
    if (q.get('ws')) return q.get('ws');
    var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    if (location.hostname && location.port) {
      return proto + '//' + location.host + '/ws/telemetry';
    }
    return 'ws://127.0.0.1:8742/ws/telemetry';
  }

  function wantLive() {
    var q = new URLSearchParams(window.location.search);
    if (q.get('live') === '0' || q.get('live') === 'false') return false;
    if (q.get('live') === '1' || q.get('live') === 'true') return true;
    return location.port === '8742';
  }

  function handleTelemetryMessage(msg) {
    if (!msg || !msg.type) return;
    telem.lastMsgAt = Date.now();
    if (msg.type === 'odom') {
      pendingOdom = msg;
      lastOdom = msg;
      return;
    }
    if (msg.type === 'occupancy') {
      if (msg.domain_id && activeDomainId && msg.domain_id !== activeDomainId) {
        if (msg.pose) {
          pendingOdom = {
            type: 'odom', t: Date.now() / 1000,
            x: msg.pose.x || 0, y: msg.pose.y || 0, z: 0,
            yaw: msg.pose.yaw || 0, vx: 0, vy: 0, wz: 0
          };
          lastOdom = pendingOdom;
        }
        return;
      }
      showErr('');
      // Keep live trail; only refresh cells + soft pose from occupancy frames
      var keepTrail = trailPoints.slice();
      setOccupancy(msg);
      currentMap = msg;
      if (msg.pose && !pendingOdom) {
        pendingOdom = {
          type: 'odom', t: Date.now() / 1000,
          x: msg.pose.x || 0, y: msg.pose.y || 0, z: 0,
          yaw: msg.pose.yaw || 0, vx: 0, vy: 0, wz: 0
        };
      }
      if (keepTrail.length) {
        trailPoints = keepTrail;
        trailDirty = true;
      }
      var res = msg.res_m || 0.08;
      var cells = msg.cells || [];
      var range = msg.range_m || 3.5;
      var meta = findDomain(activeDomainId);
      var bits = cells.length + ' cells · res ' + res.toFixed(2) + ' m · range ' + range.toFixed(1) + ' m · LIVE';
      if (meta) bits += ' · ' + (meta.run_count || 0) + ' runs';
      statsEl.textContent = bits;
      return;
    }
    if (msg.type === 'status') {
      telem.status = msg;
      var bits = (msg.mode || 'ws') + ' · ' + (msg.hz || '?') + ' Hz';
      if (msg.battery != null) bits += ' · bat ' + Math.round(msg.battery) + '%';
      setLiveHud('live', bits);
    }
  }

  function scheduleReconnect() {
    if (!telem.enabled) return;
    if (telem.timer) return;
    telem.retries += 1;
    var wait = Math.min(8000, 400 * Math.pow(1.6, Math.min(telem.retries, 8)));
    setLiveHud('reconnecting', 'retry in ' + Math.round(wait / 100) / 10 + 's');
    telem.timer = setTimeout(function () {
      telem.timer = null;
      openTelemetrySocket();
    }, wait);
  }

  function openTelemetrySocket() {
    if (!telem.enabled) return;
    if (telem.ws && (telem.ws.readyState === 0 || telem.ws.readyState === 1)) return;
    var url = telem.url || defaultWsUrl();
    telem.url = url;
    setLiveHud('connecting', url.replace(/^ws(s)?:\/\//, ''));
    var ws;
    try { ws = new WebSocket(url); } catch (e) {
      setLiveHud('offline', String(e.message || e));
      scheduleReconnect();
      return;
    }
    telem.ws = ws;
    ws.onopen = function () {
      telem.retries = 0;
      setLiveHud('live', url.replace(/^ws(s)?:\/\//, ''));
    };
    ws.onmessage = function (ev) {
      try { handleTelemetryMessage(JSON.parse(ev.data)); }
      catch (err) { /* ignore bad frame */ }
    };
    ws.onerror = function () { /* onclose handles retry */ };
    ws.onclose = function () {
      if (telem.ws === ws) telem.ws = null;
      if (telem.enabled) scheduleReconnect();
      else setLiveHud('offline', 'poll');
    };
  }

  function connectTelemetry(url) {
    telem.enabled = true;
    if (url) telem.url = url;
    if (telem.timer) { clearTimeout(telem.timer); telem.timer = null; }
    if (telem.ws) {
      try { telem.ws.close(); } catch (e) {}
      telem.ws = null;
    }
    openTelemetrySocket();
  }

  function disconnectTelemetry() {
    telem.enabled = false;
    if (telem.timer) { clearTimeout(telem.timer); telem.timer = null; }
    if (telem.ws) {
      try { telem.ws.close(); } catch (e) {}
      telem.ws = null;
    }
    setLiveHud('offline', 'poll');
  }

  function applyPendingOdom() {
    if (!pendingOdom) return;
    var msg = pendingOdom;
    pendingOdom = null;
    var pose = { x: msg.x || 0, y: msg.y || 0, yaw: msg.yaw || 0 };
    // Throttled trail append — mark dirty; rebuild at most ~8 Hz
    var x = pose.x, y = pose.y;
    var last = trailPoints[trailPoints.length - 1];
    if (!last || Math.hypot(last.x - x, last.y - y) >= 0.04) {
      trailPoints.push({ x: x, y: y, yaw: pose.yaw });
      if (trailPoints.length > MAX_TRAIL) trailPoints.shift();
      trailDirty = true;
    } else if (last) {
      last.yaw = pose.yaw;
      trailDirty = true;
    }
    setPose(pose, msg);
  }

  // ---- Booster K1 proxy (~95×40×18 cm) ------------------------------------
  function buildK1() {
    clearGroup(robotGroup);
    var white = stdMat(K1_WHITE, { metalness: 0.28, roughness: 0.38 });
    var dark = stdMat(K1_DARK, { metalness: 0.45, roughness: 0.32 });
    var led = stdMat(ACCENT, { metalness: 0.1, roughness: 0.28, emissive: ACCENT, emissiveIntensity: 0.9 });
    led.userData.pulse = true;

    var foot = new THREE.Mesh(
      new THREE.RingGeometry(K1_W * 0.52, K1_W * 0.72, 64),
      new THREE.MeshBasicMaterial({ color: ACCENT, transparent: true, opacity: 0.5, side: THREE.DoubleSide })
    );
    foot.rotation.x = -Math.PI / 2;
    foot.position.y = 0.01;
    foot.userData.pulseRing = true;
    robotGroup.add(foot);

    // pelvis / torso stack sized to envelope
    robotGroup.add(makeBox(K1_W * 0.70, 0.09, K1_D * 0.92, dark, 0, 0.46, 0));
    robotGroup.add(makeBox(K1_W * 0.62, 0.30, K1_D * 0.82, white, 0, 0.66, 0));
    robotGroup.add(makeBox(K1_W * 0.38, 0.018, 0.012, led, 0, 0.72, K1_D * 0.42));
    robotGroup.add(makeBox(0.11, 0.10, 0.10, white, 0, 0.88, 0.01));
    robotGroup.add(makeBox(0.07, 0.028, 0.018, led, 0, 0.90, 0.065));
    robotGroup.add(makeBox(K1_W * 0.92, 0.045, 0.055, dark, 0, 0.78, 0));

    [-1, 1].forEach(function (s) {
      robotGroup.add(makeBox(0.048, 0.24, 0.048, white, s * K1_W * 0.40, 0.62, 0));
      robotGroup.add(makeBox(0.042, 0.20, 0.042, dark, s * K1_W * 0.40, 0.42, 0.015));
      robotGroup.add(makeBox(0.078, 0.24, 0.085, white, s * 0.085, 0.30, 0));
      robotGroup.add(makeBox(0.068, 0.22, 0.075, dark, s * 0.085, 0.11, 0.01));
      robotGroup.add(makeBox(0.09, 0.035, 0.14, dark, s * 0.085, 0.02, 0.02));
    });

    var nose = new THREE.Mesh(new THREE.ConeGeometry(0.04, 0.11, 3), led);
    nose.rotation.x = Math.PI / 2;
    nose.position.set(0, 0.58, K1_D * 0.55);
    robotGroup.add(nose);
    robotGroup.visible = layers.robot;
  }

  // ---- Environments -------------------------------------------------------
  function addFloor(size, repeat) {
    var map = texRepeat(floorTex, repeat, repeat);
    var m = map
      ? new THREE.MeshStandardMaterial({ map: map, color: 0xd8d8d8, metalness: 0.04, roughness: 0.88 })
      : stdMat(0x1a1a1c, { metalness: 0.05, roughness: 0.9 });
    var floor = new THREE.Mesh(new THREE.PlaneGeometry(size, size), m);
    floor.rotation.x = -Math.PI / 2;
    floor.receiveShadow = true;
    envGroup.add(floor);

    var lineMat = new THREE.MeshBasicMaterial({ color: SAFETY });
    return function stripe(x, z, w, d) {
      var s = new THREE.Mesh(new THREE.PlaneGeometry(w, d), lineMat);
      s.rotation.x = -Math.PI / 2;
      s.position.set(x, 0.01, z);
      envGroup.add(s);
    };
  }

  function metalMat(tint) {
    if (metalTex) {
      return new THREE.MeshStandardMaterial({
        map: texRepeat(metalTex, 1.2, 2.4) || metalTex,
        color: tint != null ? tint : 0x9aa7b5,
        metalness: 0.72,
        roughness: 0.32
      });
    }
    return stdMat(tint != null ? tint : RACK, { metalness: 0.62, roughness: 0.34 });
  }

  function rackBay(x, z, len, depth, levels) {
    var upright = metalMat(0x8a96a4);
    var shelf = stdMat(0x3a4048, { metalness: 0.35, roughness: 0.52 });
    var beam = stdMat(SAFETY, { metalness: 0.4, roughness: 0.42, emissive: SAFETY, emissiveIntensity: 0.06 });
    var brace = stdMat(0x2e343c, { metalness: 0.5, roughness: 0.4 });
    var h = 2.75;
    var corners = [
      [-depth / 2, -len / 2], [-depth / 2, len / 2],
      [depth / 2, -len / 2], [depth / 2, len / 2]
    ];
    corners.forEach(function (p) {
      envGroup.add(makeBox(0.07, h, 0.07, upright, x + p[0], h / 2, z + p[1]));
    });
    // X-bracing on outer faces
    for (var b = 0; b < 3; b++) {
      var bz = z - len / 2 + (b + 0.5) * (len / 3);
      envGroup.add(makeBox(0.03, h * 0.85, 0.03, brace, x - depth / 2, h * 0.48, bz));
      envGroup.add(makeBox(0.03, h * 0.85, 0.03, brace, x + depth / 2, h * 0.48, bz));
    }
    for (var i = 0; i < levels; i++) {
      var y = 0.32 + i * (h - 0.45) / Math.max(levels - 1, 1);
      envGroup.add(makeBox(depth, 0.045, len, shelf, x, y, z));
      envGroup.add(makeBox(0.04, 0.05, len, beam, x - depth / 2 - 0.02, y, z));
      envGroup.add(makeBox(0.04, 0.05, len, beam, x + depth / 2 + 0.02, y, z));
      var cartons = [0x6e5b45, 0x5a4e40, 0x7a6550, 0x4a5560, 0x8a7358];
      for (var k = -2; k <= 2; k++) {
        if ((i + k + 5) % 2 === 0) continue;
        var carton = stdMat(cartons[(i + k + 5) % cartons.length], { metalness: 0.02, roughness: 0.82 });
        var ch = 0.28 + (Math.abs(k) % 3) * 0.07;
        envGroup.add(makeBox(depth * 0.72, ch, len * 0.16, carton, x, y + ch / 2 + 0.02, z + k * len * 0.18));
      }
    }
  }

  function buildWarehouse() {
    clearGroup(envGroup);
    var stripe = addFloor(32, 12);
    // center aisle safety lanes
    stripe(0, 0, 0.14, 18);
    stripe(-0.42, 0, 0.05, 18);
    stripe(0.42, 0, 0.05, 18);
    stripe(0, 0, 14, 0.12);
    stripe(0, 4.2, 14, 0.08);
    stripe(0, -4.2, 14, 0.08);
    // hazard chevrons near dock
    stripe(-1.1, -8.2, 0.9, 0.08);
    stripe(1.1, -8.2, 0.9, 0.08);

    rackBay(-3.15, 0, 11, 1.15, 5);
    rackBay(3.15, 0, 11, 1.15, 5);
    rackBay(-3.15, 8.2, 4.2, 1.15, 4);
    rackBay(3.15, 8.2, 4.2, 1.15, 4);

    var col = metalMat(0x6d7784);
    [[-7, -7], [-7, 7], [7, -7], [7, 7], [0, 10.5], [0, -9]].forEach(function (p) {
      envGroup.add(makeBox(0.38, 4.4, 0.38, col, p[0], 2.2, p[1]));
    });

    var wallMap = texRepeat(plasterTex || concreteTex, 4, 1.2);
    var wall = wallMap
      ? new THREE.MeshStandardMaterial({
          map: wallMap,
          color: 0xb0b4b8,
          metalness: 0.05,
          roughness: concreteRough ? 0.85 : 0.9,
          roughnessMap: concreteRough || null
        })
      : stdMat(0x14171b, { metalness: 0.08, roughness: 0.92 });
    envGroup.add(makeBox(20, 4.4, 0.22, wall, 0, 2.2, -9.5));
    envGroup.add(makeBox(0.22, 4.4, 22, wall, -9.5, 2.2, 0));
    envGroup.add(makeBox(0.22, 4.4, 22, wall, 9.5, 2.2, 0));

    // dock door
    var door = stdMat(0x1a222a, { metalness: 0.35, roughness: 0.45 });
    envGroup.add(makeBox(3.4, 3.2, 0.12, door, 0, 1.6, -9.35));
    envGroup.add(makeBox(3.6, 0.12, 0.18, metalMat(0x8899aa), 0, 3.25, -9.32));
    envGroup.add(makeBox(2.6, 0.28, 0.06, stdMat(ACCENT, { emissive: ACCENT, emissiveIntensity: 0.35 }), 0, 3.55, -9.2));

    // skylights + soft light panes
    var sky = stdMat(0xb7d0ea, { metalness: 0, roughness: 1, emissive: 0x88aacc, emissiveIntensity: 0.42 });
    [-4, -1.3, 1.3, 4].forEach(function (x) {
      envGroup.add(makeBox(1.35, 0.06, 16, sky, x, 4.35, 0));
    });
    // overhead joists
    var joist = metalMat(0x707986);
    for (var jz = -7; jz <= 8; jz += 3) {
      envGroup.add(makeBox(18, 0.18, 0.18, joist, 0, 4.15, jz));
    }

    // bollards
    var bollard = stdMat(SAFETY, { metalness: 0.3, roughness: 0.4, emissive: SAFETY, emissiveIntensity: 0.05 });
    [[-1.8, -8.0], [1.8, -8.0], [-2.2, 4.2], [2.2, 4.2]].forEach(function (p) {
      envGroup.add(makeBox(0.16, 0.55, 0.16, bollard, p[0], 0.28, p[1]));
    });

    // Free library props (Poly Haven / Kenney / authored pallet)
    placeGltfClone('lib-pallet', -1.6, 0, 6.5, 1.0, 0);
    placeGltfClone('ph-box', -1.55, 0.12, 6.55, 1.0, 0.2);
    placeGltfClone('lib-pallet', 1.7, 0, 6.2, 1.0, -0.15);
    placeGltfClone('ph-crate', 1.7, 0.12, 6.2, 1.0, -0.1);
    placeGltfClone('lib-pallet', -1.5, 0, -6.0, 1.0, 0.1);
    placeGltfClone('ph-box', -1.45, 0.12, -5.95, 0.95, 0.35);
    placeGltfClone('lib-cone', 0.5, 0, -7.5, 1.1, 0);
    placeGltfClone('lib-barrier', -7.2, 0, -8.4, 1.0, Math.PI / 2);
    placeGltfClone('lib-barrier', 7.2, 0, -8.4, 1.0, -Math.PI / 2);
    placeGltfClone('lib-handtruck', 2.0, 0, -4.0, 1.0, 0.5);
    placeGltfClone('ph-plastic', 2.4, 0, -3.5, 1.0, 0.2);
    placeGltfClone('lib-shelves', -8.6, 0, 2.5, 1.0, Math.PI / 2);
    placeGltfClone('ph-rack', 8.6, 0, -1.5, 1.0, -Math.PI / 2);
    placeGltfClone('lib-barrel', -6.8, 0, 5.5, 1.0, 0.2);
    placeGltfClone('lib-barrel', -6.2, 0, 5.8, 1.0, -0.3);
    placeGltfClone('lib-shutter', 0, 0, -9.55, 1.05, 0);
    placeGltfClone('lib-light', -3.5, 4.0, 0, 1.0, 0);
    placeGltfClone('lib-light', 3.5, 4.0, 0, 1.0, 0);
    placeGltfClone('lib-wet', 1.2, 0, -7.2, 1.0, 0.15);

    envGroup.visible = layers.env;
  }

  function buildKitchen() {
    clearGroup(envGroup);
    // Prefer wood floor when available
    var floorMap = texRepeat(woodTex || floorTex, 8, 8);
    if (floorMap) {
      var floor = new THREE.Mesh(
        new THREE.PlaneGeometry(16, 16),
        new THREE.MeshStandardMaterial({ map: floorMap, color: 0xc8b8a0, metalness: 0.04, roughness: 0.85 })
      );
      floor.rotation.x = -Math.PI / 2;
      floor.receiveShadow = true;
      envGroup.add(floor);
      var grid = new THREE.GridHelper(16, 16, 0x2a2a2e, 0x1a1a1c);
      grid.position.y = 0.002;
      envGroup.add(grid);
    } else {
      addFloor(16, 6);
    }
    var cab = stdMat(0x2a2a2e, { metalness: 0.22, roughness: 0.52 });
    var counter = stdMat(0xd8d4cc, { metalness: 0.12, roughness: 0.42 });
    // Island counter + back run (architecture); free GLTF props drop in on top
    envGroup.add(makeBox(1.8, 0.9, 0.9, cab, 0, 0.45, 0.5));
    envGroup.add(makeBox(1.9, 0.04, 1.0, counter, 0, 0.92, 0.5));
    envGroup.add(makeBox(4.5, 0.9, 0.6, cab, 0, 0.45, -2.8));
    envGroup.add(makeBox(4.6, 0.04, 0.68, counter, 0, 0.92, -2.8));
    envGroup.add(makeBox(0.6, 0.9, 3.2, cab, -2.6, 0.45, -0.8));
    envGroup.add(makeBox(0.6, 0.9, 3.2, cab, 2.6, 0.45, -0.8));
    // Procedural fridge (no free CC0 fridge mesh in catalog)
    envGroup.add(makeBox(0.7, 1.8, 0.7, stdMat(0xe8e8ea, { metalness: 0.55, roughness: 0.28 }), -2.5, 0.9, 1.6));
    envGroup.add(makeBox(0.02, 0.7, 0.55, stdMat(0x8a96a4, { metalness: 0.7, roughness: 0.3 }), -2.14, 1.35, 1.6));
    envGroup.add(makeBox(0.02, 0.65, 0.55, stdMat(0x8a96a4, { metalness: 0.7, roughness: 0.3 }), -2.14, 0.55, 1.6));
    // Doorway
    envGroup.add(makeBox(0.08, 2.1, 0.08, cab, 2.8, 1.05, 2.4));
    envGroup.add(makeBox(0.08, 2.1, 0.08, cab, 3.8, 1.05, 2.4));
    envGroup.add(makeBox(1.1, 0.08, 0.08, cab, 3.3, 2.1, 2.4));
    // Soft ceiling wash
    var wash = stdMat(0xf2f2f7, { emissive: 0xd8e8f8, emissiveIntensity: 0.35, roughness: 1, metalness: 0 });
    envGroup.add(makeBox(1.2, 0.05, 1.2, wash, 0, 2.6, 0));
    envGroup.add(makeBox(1.2, 0.05, 1.2, wash, -2, 2.6, -1.5));
    envGroup.add(makeBox(1.2, 0.05, 1.2, wash, 2, 2.6, -1.2));

    // Free Poly Haven kitchen props (catalog library/)
    placeGltfClone('kit-stove', 1.35, 0, -2.45, 1.0, 0);
    placeGltfClone('kit-cabinet', 2.35, 0, -0.7, 1.0, Math.PI / 2);
    placeGltfClone('kit-table', 0.15, 0, -1.05, 1.0, 0.15);
    placeGltfClone('kit-chair', -0.55, 0, -0.95, 1.0, 0.4);
    placeGltfClone('kit-chair', 0.85, 0, -1.25, 1.0, -0.35);
    placeGltfClone('kit-stool', -1.1, 0, 0.35, 1.0, 0.2);
    placeGltfClone('kit-microwave', -2.2, 0.95, -2.55, 1.0, 0);
    placeGltfClone('kit-trash', 2.15, 0, 1.35, 1.0, 0.2);
    placeGltfClone('lib-wet', 3.1, 0, 1.8, 1.0, -0.4);
    placeGltfClone('lib-light', 0, 2.45, 0.2, 1.0, 0);

    envGroup.visible = layers.env;
  }

  function woodMat(tint) {
    var map = texRepeat(woodTex, 1.4, 1.4);
    if (map) {
      return new THREE.MeshStandardMaterial({
        map: map, color: tint != null ? tint : 0xc4a574, metalness: 0.04, roughness: 0.78
      });
    }
    return stdMat(tint != null ? tint : 0x5a4632, { metalness: 0.05, roughness: 0.75 });
  }

  function cardboardMat(tint) {
    var map = texRepeat(cardboardTex, 1.1, 1.1);
    if (map) {
      return new THREE.MeshStandardMaterial({
        map: map, color: tint != null ? tint : 0xd2b48c, metalness: 0.02, roughness: 0.88
      });
    }
    return stdMat(tint != null ? tint : 0x6e5b45, { metalness: 0.02, roughness: 0.82 });
  }

  function palletStack(x, z, layers, wrapped) {
    var wood = woodMat(0xb0895a);
    var load = wrapped
      ? stdMat(0xc8d0d8, { metalness: 0.15, roughness: 0.35, transparent: true, opacity: 0.92 })
      : cardboardMat(0xc4a06a);
    envGroup.add(makeBox(1.05, 0.12, 1.05, wood, x, 0.06, z));
    var y = 0.12;
    for (var i = 0; i < layers; i++) {
      var h = 0.28 + (i % 3) * 0.06;
      envGroup.add(makeBox(0.92, h, 0.92, load, x, y + h / 2, z));
      y += h;
      if (!wrapped && i < layers - 1) {
        envGroup.add(makeBox(1.0, 0.04, 1.0, wood, x, y + 0.02, z));
        y += 0.04;
      }
    }
    if (wrapped) {
      envGroup.add(makeBox(0.96, Math.max(0.02, y - 0.12), 0.02, stdMat(ACCENT, {
        emissive: ACCENT, emissiveIntensity: 0.2, transparent: true, opacity: 0.55
      }), x, 0.12 + (y - 0.12) / 2, z + 0.47));
    }
  }

  function conveyorSection(x, z, len, rotY) {
    var frame = metalMat(0x6a7380);
    var belt = stdMat(0x1a1c1e, { metalness: 0.2, roughness: 0.55 });
    var stripe = stdMat(SAFETY, { metalness: 0.25, roughness: 0.45, emissive: SAFETY, emissiveIntensity: 0.08 });
    var g = new THREE.Group();
    g.add(makeBox(0.9, 0.08, len, belt, 0, 0.55, 0));
    g.add(makeBox(0.08, 0.55, len, frame, -0.48, 0.28, 0));
    g.add(makeBox(0.08, 0.55, len, frame, 0.48, 0.28, 0));
    g.add(makeBox(0.92, 0.02, 0.08, stripe, 0, 0.6, -len * 0.22));
    g.add(makeBox(0.92, 0.02, 0.08, stripe, 0, 0.6, len * 0.22));
    // legs
    [[-0.4, -len * 0.4], [0.4, -len * 0.4], [-0.4, len * 0.4], [0.4, len * 0.4]].forEach(function (p) {
      g.add(makeBox(0.06, 0.5, 0.06, frame, p[0], 0.25, p[1]));
    });
    // boxes riding the belt
    g.add(makeBox(0.35, 0.22, 0.35, cardboardMat(0xc9a66b), 0.05, 0.72, -len * 0.15));
    g.add(makeBox(0.28, 0.18, 0.42, cardboardMat(0xa88858), -0.1, 0.7, len * 0.18));
    g.position.set(x, 0, z);
    if (rotY) g.rotation.y = rotY;
    envGroup.add(g);
  }

  function dockDoor(x, z, w, open) {
    var frame = metalMat(0x8899aa);
    var panelMap = texRepeat(shutterTex || concreteTex, 1.2, 2.2);
    var panel = panelMap
      ? new THREE.MeshStandardMaterial({ map: panelMap, color: 0x8a929a, metalness: 0.55, roughness: 0.4 })
      : stdMat(0x1a222a, { metalness: 0.45, roughness: 0.4 });
    envGroup.add(makeBox(w + 0.35, 0.18, 0.22, frame, x, 3.35, z));
    envGroup.add(makeBox(0.16, 3.2, 0.22, frame, x - w / 2 - 0.08, 1.6, z));
    envGroup.add(makeBox(0.16, 3.2, 0.22, frame, x + w / 2 + 0.08, 1.6, z));
    var doorH = open ? 1.1 : 3.05;
    var doorY = open ? 2.7 : 1.55;
    envGroup.add(makeBox(w, doorH, 0.1, panel, x, doorY, z + 0.02));
    // cyan bay label
    envGroup.add(makeBox(w * 0.55, 0.22, 0.05, stdMat(ACCENT, {
      emissive: ACCENT, emissiveIntensity: 0.45
    }), x, 3.55, z + 0.12));
  }

  function forkliftProxy(x, z, yaw) {
    var body = stdMat(0xe0a820, { metalness: 0.35, roughness: 0.42 });
    var dark = stdMat(0x1c1c1e, { metalness: 0.4, roughness: 0.35 });
    var g = new THREE.Group();
    g.add(makeBox(0.95, 0.55, 1.55, body, 0, 0.55, 0));
    g.add(makeBox(0.85, 0.35, 0.55, dark, 0, 1.0, -0.35));
    g.add(makeBox(0.08, 1.4, 0.08, metalMat(0x9aa7b5), -0.28, 1.1, 0.85));
    g.add(makeBox(0.08, 1.4, 0.08, metalMat(0x9aa7b5), 0.28, 1.1, 0.85));
    g.add(makeBox(0.7, 0.05, 0.9, metalMat(0xb0bac4), 0, 0.35, 1.15));
    g.add(makeBox(0.22, 0.22, 0.12, dark, -0.4, 0.22, 0.4));
    g.add(makeBox(0.22, 0.22, 0.12, dark, 0.4, 0.22, 0.4));
    g.add(makeBox(0.22, 0.22, 0.12, dark, -0.4, 0.22, -0.5));
    g.add(makeBox(0.22, 0.22, 0.12, dark, 0.4, 0.22, -0.5));
    g.position.set(x, 0, z);
    g.rotation.y = yaw || 0;
    envGroup.add(g);
  }

  function baySign(x, y, z, labelW) {
    envGroup.add(makeBox(labelW || 1.4, 0.32, 0.06, stdMat(0x0a0a0c, { metalness: 0.2, roughness: 0.5 }), x, y, z));
    envGroup.add(makeBox((labelW || 1.4) * 0.72, 0.14, 0.04, stdMat(ACCENT, {
      emissive: ACCENT, emissiveIntensity: 0.55
    }), x, y, z + 0.04));
  }

  function placeGltfClone(name, x, y, z, scale, rotY) {
    var src = gltfCache[name];
    if (!src) return;
    var root = new THREE.Group();
    var clone = src.clone(true);
    root.add(clone);
    root.scale.setScalar(scale || 1);
    if (rotY) root.rotation.y = rotY;
    root.position.set(x, y || 0, z);
    root.traverse(function (o) {
      if (o.isMesh) { o.castShadow = true; o.receiveShadow = true; }
    });
    // Ground to floor unless caller passed an explicit elevated Y (e.g. microwave on counter)
    if (y == null || y === 0) {
      var box3 = new THREE.Box3().setFromObject(root);
      if (isFinite(box3.min.y)) root.position.y -= box3.min.y;
    }
    envGroup.add(root);
  }

  function buildDistributionHub() {
    clearGroup(envGroup);
    var floorMap = texRepeat(antiSlipTex || floorTex, 14, 14);
    var floorMat = floorMap
      ? new THREE.MeshStandardMaterial({ map: floorMap, color: 0xc8c8c8, metalness: 0.04, roughness: 0.9 })
      : stdMat(0x1a1a1c, { metalness: 0.05, roughness: 0.9 });
    var floor = new THREE.Mesh(new THREE.PlaneGeometry(36, 36), floorMat);
    floor.rotation.x = -Math.PI / 2;
    floor.receiveShadow = true;
    envGroup.add(floor);

    var lineMat = new THREE.MeshBasicMaterial({ color: SAFETY });
    function stripe(x, z, w, d) {
      var s = new THREE.Mesh(new THREE.PlaneGeometry(w, d), lineMat);
      s.rotation.x = -Math.PI / 2;
      s.position.set(x, 0.012, z);
      envGroup.add(s);
    }
    // Multi-aisle floor markings (aisles at x=0, ±3.8)
    [-3.8, 0, 3.8].forEach(function (ax) {
      stripe(ax, 0, 0.12, 20);
      stripe(ax - 0.55, 0, 0.05, 20);
      stripe(ax + 0.55, 0, 0.05, 20);
    });
    stripe(0, 0, 22, 0.1);
    stripe(0, 5.5, 22, 0.08);
    stripe(0, -5.5, 22, 0.08);
    // dock apron chevrons
    for (var cx = -7; cx <= 7; cx += 2) {
      stripe(cx, -9.0, 0.7, 0.08);
      stripe(cx + 0.35, -8.6, 0.7, 0.08);
    }

    // Four rack rows → three travel aisles
    rackBay(-5.85, 0.5, 14, 1.2, 5);
    rackBay(-2.05, 0.5, 14, 1.2, 5);
    rackBay(2.05, 0.5, 14, 1.2, 5);
    rackBay(5.85, 0.5, 14, 1.2, 5);
    // End-cap racks at north
    rackBay(-4.0, 9.2, 3.8, 1.15, 4);
    rackBay(4.0, 9.2, 3.8, 1.15, 4);

    // Corrugated / concrete perimeter
    var wallMap = texRepeat(concreteTex || plasterTex, 6, 1.4);
    var wall = wallMap
      ? new THREE.MeshStandardMaterial({
          map: wallMap, color: 0x9aa0a6, metalness: 0.18, roughness: 0.72
        })
      : stdMat(0x14171b, { metalness: 0.08, roughness: 0.92 });
    envGroup.add(makeBox(28, 5.0, 0.28, wall, 0, 2.5, -10.4));
    envGroup.add(makeBox(0.28, 5.0, 28, wall, -11.5, 2.5, 0));
    envGroup.add(makeBox(0.28, 5.0, 28, wall, 11.5, 2.5, 0));
    envGroup.add(makeBox(28, 5.0, 0.28, wall, 0, 2.5, 11.5));

    // Loading dock — three roll-up doors
    dockDoor(-5.2, -10.2, 3.2, true);
    dockDoor(0, -10.2, 3.4, false);
    dockDoor(5.2, -10.2, 3.2, true);

    // Structural columns
    var col = metalMat(0x6d7784);
    [[-9, -8], [-9, 0], [-9, 8], [9, -8], [9, 0], [9, 8], [0, 10.8], [-4, -9.5], [4, -9.5]].forEach(function (p) {
      envGroup.add(makeBox(0.42, 5.0, 0.42, col, p[0], 2.5, p[1]));
    });

    // Skylights + industrial light bars
    var sky = stdMat(0xb7d0ea, { metalness: 0, roughness: 1, emissive: 0x88aacc, emissiveIntensity: 0.48 });
    [-6, -2, 2, 6].forEach(function (x) {
      envGroup.add(makeBox(1.5, 0.06, 18, sky, x, 4.95, 0));
    });
    var joist = metalMat(0x707986);
    for (var jz = -8; jz <= 9; jz += 2.5) {
      envGroup.add(makeBox(22, 0.16, 0.16, joist, 0, 4.75, jz));
      // pendant light housings
      [-6, 0, 6].forEach(function (lx) {
        envGroup.add(makeBox(0.55, 0.08, 0.55, stdMat(0x222428, { metalness: 0.5, roughness: 0.35 }), lx, 4.55, jz));
        envGroup.add(makeBox(0.45, 0.04, 0.45, stdMat(0xe8f4ff, {
          emissive: 0xa8d4ff, emissiveIntensity: 0.7
        }), lx, 4.5, jz));
      });
    }

    // Conveyor run along east wall
    conveyorSection(8.4, -4.5, 3.2, 0);
    conveyorSection(8.4, -1.0, 3.2, 0);
    conveyorSection(8.4, 2.5, 3.2, 0);
    conveyorSection(8.4, 6.0, 2.6, 0);
    conveyorSection(7.2, 8.4, 2.2, Math.PI / 2);

    // Staging pallets + shrink-wrap loads
    palletStack(-3.2, -7.4, 3, true);
    palletStack(-1.6, -7.6, 2, false);
    palletStack(3.4, -7.2, 4, true);
    palletStack(5.0, -7.5, 2, false);
    palletStack(-7.2, 4.5, 3, false);
    palletStack(-7.0, 6.2, 2, true);
    palletStack(7.0, -7.8, 3, false);
    palletStack(-4.6, 8.4, 2, false);
    palletStack(4.4, 8.5, 3, true);

    // Safety bollards + barriers at dock
    var bollard = stdMat(SAFETY, { metalness: 0.3, roughness: 0.4, emissive: SAFETY, emissiveIntensity: 0.08 });
    [[-7.2, -8.6], [-3.2, -8.6], [3.2, -8.6], [7.2, -8.6], [-1.0, 4.8], [1.0, 4.8], [-4.6, -2], [4.6, -2]].forEach(function (p) {
      envGroup.add(makeBox(0.18, 0.62, 0.18, bollard, p[0], 0.31, p[1]));
    });
    // low barrier rails
    var rail = stdMat(SAFETY, { metalness: 0.35, roughness: 0.4 });
    envGroup.add(makeBox(0.08, 0.08, 4.5, rail, -8.6, 0.55, -6));
    envGroup.add(makeBox(0.08, 0.08, 4.5, rail, 8.6, 0.55, -6));

    // Safety cones (procedural + Kenney if loaded)
    var coneMat = stdMat(0xe07020, { metalness: 0.15, roughness: 0.55, emissive: 0xe07020, emissiveIntensity: 0.12 });
    [[-2.4, -8.2], [2.4, -8.2], [6.5, 1.0], [-6.5, 7.5]].forEach(function (p) {
      var cone = new THREE.Mesh(new THREE.ConeGeometry(0.14, 0.45, 10), coneMat);
      cone.position.set(p[0], 0.22, p[1]);
      cone.castShadow = true;
      envGroup.add(cone);
    });

    // Overhead bay labels
    baySign(-5.2, 3.85, -9.9, 1.6);
    baySign(0, 3.85, -9.9, 1.8);
    baySign(5.2, 3.85, -9.9, 1.6);
    baySign(-3.8, 3.2, 0, 1.1);
    baySign(3.8, 3.2, 0, 1.1);

    // Forklift near open dock
    forkliftProxy(-5.0, -8.0, 0.35);
    forkliftProxy(6.2, -6.5, -1.1);

    // Hand-truck / pallet-jack proxies
    var jack = metalMat(0x4a5560);
    [[-0.8, -6.8], [2.2, 6.5]].forEach(function (p) {
      envGroup.add(makeBox(0.55, 0.08, 1.1, jack, p[0], 0.12, p[1]));
      envGroup.add(makeBox(0.08, 0.55, 0.08, jack, p[0] - 0.2, 0.4, p[1] - 0.45));
      envGroup.add(makeBox(0.45, 0.05, 0.05, jack, p[0], 0.7, p[1] - 0.45));
    });

    // Tote bins near conveyor
    var tote = stdMat(0x2a6a8a, { metalness: 0.15, roughness: 0.55 });
    [[7.6, -2.5], [7.6, 0.5], [7.6, 3.5]].forEach(function (p) {
      envGroup.add(makeBox(0.55, 0.35, 0.4, tote, p[0], 0.18, p[1]));
    });

    // Optional Kenney / Poly Haven glTF accents (loaded async into cache)
    placeGltfClone('box-large', -6.8, 0, -5.5, 1.8, 0.2);
    placeGltfClone('box-wide', 6.8, 0, 4.2, 1.6, -0.4);
    placeGltfClone('cone', -2.4, 0, -8.2, 1.2, 0);
    placeGltfClone('conveyor-long', 8.4, 0, -4.5, 1.0, 0);
    placeGltfClone('ph-box', -2.8, 0, -6.8, 1.0, 0.3);
    placeGltfClone('ph-crate', 2.8, 0, -6.6, 1.0, -0.2);
    placeGltfClone('lib-pallet', -3.2, 0, -7.4, 1.0, 0.15);
    placeGltfClone('lib-pallet', 3.4, 0, -7.2, 1.0, -0.2);
    placeGltfClone('lib-barrier', -8.2, 0, -8.8, 1.0, Math.PI / 2);
    placeGltfClone('lib-crush', 8.2, 0, -8.5, 1.0, -Math.PI / 2);
    placeGltfClone('lib-hand-truck', -0.8, 0, -6.8, 1.0, 0.4);
    placeGltfClone('ph-plastic', 7.4, 0, -0.8, 1.0, 0.5);
    placeGltfClone('ph-tote', 7.5, 0, 2.2, 1.0, 0);
    placeGltfClone('ph-rack', -8.6, 0, 2.0, 1.0, Math.PI / 2);
    placeGltfClone('lib-barrier', -7.5, 0, -8.8, 1.0, 0.1);
    placeGltfClone('lib-barrier', 7.5, 0, -8.8, 1.0, -0.1);
    placeGltfClone('lib-cone', 2.4, 0, -8.2, 1.0, 0);
    placeGltfClone('lib-crush', -8.8, 0, -4.0, 1.0, Math.PI / 2);
    placeGltfClone('lib-block', 8.8, 0, -6.5, 1.0, 0);
    placeGltfClone('lib-handtruck', -0.9, 0, -6.5, 1.0, 0.4);
    placeGltfClone('lib-shelves', -9.2, 0, 6.5, 1.0, Math.PI / 2);
    placeGltfClone('lib-barrel', -7.4, 0, 5.2, 1.0, 0.2);
    placeGltfClone('lib-barrel', -6.8, 0, 5.5, 1.0, -0.3);
    placeGltfClone('lib-shutter', 0, 0, -10.15, 1.15, 0);
    placeGltfClone('lib-light', -3.8, 3.8, 0, 1.0, 0);
    placeGltfClone('lib-light', 3.8, 3.8, 0, 1.0, 0);
    placeGltfClone('lib-wet', 1.2, 0, -7.5, 1.0, 0.15);

    envGroup.visible = layers.env;
  }

  function buildEnvironmentFor(domainId) {
    var id = String(domainId || '').toLowerCase();
    if (id.indexOf('kitchen') >= 0) buildKitchen();
    else if (id.indexOf('distribution') >= 0 || id.indexOf('hub') >= 0) buildDistributionHub();
    else if (id.indexOf('patio') >= 0 || id.indexOf('outdoor') >= 0) buildDistributionHub(); // legacy ids
    else buildWarehouse();
  }

  function preloadHubGltf() {
    if (!gltfLoader) return Promise.resolve();
    var jobs = [
      ['box-large', './assets/distribution-hub/kenney/box-large.glb'],
      ['box-wide', './assets/distribution-hub/kenney/box-wide.glb'],
      ['cone', './assets/distribution-hub/kenney/cone.glb'],
      ['conveyor-long', './assets/distribution-hub/kenney/conveyor-long.glb'],
      ['door-wide-open', './assets/distribution-hub/kenney/door-wide-open.glb'],
      ['ph-box', './assets/distribution-hub/polyhaven/cardboard_box_01/cardboard_box_01_1k.gltf'],
      ['ph-crate', './assets/distribution-hub/polyhaven/wooden_crate_01/wooden_crate_01_1k.gltf'],
      ['ph-plastic', './assets/distribution-hub/polyhaven/plastic_crate_01/plastic_crate_01_1k.gltf'],
      ['ph-tote', './assets/distribution-hub/polyhaven/industrial_pastic_container/industrial_pastic_container_1k.gltf'],
      ['ph-rack', './assets/distribution-hub/polyhaven/worn_metal_rack/worn_metal_rack_1k.gltf'],
      ['lib-barrier', './assets/library/warehouse/concrete_road_barrier/concrete_road_barrier_1k.gltf'],
      ['lib-cone', './assets/library/cross-domain/traffic/Traffic_Cone.glb'],
      ['lib-crush', './assets/library/cross-domain/traffic/Crush_Barrier.glb'],
      ['lib-block', './assets/library/cross-domain/traffic/Road_Block.glb'],
      ['lib-handtruck', './assets/library/warehouse/hand_truck/hand_truck_1k.gltf'],
      ['lib-hand-truck', './assets/library/warehouse/hand_truck/hand_truck_1k.gltf'],
      ['lib-shelves', './assets/library/warehouse/steel_frame_shelves_01/steel_frame_shelves_01_1k.gltf'],
      ['lib-barrel', './assets/library/warehouse/Barrel_01/Barrel_01_1k.gltf'],
      ['lib-shutter', './assets/library/warehouse/rollershutter_door/rollershutter_door_1k.gltf'],
      ['lib-light', './assets/library/cross-domain/caged_hanging_light/caged_hanging_light_1k.gltf'],
      ['lib-wet', './assets/library/cross-domain/WetFloorSign_01/WetFloorSign_01_1k.gltf'],
      ['lib-pallet', './assets/library/warehouse/wooden_pallet_cc0/wooden_pallet_cc0.gltf'],
      ['kit-stove', './assets/library/kitchen/electric_stove/electric_stove_1k.gltf'],
      ['kit-cabinet', './assets/library/kitchen/drawer_cabinet/drawer_cabinet_1k.gltf'],
      ['kit-table', './assets/library/kitchen/dining_table/dining_table_1k.gltf'],
      ['kit-chair', './assets/library/kitchen/dining_chair_02/dining_chair_02_1k.gltf'],
      ['kit-stool', './assets/library/kitchen/wooden_stool_01/wooden_stool_01_1k.gltf'],
      ['kit-microwave', './assets/library/kitchen/vintage_microwave/vintage_microwave_1k.gltf'],
      ['kit-trash', './assets/library/kitchen/trashbag/trashbag_1k.gltf']
    ];
    return Promise.all(jobs.map(function (pair) {
      return new Promise(function (resolve) {
        gltfLoader.load(pair[1], function (gltf) {
          gltfCache[pair[0]] = gltf.scene;
          resolve(pair[0]);
        }, undefined, function () { resolve(null); });
      });
    })).then(function () {
      // Rebuild active domain so free meshes appear after async load
      if (activeDomainId) buildEnvironmentFor(activeDomainId);
    });
  }

  // ---- Occupancy (subtle structural hits — not toy cyan pillars) ----------
  function clearCells() { clearGroup(cellGroup); }

  function setOccupancy(data) {
    clearCells();
    if (!data) return;
    var res = data.res_m || 0.08;
    var cells = data.cells || [];
    var maxHits = 1;
    for (var i = 0; i < cells.length; i++) maxHits = Math.max(maxHits, cells[i].hits || 1);
    var geo = new THREE.BoxGeometry(res * 0.92, 1, res * 0.92);
    for (var j = 0; j < cells.length; j++) {
      var cell = cells[j];
      var hits = cell.hits || 1;
      var t = hits / maxHits;
      var h = 0.06 + 0.55 * Math.pow(t, 0.9);
      var warm = t > 0.75;
      var m = new THREE.MeshStandardMaterial({
        color: warm ? DANGER : OCC_BASE,
        emissive: warm ? DANGER : ACCENT,
        emissiveIntensity: warm ? 0.18 : 0.04 + 0.1 * t,
        metalness: 0.08,
        roughness: 0.55,
        transparent: true,
        opacity: 0.35 + 0.4 * t,
        depthWrite: t > 0.6
      });
      var mesh = new THREE.Mesh(geo, m);
      mesh.position.set(cell.x || 0, h / 2, cell.y || 0);
      mesh.scale.y = h;
      mesh.castShadow = t > 0.5;
      cellGroup.add(mesh);
    }
    cellGroup.visible = layers.occ;
  }

  // ---- Odom trail ---------------------------------------------------------
  function rebuildTrail() {
    clearGroup(trailGroup);
    if (trailPoints.length < 2) { trailGroup.visible = layers.trail; return; }
    var pts = trailPoints.map(function (p) { return new THREE.Vector3(p.x, 0.045, p.y); });
    var geo = new THREE.BufferGeometry().setFromPoints(pts);
    trailGroup.add(new THREE.Line(geo, new THREE.LineBasicMaterial({
      color: ACCENT, transparent: true, opacity: 0.85
    })));
    // soft underglow ribbon
    var ribbon = new THREE.Line(
      new THREE.BufferGeometry().setFromPoints(pts.map(function (p) {
        return new THREE.Vector3(p.x, 0.02, p.z);
      })),
      new THREE.LineBasicMaterial({ color: ACCENT, transparent: true, opacity: 0.28 })
    );
    trailGroup.add(ribbon);

    var dotGeo = new THREE.SphereGeometry(0.028, 10, 10);
    var dotMat = new THREE.MeshBasicMaterial({ color: ACCENT });
    for (var k = 0; k < trailPoints.length; k += 6) {
      var d = new THREE.Mesh(dotGeo, dotMat);
      d.position.set(trailPoints[k].x, 0.055, trailPoints[k].y);
      trailGroup.add(d);
    }
    // heading tip
    var last = trailPoints[trailPoints.length - 1];
    var tip = new THREE.Mesh(
      new THREE.ConeGeometry(0.05, 0.14, 4),
      new THREE.MeshBasicMaterial({ color: ACCENT })
    );
    tip.rotation.x = Math.PI / 2;
    tip.position.set(last.x, 0.07, last.y);
    tip.rotation.z = -(last.yaw || 0);
    trailGroup.add(tip);
    trailGroup.visible = layers.trail;
  }

  function pushTrail(pose) {
    if (!pose) return;
    var x = pose.x || 0, y = pose.y || 0;
    var last = trailPoints[trailPoints.length - 1];
    if (last && Math.hypot(last.x - x, last.y - y) < 0.035) {
      last.yaw = pose.yaw || last.yaw || 0;
      return;
    }
    trailPoints.push({ x: x, y: y, yaw: pose.yaw || 0 });
    if (trailPoints.length > MAX_TRAIL) trailPoints.shift();
    rebuildTrail();
  }

  function setPose(pose, odomExtras) {
    pose = pose || { x: 0, y: 0, yaw: 0 };
    robotGroup.position.set(pose.x || 0, 0, pose.y || 0);
    robotGroup.rotation.y = -(pose.yaw || 0);
    robotGroup.visible = layers.robot;
    poseXEl.textContent = (pose.x || 0).toFixed(2);
    poseYEl.textContent = (pose.y || 0).toFixed(2);
    poseYawEl.textContent = (pose.yaw || 0).toFixed(2);
    poseTrailEl.textContent = String(trailPoints.length);
    if (poseVxEl) {
      var vx = odomExtras && odomExtras.vx != null ? odomExtras.vx : (lastOdom && lastOdom.vx) || 0;
      var vy = odomExtras && odomExtras.vy != null ? odomExtras.vy : (lastOdom && lastOdom.vy) || 0;
      var wz = odomExtras && odomExtras.wz != null ? odomExtras.wz : (lastOdom && lastOdom.wz) || 0;
      poseVxEl.textContent = Number(vx).toFixed(2);
      poseVyEl.textContent = Number(vy).toFixed(2);
      poseWzEl.textContent = Number(wz).toFixed(2);
    }
    if (layers.follow) {
      orbitTarget.set(pose.x || 0, 0.4, pose.y || 0);
      if (controls) {
        controls.target.copy(orbitTarget);
        controls.update();
      } else {
        camera.lookAt(orbitTarget);
      }
    }
  }

  function setMap(data) {
    if (!data) return;
    currentMap = data;
    setOccupancy(data);
    var pose = data.pose || { x: 0, y: 0, yaw: 0 };
    if (data.trail && data.trail.length) {
      trailPoints = data.trail.slice(-MAX_TRAIL);
      rebuildTrail();
    } else {
      pushTrail(pose);
    }
    setPose(pose);
    var res = data.res_m || 0.08;
    var cells = data.cells || [];
    var range = data.range_m || 3.5;
    var meta = findDomain(activeDomainId);
    var bits = cells.length + ' cells · res ' + res.toFixed(2) + ' m · range ' + range.toFixed(1) + ' m';
    if (meta) bits += ' · ' + (meta.run_count || 0) + ' runs · ' + formatUpdated(meta.updated);
    bits += ' · K1 ' + (K1_H * 100).toFixed(0) + '×' + (K1_W * 100).toFixed(0) + '×' + (K1_D * 100).toFixed(0) + ' cm';
    statsEl.textContent = bits;
  }

  // ---- Domains ------------------------------------------------------------
  function occupancyUrl(id) {
    return DATA_ROOT + '/domains/' + encodeURIComponent(id) + '/occupancy.json';
  }

  function renderDomainChips() {
    while (domainBar.firstChild) domainBar.removeChild(domainBar.firstChild);
    registry.domains.forEach(function (d) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'chip' + (d.id === activeDomainId ? ' active' : '');
      btn.title = (d.name || d.id) + ' · ' + (d.cell_count || 0) + ' cells · ' + (d.run_count || 0) + ' runs';
      btn.textContent = d.name || d.id;
      btn.addEventListener('click', function () { switchDomain(d.id); });
      domainBar.appendChild(btn);
    });
    var neu = document.createElement('button');
    neu.type = 'button';
    neu.className = 'chip new';
    neu.textContent = '+ New domain';
    neu.addEventListener('click', openNewDomainModal);
    domainBar.appendChild(neu);
  }

  function switchDomain(id, opts) {
    opts = opts || {};
    if (!id) return Promise.resolve();
    activeDomainId = id;
    registry.active = id;
    trailPoints = [];
    rebuildTrail();
    var meta = findDomain(id);
    domainTitle.textContent = (meta && meta.name) ? meta.name : id;
    renderDomainChips();
    buildEnvironmentFor(id);
    if (typeof window.k1LocalMapOnDomainChange === 'function') {
      try { window.k1LocalMapOnDomainChange(id); } catch (e) {}
    }
    // Label→asset fill-in: load persisted instances for this domain (reruns accumulate)
    if (window.k1LocalMapAssets && window.k1LocalMapAssets.isReady()) {
      try {
        window.k1LocalMapAssets.attachToScene(scene);
        var qAssets = new URLSearchParams(window.location.search);
        var wantDemo = qAssets.get('demo_assets') === '1' || qAssets.get('assets') === 'demo';
        if (wantDemo) {
          window.k1LocalMapAssets.runDemo(id).catch(function () {});
        } else {
          window.k1LocalMapAssets.loadInstancesForDomain(id).catch(function () {});
        }
      } catch (e) {}
    }
    return loadJson(occupancyUrl(id)).then(function (d) {
      showErr('');
      if (!d.domain_id) d.domain_id = id;
      setMap(d);
      return d;
    }).catch(function (e) {
      if (!opts.quiet) showErr('domain: ' + e);
      clearCells();
      statsEl.textContent = 'empty domain — import a run or load sample';
    });
  }

  function setRegistry(reg, opts) {
    opts = opts || {};
    registry = reg || { active: null, domains: [] };
    if (!registry.domains) registry.domains = [];
    var next = opts.forceId || registry.active || (registry.domains[0] && registry.domains[0].id) || null;
    renderDomainChips();
    if (next) return switchDomain(next, opts);
    domainTitle.textContent = 'no domains';
    return Promise.resolve();
  }

  function refreshRegistry() {
    return loadJson(DOMAINS_INDEX).then(function (reg) {
      return setRegistry(reg, { forceId: reg.active });
    });
  }

  function openNewDomainModal() {
    modal.classList.add('open');
    modal.setAttribute('aria-hidden', 'false');
    newNameInput.value = '';
    setTimeout(function () { newNameInput.focus(); }, 30);
  }
  function closeNewDomainModal() {
    modal.classList.remove('open');
    modal.setAttribute('aria-hidden', 'true');
  }
  function slugify(name) {
    return String(name || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 48) || ('domain-' + Date.now());
  }
  function requestNewDomain(name) {
    var n = (name || '').trim();
    if (!n) return;
    var id = slugify(n);
    if (typeof window.k1LocalMapHostCreateDomain === 'function') {
      try {
        window.k1LocalMapHostCreateDomain(JSON.stringify({ id: id, name: n }));
        closeNewDomainModal();
        return;
      } catch (e) {}
    }
    registry.domains.push({ id: id, name: n, updated: new Date().toISOString(), run_count: 0, cell_count: 0 });
    registry.active = id;
    closeNewDomainModal();
    switchDomain(id);
  }

  document.getElementById('new-domain-cancel').addEventListener('click', closeNewDomainModal);
  document.getElementById('new-domain-ok').addEventListener('click', function () { requestNewDomain(newNameInput.value); });
  newNameInput.addEventListener('keydown', function (e) {
    if (e.key === 'Enter') requestNewDomain(newNameInput.value);
    if (e.key === 'Escape') closeNewDomainModal();
  });
  modal.addEventListener('click', function (e) { if (e.target === modal) closeNewDomainModal(); });

  Array.prototype.forEach.call(document.querySelectorAll('#layers .layer'), function (btn) {
    btn.addEventListener('click', function () {
      var id = btn.getAttribute('data-layer');
      layers[id] = !layers[id];
      btn.classList.toggle('on', layers[id]);
      if (id === 'env') envGroup.visible = layers.env;
      if (id === 'occ') cellGroup.visible = layers.occ;
      if (id === 'robot') robotGroup.visible = layers.robot;
      if (id === 'trail') trailGroup.visible = layers.trail;
    });
  });

  function cellKey(c, res) {
    var q = res || 0.08;
    return Math.round((c.x || 0) / q) + ':' + Math.round((c.y || 0) / q);
  }

  function mergeOccupancy(base, incoming) {
    var res = (incoming && incoming.res_m) || (base && base.res_m) || 0.08;
    var out = {
      domain_id: (base && base.domain_id) || (incoming && incoming.domain_id) || activeDomainId,
      res_m: res,
      range_m: Math.max((base && base.range_m) || 0, (incoming && incoming.range_m) || 0, 3.5),
      pose: (incoming && incoming.pose) || (base && base.pose) || { x: 0, y: 0, yaw: 0 },
      trail: (incoming && incoming.trail) || (base && base.trail) || [],
      cells: []
    };
    var map = {};
    function absorb(src) {
      if (!src || !src.cells) return;
      for (var i = 0; i < src.cells.length; i++) {
        var c = src.cells[i];
        var k = cellKey(c, res);
        if (!map[k]) map[k] = { x: c.x || 0, y: c.y || 0, hits: c.hits || 1 };
        else {
          map[k].hits += c.hits || 1;
          map[k].x = (map[k].x + (c.x || 0)) / 2;
          map[k].y = (map[k].y + (c.y || 0)) / 2;
        }
      }
    }
    absorb(base); absorb(incoming);
    Object.keys(map).forEach(function (k) { out.cells.push(map[k]); });
    return out;
  }

  window.k1LocalMap = {
    _scene: scene,
    setMap: setMap,
    clear: function () {
      clearCells();
      trailPoints = [];
      rebuildTrail();
      currentMap = { domain_id: activeDomainId, res_m: 0.08, range_m: 3.5, pose: { x: 0, y: 0, yaw: 0 }, cells: [] };
      statsEl.textContent = 'cleared';
    },
    resetView: function () {
      frameCamera(DEFAULT_RADIUS, DEFAULT_THETA, DEFAULT_PHI,
        new THREE.Vector3(robotGroup.position.x, 0.4, robotGroup.position.z));
    },
    getControls: function () { return controls; },
    setFollowPose: function (on) { layers.follow = !!on; },
    setShowRobot: function (on) { layers.robot = !!on; robotGroup.visible = layers.robot; },
    setPose: function (pose) { pushTrail(pose); setPose(pose); },
    loadSample: function () {
      return loadJson('./sample.json').then(setMap).catch(function (e) { showErr(String(e)); });
    },
    loadFeed: function (path) {
      return loadJson(path || './feed.json').then(function (d) { showErr(''); setMap(d); })
        .catch(function (e) { showErr('feed: ' + e); });
    },
    getActiveDomain: function () { return activeDomainId; },
    listDomains: function () { return registry.domains.slice(); },
    setRegistry: setRegistry,
    refreshDomains: refreshRegistry,
    switchDomain: switchDomain,
    openNewDomain: openNewDomainModal,
    mergeIntoActive: function (incoming) {
      var merged = mergeOccupancy(currentMap, incoming);
      setMap(merged);
      var meta = findDomain(activeDomainId);
      if (meta) {
        meta.cell_count = (merged.cells || []).length;
        meta.run_count = (meta.run_count || 0) + 1;
        meta.updated = new Date().toISOString();
        renderDomainChips();
      }
      return merged;
    },
    getCurrentMap: function () { return currentMap; },
    connectTelemetry: connectTelemetry,
    disconnectTelemetry: disconnectTelemetry,
    getLastOdom: function () { return lastOdom; },
    getTelemetryState: function () {
      return {
        enabled: telem.enabled,
        state: telem.state,
        url: telem.url,
        lastMsgAt: telem.lastMsgAt,
        status: telem.status,
        lastOdom: lastOdom
      };
    }
  };

  buildK1();
  setLiveHud('offline', 'poll');

  // Domains must boot even if texture decode hangs (headless / slow GPU).
  var q0 = new URLSearchParams(window.location.search);
  var deepDomain = q0.get('domain');
  refreshRegistry().then(function () {
    if (deepDomain) return switchDomain(deepDomain);
  }).catch(function () {
    domainTitle.textContent = 'sample';
    buildWarehouse();
    window.k1LocalMap.loadSample();
  }).then(function () {
    if (wantLive()) connectTelemetry();
  });

  function loadTexFallback(primary, secondary) {
    return loadTex(primary).then(function (t) {
      if (t) return t;
      return secondary ? loadTex(secondary) : null;
    });
  }
  Promise.all([
    loadTexFallback('./assets/distribution-hub/floor_warehouse_diff.jpg', './assets/floor_diff.jpg').then(function (t) { floorTex = t; }),
    loadTexFallback('./assets/distribution-hub/metal_plate_diff.jpg', './assets/metal_diff.jpg').then(function (t) { metalTex = t; }),
    loadTexFallback('./assets/distribution-hub/painted_concrete_diff.jpg', './assets/plaster_diff.jpg').then(function (t) { plasterTex = t; }),
    loadTexFallback('./assets/distribution-hub/corrugated_diff.jpg', './assets/concrete_color.jpg').then(function (t) { concreteTex = t; }),
    loadTex('./assets/concrete_rough.jpg').then(function (t) { concreteRough = t; }),
    loadTexFallback('./assets/library/materials/wood_floor_diff.jpg', './assets/distribution-hub/wood_pallet_diff.jpg').then(function (t) { woodTex = t; }),
    loadTex('./assets/distribution-hub/cardboard_diff.jpg').then(function (t) { cardboardTex = t; }),
    loadTex('./assets/distribution-hub/shutter_diff.jpg').then(function (t) { shutterTex = t; }),
    loadTexFallback('./assets/distribution-hub/floor_anti_slip_diff.jpg', './assets/distribution-hub/floor_warehouse_diff.jpg').then(function (t) { antiSlipTex = t; })
  ]).then(function () {
    if (activeDomainId) buildEnvironmentFor(activeDomainId);
    return preloadHubGltf();
  }).catch(function () { /* textures optional */ });

  setInterval(function () {
    // When live WS is healthy, skip feed.json poll to avoid fighting the stream
    if (telem.enabled && telem.state === 'live' && (Date.now() - telem.lastMsgAt) < 3000) return;
    if (!activeDomainId) return;
    loadJson('./feed.json').then(function (d) {
      if (d && d.domain_id && d.domain_id !== activeDomainId) return;
      showErr('');
      if (d.pose) pushTrail(d.pose);
      setMap(d);
    }).catch(function () {});
  }, 2000);

  (function tick() {
    requestAnimationFrame(tick);
    applyPendingOdom();
    if (trailDirty) {
      trailRebuildCooldown -= 1;
      if (trailRebuildCooldown <= 0) {
        rebuildTrail();
        trailDirty = false;
        trailRebuildCooldown = 4; // ~every 4 frames at 60fps ≈ 15 Hz max rebuild
      }
    }
    ledPulse += 0.04;
    var pulse = 0.55 + 0.45 * Math.sin(ledPulse);
    robotGroup.traverse(function (obj) {
      if (obj.userData && obj.userData.pulseRing && obj.material) {
        obj.material.opacity = 0.28 + 0.32 * pulse;
        obj.scale.setScalar(0.96 + 0.06 * pulse);
      }
      if (obj.material && obj.material.userData && obj.material.userData.pulse) {
        obj.material.emissiveIntensity = 0.55 + 0.5 * pulse;
      }
    });
    if (controls) controls.update();
    renderer.render(scene, camera);
  })();
})();
