/* K1 Local Map — catalog-driven asset placement
 * Future loop: domain create → robot map runs → labeling detections → assignAsset → instances.json
 * Live robot labeling will call the same window.k1LocalMap.registerDetections / assignAsset API.
 */
(function () {
  'use strict';

  var DATA_ROOT = '../localmap-data';
  var CATALOG_URL = './assets/catalog.json';
  var ONTOLOGY_URL = DATA_ROOT + '/asset-ontology.json';
  var GLOBAL_ASSETS_URL = DATA_ROOT + '/global-assets.json';

  var catalog = { version: 0, assets: [] };
  var ontology = { version: 0, label_classes: [] };
  var globalAssets = { version: 0, scope: 'global', assets: [], label_aliases: {} };
  var assetById = {};
  var labelByKey = {};
  var meshCache = {};
  var instanceGroup = new THREE.Group();
  instanceGroup.name = 'assetInstances';
  var instances = [];
  var activeDomainId = null;
  /** Bumps on every domain instance load so a slower prior fetch cannot place into the new domain. */
  var instanceLoadGen = 0;
  var ready = false;
  var gltfLoader = null;

  function ensureLoader() {
    if (gltfLoader) return gltfLoader;
    if (typeof THREE.GLTFLoader === 'function') {
      gltfLoader = new THREE.GLTFLoader();
    }
    return gltfLoader;
  }

  function loadJson(url) {
    return fetch(url, { cache: 'no-store' }).then(function (r) {
      if (!r.ok) throw new Error(url + ' ' + r.status);
      return r.json();
    });
  }

  function stdMat(color, opts) {
    opts = opts || {};
    var mat = new THREE.MeshStandardMaterial({
      color: color,
      metalness: opts.metalness != null ? opts.metalness : 0.15,
      roughness: opts.roughness != null ? opts.roughness : 0.55,
      emissive: opts.emissive != null ? opts.emissive : 0x000000,
      emissiveIntensity: opts.emissiveIntensity || 0
    });
    if (opts.transparent) {
      mat.transparent = true;
      mat.opacity = opts.opacity != null ? opts.opacity : 0.5;
      mat.depthWrite = false;
    }
    return mat;
  }

  function box(w, h, d, mat) {
    var m = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), mat);
    m.castShadow = true;
    m.receiveShadow = true;
    return m;
  }

  function cyl(rTop, rBot, h, mat, seg) {
    var m = new THREE.Mesh(new THREE.CylinderGeometry(rTop, rBot, h, seg || 16), mat);
    m.castShadow = true;
    return m;
  }

  /** Procedural recipes — used when status=procedural or GLTF fails */
  function buildProcedural(kind, scale) {
    var g = new THREE.Group();
    var sx = (scale && scale[0]) || 1;
    var sy = (scale && scale[1]) || 1;
    var sz = (scale && scale[2]) || 1;
    var wood = stdMat(0x6b542e, { roughness: 0.8, metalness: 0.02 });
    var metal = stdMat(0x8a96a4, { metalness: 0.65, roughness: 0.35 });
    var dark = stdMat(0x2a2a2e, { roughness: 0.55, metalness: 0.2 });
    var light = stdMat(0xe8e8ea, { metalness: 0.45, roughness: 0.3 });
    var safety = stdMat(0xc9a227, { metalness: 0.3, roughness: 0.4, emissive: 0xc9a227, emissiveIntensity: 0.05 });
    var carton = stdMat(0x6e5b45, { roughness: 0.85 });
    // Procedural stand-ins use metal / soft white — never HUD cyan plastic
    var accent = stdMat(0xc8cdd2, { metalness: 0.2, roughness: 0.45, emissive: 0xc8cdd2, emissiveIntensity: 0.06 });
    var plastic = stdMat(0x5a626c, { roughness: 0.5, metalness: 0.18 });

    function add(mesh, x, y, z) {
      mesh.position.set(x || 0, y || 0, z || 0);
      g.add(mesh);
    }

    switch (String(kind || '')) {
      case 'fridge':
        add(box(sx, sy, sz, light), 0, sy / 2, 0);
        add(box(sx * 0.02, sy * 0.45, sz * 0.9, metal), sx * 0.48, sy * 0.7, 0);
        add(box(sx * 0.02, sy * 0.4, sz * 0.9, metal), sx * 0.48, sy * 0.28, 0);
        break;
      case 'counter':
        add(box(sx, sy * 0.85, sz, dark), 0, sy * 0.42, 0);
        add(box(sx * 1.02, sy * 0.05, sz * 1.05, stdMat(0xd8d4cc, { roughness: 0.4 })), 0, sy * 0.9, 0);
        break;
      case 'cabinet':
        add(box(sx, sy, sz, dark), 0, sy / 2, 0);
        add(box(sx * 0.9, sy * 0.02, sz * 0.05, metal), 0, sy * 0.55, sz * 0.48);
        break;
      case 'stove':
        add(box(sx, sy * 0.75, sz, dark), 0, sy * 0.38, 0);
        add(box(sx * 0.95, sy * 0.04, sz * 0.95, metal), 0, sy * 0.78, 0);
        [[-0.25, -0.2], [0.25, -0.2], [-0.25, 0.2], [0.25, 0.2]].forEach(function (p) {
          add(cyl(sx * 0.12, sx * 0.12, 0.03, stdMat(0x1a1a1c)), p[0] * sx, sy * 0.82, p[1] * sz);
        });
        break;
      case 'table':
        add(box(sx, 0.05, sz, wood), 0, sy, 0);
        [[-1, -1], [-1, 1], [1, -1], [1, 1]].forEach(function (c) {
          add(box(0.06, sy, 0.06, wood), c[0] * sx * 0.42, sy / 2, c[1] * sz * 0.42);
        });
        break;
      case 'chair':
        add(box(sx, 0.06, sz, wood), 0, sy * 0.45, 0);
        add(box(sx, sy * 0.5, 0.05, wood), 0, sy * 0.75, -sz * 0.4);
        [[-1, -1], [-1, 1], [1, -1], [1, 1]].forEach(function (c) {
          add(box(0.04, sy * 0.45, 0.04, wood), c[0] * sx * 0.4, sy * 0.22, c[1] * sz * 0.4);
        });
        break;
      case 'stool':
        add(cyl(sx * 0.35, sx * 0.35, 0.05, wood), 0, sy, 0);
        add(cyl(0.04, 0.05, sy, wood), 0, sy / 2, 0);
        break;
      case 'microwave':
        add(box(sx, sy, sz, dark), 0, sy / 2, 0);
        add(box(sx * 0.55, sy * 0.55, 0.02, stdMat(0x111111, { metalness: 0.8, roughness: 0.2 })), 0, sy / 2, sz * 0.51);
        break;
      case 'trash':
        add(cyl(sx * 0.35, sx * 0.4, sy, stdMat(0x1c1c1e, { roughness: 0.7 })), 0, sy / 2, 0);
        break;
      case 'door_frame':
        add(box(0.08, sy, 0.08, dark), -sx / 2, sy / 2, 0);
        add(box(0.08, sy, 0.08, dark), sx / 2, sy / 2, 0);
        add(box(sx + 0.1, 0.08, 0.08, dark), 0, sy, 0);
        break;
      case 'person':
        add(cyl(sx * 0.22, sx * 0.25, sy * 0.55, stdMat(0x3a4550)), 0, sy * 0.35, 0);
        add(new THREE.Mesh(new THREE.SphereGeometry(sx * 0.18, 12, 12), stdMat(0xd4b89a)), 0, sy * 0.82, 0);
        break;
      case 'cone':
        add(cyl(0.02, sx * 0.35, sy, stdMat(0xff6a00, { roughness: 0.45 })), 0, sy / 2, 0);
        add(box(sx * 0.7, 0.03, sx * 0.7, stdMat(0x222222)), 0, 0.02, 0);
        break;
      case 'sign_wet':
        add(box(sx * 0.08, sy, sz * 0.08, safety), 0, sy / 2, 0);
        add(box(sx, sy * 0.45, 0.04, safety), 0, sy * 0.7, 0);
        break;
      case 'hanging_light':
        add(cyl(0.08, 0.12, 0.25, metal), 0, 0, 0);
        add(cyl(0.02, 0.02, sy || 0.8, metal), 0, (sy || 0.8) / 2 + 0.1, 0);
        break;
      case 'sofa':
        add(box(sx, sy * 0.4, sz, stdMat(0x4a5560)), 0, sy * 0.25, 0);
        add(box(sx, sy * 0.45, 0.15, stdMat(0x3a4450)), 0, sy * 0.55, -sz * 0.4);
        break;
      case 'plant':
        add(cyl(sx * 0.3, sx * 0.25, sy * 0.35, stdMat(0x8a5a3a)), 0, sy * 0.18, 0);
        add(new THREE.Mesh(new THREE.SphereGeometry(sx * 0.4, 10, 10), stdMat(0x2d6a3a, { roughness: 0.9 })), 0, sy * 0.65, 0);
        break;
      case 'pillar':
        add(box(sx, sy, sz, metal), 0, sy / 2, 0);
        break;
      case 'wall':
        add(box(sx, sy, sz, stdMat(0xb0b4b8, { roughness: 0.9 })), 0, sy / 2, 0);
        break;
      case 'stairs':
        for (var i = 0; i < 8; i++) {
          var t = (i + 1) / 8;
          add(box(sx, sy / 8, sz / 8, wood), 0, (i + 0.5) * (sy / 8), -sz / 2 + (i + 0.5) * (sz / 8));
        }
        break;
      case 'elevator':
        add(box(sx, sy, sz, metal), 0, sy / 2, 0);
        add(box(sx * 0.4, sy * 0.85, 0.02, accent), -sx * 0.22, sy / 2, sz * 0.55);
        add(box(sx * 0.4, sy * 0.85, 0.02, accent), sx * 0.22, sy / 2, sz * 0.55);
        break;
      case 'desk':
        add(box(sx, 0.05, sz, wood), 0, sy * 0.72, 0);
        add(box(sx * 0.95, sy * 0.55, sz * 0.08, dark), 0, sy * 0.35, -sz * 0.4);
        [[-1, -1], [-1, 1], [1, -1], [1, 1]].forEach(function (c) {
          add(box(0.05, sy * 0.7, 0.05, metal), c[0] * sx * 0.42, sy * 0.35, c[1] * sz * 0.4);
        });
        break;
      case 'shelf':
      case 'bookcase':
        add(box(sx, sy, sz * 0.15, wood), 0, sy / 2, -sz * 0.35);
        for (var sh = 0; sh < 5; sh++) {
          add(box(sx * 0.95, 0.03, sz * 0.85, wood), 0, 0.15 + sh * (sy - 0.25) / 4, 0);
        }
        break;
      case 'cubicle': {
        // Fabric/plaster partitions + wood worksurface at ~0.75 m (not floating white slab)
        var part = stdMat(0xb8bcc0, { roughness: 0.88, metalness: 0.04 });
        var desk = wood;
        var partH = Math.min(sy * 0.85, 1.25);
        add(box(sx, partH, 0.05, part), 0, partH / 2, -sz * 0.48);
        add(box(0.05, partH, sz, part), -sx * 0.48, partH / 2, 0);
        add(box(sx * 0.12, 0.04, sz * 0.12, metal), -sx * 0.35, 0.72, sz * 0.2);
        add(box(sx * 0.12, 0.04, sz * 0.12, metal), sx * 0.35, 0.72, sz * 0.2);
        add(box(sx * 0.85, 0.05, sz * 0.7, desk), 0, 0.75, -sz * 0.05);
        break;
      }
      case 'window':
        add(box(sx, sy, sz, metal), 0, sy / 2, 0);
        add(box(sx * 0.85, sy * 0.75, 0.02, stdMat(0xa8c8e8, {
          transparent: true, opacity: 0.45, metalness: 0.1, roughness: 0.15
        })), 0, sy / 2, sz * 0.4);
        add(box(0.04, sy * 0.75, 0.03, metal), 0, sy / 2, sz * 0.35);
        break;
      case 'water_cooler':
        add(box(sx * 0.7, sy * 0.55, sz * 0.7, light), 0, sy * 0.28, 0);
        add(cyl(sx * 0.28, sx * 0.28, sy * 0.4, stdMat(0x7ec8e8, {
          transparent: true, opacity: 0.55, roughness: 0.2
        })), 0, sy * 0.75, 0);
        add(box(sx * 0.15, 0.08, sz * 0.15, metal), sx * 0.25, sy * 0.45, sz * 0.35);
        break;
      case 'exit_sign':
        add(box(sx, sy, sz, stdMat(0x1a3a28, { metalness: 0.2, roughness: 0.4 })), 0, sy / 2, 0);
        add(box(sx * 0.85, sy * 0.55, 0.02, stdMat(0x3cff8a, {
          emissive: 0x3cff8a, emissiveIntensity: 0.55, metalness: 0.1, roughness: 0.35
        })), 0, sy / 2, sz * 0.6);
        break;
      case 'hallway_carpet':
        add(box(sx, Math.max(sy, 0.02), sz, stdMat(0x5a4a3a, { roughness: 0.95, metalness: 0.02 })), 0, 0.01, 0);
        break;
      case 'ceiling_tile':
        add(box(sx, Math.max(sy, 0.02), sz, light), 0, 0, 0);
        break;
      case 'signage':
        add(box(sx, sy, sz, dark), 0, sy / 2, 0);
        add(box(sx * 0.85, sy * 0.55, 0.02, accent), 0, sy / 2, sz * 0.52);
        break;
      case 'pallet_rack':
        [[-1, -1], [-1, 1], [1, -1], [1, 1]].forEach(function (c) {
          add(box(0.07, sy, 0.07, metal), c[0] * sx * 0.45, sy / 2, c[1] * sz * 0.45);
        });
        for (var lv = 0; lv < 4; lv++) {
          var y = 0.2 + lv * (sy - 0.3) / 3;
          add(box(sx, 0.04, sz, dark), 0, y, 0);
          add(box(0.04, 0.05, sz, safety), -sx / 2, y, 0);
          add(box(0.04, 0.05, sz, safety), sx / 2, y, 0);
        }
        break;
      case 'pallet':
        add(box(sx, sy, sz, wood), 0, sy / 2, 0);
        for (var s = -1; s <= 1; s++) add(box(sx, sy * 0.6, 0.08, wood), 0, sy * 0.3, s * sz * 0.35);
        break;
      case 'box':
        add(box(sx, sy, sz, carton), 0, sy / 2, 0);
        break;
      case 'crate':
        add(box(sx, sy, sz, wood), 0, sy / 2, 0);
        add(box(sx * 0.9, sy * 0.05, sz * 0.9, dark), 0, sy * 0.95, 0);
        break;
      case 'tote':
        add(box(sx, sy, sz, plastic), 0, sy / 2, 0);
        break;
      case 'barrel':
        add(cyl(sx * 0.45, sx * 0.45, sy, stdMat(0x3a6a3a, { metalness: 0.4 })), 0, sy / 2, 0);
        break;
      case 'hand_truck':
        add(box(sx * 0.15, sy, 0.05, metal), 0, sy / 2, -sz * 0.35);
        add(box(sx, 0.05, sz * 0.5, metal), 0, 0.08, 0);
        add(cyl(0.08, 0.08, 0.05, dark, 12), -sx * 0.35, 0.08, -sz * 0.15);
        add(cyl(0.08, 0.08, 0.05, dark, 12), sx * 0.35, 0.08, -sz * 0.15);
        break;
      case 'dock_door':
        add(box(sx, sy, sz, dark), 0, sy / 2, 0);
        for (var sl = 0; sl < 10; sl++) {
          add(box(sx * 0.95, sy * 0.06, 0.02, metal), 0, (sl + 0.5) * (sy / 10), sz * 0.55);
        }
        break;
      case 'conveyor':
        add(box(sx, sy * 0.25, sz, dark), 0, sy * 0.35, 0);
        add(box(sx * 0.95, 0.04, sz * 0.85, metal), 0, sy * 0.5, 0);
        add(box(0.05, sy * 0.4, sz, safety), -sx / 2, sy * 0.45, 0);
        add(box(0.05, sy * 0.4, sz, safety), sx / 2, sy * 0.45, 0);
        break;
      case 'conveyor_curve':
        add(box(sx, sy * 0.25, sz * 0.55, dark), 0, sy * 0.35, -sz * 0.2);
        add(box(sx * 0.55, sy * 0.25, sz, dark), sx * 0.2, sy * 0.35, 0);
        add(box(0.05, sy * 0.35, sz * 0.55, safety), -sx / 2, sy * 0.4, -sz * 0.2);
        break;
      case 'conveyor_elevated':
        add(box(sx, 0.08, sz, dark), 0, sy * 0.75, 0);
        [[-1, -1], [-1, 1], [1, -1], [1, 1]].forEach(function (c) {
          add(box(0.07, sy * 0.75, 0.07, metal), c[0] * sx * 0.4, sy * 0.38, c[1] * sz * 0.4);
        });
        add(box(sx * 0.95, 0.04, 0.06, safety), 0, sy * 0.8, 0);
        break;
      case 'assembly_station':
        add(box(sx, 0.06, sz, metal), 0, sy * 0.85, 0);
        [[-1, -1], [-1, 1], [1, -1], [1, 1]].forEach(function (c) {
          add(box(0.06, sy * 0.85, 0.06, metal), c[0] * sx * 0.42, sy * 0.42, c[1] * sz * 0.4);
        });
        add(box(sx * 0.9, sy * 0.3, sz * 0.75, dark), 0, sy * 0.3, 0);
        add(box(sx * 0.25, 0.04, sz * 0.3, safety), -sx * 0.3, sy * 0.9, 0);
        break;
      case 'robotic_arm':
        add(box(sx * 0.7, 0.1, sz * 0.7, metal), 0, 0.05, 0);
        add(cyl(sx * 0.22, sx * 0.22, sy * 0.28, dark), 0, sy * 0.2, 0);
        add(box(sx * 0.2, sx * 0.2, sz * 0.7, safety), 0, sy * 0.45, sz * 0.2);
        add(box(sx * 0.15, sx * 0.15, sz * 0.55, safety), sx * 0.05, sy * 0.55, sz * 0.45);
        add(box(sx * 0.25, 0.05, 0.08, metal), 0, sy * 0.5, sz * 0.7);
        break;
      case 'parts_bin':
        add(box(sx, sy, sz, plastic), 0, sy / 2, 0);
        add(box(sx * 0.9, 0.04, sz * 0.9, dark), 0, sy * 0.95, 0);
        break;
      case 'safety_fence':
        add(box(0.06, sy, 0.06, metal), -sx / 2, sy / 2, 0);
        add(box(0.06, sy, 0.06, metal), sx / 2, sy / 2, 0);
        add(box(sx, 0.08, 0.06, safety), 0, sy, 0);
        add(box(sx * 0.95, sy * 0.75, 0.03, stdMat(0x9aa7b5, { metalness: 0.5, roughness: 0.35, transparent: true, opacity: 0.35 })), 0, sy * 0.5, 0);
        break;
      case 'control_panel':
        add(box(sx * 0.7, sy * 0.75, sz * 0.6, metal), 0, sy * 0.38, 0);
        add(box(sx, sy * 0.35, 0.08, dark), 0, sy * 0.9, sz * 0.15);
        add(box(sx * 0.75, sy * 0.22, 0.03, accent), 0, sy * 0.92, sz * 0.22);
        break;
      case 'overhead_gantry':
        add(box(sx, 0.12, 0.12, metal), 0, sy * 0.9, 0);
        add(box(0.12, sy * 0.9, 0.12, metal), -sx * 0.45, sy * 0.45, 0);
        add(box(0.12, sy * 0.9, 0.12, metal), sx * 0.45, sy * 0.45, 0);
        add(box(0.4, 0.25, sz * 0.5, safety), 0, sy * 0.82, 0);
        break;
      case 'bollard':
        add(cyl(sx * 0.5, sx * 0.5, sy, safety), 0, sy / 2, 0);
        break;
      case 'floor_tape':
        add(box(sx, Math.max(sy, 0.01), sz, safety), 0, 0.01, 0);
        break;
      case 'forklift':
        add(box(sx, sy * 0.35, sz * 0.7, safety), 0, sy * 0.3, -sz * 0.05);
        add(box(sx * 0.5, sy * 0.45, sz * 0.4, dark), 0, sy * 0.55, -sz * 0.15);
        add(box(0.08, sy * 0.7, 0.08, metal), -sx * 0.2, sy * 0.45, sz * 0.35);
        add(box(0.08, sy * 0.7, 0.08, metal), sx * 0.2, sy * 0.45, sz * 0.35);
        add(box(sx * 0.55, 0.05, 0.08, metal), 0, sy * 0.2, sz * 0.45);
        break;
      case 'tv':
        add(box(sx, sy, sz, dark), 0, sy / 2, 0);
        add(box(sx * 0.9, sy * 0.85, 0.02, accent), 0, sy / 2, sz * 0.55);
        break;
      case 'laptop':
        add(box(sx, sy, sz, dark), 0, sy / 2, 0);
        add(box(sx, sy * 8, 0.02, dark), 0, sy * 5, -sz * 0.45);
        break;
      case 'clock':
        add(cyl(sx * 0.45, sx * 0.45, sz, light, 24), 0, 0, 0);
        break;
      case 'book':
        add(box(sx, sy, sz, stdMat(0x6a3040)), 0, sy / 2, 0);
        add(box(sx * 0.95, sy * 0.9, sz * 0.9, stdMat(0x304060)), 0, sy * 1.4, 0);
        break;
      case 'bed':
        add(box(sx, sy * 0.35, sz, wood), 0, sy * 0.2, 0);
        add(box(sx * 0.95, sy * 0.25, sz * 0.95, stdMat(0xd8dce2)), 0, sy * 0.45, 0);
        break;
      case 'toilet':
        add(box(sx, sy * 0.45, sz * 0.7, light), 0, sy * 0.25, 0);
        add(box(sx * 0.7, sy * 0.5, sz * 0.35, light), 0, sy * 0.65, -sz * 0.25);
        break;
      case 'k1_robot':
        add(box(sx, sy * 0.55, sz, dark), 0, sy * 0.4, 0);
        add(box(sx * 0.7, sy * 0.35, sz * 0.7, accent), 0, sy * 0.78, 0);
        add(box(sx * 0.15, sy * 0.35, sz * 0.15, metal), -sx * 0.55, sy * 0.55, 0);
        add(box(sx * 0.15, sy * 0.35, sz * 0.15, metal), sx * 0.55, sy * 0.55, 0);
        break;
      case 'barrier':
        add(box(sx, sy * 0.55, sz, stdMat(0xc8cdd2, { roughness: 0.85 })), 0, sy * 0.3, 0);
        add(box(sx * 0.95, sy * 0.12, sz * 1.05, safety), 0, sy * 0.55, 0);
        break;
      case 'fence':
        for (var p = -1; p <= 1; p += 2) {
          add(box(0.06, sy, 0.06, metal), p * sx * 0.48, sy / 2, 0);
        }
        add(box(sx, 0.04, 0.04, metal), 0, sy * 0.85, 0);
        add(box(sx, 0.04, 0.04, metal), 0, sy * 0.45, 0);
        add(box(sx, 0.04, 0.04, metal), 0, sy * 0.15, 0);
        for (var bar = 0; bar < 6; bar++) {
          var bx = -sx * 0.4 + bar * (sx * 0.8 / 5);
          add(box(0.03, sy * 0.7, 0.03, metal), bx, sy * 0.5, 0);
        }
        break;
      case 'robot_arm':
        add(cyl(sx * 0.35, sx * 0.4, sy * 0.15, dark), 0, sy * 0.08, 0);
        add(box(sx * 0.2, sy * 0.55, sx * 0.2, metal), 0, sy * 0.4, 0);
        add(box(sx * 0.75, sx * 0.15, sx * 0.15, safety), sx * 0.25, sy * 0.7, 0);
        add(box(sx * 0.12, sy * 0.35, sx * 0.12, metal), sx * 0.55, sy * 0.55, 0);
        add(box(sx * 0.25, 0.08, 0.08, accent), sx * 0.55, sy * 0.35, 0);
        break;
      case 'station':
        add(box(sx, sy * 0.15, sz, dark), 0, sy * 0.1, 0);
        add(box(sx * 0.7, sy * 0.7, sz * 0.7, metal), 0, sy * 0.5, 0);
        add(box(sx * 0.85, sy * 0.08, sz * 0.85, safety), 0, sy * 0.88, 0);
        break;
      case 'shrink_wrap':
        add(box(sx, sy * 0.12, sz, wood), 0, sy * 0.06, 0);
        add(box(sx * 0.9, sy * 0.7, sz * 0.9, carton), 0, sy * 0.5, 0);
        add(box(sx * 0.95, sy * 0.75, sz * 0.95, stdMat(0xb8d4e8, {
          transparent: true, opacity: 0.35, metalness: 0.05, roughness: 0.25
        })), 0, sy * 0.52, 0);
        break;
      default:
        add(box(sx, sy, sz, metal), 0, sy / 2, 0);
        break;
    }
    return g;
  }

  function indexCatalog(cat) {
    catalog = cat || catalog;
    assetById = {};
    (catalog.assets || []).forEach(function (a) { assetById[a.id] = a; });
  }

  function indexGlobalAssets(ga) {
    globalAssets = ga || globalAssets;
    (globalAssets.assets || []).forEach(function (a) {
      if (a && a.id && !assetById[a.id]) assetById[a.id] = a;
    });
    var aliases = globalAssets.label_aliases || {};
    Object.keys(aliases).forEach(function (key) {
      var entry = aliases[key];
      if (!entry) return;
      var canonId = entry.alias_of || entry.label_class || key;
      var k = String(key).toLowerCase();
      if (labelByKey[k]) return;
      if (labelByKey[String(canonId).toLowerCase()]) {
        labelByKey[k] = labelByKey[String(canonId).toLowerCase()];
      } else if (entry.default_asset) {
        labelByKey[k] = {
          id: canonId,
          default_asset: entry.default_asset,
          scale_m: entry.scale_m,
          placement: entry.placement || 'footprint',
          domain_assets: entry.domain_assets || {},
          scope: 'global'
        };
      }
    });
  }

  function indexOntology(ont) {
    ontology = ont || ontology;
    labelByKey = {};
    (ontology.label_classes || []).forEach(function (lc) {
      labelByKey[String(lc.id).toLowerCase()] = lc;
      if (lc.coco_id != null) labelByKey['coco:' + lc.coco_id] = lc;
      (lc.aliases || []).forEach(function (al) {
        labelByKey[String(al).toLowerCase()] = lc;
      });
    });
  }

  function resolveLabel(labelClass) {
    if (labelClass == null) return null;
    if (typeof labelClass === 'number') return labelByKey['coco:' + labelClass] || null;
    var s = String(labelClass).toLowerCase().replace(/\s+/g, '_');
    return labelByKey[s] || labelByKey[s.replace(/-/g, '_')] || null;
  }

  function resolveAsset(labelClass, domainId) {
    var lc = resolveLabel(labelClass);
    if (!lc) return null;
    var assetId = lc.default_asset;
    // Domain-specific override (office desks/chairs) — else global catalog default
    if (lc.domain_assets && domainId && lc.domain_assets[domainId]) {
      assetId = lc.domain_assets[domainId];
    }
    return { label: lc, asset: assetById[assetId] || null, assetId: assetId };
  }

  function loadGltf(path) {
    return new Promise(function (resolve, reject) {
      var loader = ensureLoader();
      if (!loader) {
        reject(new Error('GLTFLoader missing'));
        return;
      }
      loader.load(path, function (gltf) {
        resolve(gltf.scene || gltf.scenes[0]);
      }, undefined, reject);
    });
  }

  function fitObjectToScale(obj, scaleM) {
    if (!scaleM) return;
    var box3 = new THREE.Box3().setFromObject(obj);
    var size = new THREE.Vector3();
    box3.getSize(size);
    if (size.x < 1e-4 || size.y < 1e-4 || size.z < 1e-4) return;
    var sx = scaleM[0] / size.x;
    var sy = scaleM[1] / size.y;
    var sz = scaleM[2] / size.z;
    // Uniform-ish: prefer footprint XZ average for furniture, keep Y
    var s = Math.min(sx, sz);
    obj.scale.set(s, sy, s);
    box3.setFromObject(obj);
    var min = box3.min;
    obj.position.y -= min.y;
  }

  function instantiateAsset(asset, scaleM) {
    var key = (asset && asset.id) || 'unknown';
    var scaleKey = (scaleM || []).join(',');
    var cacheKey = key + '|' + scaleKey;

    function fromProcedural(kind) {
      var root = buildProcedural(kind, scaleM);
      root.userData.assetId = key;
      root.userData.procedural = kind;
      return root;
    }

    if (!asset) return Promise.resolve(fromProcedural('box'));

    if (asset.status === 'procedural' || !asset.path) {
      return Promise.resolve(fromProcedural(asset.procedural || asset.fallback_procedural || 'box'));
    }

    if (meshCache[cacheKey]) {
      return Promise.resolve(meshCache[cacheKey].clone(true));
    }

    return loadGltf('./' + asset.path.replace(/^\.\//, '')).then(function (scene) {
      var root = new THREE.Group();
      root.add(scene);
      fitObjectToScale(root, scaleM);
      root.userData.assetId = key;
      meshCache[cacheKey] = root;
      return root.clone(true);
    }).catch(function () {
      return fromProcedural(asset.fallback_procedural || asset.procedural || 'box');
    });
  }

  function yawFromPoseMap(poseMap) {
    if (!poseMap) return null;
    if (poseMap.yaw != null) return poseMap.yaw;
    var qw = poseMap.qw, qx = poseMap.qx || 0, qy = poseMap.qy || 0, qz = poseMap.qz;
    if (qw == null || qz == null) return null;
    return Math.atan2(2 * (qw * qz + qx * qy), 1 - 2 * (qy * qy + qz * qz));
  }

  function normalizePose(pose) {
    pose = pose || {};
    var pm = pose.pose_map;
    var x = pose.x, y = pose.y, z = pose.z, yaw = pose.yaw;
    if (pm) {
      if (x == null && pm.x != null) x = pm.x;
      if (y == null && pm.y != null) y = pm.y;
      if (z == null && pm.z != null) z = pm.z;
      if (yaw == null) yaw = yawFromPoseMap(pm);
    }
    return {
      x: x || 0,
      y: y != null ? y : 0,
      z: z != null ? z : 0,
      yaw: yaw || 0,
      w: pose.w,
      h: pose.h,
      pose_map: pm || null,
      use_measured_z: !!pose.use_measured_z,
      placement: pose.placement,
      detection_id: pose.detection_id,
      run_id: pose.run_id,
      confidence: pose.confidence
    };
  }

  /** Map FLU (x,y) → Three.js (x,z); yaw_three = -yaw_map. Footprint assets sit on floor. */
  function placeNode(node, pose) {
    var p = normalizePose(pose);
    node.position.x = p.x;
    node.position.z = p.y;
    var lift = (p.use_measured_z || p.placement === 'free') ? p.z : 0;
    node.position.y = lift || 0;
    if (p.yaw != null) node.rotation.y = -p.yaw;
    instanceGroup.add(node);
  }

  function instanceKey(rec) {
    if (rec.run_id && rec.detection_id) return String(rec.run_id) + '::' + String(rec.detection_id);
    if (rec.id) return String(rec.id);
    return null;
  }

  function removeInstanceAt(idx) {
    var old = instances[idx];
    if (old && old._node && old._node.parent) old._node.parent.remove(old._node);
    instances.splice(idx, 1);
  }

  function recordInstance(rec, node) {
    var key = instanceKey(rec);
    if (key) {
      for (var i = instances.length - 1; i >= 0; i--) {
        if (instanceKey(instances[i]) === key || instances[i].id === key) {
          removeInstanceAt(i);
        }
      }
    }
    var entry = {
      id: rec.id || key || ('inst-' + Date.now() + '-' + Math.floor(Math.random() * 1e4)),
      detection_id: rec.detection_id,
      label_class: rec.label_class,
      asset_id: rec.asset_id,
      x: rec.x || 0,
      y: rec.y || 0,
      z: rec.z || 0,
      yaw: rec.yaw || 0,
      pose_map: rec.pose_map || null,
      w: rec.w,
      h: rec.h,
      confidence: rec.confidence,
      covariance: rec.covariance,
      run_id: rec.run_id,
      t_ns: rec.t_ns,
      T_source: rec.T_source,
      placement_method: rec.placement_method,
      domain_id: activeDomainId,
      source: rec.source || 'assign'
    };
    entry._node = node;
    instances.push(entry);
    return entry;
  }

  function clearInstances() {
    // Invalidate in-flight assignAsset / loadInstances placements (GLTF resolve races).
    instanceLoadGen += 1;
    while (instanceGroup.children.length) {
      instanceGroup.remove(instanceGroup.children[0]);
    }
    instances = [];
  }

  function persistLocal() {
    if (!activeDomainId) return;
    try {
      var payload = {
        domain_id: activeDomainId,
        updated: new Date().toISOString(),
        instances: instances.map(function (i) {
          return {
            id: i.id,
            detection_id: i.detection_id,
            label_class: i.label_class,
            asset_id: i.asset_id,
            x: i.x, y: i.y, z: i.z, yaw: i.yaw,
            pose_map: i.pose_map,
            w: i.w, h: i.h,
            confidence: i.confidence,
            covariance: i.covariance,
            run_id: i.run_id,
            t_ns: i.t_ns,
            T_source: i.T_source,
            placement_method: i.placement_method,
            source: i.source
          };
        })
      };
      localStorage.setItem('k1LocalMap.instances.' + activeDomainId, JSON.stringify(payload));
      if (typeof window.k1LocalMapHostSaveInstances === 'function') {
        try { window.k1LocalMapHostSaveInstances(JSON.stringify(payload)); } catch (e) {}
      }
      return payload;
    } catch (e) {
      return null;
    }
  }

  function assignAsset(labelClass, pose, extras) {
    extras = extras || {};
    pose = normalizePose(pose || {});
    // Capture generation + domain before any async GLTF work so a later
    // clearInstances / switchDomain cannot leave stale racks on an empty domain.
    var expectGen = extras.gen != null ? extras.gen : instanceLoadGen;
    var expectDomain = extras.domain_id || activeDomainId;
    var resolved = resolveAsset(labelClass, expectDomain || activeDomainId);
    if (!resolved || !resolved.label) {
      return Promise.reject(new Error('unknown label class: ' + labelClass));
    }
    var scale = extras.scale_m || resolved.label.scale_m;
    if (pose.w && pose.h && resolved.label.placement === 'footprint') {
      scale = [pose.w, (resolved.label.scale_m && resolved.label.scale_m[1]) || 1, pose.h];
    }
    var placeOpts = Object.assign({}, pose, {
      placement: extras.placement || (resolved.label && resolved.label.placement) || pose.placement,
      use_measured_z: extras.use_measured_z || pose.use_measured_z
    });
    var asset = resolved.asset;
    return instantiateAsset(asset, scale).then(function (node) {
      if (expectGen !== instanceLoadGen || (expectDomain && activeDomainId !== expectDomain)) {
        return null; // stale — domain switched or instances cleared while GLTF loaded
      }
      placeNode(node, placeOpts);
      var rec = recordInstance({
        id: extras.id,
        detection_id: extras.detection_id || pose.detection_id,
        label_class: resolved.label.id,
        asset_id: resolved.assetId,
        x: pose.x,
        y: pose.y,
        z: pose.z,
        yaw: pose.yaw,
        pose_map: extras.pose_map || pose.pose_map,
        w: pose.w,
        h: pose.h,
        confidence: extras.confidence != null ? extras.confidence : pose.confidence,
        covariance: extras.covariance,
        run_id: extras.run_id || pose.run_id,
        t_ns: extras.t_ns,
        T_source: extras.T_source,
        placement_method: extras.placement_method,
        source: extras.source || 'assignAsset'
      }, node);
      if (extras.persist !== false) persistLocal();
      return rec;
    });
  }

  function registerDetections(dets, opts) {
    opts = opts || {};
    dets = dets || [];
    var expectDomain = opts.domainId != null ? opts.domainId : activeDomainId;
    var expectGen = opts.gen != null ? opts.gen : instanceLoadGen;
    var chain = Promise.resolve([]);
    dets.forEach(function (d) {
      chain = chain.then(function (acc) {
        if (expectGen !== instanceLoadGen || activeDomainId !== expectDomain) return acc;
        var pose = {
          x: d.x, y: d.y, z: d.z, yaw: d.yaw, w: d.w, h: d.h,
          pose_map: d.pose_map,
          confidence: d.confidence, run_id: d.run_id,
          detection_id: d.detection_id
        };
        return assignAsset(d.class || d.label_class || d.label || d.cls, pose, {
          id: d.id,
          detection_id: d.detection_id,
          confidence: d.confidence,
          covariance: d.covariance,
          run_id: d.run_id,
          t_ns: d.t_ns,
          T_source: d.T_source,
          placement_method: d.placement_method,
          pose_map: d.pose_map,
          source: d.source || 'detection',
          persist: false,
          gen: expectGen,
          domain_id: expectDomain
        }).then(function (rec) {
          if (rec) acc.push(rec);
          return acc;
        }).catch(function () { return acc; });
      });
    });
    return chain.then(function (acc) {
      if (expectGen !== instanceLoadGen || activeDomainId !== expectDomain) return [];
      persistLocal();
      return acc;
    });
  }

  function importExactInstances(list, opts) {
    opts = opts || {};
    return registerDetections((list || []).map(function (inst) {
      return Object.assign({}, inst, {
        class: inst.label_class || inst.label,
        source: inst.source || 'exact_placer'
      });
    })).then(function (acc) {
      if (opts.persist !== false) persistLocal();
      return acc;
    });
  }

  function loadInstancesForDomain(domainId) {
    activeDomainId = domainId;
    clearInstances(); // bumps instanceLoadGen — capture AFTER so assignAsset gens match
    var gen = instanceLoadGen;
    // Empty / unknown domains: missing instances.json + no localStorage → stay empty
    // (do not inherit meshes from a previously selected domain).
    if (!domainId) return Promise.resolve([]);
    var url = DATA_ROOT + '/domains/' + encodeURIComponent(domainId) + '/instances.json';
    return loadJson(url).catch(function () {
      if (gen !== instanceLoadGen || activeDomainId !== domainId) return { instances: [] };
      try {
        var raw = localStorage.getItem('k1LocalMap.instances.' + domainId);
        return raw ? JSON.parse(raw) : { instances: [] };
      } catch (e) {
        return { instances: [] };
      }
    }).then(function (data) {
      if (gen !== instanceLoadGen || activeDomainId !== domainId) return [];
      var list = (data && data.instances) || [];
      // Keep localStorage in sync with disk so emptied domains stay empty after a prior demo/fixture.
      try {
        if (!list.length) localStorage.removeItem('k1LocalMap.instances.' + domainId);
        else localStorage.setItem('k1LocalMap.instances.' + domainId, JSON.stringify({
          domain_id: domainId,
          instances: list
        }));
      } catch (e) {}
      // Re-clear in case a stale sibling load placed meshes between fetch and place.
      // Keep the same gen so in-flight work for THIS load stays valid.
      while (instanceGroup.children.length) {
        instanceGroup.remove(instanceGroup.children[0]);
      }
      instances = [];
      var chain = Promise.resolve();
      list.forEach(function (inst) {
        chain = chain.then(function () {
          if (gen !== instanceLoadGen || activeDomainId !== domainId) return null;
          return assignAsset(inst.label_class || inst.asset_id, {
            x: inst.x, y: inst.y, z: inst.z, yaw: inst.yaw, w: inst.w, h: inst.h,
            pose_map: inst.pose_map
          }, {
            id: inst.id,
            detection_id: inst.detection_id,
            confidence: inst.confidence,
            covariance: inst.covariance,
            run_id: inst.run_id,
            t_ns: inst.t_ns,
            T_source: inst.T_source,
            placement_method: inst.placement_method,
            pose_map: inst.pose_map,
            source: inst.source || 'instances.json',
            persist: false,
            scale_m: inst.scale_m,
            gen: gen,
            domain_id: domainId
          });
        });
      });
      return chain.then(function () {
        if (gen !== instanceLoadGen || activeDomainId !== domainId) return [];
        return instances.slice();
      });
    });
  }

  function attachToScene(scene) {
    if (scene && instanceGroup.parent !== scene) scene.add(instanceGroup);
  }

  function init(opts) {
    opts = opts || {};
    if (opts.scene) attachToScene(opts.scene);
    return Promise.all([
      loadJson(CATALOG_URL).catch(function () { return { assets: [] }; }),
      loadJson(ONTOLOGY_URL).catch(function () { return { label_classes: [] }; }),
      loadJson(GLOBAL_ASSETS_URL).catch(function () { return { assets: [], label_aliases: {} }; })
    ]).then(function (pair) {
      indexCatalog(pair[0]);
      indexOntology(pair[1]);
      indexGlobalAssets(pair[2]);
      ready = true;
      return { catalog: catalog, ontology: ontology, globalAssets: globalAssets };
    });
  }

  function demoSeedFor(domainId) {
    var id = String(domainId || '').toLowerCase();
    if (id === 'office') {
      return [
        { class: 'elevator', x: -5.4, y: 4.6, yaw: 0, w: 1.2, h: 0.25, confidence: 0.94, run_id: 'demo-office-1' },
        { class: 'elevator', x: -3.8, y: 4.6, yaw: 0, w: 1.2, h: 0.25, confidence: 0.93, run_id: 'demo-office-1' },
        { class: 'door', x: 5.5, y: -0.2, yaw: 1.57, w: 1.0, h: 0.15, confidence: 0.9, run_id: 'demo-office-1' },
        { class: 'window', x: -2.5, y: 4.9, yaw: 0, w: 1.6, h: 0.12, confidence: 0.88, run_id: 'demo-office-1' },
        { class: 'window', x: 2.5, y: 4.9, yaw: 0, w: 1.6, h: 0.12, confidence: 0.87, run_id: 'demo-office-1' },
        { class: 'desk', x: -3.0, y: -1.2, yaw: 0, w: 1.4, h: 0.7, confidence: 0.91, run_id: 'demo-office-2' },
        { class: 'desk', x: -1.0, y: -1.2, yaw: 0, w: 1.4, h: 0.7, confidence: 0.9, run_id: 'demo-office-2' },
        { class: 'desk', x: 1.0, y: -1.2, yaw: 0, w: 1.4, h: 0.7, confidence: 0.89, run_id: 'demo-office-2' },
        { class: 'chair', x: -3.0, y: -0.45, yaw: 3.14, confidence: 0.86, run_id: 'demo-office-2' },
        { class: 'chair', x: -1.0, y: -0.45, yaw: 3.14, confidence: 0.85, run_id: 'demo-office-2' },
        { class: 'cubicle', x: -2.0, y: 0.0, yaw: 0, w: 1.6, h: 1.6, confidence: 0.82, run_id: 'demo-office-2' },
        { class: 'cubicle', x: 2.0, y: 0.0, yaw: 0, w: 1.6, h: 1.6, confidence: 0.81, run_id: 'demo-office-2' },
        { class: 'monitor', x: -3.0, y: -1.45, yaw: 0, w: 0.55, h: 0.08, confidence: 0.8, run_id: 'demo-office-2' },
        { class: 'shelf', x: 5.2, y: 2.5, yaw: 1.57, w: 0.4, h: 1.2, confidence: 0.87, run_id: 'demo-office-3' },
        { class: 'bookcase', x: 5.2, y: -2.8, yaw: 1.57, w: 0.4, h: 1.0, confidence: 0.85, run_id: 'demo-office-3' },
        { class: 'table', x: -3.5, y: 2.8, yaw: 0.2, w: 1.6, h: 0.9, confidence: 0.86, run_id: 'demo-office-3' },
        { class: 'couch', x: 3.2, y: 3.2, yaw: -0.4, w: 1.8, h: 0.85, confidence: 0.84, run_id: 'demo-office-3' },
        { class: 'plant', x: 4.6, y: 4.2, yaw: 0, confidence: 0.78, run_id: 'demo-office-3' },
        { class: 'water_cooler', x: 4.8, y: -4.2, yaw: 0, confidence: 0.8, run_id: 'demo-office-3' },
        { class: 'exit_sign', x: 5.5, y: 0.6, yaw: 1.57, confidence: 0.95, run_id: 'demo-office-1' },
        { class: 'hallway_carpet', x: 0, y: -3.8, yaw: 0, w: 10, h: 1.4, confidence: 0.99, run_id: 'demo-office-1' },
        { class: 'person', x: 0.4, y: -3.2, yaw: 0.5, confidence: 0.92, run_id: 'demo-office-3' }
      ];
    }
    if (id === 'assembly-factory' || id === 'kitchen') {
      return [
        { class: 'conveyor', x: -3.4, y: 0, yaw: 0, w: 0.9, h: 8, confidence: 0.93, run_id: 'demo-factory-1' },
        { class: 'conveyor', x: 0, y: 0, yaw: 0, w: 0.9, h: 8, confidence: 0.91, run_id: 'demo-factory-1' },
        { class: 'conveyor', x: 3.4, y: 0, yaw: 0, w: 0.9, h: 8, confidence: 0.92, run_id: 'demo-factory-1' },
        { class: 'assembly_station', x: -2.2, y: -2.0, yaw: 1.57, confidence: 0.88, run_id: 'demo-factory-2' },
        { class: 'assembly_station', x: 2.2, y: 2.0, yaw: -1.57, confidence: 0.85, run_id: 'demo-factory-2' },
        { class: 'robotic_arm', x: -2.15, y: -0.4, yaw: 1.57, confidence: 0.78, run_id: 'demo-factory-2' },
        { class: 'robotic_arm', x: 2.15, y: 0.5, yaw: -1.57, confidence: 0.76, run_id: 'demo-factory-2' },
        { class: 'parts_bin', x: -2.4, y: -3.2, yaw: 0.2, confidence: 0.82, run_id: 'demo-factory-3' },
        { class: 'safety_fence', x: -4.6, y: 0, yaw: 0, confidence: 0.9, run_id: 'demo-factory-3' },
        { class: 'control_panel', x: -1.0, y: -4.8, yaw: 0.2, confidence: 0.84, run_id: 'demo-factory-3' },
        { class: 'overhead_gantry', x: 0, y: 0.5, yaw: 0, confidence: 0.75, run_id: 'demo-factory-1' },
        { class: 'person', x: 0.2, y: -1.5, yaw: 0.3, confidence: 0.9, run_id: 'demo-factory-4' }
      ];
    }
    if (id === 'distribution-hub' || id === 'outdoor-patio' || id === 'patio') {
      return [
        { class: 'pallet_rack', x: -3.2, y: -2, yaw: 0, w: 1.2, h: 4, confidence: 0.9, run_id: 'demo-hub-1' },
        { class: 'pallet_rack', x: 3.2, y: 1, yaw: 0, w: 1.2, h: 4, confidence: 0.88, run_id: 'demo-hub-1' },
        { class: 'conveyor', x: 0, y: -6.5, yaw: 0, w: 4, h: 0.7, confidence: 0.85, run_id: 'demo-hub-1' },
        { class: 'dock_door', x: 0, y: -9.2, yaw: 0, w: 3, h: 0.3, confidence: 0.92, run_id: 'demo-hub-1' },
        { class: 'pallet', x: -1.5, y: 6.2, yaw: 0.1, w: 1.2, h: 1.0, confidence: 0.8, run_id: 'demo-hub-2' },
        { class: 'cardboard_box', x: -1.5, y: 6.2, yaw: 0.1, w: 0.5, h: 0.45, confidence: 0.81, run_id: 'demo-hub-2' },
        { class: 'crate', x: 1.8, y: 5.8, yaw: -0.2, w: 0.6, h: 0.5, confidence: 0.78, run_id: 'demo-hub-2' },
        { class: 'tote_bin', x: 1.2, y: -3.5, yaw: 0.4, w: 0.55, h: 0.4, confidence: 0.76, run_id: 'demo-hub-2' },
        { class: 'bollard', x: -1.8, y: -8.0, yaw: 0, confidence: 0.95, run_id: 'demo-hub-3' },
        { class: 'bollard', x: 1.8, y: -8.0, yaw: 0, confidence: 0.95, run_id: 'demo-hub-3' },
        { class: 'safety_cone', x: 0.9, y: -7.2, yaw: 0, confidence: 0.9, run_id: 'demo-hub-3' },
        { class: 'pallet_jack', x: 2.2, y: -5.5, yaw: 1.2, confidence: 0.7, run_id: 'demo-hub-3' },
        { class: 'forklift', x: -4.5, y: -7.5, yaw: 0.3, confidence: 0.65, run_id: 'demo-hub-3' },
        { class: 'barrel', x: 4.2, y: 3.5, yaw: 0, confidence: 0.74, run_id: 'demo-hub-3' },
        { class: 'floor_tape', x: 0, y: 0, yaw: 0, w: 0.12, h: 12, confidence: 0.99, run_id: 'demo-hub-1' }
      ];
    }
    if (id === 'warehouse-bay-a') {
      return [
        { class: 'corner_shelf', x: -8.6, y: 2.5, yaw: 1.57, confidence: 0.9, run_id: 'demo-wh-1' },
        { class: 'pallet_rack', x: -3.15, y: 0, yaw: 0, w: 1.15, h: 6, confidence: 0.9, run_id: 'demo-wh-1' },
        { class: 'pallet_rack', x: 3.15, y: 0, yaw: 0, w: 1.15, h: 6, confidence: 0.9, run_id: 'demo-wh-1' },
        { class: 'pallet', x: -1.6, y: 6.5, yaw: 0, confidence: 0.8, run_id: 'demo-wh-2' },
        { class: 'cardboard_box', x: -1.6, y: 6.5, yaw: 0.2, confidence: 0.82, run_id: 'demo-wh-2' },
        { class: 'crate', x: 1.7, y: 6.2, yaw: -0.1, confidence: 0.77, run_id: 'demo-wh-2' },
        { class: 'bollard', x: -1.8, y: -8.0, yaw: 0, confidence: 0.95, run_id: 'demo-wh-3' },
        { class: 'bollard', x: 1.8, y: -8.0, yaw: 0, confidence: 0.95, run_id: 'demo-wh-3' },
        { class: 'safety_cone', x: 0.5, y: -7.5, yaw: 0, confidence: 0.9, run_id: 'demo-wh-3' },
        { class: 'dock_door', x: 0, y: -9.35, yaw: 0, confidence: 0.88, run_id: 'demo-wh-1' },
        { class: 'tote_bin', x: 2.0, y: -4.0, yaw: 0.5, confidence: 0.7, run_id: 'demo-wh-2' }
      ];
    }
    // Operator-created / unknown domains: empty unless autofill / import places assets.
    return [];
  }


  function runDemo(domainId) {
    var id = domainId || activeDomainId;
    activeDomainId = id;
    clearInstances(); // bumps gen — capture AFTER
    var gen = instanceLoadGen;
    return registerDetections(demoSeedFor(id), { domainId: id, gen: gen }).then(function (acc) {
      if (gen !== instanceLoadGen || activeDomainId !== id) return [];
      return acc;
    });
  }

  // Public API merged onto k1LocalMap when ready
  var api = {
    init: init,
    attachToScene: attachToScene,
    getCatalog: function () { return catalog; },
    getOntology: function () { return ontology; },
    getGlobalAssets: function () { return globalAssets; },
    resolveLabel: resolveLabel,
    resolveAsset: resolveAsset,
    assignAsset: assignAsset,
    registerDetections: registerDetections,
    clearInstances: clearInstances,
    getInstances: function () {
      return instances.map(function (i) {
        return {
          id: i.id, detection_id: i.detection_id,
          label_class: i.label_class, asset_id: i.asset_id,
          x: i.x, y: i.y, z: i.z, yaw: i.yaw, pose_map: i.pose_map,
          w: i.w, h: i.h,
          confidence: i.confidence, covariance: i.covariance,
          run_id: i.run_id, t_ns: i.t_ns,
          T_source: i.T_source, placement_method: i.placement_method,
          source: i.source, domain_id: i.domain_id
        };
      });
    },
    exportInstances: persistLocal,
    loadInstancesForDomain: loadInstancesForDomain,
    importExactInstances: importExactInstances,
    runAssetDemo: runDemo,
    demoSeedFor: demoSeedFor,
    instanceGroup: instanceGroup,
    isReady: function () { return ready; }
  };

  window.k1LocalMapAssets = api;

  function patchK1() {
    if (!window.k1LocalMap) return false;
    var k = window.k1LocalMap;
    k.registerDetections = function (dets) { return api.registerDetections(dets); };
    k.assignAsset = function (labelClass, pose, extras) { return api.assignAsset(labelClass, pose, extras); };
    k.importExactInstances = function (list, opts) { return api.importExactInstances(list, opts); };
    k.clearAssetInstances = function () { api.clearInstances(); persistLocal(); };
    k.getAssetInstances = function () { return api.getInstances(); };
    k.exportAssetInstances = function () { return api.exportInstances(); };
    k.loadAssetInstances = function (domainId) { return api.loadInstancesForDomain(domainId || k.getActiveDomain()); };
    k.runAssetDemo = function (domainId) { return api.runDemo(domainId || k.getActiveDomain()); };
    k.getAssetCatalog = function () { return api.getCatalog(); };
    k.getAssetOntology = function () { return api.getOntology(); };
    k.getGlobalAssets = function () { return api.getGlobalAssets(); };
    return true;
  }

  // Patch when viewer exposes API
  var tries = 0;
  (function waitPatch() {
    if (patchK1() || tries++ > 200) return;
    setTimeout(waitPatch, 25);
  })();
})();
