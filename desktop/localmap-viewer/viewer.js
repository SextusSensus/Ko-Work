/* K1 Local Map — Three.js occupancy viewer + domain switcher */
(function () {
  'use strict';

  var ACCENT = 0x32d4ff;
  var BG = 0x000000;
  var MUTED = 0x8e8e93;
  var CELL = 0x32d4ff;
  var ROBOT = 0xf5f5f7;

  var DATA_ROOT = '../localmap-data';
  var DOMAINS_INDEX = DATA_ROOT + '/domains.json';

  var viewport = document.getElementById('viewport');
  var statsEl = document.getElementById('stats');
  var errEl = document.getElementById('err');
  var domainBar = document.getElementById('domain-bar');
  var domainTitle = document.getElementById('domain-title');
  var modal = document.getElementById('new-domain-modal');
  var newNameInput = document.getElementById('new-domain-name');

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

  var target = new THREE.Vector3(0, 0.2, 0);
  var spherical = { radius: 6.2, theta: 0.85, phi: 0.95 };
  var dragging = false;
  var panning = false;
  var lastX = 0, lastY = 0;
  var followPose = true;
  var showRobot = true;

  var registry = { active: null, domains: [] };
  var currentMap = null;
  var activeDomainId = null;

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

  function findDomain(id) {
    for (var i = 0; i < registry.domains.length; i++) {
      if (registry.domains[i].id === id) return registry.domains[i];
    }
    return null;
  }

  function formatUpdated(iso) {
    if (!iso) return '—';
    try {
      var d = new Date(iso);
      if (isNaN(d.getTime())) return iso;
      return d.toISOString().slice(0, 16).replace('T', ' ') + 'Z';
    } catch (_) { return iso; }
  }

  function setMap(data) {
    if (!data) return;
    currentMap = data;
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

    var meta = findDomain(activeDomainId);
    var metaBits = cells.length + ' cells · res ' + res.toFixed(2) + ' m · range ' + range.toFixed(1) + ' m'
      + ' · pose (' + (pose.x || 0).toFixed(2) + ', ' + (pose.y || 0).toFixed(2) + ', yaw ' + (pose.yaw || 0).toFixed(2) + ')';
    if (meta) {
      metaBits += ' · ' + (meta.run_count || 0) + ' runs · updated ' + formatUpdated(meta.updated);
    }
    statsEl.textContent = metaBits;
  }

  function showErr(msg) {
    if (!msg) { errEl.style.display = 'none'; errEl.textContent = ''; return; }
    errEl.style.display = 'block';
    errEl.textContent = msg;
  }

  function loadJson(url) {
    return fetch(url, { cache: 'no-store' }).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status + ' ' + url);
      return r.json();
    });
  }

  function occupancyUrl(id) {
    return DATA_ROOT + '/domains/' + encodeURIComponent(id) + '/occupancy.json';
  }

  function renderDomainChips() {
    while (domainBar.firstChild) domainBar.removeChild(domainBar.firstChild);
    var label = document.createElement('span');
    label.className = 'label';
    label.textContent = 'Domain';
    domainBar.appendChild(label);

    registry.domains.forEach(function (d) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'chip' + (d.id === activeDomainId ? ' active' : '');
      btn.setAttribute('role', 'tab');
      btn.setAttribute('aria-selected', d.id === activeDomainId ? 'true' : 'false');
      btn.dataset.domainId = d.id;
      btn.title = (d.name || d.id) + ' · ' + (d.cell_count || 0) + ' cells · ' + (d.run_count || 0) + ' runs · ' + formatUpdated(d.updated);
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
    var meta = findDomain(id);
    domainTitle.textContent = (meta && meta.name) ? meta.name : id;
    renderDomainChips();
    return loadJson(occupancyUrl(id)).then(function (d) {
      showErr('');
      if (!d.domain_id) d.domain_id = id;
      setMap(d);
      if (typeof window.k1LocalMapOnDomainChange === 'function') {
        try { window.k1LocalMapOnDomainChange(id); } catch (_) {}
      }
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
    // Prefer host bridge (WinForms writes disk); else optimistic in-memory for browser preview.
    if (typeof window.k1LocalMapHostCreateDomain === 'function') {
      try {
        window.k1LocalMapHostCreateDomain(JSON.stringify({ id: id, name: n }));
        closeNewDomainModal();
        return;
      } catch (_) {}
    }
    var now = new Date().toISOString();
    registry.domains.push({ id: id, name: n, updated: now, run_count: 0, cell_count: 0 });
    registry.active = id;
    activeDomainId = id;
    currentMap = { domain_id: id, res_m: 0.08, range_m: 3.5, pose: { x: 0, y: 0, yaw: 0 }, cells: [] };
    setMap(currentMap);
    domainTitle.textContent = n;
    renderDomainChips();
    closeNewDomainModal();
    showErr('Created in-memory (host will persist on Windows).');
    setTimeout(function () { showErr(''); }, 2500);
  }

  document.getElementById('new-domain-cancel').addEventListener('click', closeNewDomainModal);
  document.getElementById('new-domain-ok').addEventListener('click', function () {
    requestNewDomain(newNameInput.value);
  });
  newNameInput.addEventListener('keydown', function (e) {
    if (e.key === 'Enter') requestNewDomain(newNameInput.value);
    if (e.key === 'Escape') closeNewDomainModal();
  });
  modal.addEventListener('click', function (e) {
    if (e.target === modal) closeNewDomainModal();
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
      cells: []
    };
    var map = {};
    function absorb(src) {
      if (!src || !src.cells) return;
      for (var i = 0; i < src.cells.length; i++) {
        var c = src.cells[i];
        var k = cellKey(c, res);
        if (!map[k]) {
          map[k] = { x: c.x || 0, y: c.y || 0, hits: c.hits || 1 };
        } else {
          map[k].hits = (map[k].hits || 1) + (c.hits || 1);
          map[k].x = ((map[k].x || 0) + (c.x || 0)) / 2;
          map[k].y = ((map[k].y || 0) + (c.y || 0)) / 2;
        }
      }
    }
    absorb(base);
    absorb(incoming);
    for (var k in map) if (Object.prototype.hasOwnProperty.call(map, k)) out.cells.push(map[k]);
    return out;
  }

  // Public API for WinForms WebView2 / host interop
  window.k1LocalMap = {
    setMap: setMap,
    clear: function () {
      clearCells();
      currentMap = { domain_id: activeDomainId, res_m: 0.08, range_m: 3.5, pose: { x: 0, y: 0, yaw: 0 }, cells: [] };
      statsEl.textContent = 'cleared';
    },
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
    },
    getActiveDomain: function () { return activeDomainId; },
    listDomains: function () { return registry.domains.slice(); },
    setRegistry: function (reg) { return setRegistry(reg || { active: null, domains: [] }); },
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

  // Boot: domains registry → active occupancy; fall back to sample
  refreshRegistry().catch(function () {
    domainTitle.textContent = 'sample';
    window.k1LocalMap.loadSample();
  });

  // Optional live feed poll (host may sync active domain → feed.json)
  setInterval(function () {
    if (!activeDomainId) return;
    loadJson('./feed.json').then(function (d) {
      if (d && d.domain_id && d.domain_id !== activeDomainId) return;
      showErr('');
      setMap(d);
    }).catch(function () { /* optional */ });
  }, 2000);

  function tick() {
    requestAnimationFrame(tick);
    renderer.render(scene, camera);
  }
  tick();
})();
