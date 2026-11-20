"""Command line: ``python -m recon [--as-of ISO] [--out DIR]``."""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from .engine import reconcile
from .report import write


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="recon",
        description="Reconcile mixed-load receiving shortages: buyer payments "
                    "separate from orchard deductions.")
    parser.add_argument("--as-of", help="review moment, ISO-8601 with offset")
    parser.add_argument("--out", default="output", help="output directory")
    parser.add_argument("--json", action="store_true", help="print JSON to stdout")
    args = parser.parse_args(argv)

    as_of = datetime.fromisoformat(args.as_of) if args.as_of else None
    bundle = reconcile(as_of=as_of)
    paths = write(bundle, Path(args.out))
    if args.json:
        print(paths["json"].read_text(encoding="utf-8"))
    else:
        for name, path in paths.items():
            print(f"{name}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
