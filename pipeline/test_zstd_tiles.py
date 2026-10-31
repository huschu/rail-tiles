"""Recompressing a PMTiles archive to zstd.

Run: python3 -m unittest discover -s pipeline -p 'test_*.py'  (or `make test`)
"""
import gzip
import os
import struct
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import zstd_tiles as Z
from diff_tiles import Archive, tileid_to_zxy

META = gzip.compress(b'{"attribution":"OSM"}', mtime=0)


def gzip_archive(path, tiles):
    """tiles: [(tileId, runLength, payload)], payloads repeated by equality share one blob."""
    data = bytearray(); at = {}; ents = []
    for tid, rl, payload in tiles:
        if payload not in at:
            z = gzip.compress(payload, mtime=0)
            at[payload] = (len(data), len(z)); data += z
        ents.append((tid, rl, *at[payload]))
    root, leaves = Z.directories(ents)
    head = bytearray(127)
    head[:8] = b"PMTiles\x03"
    root_off = 127; meta_off = root_off + len(root)
    leaf_off = meta_off + len(META); data_off = leaf_off + len(leaves)
    struct.pack_into("<11Q", head, 8, root_off, len(root), meta_off, len(META),
                     leaf_off, len(leaves), data_off, len(data),
                     sum(rl for _, rl, _ in tiles), len(ents), len(at))
    max_z = tileid_to_zxy(tiles[-1][0] + tiles[-1][1] - 1)[0]
    head[96:102] = bytes([1, 2, 2, 1, 0, max_z])
    struct.pack_into("<4i", head, 102, -1800000000, -850000000, 1800000000, 850000000)
    with open(path, "wb") as f:
        f.write(head + root + META + leaves + data)


def contents(path):
    a = Archive(path)
    return {tid: a.tile_at(off, ln) for tid, off, ln in a.tiles()}


class RecompressTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.src = os.path.join(self.dir.name, "in.pmtiles")
        self.out = os.path.join(self.dir.name, "out.pmtiles")

    def tearDown(self):
        self.dir.cleanup()

    def recompress(self, tiles):
        gzip_archive(self.src, tiles)
        Z.recompress(self.src, self.out, workers=2)
        return Archive(self.out)

    def test_tiles_decode_to_the_same_bytes(self):
        tiles = [(i, 1, f"tile {i} ".encode() * (i % 7 + 1)) for i in range(2000)]
        a = self.recompress(tiles)
        self.assertEqual(a.tcomp, 4)
        self.assertEqual(a.icomp, 2)
        self.assertEqual(contents(self.out), contents(self.src))

    def test_shared_blobs_and_run_lengths_survive(self):
        ocean = b"ocean" * 50
        tiles = [(0, 1, b"z0"), (1, 3, ocean), (4, 1, b"land"), (5, 2, ocean), (9, 1, b"land")]
        self.recompress(tiles)
        a = Archive(self.out)
        counts = struct.unpack_from("<3Q", a.mm, 72)
        self.assertEqual(counts, (8, 5, 3))
        self.assertEqual(len({(o, n) for _, o, n in a.tiles()}), 3)
        self.assertEqual(contents(self.out), contents(self.src))

    def test_a_root_too_large_splits_into_leaves(self):
        tiles = [(i * 2, 1, str(i).encode()) for i in range(5000)]
        with mock.patch.object(Z, "ROOT_MAX", 60):
            a = self.recompress(tiles)
        self.assertGreater(struct.unpack_from("<Q", a.mm, 48)[0], 0)
        self.assertEqual(contents(self.out), contents(self.src))

    def test_metadata_is_copied(self):
        a = self.recompress([(0, 1, b"x")])
        off, n = struct.unpack_from("<QQ", a.mm, 24)
        self.assertEqual(a.mm[off:off + n], META)

    def test_header_keeps_everything_but_the_tile_codec(self):
        a = self.recompress([(i, 1, f"t{i}".encode()) for i in range(50)])
        with open(self.src, "rb") as f:
            want = bytearray(f.read(127)[96:])
        want[98 - 96] = 4
        self.assertEqual(bytes(a.mm[96:127]), bytes(want))
        self.assertFalse(os.path.exists(self.out + ".part"))

    def test_hashes_ignore_the_codec(self):
        from diff_tiles import cmd_hash, read_hashes
        self.recompress([(i, 1, f"t{i}".encode()) for i in range(50)])
        h_in, h_out = (os.path.join(self.dir.name, n) for n in ("in.hashes", "out.hashes"))
        cmd_hash(self.src, h_in); cmd_hash(self.out, h_out)
        self.assertEqual(read_hashes(h_in), read_hashes(h_out))


if __name__ == "__main__":
    unittest.main()
