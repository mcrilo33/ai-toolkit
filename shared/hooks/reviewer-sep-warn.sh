#!/usr/bin/env bash
# reviewer-sep-warn — shipping-gate hook (git push / gh pr).
#
# Verifies that the diff being pushed was reviewed and APPROVED, by binding the
# approval to the EXACT content via a review-evidence artifact:
#
#   .review/<diff_hash>.json   { "verdict": "APPROVE" | "REQUEST_CHANGES", ... }
#
# The code-review agent writes this artifact on APPROVE (see code-review.md),
# computing diff_hash over the staged change. This hook recomputes the hash over
# the pushed range (BASE..HEAD) and ships only if a matching APPROVE artifact
# exists. This closes the hole where an agent types `Reviewed-by: code-review`
# without any review having happened: now an approval bound to THIS diff must
# exist, and a stale approval of an earlier diff will not match.
#
# HONEST CEILING (do not overclaim): with a resolvable REVIEW_STAMP_KEY the
# artifact must carry a valid HMAC-SHA256 signature over "<hash>:<verdict>" —
# forging an APPROVE now requires extracting the signing key (Keychain/env),
# not just a file write. The remaining ceiling: the key is readable by the
# same user, so a determined same-user process can still extract it and sign;
# reviewer *identity/independence* remains a trust assumption. With no
# resolvable key the gate degrades to the artifact-existence check (warned).
# The `Reviewed-by: code-review` trailer is kept as a secondary history signal.
#
# Platform behavior (see ship_gate_enforce in lib/utils.sh):
#   • Cursor (beforeShellExecution): hard DENY (exit 2) — push/PR blocked until a
#     matching APPROVE artifact exists.
#   • Claude/Copilot / native git hooks: advisory warn, never blocks (exit 0).
#
# Degrades to allow (never a false block) when the hash cannot be computed:
# no upstream/merge-base, detached state, or any git failure.
set -euo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HOOK_DIR/lib/utils.sh"

INPUT=$(read_stdin)
COMMAND=$(get_shell_command "$INPUT")

[ -z "$COMMAND" ] && exit 0

# Only act on shipping-gate commands: git push, or gh pr create/merge.
# Boundary-aware: chained/prefixed forms (`cd x && git push`) must not bypass.
is_git_push_or_pr "$COMMAND" || exit 0

PROJECT_ROOT=$(project_root_from_payload "$INPUT")

# Resolve base and compute the content-bound hash of the pushed range. Any
# failure (no upstream, no merge-base, empty range) ⇒ exit 0: an advisory hook
# must never block a push it cannot adjudicate.
BASE=$(review_base_ref "$PROJECT_ROOT")
[ -z "$BASE" ] && exit 0

HASH=$(review_diff_hash "$PROJECT_ROOT" "$BASE" range)
[ -z "$HASH" ] && exit 0

# An empty diff (nothing to review) ⇒ allow. The sha256 of empty input is the
# well-known constant; treat "no changes vs base" as nothing to gate.
EMPTY_SHA="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
[ "$HASH" = "$EMPTY_SHA" ] && exit 0

ARTIFACT=$(read_review_artifact "$PROJECT_ROOT" "$HASH")

if [ -z "$ARTIFACT" ]; then
  ship_gate_enforce "$INPUT" "reviewer-separation: no review-evidence artifact for the diff being pushed.
Expected an APPROVE record at: .review/$HASH.json

This diff has no record that the code-review agent reviewed it. Spawn the
code-review agent on the staged diff; on APPROVE it writes the artifact bound to
this exact content. On Cursor the push is BLOCKED until a matching APPROVE
artifact exists.

NOTE: a local hook cannot verify a *different* agent authored the artifact — it
proves a review of this diff exists, not reviewer independence."
  exit 0
fi

VERDICT=$(review_artifact_verdict "$ARTIFACT")

if [ "$VERDICT" != "APPROVE" ]; then
  ship_gate_enforce "$INPUT" "reviewer-separation: the review of this diff is not an APPROVE (verdict: ${VERDICT:-unknown}).
Artifact: .review/$HASH.json

Address the review findings, re-stage, and obtain a fresh APPROVE review of the
updated diff. On Cursor the push is BLOCKED until the verdict is APPROVE."
  exit 0
fi

# Verify the artifact's HMAC signature when a key resolves. No resolvable key
# ⇒ keep the existence-check behavior but surface that verification was
# skipped (naming REVIEW_STAMP_KEY so the operator can wire it up).
STAMP_KEY=$(review_stamp_key)
if [ -n "$STAMP_KEY" ]; then
  SIGNATURE=$(review_artifact_signature "$ARTIFACT")
  if [ -z "$SIGNATURE" ] \
    || ! review_stamp_verify_sig "$HASH" "$VERDICT" "$SIGNATURE" "$STAMP_KEY"; then
    ship_gate_enforce "$INPUT" "reviewer-separation: the review artifact signature is missing or invalid.
Artifact: .review/$HASH.json

The APPROVE record exists but its HMAC signature does not verify against
REVIEW_STAMP_KEY — the artifact may have been forged or tampered with. Obtain a
fresh review via the review-stamp MCP server (approve_review), which signs the
artifact with the key. On Cursor the push is BLOCKED until the signature
verifies."
    exit 0
  fi
else
  warn "reviewer-separation: REVIEW_STAMP_KEY not resolvable (env or Keychain) —
artifact signature verification SKIPPED; only artifact existence was checked.
Store the key to enable HMAC verification of review approvals."
fi

# APPROVE artifact matches the pushed diff. Secondary signal: surface (do not
# block on) a missing `Reviewed-by: code-review` trailer so history still
# carries it. The artifact is the authority.
if UPSTREAM=$(git -C "$PROJECT_ROOT" rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' 2>/dev/null); then
  RANGE="$UPSTREAM..HEAD"
else
  RANGE="HEAD"
fi
COMMITS=$(git -C "$PROJECT_ROOT" rev-list --no-merges "$RANGE" 2>/dev/null || true)
MISSING_TRAILER=0
while IFS= read -r sha; do
  [ -z "$sha" ] && continue
  BODY=$(git -C "$PROJECT_ROOT" log -1 --format=%B "$sha" 2>/dev/null || true)
  echo "$BODY" | grep -qiE '^[[:space:]]*Reviewed-by:[[:space:]]*code-review' || MISSING_TRAILER=1
done <<< "$COMMITS"

if [ "$MISSING_TRAILER" -eq 1 ]; then
  warn "reviewer-separation: APPROVE artifact verified for this diff, but some commits
lack a 'Reviewed-by: code-review' trailer. Add it for an auditable history trace
(non-blocking — the artifact is the authority)."
fi

exit 0
