#!/usr/bin/env python3
"""Project an approved plan-contract IR onto an executable Maestro plan.

`plan-contract/SKILL.md` says an approved IR reaches an executable plan through
a projection, and `plan_contract_ingress.author_from_plan_contract` implements
exactly that -- it had no command, so every plan was retyped by hand from an
approved IR and then hand-repaired under fire.

Authoring is not execution. This writes one plan file and touches no run, no
ledger, no vault, and no ref, which is why it is a tool rather than a fifth
operator verb: the operator surface stays frozen at `run start`, `run resume`,
`run amend`, and `run status`.

Every refusal comes from the ingress module, which is where the rules live.
This entry point adds none of its own.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

_TOOLS_DIR = Path(__file__).resolve().parent
_RUNTIME_ROOT = _TOOLS_DIR.parent
if str(_RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(_RUNTIME_ROOT))

from adw_modules import plan_author  # noqa: E402
from adw_modules import plan_contract_ingress as ingress  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="plan_author_cli.py",
        description=(
            "Project an approved plan-contract IR onto an executable Maestro "
            "plan. Writes a plan file; starts no run."
        ),
    )
    parser.add_argument("--from-plan-contract", dest="ir", required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--out", dest="destination", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--rendered", default=None)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        _stored, trace = ingress.author_from_plan_contract(
            Path(args.ir),
            Path(args.receipt),
            Path(args.destination),
            Path(args.repo),
            Path(args.rendered) if args.rendered else None,
        )
    except (plan_author.AuthoringError, ingress.IngressProjectionIncomplete) as exc:
        print(
            json.dumps(
                {
                    "outcome": type(exc).__name__,
                    "detail": str(exc),
                },
                sort_keys=True,
            )
        )
        return 1
    print(
        json.dumps(
            {"outcome": "PLAN_AUTHORED", "plan": args.destination, **trace},
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
