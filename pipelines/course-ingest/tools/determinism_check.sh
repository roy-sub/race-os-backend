#!/usr/bin/env bash
# Determinism proof: regenerate every `ready` course twice, end to end, and diff.
#
# The pipeline's headline guarantee is that the same seed spec produces
# byte-identical output on every run. That is what makes a season-over-season
# bundle diff meaningful: if the output moved, an input moved. This script is
# how that claim is checked against the real data sources rather than the
# offline test fixtures.
#
#   ./tools/determinism_check.sh [cache_dir]
#
# Exits non-zero on any difference.
set -euo pipefail

cd "$(dirname "$0")/.."
CACHE="${1:-.cache}"
A=$(mktemp -d); B=$(mktemp -d)
trap 'rm -rf "$A" "$B"' EXIT

echo "=== run A -> $A"
python3 -m course_ingest.cli regenerate-all --cache "$CACHE" --out "$A" \
    --skip-terrain --skip-visuals >/dev/null

echo "=== run B -> $B"
python3 -m course_ingest.cli regenerate-all --cache "$CACHE" --out "$B" \
    --skip-terrain --skip-visuals >/dev/null

echo "=== diff"
if diff -r "$A/bundles" "$B/bundles"; then
    echo
    for f in "$A"/bundles/*; do
        printf '%-46s %s\n' "$(basename "$f")" "$(sha256sum <"$f" | cut -c1-64)"
    done
    echo
    echo "PASS: $(ls "$A"/bundles | wc -l) artefacts byte-identical across two full runs"
else
    echo "FAIL: output differs between runs"
    exit 1
fi
