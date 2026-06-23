#!/usr/bin/env python3
"""Deliberate, auditable admin acts on the frozen eval sets — never the agent loop.

  python scripts/eval_admin.py verify              # check frozen sets vs the lock
  python scripts/eval_admin.py refreeze [--at DATE]# re-ground: re-lock after a human edit
  python scripts/eval_admin.py burn <case_id>      # a holdout case was inspected ->
                                                   # move to dev, rotate in a reserve case

Burning is the manual half of the hold-out discipline: freeze is enforced (verify),
but "if I read a holdout case I burn it" is a human commitment — this makes the burn
an explicit, logged command rather than a system guarantee.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
from runtime.agent_eval import (  # noqa: E402
    burn_holdout_case,
    refreeze,
    verify_frozen,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('verify')
    rf = sub.add_parser('refreeze')
    rf.add_argument('--at', default='', help='frozen_at date to record')
    bn = sub.add_parser('burn')
    bn.add_argument('case_id')
    args = ap.parse_args()

    if args.cmd == 'verify':
        bad = verify_frozen()
        if bad:
            print('TAMPERED:', *bad, sep='\n  ')
            return 1
        print('frozen eval intact')
        return 0
    if args.cmd == 'refreeze':
        lock = refreeze(frozen_at=args.at)
        print('re-froze:', lock['files'])
        return 0
    if args.cmd == 'burn':
        print(burn_holdout_case(args.case_id))
        return 0
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
