# rail-tiles

Turns OSM extracts into a PMTiles railway pyramid (z4–z16) served over HTTP range
requests. Built weekly by GitHub Actions and published to Releases; a
`manifest.json` at the stable `latest` tag names the current archive.

## What's here

| path | role |
|---|---|
| `pipeline/` | classify tags → typed fields, chain ways, collapse parallel track, tile per zoom, verify |
| `scripts/fetch_region.sh` | download an extract (Geofabrik → OSM-France fallback), filter to railway ways |
| `.github/workflows/build-tiles.yml` | per-region matrix build → one joined archive → publish |
| `samples/` | pre-filtered extracts for local testing |

## Output

- z4–z16, single `rail` layer. Below z12: merged chains carrying source spans.
  z12 and up: raw OSM ways.
- Lean attributes (raw numbers; speed bands are the client's job): `kind`,
  `lifecycle`, `usage`, `service`, `elec`, `voltage`, `frequency`,
  `gauge_class`, `gauge_mm`, `maxspeed`, `name`, `ref`, `osm_id`, `src`.
- Nothing is dropped by density — tippecanoe's drop flags stay off; density is
  managed by chaining (z4–z11) and segment-level parallel collapse (z4–z14).
- Europe is ~688 MB; a client only downloads the tiles it views.

## Build

Weekly cron plus manual dispatch. Per region: fetch → filter → tile → verify,
skipping regions whose extract is unchanged (md5-keyed cache). Then `tile-join`
into `europe-<date>.pmtiles`, publish to a dated Release, and update
`manifest.json` at the `latest` tag. A smoke run (dispatch with a `regions`
list) builds an artifact and does not publish.

## Local

```
make selftest              # merge-honesty gate, no data
make build R=switzerland   # build one sample region
make verify R=switzerland  # run the gates on it
make join                  # tile-join the sample builds
```

Needs `osmium`, `tippecanoe`, `tile-join`, `tippecanoe-decode`, Python 3.

## Gates

Feature conservation, category presence at low zoom, per-tile vertex budget,
attribute completeness, merge honesty.
