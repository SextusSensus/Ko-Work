# Local Map / Sky Connect — Perfection Audit

**Branch:** `cursor/k1finder-ui-85c6`  
**Tip audited:** `1d580ceb` (+ follow-up fixes on this branch)  
**Date:** 2026-09-11  
**Auditor:** Cloud agent (code + selftest + headless WebGL screenshots)

> **Superseded 2026-09-15:** the Office / Assembly Factory / Warehouse Bay A / Distribution Hub demo domains audited below were removed; `exact-lab` is the only shipped domain. Findings and evidence screenshots that mention those domains describe the pre-removal tree.

## Sibling branches

All previously parallel Local Map lanes are **merged into tip** (ancestors of `1d580ceb`):

| Branch | Status |
|--------|--------|
| `cursor/empty-new-domain-aa12` | merged |
| `cursor/localmap-quality-assets-00bd` | merged |
| `cursor/localmap-brown-box-fix-f790` | merged |
| `cursor/localmap-quality-brown-merge-f790` | merged |
| `cursor/localmap-rerun-exact-62e6` | merged |
| `cursor/office-domain-autofill-24d7` | merged |
| `cursor/scene-quality-free-assets-3b47` | merged (superseded; kitchen seed removed later) |
| `cursor/council-audit-fixes-85c6` | merged |

No unmerged sibling still carries unique Local Map UI work.

## Checklist

| # | Item | Verdict | Notes |
|---|------|---------|-------|
| 1 | Domain chips show **distinct** scenes (no stale occupancy/instances) | **PASS** *(after P0 fix)* | Mesh counts differ (WH≈611 / Hub≈1211 / Factory≈688 / Office≈2023). `domainSwitchGen` + instance gen guards. Screenshots: `audit_domain_*.png`. |
| 2 | Kitchen removed as seed; factory present | **PASS** | Chips: Office / Assembly Factory / Warehouse Bay A / Distribution Hub (+ Exact Lab). No Kitchen chip. Legacy `kitchen` → assembly shell only. |
| 3 | New domain is empty (ground+robot only) | **PASS** *(after P0 fix)* | Was **FAIL**: async `assignAsset` GLTF resolve could place prior-domain racks after create. Fixed. Proof: `audit_domain_empty_new.png` (ground + K1 only). |
| 4 | Exterior toggle independent + persists | **PASS** | Per-domain shells; `localStorage k1LocalMap.exteriorVisible`. On/off shots: `audit_{warehouse,hub,factory}_exterior_{on,off}.png`. |
| 5 | OrbitControls full 360 / zoom / pan | **PASS** | `enableRotate/Zoom/Pan`, azimuth ±∞, polar `[0.18, π−0.18]` (anti-gimbal, not top-down lock). |
| 6 | Preview HTML + WinForms `k1LocalMap` wired | **PASS** | Viewer exports `window.k1LocalMap` (+ assets API). `K1Finder.ps1` WebView2 host bridge (`k1LocalMapHostCreateDomain`, `switchDomain`, etc.). Preview deep-links into viewer (does not re-embed API). |
| 7 | No giant brown cartons / toy blue-red env fills | **WARN** | Shelf cartons are Poly Haven / real-metre proxies (no slab fills). Kenney Factory Kit still uses brand orange/blue/purple on cranes/boxes — intentional free pack, not procedural toy fills. |
| 8 | Poly Haven / free GLBs + attribution; zero paid CGTrader URLs | **PASS** | Catalog sources: Poly Haven, Kenney, ambientCG, OGA, Booster. Attribution + MESHY docs present. `rg cgtrader.com` → none. |
| 9 | Real-world metre scales | **PASS** | `PROP_SCALE_M` e.g. cone ~0.70 m; industrial shelves; K1 0.95 m. |
| 10 | Official Booster K1 mesh + procedural fallback | **PASS** | `assets/library/robot/k1/k1_22dof.glb` loads; `buildK1Procedural` fallback. |
| 11 | Office domain + ontology label→asset | **PASS** | Office seed + `asset-ontology.json` / `global-assets.json` autofill. |
| 12 | Meshy: docs / local drop-in only | **PASS** | `MESHY.md` + gitignored `library/meshy-local/**` (README only in git). No binaries. |
| 13 | Exact SE(3) placer selftests | **PASS** | `python3 eval/localmap_rerun_selftest.py` → **ALL PASS** (1e-6). |
| 14 | Fail-closed without depth; raycast only when allowed | **PASS** | Covered by selftest (C/D refused; C with `--allow-ground-raycast`). |
| 15 | Instances store `pose_map` + provenance | **PASS** | `pose_map`, `placement_method`, `run_id`, `T_source`, `source`, etc. |
| 16 | No merge conflict markers | **PASS** | No `<<<<<<<` / `>>>>>>>` in tree. |
| 17 | No broken imports / missing catalog paths | **PASS** | 95 downloaded paths resolve; 0 missing. 2 `free_link` rows are intentional URL-only. |
| 18 | README/docs consistent | **PASS** *(after P1 fix)* | Was **FAIL**/WARN: docs + `sample.json` still said `kitchen`. Updated to warehouse / assembly. |
| 19 | Drive/safety gates in `K1Finder.ps1` unchanged | **PASS** | DRIVE / ARM MOTION / STOP / Deadman HB / `--require-heartbeat` intact. Diff is Local Map + styling/layout; gates not weakened. |

