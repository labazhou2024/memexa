#!/usr/bin/env bash
# release_orchestrator.sh — drive the 9 stages of the oss-release skill.
#
# Usage:
#   bash scripts/release_orchestrator.sh --check          # dry-run, no writes
#   bash scripts/release_orchestrator.sh --all            # run every stage
#   bash scripts/release_orchestrator.sh --stage N        # run one stage
#   bash scripts/release_orchestrator.sh --from N         # resume from stage N
#
# Stages:
#   1  snapshot local state
#   2  update maintainer profile README
#   3  preflight repo renames
#   4  create the public repo
#   5  push local commits + tags
#   6  configure topics + discussions + security
#   7  branch protection on main
#   8  wait for CI green
#   9  cut the first release
#
# Idempotency contract: every stage is safe to re-run. The script reads
# .release.yaml in the workspace root for project-specific values.

set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CFG="${ROOT}/.release.yaml"
AUDIT="${ROOT}/.release_audit.log"
DRY=0
START=1
END=9

# -- arg parse --
while [ $# -gt 0 ]; do
  case "$1" in
    --check)  DRY=1; shift ;;
    --all)    START=1; END=9; shift ;;
    --stage)  START="$2"; END="$2"; shift 2 ;;
    --from)   START="$2"; END=9; shift 2 ;;
    -h|--help)
      grep '^#' "$0" | head -25
      exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [ ! -f "${CFG}" ]; then
  echo "[fatal] missing ${CFG}" >&2
  exit 1
fi

# -- minimal YAML reader (jq-only, no python dep) --
yq() {
  # Convert YAML -> JSON via a tiny embedded python; jq parses the result.
  python -c "
import sys, yaml, json
sys.stdout.write(json.dumps(yaml.safe_load(open(sys.argv[1]))))
" "${CFG}" | jq -r "$1"
}

OWNER=$(yq '.owner')
REPO=$(yq '.repo')
DESC=$(yq '.description')
HOMEPAGE=$(yq '.homepage')

audit() {
  local stage="$1"
  local status="$2"
  local detail="${3:-}"
  local ts
  ts=$(date -Iseconds)
  echo "${ts} stage=${stage} status=${status} detail=${detail}" | tee -a "${AUDIT}" >&2
}

run() {
  if [ "${DRY}" -eq 1 ]; then
    echo "[dry] $*" >&2
    return 0
  fi
  "$@"
}

# ─── Stage 1 — snapshot ───────────────────────────────────────────────────────
stage_1() {
  echo "── Stage 1: snapshot ───────────────────────────────────────────────"
  local dirty
  dirty=$(git -C "${ROOT}" status --short | wc -l | tr -d ' ')
  if [ "${dirty}" -gt 0 ]; then
    audit 1 fail "dirty tree (${dirty} entries)"
    return 1
  fi
  echo "  commits (last 5):"
  git -C "${ROOT}" log --oneline -5 | sed 's/^/    /'
  echo "  tags:"
  git -C "${ROOT}" tag -l | sed 's/^/    /'
  audit 1 ok
}

# ─── Stage 2 — profile README ─────────────────────────────────────────────────
stage_2() {
  echo "── Stage 2: profile README ─────────────────────────────────────────"
  local p_owner p_repo p_path p_src
  p_owner=$(yq '.profile_readme.owner // empty')
  p_repo=$(yq '.profile_readme.repo // empty')
  p_path=$(yq '.profile_readme.path // empty')
  p_src=$(yq '.profile_readme.source_file // empty')
  if [ -z "${p_owner}" ]; then
    audit 2 skip "no profile_readme config"
    return 0
  fi
  if [ ! -f "${ROOT}/${p_src}" ]; then
    audit 2 fail "source ${p_src} missing"
    return 1
  fi
  # Fetch current sha for idempotent update.
  local sha
  sha=$(gh api "/repos/${p_owner}/${p_repo}/contents/${p_path}" --jq .sha 2>/dev/null || echo "")
  local b64
  b64=$(base64 -w0 < "${ROOT}/${p_src}" 2>/dev/null || base64 < "${ROOT}/${p_src}")
  if [ "${DRY}" -eq 1 ]; then
    echo "[dry] PUT /repos/${p_owner}/${p_repo}/contents/${p_path}"
    audit 2 ok dry
    return 0
  fi
  if [ -n "${sha}" ]; then
    gh api -X PUT "/repos/${p_owner}/${p_repo}/contents/${p_path}" \
      --field message="docs: refresh profile README" \
      --field content="${b64}" \
      --field sha="${sha}" >/dev/null
  else
    gh api -X PUT "/repos/${p_owner}/${p_repo}/contents/${p_path}" \
      --field message="docs: add profile README" \
      --field content="${b64}" >/dev/null
  fi
  audit 2 ok "pushed ${p_path}"
}

