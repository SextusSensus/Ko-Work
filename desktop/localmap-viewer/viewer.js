/* K1 Local Map — warehouse-grade Three.js viewer
 * Aesthetic: Tesla × SpaceX × Apple (near-black, white type, cyan #32D4FF).
 * Booster K1: official K1_22dof.urdf + meshes/*.STL only (BSD-3) — no character GLB.
 * (~0.95 m H). Procedural proxy only if URDF load fails.
 * Textures: Poly Haven CC0 (assets/ATTRIBUTION.md).
 */
(function () {
  'use strict';

  var ACCENT = 0x3b82f6; // launch blue telemetry — never env prop fill
  var BG = 0x000000;
  var SAFETY = 0xc9a227;
  var OCC_GRAPHITE = 0x2a2e34;
  var OCC_EDGE = 0xd8dce2;
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
  var floorTex = null, antiSlipTex = null, concreteRough = null, floorNorTex = null, floorArmTex = null;
  var texLoader = new THREE.TextureLoader();

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

  function snapK1ToFloor(robot) {
    if (!robot) return false;
    // Reset then measure so repeated snaps do not drift as STLs stream in.
    robot.position.y = 0;
    robot.updateMatrixWorld(true);
    var box = new THREE.Box3().setFromObject(robot);
    if (!isFinite(box.min.y) || !isFinite(box.max.y)) return false;
    var height = box.max.y - box.min.y;
    // Incomplete mesh loads produce tiny boxes — wait for STLs.
    if (height < 0.45) return false;
    robot.position.y = -box.min.y + 0.002;
    return true;
  }

  function attachK1Urdf(robot) {
    clearGroup(robotGroup);
    addK1FootRing();
    k1UrdfRobot = robot;
    applyK1StandPose(robot);
    // URDF is Z-up; Local Map is Y-up
    robot.rotation.x = -Math.PI / 2;
    snapK1ToFloor(robot);
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
    // Re-snap for several frames while async STL meshes finish loading.
    var tries = 0;
    function resnap() {
      snapK1ToFloor(robot);
      tries += 1;
      if (tries < 90) requestAnimationFrame(resnap);
    }
    requestAnimationFrame(resnap);
  }

  function buildK1() {
    // Procedural stand-in until / unless URDF loads — never a separate character GLB
    buildK1Procedural();
    if (typeof URDFLoader === 'undefined') {
      console.warn('[k1] URDFLoader missing — procedural fallback only');
      return;
    }
    try {
      var manager = new THREE.LoadingManager();
      var pending = null;
      manager.onLoad = function () {
        if (pending) snapK1ToFloor(pending);
      };
      var loader = new URDFLoader(manager);
      loader.workingPath = K1_URDF_DIR;
      loader.load(K1_URDF_URL, function (robot) {
        pending = robot;
        attachK1Urdf(robot);
        snapK1ToFloor(robot);
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

  /** Minimal empty shell: optional ground plane only — no racks, cones, furniture. */
  function buildEmptyDomain() {
    beginEnvBuild();
    addFloor(24, 10);
    envGroup.visible = layers.env;
  }

  // ---- Occupancy (graphite cells + soft white edges — not toy cyan/red) ---
  function clearCells() { clearGroup(cellGroup); }

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
    buildEmptyDomain();
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
        window.k1LocalMapAssets.loadInstancesForDomain(id).catch(function () {});
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
    // Unknown / removed ids (stale ?domain=, old demo active) fall through to a real registry entry.
    var next = [opts.forceId, registry.active].filter(findDomain)[0] || (registry.domains[0] && registry.domains[0].id) || null;
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
      notes: 'operator-created empty domain',
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
    buildEmptyDomain();
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
    loadTexFallback('./assets/library/materials/concrete_floor_painted_diff_1k.jpg', './assets/floor_diff.jpg').then(function (t) { floorTex = t; }),
    loadTex('./assets/library/materials/floor_anti_slip_diff.jpg').then(function (t) { antiSlipTex = t; }),
    loadTex('./assets/concrete_rough.jpg').then(function (t) { concreteRough = t; }),
    loadTex('./assets/library/materials/concrete_floor_painted_nor_gl_1k.jpg').then(function (t) { floorNorTex = t; }),
    loadTex('./assets/library/materials/concrete_floor_painted_arm_1k.jpg').then(function (t) { floorArmTex = t; })
  ]).then(function () {
    if (activeDomainId) buildEmptyDomain();
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