## Ranked defects

### P0 (fixed this session)

1. **Stale asset instances on new/empty domains**  
   `desktop/localmap-viewer/asset-placer.js` — in-flight `assignAsset` after GLTF resolve could `placeNode` onto a domain that had already switched/cleared (e.g. warehouse rack at (−3.15, 2) on “Audit Empty Bay”).  
   **Fix:** bump `instanceLoadGen` in `clearInstances`; re-check gen/domain after `instantiateAsset`; pass `gen`/`domain_id` through load/demo/register paths. Quiet occupancy 404 for brand-new domains in `viewer.js` `createEmptyDomain`.

### P1 (fixed this session)

2. **`sample.json` still `domain_id: kitchen`** — polluted mental model + any copy paths. → `warehouse-bay-a`.  
3. **`Ensure-LocalMapDomains` copied `sample.json` into missing occupancy** — could seed Kitchen cells into factory/hub/warehouse. → write empty occupancy instead (`K1Finder.ps1`).  
4. **`Import-LocalMapRunIntoActive` fell back to merging `sample.json`** when robot dump missing — could fill empty domains with unrelated cells. → fail closed with operator message.  
5. **Docs used `--domain kitchen` / `-Domain kitchen`** (`docs/localmap-rerun-integration.md`, assets preview comment). → warehouse-bay-a / assembly wording.

### P2 (residual for Todd)

6. **Kenney Factory Kit brand colors** (orange/blue/purple cranes & totes) read “toy” vs Poly Haven PBR warehouse/office. Optional: retint materials or swap to muted industrial GLBs.  
7. **Kitchen library assets remain in catalog** (for ontology/exact-lab fixtures) though Kitchen is not a seed domain — fine, but docs still mention kitchen asset paths historically.  
8. **Browser-only `createDomain`** cannot write `domains/<id>/*.json` without `/api/domains` or WinForms host — empty domain works in-session via localStorage; refresh loses disk files until host persists. Expected for static preview.  
9. **Preview HTML** links to the viewer but does not host `k1LocalMap` itself (WinForms does).  
10. **Software WebGL in CI/headless** is slow (GPU stall warnings); instance loads may appear incomplete if screenshots are taken too early — not a product bug on real GPU/WebView2.

## Evidence artifacts

| File | What |
|------|------|
| `/opt/cursor/artifacts/audit_domain_warehouse.png` | Warehouse Bay A |
| `/opt/cursor/artifacts/audit_domain_hub.png` | Distribution Hub |
| `/opt/cursor/artifacts/audit_domain_factory.png` | Assembly Factory |
| `/opt/cursor/artifacts/audit_domain_office.png` | Office |
| `/opt/cursor/artifacts/audit_domain_empty_new.png` | New empty domain (ground+robot) |
| `/opt/cursor/artifacts/audit_*_exterior_{on,off}.png` | Exterior toggle |
| `/opt/cursor/artifacts/audit_capture_meta.json` | Controls + mesh counts |

## Executive summary

**Ready for Todd with caveats.** Core consolidation checklist is green after the empty-domain instance race and kitchen-sample pollution fixes. Remaining items are P2 polish (Kenney factory color language, browser-only persistence). Re-run `eval/localmap_rerun_selftest.py` and spot-check empty-domain create after pull.
