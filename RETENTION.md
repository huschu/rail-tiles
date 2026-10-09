# Release asset retention

Status: implemented as the "Prune old release assets" step of the `join` job in
[build-tiles.yml](.github/workflows/build-tiles.yml).

Each weekly build publishes ~3.6 GB of `.pmtiles`, ~92 MB of `.hashes`, and a few
hundred KB of `.changed` to a dated release. GitHub releases never expire, so with
no pruning storage grows ~3.7 GB per week forever. The prune step strips the large
assets from old releases while keeping the change lists, which are the only past
assets the incremental client update needs. See
[Incremental updates](README.md#incremental-updates) for the update mechanism.

## Why deleting old archives is safe

Once every client has re-read `latest`, nothing reads an old build's `.pmtiles`:

- The client range-fetches tiles only from the archives named in the manifest it
  last read. On the incremental path it drops stale tiles and refetches them from
  that manifest's archives, never an older one.
- The next build diffs against the previous build's `.hashes`, not its archive.
  That is the reason the hash sidecar exists.
- A client on a stale persisted manifest is offline by definition (it reached the
  persisted copy only because the `latest` fetch failed), and offline it serves
  its disk cache, not the network archive.

The exception is an app that stays suspended across builds. The app reads
`latest` only at cold launch, so a suspended app keeps fetching from the build it
launched with. If that build's archive is gone, tiles it has cached show their
old version and uncached tiles stay blank until a relaunch. Archives therefore
stay for a fixed time rather than a fixed number of builds, because manual runs
can publish several builds in one week.

`.hashes` are build-internal; the client never fetches them. `.changed` are the
history a client chains through to catch up across builds, so they are the one
thing to keep.

## Retention rules

| asset | keep | delete |
|---|---|---|
| `<region>-<tag>.pmtiles` | builds from the last 14 days, and always the 2 newest | older |
| `<region>-<tag>.hashes` | the 2 newest builds | older |
| `<region>-<tag>.changed` | all (at least the `builds` window, 26) | — |

The 14 days are a grace window for apps suspended across builds. The 2-newest
floor keeps the prior build's archives when builds pause for longer than that.
Only the next build's diff reads `.hashes`, and it reads the newest release's, so
two builds of hashes leave one spare.

After pruning, an old dated release holds only its `.changed` files. GitHub deletes
assets individually with `gh release delete-asset <tag> <name>` without touching
the release or its other assets, so the change-list URLs keep resolving.

## Implementation

The prune runs as the last publishing step of a full build:

1. List dated releases (`gh release list`, tags matching `^[0-9]{8}$`), newest
   first.
2. Keep the 2 newest releases whole. The build just published the newest one.
3. On every older release, delete its `.hashes`. Also delete its `.pmtiles` when
   its tag is more than 14 days old. Keep `.changed`.

It runs only when both publish steps succeeded, so it never deletes an archive a
half-finished build still points at. Smoke runs skip it. It never touches the
`latest` release, which holds `manifest.json`, or the small
`night-trains-<tag>.json.gz`. A failed delete logs a warning and the next build
retries it.

## Result

Storage drops from unbounded to 14 days of archives, at ~3.6 GB per build before
the stations layer, and ~180 MB of hashes, plus a slowly growing pile of change
lists. At two to three builds a week that is roughly 15 to 25 GB of archives.
