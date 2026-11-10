#!/usr/bin/env python3
"""fort-clip-factory entry point.

  python main.py                # full cycle: find, clip, upload
  python main.py --dry-run      # everything except the upload
  python main.py --offline      # no API search: use ingest/ drops
  python main.py --sample OUT   # render a sample Short, no upload
  python main.py --verify-only  # just the oEmbed verification pass
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src import ledger, orchestrator, verify  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="no upload, no memory writes")
    ap.add_argument("--offline", action="store_true",
                    help="skip API search; mine the ingest/ folder")
    ap.add_argument("--sample", metavar="OUT.mp4", default=None,
                    help="render a sample Short to OUT and stop")
    ap.add_argument("--verify-only", action="store_true",
                    help="run only the post-publish verification pass")
    args = ap.parse_args()

    if args.verify_only:
        state = ledger.load()
        report = verify.run(state)
        return 1 if report["alerts"] else 0

    sample = Path(args.sample).resolve() if args.sample else None
    return orchestrator.run(dry_run=args.dry_run,
                            offline=args.offline,
                            sample_out=sample)


if __name__ == "__main__":
    sys.exit(main())
