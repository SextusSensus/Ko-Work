# Free assets — K1 Local Map

**Hard rule (Todd): NO PAID ASSETS.** Catalog statuses are only:

| Status | Meaning |
|--------|---------|
| `downloaded` | CC0 (or equivalent) binary committed under `assets/` |
| `procedural` | Three.js stand-in in `viewer.js` |
| `free_link` | Discoverable free twin URL; not required in-repo |

Never `paid` / `purchase` / `link_only` / marketplace purchase targets.

## Sources we ship from

- [Poly Haven](https://polyhaven.com/) — CC0 models + textures
- [ambientCG](https://ambientcg.com/) — CC0 PBR materials
- [Kenney](https://kenney.nl/) — Factory Kit CC0 ([OpenGameArt mirror](https://opengameart.org/content/factory-kit))
- [OpenGameArt — Traffic Road Assets](https://opengameart.org/content/traffic-road-assets) (MilkAndBanana, CC0)
- Authored CC0 stand-ins (e.g. `wooden_pallet_cc0`)
- Procedural recipes in `viewer.js` when no free mesh exists (forklift, bollard, fridge, K1 proxy)

## Catalog + ontology

- Runtime inventory: [`catalog.json`](./catalog.json)
- Label → asset map: [`../../localmap-data/asset-ontology.json`](../../localmap-data/asset-ontology.json)
- Paid→free twin table: [`FREE-ALTERNATIVES.md`](./FREE-ALTERNATIVES.md)
- Per-file credits: [`ATTRIBUTION.md`](./ATTRIBUTION.md)

## Domain coverage

| Domain need | Free path |
|-------------|-----------|
| Kitchen appliances / cabinets | Poly Haven stove, microwave, drawer cabinet + procedural fridge/counter |
| Warehouse Bay A | Poly Haven racks/boxes/crates + procedural aisle racks/bollards |
| Distribution Hub | Kenney conveyors/doors + Poly Haven props + procedural forklifts/dock |
| Generic indoor | Sofa, plant, procedural walls/pillars/doors |
| Robot proxy | Procedural K1 envelope (not Booster mesh); URDF ref only |

## Explicitly rejected

- Any **paid** marketplace listing as a purchase target (including CGTrader)
- Marketplace “free” Royalty Free downloads when git redistribution is unclear — use Poly Haven / Kenney CC0 twins or procedural instead
- Purchase wishlists / `link_only` shopping lists of paid URLs

If a paid marketplace listing looks useful, scrape **free** CC0 sources (Poly Haven, Kenney, ambientCG, OpenGameArt) for an equivalent — do not keep the paid URL in-repo.
