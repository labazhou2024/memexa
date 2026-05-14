#!/usr/bin/env bash
# full_pii_scan.sh — full-tree PII residual sweep.
#
# Returns exit 0 when no PII pattern hit, exit 1 when at least one match.
# Used by `make pii-scan` as the fallback when the staged-diff scanner
# cannot run (e.g. CI on a checkout, or a manual full-tree audit).
#
# Behavior contract for OSS contributors:
#
#   * The pattern file `scripts/pii_patterns.txt` is the single source of
#     truth. Edit that file, never inline patterns into Makefile.
#   * Match counts only — never echo matched lines, to keep CI logs and
#     local terminals safe to share.
#
# The script intentionally suppresses match content (only file + count
# is printed). To inspect matches, run `bash scripts/pre-commit-pii-scan.sh`
# locally with the staged diff loaded; never paste matched content into
# issues or pull requests.

set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PATTERN_FILE="${ROOT}/scripts/pii_patterns.txt"

if [ ! -f "${PATTERN_FILE}" ]; then
  echo "missing pattern file: ${PATTERN_FILE}" >&2
  exit 2
fi

# Materialize a comments-stripped pattern file (grep -f treats lines
# starting with '#' as literal patterns, which would match every code
# comment in the tree).
TMP_PAT="$(mktemp -t memex_pii.XXXXXX)"
trap 'rm -f "${TMP_PAT}"' EXIT
grep -vE '^[[:space:]]*(#|$)' "${PATTERN_FILE}" > "${TMP_PAT}"

# Scan src + docs + tests + config + examples + .github + top-level files
# but NOT the .audit/ research dir (it intentionally archives the raw
# pre-sanitization terms for the maintainer's records).
#
# Self-referential exclusions: scripts that EMBED the PII patterns as
# detector strings would otherwise self-trigger. List them here.
SKIP_PATHS=(
  ".github/workflows/ci.yml"      # PATTERNS env var literally references the tokens
  ".github/workflows/security.yml"
  "scripts/pre-commit-pii-scan.sh"
  "scripts/full_pii_scan.sh"
  "scripts/pii_patterns.txt"
  "Makefile"
)

# Build a single -path exclusion list for find.
EXCLUDE_FIND=()
for p in "${SKIP_PATHS[@]}"; do
  EXCLUDE_FIND+=( -not -path "*/${p}" )
done

hits=0
for dir in src docs tests config examples .github; do
  [ -d "${ROOT}/${dir}" ] || continue
  files=$(find "${ROOT}/${dir}" -type f \
            \( -name '*.py' -o -name '*.md' -o -name '*.yml' -o -name '*.yaml' \
               -o -name '*.json' -o -name '*.toml' -o -name '*.sh' \
               -o -name '*.html' -o -name '*.cfg' \) \
            "${EXCLUDE_FIND[@]}" 2>/dev/null)
  if [ -z "${files}" ]; then
    continue
  fi
  # Pipe filenames through xargs grep so the SKIP filter actually applies.
  count=$(echo "${files}" | xargs grep -EHf "${TMP_PAT}" 2>/dev/null | wc -l | tr -d ' ')
  if [ "${count}" -gt 0 ]; then
    echo "  [hit] ${dir}/  ${count} match(es)"
    hits=$((hits + count))
  fi
done

if [ "${hits}" -gt 0 ]; then
  echo "PII scan failed: ${hits} match(es) across the tree" >&2
  exit 1
fi

echo "PII scan clean: 0 matches"
exit 0
