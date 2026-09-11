"""Generate historical snapshots by re-running main.py with --as-of for a series of past dates.

Each snapshot is a rolling `--months` window ending on that date.
Dates that already have `history/results-YYYY-MM-DD.json` are skipped by default.

Example:
    # 24 monthly snapshots, each a rolling 6-month window
    python backfill.py --months 6 --step-months 1 --count 24
"""
import argparse
import os
import subprocess
import sys
from datetime import datetime, timezone

from dateutil.relativedelta import relativedelta


def month_end(d):
    """Last day of the month containing d, at 00:00 UTC."""
    first_next = (d.replace(day=1) + relativedelta(months=1))
    return (first_next - relativedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--months", type=int, default=6,
                   help="Window size passed to main.py (default: 6)")
    p.add_argument("--step-months", type=int, default=1,
                   help="Spacing between snapshots in months (default: 1)")
    p.add_argument("--count", type=int, default=24,
                   help="How many snapshots to generate, going back from --end (default: 24)")
    p.add_argument("--end", type=str, default=None,
                   help="Most recent snapshot date (YYYY-MM-DD). Default: end of last full month.")
    p.add_argument("--force", action="store_true",
                   help="Regenerate snapshots even if the dated file already exists.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the dates that would be generated, then exit.")
    args = p.parse_args()

    if args.end:
        end = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        end = month_end(datetime.now(timezone.utc) - relativedelta(months=1))

    dates = [(end - relativedelta(months=args.step_months * i)).strftime("%Y-%m-%d")
             for i in range(args.count)]
    dates.sort()  # oldest first so github_to_org.json accumulates over time

    print(f"Will generate {len(dates)} snapshot(s) with --months {args.months}: "
          f"{dates[0]} .. {dates[-1]}")

    if args.dry_run:
        for d in dates:
            print(f"  {d}")
        return

    for d in dates:
        path = f"history/results-{d}.json"
        if os.path.exists(path) and not args.force:
            print(f"[skip] {path} already exists")
            continue
        print(f"[run ] python main.py --months {args.months} --as-of {d}")
        cmd = [sys.executable, "main.py", "--months", str(args.months), "--as-of", d]
        subprocess.run(cmd, check=True)


if __name__ == "__main__":
    main()