# ─── Stage 3 — preflight repo renames ─────────────────────────────────────────
stage_3() {
  echo "── Stage 3: preflight renames ──────────────────────────────────────"
  local n
  n=$(yq '.preflight_renames | length // 0')
  if [ "${n}" -eq 0 ] || [ "${n}" = "null" ]; then
    audit 3 skip "no preflight_renames"
    return 0
  fi
  local i=0
  while [ "${i}" -lt "${n}" ]; do
    local from to
    from=$(yq ".preflight_renames[${i}].from")
    to=$(yq ".preflight_renames[${i}].to")
    # Check repo exists + current name
    if gh repo view "${OWNER}/${from}" >/dev/null 2>&1; then
      run gh repo rename "${to}" --repo "${OWNER}/${from}" --yes
      audit 3 ok "${from} -> ${to}"
    elif gh repo view "${OWNER}/${to}" >/dev/null 2>&1; then
      audit 3 skip "already renamed: ${OWNER}/${to}"
    else
      audit 3 skip "neither ${from} nor ${to} exists for ${OWNER}"
    fi
    i=$((i+1))
  done
}

# ─── Stage 4 — create the public repo ─────────────────────────────────────────
stage_4() {
  echo "── Stage 4: create public repo ─────────────────────────────────────"
  if gh repo view "${OWNER}/${REPO}" >/dev/null 2>&1; then
    audit 4 skip "${OWNER}/${REPO} already exists"
    return 0
  fi
  if [ "${DRY}" -eq 1 ]; then
    echo "[dry] gh repo create ${OWNER}/${REPO} --public --description \"${DESC}\""
    audit 4 ok dry
    return 0
  fi
  gh repo create "${OWNER}/${REPO}" \
    --public \
    --description "${DESC}" \
    --homepage "${HOMEPAGE}" >/dev/null
  audit 4 ok "created ${OWNER}/${REPO}"
}

# ─── Stage 5 — push local commits + tags ──────────────────────────────────────
stage_5() {
  echo "── Stage 5: git push ───────────────────────────────────────────────"
  local url="https://github.com/${OWNER}/${REPO}.git"
  if git -C "${ROOT}" remote get-url origin >/dev/null 2>&1; then
    run git -C "${ROOT}" remote set-url origin "${url}"
  else
    run git -C "${ROOT}" remote add origin "${url}"
  fi
  run git -C "${ROOT}" push -u origin main
  run git -C "${ROOT}" push origin --tags
  audit 5 ok "pushed to ${url}"
}

# ─── Stage 6 — configure topics + discussions + security ──────────────────────
stage_6() {
  echo "── Stage 6: repo metadata ──────────────────────────────────────────"
  # topics
  local n_topics
  n_topics=$(yq '.topics | length // 0')
  local i=0
  local args=()
  while [ "${i}" -lt "${n_topics}" ]; do
    local t
    t=$(yq ".topics[${i}]")
    args+=("--add-topic" "${t}")
    i=$((i+1))
  done
  if [ "${#args[@]}" -gt 0 ]; then
    run gh repo edit "${OWNER}/${REPO}" "${args[@]}"
    audit 6 ok "topics=${n_topics}"
  fi

  # discussions
  if [ "$(yq '.discussions')" = "true" ]; then
    run gh repo edit "${OWNER}/${REPO}" --enable-discussions
    audit 6 ok discussions
  fi

  # secret_scanning (free for public repos)
  if [ "$(yq '.secret_scanning')" = "true" ]; then
    if [ "${DRY}" -eq 1 ]; then
      echo "[dry] gh api PATCH /repos/${OWNER}/${REPO} secret_scanning"
    else
      gh api -X PATCH "/repos/${OWNER}/${REPO}" \
        --field 'security_and_analysis[secret_scanning][status]=enabled' \
        --field 'security_and_analysis[secret_scanning_push_protection][status]=enabled' \
        >/dev/null 2>&1 || audit 6 skip "secret_scanning api unavail"
    fi
    audit 6 ok secret_scanning
  fi
}

