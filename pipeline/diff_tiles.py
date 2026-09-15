#!/usr/bin/env python3
"""
Per-tile change lists for incremental client updates.

A weekly rebuild changes well under 1% of tiles (rail geometry barely moves), yet
the client today purges its whole cache on any buildId change. This emits, per
continent archive, the exact set of tiles whose bytes differ from the previous
build, so the client drops only those and keeps the rest.

Two artifacts per continent:

  <region>-<tag>.hashes   build-internal. tileId -> 8-byte content hash for every
                          addressed tile. Compared against the previous build to
                          find the delta without re-downloading the archive.
  <region>-<tag>.changed  client-facing, gzipped JSON. The z/x/y tiles that
                          changed, were added, or were removed since the previous
                          build. The client deletes each from its disk cache; the
                          next camera visit refetches it. Gzipped because the app
                          already gunzips (tiles are gzipped MVT); GitHub serves
                          release assets verbatim, so no double-decompression.

The hash is over the stored (compressed) tile bytes, so it is exact: an unchanged
tile hashes identically across builds (confirmed: reused regions are byte-identical
and tile-join is deterministic). A same-length edit is caught where a length-only
diff would miss it.

Commands:
  diff_tiles.py hash  ARCHIVE.pmtiles  OUT.hashes
  diff_tiles.py diff  --region R --from PREV --to TAG \
                      --prev PREV.hashes --cur CUR.hashes --out OUT.changed
"""
import argparse
import gzip
import hashlib
import json
import mmap
import struct
import sys

# ---- PMTiles v3 reader (enough to enumerate tiles; mirrors PMTiles.swift) ----

class _Reader:
    __slots__ = ("b", "i")
    def __init__(self, b): self.b = b; self.i = 0
    def v(self):
        r = 0; s = 0; b = self.b
        while True:
            x = b[self.i]; self.i += 1
            r |= (x & 0x7f) << s
            if not (x & 0x80): break
            s += 7
        return r


def _deserialize(data):
    r = _Reader(data)
    n = r.v()
    ids = [0] * n; rl = [0] * n; ln = [0] * n; off = [0] * n
    last = 0
    for k in range(n):
        last += r.v(); ids[k] = last
    for k in range(n): rl[k] = r.v()
    for k in range(n): ln[k] = r.v()
    for k in range(n):
        x = r.v()
        off[k] = (off[k - 1] + ln[k - 1]) if (x == 0 and k > 0) else x - 1
    return ids, rl, ln, off, n


class Archive:
    def __init__(self, path):
        self.f = open(path, "rb")
        self.mm = mmap.mmap(self.f.fileno(), 0, access=mmap.ACCESS_READ)
        head = self.mm[:127]
        if head[:7] != b"PMTiles" or head[7] != 3:
            raise ValueError(f"{path}: not a PMTiles v3 archive")
        u64 = lambda o: struct.unpack_from("<Q", head, o)[0]
        self.root_off, self.root_len = u64(8), u64(16)
        self.leaf_off = u64(40)
        self.tile_off = u64(56)
        self.icomp, self.tcomp = head[97], head[98]

    def _dir(self, off, length):
        raw = self.mm[off:off + length]
        if self.icomp == 2:
            raw = gzip.decompress(raw)
        return _deserialize(raw)

    def tiles(self):
        """Yield (tileId, offset, length) for every addressed tile."""
        stack = [self._dir(self.root_off, self.root_len)]
        pending_leaves = []
        while stack:
            ids, rl, ln, off, n = stack.pop()
            for k in range(n):
                if rl[k] == 0:
                    pending_leaves.append((off[k], ln[k]))
                else:
                    for j in range(rl[k]):
                        yield ids[k] + j, off[k], ln[k]
            while pending_leaves:
                loff, llen = pending_leaves.pop()
                stack.append(self._dir(self.leaf_off + loff, llen))

    def hash_at(self, off, length):
        base = self.tile_off
        return hashlib.blake2b(self.mm[base + off:base + off + length],
                               digest_size=8).digest()


# ---- Hilbert tileId <-> z/x/y (verbatim port of protomaps/PMTiles) ----

def _rotate(n, x, y, rx, ry):
    if ry == 0:
        if rx == 1:
            x = n - 1 - x; y = n - 1 - y
        return y, x
    return x, y


def tileid_to_zxy(i):
    acc = 0; z = 0
    while True:
        num = 1 << (2 * z)           # 4**z tiles on level z
        if acc + num > i:
            pos = i - acc
            n = 1 << z
            x = y = 0; t = pos; s = 1
            while s < n:
                rx = 1 & (t >> 1)
                ry = 1 & (t ^ rx)
                x, y = _rotate(s, x, y, rx, ry)
                x += s * rx; y += s * ry
                t >>= 2; s <<= 1
            return z, x, y
        acc += num; z += 1


