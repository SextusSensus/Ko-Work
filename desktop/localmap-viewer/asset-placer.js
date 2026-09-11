/* K1 Local Map — catalog-driven asset placement
 * Future loop: domain create → robot map runs → labeling detections → assignAsset → instances.json
 * Live robot labeling will call the same window.k1LocalMap.registerDetections / assignAsset API.
 */
(function () {
  'use strict';

  var DATA_ROOT = '../localmap-data';
  var CATALOG_URL = './assets/catalog.json';
  var ONTOLOGY_URL = DATA_ROOT + '/asset-ontology.json';

  var catalog = { version: 0, assets: [] };
  var ontology = { version: 0, label_classes: [] };
  var assetById = {};
  var labelByKey = {};
  var meshCache = {};
  var instanceGroup = new THREE.Group();
  instanceGroup.name = 'assetInstances';
  var instances = [];
  var activeDomainId = null;
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
    // Domain-specific override hook (future)
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

  function placeNode(node, pose) {
    pose = pose || {};
    node.position.x = pose.x || 0;
    node.position.z = pose.y != null ? pose.y : (pose.z || 0);
    if (pose.yaw != null) node.rotation.y = -pose.yaw;
    instanceGroup.add(node);
  }

  function recordInstance(rec, node) {
    var entry = {
      id: rec.id || ('inst-' + Date.now() + '-' + Math.floor(Math.random() * 1e4)),
      label_class: rec.label_class,
      asset_id: rec.asset_id,
      x: rec.x || 0,
      y: rec.y || 0,
      yaw: rec.yaw || 0,
      w: rec.w,
      h: rec.h,
      confidence: rec.confidence,
      run_id: rec.run_id,
      domain_id: activeDomainId,
      source: rec.source || 'assign'
    };
    entry._node = node;
    instances.push(entry);
    return entry;
  }

  function clearInstances() {
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
            label_class: i.label_class,
            asset_id: i.asset_id,
            x: i.x, y: i.y, yaw: i.yaw,
            w: i.w, h: i.h,
            confidence: i.confidence,
            run_id: i.run_id,
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
    pose = pose || {};
    var resolved = resolveAsset(labelClass, activeDomainId);
    if (!resolved || !resolved.label) {
      return Promise.reject(new Error('unknown label class: ' + labelClass));
    }
    var scale = extras.scale_m || resolved.label.scale_m;
    if (pose.w && pose.h && resolved.label.placement === 'footprint') {
      scale = [pose.w, (resolved.label.scale_m && resolved.label.scale_m[1]) || 1, pose.h];
    }
    var asset = resolved.asset;
    return instantiateAsset(asset, scale).then(function (node) {
      placeNode(node, pose);
      var rec = recordInstance({
        id: extras.id,
        label_class: resolved.label.id,
        asset_id: resolved.assetId,
        x: pose.x || 0,
        y: pose.y != null ? pose.y : 0,
        yaw: pose.yaw || 0,
        w: pose.w,
        h: pose.h,
        confidence: extras.confidence != null ? extras.confidence : pose.confidence,
        run_id: extras.run_id || pose.run_id,
        source: extras.source || 'assignAsset'
      }, node);
      if (extras.persist !== false) persistLocal();
      return rec;
    });
  }

  function registerDetections(dets) {
    dets = dets || [];
    var chain = Promise.resolve([]);
    dets.forEach(function (d) {
      chain = chain.then(function (acc) {
        return assignAsset(d.class || d.label_class || d.label || d.cls, {
          x: d.x, y: d.y, yaw: d.yaw, w: d.w, h: d.h, confidence: d.confidence, run_id: d.run_id
        }, {
          confidence: d.confidence,
          run_id: d.run_id,
          source: 'detection',
          persist: false
        }).then(function (rec) {
          acc.push(rec);
          return acc;
        }).catch(function () { return acc; });
      });
    });
    return chain.then(function (acc) {
      persistLocal();
      return acc;
    });
  }

  function loadInstancesForDomain(domainId) {
    activeDomainId = domainId;
    clearInstances();
    var url = DATA_ROOT + '/domains/' + encodeURIComponent(domainId) + '/instances.json';
    return loadJson(url).catch(function () {
      try {
        var raw = localStorage.getItem('k1LocalMap.instances.' + domainId);
        return raw ? JSON.parse(raw) : { instances: [] };
      } catch (e) {
        return { instances: [] };
      }
    }).then(function (data) {
      var list = (data && data.instances) || [];
      var chain = Promise.resolve();
      list.forEach(function (inst) {
        chain = chain.then(function () {
          return assignAsset(inst.label_class || inst.asset_id, {
            x: inst.x, y: inst.y, yaw: inst.yaw, w: inst.w, h: inst.h
          }, {
            id: inst.id,
            confidence: inst.confidence,
            run_id: inst.run_id,
            source: inst.source || 'instances.json',
            persist: false,
            scale_m: inst.scale_m
          });
        });
      });
      return chain.then(function () { return instances.slice(); });
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
      loadJson(ONTOLOGY_URL).catch(function () { return { label_classes: [] }; })
    ]).then(function (pair) {
      indexCatalog(pair[0]);
      indexOntology(pair[1]);
      ready = true;
      return { catalog: catalog, ontology: ontology };
    });
  }

  function demoSeedFor(domainId) {
    var id = String(domainId || '').toLowerCase();
    if (id.indexOf('assembly') >= 0 || id.indexOf('factory') >= 0 || id.indexOf('kitchen') >= 0) {
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
    if (id.indexOf('distribution') >= 0 || id.indexOf('hub') >= 0) {
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
    // warehouse-bay-a default
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

  function runDemo(domainId) {
    clearInstances();
    return registerDetections(demoSeedFor(domainId || activeDomainId));
  }

  // Public API merged onto k1LocalMap when ready
  var api = {
    init: init,
    attachToScene: attachToScene,
    getCatalog: function () { return catalog; },
    getOntology: function () { return ontology; },
    resolveLabel: resolveLabel,
    resolveAsset: resolveAsset,
    assignAsset: assignAsset,
    registerDetections: registerDetections,
    clearInstances: clearInstances,
    getInstances: function () {
      return instances.map(function (i) {
        return {
          id: i.id, label_class: i.label_class, asset_id: i.asset_id,
          x: i.x, y: i.y, yaw: i.yaw, w: i.w, h: i.h,
          confidence: i.confidence, run_id: i.run_id, source: i.source, domain_id: i.domain_id
        };
      });
    },
    exportInstances: persistLocal,
    loadInstancesForDomain: loadInstancesForDomain,
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
    k.clearAssetInstances = function () { api.clearInstances(); persistLocal(); };
    k.getAssetInstances = function () { return api.getInstances(); };
    k.exportAssetInstances = function () { return api.exportInstances(); };
    k.loadAssetInstances = function (domainId) { return api.loadInstancesForDomain(domainId || k.getActiveDomain()); };
    k.runAssetDemo = function (domainId) { return api.runDemo(domainId || k.getActiveDomain()); };
    k.getAssetCatalog = function () { return api.getCatalog(); };
    k.getAssetOntology = function () { return api.getOntology(); };
    return true;
  }

  // Patch when viewer exposes API
  var tries = 0;
  (function waitPatch() {
    if (patchK1() || tries++ > 200) return;
    setTimeout(waitPatch, 25);
  })();
})();
