#!/usr/bin/env bash
# Package the session's deliverables as a single downloadable archive.
#
#   ./tools/make_artifact_zip.sh
#
# The pull request is the real delivery path; this is a backup copy.
set -euo pipefail

cd "$(dirname "$0")/../../.."          # repo root
OUT="dist/session-b-artifacts.zip"
mkdir -p dist
rm -f "$OUT"

zip -q -r "$OUT" \
    pipelines/course-ingest \
    backend/tests/golden \
    -x '*/__pycache__/*' '*.pyc' '*/.pytest_cache/*' '*/.cache/*' '*.egg-info/*'

echo "$OUT  $(du -h "$OUT" | cut -f1)"
unzip -l "$OUT" | tail -1
