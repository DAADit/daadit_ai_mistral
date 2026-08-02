#!/usr/bin/env bash
#
# Replace a copied-in `daadit_ai_mistral` folder in a deployment repo with a
# git submodule of this product repo, pinned to a release tag.
#
# Why: on 2026-08-01 the deploy repo carried its own 19.0.6.x line of the
# module while this repo was at 19.0.7.x. Two independent lines of the same
# module means product work never reaches production, and a deploy-side fix
# can be wiped by the next sync. A submodule makes the second line impossible.
#
# Run this INSIDE the deployment repo, on a fresh branch. It refuses to run
# while the copied folder still differs from the release you pin to: that
# difference is deploy-only work which must be PR'd to the product repo first,
# or this migration deletes it.
#
#   ./adopt_as_submodule.sh --release v19.0.8.0.0
#   ./adopt_as_submodule.sh --release v19.0.8.0.0 \
#       --current addons/daadit_ai_mistral --mount submodules/daadit_ai_mistral
#
# Layout note: this repo's root holds `daadit_ai_mistral/__manifest__.py`, so
# the submodule is mounted next to the old folder rather than on top of it —
# Odoo.sh scans the repository recursively for manifests, so
# `<mount>/daadit_ai_mistral/` is found without touching the addons path.
#
set -euo pipefail

PRODUCT_URL="https://github.com/DAADit/daadit_ai_mistral.git"
MODULE="daadit_ai_mistral"
CURRENT="daadit_ai_mistral"
MOUNT="submodules/daadit_ai_mistral"
RELEASE=""
FORCE=0

usage() { sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --release) RELEASE="${2:?--release needs a tag}"; shift 2 ;;
        --current) CURRENT="${2:?--current needs a directory}"; shift 2 ;;
        --mount)   MOUNT="${2:?--mount needs a directory}"; shift 2 ;;
        --force)   FORCE=1; shift ;;
        -h|--help) usage 0 ;;
        *) echo "unknown argument: $1" >&2; usage 1 ;;
    esac
done

die() { echo "error: $*" >&2; exit 1; }

[ -n "$RELEASE" ] || die "--release <tag> is required"
git rev-parse --git-dir >/dev/null 2>&1 || die "run this inside the deployment repo"

branch="$(git rev-parse --abbrev-ref HEAD)"
case "$branch" in main|master) die "on '$branch' — make a branch first" ;; esac

[ -z "$(git status --porcelain)" ] || die "working tree is dirty; commit or stash first"
[ -d "$CURRENT" ] || die "'$CURRENT' does not exist — pass --current"
[ -f "$CURRENT/__manifest__.py" ] || die "'$CURRENT' holds no __manifest__.py"
[ ! -e "$MOUNT" ] || die "'$MOUNT' already exists — pass --mount"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

echo "→ fetching $RELEASE from the product repo"
git clone --quiet --depth 1 --branch "$RELEASE" "$PRODUCT_URL" "$work/product" \
    || die "tag '$RELEASE' not found in $PRODUCT_URL"

# Anything that shows up here lives only in the deployment repo.
if ! diff -ruN --exclude='*.pyc' --exclude='__pycache__' \
        "$work/product/$MODULE" "$CURRENT" > "$work/deploy-only.diff"; then
    cp "$work/deploy-only.diff" ./deploy-only.diff
    cat >&2 <<EOF

STOP — '$CURRENT' differs from $RELEASE.
The differences are in ./deploy-only.diff ($(wc -l < "$work/deploy-only.diff") lines).

Whatever in there is not part of the release is deploy-only work (such as the
2026-07-31 duplicate-to-do fix). PR it to $PRODUCT_URL,
get it merged, cut a new tag, and re-run with that tag. Migrating now deletes it.

Reviewed the diff and it holds nothing worth keeping? Re-run with --force.
EOF
    [ "$FORCE" -eq 1 ] || exit 2
fi

echo "→ removing '$CURRENT' and mounting the submodule at '$MOUNT'"
git rm -r --quiet "$CURRENT"
git submodule add --quiet "$PRODUCT_URL" "$MOUNT"
git -C "$MOUNT" checkout --quiet "$RELEASE"
git add .gitmodules "$MOUNT"
git commit --quiet -m "daadit_ai_mistral as submodule pinned at $RELEASE

Ends the second, deploy-only line of this module (19.0.6.x), which never
received product-repo work such as the per-agent AI budgets. Upgrading the
module is now a pin move, so what runs in production is always traceable to
a release."

cat <<EOF

done. '$MOUNT/$MODULE/' now holds the module, pinned at $RELEASE.
(The submodule is on a detached HEAD — that is correct for a pin.)

Next:
  1. push this branch and open the PR
  2. after deploy: ir.module.module → daadit_ai_mistral shows the new version,
     and model 'daadit.ai.budget' exists
  3. later release: git -C $MOUNT fetch --tags && \\
     git -C $MOUNT checkout <tag> && git add $MOUNT && git commit
EOF
