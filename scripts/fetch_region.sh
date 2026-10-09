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
# Keep every track kind the classifier knows (incl. funicular) plus every
# lifecycle form: railway=<value> and the prefixed keys (construction:railway,
# disused:railway, ...). Must stay a superset of classify.PASSENGER_KINDS and
# classify.lifecycle, or a kind is silently dropped before tiling.
FILTER=(
  "w/railway=rail,light_rail,narrow_gauge,monorail,subway,tram,funicular,construction,proposed,disused,abandoned,razed,preserved"
  w/construction:railway w/proposed:railway w/disused:railway
  w/abandoned:railway w/razed:railway w/preserved:railway w/railway:preserved
  # Stations (pipeline/stations.py). Station areas keep their nodes so they get
  # a centroid. The stop positions routes name come with the track ways they
  # sit on; selecting them by tag would add every bus stop.
  n/railway=station,halt,stop,tram_stop n/public_transport=station
  w/railway=station w/public_transport=station
)

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
    if [ "$URL" != "${MIRRORS[0]}" ]; then
      # The border partition assumes Geofabrik's boundaries (pipeline/partition.py).
      echo "::warning::$NAME: extract from a fallback mirror, cut along other boundaries; border ways may be missing or doubled until Geofabrik serves it again"
    fi
    ok=1; break
  fi
  echo "[$NAME] mirror failed, trying next"
  rm -f "$RAW"
done
[ "$ok" = 1 ] || { echo "[$NAME] ALL MIRRORS FAILED"; exit 1; }

# Extract timestamp before we delete the raw.
osmium fileinfo -e -g data.timestamp.last "$RAW" > "$OUT/$NAME.timestamp" 2>/dev/null || true
echo "[$NAME] extract timestamp: $(cat "$OUT/$NAME.timestamp" 2>/dev/null)"

echo "[$NAME] filtering to railway ways and route relations..."
# Route relations carry the line colours and the route membership chains split
# on (pipeline/routes.py): services and the lines (route=railway, tracks)
# the app highlights. Stop areas tie a station to the stops routes name
# (pipeline/stations.py). -R keeps them
# without their members: only their tags and member way ids are read, and the
# members would drag in platforms and stops. -R cannot apply to the ways, which
# need their nodes, hence two passes merged into one extract.
WAYS="$OUT/$NAME-ways.osm.pbf"
ROUTES="$OUT/$NAME-routes.osm.pbf"
if nice -n 10 osmium tags-filter "$RAW" "${FILTER[@]}" -o "$WAYS" --overwrite \
   && nice -n 10 osmium tags-filter "$RAW" "r/route=train,subway,light_rail,tram,monorail,funicular,railway,tracks" \
        r/public_transport=stop_area -R -o "$ROUTES" --overwrite \
   && osmium merge "$WAYS" "$ROUTES" -o "$FILT" --overwrite; then
  echo "[$NAME] filtered -> $(du -h "$FILT" | cut -f1)"
  rm -f "$WAYS" "$ROUTES"
else
  echo "[$NAME] FILTER FAILED"; rm -f "$RAW" "$WAYS" "$ROUTES"; exit 1
fi
rm -f "$RAW"
echo "[$NAME] raw deleted"
