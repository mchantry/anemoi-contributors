"""Generate historical snapshots from the local cache.

Because snapshots are pure functions of the cache, backfilling arbitrary past
dates is essentially free — no GitHub API calls, no rate limits.

Prerequisites:
- Run `python main.py --refresh-only` at least once to build the cache.

Examples:
    # 24 monthly snapshots, each a rolling 6-month window
    python backfill.py --months 6 --step-months 1 --count 24

    # 52 weekly snapshots ending yesterday, each a rolling 6-month window
    python backfill.py --months 6 --step-weeks 1 --count 52
"""
import argparse
import glob
import json
import os
from datetime import datetime, timedelta, timezone

from dateutil.relativedelta import relativedelta

from cache import load_cache
from main import (
    REPO_LIST,
    build_first_contribution,
    load_email_to_org_mapping,
    load_github_to_org_mapping,
    merge_caches,
    save_email_to_org_mapping,
    save_github_to_org_mapping,
    snapshot_repo,
)


def month_end(d):
    """Last day of the month containing d, at 00:00 UTC."""
    first_next = d.replace(day=1) + relativedelta(months=1)
    return (first_next - relativedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--months", type=int, default=6,
                   help="Window size passed to snapshot_repo (default: 6)")
    step_group = p.add_mutually_exclusive_group()
    step_group.add_argument("--step-months", type=int,
                            help="Spacing between snapshots in months (default when no step flag given: 1)")
    step_group.add_argument("--step-weeks", type=int,
                            help="Spacing between snapshots in weeks")
    step_group.add_argument("--step-days", type=int,
                            help="Spacing between snapshots in days")
    p.add_argument("--count", type=int, default=24,
                   help="How many snapshots to generate, going back from --end (default: 24)")
    p.add_argument("--end", type=str, default=None,
                   help="Most recent snapshot date (YYYY-MM-DD). "
                        "Default: end of last full month for --step-months, else yesterday.")
    p.add_argument("--force", action="store_true",
                   help="Regenerate snapshots even if the dated file already exists.")
    p.add_argument("--regenerate-existing", action="store_true",
                   help="Ignore the date schedule and recompute every snapshot already in history/, "
                        "each at its own date and window. Use after changing the snapshot schema or "
                        "the organisation mapping.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the dates that would be generated, then exit.")
    args = p.parse_args()

    step_months = args.step_months
    step_weeks = args.step_weeks
    step_days = args.step_days
    if step_months is None and step_weeks is None and step_days is None:
        step_months = 1  # backwards-compatible default

    def step_back(base, n):
        if step_months is not None:
            return base - relativedelta(months=step_months * n)
        if step_weeks is not None:
            return base - timedelta(weeks=step_weeks * n)
        return base - timedelta(days=step_days * n)

    if args.end:
        end = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    elif step_months is not None:
        end = month_end(datetime.now(timezone.utc) - relativedelta(months=1))
    else:
        end = (datetime.now(timezone.utc) - timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0)

    if args.regenerate_existing:
        # The filename is the snapshot's identity; `generated_at` inside older
        # files records when the backfill ran, not the date it describes.
        targets = []
        for path in sorted(glob.glob("history/results-*.json")):
            date_str = os.path.basename(path).replace("results-", "").replace(".json", "")
            with open(path) as f:
                existing = json.load(f)
            targets.append((datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc),
                            existing.get("months", args.months)))
        targets.sort()
        if not targets:
            print("Nothing in history/ to regenerate.")
            return
        print(f"Will regenerate {len(targets)} existing snapshot(s): "
              f"{targets[0][0].strftime('%Y-%m-%d')} .. {targets[-1][0].strftime('%Y-%m-%d')}")
    else:
        dates = [step_back(end, i) for i in range(args.count)]
        dates.sort()  # oldest first so any newly-discovered logins accumulate meaningfully
        targets = [(d, args.months) for d in dates]

        step_desc = (f"{step_months} month(s)" if step_months is not None else
                     f"{step_weeks} week(s)" if step_weeks is not None else
                     f"{step_days} day(s)")
        print(f"Will generate {len(dates)} snapshot(s) with --months {args.months}, "
              f"step {step_desc}: {dates[0].strftime('%Y-%m-%d')} .. {dates[-1].strftime('%Y-%m-%d')}")

    if args.dry_run:
        for d, window in targets:
            print(f"  {d.strftime('%Y-%m-%d')} ({window} month window)")
        return

    github_to_org = load_github_to_org_mapping()
    email_to_org = load_email_to_org_mapping()

    caches = {r: load_cache(r) for r in REPO_LIST}
    empty = [r for r, c in caches.items() if not c["prs"] and not c["issues"]]
    if empty:
        print(f"[warn] cache empty for: {', '.join(empty)}")
        print("       run `python main.py --refresh-only` first to populate.")
        if len(empty) == len(REPO_LIST):
            raise SystemExit(1)

    first_seen = build_first_contribution(caches)
    merged = merge_caches(caches)

    os.makedirs("history", exist_ok=True)
    written = 0
    for d, window in targets:
        date_str = d.strftime("%Y-%m-%d")
        path = f"history/results-{date_str}.json"
        if os.path.exists(path) and not (args.force or args.regenerate_existing):
            print(f"[skip] {path} already exists")
            continue
        results = {
            "generated_at": d.isoformat(),
            "months": window,
            "repos": {
                r: snapshot_repo(caches[r], github_to_org, email_to_org, window, d, first_seen)
                for r in REPO_LIST
            },
            "aggregate": snapshot_repo(merged, github_to_org, email_to_org, window, d, first_seen),
        }
        with open(path, "w") as f:
            json.dump(results, f, indent=2)
        written += 1
        print(f"[snap] {date_str}")

    print(f"\nWrote {written} snapshot(s).")
    save_github_to_org_mapping(github_to_org)
    save_email_to_org_mapping(email_to_org)


if __name__ == "__main__":
    main()
