#!/usr/bin/env python3
"""Fail when the repo carries something that must stay private.

    uv run python scripts/check_publishable.py              # tracked files
    uv run python scripts/check_publishable.py --history    # and every commit

Run --history before changing the repo's visibility: going public publishes the
history, not just HEAD. The rules live in `strategy_store.publishable`; the
private term list lives outside the repo (STRATEGY_PRIVATE_TERMS, default
~/.strategy/private-terms.txt). This file is only the command line.
"""

from __future__ import annotations

import argparse
import sys

from strategy_store.publishable import (
    load_private_terms,
    private_terms_path,
    scan_history,
    scan_tree,
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--history", action="store_true", help="also scan every commit on every ref")
    args = ap.parse_args(argv)

    terms = load_private_terms()
    if terms is None:
        print(f"warning: no private term list at {private_terms_path()} — names NOT checked")

    findings = scan_tree(terms=terms)
    if args.history:
        findings += scan_history(terms=terms)
    for f in findings:
        print(f)
    print(f"{len(findings)} finding(s)" + (" (tree + history)" if args.history else " (tree)"))
    return 1 if findings or terms is None else 0


if __name__ == "__main__":
    sys.exit(main())
