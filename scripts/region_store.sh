#!/bin/bash
# The last good build of each region, kept as one asset per region on the
# build-internal `last-good` release. A region job falls back to it when it has
# no fresh build and the Actions cache holds no older one, which happens when
# GitHub evicts or expires cache entries. Release assets are never evicted.
#
# A bundle holds the region's work/ files: the tileset and its sidecars, the
# track graph, the timestamp, a <name>.norail marker for a region without
# railways, and <name>.code, the code hashes it was built with. Each save
# uploads under a new name, and the join's sweep deletes all but each region's
# newest bundle, so a failed upload leaves the previous bundle in place. Saving
# without listing or deleting keeps the 242 region jobs within the token's
# 1,000 API requests an hour.
#
# Usage: region_store.sh save <name> <work-dir>
#        region_store.sh restore <name> <work-dir> [member...]
#        region_store.sh sweep
# Needs GH_TOKEN and GITHUB_REPOSITORY, and for save and restore CODE, the
# build's code hashes. save names the bundle after GITHUB_RUN_ID and
# GITHUB_RUN_ATTEMPT. restore extracts only the given members when there are
# any, and warns when the bundle was built by other code: a carried-forward
# build may lack newer attributes.
set -uo pipefail
CMD="${1:?save, restore or sweep}"
TAG=last-good
REPO="$GITHUB_REPOSITORY"

# "<id> <name>" of every bundle, oldest first within each region. Region names
# are [a-z0-9-] only, so the first dot ends the name.
bundles() {
  local id
  id=$(gh api "repos/$REPO/releases/tags/$TAG" -q .id 2>/dev/null) || return 1
  gh api --paginate "repos/$REPO/releases/$id/assets?per_page=100" -q '.[] | "\(.id) \(.name)"' \
    | { grep -E '^[0-9]+ [a-z0-9-]+\.[0-9]+-[0-9]+\.tar\.zst$' || true; } \
    | sort -k2,2V
}

case "$CMD" in
  save)
    NAME="${2:?name required}"; WORK="${3:?work dir required}"
    printf '%s\n' "$CODE" > "$WORK/$NAME.code"
    FILES=()
    for f in "$NAME.pmtiles" "$NAME.night.json" "$NAME.stations.geojsonseq" "$NAME.border.json" \
             "$NAME.graph.jsonl.zst" "$NAME.timestamp" "$NAME.norail" "$NAME.code"; do
      [ -f "$WORK/$f" ] && FILES+=("$f")
    done
    BUNDLE="${RUNNER_TEMP:-/tmp}/$NAME.$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT.tar.zst"
    tar --zstd -cf "$BUNDLE" -C "$WORK" "${FILES[@]}" || { echo "::warning::$NAME: could not pack the last good build"; exit 0; }
    for attempt in 1 2 3; do
      gh release upload "$TAG" "$BUNDLE" --clobber --repo "$REPO" && break
      if [ "$attempt" -eq 3 ]; then echo "::warning::$NAME: could not upload the last good build"; exit 0; fi
      sleep 30
    done
    echo "$NAME: saved $(basename "$BUNDLE") ($(du -h "$BUNDLE" | cut -f1))"
    ;;
  restore)
    NAME="${2:?name required}"; WORK="${3:?work dir required}"; shift 3
    MEMBERS=(); [ $# -eq 0 ] || MEMBERS=("$NAME.code" "$@")
    # The dot after the name keeps guinea apart from guinea-bissau.
    read -r asset name < <(bundles | { grep -F " $NAME." || true; } | tail -n 1)
    [ -n "${asset:-}" ] || { echo "$NAME: no last good build stored"; exit 1; }
    # Unpacked aside first: a download cut short must not leave a truncated
    # tileset in work/ for the build step to carry forward.
    TMP=$(mktemp -d "${RUNNER_TEMP:-/tmp}/$NAME-restore.XXXXXX")
    gh api -H "Accept: application/octet-stream" "repos/$REPO/releases/assets/$asset" \
      | tar --zstd -xf - -C "$TMP" ${MEMBERS[@]+"${MEMBERS[@]}"} \
      || { echo "::warning::$NAME: could not restore $name"; exit 1; }
    mv -f "$TMP"/* "$WORK"/
    echo "$NAME: restored $name"
    if [ "$(cat "$WORK/$NAME.code")" != "$CODE" ]; then
      echo "::warning::$NAME: the last good build is from older code ($(cat "$WORK/$NAME.code"))"
    fi
    ;;
  sweep)
    LIST=$(bundles) || { echo "no $TAG release"; exit 0; }
    KEEP=$(awk '{split($2, p, "."); newest[p[1]] = $1} END {for (r in newest) print newest[r]}' <<< "$LIST")
    echo "$LIST" | awk 'NF' | while read -r asset name; do
      grep -qx "$asset" <<< "$KEEP" && continue
      echo "delete $name"
      gh api -X DELETE "repos/$REPO/releases/assets/$asset" >/dev/null \
        || echo "::warning::could not delete the older bundle $name"
    done
    echo "$(awk 'NF' <<< "$KEEP" | wc -l | tr -d ' ') regions in $TAG"
    ;;
  *)
    echo "usage: region_store.sh save <name> <work-dir> | restore <name> <work-dir> [member...] | sweep" >&2; exit 2
    ;;
esac
