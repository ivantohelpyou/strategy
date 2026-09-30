#!/usr/bin/env python3
"""Fail when a program that copies this store's tables has fallen behind it.

    uv run python scripts/check_schema_contract.py            # exit 1 on a violation
    uv run python scripts/check_schema_contract.py --strict    # also fail on lag
    uv run python scripts/check_schema_contract.py --json

The contract, the registry of who copies what, and the diff all live in
`strategy_store.schema_contract` — read its docstring for why they live on this
side of the copy rather than in the copy. This file is only the command line.

Point it at a consumer checkout with that consumer's env var (FOLLOW_WHEE_SRC).
A consumer whose checkout is absent is skipped, not failed: the check is only
meaningful where both trees exist.
"""

from __future__ import annotations

import argparse
import json

from strategy_store.schema_contract import CONSUMERS, STORE_MODELS, check


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--strict", action="store_true", help="also fail on lag (store-only tables)")
    ap.add_argument("--json", action="store_true", dest="as_json")
    args = ap.parse_args(argv)

    reports = []
    missing = []
    for c in CONSUMERS:
        if not c.present:
            missing.append(c)
            continue
        reports.append((c, check(c)))

    if args.as_json:
        print(
            json.dumps(
                {
                    "store": str(STORE_MODELS),
                    "skipped": [{"consumer": c.name, "expected": str(c.models)} for c in missing],
                    "consumers": [
                        {
                            "consumer": r.consumer,
                            "models": str(r.models),
                            "violations": [
                                {"key": v.key, "kind": v.kind, "message": v.message}
                                for v in r.violations
                            ],
                            "unrecorded": [v.key for v in r.unrecorded],
                            "stale_records": r.stale_records,
                            "undeclared": r.undeclared,
                            "lag": r.lag,
                        }
                        for _, r in reports
                    ],
                },
                indent=2,
            )
        )
    else:
        print(f"this store : {STORE_MODELS}")
        for c in missing:
            print(f"\n{c.name}: SKIPPED — no checkout at {c.models} (set {c.env})")
        for consumer, r in reports:
            print(f"\n{r.consumer} : {r.models}")
            print(f"  borrows : {', '.join(consumer.borrows)}")
            print(f"  its own : {', '.join(consumer.owns) or '(none)'}")
            if r.lag:
                print(f"\n  LAG — tables here that {r.consumer} does not borrow ({len(r.lag)}):")
                for t in r.lag:
                    print(f"    · {t}")
                print("    Allowed by the contract. Lag is reported because 'behind by eight")
                print("    schema objects' is the fact that decides whether the copy is")
                print("    still cheaper than the shared package (solutions-14t).")
            if r.undeclared:
                n = len(r.undeclared)
                print(f"\n  UNDECLARED — in the copy, in neither borrows nor owns ({n}):")
                for t in r.undeclared:
                    print(f"    ? {t}")
                print("    Nobody has decided which side owns these. Add them to the")
                print("    consumer's entry in schema_contract.CONSUMERS.")
            if r.violations:
                n = len(r.violations)
                print(f"\n  VIOLATION — the copy is behind on a borrowed table ({n}):")
                for v in r.violations:
                    mark = "✗" if v in r.unrecorded else "·"
                    print(f"    {mark} {v.message}")
                if r.unrecorded:
                    print("\n    The contract is: never drop or rename what the CLI expects.")
                    print("    Apply these in the copy, or record them in")
                    print("    schema_contract.KNOWN_DRIFT with the bead that will close them.")
                else:
                    print("\n    All recorded in schema_contract.KNOWN_DRIFT — known, not new.")
            elif not r.undeclared:
                print("\n  ✓ every column written here exists in the copy, same type")
            if r.stale_records:
                print(f"\n  STALE RECORD — recorded drift that no longer reproduces "
                      f"({len(r.stale_records)}):")
                for k in r.stale_records:
                    print(f"    ! {k}")
                print("    Fixed in the copy. Delete it from schema_contract.KNOWN_DRIFT.")

    if any(r.unrecorded or r.undeclared or r.stale_records for _, r in reports):
        return 1
    if args.strict and any(r.lag for _, r in reports):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
