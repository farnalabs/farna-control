#!/usr/bin/env bash
# Dispatcher-side skip for MAIN-branch failure dispatches to the Branch Fixer.
#
# WHY. When main goes red, every later merge to main produces another red
# CI/deploy run and each run used to dispatch the Branch Fixer again. One
# SonarCloud failure on 2026-11-17 produced four Branch Fixer PRs (#3214,
# #3215, #3216, #3217), one per main SHA. The main-failure fingerprint is now
# stable across commits (workflow + failure identity, no SHA), and the Branch
# Fixer embeds `<!-- branch-fixer-fingerprint: <fp> -->` in the PR body it
# raises. So before POSTing, the caller asks this script whether an OPEN PR
# already carries the fingerprint; if one does, the failure is already being
# fixed and the dispatch is skipped.
#
# CONTRACT (env in, exit code out):
#   FIX_FINGERPRINT  required. The exact fingerprint the caller would dispatch.
#   REPO             required. owner/name.
#   GH_TOKEN         required by gh (pull-requests: read; write to comment).
#   RUN_URL          optional. When set and a PR is found, the failing run URL
#                    is commented on that PR as evidence the failure persists.
#   FIX_PR_LIST_LIMIT optional, default 300 open PRs scanned.
#
#   exit 0  an open fix PR carries the fingerprint -> caller SKIPS the dispatch.
#   exit 1  no open PR carries it                  -> caller dispatches.
#   exit 2  the lookup itself failed               -> caller dispatches.
#
# FAIL-OPEN. A lookup error must never swallow a main failure: the script
# warns and exits non-zero so the caller dispatches anyway. The Branch Fixer
# has its own hard dedupe gate on the same fingerprint, so a duplicate
# dispatch is cheap; a missed one is not.
#
# The match is an exact substring `contains` on the raw fingerprint string,
# not a GitHub search query: search tokenises on `|` and `:`, so it cannot
# match the fingerprint exactly.
set -uo pipefail

if [ -z "${FIX_FINGERPRINT:-}" ] || [ -z "${REPO:-}" ]; then
  echo "::warning::main_failure_fix_pr_open.sh: FIX_FINGERPRINT and REPO are required — cannot check for an open fix PR, dispatching the Branch Fixer anyway."
  exit 2
fi

LIMIT="${FIX_PR_LIST_LIMIT:-300}"

if ! PRS_JSON=$(gh pr list --repo "${REPO}" --state open --limit "${LIMIT}" --json number,body 2>&1); then
  echo "::warning::Could not list open PRs to check for an existing fix for main failure '${FIX_FINGERPRINT}' (${PRS_JSON}) — dispatching the Branch Fixer anyway (fail-open; the fixer has its own dedupe gate)."
  exit 2
fi

if ! FIX_PR=$(printf '%s' "${PRS_JSON}" | jq -r --arg fp "${FIX_FINGERPRINT}" \
  '[.[] | select((.body // "") | contains($fp)) | .number] | sort | .[0] // empty' 2>&1); then
  echo "::warning::Could not parse the open-PR list while checking main failure '${FIX_FINGERPRINT}' (${FIX_PR}) — dispatching the Branch Fixer anyway (fail-open)."
  exit 2
fi

if [ -z "${FIX_PR}" ]; then
  echo "No open PR carries fingerprint '${FIX_FINGERPRINT}' — dispatching the Branch Fixer."
  exit 1
fi

echo "::notice::Main failure '${FIX_FINGERPRINT}' already has open fix PR #${FIX_PR} — not re-dispatching the Branch Fixer"

# Evidence for the open fix PR: the failure is still live on a newer main run.
# Best-effort — the skip decision is already made; a failed comment is a
# visible warning, never a silent no-op.
if [ -n "${RUN_URL:-}" ]; then
  COMMENT="Main is still failing with the same fingerprint (\`${FIX_FINGERPRINT}\`) on a newer run: ${RUN_URL}. The Branch Fixer was not re-dispatched because this PR is open for it."
  if ! COMMENT_ERR=$(gh pr comment "${FIX_PR}" --repo "${REPO}" --body "${COMMENT}" 2>&1); then
    echo "::warning::Could not comment the failing run on fix PR #${FIX_PR} (${COMMENT_ERR}) — the dispatch was still skipped."
  fi
fi
exit 0
