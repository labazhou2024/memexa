#!/usr/bin/env bash
# Reject staged changes that contain real-identifier tokens.
#
# Hard rule: this list MUST match the GitHub Actions ``pii-scan`` job
# in ``.github/workflows/ci.yml`` so the local hook and CI agree.
set -euo pipefail

PATTERNS=(
    "DemoUser"
    "OurUser"
    "demo_user[^_]"
    "labazhou2024"
    "test_handle_1"
    "<USER_QQ_ID>"
    "<REDACTED>"
    "@mail\.ustc\.edu\.cn"
    "100\.73\.32\.96"
    "222\.195\.68\.89"
)

PATTERN_JOINED=$(IFS="|"; echo "${PATTERNS[*]}")

# Grep the staged diff only (not the whole tree).
STAGED=$(git diff --cached --name-only --diff-filter=ACMR)
if [ -z "$STAGED" ]; then
    exit 0
fi

HITS=0
for f in $STAGED; do
    if [ ! -f "$f" ]; then
        continue
    fi
    # Skip binary files
    if file --mime "$f" | grep -q "binary"; then
        continue
    fi
    if grep -nE "$PATTERN_JOINED" "$f" > /tmp/pii_hits_$$.txt 2>/dev/null; then
        HITS=1
        echo "❌ $f"
        cat /tmp/pii_hits_$$.txt
    fi
done
rm -f /tmp/pii_hits_$$.txt

if [ "$HITS" -eq 1 ]; then
    echo ""
    echo "Staged changes contain real-identifier tokens. Either sanitise the"
    echo "value or update the placeholder so the test passes."
    exit 1
fi
exit 0
