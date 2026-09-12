/* K1 Local Map — warehouse-grade Three.js viewer
 * Aesthetic: Tesla × SpaceX × Apple (near-black, white type, cyan #32D4FF).
 * Booster K1: official K1_22dof.urdf + meshes/*.STL only (BSD-3) — no character GLB.
 * (~0.95 m H). Procedural proxy only if URDF load fails.
 * Textures: Poly Haven CC0 (assets/ATTRIBUTION.md).
 */
(function () {
  'use strict';

  var ACCENT = 0x32d4ff; // HUD / robot telemetry only — never env prop fill
  var DANGER = 0xe31937; // STOP / e-stop chrome only — never occupancy blobs
  var BG = 0x000000;
  var RACK = 0x3a424c;
  var SAFETY = 0xc9a227;
  var OCC_GRAPHITE = 0x2a2e34;
  var OCC_EDGE = 0xd8dce2;
  var SIGN_FACE = 0xc8cdd2;
  var PLASTIC_GRAY = 0x5a626c;
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

  // exterior: building shell / perimeter walls (default ON — hide to inspect interior)
  // Occupancy defaults off when free GLB props are present (see syncOccLayerDefault)
  var layers = { env: true, occ: false, robot: true, trail: true, follow: true, exterior: true };
  try {
    var _exStored = localStorage.getItem('k1LocalMap.exteriorVisible');
    if (_exStored === '0' || _exStored === 'false') layers.exterior = false;
    else if (_exStored === '1' || _exStored === 'true') layers.exterior = true;
  } catch (e) {}

  var scene = new THREE.Scene();
  scene.background = new THREE.Color(BG);
  scene.fog = new THREE.FogExp2(BG, 0.016);

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

  // Soft industrial / office GI-ish: cool sky hemisphere + warm key + floor bounce
  scene.add(new THREE.HemisphereLight(0xd8e4f2, 0x1a1814, 0.55));
  scene.add(new THREE.AmbientLight(0xf2f0ea, 0.20));
  var key = new THREE.DirectionalLight(0xfff2e0, 1.05);
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
    key.shadow.bias = -0.00025;
    key.shadow.radius = 2.5;
  }
  scene.add(key);
  // Neutral fill rim — do not paint env meshes with HUD cyan
  var rim = new THREE.DirectionalLight(0xe8eef4, 0.22);
  rim.position.set(-5, 4, -7);
  scene.add(rim);
  var fill = new THREE.DirectionalLight(0xffffff, 0.28);
  fill.position.set(-8, 6, 3);
  scene.add(fill);
  var bounce = new THREE.DirectionalLight(0xffe8d0, 0.12);
  bounce.position.set(2, 1.5, -6);
  scene.add(bounce);

  var envGroup = new THREE.Group();
  var exteriorGroup = new THREE.Group();
  exteriorGroup.name = 'exteriorShell';
  exteriorGroup.visible = !!layers.exterior;
  var cellGroup = new THREE.Group();
  var robotGroup = new THREE.Group();
  var trailGroup = new THREE.Group();
  scene.add(envGroup);
  scene.add(cellGroup);
  scene.add(robotGroup);
  scene.add(trailGroup);

  /** Reset env + attach a fresh exterior shell group (walls stay togglable). */
  function beginEnvBuild() {
    clearGroup(envGroup);
    exteriorGroup = new THREE.Group();
    exteriorGroup.name = 'exteriorShell';
    exteriorGroup.visible = !!layers.exterior && !!layers.env;
    envGroup.add(exteriorGroup);
  }

  function applyExteriorVisibility() {
    if (exteriorGroup) exteriorGroup.visible = !!layers.exterior && !!layers.env;
    try {
      localStorage.setItem('k1LocalMap.exteriorVisible', layers.exterior ? '1' : '0');
    } catch (e) {}
    var btn = document.querySelector('#layers .layer[data-layer="exterior"]');
    if (btn) btn.classList.toggle('on', !!layers.exterior);
  }

  function setExteriorVisible(on) {
    layers.exterior = !!on;
    applyExteriorVisibility();
    notifyParent({ type: 'k1-layers', layers: Object.assign({}, layers) });
    return layers.exterior;
  }

  function setLayer(id, on) {
    if (!Object.prototype.hasOwnProperty.call(layers, id)) return false;
    layers[id] = !!on;
    var btn = document.querySelector('#layers .layer[data-layer="' + id + '"]');
    if (btn) btn.classList.toggle('on', layers[id]);
    if (id === 'env') {
      envGroup.visible = layers.env;
      applyExteriorVisibility();
    } else if (id === 'exterior') {
      applyExteriorVisibility();
    } else if (id === 'occ') {
      cellGroup.visible = layers.occ;
    } else if (id === 'robot') {
      robotGroup.visible = layers.robot;
    } else if (id === 'trail') {
      trailGroup.visible = layers.trail;
    }
    // follow is read in the animation loop; no mesh toggle
    notifyParent({ type: 'k1-layers', layers: Object.assign({}, layers) });
    return layers[id];
  }

  function notifyParent(payload) {
    try {
      if (window.parent && window.parent !== window) {
        window.parent.postMessage(Object.assign({ source: 'k1-localmap' }, payload), '*');
      }
    } catch (e) {}
  }

  var trailPoints = [];
  // Default elevated 3/4 framing (OrbitControls owns target + spherical state)
  var DEFAULT_RADIUS = 16;
  var DEFAULT_THETA = 0.72;  // azimuth
  var DEFAULT_PHI = 1.18;    // ~68° from zenith → classic elevated 3/4 (clear of joists)
  var orbitTarget = new THREE.Vector3(0, 0.45, 0);

  var registry = { active: null, domains: [] };
  var currentMap = null;
  var activeDomainId = null;
  /** Bumps on every domain switch so stale occupancy / feed fetches cannot paint the wrong map. */
  var domainSwitchGen = 0;
  var floorTex = null, metalTex = null, plasterTex = null, concreteTex = null, concreteRough = null;
  var woodTex = null, cardboardTex = null, shutterTex = null, antiSlipTex = null, carpetTex = null;
  var beltTex = null, cautionTex = null, brushMetalTex = null;
  var floorNorTex = null, floorArmTex = null, plasterNorTex = null, carpetNorTex = null, ceilingTileTex = null;
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
    // Keep polar off the poles so azimuth never gimbal-locks (360° drag stays usable)
    controls.minPolarAngle = 0.18;
    controls.maxPolarAngle = Math.PI - 0.18;
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
      // Disable damping for one sync so residual sphericalDelta cannot fight the posed camera
      var damp = controls.enableDamping;
      controls.enableDamping = false;
      controls.update();
      controls.enableDamping = damp;
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

  // ---- Booster K1 from official URDF only (no separate character GLB) ----
  // Source of truth: assets/library/robot/k1/K1_22dof.urdf + meshes/*.STL
  var K1_URDF_URL = './assets/library/robot/k1/K1_22dof.urdf';
  var K1_URDF_DIR = './assets/library/robot/k1/';
  var k1MeshLoaded = false;
  var k1UrdfRobot = null;

  // Neutral standing pose (radians) — exact joint names from K1_22dof.urdf
  var K1_STAND_Q = {
    left_shoulder_roll_joint: -1.15,
    right_shoulder_roll_joint: 1.15,
    aaleft_shoulder_pitch_joint: 0.2,
    aaright_shoulder_pitch_joint: 0.2,
    left_elbow_pitch_joint: 0.35,
    right_elbow_pitch_joint: 0.35
  };

  function addK1FootRing() {
    var foot = new THREE.Mesh(
      new THREE.RingGeometry(K1_W * 0.52, K1_W * 0.72, 96),
      new THREE.MeshBasicMaterial({ color: ACCENT, transparent: true, opacity: 0.42, side: THREE.DoubleSide })
    );
    foot.rotation.x = -Math.PI / 2;
    foot.position.y = 0.012;
    foot.userData.pulseRing = true;
    robotGroup.add(foot);
  }

  function buildK1Procedural() {
    clearGroup(robotGroup);
    var white = stdMat(K1_WHITE, { metalness: 0.28, roughness: 0.38 });
    var dark = stdMat(K1_DARK, { metalness: 0.45, roughness: 0.32 });
    var led = stdMat(ACCENT, { metalness: 0.1, roughness: 0.28, emissive: ACCENT, emissiveIntensity: 0.9 });
    led.userData.pulse = true;
    addK1FootRing();
    var hipY = 0.46;
    var trunkH = 0.28;
    var trunkW = 0.18;
    var trunkD = 0.14;
    robotGroup.add(makeBox(trunkW * 1.15, 0.08, trunkD * 1.1, dark, 0, hipY, 0));
    robotGroup.add(makeBox(trunkW, trunkH, trunkD, white, 0, hipY + 0.08 + trunkH / 2, 0));
    robotGroup.add(makeBox(trunkW * 0.85, 0.016, 0.012, led, 0, hipY + 0.18, trunkD * 0.55));
    robotGroup.add(makeBox(0.10, 0.09, 0.10, white, 0, 0.88, 0.01));
    robotGroup.add(makeBox(0.07, 0.028, 0.018, led, 0, 0.90, 0.065));
    robotGroup.add(makeBox(0.36, 0.04, 0.05, dark, 0, hipY + 0.28, 0));
    [-1, 1].forEach(function (s) {
      robotGroup.add(makeBox(0.045, 0.20, 0.045, white, s * 0.155, hipY + 0.16, 0));
      robotGroup.add(makeBox(0.04, 0.18, 0.04, dark, s * 0.155, hipY - 0.02, 0.01));
      robotGroup.add(makeBox(0.07, 0.22, 0.08, white, s * 0.085, 0.30, 0));
      robotGroup.add(makeBox(0.06, 0.20, 0.07, dark, s * 0.085, 0.10, 0.01));
      robotGroup.add(makeBox(0.09, 0.03, 0.14, dark, s * 0.085, 0.02, 0.02));
    });
    var nose = new THREE.Mesh(new THREE.ConeGeometry(0.035, 0.10, 3), led);
    nose.rotation.x = Math.PI / 2;
    nose.position.set(0, hipY + 0.12, trunkD * 0.7);
    robotGroup.add(nose);
    robotGroup.visible = layers.robot;
  }

  function applyK1StandPose(robot) {
    if (!robot) return;
    Object.keys(K1_STAND_Q).forEach(function (name) {
      var q = K1_STAND_Q[name];
      if (typeof robot.setJointValue === 'function') {
        try { robot.setJointValue(name, q); return; } catch (e) {}
      }
      var j = robot.joints && robot.joints[name];
      if (j && typeof j.setJointValue === 'function') j.setJointValue(q);
      else if (j && typeof j.setAngle === 'function') j.setAngle(q);
    });
  }

  function attachK1Urdf(robot) {
    clearGroup(robotGroup);
    addK1FootRing();
    k1UrdfRobot = robot;
    applyK1StandPose(robot);
    // URDF is Z-up; Local Map is Y-up
    robot.rotation.x = -Math.PI / 2;
    robot.updateMatrixWorld(true);
    var box = new THREE.Box3().setFromObject(robot);
    if (isFinite(box.min.y)) robot.position.y -= box.min.y;
    robot.traverse(function (obj) {
      if (obj.isMesh) {
        obj.castShadow = true;
        obj.receiveShadow = true;
        if (obj.material) {
          var mats = Array.isArray(obj.material) ? obj.material : [obj.material];
          mats.forEach(function (mat) {
            if (!mat) return;
            if (mat.color && mat.color.getHex && mat.color.getHex() === 0xffffff) {
              mat.color.setHex(K1_WHITE);
            }
            mat.metalness = mat.metalness != null ? mat.metalness : 0.35;
            mat.roughness = mat.roughness != null ? mat.roughness : 0.45;
          });
        }
      }
    });
    robotGroup.add(robot);
    robotGroup.visible = layers.robot;
    k1MeshLoaded = true;
  }

  function buildK1() {
    // Procedural stand-in until / unless URDF loads — never a separate character GLB
    buildK1Procedural();
    if (typeof URDFLoader === 'undefined') {
      console.warn('[k1] URDFLoader missing — procedural fallback only');
      return;
    }
    try {
      var loader = new URDFLoader();
      loader.workingPath = K1_URDF_DIR;
      loader.load(K1_URDF_URL, function (robot) {
        attachK1Urdf(robot);
      }, undefined, function (err) {
        console.warn('[k1] URDF load failed; keeping procedural proxy', err);
        k1MeshLoaded = false;
      });
    } catch (e) {
      console.warn('[k1] URDFLoader error', e);
      k1MeshLoaded = false;
    }
  }

  // ---- Environments -------------------------------------------------------
  function pbrFloorMat(diffTex, norTex, armTex, repeat, tint) {
    var map = texRepeat(diffTex, repeat, repeat);
    var opts = {
      color: tint != null ? tint : 0xd0d0d0,
      metalness: 0.04,
      roughness: 0.88
    };
    if (map) opts.map = map;
    if (norTex) {
      opts.normalMap = texRepeat(norTex, repeat, repeat) || norTex;
      opts.normalScale = new THREE.Vector2(0.7, 0.7);
    }
    if (armTex) {
      var arm = texRepeat(armTex, repeat, repeat) || armTex;
      opts.roughnessMap = arm;
      opts.aoMap = arm;
      opts.aoMapIntensity = 0.65;
    } else if (concreteRough) {
      opts.roughnessMap = concreteRough;
    }
    return (map || norTex)
      ? new THREE.MeshStandardMaterial(opts)
      : stdMat(0x1a1a1c, { metalness: 0.05, roughness: 0.9 });
  }

  function addFloor(size, repeat) {
    var m = pbrFloorMat(antiSlipTex || floorTex, floorNorTex, floorArmTex, repeat, 0xd4d4d4);
    var floor = new THREE.Mesh(new THREE.PlaneGeometry(size, size), m);
    floor.rotation.x = -Math.PI / 2;
    floor.receiveShadow = true;
    if (floor.geometry.attributes && !floor.geometry.attributes.uv2 && floor.geometry.attributes.uv) {
      floor.geometry.setAttribute('uv2', floor.geometry.attributes.uv);
    }
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

  /** Place real-world cartons ON a shelf board (no giant untextured BoxGeometry slabs). */
  function placeRackCartons(x, z, len, depth, shelfTop, levelIdx) {
    // Poly Haven cardboard / wooden crate only — avoid Kenney toy cubes on racks
    var names = ['ph-box', 'ph-crate', 'ph-box', 'ph-box', 'ph-crate'];
    var slotCount = Math.max(2, Math.min(4, Math.round(len / 2.8)));
    for (var k = 0; k < slotCount; k++) {
      if ((levelIdx + k) % 3 === 0) continue; // leave empty slots
      var t = slotCount === 1 ? 0.5 : k / (slotCount - 1);
      var along = (t - 0.5) * len * 0.68;
      var lateral = ((k + levelIdx) % 2 === 0 ? -0.1 : 0.1) * Math.min(depth, 1.2);
      var yaw = ((k * 0.41 + levelIdx * 0.17) % 1.0) - 0.5;
      var name = names[(levelIdx + k) % names.length];
      if (!gltfCache[name]) name = gltfCache['ph-box'] ? 'ph-box' : (gltfCache['ph-crate'] ? 'ph-crate' : null);
      if (name) {
        placeOnSurface(name, x + lateral, shelfTop, z + along, null, yaw);
      } else {
        // Textured cardboard proxy at real metres (never len×depth slabs)
        var bw = 0.42 + (k % 3) * 0.04;
        var bh = 0.34 + (levelIdx % 2) * 0.05;
        var bd = 0.40 + ((k + 1) % 3) * 0.04;
        envGroup.add(makeBox(bw, bh, bd, cardboardMat(0xc4a06a), x + lateral, shelfTop + bh / 2 + 0.004, z + along));
      }
    }
  }

  /** Clone a cached GLB so its AABB bottom sits on surfaceY (metres). */
  function placeOnSurface(name, x, surfaceY, z, scale, rotY) {
    var src = gltfCache[name];
    if (!src) return false;
    var root = new THREE.Group();
    var clone = src.clone(true);
    root.add(clone);
    if (Array.isArray(scale)) {
      fitRootToScaleM(root, scale);
    } else if (PROP_SCALE_M[name]) {
      fitRootToScaleM(root, PROP_SCALE_M[name]);
    } else if (scale != null) {
      root.scale.setScalar(scale);
    }
    if (rotY) root.rotation.y = rotY;
    root.position.set(x, 0, z);
    root.traverse(function (o) {
      if (o.isMesh) { o.castShadow = true; o.receiveShadow = true; }
    });
    root.updateMatrixWorld(true);
    var box3 = new THREE.Box3().setFromObject(root);
    // Sit fully ON the board (2 mm clearance) — avoid shelf intersection
    if (isFinite(box3.min.y)) root.position.y = surfaceY - box3.min.y + 0.002;
    else root.position.y = surfaceY + 0.002;
    envGroup.add(root);
    return true;
  }

  function rackBay(x, z, len, depth, levels) {
    var upright = metalMat(0x8a96a4);
    var shelf = stdMat(0x3a4048, { metalness: 0.35, roughness: 0.52 });
    var beam = stdMat(SAFETY, { metalness: 0.4, roughness: 0.42, emissive: SAFETY, emissiveIntensity: 0.06 });
    var brace = stdMat(0x2e343c, { metalness: 0.5, roughness: 0.4 });
    var h = 3.0; // pallet-rack bay ~2.7–4.5 m; low-bay default 3.0 m
    var shelfThick = 0.045;
    var corners = [
      [-depth / 2, -len / 2], [-depth / 2, len / 2],
      [depth / 2, -len / 2], [depth / 2, len / 2]
    ];
    corners.forEach(function (p) {
      envGroup.add(makeBox(0.07, h, 0.07, upright, x + p[0], h / 2, z + p[1]));
    });
    // X-bracing on outer faces (structural only — not bay-ID chrome)
    for (var b = 0; b < 3; b++) {
      var bz = z - len / 2 + (b + 0.5) * (len / 3);
      envGroup.add(makeBox(0.03, h * 0.85, 0.03, brace, x - depth / 2, h * 0.48, bz));
      envGroup.add(makeBox(0.03, h * 0.85, 0.03, brace, x + depth / 2, h * 0.48, bz));
    }
    for (var i = 0; i < levels; i++) {
      var y = 0.32 + i * (h - 0.45) / Math.max(levels - 1, 1);
      envGroup.add(makeBox(depth, shelfThick, len, shelf, x, y, z));
      envGroup.add(makeBox(0.04, 0.05, len, beam, x - depth / 2 - 0.02, y, z));
      envGroup.add(makeBox(0.04, 0.05, len, beam, x + depth / 2 + 0.02, y, z));
      // Board is centered at y → top face at y + half thickness — Poly Haven/Kenney GLBs
      placeRackCartons(x, z, len, depth, y + shelfThick * 0.5 + 0.002, i);
    }
  }

  function buildWarehouse() {
    beginEnvBuild();
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
    exteriorGroup.add(makeBox(20, 4.4, 0.22, wall, 0, 2.2, -9.5));
    exteriorGroup.add(makeBox(0.22, 4.4, 22, wall, -9.5, 2.2, 0));
    exteriorGroup.add(makeBox(0.22, 4.4, 22, wall, 9.5, 2.2, 0));
    exteriorGroup.add(makeBox(20, 4.4, 0.22, wall, 0, 2.2, 9.5));

    // dock door
    var door = stdMat(0x1a222a, { metalness: 0.35, roughness: 0.45 });
    envGroup.add(makeBox(3.4, 3.2, 0.12, door, 0, 1.6, -9.35));
    envGroup.add(makeBox(3.6, 0.12, 0.18, metalMat(0x8899aa), 0, 3.25, -9.32));
    // Dock ID plate — brushed metal + soft white face (not HUD cyan)
    envGroup.add(makeBox(2.6, 0.28, 0.06, stdMat(0x1a1c20, { metalness: 0.45, roughness: 0.4 }), 0, 3.55, -9.2));
    envGroup.add(makeBox(2.1, 0.12, 0.04, stdMat(SIGN_FACE, { metalness: 0.15, roughness: 0.45, emissive: SIGN_FACE, emissiveIntensity: 0.08 }), 0, 3.55, -9.16));

    // skylights + soft light panes
    var sky = stdMat(0x9aa3ab, { metalness: 0.05, roughness: 0.55, emissive: 0x6a737d, emissiveIntensity: 0.18 });
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
      envGroup.add(makeBox(0.16, 0.75, 0.16, bollard, p[0], 0.38, p[1]));
    });

    // Free library props (Poly Haven / Kenney / authored pallet)
    placeGltfClone('lib-pallet', -1.6, 0, 6.5, 1.0, 0);
    placeGltfClone('ph-box', -1.55, 0.12, 6.55, 1.0, 0.2);
    placeGltfClone('lib-pallet', 1.7, 0, 6.2, 1.0, -0.15);
    placeGltfClone('ph-crate', 1.7, 0.12, 6.2, 1.0, -0.1);
    placeGltfClone('lib-pallet', -1.5, 0, -6.0, 1.0, 0.1);
    placeGltfClone('ph-box', -1.45, 0.12, -5.95, null, 0.35);
    placeGltfClone('lib-cone', 0.5, 0, -7.5, null, 0); // 0.35×0.70×0.35 m
    placeGltfClone('lib-barrier', -7.2, 0, -8.4, 1.0, Math.PI / 2);
    placeGltfClone('lib-barrier', 7.2, 0, -8.4, 1.0, -Math.PI / 2);
    placeGltfClone('lib-handtruck', 2.0, 0, -4.0, 1.0, 0.5);
    placeGltfClone('ph-plastic', 2.4, 0, -3.5, 1.0, 0.2);
    placeGltfClone('lib-shelves', -8.6, 0, 2.5, null, Math.PI / 2); // corner 1.10×2.20×0.50 m
    placeGltfClone('ph-rack', 8.6, 0, -1.5, 1.0, -Math.PI / 2);
    placeGltfClone('lib-barrel', -6.8, 0, 5.5, 1.0, 0.2);
    placeGltfClone('lib-barrel', -6.2, 0, 5.8, 1.0, -0.3);
    placeGltfClone('lib-shutter', 0, 0, -9.55, null, 0);
    placeGltfClone('lib-light', -3.5, 4.0, 0, 1.0, 0);
    placeGltfClone('lib-light', 3.5, 4.0, 0, 1.0, 0);
    placeGltfClone('lib-wet', 1.2, 0, -7.2, 1.0, 0.15);
    placeGltfClone('lib-fluorescent', -2.0, 4.1, 2.5, null, 0);
    placeGltfClone('lib-fluorescent', 2.0, 4.1, -2.5, null, 0);
    placeGltfClone('lib-crate03', -2.2, 0, 5.8, null, 0.4);
    placeGltfClone('lib-crate03', 2.0, 0, 5.5, null, -0.2);
    placeGltfClone('lib-trash', 7.8, 0, -6.5, null, 0.3);
    placeGltfClone('lib-propane', -7.5, 0, 4.2, null, 0.1);
    placeGltfClone('lib-barrel2', -5.8, 0, 6.2, null, -0.4);
    placeGltfClone('lib-cam', -8.8, 3.6, -8.8, null, 0.7);
    placeGltfClone('lib-cam', 8.8, 3.6, 8.8, null, -2.2);
    forkliftProxy(-2.4, -7.2, 0.4);

    envGroup.visible = layers.env;
  }


  /** Dense Assembly Factory / factory line (replaces Kitchen seed domain). */
  function buildAssemblyFactory() {
    beginEnvBuild();
    var floorMat = pbrFloorMat(antiSlipTex || floorTex || beltTex, floorNorTex, floorArmTex, 12, 0xc8c8c8);
    var floor = new THREE.Mesh(new THREE.PlaneGeometry(28, 24), floorMat);
    floor.rotation.x = -Math.PI / 2;
    floor.receiveShadow = true;
    if (floor.geometry.attributes && !floor.geometry.attributes.uv2 && floor.geometry.attributes.uv) {
      floor.geometry.setAttribute('uv2', floor.geometry.attributes.uv);
    }
    envGroup.add(floor);

    var lineMat = new THREE.MeshBasicMaterial({ color: SAFETY });
    function stripe(x, z, w, d) {
      var s = new THREE.Mesh(new THREE.PlaneGeometry(w, d), lineMat);
      s.rotation.x = -Math.PI / 2;
      s.position.set(x, 0.012, z);
      envGroup.add(s);
    }
    [-3.4, 0, 3.4].forEach(function (ax) {
      stripe(ax, 0, 1.2, 14);
      stripe(ax - 0.55, 0, 0.06, 14);
      stripe(ax + 0.55, 0, 0.06, 14);
    });
    stripe(0, -4.8, 16, 1.1);
    stripe(0, 4.6, 16, 1.1);
    stripe(0, 0, 18, 0.08);

    conveyorSection(-3.4, -2.8, 3.4, 0);
    conveyorSection(-3.4, 0.6, 3.4, 0);
    conveyorSection(-3.4, 3.4, 2.4, 0);
    conveyorSection(0, -2.8, 3.4, 0);
    conveyorSection(0, 0.6, 3.4, 0);
    conveyorSection(0, 3.4, 2.4, 0);
    conveyorSection(3.4, -2.8, 3.4, 0);
    conveyorSection(3.4, 0.6, 3.4, 0);
    conveyorSection(3.4, 3.4, 2.4, 0);
    conveyorSection(0, -4.8, 4.5, Math.PI / 2);
    conveyorElevated(0, 4.6, 5.5, 1.3, Math.PI / 2);

    assemblyStation(-2.2, -2.0, Math.PI / 2);
    assemblyStation(-2.2, 1.5, Math.PI / 2);
    assemblyStation(2.2, -1.0, -Math.PI / 2);
    assemblyStation(2.2, 2.0, -Math.PI / 2);
    assemblyStation(-1.2, -4.2, 0);
    assemblyStation(1.2, 4.0, Math.PI);
    roboticArmProxy(-2.15, -0.4, Math.PI / 2);
    roboticArmProxy(2.15, 0.5, -Math.PI / 2);
    roboticArmProxy(-2.2, 3.0, Math.PI / 2);
    roboticArmProxy(2.2, -2.6, -Math.PI / 2);

    // Poly Haven plastic crates / totes instead of flat gray plastic blobs
    [[-2.5, -3.2], [-2.4, 0.4], [-2.5, 2.4], [2.5, -2.8], [2.4, 0.0], [2.5, 3.0]].forEach(function (p, i) {
      placeGltfClone(i % 2 ? 'lib-crate03' : 'ph-tote', p[0], 0, p[1], null, i * 0.35);
    });
    [[-4.2, -1.5], [4.2, 1.2], [-0.8, -3.6], [0.9, 3.4]].forEach(function (p, i) {
      placeGltfClone(i % 2 ? 'ph-plastic' : 'lib-crate03', p[0], 0, p[1], null, -i * 0.25);
    });

    safetyFenceRun(-4.65, 0.0, 10.5, 0);
    safetyFenceRun(4.65, 0.0, 10.5, 0);
    safetyFenceRun(-1.7, -5.4, 4.5, Math.PI / 2);
    safetyFenceRun(1.7, 5.2, 4.5, Math.PI / 2);
    lightCurtain(-3.4, -4.55, 0);
    lightCurtain(3.4, -4.55, 0);
    lightCurtain(0, 5.35, Math.PI / 2);

    controlPanelHmi(-1.0, -5.0, 0.25);
    controlPanelHmi(1.3, 4.9, Math.PI);
    controlPanelHmi(-4.3, 4.2, 0.6);

    var gantry = brushFrameMat(0x9098a2);
    [-3.4, 0, 3.4].forEach(function (x) {
      envGroup.add(makeBox(0.1, 0.1, 12.5, gantry, x, 3.55, 0));
      envGroup.add(makeBox(1.6, 0.08, 0.08, gantry, x, 3.55, -3));
      envGroup.add(makeBox(1.6, 0.08, 0.08, gantry, x, 3.55, 2));
    });
    envGroup.add(makeBox(14, 0.1, 0.1, gantry, 0, 3.55, 4.6));
    var wash = stdMat(0xf2f2f7, { emissive: 0xd8e8f8, emissiveIntensity: 0.4, roughness: 1, metalness: 0 });
    [[-3.4, -2], [-3.4, 2], [0, -2], [0, 2], [3.4, -2], [3.4, 2], [0, -4.5], [0, 4.2]].forEach(function (p) {
      envGroup.add(makeBox(1.1, 0.05, 0.55, wash, p[0], 3.7, p[1]));
    });

    var col = metalMat(0x6d7784);
    [[-6, -6], [-6, 6], [6, -6], [6, 6], [0, -6.5], [0, 6.5]].forEach(function (p) {
      envGroup.add(makeBox(0.36, 4.6, 0.36, col, p[0], 2.3, p[1]));
    });

    var wallMapF = texRepeat(concreteTex || plasterTex, 5, 1.3);
    var wallF = wallMapF
      ? new THREE.MeshStandardMaterial({ map: wallMapF, color: 0x9aa0a6, metalness: 0.16, roughness: 0.7 })
      : stdMat(0x14171b, { metalness: 0.08, roughness: 0.92 });
    exteriorGroup.add(makeBox(22, 4.8, 0.26, wallF, 0, 2.4, -7.2));
    exteriorGroup.add(makeBox(22, 4.8, 0.26, wallF, 0, 2.4, 7.2));
    exteriorGroup.add(makeBox(0.26, 4.8, 16, wallF, -7.2, 2.4, 0));
    exteriorGroup.add(makeBox(0.26, 4.8, 16, wallF, 7.2, 2.4, 0));
    exteriorGroup.add(makeBox(2.2, 2.6, 0.14, stdMat(0x1a222a, { metalness: 0.4, roughness: 0.4 }), -4.5, 1.3, -7.05));
    exteriorGroup.add(makeBox(3.4, 3.0, 0.14, stdMat(0x1a222a, { metalness: 0.4, roughness: 0.4 }), 3.5, 1.5, -7.05));
    baySign(0, 3.9, -7.0, 2.4);

    placeGltfClone('asm-conveyor-long', -3.4, 0, -2.8, 1.0, 0);
    placeGltfClone('asm-conveyor-stripe', 0, 0, 0.6, 1.0, 0);
    placeGltfClone('asm-conveyor-long', 3.4, 0, 2.0, 1.0, 0);
    placeGltfClone('asm-conveyor-curve', 0, 0, -4.8, 1.0, Math.PI);
    placeGltfClone('asm-crane', 0, 0, 0.5, 1.2, 0);
    placeGltfClone('asm-crane-lift', 0, 2.7, 0.5, 1.0, 0);
    placeGltfClone('asm-hopper', -4.0, 0, -0.5, 1.0, 0.3);
    placeGltfClone('asm-hopper-round', 4.0, 0, 1.5, 1.0, -0.4);
    placeGltfClone('asm-fence', -4.6, 0, -2.0, 1.0, 0);
    placeGltfClone('asm-fence-stripe', 4.6, 0, 2.0, 1.0, Math.PI);
    placeGltfClone('asm-structure', -5.5, 0, 4.5, 1.0, Math.PI / 2);
    placeGltfClone('asm-structure-wall', 5.5, 0, -4.5, 1.0, -Math.PI / 2, exteriorGroup);
    placeGltfClone('asm-catwalk', 0, 0, 5.5, 1.0, 0);
    placeGltfClone('asm-box-small', -3.3, 0.55, -1.5, 1.0, 0.2);
    placeGltfClone('asm-box-large', 3.3, 0.55, 1.0, 1.0, -0.15);
    placeGltfClone('ph-tote', -2.4, 0, -3.1, 1.0, 0.2);
    placeGltfClone('ph-plastic', 2.5, 0, -2.7, 1.0, -0.3);
    placeGltfClone('ph-toolchest', -4.2, 0, -3.5, 1.0, 0.4);
    placeGltfClone('ph-cart', 4.0, 0, 3.2, 1.0, -0.5);
    placeGltfClone('ph-drill', -4.0, 0, 2.8, 1.0, 0.2);
    placeGltfClone('ph-extinguisher', 4.4, 0, -4.8, 1.0, 0);
    placeGltfClone('ph-ladder', -5.8, 0, 0.5, 1.0, 0.1);
    placeGltfClone('ph-pipe-lamp', -3.4, 3.2, -1.0, 1.0, 0);
    placeGltfClone('ph-pipe-lamp', 3.4, 3.2, 1.5, 1.0, Math.PI);
    placeGltfClone('lib-barrel', -5.6, 0, -2.2, 1.0, 0.2);
    placeGltfClone('lib-barrel', -5.2, 0, -1.7, 1.0, -0.3);
    placeGltfClone('lib-light', -2, 3.6, 0, 1.0, 0);
    placeGltfClone('lib-light', 2, 3.6, 0, 1.0, 0);
    placeGltfClone('lib-cone', 0.6, 0, -5.6, 1.1, 0);
    placeGltfClone('asm-arrow', -3.4, 0.02, -4.2, 1.2, 0);
    placeGltfClone('asm-arrow', 3.4, 0.02, 4.0, 1.2, Math.PI);

    envGroup.visible = layers.env;
    applyExteriorVisibility();
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
    envGroup.add(makeBox(1.20, 0.14, 1.00, wood, x, 0.07, z));
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
      // Shrink-wrap face — frosted plastic, not cyan strip
      envGroup.add(makeBox(0.96, Math.max(0.02, y - 0.12), 0.02, stdMat(0xc8d0d8, {
        metalness: 0.08, roughness: 0.28, transparent: true, opacity: 0.35
      }), x, 0.12 + (y - 0.12) / 2, z + 0.47));
    }
  }

  function beltMat() {
    var map = texRepeat(beltTex, 2.2, 1.0);
    if (map) {
      return new THREE.MeshStandardMaterial({
        map: map, color: 0x2a2c2e, metalness: 0.08, roughness: 0.72
      });
    }
    return stdMat(0x1a1c1e, { metalness: 0.2, roughness: 0.55 });
  }

  function brushFrameMat(tint) {
    var map = texRepeat(brushMetalTex || metalTex, 1.6, 1.6);
    if (map) {
      return new THREE.MeshStandardMaterial({
        map: map, color: tint != null ? tint : 0x8a929a, metalness: 0.72, roughness: 0.32
      });
    }
    return metalMat(tint != null ? tint : 0x6a7380);
  }

  function cautionMat() {
    var map = texRepeat(cautionTex, 2.5, 1.0);
    if (map) {
      return new THREE.MeshStandardMaterial({
        map: map, color: 0xffffff, metalness: 0.15, roughness: 0.55,
        emissive: SAFETY, emissiveIntensity: 0.04
      });
    }
    return stdMat(SAFETY, { metalness: 0.25, roughness: 0.45, emissive: SAFETY, emissiveIntensity: 0.08 });
  }

  function conveyorSection(x, z, len, rotY) {
    var frame = brushFrameMat(0x6a7380);
    var belt = beltMat();
    var stripe = cautionMat();
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

  function conveyorElevated(x, z, len, height, rotY) {
    var frame = brushFrameMat(0x7a8490);
    var belt = beltMat();
    var g = new THREE.Group();
    var y0 = height != null ? height : 1.35;
    g.add(makeBox(0.85, 0.07, len, belt, 0, y0, 0));
    g.add(makeBox(0.07, 0.45, len, frame, -0.44, y0 - 0.2, 0));
    g.add(makeBox(0.07, 0.45, len, frame, 0.44, y0 - 0.2, 0));
    [[-0.38, -len * 0.4], [0.38, -len * 0.4], [-0.38, len * 0.4], [0.38, len * 0.4]].forEach(function (p) {
      g.add(makeBox(0.07, y0, 0.07, frame, p[0], y0 / 2, p[1]));
    });
    g.add(makeBox(0.88, 0.03, 0.06, cautionMat(), 0, y0 + 0.05, 0));
    g.position.set(x, 0, z);
    if (rotY) g.rotation.y = rotY;
    envGroup.add(g);
  }

  function assemblyStation(x, z, rotY) {
    var frame = brushFrameMat(0x707880);
    var top = stdMat(0x3a3e44, { metalness: 0.35, roughness: 0.45 });
    var g = new THREE.Group();
    g.add(makeBox(1.35, 0.06, 0.7, top, 0, 0.92, 0));
    [[-0.55, -0.28], [0.55, -0.28], [-0.55, 0.28], [0.55, 0.28]].forEach(function (p) {
      g.add(makeBox(0.06, 0.9, 0.06, frame, p[0], 0.45, p[1]));
    });
    g.add(makeBox(1.2, 0.35, 0.55, stdMat(0x2a2e34, { metalness: 0.25, roughness: 0.55 }), 0, 0.35, 0));
    // tool tray + small part
    g.add(makeBox(0.35, 0.05, 0.25, cautionMat(), -0.4, 0.98, 0.1));
    g.add(makeBox(0.18, 0.12, 0.18, stdMat(0x4a90a8, { metalness: 0.4, roughness: 0.4 }), 0.35, 1.01, -0.05));
    g.position.set(x, 0, z);
    if (rotY) g.rotation.y = rotY;
    envGroup.add(g);
  }

  function roboticArmProxy(x, z, rotY) {
    var base = brushFrameMat(0x889099);
    var link = stdMat(0xe0a820, { metalness: 0.45, roughness: 0.35 });
    var dark = stdMat(0x1c1c1e, { metalness: 0.5, roughness: 0.3 });
    var g = new THREE.Group();
    g.add(makeBox(0.55, 0.12, 0.55, base, 0, 0.06, 0));
    g.add(makeBox(0.28, 0.45, 0.28, dark, 0, 0.35, 0));
    // shoulder → elbow → wrist (simple articulated proxy)
    var arm = new THREE.Group();
    arm.position.set(0, 0.58, 0);
    arm.add(makeBox(0.16, 0.16, 0.55, link, 0, 0, 0.22));
    arm.add(makeBox(0.14, 0.14, 0.45, link, 0.05, 0.12, 0.55));
    arm.add(makeBox(0.1, 0.22, 0.1, dark, 0.05, 0.05, 0.78));
    arm.add(makeBox(0.18, 0.04, 0.08, base, 0.05, -0.02, 0.88));
    arm.rotation.y = 0.35;
    g.add(arm);
    g.position.set(x, 0, z);
    if (rotY) g.rotation.y = rotY;
    envGroup.add(g);
  }

  function safetyFenceRun(x, z, len, rotY) {
    var post = brushFrameMat(0x8a929a);
    var mesh = stdMat(0x9aa7b5, { metalness: 0.55, roughness: 0.35, transparent: true, opacity: 0.35 });
    var caution = cautionMat();
    var g = new THREE.Group();
    var n = Math.max(2, Math.round(len / 1.1));
    for (var i = 0; i < n; i++) {
      var lz = -len / 2 + (i / (n - 1)) * len;
      g.add(makeBox(0.06, 1.35, 0.06, post, 0, 0.68, lz));
    }
    g.add(makeBox(0.04, 1.1, len * 0.96, mesh, 0, 0.7, 0));
    g.add(makeBox(0.08, 0.1, len * 0.98, caution, 0, 1.35, 0));
    g.add(makeBox(0.08, 0.08, len * 0.98, caution, 0, 0.12, 0));
    g.position.set(x, 0, z);
    if (rotY) g.rotation.y = rotY;
    envGroup.add(g);
  }

  function lightCurtain(x, z, rotY) {
    var post = brushFrameMat(0x555a62);
    // IR-style beams — muted amber, not saturated danger-red plastic
    var beam = stdMat(0x8a6a28, { metalness: 0.25, roughness: 0.4, emissive: 0x6a5018, emissiveIntensity: 0.22, transparent: true, opacity: 0.4 });
    var g = new THREE.Group();
    g.add(makeBox(0.08, 1.55, 0.08, post, -0.55, 0.78, 0));
    g.add(makeBox(0.08, 1.55, 0.08, post, 0.55, 0.78, 0));
    for (var i = 0; i < 8; i++) {
      var y = 0.25 + i * 0.16;
      g.add(makeBox(1.05, 0.015, 0.015, beam, 0, y, 0));
    }
    g.position.set(x, 0, z);
    if (rotY) g.rotation.y = rotY;
    envGroup.add(g);
  }

  function controlPanelHmi(x, z, rotY) {
    var ped = brushFrameMat(0x6a7380);
    var panel = stdMat(0x1a1c20, { metalness: 0.4, roughness: 0.35 });
    // Dark industrial LCD — not HUD cyan fill
    var screen = stdMat(0x1a2820, { metalness: 0.15, roughness: 0.35, emissive: 0x243830, emissiveIntensity: 0.25 });
    var g = new THREE.Group();
    g.add(makeBox(0.35, 1.05, 0.28, ped, 0, 0.52, 0));
    g.add(makeBox(0.48, 0.42, 0.08, panel, 0, 1.25, 0.05));
    g.add(makeBox(0.38, 0.28, 0.03, screen, 0, 1.28, 0.1));
    g.add(makeBox(0.08, 0.08, 0.04, stdMat(0x33cc66, { emissive: 0x33cc66, emissiveIntensity: 0.4 }), -0.14, 1.08, 0.12));
    g.add(makeBox(0.08, 0.08, 0.04, stdMat(DANGER, { emissive: DANGER, emissiveIntensity: 0.35 }), 0.0, 1.08, 0.12));
    g.add(makeBox(0.08, 0.08, 0.04, stdMat(SAFETY, { emissive: SAFETY, emissiveIntensity: 0.3 }), 0.14, 1.08, 0.12));
    g.position.set(x, 0, z);
    if (rotY) g.rotation.y = rotY;
    envGroup.add(g);
  }

  /** East-side assembly / packaging line for Distribution Hub (OrbitControls untouched). */
  function buildAssemblyLineZone() {
    // Lane markings along the line footprint
    var lineMat = new THREE.MeshBasicMaterial({ color: SAFETY });
    function lane(x, z, w, d, rotY) {
      var s = new THREE.Mesh(new THREE.PlaneGeometry(w, d), lineMat);
      s.rotation.x = -Math.PI / 2;
      if (rotY) s.rotation.z = rotY;
      s.position.set(x, 0.014, z);
      envGroup.add(s);
    }
    lane(8.4, 1.0, 1.35, 14.5, 0);
    lane(7.55, 1.0, 0.08, 14.5, 0);
    lane(9.25, 1.0, 0.08, 14.5, 0);
    lane(7.2, 8.4, 3.2, 1.2, 0);

    // Straight + elevated + curve (procedural) — Kenney GLBs layered via placeGltfClone
    conveyorSection(8.4, -4.5, 3.2, 0);
    conveyorSection(8.4, -1.0, 3.2, 0);
    conveyorSection(8.4, 2.5, 3.2, 0);
    conveyorElevated(8.4, 5.5, 2.4, 1.25, 0);
    conveyorSection(8.4, 7.6, 1.6, 0);
    conveyorSection(7.0, 8.6, 2.0, Math.PI / 2);

    // Workstations + robotic arms along west side of belt
    assemblyStation(7.15, -3.2, Math.PI / 2);
    assemblyStation(7.15, 0.2, Math.PI / 2);
    assemblyStation(7.15, 3.6, Math.PI / 2);
    roboticArmProxy(7.2, -1.6, Math.PI / 2);
    roboticArmProxy(7.2, 1.8, Math.PI / 2);

    // Parts bins / totes
    var tote = stdMat(PLASTIC_GRAY, { metalness: 0.18, roughness: 0.55 });
    [[7.55, -4.2], [7.55, -2.5], [7.55, 0.9], [7.55, 2.8], [7.55, 4.8], [9.2, -0.5], [9.2, 2.2]].forEach(function (p) {
      envGroup.add(makeBox(0.5, 0.32, 0.38, tote, p[0], 0.16, p[1]));
    });

    // Safety fencing + light curtains
    safetyFenceRun(9.55, -2.0, 6.5, 0);
    safetyFenceRun(9.55, 4.0, 5.0, 0);
    safetyFenceRun(6.55, 0.5, 8.0, 0);
    lightCurtain(8.4, -6.2, 0);
    lightCurtain(8.4, 8.95, Math.PI / 2);

    // HMI / control pedestals
    controlPanelHmi(6.7, -5.0, 0.4);
    controlPanelHmi(6.7, 6.2, -0.2);

    // Overhead gantry rail (procedural beam + Kenney crane if loaded)
    var gantry = brushFrameMat(0x9098a2);
    envGroup.add(makeBox(0.12, 0.12, 12.5, gantry, 8.4, 3.35, 1.0));
    envGroup.add(makeBox(1.8, 0.1, 0.1, gantry, 8.4, 3.35, -4.5));
    envGroup.add(makeBox(1.8, 0.1, 0.1, gantry, 8.4, 3.35, 2.5));
    envGroup.add(makeBox(1.8, 0.1, 0.1, gantry, 8.4, 3.35, 7.5));
    envGroup.add(makeBox(0.35, 0.25, 0.55, stdMat(SAFETY, { metalness: 0.4, roughness: 0.4 }), 8.4, 3.15, 0.5));

    baySign(8.4, 3.55, -6.5, 1.8);

    // Free Kenney / Poly Haven accents for the line
    placeGltfClone('asm-conveyor', 8.4, 0, -4.5, 1.0, 0);
    placeGltfClone('asm-conveyor-long', 8.4, 0, -1.0, 1.0, 0);
    placeGltfClone('asm-conveyor-stripe', 8.4, 0, 2.5, 1.0, 0);
    placeGltfClone('asm-conveyor-curve', 7.0, 0, 8.6, 1.0, Math.PI);
    placeGltfClone('asm-crane', 8.4, 0, 1.0, 1.15, 0);
    placeGltfClone('asm-crane-lift', 8.4, 2.6, 0.5, 1.0, 0);
    placeGltfClone('asm-hopper', 9.15, 0, 5.8, 1.0, -0.4);
    placeGltfClone('asm-structure', 9.6, 0, -5.5, 1.0, Math.PI / 2);
    placeGltfClone('ph-tote', 7.5, 0, 2.2, 1.0, 0);
    placeGltfClone('ph-plastic', 7.45, 0, -0.8, 1.0, 0.5);
    placeGltfClone('asm-arrow', 8.4, 0.02, -5.8, 1.2, 0);
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
    // Bay ID plate — soft white on dark metal (not HUD cyan)
    envGroup.add(makeBox(w * 0.55, 0.22, 0.05, stdMat(0x1a1c20, { metalness: 0.4, roughness: 0.4 }), x, 3.55, z + 0.12));
    envGroup.add(makeBox(w * 0.42, 0.1, 0.03, stdMat(SIGN_FACE, {
      metalness: 0.12, roughness: 0.45, emissive: SIGN_FACE, emissiveIntensity: 0.1
    }), x, 3.55, z + 0.15));
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
    envGroup.add(makeBox((labelW || 1.4) * 0.72, 0.14, 0.04, stdMat(SIGN_FACE, {
      metalness: 0.12, roughness: 0.45, emissive: SIGN_FACE, emissiveIntensity: 0.12
    }), x, y, z + 0.04));
  }

  // Industry-standard real-world sizes in metres (NOT relative to K1).
  // K1 keeps its own envelope (~0.95×0.40×0.18 m). Aligns with asset-ontology scale_m.
  var PROP_SCALE_M = {
    'lib-cone': [0.35, 0.70, 0.35],       // traffic cone ~0.45–0.75 m tall
    'cone': [0.30, 0.70, 0.30],
    'lib-shelves': [1.10, 2.20, 0.50],    // corner steel shelf (~1.8–2.4 H, ~0.4–0.6 D)
    'ph-rack': [1.20, 3.00, 0.60],        // pallet-rack bay section (~2.7–4.5 H)
    'lib-crush': [2.20, 1.10, 0.45],
    'lib-block': [1.60, 0.85, 0.55],
    'lib-barrier': [1.55, 0.82, 0.55],    // concrete road barrier
    'lib-handtruck': [0.55, 1.20, 0.70],
    'lib-hand-truck': [0.55, 1.20, 0.70],
    'box-large': [0.55, 0.50, 0.50],      // cardboard, not Kenney toy cubes
    'box-wide': [0.55, 0.45, 0.70],
    'ph-box': [0.45, 0.40, 0.45],
    'ph-crate': [0.60, 0.45, 0.45],
    'ph-plastic': [0.45, 0.40, 0.55],
    'ph-tote': [0.55, 0.35, 0.40],
    'lib-barrel': [0.60, 0.90, 0.60],
    'lib-pallet': [1.20, 0.14, 1.00],     // Euro/GMA-ish pallet
    'lib-shutter': [3.20, 3.20, 0.25],    // dock door ~3–4 × 3–4 m
    'lib-wet': [0.35, 0.90, 0.20],
    'off-desk': [1.40, 0.75, 0.70],
    'off-chair': [0.50, 0.90, 0.50],
    'off-shelf': [0.90, 2.10, 0.40],
    'off-table': [1.20, 0.75, 0.70],
    'off-tv': [0.70, 0.45, 0.10],
    'off-plant': [0.40, 1.00, 0.40],
    'off-sofa': [1.80, 0.85, 0.85],
    'off-armchair': [0.75, 0.90, 0.75],
    'off-coffee': [1.10, 0.40, 0.60],
    'off-bookcase': [0.95, 2.00, 0.40],
    'off-cabinet': [1.00, 0.85, 0.45],
    'off-cabinet2': [0.90, 1.60, 0.45],
    'lib-crate03': [0.50, 0.35, 0.60],
    'lib-trash': [0.50, 0.95, 0.50],
    'lib-propane': [0.40, 1.20, 0.40],
    'lib-barrel2': [0.60, 0.90, 0.60],
    'lib-fluorescent': [1.20, 0.12, 0.25],
    'lib-cam': [0.25, 0.20, 0.30],
    'ph-fence': [2.00, 2.00, 0.15]
  };

  function fitRootToScaleM(root, scaleM) {
    if (!scaleM || !scaleM.length) return;
    var box3 = new THREE.Box3().setFromObject(root);
    var size = new THREE.Vector3();
    box3.getSize(size);
    if (size.x < 1e-4 || size.y < 1e-4 || size.z < 1e-4) return;
    var sx = scaleM[0] / size.x;
    var sy = scaleM[1] / size.y;
    var sz = scaleM[2] / size.z;
    // Uniform footprint XZ; independent Y (same as asset-placer.fitObjectToScale)
    var s = Math.min(sx, sz);
    root.scale.set(s, sy, s);
  }

  function placeGltfClone(name, x, y, z, scale, rotY) {
    var src = gltfCache[name];
    if (!src) return;
    var root = new THREE.Group();
    var clone = src.clone(true);
    root.add(clone);
    // Prefer explicit scale_m [X,Y,Z] metres, then PROP_SCALE_M, else legacy scalar
    if (Array.isArray(scale)) {
      fitRootToScaleM(root, scale);
    } else if (PROP_SCALE_M[name]) {
      fitRootToScaleM(root, PROP_SCALE_M[name]);
    } else {
      root.scale.setScalar(scale == null ? 1 : scale);
    }
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
    beginEnvBuild();
    var floorMat = pbrFloorMat(antiSlipTex || floorTex, floorNorTex, floorArmTex, 14, 0xc8c8c8);
    var floor = new THREE.Mesh(new THREE.PlaneGeometry(36, 36), floorMat);
    floor.rotation.x = -Math.PI / 2;
    floor.receiveShadow = true;
    if (floor.geometry.attributes && !floor.geometry.attributes.uv2 && floor.geometry.attributes.uv) {
      floor.geometry.setAttribute('uv2', floor.geometry.attributes.uv);
    }
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
    exteriorGroup.add(makeBox(28, 5.0, 0.28, wall, 0, 2.5, -10.4));
    exteriorGroup.add(makeBox(0.28, 5.0, 28, wall, -11.5, 2.5, 0));
    exteriorGroup.add(makeBox(0.28, 5.0, 28, wall, 11.5, 2.5, 0));
    exteriorGroup.add(makeBox(28, 5.0, 0.28, wall, 0, 2.5, 11.5));

    // Loading dock — three roll-up doors
    dockDoor(-5.2, -10.2, 3.2, true);
    dockDoor(0, -10.2, 3.4, false);
    dockDoor(5.2, -10.2, 3.2, true);

    // Structural columns
    var col = metalMat(0x6d7784);
    [[-9, -8], [-9, 0], [-9, 8], [9, -8], [9, 0], [9, 8], [0, 10.8], [-4, -9.5], [4, -9.5]].forEach(function (p) {
      envGroup.add(makeBox(0.42, 5.0, 0.42, col, p[0], 2.5, p[1]));
    });

    // Skylights + industrial light bars (narrower — free floor props stay readable in orbit)
    var sky = stdMat(0x9aa3ab, { metalness: 0.05, roughness: 0.55, emissive: 0x6a737d, emissiveIntensity: 0.16 });
    [-5.5, 5.5].forEach(function (x) {
      envGroup.add(makeBox(0.9, 0.05, 14, sky, x, 4.95, 0));
    });
    var joist = metalMat(0x707986);
    for (var jz = -8; jz <= 9; jz += 3.5) {
      envGroup.add(makeBox(22, 0.16, 0.16, joist, 0, 4.75, jz));
      // pendant light housings
      [-6, 0, 6].forEach(function (lx) {
        envGroup.add(makeBox(0.55, 0.08, 0.55, stdMat(0x222428, { metalness: 0.5, roughness: 0.35 }), lx, 4.55, jz));
        envGroup.add(makeBox(0.45, 0.04, 0.45, stdMat(0xf2eee6, {
          emissive: 0xe8e0d0, emissiveIntensity: 0.45
        }), lx, 4.5, jz));
      });
    }

    // East assembly / packaging line (conveyors, stations, arms, fencing, gantry)
    buildAssemblyLineZone();

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
      envGroup.add(makeBox(0.18, 0.80, 0.18, bollard, p[0], 0.40, p[1]));
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

    // Free CC0 Kenney / Poly Haven props — dock + center aisle (orbit-readable)
    placeGltfClone('conveyor-long', 0, 0, -4.2, 1.0, 0);
    placeGltfClone('box-large', -1.1, 0, -4.0, null, 0.15);
    placeGltfClone('box-wide', 1.3, 0, -3.7, 1.1, -0.2);
    placeGltfClone('box-large', -6.8, 0, -5.5, null, 0.2);
    placeGltfClone('box-wide', 6.8, 0, 4.2, 1.05, -0.4);
    placeGltfClone('cone', -2.4, 0, -8.2, 1.15, 0);
    placeGltfClone('lib-cone', 2.4, 0, -8.2, null, 0);
    placeGltfClone('ph-box', -2.8, 0, -6.8, null, 0.3);
    placeGltfClone('ph-crate', 2.8, 0, -6.6, 1.15, -0.2);
    placeGltfClone('ph-box', 0.15, 0, -5.9, 1.1, -0.1);
    placeGltfClone('lib-pallet', -3.2, 0, -7.4, 1.05, 0.15);
    placeGltfClone('lib-pallet', 3.4, 0, -7.2, 1.05, -0.2);
    placeGltfClone('lib-pallet', 0.5, 0, 5.5, 1.05, 0.4);
    placeGltfClone('ph-plastic', 0.55, 0, 5.55, 1.1, 0.2);
    placeGltfClone('ph-tote', 7.5, 0, 2.2, 1.05, 0);
    placeGltfClone('lib-barrier', -8.2, 0, -8.8, 1.0, Math.PI / 2);
    placeGltfClone('lib-crush', 8.2, 0, -8.5, 1.0, -Math.PI / 2);
    placeGltfClone('lib-hand-truck', -0.8, 0, -6.8, 1.05, 0.4);
    placeGltfClone('ph-rack', -8.6, 0, 2.0, 1.1, Math.PI / 2);
    placeGltfClone('lib-barrier', -7.5, 0, -8.8, 1.0, 0.1);
    placeGltfClone('lib-barrier', 7.5, 0, -8.8, 1.0, -0.1);
    placeGltfClone('lib-crush', -8.8, 0, -4.0, 1.0, Math.PI / 2);
    placeGltfClone('lib-block', 8.8, 0, -6.5, 1.0, 0);
    placeGltfClone('lib-handtruck', -0.9, 0, -6.5, 1.05, 0.4);
    placeGltfClone('lib-shelves', -9.2, 0, 6.5, null, Math.PI / 2); // corner 1.10×2.20×0.50 m
    placeGltfClone('lib-barrel', -7.4, 0, 5.2, 1.05, 0.2);
    placeGltfClone('lib-barrel', -6.8, 0, 5.5, 1.05, -0.3);
    placeGltfClone('lib-shutter', 0, 0, -10.15, null, 0);
    placeGltfClone('door-wide-open', -5.2, 0, -10.05, 1.15, 0);
    placeGltfClone('lib-light', -3.8, 3.8, 0, 1.0, 0);
    placeGltfClone('lib-light', 3.8, 3.8, 0, 1.0, 0);
    placeGltfClone('lib-wet', 1.2, 0, -7.5, null, 0.15);
    placeGltfClone('lib-fluorescent', -5.5, 4.6, 2.0, null, 0);
    placeGltfClone('lib-fluorescent', 5.5, 4.6, -2.0, null, 0);
    placeGltfClone('lib-crate03', -1.5, 0, 6.0, null, 0.3);
    placeGltfClone('lib-crate03', 1.8, 0, 6.2, null, -0.4);
    placeGltfClone('lib-trash', 9.5, 0, -7.5, null, 0.2);
    placeGltfClone('lib-propane', -9.2, 0, 3.5, null, 0.5);
    placeGltfClone('lib-barrel2', 7.6, 0, 5.8, null, -0.2);
    placeGltfClone('lib-cam', -10.5, 4.2, -9.5, null, 0.8);
    placeGltfClone('ph-fence', 9.8, 0, 0.5, null, Math.PI / 2);

    envGroup.visible = layers.env;
  }


  function buildOffice() {
    beginEnvBuild();
    // Poly Haven dirty_carpet PBR (diff + nor + arm) — warm office carpet
    var carpetMat = pbrFloorMat(carpetTex || woodTex || floorTex, carpetNorTex, floorArmTex, 5, 0xb8a898);
    var floor = new THREE.Mesh(new THREE.PlaneGeometry(16, 14), carpetMat);
    floor.rotation.x = -Math.PI / 2;
    floor.receiveShadow = true;
    if (floor.geometry.attributes && !floor.geometry.attributes.uv2 && floor.geometry.attributes.uv) {
      floor.geometry.setAttribute('uv2', floor.geometry.attributes.uv);
    }
    envGroup.add(floor);

    var wallMap = texRepeat(plasterTex || concreteTex, 3.2, 1.2);
    var wallOpts = {
      color: 0xe4e6e8, metalness: 0.03, roughness: 0.9
    };
    if (wallMap) wallOpts.map = wallMap;
    if (plasterNorTex) {
      wallOpts.normalMap = texRepeat(plasterNorTex, 3.2, 1.2) || plasterNorTex;
      wallOpts.normalScale = new THREE.Vector2(0.45, 0.45);
    }
    if (concreteRough) wallOpts.roughnessMap = concreteRough;
    var wall = wallMap || plasterNorTex
      ? new THREE.MeshStandardMaterial(wallOpts)
      : stdMat(0xd0d4d8, { metalness: 0.05, roughness: 0.9 });
    exteriorGroup.add(makeBox(16, 3.1, 0.18, wall, 0, 1.55, -6.2));
    exteriorGroup.add(makeBox(16, 3.1, 0.18, wall, 0, 1.55, 6.2));
    exteriorGroup.add(makeBox(0.18, 3.1, 14, wall, -7.4, 1.55, 0));
    exteriorGroup.add(makeBox(0.18, 3.1, 14, wall, 7.4, 1.55, 0));

    // Cubicle partitions — drywall tone (not floating white slabs)
    var part = wallMap
      ? new THREE.MeshStandardMaterial({ map: texRepeat(plasterTex, 1.2, 1.0) || plasterTex, color: 0xd8dce0, metalness: 0.04, roughness: 0.86 })
      : stdMat(0xc8ccd0, { metalness: 0.05, roughness: 0.8 });
    var partBase = metalMat(0x6a7380);
    [[-3.2, -0.4], [-0.4, -0.4], [2.4, -0.4]].forEach(function (p) {
      envGroup.add(makeBox(1.7, 0.06, 1.7, partBase, p[0], 0.03, p[1]));
      envGroup.add(makeBox(1.7, 1.2, 0.05, part, p[0], 0.66, p[1] - 0.82));
      envGroup.add(makeBox(0.05, 1.2, 1.7, part, p[0] - 0.82, 0.66, p[1]));
    });

    // Acoustic ceiling tiles
    var ceilMap = texRepeat(ceilingTileTex || plasterTex, 1.0, 1.0);
    var ceil = ceilMap
      ? new THREE.MeshStandardMaterial({ map: ceilMap, color: 0xf0f0f2, metalness: 0.02, roughness: 0.92, emissive: 0xdde4ea, emissiveIntensity: 0.12 })
      : stdMat(0xe8e8ea, { metalness: 0.02, roughness: 0.95, emissive: 0xdde4ea, emissiveIntensity: 0.22 });
    for (var cx = -5; cx <= 5; cx += 2) {
      for (var cz = -4; cz <= 4; cz += 2) {
        envGroup.add(makeBox(1.85, 0.04, 1.85, ceil, cx, 2.95, cz));
      }
    }

    var glass = stdMat(0xa8c8e8, { metalness: 0.1, roughness: 0.12, transparent: true, opacity: 0.38 });
    envGroup.add(makeBox(1.6, 1.3, 0.06, glass, -3.0, 1.7, -6.05));
    envGroup.add(makeBox(1.6, 1.3, 0.06, glass, 0, 1.7, -6.05));
    envGroup.add(makeBox(1.6, 1.3, 0.06, glass, 3.0, 1.7, -6.05));

    var elev = metalMat(0x8a96a4);
    envGroup.add(makeBox(1.15, 2.2, 0.12, elev, -5.8, 1.1, 5.95));
    envGroup.add(makeBox(1.15, 2.2, 0.12, elev, -4.2, 1.1, 5.95));

    // Desk row — Poly Haven metal desks + chairs
    [-3.2, -0.4, 2.4].forEach(function (x) {
      placeGltfClone('off-desk', x, 0, -0.55, null, 0);
      placeGltfClone('off-chair', x, 0, 0.25, null, Math.PI);
      placeGltfClone('off-tv', x, 0.75, -0.85, null, 0);
    });
    // Second bank along north
    placeGltfClone('off-desk', -3.2, 0, 2.4, null, Math.PI);
    placeGltfClone('off-chair', -3.2, 0, 1.6, null, 0);
    placeGltfClone('off-desk', -0.4, 0, 2.4, null, Math.PI);
    placeGltfClone('off-chair', -0.4, 0, 1.6, null, 0);

    placeGltfClone('off-shelf', 6.2, 0, 2.8, null, -Math.PI / 2);
    placeGltfClone('off-shelf', 6.2, 0, -2.2, null, -Math.PI / 2);
    placeGltfClone('off-bookcase', -6.4, 0, -2.5, null, Math.PI / 2);
    placeGltfClone('off-cabinet', -6.4, 0, 1.5, null, Math.PI / 2);
    placeGltfClone('off-cabinet2', 5.8, 0, 5.0, null, Math.PI);
    placeGltfClone('off-coffee', 2.8, 0, 4.2, null, 0.2);
    placeGltfClone('off-armchair', 1.6, 0, 4.4, null, -0.6);
    placeGltfClone('off-armchair', 4.0, 0, 4.0, null, 0.8);
    placeGltfClone('off-sofa', 3.0, 0, 5.2, null, Math.PI);
    placeGltfClone('off-table', -4.5, 0, 3.5, null, 0.15);
    placeGltfClone('off-plant', 5.8, 0, -5.0, null, 0);
    placeGltfClone('off-plant', -6.2, 0, -5.0, null, 0.4);
    placeGltfClone('off-plant', 0.5, 0, 5.4, null, -0.2);
    placeGltfClone('lib-fluorescent', -2, 2.85, -1, null, 0);
    placeGltfClone('lib-fluorescent', 2, 2.85, 2, null, 0);
    placeGltfClone('lib-fluorescent', -2, 2.85, 3.5, null, 0);
    placeGltfClone('lib-light', 0, 2.7, 0, 1.0, 0);
    placeGltfClone('lib-cam', -6.8, 2.7, -5.8, null, 0.5);
    placeGltfClone('lib-trash', 6.5, 0, -4.5, null, 0.2);

    envGroup.visible = layers.env;
  }

  /**
   * Known seed / demo domains may ship a themed shell + props.
   * Operator-created domains must stay EMPTY (ground plane only) until Import last run /
   * Rerun placer / autofill / ?demo_assets=1 adds content.
   */
  var SEEDED_ENV_DOMAIN_IDS = {
    'assembly-factory': 'assembly',
    'kitchen': 'assembly', // legacy id → factory shell
    'warehouse-bay-a': 'warehouse',
    'distribution-hub': 'hub',
    'outdoor-patio': 'hub', // legacy id
    'patio': 'hub',
    'office': 'office'
  };

  /** Minimal empty shell: optional ground plane only — no racks, cones, furniture. */
  function buildEmptyDomain() {
    beginEnvBuild();
    addFloor(24, 10);
    envGroup.visible = layers.env;
  }

  function buildEnvironmentFor(domainId) {
    var id = String(domainId || '').toLowerCase();
    var kind = SEEDED_ENV_DOMAIN_IDS[id];
    if (kind === 'office') buildOffice();
    else if (kind === 'assembly') buildAssemblyFactory();
    else if (kind === 'hub') buildDistributionHub();
    else if (kind === 'warehouse') buildWarehouse();
    else buildEmptyDomain();
  }

  function preloadHubGltf() {
    if (!gltfLoader) return Promise.resolve();
    var jobs = [
      ['box-large', './assets/distribution-hub/kenney/box-large.glb'],
      ['box-wide', './assets/distribution-hub/kenney/box-wide.glb'],
      ['cone', './assets/distribution-hub/kenney/cone.glb'],
      ['conveyor-long', './assets/distribution-hub/kenney/conveyor-long.glb'],
      ['asm-conveyor', './assets/library/assembly-line/kenney/conveyor.glb'],
      ['asm-conveyor-long', './assets/library/assembly-line/kenney/conveyor-long.glb'],
      ['asm-conveyor-stripe', './assets/library/assembly-line/kenney/conveyor-long-stripe.glb'],
      ['asm-conveyor-curve', './assets/library/assembly-line/kenney/conveyor-corner.glb'],
      ['asm-crane', './assets/library/assembly-line/kenney/crane.glb'],
      ['asm-crane-lift', './assets/library/assembly-line/kenney/crane-lift.glb'],
      ['asm-hopper', './assets/library/assembly-line/kenney/hopper-square.glb'],
      ['asm-structure', './assets/library/assembly-line/kenney/structure-yellow-tall.glb'],
      ['asm-arrow', './assets/library/assembly-line/kenney/arrow.glb'],
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
      ['asm-hopper-round', './assets/library/assembly-line/kenney/hopper-round.glb'],
      ['asm-structure-wall', './assets/library/assembly-line/kenney/structure-wall.glb'],
      ['asm-fence', './assets/library/assembly-line/kenney/conveyor-bars-fence.glb'],
      ['asm-fence-stripe', './assets/library/assembly-line/kenney/conveyor-bars-stripe-fence.glb'],
      ['asm-catwalk', './assets/library/assembly-line/kenney/catwalk-straight.glb'],
      ['asm-box-small', './assets/library/assembly-line/kenney/box-small.glb'],
      ['asm-box-large', './assets/library/assembly-line/kenney/box-large.glb'],
      ['ph-toolchest', './assets/library/assembly-line/polyhaven/metal_tool_chest/metal_tool_chest_1k.gltf'],
      ['ph-cart', './assets/library/assembly-line/polyhaven/industrial_storage_cart/industrial_storage_cart_1k.gltf'],
      ['ph-drill', './assets/library/assembly-line/polyhaven/drill_press_01/drill_press_01_1k.gltf'],
      ['ph-extinguisher', './assets/library/assembly-line/polyhaven/korean_fire_extinguisher_01/korean_fire_extinguisher_01_1k.gltf'],
      ['ph-ladder', './assets/library/assembly-line/polyhaven/ladder_sectioned_01/ladder_sectioned_01_1k.gltf'],
      ['ph-pipe-lamp', './assets/library/assembly-line/polyhaven/industrial_pipe_lamp/industrial_pipe_lamp_1k.gltf'],
      ['off-desk', './assets/library/office/metal_office_desk/metal_office_desk_1k.gltf'],
      ['off-chair', './assets/library/office/plastic_monobloc_chair_01/plastic_monobloc_chair_01_1k.gltf'],
      ['off-shelf', './assets/library/office/Shelf_01/Shelf_01_1k.gltf'],
      ['off-table', './assets/library/office/SchoolDesk_01/SchoolDesk_01_1k.gltf'],
      ['off-tv', './assets/library/office/Television_01/Television_01_1k.gltf'],
      ['off-plant', './assets/library/office/potted_plant_02/potted_plant_02_1k.gltf'],
      ['off-sofa', './assets/library/generic/Sofa_01/Sofa_01_1k.gltf'],
      ['off-armchair', './assets/library/office/modern_arm_chair_01/modern_arm_chair_01_1k.gltf'],
      ['off-coffee', './assets/library/office/modern_coffee_table_01/modern_coffee_table_01_1k.gltf'],
      ['off-bookcase', './assets/library/office/wooden_bookshelf_worn/wooden_bookshelf_worn_1k.gltf'],
      ['off-cabinet', './assets/library/office/modern_wooden_cabinet/modern_wooden_cabinet_1k.gltf'],
      ['off-cabinet2', './assets/library/office/painted_wooden_cabinet/painted_wooden_cabinet_1k.gltf'],
      ['lib-crate03', './assets/library/warehouse/plastic_crate_03/plastic_crate_03_1k.gltf'],
      ['lib-trash', './assets/library/warehouse/metal_trash_can/metal_trash_can_1k.gltf'],
      ['lib-propane', './assets/library/warehouse/propane_tank/propane_tank_1k.gltf'],
      ['lib-barrel2', './assets/library/warehouse/Barrel_02/Barrel_02_1k.gltf'],
      ['lib-fluorescent', './assets/library/cross-domain/mounted_fluorescent_lights/mounted_fluorescent_lights_1k.gltf'],
      ['lib-cam', './assets/library/cross-domain/security_camera_01/security_camera_01_1k.gltf'],
      ['ph-fence', './assets/library/assembly-line/polyhaven/modular_chainlink_fence/modular_chainlink_fence_1k.gltf']
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
      var loaded = 0;
      Object.keys(gltfCache).forEach(function (k) { if (gltfCache[k]) loaded++; });
      syncOccLayerDefault(loaded > 0);
      if (activeDomainId) buildEnvironmentFor(activeDomainId);
      // Robot avatar is URDF-only (buildK1) — never attach a character GLB here
    });
  }

  // ---- Occupancy (graphite cells + soft white edges — not toy cyan/red) ---
  function clearCells() { clearGroup(cellGroup); }

  function syncOccLayerDefault(hasGltfProps) {
    var want = !hasGltfProps;
    layers.occ = want;
    cellGroup.visible = want;
    var btn = document.querySelector('#layers .layer[data-layer="occ"]');
    if (btn) btn.classList.toggle('on', want);
  }

  function setOccupancy(data) {
    clearCells();
    if (!data) return;
    var res = data.res_m || 0.08;
    var cells = data.cells || [];
    var maxHits = 1;
    for (var i = 0; i < cells.length; i++) maxHits = Math.max(maxHits, cells[i].hits || 1);
    var geo = new THREE.BoxGeometry(res * 0.88, 1, res * 0.88);
    var edgeGeo = new THREE.BoxGeometry(res * 0.94, 1, res * 0.94);
    for (var j = 0; j < cells.length; j++) {
      var cell = cells[j];
      var hits = cell.hits || 1;
      var t = hits / maxHits;
      var h = 0.04 + 0.28 * Math.pow(t, 0.9);
      var fill = new THREE.MeshStandardMaterial({
        color: OCC_GRAPHITE,
        emissive: 0x000000,
        emissiveIntensity: 0,
        metalness: 0.12,
        roughness: 0.82,
        transparent: true,
        opacity: 0.28 + 0.35 * t,
        depthWrite: false
      });
      var edge = new THREE.MeshStandardMaterial({
        color: OCC_EDGE,
        emissive: OCC_EDGE,
        emissiveIntensity: 0.04 + 0.06 * t,
        metalness: 0.05,
        roughness: 0.55,
        transparent: true,
        opacity: 0.18 + 0.22 * t,
        depthWrite: false
      });
      var mesh = new THREE.Mesh(geo, fill);
      mesh.position.set(cell.x || 0, h / 2, cell.y || 0);
      mesh.scale.y = h;
      var rim = new THREE.Mesh(edgeGeo, edge);
      rim.position.set(cell.x || 0, h / 2, cell.y || 0);
      rim.scale.y = Math.max(0.02, h * 0.08);
      rim.position.y = h + 0.005;
      cellGroup.add(rim);
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
    var gen = ++domainSwitchGen;
    activeDomainId = id;
    registry.active = id;
    trailPoints = [];
    rebuildTrail();
    // Drop previous domain's occupancy immediately so a slow/stale fetch cannot linger
    // (and so empty/new domains stay empty until their own data arrives).
    clearCells();
    currentMap = { domain_id: id, res_m: 0.08, range_m: 3.5, pose: { x: 0, y: 0, yaw: 0 }, cells: [] };
    statsEl.textContent = 'loading…';
    var meta = findDomain(id);
    domainTitle.textContent = (meta && meta.name) ? meta.name : id;
    renderDomainChips();
    buildEnvironmentFor(id);
    if (typeof window.k1LocalMapOnDomainChange === 'function') {
      try { window.k1LocalMapOnDomainChange(id); } catch (e) {}
    }
    notifyParent({
      type: 'k1-domain',
      active: id,
      domains: registry.domains.slice(),
      title: (meta && meta.name) ? meta.name : id,
      stats: statsEl ? statsEl.textContent : ''
    });
    // Label→asset fill-in: load persisted instances for this domain (reruns accumulate).
    // Prefer the onDomainChange hook (index.html) when present to avoid double-fetch races;
    // fall back here when assets boot before the hook is installed.
    if (!window.k1LocalMapOnDomainChange && window.k1LocalMapAssets && window.k1LocalMapAssets.isReady()) {
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
      if (gen !== domainSwitchGen || activeDomainId !== id) return null; // stale response
      showErr('');
      if (!d.domain_id) d.domain_id = id;
      setMap(d);
      return d;
    }).catch(function (e) {
      if (gen !== domainSwitchGen || activeDomainId !== id) return null; // stale error
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

  function refreshRegistry(opts) {
    opts = opts || {};
    return loadJson(DOMAINS_INDEX).then(function (reg) {
      return setRegistry(reg, { forceId: opts.forceId || reg.active });
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
  function emptyOccupancy(domainId) {
    return {
      domain_id: domainId,
      res_m: 0.08,
      range_m: 3.5,
      pose: { x: 0, y: 0, yaw: 0 },
      trail: [],
      cells: []
    };
  }

  function emptyInstancesPayload(domainId) {
    return {
      domain_id: domainId,
      updated: new Date().toISOString(),
      notes: 'operator-created empty domain — import a run or enable ?demo_assets=1',
      instances: []
    };
  }

  /** Persist a brand-new EMPTY domain (no props, no occupancy, no demo instances). */
  function createEmptyDomain(name, opts) {
    opts = opts || {};
    var n = (name || '').trim();
    if (!n) return Promise.reject(new Error('name required'));
    var id = opts.id || slugify(n);
    var meta = {
      id: id,
      name: n,
      updated: new Date().toISOString(),
      run_count: 0,
      cell_count: 0
    };
    if (!opts.skipHost && typeof window.k1LocalMapHostCreateDomain === 'function') {
      try {
        window.k1LocalMapHostCreateDomain(JSON.stringify({ id: id, name: n, empty: true }));
        return Promise.resolve(meta);
      } catch (e) {}
    }
    var body = {
      id: id,
      name: n,
      empty: true,
      occupancy: emptyOccupancy(id),
      instances: emptyInstancesPayload(id)
    };
    return fetch('/api/domains', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }).then(function (res) {
      if (!res.ok) throw new Error('create domain HTTP ' + res.status);
      return res.json().catch(function () { return body; });
    }).catch(function () {
      return null;
    }).then(function () {
      var exists = findDomain(id);
      if (!exists) registry.domains.push(meta);
      else {
        exists.name = n;
        exists.run_count = 0;
        exists.cell_count = 0;
        exists.updated = meta.updated;
      }
      registry.active = id;
      try {
        localStorage.setItem('k1LocalMap.instances.' + id, JSON.stringify(emptyInstancesPayload(id)));
      } catch (e) {}
      if (window.k1LocalMapAssets && window.k1LocalMapAssets.clearInstances) {
        try { window.k1LocalMapAssets.clearInstances(); } catch (e2) {}
      }
      // quiet: missing occupancy.json is expected for brand-new browser-created domains
      return switchDomain(id, { quiet: true }).then(function () {
        setMap(emptyOccupancy(id));
        showErr('');
        statsEl.textContent = 'empty domain — import a run or load sample';
        return meta;
      });
    });
  }

  function requestNewDomain(name) {
    var n = (name || '').trim();
    if (!n) return;
    closeNewDomainModal();
    createEmptyDomain(n).catch(function (e) { showErr('new domain: ' + e); });
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
      if (id === 'env') {
        envGroup.visible = layers.env;
        applyExteriorVisibility();
      }
      if (id === 'exterior') applyExteriorVisibility();
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
    setFollowPose: function (on) { return setLayer('follow', !!on); },
    setShowRobot: function (on) { return setLayer('robot', !!on); },
    setExteriorVisible: setExteriorVisible,
    getExteriorVisible: function () { return !!layers.exterior; },
    setLayer: setLayer,
    getLayers: function () { return Object.assign({}, layers); },
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
    createDomain: createEmptyDomain,
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
    getHudSnapshot: function () {
      return {
        active: activeDomainId,
        title: domainTitle ? domainTitle.textContent : '',
        stats: statsEl ? statsEl.textContent : '',
        domains: registry.domains.slice(),
        layers: Object.assign({}, layers),
        pose: {
          x: poseXEl ? poseXEl.textContent : '0.00',
          y: poseYEl ? poseYEl.textContent : '0.00',
          yaw: poseYawEl ? poseYawEl.textContent : '0.00',
          trail: poseTrailEl ? poseTrailEl.textContent : '0',
          vx: poseVxEl ? poseVxEl.textContent : '0.00',
          vy: poseVyEl ? poseVyEl.textContent : '0.00',
          wz: poseWzEl ? poseWzEl.textContent : '0.00'
        },
        live: {
          state: liveStateEl ? liveStateEl.textContent : 'OFFLINE',
          detail: liveDetailEl ? liveDetailEl.textContent : 'poll',
          className: liveDotEl ? liveDotEl.className : 'off'
        },
        telemetry: {
          enabled: telem.enabled,
          state: telem.state,
          url: telem.url,
          lastMsgAt: telem.lastMsgAt,
          status: telem.status,
          lastOdom: lastOdom
        }
      };
    },
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

  notifyParent({ type: 'k1-ready' });

  buildK1();
  setLiveHud('offline', 'poll');

  // Domains must boot even if texture decode hangs (headless / slow GPU).
  // Prefer ?domain= over domains.json active so deep-links / screenshots stick.
  var q0 = new URLSearchParams(window.location.search);
  var deepDomain = q0.get('domain');
  refreshRegistry({ forceId: deepDomain || undefined }).catch(function () {
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
    loadTexFallback('./assets/distribution-hub/floor_anti_slip_diff.jpg', './assets/distribution-hub/floor_warehouse_diff.jpg').then(function (t) { antiSlipTex = t; }),
    loadTexFallback('./assets/library/assembly-line/textures/rubber_belt_diff.jpg', './assets/library/assembly-line/textures/rubber_mat_diff.jpg').then(function (t) { beltTex = t; }),
    loadTex('./assets/library/assembly-line/textures/caution_stripes_diff.jpg').then(function (t) { cautionTex = t; }),
    loadTexFallback('./assets/library/assembly-line/textures/brushed_metal_diff.jpg', './assets/library/assembly-line/textures/scratched_metal_diff.jpg').then(function (t) { brushMetalTex = t; }),
    loadTex('./assets/library/office/textures/carpet_ph_diff_1k.jpg').then(function (t) { carpetTex = t; }),
    loadTex('./assets/library/office/textures/carpet_ph_nor_gl_1k.jpg').then(function (t) { carpetNorTex = t; }),
    loadTexFallback('./assets/library/materials/concrete_floor_painted_diff_1k.jpg', './assets/distribution-hub/floor_warehouse_diff.jpg').then(function (t) { if (t) floorTex = t; }),
    loadTex('./assets/library/materials/concrete_floor_painted_nor_gl_1k.jpg').then(function (t) { floorNorTex = t; }),
    loadTex('./assets/library/materials/concrete_floor_painted_arm_1k.jpg').then(function (t) { floorArmTex = t; }),
    loadTexFallback('./assets/library/materials/painted_plaster_wall_diff_1k.jpg', './assets/plaster_diff.jpg').then(function (t) { if (t) plasterTex = t; }),
    loadTex('./assets/library/materials/painted_plaster_wall_nor_gl_1k.jpg').then(function (t) { plasterNorTex = t; }),
    loadTexFallback('./assets/library/office/textures/ceiling_ph_diff_1k.jpg', './assets/library/office/textures/ceiling_tile_diff.jpg').then(function (t) { ceilingTileTex = t; }),
    loadTexFallback('./assets/library/assembly-line/textures/rubber_tiles_ph_diff_1k.jpg', './assets/library/assembly-line/textures/rubber_belt_diff.jpg').then(function (t) { if (t) beltTex = t; })
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
