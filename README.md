# rail-tiles

Turns OSM extracts into a PMTiles vector pyramid (z4–z16) that the Oberleitung
app reads directly over HTTP range requests. No backend, no bundled data. Built
weekly by GitHub Actions and published to Releases.

The design rationale, measurements, and rules live in `TILE-PIPELINE.md` in the
app repo. This README is how the code here maps to it.

## Why this build is unusual

The app spends a fixed vertex budget at render time on whatever the user filters
to, not on a fixed set per zoom. So a category dropped at build time is
permanently invisible. **Nothing is dropped at any zoom** — density is managed
by chaining and parallel collapse upstream and by the client's render budget
downstream, both reversible. Every tippecanoe call passes
`--no-feature-limit --no-tile-size-limit`; the default behaviour drops features
to fit tile size, silently.

## Pipeline

```
per region (matrix, independent, fail-fast off)
  fetch_region.sh   download extract (mirror fallback), filter to railway ways,
                    delete raw immediately (peak disk = one extract)
  build_region.py   classify tags -> typed fields; chain non-service ways once
                    for z4-z11; per zoom simplify + collapse parallel track;
                    one tippecanoe pass per zoom; tile-join to one region file
  verify.py         gates 2-5 (gate 1 runs inside build_region)

join
  tile-join all regions -> europe-<date>.pmtiles
  manifest.py -> manifest.json
  Release <date> holds the archive; the moving 'latest' tag holds the manifest
```

### Files

| file | role |
|---|---|
| `pipeline/classify.py` | tag → typed fields, render key, emitted schema |
| `pipeline/geometry.py` | Douglas-Peucker, chaining, parallel collapse |
| `pipeline/zoomparams.py` | per-zoom tolerance and collapse distance |
| `pipeline/build_region.py` | one region → pyramid (gate 1 inline) |
| `pipeline/verify.py` | gates 2–5 |
| `pipeline/regions.py` | the Europe matrix |
| `pipeline/manifest.py` | manifest.json |
| `scripts/fetch_region.sh` | download + filter + delete |

## Rules encoded here

- **Chain z4–z11, raw ways z12+.** Chaining forces attribute homogeneity, worth
  it zoomed out, not worth it where anyone inspects a way. Chained once and
  reused across low bands (topology is tolerance-independent).
- **Parallel collapse, segment level.** Within one render key, drop vertices
  shadowed by an already-surviving track and keep the divergent remainder.
  Longest-first so a continuous main track survives. Off from z15 up, where the
  screen resolves both tracks. No grid-snapping (it welds separate lines).
- **Service track from z12 only.** The most expensive category; nobody wants
  yard throats at country zoom.
- **Raw numbers in the tiles, speed bands in the app.** Tiles carry the actual
  `maxspeed`; the app maps it to a band. The merge still caps a chain at one
  band internally (`classify.BAND_EDGES`, which must mirror `SpeedBand.bands` in
  the app's `Style.swift`).
- **Source spans on chains.** Each vertex tracks its source `osm_id`, so after
  simplify and collapse a tap still resolves to a real way (`src` = a JSON array
  of `[osm_id, start_vertex, end_vertex]`; single-source features carry a scalar
  `osm_id`).

## Emitted attribute schema (lean)

`kind`, `lifecycle`, `usage?`, `service?`, `elec`, `voltage?`, `frequency?`,
`gauge_class`, `gauge_mm?`, `maxspeed?`, `name?`, `ref?`, `osm_id`, `src?`.

Deep fields (operator, owner, wikidata, protection, dates) are not carried; a
later per-way lookup can fetch them. Bumping the schema (a field, an enum, the
band edges) requires bumping `manifest.SCHEMA_VERSION` so clients hard-purge.

## CI gates

1. **Feature conservation** — tippecanoe's kept count equals the input per zoom
   (inside `build_region.py`). Guards silent dropping.
2. **Category presence at low zoom** — every kind at z16 is present at z4.
3. **Vertex budget** — no ~3×4 tile window exceeds 120 k default-filter
   vertices. Run per region; a metro spanning a country border is a known gap.
4. **Attribute completeness** — every feature carries the render/style fields
   and an identity (`osm_id`/`src`).
5. **Merge honesty** — no chain spans >1 speed band or mixes known with unknown
   speed (`verify.py --self-test`).

## Local

```
make selftest              # gate 5, no data
make build R=switzerland   # build one checked-in sample
make verify R=switzerland
make join                  # tile-join the sample builds
```

Needs `osmium`, `tippecanoe`, `tile-join`, `tippecanoe-decode`, Python 3.

## Size

Measured: Switzerland 14 MB, Czechia 25 MB as full z4–z16 PMTiles. Europe is
expected around 0.4–0.6 GB (well under the 2 GB Releases limit; range requests
mean a client downloads only what it views). This is several times the earlier
back-of-envelope estimate, which summed only sampled zoom levels.
