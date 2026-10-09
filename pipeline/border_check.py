#!/usr/bin/env python3
"""
Check the border partition across all regions of a build.

Each region writes `<region>.border.json` (build_region.py --border): the ways
it left to a neighbour and the border ways it kept. A way left to a neighbour
that the neighbour did not keep is lost from the map (a gap); a way two regions
kept is drawn twice. A neighbour without a record (it failed, or carried
forward a tileset from before the partition) cannot be checked.

Prints a summary, the duplicates, and a GitHub warning for lost and unchecked
ways; exits 0, since a gap from a carried-forward region heals when it
rebuilds.

Usage: border_check.py REGIONS_DIR
"""
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import partition


def main():
    records = {}
    for p in glob.glob(os.path.join(sys.argv[1], "**", "*.border.json"), recursive=True):
        records[os.path.basename(p)[:-len(".border.json")]] = json.load(open(p))
    gaps, dups, missing = partition.check(records)
    given = sum(len(r["given"]) for r in records.values())
    print(f"border check over {len(records)} regions: {given:,} ways left to neighbours, "
          f"{sum(gaps.values()):,} lost, {sum(dups.values()):,} kept twice, "
          f"{sum(missing.values()):,} left to regions without a record")
    def top(counts):
        return ", ".join(f"{k} {v:,}" for k, v in sorted(counts.items(), key=lambda t: -t[1])[:12])
    # A few duplicates per border are expected: ways with a node within the
    # margin of the line. Lost and unchecked ways are not.
    if dups:
        print(f"kept twice: {top(dups)}")
    for kind, counts in (("lost", gaps), ("unchecked, no record", missing)):
        if counts:
            print(f"::warning title=Border ways {kind}::{top(counts)}")


if __name__ == "__main__":
    main()
