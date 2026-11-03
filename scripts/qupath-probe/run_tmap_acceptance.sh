#!/usr/bin/env bash
# TMAP06 QuPath default-reader acceptance (see
# docs/slide-tools/tmap06-compatibility-update-plan-20261103.md 「验收矩阵」).
#
# usage: run_tmap_acceptance.sh <qupath-root> <out-dir> <image> [--expect-classic] \
#                               [--roi name,level,x,y,w,h ...] [--min-textured K]
#
# Runs scripts/qupath-probe/tmap-acceptance-probe.groovy with the QuPath
# launcher's own JVM (no reader forced, no preference changed), then validates
# the evidence with check_tmap_acceptance.py (tifffile decode of the same
# product). All JVM temp/home/prefs state is kept under <out-dir>.
#
# Environment:
#   JAVA_TOOL_OPTIONS_EXTRA  extra JVM options appended (optional)
#   QUPATH_HEAP              -Xmx value (default 2g)
#   PYTHON                   interpreter for the checker (default python3)
#
# Exit status:
#   0    QuPath exited 0 AND every evidence check passed
#   nonzero  QuPath/timeout exit status (124 = timeout, 127 = launcher missing)
#  2    QuPath exited 0 but the evidence is missing/incomplete
set -euo pipefail

usage() {
    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
}

if [ $# -lt 3 ]; then
    echo "run_tmap_acceptance: usage: run_tmap_acceptance.sh <qupath-root> <out-dir> <image> [flags...]" >&2
    usage >&2
    exit 64
fi

E=$(cd "$(dirname "$0")" && pwd)
Q=$1; OUT=$2; IMG=$3
shift 3
CLASSIC=0
ROIS=()
MIN_TEXTURED=2
while [ $# -gt 0 ]; do
    case "$1" in
        --expect-classic) CLASSIC=1; shift ;;
        --roi) ROIS+=("$2"); shift 2 ;;
        --min-textured) MIN_TEXTURED=$2; shift 2 ;;
        *) echo "run_tmap_acceptance: unknown flag: $1" >&2; exit 64 ;;
    esac
done

if [ ! -x "$Q/bin/QuPath" ]; then
    echo "run_tmap_acceptance: FAIL: QuPath launcher not found/executable: $Q/bin/QuPath" >&2
    exit 127
fi
if [ ! -f "$IMG" ]; then
    echo "run_tmap_acceptance: FAIL: image not found: $IMG" >&2
    exit 66
fi

rm -rf "$OUT"; mkdir -p "$OUT/regions" "$OUT/project" "$OUT/tmp" \
    "$OUT/java-home" "$OUT/java-prefs"

ARGS=(-a "$IMG" -a "$OUT/probe.json" -a "$OUT/regions" -a "$OUT/project")
for roi in ${ROIS[@]+"${ROIS[@]}"}; do
    ARGS+=(-a "$roi")
done

set +e
JAVA_TOOL_OPTIONS="-Xmx${QUPATH_HEAP:-2g} -Djava.io.tmpdir=$OUT/tmp \
-Djava.util.prefs.userRoot=$OUT/java-prefs -Duser.home=$OUT/java-home \
${JAVA_TOOL_OPTIONS_EXTRA:-}" \
timeout 900 "$Q/bin/QuPath" script "$E/tmap-acceptance-probe.groovy" \
    "${ARGS[@]}" > "$OUT/probe.log" 2>&1
rc=$?
set -e

if [ "$rc" -ne 0 ]; then
    echo "run_tmap_acceptance: FAIL: QuPath/timeout exited $rc (log: $OUT/probe.log)" >&2
    exit "$rc"
fi
if [ ! -f "$OUT/probe.json" ]; then
    echo "run_tmap_acceptance: FAIL: probe.json missing (log: $OUT/probe.log)" >&2
    exit 2
fi

PYTHON=${PYTHON:-python3}
CHECK=(--min-textured "$MIN_TEXTURED")
if [ "$CLASSIC" -eq 1 ]; then
    CHECK+=(--expect-classic)
fi
if ! "$PYTHON" "$E/check_tmap_acceptance.py" "$OUT/probe.json" \
        "$OUT/regions" "$IMG" "$OUT/check.json" ${CHECK[@]+"${CHECK[@]}"}; then
    echo "run_tmap_acceptance: FAIL: evidence check failed (see $OUT/check.json)" >&2
    exit 2
fi
echo "run_tmap_acceptance: OK: $OUT/check.json"
