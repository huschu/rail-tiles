# Release asset retention

Status: planned, not implemented.

Each weekly build publishes ~3.6 GB of `.pmtiles`, ~92 MB of `.hashes`, and a few
hundred KB of `.changed` to a dated release. GitHub releases never expire, so with
no pruning storage grows ~3.7 GB per week forever. This plan strips the large
assets from old releases while keeping the change lists, which are the only past
assets the incremental client update needs. See
[Incremental updates](README.md#incremental-updates) for the update mechanism.

## Why deleting old archives is safe

Nothing reads an old build's `.pmtiles` once a newer build is published:

- The client range-fetches tiles only from the archives named in the `latest`
  manifest, which always point at the current build. On the incremental path it
  drops stale tiles and refetches them from the current archive, never an old one.
- The next build diffs against the previous build's `.hashes`, not its archive.
  That is the reason the hash sidecar exists.
- A client on a stale persisted manifest is offline by definition (it reached the
  persisted copy only because the `latest` fetch failed), and offline it serves
  its disk cache, not the network archive.

`.hashes` are build-internal; the client never fetches them. `.changed` are the
history a client chains through to catch up across builds, so they are the one
thing to keep.

## Retention rules

| asset | keep | delete |
|---|---|---|
| `<region>-<tag>.pmtiles` | the 2 newest builds | older |
| `<region>-<tag>.hashes` | the 2 newest builds | older |
| `<region>-<tag>.changed` | all (at least the `builds` window, 12) | — |

Keeping two builds of archives, rather than one, is a grace window: a client that
loaded the prior manifest just before a new build lands keeps working until it
re-reads `latest` on next launch. Keeping two builds of hashes tolerates a skipped
week, the diff targets the newest release that still has hashes.

After pruning, an old dated release holds only its `.changed` files. GitHub deletes
assets individually with `gh release delete-asset <tag> <name>` without touching
the release or its other assets, so the change-list URLs keep resolving.

## Implementation sketch

A prune step at the end of a successful build, or a separate scheduled workflow:

1. List dated releases (`gh release list`, tags matching `^[0-9]{8}$`), newest
   first.
2. Keep the 2 newest releases whole.
3. On every older release, delete its `.pmtiles` and `.hashes`, keep `.changed`.

Gate it on the publish step succeeding, so it never deletes an archive a
half-finished build still points at. Never touch the `latest` release, it holds
`manifest.json`.

## Result

Storage drops from unbounded to a bounded ~7 GB of archives (two builds) and
~180 MB of hashes, plus a slowly growing pile of KB-sized change lists.
