/* K1 Local Map — warehouse-grade Three.js viewer
 * Aesthetic: Tesla × SpaceX × Apple (black glass, cyan telemetry).
 * Booster K1 proxy envelope: ~0.95 m H × 0.40 m W × 0.18 m D.
 * Textures: Poly Haven CC0 (assets/ATTRIBUTION.md).
 */
(function () {
  'use strict';

  // ---- constants ----------------------------------------------------------
  var ACCENT = 0x32d4ff;
  var BG = 0x000000;
  var RACK = 0x2a2e33;
  var BEAM = 0xc9a227;
  var OCC = 0x32d4ff;
  var K1_WHITE = 0xf2f2f7;
  var K1_DARK = 0x1c1c1e;
  var K1_H = 0.95, K1_W = 0.40, K1_D = 0.18;
  var MAX_TRAIL = 240;
  var DATA_ROOT = '../localmap-data';
  var DOMAINS_INDEX = DATA_ROOT + '/domains.json';

  // ---- DOM ----------------------------------------------------------------
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

  var layers = { env: true, occ: true, robot: true, trail: true, follow: true };

  // ---- three.js core ------------------------------------------------------
  var scene = new THREE.Scene();
  scene.background = new THREE.Color(BG);
  scene.fog = new THREE.FogExp2(BG, 0.028);

  var camera = new THREE.PerspectiveCamera(45, 1, 0.05, 120);
  var renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: 'high-performance' });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setSize(viewport.clientWidth || 800, viewport.clientHeight || 600);
  if (renderer.toneMapping !== undefined) {
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 1.05;
  }
  if (renderer.shadowMap) {
    renderer.shadowMap.enabled = true;
    renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  }
  viewport.appendChild(renderer.domElement);

  scene.add(new THREE.HemisphereLight(0xb0c4de, 0x1a1a1c, 0.45));
  scene.add(new THREE.AmbientLight(0xffffff, 0.18));
  var key = new THREE.DirectionalLight(0xffffff, 0.85);
  key.position.set(6, 12, 4);
  key.castShadow = true;
  if (key.shadow) {
    key.shadow.mapSize.set(1024, 1024);
    key.shadow.camera.near = 0.5;
    key.shadow.camera.far = 40;
    key.shadow.camera.left = -12;
    key.shadow.camera.right = 12;
    key.shadow.camera.top = 12;
    key.shadow.camera.bottom = -12;
  }
  scene.add(key);
  var rim = new THREE.DirectionalLight(ACCENT, 0.22);
  rim.position.set(-4, 3, -6);
  scene.add(rim);

  var envGroup = new THREE.Group();
  var cellGroup = new THREE.Group();
  var robotGroup = new THREE.Group();
  var trailGroup = new THREE.Group();
  scene.add(envGroup);
  scene.add(cellGroup);
  scene.add(robotGroup);
  scene.add(trailGroup);

  var trailPoints = [];
  var target = new THREE.Vector3(0, 0.35, 0);
  var spherical = { radius: 9.5, theta: 0.78, phi: 0.95 };
  var dragging = false, panning = false, lastX = 0, lastY = 0;

  var registry = { active: null, domains: [] };
  var currentMap = null;
  var activeDomainId = null;
  var floorTex = null, metalTex = null;
  var texLoader = new THREE.TextureLoader();

  function loadTex(url) {
    return new Promise(function (resolve) {
      texLoader.load(url, function (t) {
        t.wrapS = t.wrapT = THREE.RepeatWrapping;
        if (THREE.sRGBEncoding !== undefined) t.encoding = THREE.sRGBEncoding;
        resolve(t);
      }, undefined, function () { resolve(null); });
    });
  }

  function applyCamera() {
    camera.position.set(
      target.x + spherical.radius * Math.sin(spherical.phi) * Math.sin(spherical.theta),
      target.y + spherical.radius * Math.cos(spherical.phi),
      target.z + spherical.radius * Math.sin(spherical.phi) * Math.cos(spherical.theta)
    );
    camera.lookAt(target);
  }
  applyCamera();

  function onResize() {
    var w = viewport.clientWidth || window.innerWidth;
    var h = viewport.clientHeight || window.innerHeight;
    camera.aspect = w / Math.max(h, 1);
    camera.updateProjectionMatrix();
    renderer.setSize(w, h);
  }
  window.addEventListener('resize', onResize);
  onResize();

  renderer.domElement.addEventListener('pointerdown', function (e) {
    dragging = e.button === 0;
    panning = e.button === 2 || e.button === 1;
    lastX = e.clientX; lastY = e.clientY;
    renderer.domElement.setPointerCapture(e.pointerId);
  });
  renderer.domElement.addEventListener('pointerup', function (e) {
    dragging = false; panning = false;
    try { renderer.domElement.releasePointerCapture(e.pointerId); } catch (err) {}
  });
  renderer.domElement.addEventListener('pointermove', function (e) {
    var dx = e.clientX - lastX, dy = e.clientY - lastY;
    lastX = e.clientX; lastY = e.clientY;
    if (dragging) {
      spherical.theta -= dx * 0.005;
      spherical.phi = Math.max(0.18, Math.min(Math.PI - 0.18, spherical.phi + dy * 0.005));
      applyCamera();
    } else if (panning) {
      var right = new THREE.Vector3();
      var up = new THREE.Vector3(0, 1, 0);
      camera.getWorldDirection(right);
      right.cross(up).normalize();
      var forward = new THREE.Vector3().crossVectors(up, right).normalize();
      var scale = spherical.radius * 0.0014;
      target.addScaledVector(right, -dx * scale);
      target.addScaledVector(forward, dy * scale);
      applyCamera();
    }
  });
  renderer.domElement.addEventListener('wheel', function (e) {
    e.preventDefault();
    spherical.radius = Math.max(2.2, Math.min(28, spherical.radius * (e.deltaY > 0 ? 1.07 : 0.93)));
    applyCamera();
  }, { passive: false });
  renderer.domElement.addEventListener('contextmenu', function (e) { e.preventDefault(); });

  // ---- helpers ------------------------------------------------------------
  function stdMat(color, opts) {
    opts = opts || {};
    return new THREE.MeshStandardMaterial({
      color: color,
      metalness: opts.metalness != null ? opts.metalness : 0.15,
      roughness: opts.roughness != null ? opts.roughness : 0.65,
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
      var c = g.children.pop();
      if (c.geometry) c.geometry.dispose();
      if (c.material) {
        if (Array.isArray(c.material)) c.material.forEach(function (mm) { mm.dispose(); });
        else c.material.dispose();
      }
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

  // ---- Booster K1 proxy ---------------------------------------------------
  function buildK1() {
    clearGroup(robotGroup);
    var white = stdMat(K1_WHITE, { metalness: 0.25, roughness: 0.4 });
    var dark = stdMat(K1_DARK, { metalness: 0.4, roughness: 0.35 });
    var led = stdMat(ACCENT, { metalness: 0.1, roughness: 0.3, emissive: ACCENT, emissiveIntensity: 0.85 });

    var foot = new THREE.Mesh(
      new THREE.RingGeometry(K1_W * 0.55, K1_W * 0.68, 48),
      new THREE.MeshBasicMaterial({ color: ACCENT, transparent: true, opacity: 0.55, side: THREE.DoubleSide })
    );
    foot.rotation.x = -Math.PI / 2;
    foot.position.y = 0.012;
    robotGroup.add(foot);

    robotGroup.add(makeBox(K1_W * 0.72, 0.08, K1_D * 0.95, dark, 0, 0.42, 0));
    robotGroup.add(makeBox(K1_W * 0.62, 0.28, K1_D * 0.85, white, 0, 0.62, 0));
    robotGroup.add(makeBox(K1_W * 0.35, 0.02, 0.01, led, 0, 0.68, K1_D * 0.43));
    robotGroup.add(makeBox(0.12, 0.11, 0.11, white, 0, 0.86, 0.01));
    robotGroup.add(makeBox(0.08, 0.03, 0.02, led, 0, 0.88, 0.07));
    robotGroup.add(makeBox(K1_W * 0.95, 0.05, 0.06, dark, 0, 0.74, 0));
    [-1, 1].forEach(function (s) {
      robotGroup.add(makeBox(0.05, 0.22, 0.05, white, s * K1_W * 0.42, 0.58, 0));
      robotGroup.add(makeBox(0.045, 0.18, 0.045, dark, s * K1_W * 0.42, 0.40, 0.02));
      robotGroup.add(makeBox(0.08, 0.22, 0.09, white, s * 0.08, 0.28, 0));
      robotGroup.add(makeBox(0.07, 0.20, 0.08, dark, s * 0.08, 0.10, 0.01));
      robotGroup.add(makeBox(0.09, 0.04, 0.14, dark, s * 0.08, 0.02, 0.02));
    });
    var nose = new THREE.Mesh(new THREE.ConeGeometry(0.045, 0.12, 3), led);
    nose.rotation.x = Math.PI / 2;
    nose.position.set(0, 0.55, K1_D * 0.55);
    robotGroup.add(nose);
    robotGroup.visible = layers.robot;
  }

  // ---- Environments -------------------------------------------------------
  function addFloor(size, repeat) {
    var geo = new THREE.PlaneGeometry(size, size);
    var m;
    if (floorTex) {
      var t = floorTex.clone();
      t.needsUpdate = true;
      t.repeat.set(repeat, repeat);
      m = new THREE.MeshStandardMaterial({ map: t, color: 0xffffff, metalness: 0.05, roughness: 0.85 });
    } else {
      m = stdMat(0x1a1a1c, { metalness: 0.05, roughness: 0.9 });
    }
    var floor = new THREE.Mesh(geo, m);
    floor.rotation.x = -Math.PI / 2;
    floor.receiveShadow = true;
    envGroup.add(floor);

    var lineMat = new THREE.MeshBasicMaterial({ color: BEAM });
    return function stripe(x, z, w, d) {
      var s = new THREE.Mesh(new THREE.PlaneGeometry(w, d), lineMat);
      s.rotation.x = -Math.PI / 2;
      s.position.set(x, 0.008, z);
      envGroup.add(s);
    };
  }

  function rackBay(x, z, len, depth, levels) {
    var upright = stdMat(RACK, { metalness: 0.55, roughness: 0.35 });
    if (metalTex) {
      upright = new THREE.MeshStandardMaterial({
        map: metalTex, color: 0x8899aa, metalness: 0.65, roughness: 0.35
      });
    }
    var shelf = stdMat(0x3a4048, { metalness: 0.3, roughness: 0.55 });
    var beam = stdMat(BEAM, { metalness: 0.4, roughness: 0.45, emissive: BEAM, emissiveIntensity: 0.08 });
    var h = 2.6;
    [[-depth / 2, -len / 2], [-depth / 2, len / 2], [depth / 2, -len / 2], [depth / 2, len / 2]].forEach(function (p) {
      envGroup.add(makeBox(0.06, h, 0.06, upright, x + p[0], h / 2, z + p[1]));
    });
    for (var i = 0; i < levels; i++) {
      var y = 0.35 + i * (h - 0.4) / Math.max(levels - 1, 1);
      envGroup.add(makeBox(depth, 0.04, len, shelf, x, y, z));
      for (var k = -1; k <= 1; k++) {
        if ((i + k + 3) % 3 === 0) continue;
        var load = stdMat(0x6b5b4a, { metalness: 0.05, roughness: 0.8 });
        envGroup.add(makeBox(depth * 0.7, 0.32 + (i % 3) * 0.08, len * 0.22, load, x, y + 0.22, z + k * len * 0.28));
      }
    }
    envGroup.add(makeBox(0.03, 0.04, len, beam, x - depth / 2 - 0.02, 0.9, z));
  }

  function buildWarehouse() {
    clearGroup(envGroup);
    var stripe = addFloor(28, 10);
    stripe(0, 0, 0.12, 16);
    stripe(-0.35, 0, 0.04, 16);
    stripe(0.35, 0, 0.04, 16);
    stripe(0, 0, 12, 0.12);
    stripe(0, 4, 12, 0.08);
    stripe(0, -4, 12, 0.08);
    rackBay(-3.2, 0, 10, 1.1, 5);
    rackBay(3.2, 0, 10, 1.1, 5);
    rackBay(-3.2, 7.5, 4, 1.1, 4);
    rackBay(3.2, 7.5, 4, 1.1, 4);
    var col = stdMat(0x2c3036, { metalness: 0.5, roughness: 0.4 });
    [[-6, -6], [-6, 6], [6, -6], [6, 6], [0, 9], [0, -8]].forEach(function (p) {
      envGroup.add(makeBox(0.35, 4.2, 0.35, col, p[0], 2.1, p[1]));
    });
    var wall = stdMat(0x121417, { metalness: 0.1, roughness: 0.9 });
    envGroup.add(makeBox(18, 4.2, 0.2, wall, 0, 2.1, -9));
    envGroup.add(makeBox(3.2, 3.0, 0.15, stdMat(0x1a1f24, { metalness: 0.3, roughness: 0.5 }), 0, 1.5, -8.88));
    var sky = stdMat(0xa8c8e8, { metalness: 0, roughness: 1, emissive: 0x88aacc, emissiveIntensity: 0.35 });
    [-3, 0, 3].forEach(function (x) { envGroup.add(makeBox(1.2, 0.05, 14, sky, x, 4.15, 0)); });
    envGroup.add(makeBox(2.4, 0.35, 0.08, stdMat(ACCENT, { emissive: ACCENT, emissiveIntensity: 0.4 }), 0, 3.4, -8.7));
    envGroup.visible = layers.env;
  }

  function buildKitchen() {
    clearGroup(envGroup);
    addFloor(16, 6);
    var cab = stdMat(0x2a2a2e, { metalness: 0.2, roughness: 0.55 });
    var counter = stdMat(0xd8d4cc, { metalness: 0.1, roughness: 0.45 });
    envGroup.add(makeBox(1.8, 0.9, 0.9, cab, 0, 0.45, 0.5));
    envGroup.add(makeBox(1.9, 0.04, 1.0, counter, 0, 0.92, 0.5));
    envGroup.add(makeBox(4.5, 0.9, 0.6, cab, 0, 0.45, -2.8));
    envGroup.add(makeBox(0.6, 0.9, 3.2, cab, -2.6, 0.45, -0.8));
    envGroup.add(makeBox(0.6, 0.9, 3.2, cab, 2.6, 0.45, -0.8));
    envGroup.add(makeBox(0.7, 1.8, 0.7, stdMat(0xe8e8ea, { metalness: 0.5, roughness: 0.3 }), -2.5, 0.9, 1.6));
    envGroup.visible = layers.env;
  }

  function buildPatio() {
    clearGroup(envGroup);
    addFloor(18, 5);
    var grass = new THREE.Mesh(new THREE.PlaneGeometry(18, 6), stdMat(0x1a2a1c, { metalness: 0, roughness: 1 }));
    grass.rotation.x = -Math.PI / 2;
    grass.position.set(0, 0.004, 6);
    envGroup.add(grass);
    var plan = stdMat(0x4a3a2a, { roughness: 0.8 });
    [[-2, 3], [2, 3], [-3, 5], [3, 5]].forEach(function (p) {
      envGroup.add(makeBox(0.5, 0.4, 0.5, plan, p[0], 0.2, p[1]));
      envGroup.add(makeBox(0.15, 0.7, 0.15, stdMat(0x2d5a2d), p[0], 0.75, p[1]));
    });
    var fence = stdMat(0x3a3a3c, { metalness: 0.3, roughness: 0.5 });
    for (var i = -4; i <= 4; i++) envGroup.add(makeBox(0.08, 1.1, 0.08, fence, i * 1.2, 0.55, 8));
    envGroup.add(makeBox(10, 0.06, 0.06, fence, 0, 1.05, 8));
    envGroup.visible = layers.env;
  }

  function buildEnvironmentFor(domainId) {
    var id = String(domainId || '').toLowerCase();
    if (id.indexOf('kitchen') >= 0) buildKitchen();
    else if (id.indexOf('patio') >= 0 || id.indexOf('outdoor') >= 0) buildPatio();
    else buildWarehouse();
  }

  // ---- Occupancy ----------------------------------------------------------
  function clearCells() { clearGroup(cellGroup); }

  function setOccupancy(data) {
    clearCells();
    if (!data) return;
    var res = data.res_m || 0.08;
    var cells = data.cells || [];
    var maxHits = 1;
    for (var i = 0; i < cells.length; i++) maxHits = Math.max(maxHits, cells[i].hits || 1);
    var geo = new THREE.BoxGeometry(res * 0.88, 1, res * 0.88);
    for (var j = 0; j < cells.length; j++) {
      var cell = cells[j];
      var hits = cell.hits || 1;
      var t = hits / maxHits;
      var h = 0.12 + 1.1 * Math.pow(t, 0.85);
      var m = new THREE.MeshStandardMaterial({
        color: OCC, emissive: ACCENT, emissiveIntensity: 0.12 + 0.45 * t,
        metalness: 0.05, roughness: 0.35, transparent: true,
        opacity: 0.28 + 0.55 * t, depthWrite: t > 0.55
      });
      var mesh = new THREE.Mesh(geo, m);
      mesh.position.set(cell.x || 0, h / 2, cell.y || 0);
      mesh.scale.y = h;
      mesh.castShadow = t > 0.4;
      cellGroup.add(mesh);
    }
    cellGroup.visible = layers.occ;
  }

  // ---- Odom trail ---------------------------------------------------------
  function rebuildTrail() {
    clearGroup(trailGroup);
    if (trailPoints.length < 2) { trailGroup.visible = layers.trail; return; }
    var pts = trailPoints.map(function (p) { return new THREE.Vector3(p.x, 0.04, p.y); });
    var geo = new THREE.BufferGeometry().setFromPoints(pts);
    trailGroup.add(new THREE.Line(geo, new THREE.LineBasicMaterial({ color: ACCENT, transparent: true, opacity: 0.75 })));
    var dotGeo = new THREE.SphereGeometry(0.025, 8, 8);
    var dotMat = new THREE.MeshBasicMaterial({ color: ACCENT });
    for (var k = 0; k < trailPoints.length; k += 8) {
      var d = new THREE.Mesh(dotGeo, dotMat);
      d.position.set(trailPoints[k].x, 0.05, trailPoints[k].y);
      trailGroup.add(d);
    }
    trailGroup.visible = layers.trail;
  }

  function pushTrail(pose) {
    if (!pose) return;
    var x = pose.x || 0, y = pose.y || 0;
    var last = trailPoints[trailPoints.length - 1];
    if (last && Math.hypot(last.x - x, last.y - y) < 0.04) return;
    trailPoints.push({ x: x, y: y, yaw: pose.yaw || 0 });
    if (trailPoints.length > MAX_TRAIL) trailPoints.shift();
    rebuildTrail();
  }

  function setPose(pose) {
    pose = pose || { x: 0, y: 0, yaw: 0 };
    robotGroup.position.set(pose.x || 0, 0, pose.y || 0);
    robotGroup.rotation.y = -(pose.yaw || 0);
    robotGroup.visible = layers.robot;
    poseXEl.textContent = (pose.x || 0).toFixed(2);
    poseYEl.textContent = (pose.y || 0).toFixed(2);
    poseYawEl.textContent = (pose.yaw || 0).toFixed(2);
    poseTrailEl.textContent = String(trailPoints.length);
    if (layers.follow) {
      target.set(pose.x || 0, 0.35, pose.y || 0);
      applyCamera();
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
    setMap: setMap,
    clear: function () {
      clearCells();
      trailPoints = [];
      rebuildTrail();
      currentMap = { domain_id: activeDomainId, res_m: 0.08, range_m: 3.5, pose: { x: 0, y: 0, yaw: 0 }, cells: [] };
      statsEl.textContent = 'cleared';
    },
    resetView: function () {
      spherical = { radius: 9.5, theta: 0.78, phi: 0.95 };
      target.set(robotGroup.position.x, 0.35, robotGroup.position.z);
      applyCamera();
    },
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
    getCurrentMap: function () { return currentMap; }
  };

  // ---- boot ---------------------------------------------------------------
  buildK1();
  Promise.all([
    loadTex('./assets/floor_diff.jpg').then(function (t) { floorTex = t; }),
    loadTex('./assets/metal_diff.jpg').then(function (t) { metalTex = t; })
  ]).then(function () {
    return refreshRegistry();
  }).catch(function () {
    domainTitle.textContent = 'sample';
    buildWarehouse();
    window.k1LocalMap.loadSample();
  });

  setInterval(function () {
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
    renderer.render(scene, camera);
  })();
})();
