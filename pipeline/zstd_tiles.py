#!/usr/bin/env python3
"""
Rewrite a PMTiles archive with zstd tile compression.

tippecanoe and tile-join write only gzip (or uncompressed) tiles, so the join
recompresses each continent archive after tile-join. Every tile becomes one zstd
level-19 frame with its content size in the frame header, and no dictionary.
Directories and metadata stay gzip.

The rewrite keeps the archive's structure: the same entries and run lengths, one
stored copy per deduplicated tile, tile data in the source's (clustered) order,
the same metadata and header fields. Only the tile bytes, their offsets and
lengths, and the directory split into root and leaves change.

Needs Python 3.14+ (compression.zstd).

Usage: zstd_tiles.py IN.pmtiles OUT.pmtiles
"""
import gzip
import mmap
import os
import shutil
import struct
import sys
import tempfile
from compression import zstd
from concurrent.futures import ProcessPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from diff_tiles import Archive, decompress_tile

LEVEL = 19
ZSTD = 4
HEADER_LEN = 127
ROOT_MAX = 16384 - HEADER_LEN    # the spec keeps header + root within 16 KiB
CHUNK = 512                      # tiles per worker task

_src = None


def _open_source(path):
    global _src
    _src = Archive(path)


def _compress(spans):
    a = _src
    return [zstd.compress(decompress_tile(a.tcomp, a.mm[a.tile_off + o:a.tile_off + o + n]), LEVEL)
            for o, n in spans]


def entries(a, d=None):
    """Yield (tileId, runLength, offset, length) for every tile entry, in tileId order."""
    ids, rl, ln, off, n = d or a._dir(a.root_off, a.root_len)
    for k in range(n):
        if rl[k] == 0:
            yield from entries(a, a._dir(a.leaf_off + off[k], ln[k]))
        else:
            yield ids[k], rl[k], off[k], ln[k]


def _wv(out, n):
    while True:
        b = n & 0x7f; n >>= 7
        out.append(b | 0x80 if n else b)
        if not n: break


def serialize(ents):
    out = bytearray()
    _wv(out, len(ents))
    last = 0
    for tid, _, _, _ in ents:
        _wv(out, tid - last); last = tid
    for _, rl, _, _ in ents: _wv(out, rl)
    for _, _, _, ln in ents: _wv(out, ln)
    for k, (_, _, off, _) in enumerate(ents):
        prev = ents[k - 1] if k else None
        _wv(out, 0 if prev and off == prev[2] + prev[3] else off + 1)
    return gzip.compress(bytes(out), mtime=0)


def directories(ents):
    """(root, leaves) as go-pmtiles splits them: a root alone if it fits,
    else leaves of a growing entry count until the root over them fits."""
    root = serialize(ents)
    if len(root) <= ROOT_MAX:
        return root, b""
    leaf_size = 4096
    while True:
        leaves = bytearray(); root_ents = []
        for i in range(0, len(ents), leaf_size):
            chunk = ents[i:i + leaf_size]
            s = serialize(chunk)
            root_ents.append((chunk[0][0], 0, len(leaves), len(s)))
            leaves += s
        root = serialize(root_ents)
        if len(root) <= ROOT_MAX:
            return root, bytes(leaves)
        leaf_size = int(leaf_size * 1.2)


def recompress(src_path, out_path, workers=None):
    a = Archive(src_path)
    if a.icomp != 2:
        raise ValueError(f"{src_path}: expected gzip directories, got compression {a.icomp}")
    ents = list(entries(a))

    # Each stored blob once, in source order, which keeps a clustered archive clustered.
    blobs = sorted({(off, ln) for _, _, off, ln in ents})
    moved = {}
    with tempfile.TemporaryFile() as data:
        spans = [blobs[i:i + CHUNK] for i in range(0, len(blobs), CHUNK)]
        with ProcessPoolExecutor(workers, initializer=_open_source, initargs=(src_path,)) as ex:
            for chunk, out in zip(spans, ex.map(_compress, spans, buffersize=64)):
                for key, z in zip(chunk, out):
                    moved[key] = (data.tell(), len(z))
                    data.write(z)
        data_len = data.tell()

        ents = [(tid, rl, *moved[(off, ln)]) for tid, rl, off, ln in ents]
        root, leaves = directories(ents)
        meta_off, meta_len = struct.unpack_from("<QQ", a.mm, 24)
        meta = a.mm[meta_off:meta_off + meta_len]

        head = bytearray(a.mm[:HEADER_LEN])
        root_off = HEADER_LEN
        meta_off = root_off + len(root)
        leaf_off = meta_off + len(meta)
        data_off = leaf_off + len(leaves)
        struct.pack_into("<11Q", head, 8,
                         root_off, len(root), meta_off, len(meta), leaf_off, len(leaves),
                         data_off, data_len,
                         sum(rl for _, rl, _, _ in ents), len(ents), len(blobs))
        head[98] = ZSTD

        with open(out_path, "wb") as f:
            f.write(head); f.write(root); f.write(meta); f.write(leaves)
            data.seek(0)
            shutil.copyfileobj(data, f, 1 << 20)
    print(f"zstd: {src_path} -> {out_path}  {os.path.getsize(src_path) / 1e6:.1f} -> "
          f"{os.path.getsize(out_path) / 1e6:.1f} MB ({len(blobs):,} tiles)", file=sys.stderr)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    recompress(sys.argv[1], sys.argv[2])