def zxy_to_tileid(z, x, y):
    # Verbatim port of zxyToTileId in PMTiles.swift (rx/ry carry the bit value s,
    # not a normalised 0/1); used only to assert the inverse round-trips.
    if z == 0:
        return 0
    acc = ((1 << z) * (1 << z) - 1) // 3
    a = z - 1; tx, ty = x, y; s = 1 << a
    while s > 0:
        rx = tx & s
        ry = ty & s
        acc += ((3 * rx) ^ ry) * (1 << a)
        tx, ty = _rotate_fwd(s, tx, ty, rx, ry)
        a -= 1; s >>= 1
    return acc


def _rotate_fwd(n, x, y, rx, ry):
    if ry == 0:
        if rx != 0:
            return n - 1 - y, n - 1 - x
        return y, x
    return x, y


# ---- .hashes binary format: "RTH1", varint count, then (varint idDelta, 8B hash) ----

_MAGIC = b"RTH1"

def _wv(out, n):
    while True:
        b = n & 0x7f; n >>= 7
        out.append(b | 0x80 if n else b)
        if not n: break


def write_hashes(path, items):
    """items: iterable of (tileId, 8-byte hash), any order."""
    items = sorted(items)
    out = bytearray(_MAGIC)
    _wv(out, len(items))
    last = 0
    for tid, h in items:
        _wv(out, tid - last); last = tid
        out += h
    with open(path, "wb") as f:
        f.write(out)


def read_hashes(path):
    with open(path, "rb") as f:
        b = f.read()
    if b[:4] != _MAGIC:
        raise ValueError(f"{path}: bad hashes magic")
    r = _Reader(b); r.i = 4
    n = r.v()
    d = {}
    last = 0
    for _ in range(n):
        last += r.v()
        d[last] = bytes(b[r.i:r.i + 8]); r.i += 8
    return d


# ---- commands ----

def cmd_hash(archive_path, out_path):
    a = Archive(archive_path)
    seen = {}                        # dedupe shared blobs by (off,len)
    items = []
    for tid, off, ln in a.tiles():
        key = (off, ln)
        h = seen.get(key)
        if h is None:
            h = a.hash_at(off, ln); seen[key] = h
        items.append((tid, h))
    write_hashes(out_path, items)
    print(f"hash: {archive_path} -> {out_path} ({len(items):,} tiles)", file=sys.stderr)


def cmd_diff(region, frm, to, prev_path, cur_path, out_path):
    prev = read_hashes(prev_path)
    cur = read_hashes(cur_path)
    prev_keys = prev.keys()
    cur_keys = cur.keys()
    changed = [t for t in (prev_keys & cur_keys) if prev[t] != cur[t]]
    added = list(cur_keys - prev_keys)
    removed = list(prev_keys - cur_keys)

    tiles = []
    for tid in changed + added + removed:
        z, x, y = tileid_to_zxy(tid)
        assert zxy_to_tileid(z, x, y) == tid, f"zxy roundtrip failed for {tid}"
        tiles.append((z, x, y))
    tiles.sort()

    doc = {
        "schema": 1,
        "region": region,
        "from": frm,
        "to": to,
        "counts": {"changed": len(changed), "added": len(added), "removed": len(removed)},
        "tiles": [[z, x, y] for (z, x, y) in tiles],
    }
    raw = json.dumps(doc, separators=(",", ":")).encode()
    with open(out_path, "wb") as f:
        f.write(gzip.compress(raw, mtime=0))     # mtime=0 keeps output reproducible
    total = len(cur)
    pct = 100 * len(tiles) / total if total else 0
    print(f"diff: {region} {frm}->{to}  {len(tiles):,}/{total:,} tiles "
          f"({pct:.3f}%)  changed={len(changed)} added={len(added)} removed={len(removed)}",
          file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("hash")
    h.add_argument("archive")
    h.add_argument("out")
    d = sub.add_parser("diff")
    d.add_argument("--region", required=True)
    d.add_argument("--from", dest="frm", required=True)
    d.add_argument("--to", required=True)
    d.add_argument("--prev", required=True)
    d.add_argument("--cur", required=True)
    d.add_argument("--out", required=True)
    args = ap.parse_args()
    if args.cmd == "hash":
        cmd_hash(args.archive, args.out)
    else:
        cmd_diff(args.region, args.frm, args.to, args.prev, args.cur, args.out)


if __name__ == "__main__":
    main()
