# Meshy.ai — Local Map policy

Todd noted [Meshy](https://www.meshy.ai) has useful 3D assets. This doc is the license verdict and how (or whether) to use them with K1 Local Map.

**Sources checked (2026-09-11):** [Meshy Terms of Use](https://www.meshy.ai/terms-of-use) (updated 2026-03-07), [pricing FAQ](https://www.meshy.ai/pricing), [Free plan help](https://help.meshy.ai/en/articles/15696428-what-is-included-on-the-free-plan), [docs licensing table](https://docs.meshy.ai/en/webapp/pricing).

## License verdict

| Plan / path | Who owns output | Redistribution | Fit for this open repo? |
|-------------|-----------------|----------------|-------------------------|
| **Free** | Meshy owns; grants **CC BY 4.0** | Yes, commercially, **with attribution** to Meshy | Legally OK *if* you can download — but **Meshy 6/7 downloads are paywalled on Free** |
| **Paid (Pro+)** | Customer owns (private license) | Yes (your property) | **Not “free.”** Do not treat as catalog `downloaded` CC0. Do not agent-commit paid binaries |
| **Community publish** | Released under **CC0** (ToS §3.3) | Yes, public domain dedication | Only clean Meshy→git path *if* you publish to Community and still get a download |
| **Watermarked / preview** | N/A | Do not redistribute | **Never commit** |

**Bottom line for Ko-Work Local Map:** Meshy is **not** a preferred in-repo source. Prefer **Poly Haven / ambientCG / Kenney CC0** (and procedural stand-ins). Use Meshy as an **optional local drop-in** on Todd’s machine. Agents must **not** download Meshy paid or watermarked assets into the repo.

## Repo hard rules

1. **NO PAID ASSETS** in git — same as [`FREE-ASSETS.md`](./FREE-ASSETS.md).
2. Catalog `downloaded` rows stay **CC0** (or BSD-3 for official K1). Do not add Meshy CC BY or private-paid rows as if they were CC0.
3. Never commit Meshy watermarked previews or paywalled exports.
4. Optional local Meshy GLBs live under `library/meshy-local/` (gitignored binaries) — see below.

## Preferred sources (in-repo)

| Need | Source |
|------|--------|
| Props / rooms / industrial | [Poly Haven](https://polyhaven.com/) CC0 |
| PBR materials | [ambientCG](https://ambientcg.com/) CC0 |
| Factory / conveyor kit | [Kenney Factory Kit](https://kenney.nl/assets/factory-kit) CC0 |
| Missing mesh | Procedural recipe in `viewer.js` / `asset-placer.js` |

## Optional local drop-in (recommended)

For experiments without touching git redistribution:

1. Generate or download a model on [meshy.ai](https://www.meshy.ai) under **your** account (paid download rights if Meshy 6/7).
2. Export **GLB** (viewer loads glTF/GLB via `GLTFLoader`).
3. Place the file locally:

```text
desktop/localmap-viewer/assets/library/meshy-local/<asset-id>/<asset-id>.glb
```

   That tree’s binaries are **gitignored**. Keep a short local note next to the GLB with plan (Free/Paid/Community), date, and prompt — for your notes only.
4. Point a **local-only** catalog override or temporary `catalog.json` edit at that path (do not push paid/CC BY rows). Or load via browser console / a personal patch.
5. Prefer matching a **CC0 twin** from Poly Haven / Kenney when the prop should ship to everyone.

## If Todd later wants Community CC0 in git

Only when the asset is **explicitly** released on Meshy Community (**CC0** per ToS §3.3) and downloadable without a paid paywall:

1. Export **GLB** from Meshy → save as:

```text
desktop/localmap-viewer/assets/library/<category>/<asset-id>/<asset-id>.glb
```

   Example: `library/warehouse/meshy_tote_01/meshy_tote_01.glb`.

2. Register in [`catalog.json`](./catalog.json):

```json
{
  "id": "warehouse/meshy-tote-01",
  "name": "Tote (Meshy Community CC0)",
  "category": "warehouse",
  "status": "downloaded",
  "path": "assets/library/warehouse/meshy_tote_01/meshy_tote_01.glb",
  "license": "CC0",
  "source": "Meshy Community",
  "source_url": "https://www.meshy.ai/",
  "fallback_procedural": "box"
}
```

3. Map a label in [`../../localmap-data/asset-ontology.json`](../../localmap-data/asset-ontology.json):

```json
{
  "id": "tote",
  "aliases": ["plastic_tote", "bin"],
  "default_asset": "warehouse/meshy-tote-01",
  "placement": "footprint",
  "scale_m": [0.6, 0.4, 0.4],
  "domains": ["warehouse-bay-a", "distribution-hub"]
}
```

4. Credit in [`ATTRIBUTION.md`](./ATTRIBUTION.md) (Community CC0 still: note source + Meshy Community link).
5. Preview: `cd desktop && python3 -m http.server 8765` → open Local Map and `k1LocalMap.assignAsset('tote', { x: 0, y: 0, yaw: 0 })`.

### Free-plan CC BY 4.0 (not preferred for git)

Free outputs are **CC BY 4.0** (ToS §3.2): share/adapt commercially **with credit to Meshy**. That is redistributable, but:

- Current **Meshy 6/7 downloads are not included on Free** (help center) — so there is often no free export.
- This repo’s catalog default is **CC0**; CC BY would need a distinct `license` field, `ATTRIBUTION.md` credit, and deliberate review — **prefer a CC0 twin instead**.

Attribution example if ever committed: `3D model generated with Meshy (https://www.meshy.ai) — CC BY 4.0`.

## Agents / automation

- Do **not** scrape or commit Meshy binaries.
- Do **not** add Meshy paid URLs as purchase targets in catalog docs.
- Point gaps at Poly Haven / Kenney / procedural — see [`FREE-ALTERNATIVES.md`](./FREE-ALTERNATIVES.md).

## Links

- Policy: [`FREE-ASSETS.md`](./FREE-ASSETS.md)
- Credits: [`ATTRIBUTION.md`](./ATTRIBUTION.md)
- Catalog / ontology how-to: [`../../docs/localmap-assets.md`](../../docs/localmap-assets.md)
- Meshy ToS: https://www.meshy.ai/terms-of-use
- CC BY 4.0: https://creativecommons.org/licenses/by/4.0/
- CC0: https://creativecommons.org/publicdomain/zero/1.0/
