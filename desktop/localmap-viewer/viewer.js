/* K1 Local Map — Three.js occupancy viewer (Tesla×SpaceX×Apple black/cyan) */
(function () {
  'use strict';

  var ACCENT = 0x32d4ff;
  var BG = 0x000000;
  var MUTED = 0x8e8e93;
  var CELL = 0x32d4ff;
  var ROBOT = 0xf5f5f7;

  var viewport = document.getElementById('viewport');
  var statsEl = document.getElementById('stats');
  var errEl = document.getElementById('err');

  var scene = new THREE.Scene();
  scene.background = new THREE.Color(BG);
  scene.fog = new THREE.FogExp2(BG, 0.045);

  var camera = new THREE.PerspectiveCamera(50, 1, 0.05, 80);
  camera.position.set(4.2, 3.6, 4.2);

  var renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setSize(viewport.clientWidth, viewport.clientHeight);
  viewport.appendChild(renderer.domElement);

  var ambient = new THREE.AmbientLight(0xffffff, 0.55);
  scene.add(ambient);
  var key = new THREE.DirectionalLight(0xffffff, 0.65);
  key.position.set(4, 8, 2);
  scene.add(key);

  // Ground grid (mission-control feel)
  var grid = new THREE.GridHelper(10, 20, MUTED, 0x1c1c1e);
  grid.position.y = 0;
  scene.add(grid);

  var ring = new THREE.Mesh(
    new THREE.RingGeometry(3.45, 3.52, 64),
    new THREE.MeshBasicMaterial({ color: ACCENT, transparent: true, opacity: 0.35, side: THREE.DoubleSide })
  );
  ring.rotation.x = -Math.PI / 2;
  ring.position.y = 0.01;
  scene.add(ring);

  var cellGroup = new THREE.Group();
  scene.add(cellGroup);

  var robotGroup = new THREE.Group();
  scene.add(robotGroup);
  var robotBody = new THREE.Mesh(
    new THREE.CylinderGeometry(0.12, 0.14, 0.55, 16),
    new THREE.MeshStandardMaterial({ color: ROBOT, metalness: 0.2, roughness: 0.55, emissive: ACCENT, emissiveIntensity: 0.15 })
  );
  robotBody.position.y = 0.28;
  robotGroup.add(robotBody);
  var nose = new THREE.Mesh(
    new THREE.ConeGeometry(0.08, 0.22, 10),
    new THREE.MeshStandardMaterial({ color: ACCENT, emissive: ACCENT, emissiveIntensity: 0.4 })
  );
  nose.rotation.x = Math.PI / 2;
  nose.position.set(0, 0.28, 0.22);
  robotGroup.add(nose);

  // Orbit controls (lightweight, no OrbitControls dependency)
  var target = new THREE.Vector3(0, 0.2, 0);
  var spherical = { radius: 6.2, theta: 0.85, phi: 0.95 };
  var dragging = false;
  var panning = false;
  var lastX = 0, lastY = 0;
  var followPose = true;
  var showRobot = true;

  function applyCamera() {
    var x = target.x + spherical.radius * Math.sin(spherical.phi) * Math.sin(spherical.theta);
    var y = target.y + spherical.radius * Math.cos(spherical.phi);
    var z = target.z + spherical.radius * Math.sin(spherical.phi) * Math.cos(spherical.theta);
    camera.position.set(x, y, z);
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
    try { renderer.domElement.releasePointerCapture(e.pointerId); } catch (_) {}
  });
  renderer.domElement.addEventListener('pointermove', function (e) {
    var dx = e.clientX - lastX;
    var dy = e.clientY - lastY;
    lastX = e.clientX; lastY = e.clientY;
    if (dragging) {
      spherical.theta -= dx * 0.005;
      spherical.phi = Math.max(0.15, Math.min(Math.PI - 0.15, spherical.phi + dy * 0.005));
      applyCamera();
    } else if (panning) {
      var right = new THREE.Vector3();
      var up = new THREE.Vector3(0, 1, 0);
      camera.getWorldDirection(right);
      right.cross(up).normalize();
      var forward = new THREE.Vector3().crossVectors(up, right).normalize();
      var scale = spherical.radius * 0.0015;
      target.addScaledVector(right, -dx * scale);
      target.addScaledVector(forward, dy * scale);
      applyCamera();
    }
  });
  renderer.domElement.addEventListener('wheel', function (e) {
    e.preventDefault();
    spherical.radius = Math.max(1.2, Math.min(24, spherical.radius * (e.deltaY > 0 ? 1.08 : 0.92)));
    applyCamera();
  }, { passive: false });
  renderer.domElement.addEventListener('contextmenu', function (e) { e.preventDefault(); });

  function clearCells() {
    while (cellGroup.children.length) {
      var c = cellGroup.children.pop();
      if (c.geometry) c.geometry.dispose();
      if (c.material) c.material.dispose();
    }
  }

  function setMap(data) {
    if (!data) return;
    clearCells();
    var res = (data.res_m || 0.08);
    var cells = data.cells || [];
    var maxHits = 1;
    for (var i = 0; i < cells.length; i++) maxHits = Math.max(maxHits, cells[i].hits || 1);

    var geo = new THREE.BoxGeometry(res * 0.92, 1, res * 0.92);
    for (var j = 0; j < cells.length; j++) {
      var cell = cells[j];
      var hits = cell.hits || 1;
      var h = 0.08 + 0.55 * (hits / maxHits);
      var mat = new THREE.MeshStandardMaterial({
        color: CELL,
        emissive: ACCENT,
        emissiveIntensity: 0.15 + 0.55 * (hits / maxHits),
        metalness: 0.15,
        roughness: 0.4,
        transparent: true,
        opacity: 0.55 + 0.4 * (hits / maxHits)
      });
      var mesh = new THREE.Mesh(geo, mat);
      mesh.position.set(cell.x || 0, h / 2, cell.y || 0);
      mesh.scale.y = h;
      cellGroup.add(mesh);
    }

    var pose = data.pose || { x: 0, y: 0, yaw: 0 };
    robotGroup.visible = showRobot;
    robotGroup.position.set(pose.x || 0, 0, pose.y || 0);
    robotGroup.rotation.y = -(pose.yaw || 0);

    if (followPose) {
      target.set(pose.x || 0, 0.2, pose.y || 0);
      applyCamera();
    }

    var range = data.range_m || 3.5;
    ring.scale.set(range / 3.5, range / 3.5, 1);
    statsEl.textContent = cells.length + ' cells · res ' + res.toFixed(2) + ' m · range ' + range.toFixed(1) + ' m'
      + ' · pose (' + (pose.x || 0).toFixed(2) + ', ' + (pose.y || 0).toFixed(2) + ', yaw ' + (pose.yaw || 0).toFixed(2) + ')';
  }

  function showErr(msg) {
    if (!msg) { errEl.style.display = 'none'; errEl.textContent = ''; return; }
    errEl.style.display = 'block';
    errEl.textContent = msg;
  }

  function loadJson(url) {
    return fetch(url, { cache: 'no-store' }).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    });
  }

  // Public API for WinForms WebView2 / host interop
  window.k1LocalMap = {
    setMap: setMap,
    clear: function () { clearCells(); statsEl.textContent = 'cleared'; },
    resetView: function () {
      spherical = { radius: 6.2, theta: 0.85, phi: 0.95 };
      target.set(robotGroup.position.x, 0.2, robotGroup.position.z);
      applyCamera();
    },
    setFollowPose: function (on) { followPose = !!on; },
    setShowRobot: function (on) { showRobot = !!on; robotGroup.visible = showRobot; },
    loadSample: function () {
      return loadJson('./sample.json').then(setMap).catch(function (e) { showErr(String(e)); });
    },
    loadFeed: function (path) {
      return loadJson(path || './feed.json').then(function (d) { showErr(''); setMap(d); })
        .catch(function (e) { showErr('feed: ' + e); });
    }
  };

  // Auto-load sample, then poll feed.json if present
  window.k1LocalMap.loadSample();
  var feedTimer = setInterval(function () {
    loadJson('./feed.json').then(function (d) { showErr(''); setMap(d); }).catch(function () { /* optional */ });
  }, 1500);

  function tick() {
    requestAnimationFrame(tick);
    renderer.render(scene, camera);
  }
  tick();
})();
