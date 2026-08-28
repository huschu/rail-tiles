#!/bin/bash
# Fetch and filter one region: download the extract (mirror fallback + retries),
# osmium tags-filter to railway ways, delete the raw immediately so peak disk is
# one extract. Writes <slug>-rail.osm.pbf and <slug>.timestamp (the extract's own
# osmium timestamp, for the manifest's "data as of").
#
# Usage: fetch_region.sh <slug> <out-dir>
set -uo pipefail
SLUG="${1:?slug required}"
OUT="${2:?out dir required}"
mkdir -p "$OUT"

RAW="$OUT/$SLUG-raw.osm.pbf"
FILT="$OUT/$SLUG-rail.osm.pbf"
KINDS="rail,light_rail,narrow_gauge,construction,proposed,disused,abandoned,monorail,subway,tram"

MIRRORS=(
  "https://download.geofabrik.de/europe/$SLUG-latest.osm.pbf"
  "https://download.openstreetmap.fr/extracts/europe/$SLUG-latest.osm.pbf"
)

ok=0
for URL in "${MIRRORS[@]}"; do
  echo "[$SLUG] trying $URL"
  # Abort a genuinely stalled download so it retries and then fails over to the
  # mirror, but do NOT kill a slow-but-progressing one. Geofabrik throttles the
  # big extracts hard: Germany (~4.6 GB) came down at ~1.2 MB/s = ~61 min, which
  # is legitimate. --speed-limit/-time only aborts a connection under 50 KB/s for
  # three minutes (a real stall); --max-time is a generous 2 h ceiling that a
  # throttled multi-GB download still fits inside. -C - resumes on retry rather
  # than restarting from zero.
  if curl -L --fail --retry 4 --retry-delay 8 --retry-all-errors -C - \
          --connect-timeout 30 --speed-limit 51200 --speed-time 180 \
          --max-time 7200 \
          -o "$RAW" "$URL"; then
    echo "[$SLUG] downloaded $(du -h "$RAW" | cut -f1)"
    ok=1; break
  fi
  echo "[$SLUG] mirror failed, trying next"
  rm -f "$RAW"
done
[ "$ok" = 1 ] || { echo "[$SLUG] ALL MIRRORS FAILED"; exit 1; }

# Extract timestamp before we delete the raw.
osmium fileinfo -e -g data.timestamp.last "$RAW" > "$OUT/$SLUG.timestamp" 2>/dev/null || true
echo "[$SLUG] extract timestamp: $(cat "$OUT/$SLUG.timestamp" 2>/dev/null)"

echo "[$SLUG] filtering to railway ways..."
if nice -n 10 osmium tags-filter "$RAW" "w/railway=$KINDS" -o "$FILT" --overwrite; then
  echo "[$SLUG] filtered -> $(du -h "$FILT" | cut -f1)"
else
  echo "[$SLUG] FILTER FAILED"; rm -f "$RAW"; exit 1
fi
rm -f "$RAW"
echo "[$SLUG] raw deleted"