# ─── Stage 7 — branch protection ──────────────────────────────────────────────
stage_7() {
  echo "── Stage 7: branch protection ──────────────────────────────────────"
  local branch
  branch=$(yq '.branch_protect.branch // "main"')
  local enforce_admins
  enforce_admins=$(yq '.branch_protect.enforce_admins // false')
  local n_ctx
  n_ctx=$(yq '.branch_protect.required_status_checks | length // 0')

  if [ "${DRY}" -eq 1 ]; then
    echo "[dry] PUT /repos/${OWNER}/${REPO}/branches/${branch}/protection"
    audit 7 ok dry
    return 0
  fi

  # Build the JSON body via python (gh api --field is finicky with nested).
  python - "${OWNER}/${REPO}" "${branch}" "${enforce_admins}" "${CFG}" <<'PY' >/dev/null
import json, subprocess, sys, yaml
full, branch, enforce_admins, cfg_path = sys.argv[1:5]
cfg = yaml.safe_load(open(cfg_path))
ctx = cfg.get("branch_protect", {}).get("required_status_checks") or []
body = {
  "required_status_checks": {"strict": True, "contexts": ctx},
  "enforce_admins": enforce_admins == "true",
  "required_pull_request_reviews": None,
  "restrictions": None,
  "allow_force_pushes": False,
  "allow_deletions": False,
}
p = subprocess.run(
  ["gh", "api", "-X", "PUT", f"/repos/{full}/branches/{branch}/protection",
   "--input", "-"],
  input=json.dumps(body), text=True, capture_output=True,
)
if p.returncode != 0:
  sys.stderr.write(f"[stage7] branch_protect api: {p.stderr[:200]}\n")
  sys.exit(p.returncode)
PY
  audit 7 ok "branch=${branch}"
}

# ─── Stage 8 — wait for CI green ─────────────────────────────────────────────
stage_8() {
  echo "── Stage 8: wait for CI ────────────────────────────────────────────"
  if [ "${DRY}" -eq 1 ]; then
    audit 8 ok dry
    return 0
  fi
  # Poll: trigger via the push from Stage 5 — wait until the most recent
  # run is completed.
  local tries=0
  while [ "${tries}" -lt 60 ]; do
    local row
    row=$(gh run list --repo "${OWNER}/${REPO}" --limit 1 --json status,conclusion,databaseId 2>/dev/null)
    if [ -z "${row}" ] || [ "${row}" = "[]" ]; then
      sleep 15
      tries=$((tries+1))
      continue
    fi
    local status conclusion id
    status=$(echo "${row}" | jq -r '.[0].status')
    conclusion=$(echo "${row}" | jq -r '.[0].conclusion')
    id=$(echo "${row}" | jq -r '.[0].databaseId')
    if [ "${status}" = "completed" ]; then
      if [ "${conclusion}" = "success" ]; then
        audit 8 ok "run=${id}"
        return 0
      fi
      audit 8 fail "run=${id} conclusion=${conclusion}"
      gh run view "${id}" --repo "${OWNER}/${REPO}" --log-failed 2>&1 | tail -40
      return 1
    fi
    echo "    [${tries}] status=${status}"
    sleep 20
    tries=$((tries+1))
  done
  audit 8 fail "timeout"
  return 1
}

# ─── Stage 9 — cut the release ────────────────────────────────────────────────
stage_9() {
  echo "── Stage 9: release ────────────────────────────────────────────────"
  local tag title src_tag
  tag=$(yq '.release_tag')
  title=$(yq '.release_title')
  src_tag=$(yq '.release_use_existing_tag // empty')

  # Idempotent: skip if tag already on remote
  if git -C "${ROOT}" ls-remote --tags origin "${tag}" 2>/dev/null | grep -q "${tag}"; then
    audit 9 skip "tag ${tag} already on remote"
  else
    if [ -n "${src_tag}" ] && git -C "${ROOT}" tag -l "${src_tag}" | grep -q "${src_tag}"; then
      # Re-cut tag at the same commit as src_tag
      local sha
      sha=$(git -C "${ROOT}" rev-list -n 1 "${src_tag}")
      run git -C "${ROOT}" tag -a "${tag}" "${sha}" -m "${title}"
    else
      run git -C "${ROOT}" tag -a "${tag}" -m "${title}"
    fi
    run git -C "${ROOT}" push origin "${tag}"
  fi

  if gh release view "${tag}" --repo "${OWNER}/${REPO}" >/dev/null 2>&1; then
    audit 9 skip "release ${tag} already exists"
    return 0
  fi
  run gh release create "${tag}" \
    --repo "${OWNER}/${REPO}" \
    --title "${title}" \
    --generate-notes
  audit 9 ok "released ${tag}"
}

# ─── Dispatch ─────────────────────────────────────────────────────────────────
echo "owner=${OWNER} repo=${REPO} stages=${START}..${END} dry=${DRY}"
for s in 1 2 3 4 5 6 7 8 9; do
  if [ "${s}" -ge "${START}" ] && [ "${s}" -le "${END}" ]; then
    "stage_${s}" || { echo "[fatal] stage ${s} failed; see ${AUDIT}" >&2; exit "${s}"; }
  fi
done
echo "done. audit: ${AUDIT}"
