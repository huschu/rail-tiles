#!/bin/bash
# Fetch and filter one region: download the extract (mirror fallback + retries),
# osmium tags-filter to railway ways, delete the raw immediately so peak disk is
# one extract. Writes <name>-rail.osm.pbf and <name>.timestamp (the extract's own
# osmium timestamp, for the manifest's "data as of").
#
# Usage: fetch_region.sh <name> <mirror-relative-path> <out-dir>
#   e.g. fetch_region.sh us-california north-america/us/california-latest.osm.pbf work
set -uo pipefail
NAME="${1:?name required}"
RELPATH="${2:?mirror-relative path required}"
OUT="${3:?out dir required}"
mkdir -p "$OUT"

RAW="$OUT/$NAME-raw.osm.pbf"
FILT="$OUT/$NAME-rail.osm.pbf"
KINDS="rail,light_rail,narrow_gauge,construction,proposed,disused,abandoned,monorail,subway,tram"

MIRRORS=(
  "https://download.geofabrik.de/$RELPATH"
  "https://download.openstreetmap.fr/extracts/$RELPATH"
)

ok=0
for URL in "${MIRRORS[@]}"; do
  echo "[$NAME] trying $URL"
  # Abort a genuinely stalled download so it retries and then fails over to the
  # mirror, but do NOT kill a slow-but-progressing one. Geofabrik throttles the
  # big extracts hard: a ~4.6 GB country can take ~1 h, which is legitimate.
  # --speed-limit/-time only aborts under 50 KB/s for three minutes (a real
  # stall); --max-time is a generous 2 h ceiling; -C - resumes on retry.
  # Retry long enough to outlast Geofabrik's daily regeneration, during which an
  # extract briefly 404s. The OSM-France mirror lacks many smaller regions (404s
  # immediately), so for those Geofabrik is the only source and this retry is the
  # only safety net. 8 retries x 15 s ~= 2 min per mirror.
  if curl -L --fail --retry 8 --retry-delay 15 --retry-all-errors -C - \
          --connect-timeout 30 --speed-limit 51200 --speed-time 180 \
          --max-time 7200 \
          -o "$RAW" "$URL"; then
    echo "[$NAME] downloaded $(du -h "$RAW" | cut -f1)"
    ok=1; break
  fi
  echo "[$NAME] mirror failed, trying next"
  rm -f "$RAW"
done
[ "$ok" = 1 ] || { echo "[$NAME] ALL MIRRORS FAILED"; exit 1; }

# Extract timestamp before we delete the raw.
osmium fileinfo -e -g data.timestamp.last "$RAW" > "$OUT/$NAME.timestamp" 2>/dev/null || true
echo "[$NAME] extract timestamp: $(cat "$OUT/$NAME.timestamp" 2>/dev/null)"

echo "[$NAME] filtering to railway ways..."
if nice -n 10 osmium tags-filter "$RAW" "w/railway=$KINDS" -o "$FILT" --overwrite; then
  echo "[$NAME] filtered -> $(du -h "$FILT" | cut -f1)"
else
  echo "[$NAME] FILTER FAILED"; rm -f "$RAW"; exit 1
fi
rm -f "$RAW"
echo "[$NAME] raw deleted"
