#!/usr/bin/env bash
# full_pii_scan.sh — full-tree PII + structural-fingerprint sweep.
#
# Exit 0 when clean, exit 1 on any match. Match COUNTS only are printed,
# never the matched text, so output is safe to paste into CI logs.
#
# Two layers, so the scan has REAL coverage even on a fresh clone / CI
# (the previous version fell back to an all-commented template and
# silently reported zero — a false sense of safety):
#
#   1. STRUCTURAL patterns (committed, always on). Generic shapes that
#      must never appear in this public demo: China education-email
#      domains, absolute user-home paths, research-domain fingerprints,
#      proprietary engine module names. These carry no specific identity,
#      so they live here in the repository.
#
#   2. TOKEN patterns (scripts/pii_patterns.txt, git-ignored). The
#      maintainer's specific real tokens, layered on when present. Absent
#      on a fresh clone, where layer 1 still provides real coverage.

set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Layer 1 — structural fingerprints (committed, identity-free).
STRUCTURAL_PATTERNS=(
  '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.edu\.cn'    # any China education email
  '[Cc]:\\Users\\[A-Za-z0-9._-]+'                # Windows absolute home path
  '/Users/[a-z][A-Za-z0-9._-]+'                  # macOS absolute home path
  '/home/[a-z][A-Za-z0-9._-]+'                   # Linux absolute home path
  '[Ww]igner|electron[ _-]?on[ _-]?neon'         # research-domain fingerprint
  '维格纳|凝聚态|量子比特'                         # research-domain fingerprint
  'memexa\.core|kairos_daemon|super_strateg'     # proprietary engine module names
)

PATTERN_FILE="${ROOT}/scripts/pii_patterns.txt"

# Files that legitimately contain the patterns above as detector strings
# (and would otherwise self-trigger).
SKIP_PATHS=(
  ".github/workflows/ci.yml"
  ".github/workflows/security.yml"
  "scripts/pre-commit-pii-scan.sh"
  "scripts/full_pii_scan.sh"
  "scripts/pii_patterns.txt"
  "scripts/pii_patterns.example.txt"
  "Makefile"
)

# Build the combined pattern set: structural layer always, token layer
# when the git-ignored file is present.
TMP_PAT="$(mktemp -t memexa_pii.XXXXXX)"
trap 'rm -f "${TMP_PAT}"' EXIT
printf '%s\n' "${STRUCTURAL_PATTERNS[@]}" > "${TMP_PAT}"
if [ -f "${PATTERN_FILE}" ]; then
  grep -vE '^[[:space:]]*(#|$)' "${PATTERN_FILE}" >> "${TMP_PAT}"
fi

EXCLUDE_FIND=()
for p in "${SKIP_PATHS[@]}"; do
  EXCLUDE_FIND+=( -not -path "*/${p}" )
done

scan_files() {
  # $1 = newline-separated file list
  [ -z "$1" ] && return 0
  echo "$1" | xargs grep -EHf "${TMP_PAT}" 2>/dev/null | wc -l | tr -d ' '
}

hits=0
for dir in memexa examples tests scripts docs config .github; do
  [ -d "${ROOT}/${dir}" ] || continue
  files=$(find "${ROOT}/${dir}" -type f \
            \( -name '*.py' -o -name '*.md' -o -name '*.yml' -o -name '*.yaml' \
               -o -name '*.json' -o -name '*.jsonl' -o -name '*.toml' -o -name '*.sh' \
               -o -name '*.cfg' -o -name '*.txt' \) \
            "${EXCLUDE_FIND[@]}" 2>/dev/null)
  count=$(scan_files "${files}")
  if [ "${count:-0}" -gt 0 ]; then
    echo "  [hit] ${dir}/  ${count} match(es)"
    hits=$((hits + count))
  fi
done

# Top-level files (README, pyproject, etc.).
top_files=$(find "${ROOT}" -maxdepth 1 -type f \
              \( -name '*.md' -o -name '*.toml' -o -name '*.cfg' -o -name '*.txt' \) 2>/dev/null)
count=$(scan_files "${top_files}")
if [ "${count:-0}" -gt 0 ]; then
  echo "  [hit] (top-level)  ${count} match(es)"
  hits=$((hits + count))
fi

if [ "${hits}" -gt 0 ]; then
  echo "PII/fingerprint scan FAILED: ${hits} match(es) across the tree" >&2
  exit 1
fi
echo "PII/fingerprint scan clean: 0 matches"
exit 0
