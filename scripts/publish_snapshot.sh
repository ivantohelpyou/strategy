#!/usr/bin/env bash
# Publish the current tree to the PUBLIC repo as one snapshot commit (st-lhu).
#
#     scripts/publish_snapshot.sh            # check, commit, push
#     scripts/publish_snapshot.sh --dry-run  # check and build the commit, don't push
#
# Development happens in the private repo (ivantohelpyou/strategy-private), whose
# history carries real task ids and names quoted as evidence. The public repo
# (ivantohelpyou/strategy) never sees that history: each publish is one commit
# holding HEAD's tree, parented on the previous snapshot. It was created fresh
# rather than squashed in place because GitHub keeps force-pushed-over commits
# reachable by SHA, and those would become public with the repo.
#
# Refuses unless: on main, worktree clean, and check_publishable passes — which
# includes the private term list existing (STRATEGY_PRIVATE_TERMS).
set -euo pipefail

REMOTE="${STRATEGY_PUBLIC_REMOTE:-git@github.com:ivantohelpyou/strategy.git}"
DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

cd "$(git rev-parse --show-toplevel)"

die() { echo "publish_snapshot: $*" >&2; exit 1; }

[[ "$(git symbolic-ref --short HEAD)" == "main" ]] || die "not on main"
# check_publishable reads the working tree, so it must equal HEAD's tree
[[ -z "$(git status --porcelain --untracked-files=no)" ]] || die "uncommitted changes"

uv run python scripts/check_publishable.py || die "check_publishable failed — nothing pushed"

src="$(git rev-parse --short HEAD)"
tree="$(git rev-parse 'HEAD^{tree}')"

parent="$(git ls-remote "$REMOTE" refs/heads/main | cut -f1)"
parent_args=()
if [[ -n "$parent" ]]; then
    git fetch --quiet "$REMOTE" main
    if [[ "$(git rev-parse "$parent^{tree}")" == "$tree" ]]; then
        echo "public main already holds this tree ($src) — nothing to publish"
        exit 0
    fi
    parent_args=(-p "$parent")
fi

commit="$(git commit-tree "$tree" "${parent_args[@]}" -m "snapshot of $src ($(date -u +%F))")"
echo "built $commit: tree of $src, parent ${parent:-<none, first snapshot>}"

if (( DRY_RUN )); then
    echo "dry run — not pushed"
    exit 0
fi
git push "$REMOTE" "$commit:refs/heads/main"
