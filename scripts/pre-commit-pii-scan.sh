#!/usr/bin/env bash
# pre-commit-pii-scan.sh — reject staged changes that contain a redaction
# placeholder or an identity fingerprint.
#
# Identity-free patterns only (safe to commit here). The authoritative
# full-tree sweep is scripts/full_pii_scan.sh, run in CI.
set -uo pipefail

PATTERNS=(
  '<REDACTED>'
  '<USER_[A-Z_]+>'
  'TODO[_ ]?PII'
  '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.edu\.cn'    # any education email
  '[Cc]:\\Users\\[A-Za-z0-9._-]+'                # Windows absolute home path
  '维格纳|[Ww]igner|凝聚态'                        # research-domain fingerprint
  'memexa\.core|kairos_daemon'                    # proprietary engine module
)
PATTERN_JOINED=$(IFS="|"; echo "${PATTERNS[*]}")

STAGED=$(git diff --cached --name-only --diff-filter=ACMR)
[ -z "$STAGED" ] && exit 0

HITS=0
for f in $STAGED; do
  [ -f "$f" ] || continue
  case "$f" in
    scripts/pre-commit-pii-scan.sh|scripts/full_pii_scan.sh|scripts/pii_patterns*) continue ;;
  esac
  if grep -nE "$PATTERN_JOINED" "$f" >/dev/null 2>&1; then
    HITS=1
    echo "PII/fingerprint hit in: $f"
  fi
done

if [ "$HITS" -eq 1 ]; then
  echo ""
  echo "Staged changes contain a redaction placeholder or an identity"
  echo "fingerprint. Sanitise the value before committing."
  exit 1
fi
exit 0
