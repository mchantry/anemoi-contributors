"""Collect Anemoi contributor statistics.

Two phases:
1. Refresh the per-repo cache under `cache/` (calls the GitHub API; incremental).
2. Compute a snapshot from that cache and write it to `results.json` +
   `history/results-<date>.json`. Purely local, no API calls.

Use `--no-refresh` to skip phase 1 (useful for re-computing snapshots after
editing `github_to_org.json` / `email_to_org.json`). Use `--refresh-only` to
just warm the cache without emitting a snapshot.
"""
import argparse
import json
import os
import re
from collections import Counter
from datetime import datetime, timezone

from dateutil.relativedelta import relativedelta
from dotenv import load_dotenv
from github import Github

from cache import load_cache, refresh_repo_cache

load_dotenv()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
REPO_OWNER = "ecmwf"
REPO_LIST = ["anemoi", "anemoi-core", "anemoi-datasets",
             "anemoi-inference", "anemoi-transform",
             "anemoi-utils"]

PR_TYPE_PREFIXES = ("feat", "fix", "chore", "docs", "refactor", "test", "ci", "perf", "build", "style")


def load_github_to_org_mapping():
    with open("github_to_org.json", "r") as f:
        return json.load(f)


def save_github_to_org_mapping(mapping):
    with open("github_to_org.json", "w") as f:
        json.dump(mapping, f, indent=2)


def load_email_to_org_mapping():
    try:
        with open("email_to_org.json", "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_email_to_org_mapping(mapping):
    with open("email_to_org.json", "w") as f:
        json.dump(mapping, f, indent=2)


def _resolve_login(login, github_to_org):
    """Return the org for a login, marking unknown logins for later manual review."""
    if not login:
        return None
    if login not in github_to_org:
        print(f"Unknown GitHub user: {login}")
        github_to_org[login] = "Unknown"
    return github_to_org[login]


def _aggregate_by_org(user_counts, github_to_org):
    org_counts = Counter()
    for user, count in user_counts.items():
        org = _resolve_login(user, github_to_org) or "Unknown"
        org_counts[org] += count
    return org_counts


def classify_pr_type(title):
    """Categorise a PR by its Conventional Commits prefix."""
    t = (title or "").lower().lstrip()
    for prefix in PR_TYPE_PREFIXES:
        if re.match(rf"{prefix}\s*(\([^)]*\))?\s*!?:", t):
            return prefix
    return "other"


def snapshot_repo(cache, github_to_org, email_to_org, months, as_of):
    """Compute a per-repo snapshot from cached raw events.

    Same output schema as before so plot.py and archived snapshots stay compatible.
    """
    since = as_of - relativedelta(months=months)

    # --- Issues (skip PRs that come back through PyGithub's issues API) ---
    user_issue_count = Counter()
    for issue in cache["issues"].values():
        if issue.get("is_pull_request"):
            continue
        created_iso = issue.get("created_at")
        if not created_iso:
            continue
        created_at = datetime.fromisoformat(created_iso)
        if not (since <= created_at <= as_of):
            continue
        if issue.get("user"):
            user_issue_count[issue["user"]] += 1
    org_issue_count = _aggregate_by_org(user_issue_count, github_to_org)

    # --- Merged PRs, deduplicated per org per PR ---
    org_pr_count = Counter()
    org_pr_count_by_type = {}
    for pr in cache["prs"].values():
        if not pr.get("merged"):
            continue
        created_iso = pr.get("created_at")
        merged_iso = pr.get("merged_at")
        if not created_iso or not merged_iso:
            continue
        created_at = datetime.fromisoformat(created_iso)
        merged_at = datetime.fromisoformat(merged_iso)
        if not (since <= created_at <= as_of):
            continue
        if merged_at > as_of:
            continue

        # Login-based authors: opener + commit authors + noreply co-authors.
        logins = set()
        if pr.get("user"):
            logins.add(pr["user"])
        logins.update(pr.get("commit_authors", []))
        logins.update(pr.get("coauthor_logins", []))

        orgs = set()
        for login in logins:
            org = _resolve_login(login, github_to_org)
            if org:
                orgs.add(org)
        for h in pr.get("coauthor_email_hashes", []):
            orgs.add(email_to_org.get(h, "Unknown"))

        pr_type = classify_pr_type(pr.get("title"))
        if pr_type == "other":
            print(f"Uncategorised PR: #{pr['number']} - {pr.get('title')}")

        for org in orgs:
            org_pr_count[org] += 1
            org_pr_count_by_type.setdefault(pr_type, Counter())[org] += 1

    # --- Reviews on PRs created in-window (preserves previous semantics) ---
    user_total_review_count = Counter()
    user_unique_review_count = Counter()
    for pr in cache["prs"].values():
        created_iso = pr.get("created_at")
        if not created_iso:
            continue
        created_at = datetime.fromisoformat(created_iso)
        if not (since <= created_at <= as_of):
            continue
        seen = set()
        for review in pr.get("reviews", []):
            user = review.get("user")
            submitted_iso = review.get("submitted_at")
            if not user or not submitted_iso:
                continue
            submitted_at = datetime.fromisoformat(submitted_iso)
            if submitted_at > as_of:
                continue
            user_total_review_count[user] += 1
            if user not in seen:
                user_unique_review_count[user] += 1
                seen.add(user)

    org_total_review_count = _aggregate_by_org(user_total_review_count, github_to_org)
    org_unique_review_count = _aggregate_by_org(user_unique_review_count, github_to_org)

    return {
        "issues": dict(org_issue_count),
        "pull_requests": dict(org_pr_count),
        "pull_requests_by_type": {t: dict(c) for t, c in org_pr_count_by_type.items()},
        "total_reviews": dict(org_total_review_count),
        "unique_reviews": dict(org_unique_review_count),
    }


def _print_repo_summary(repo_name, snap, months):
    print(f"------------------------------")
    print(f" {repo_name}:")
    print(f"------------------------------")
    print(f"\nIssues opened in {repo_name} in the last {months} months by Organization:")
    for org, count in Counter(snap["issues"]).most_common():
        print(f"- {org}: {count} issues")
    print(f"\nPull Requests merged in {repo_name} in the last {months} months by Organization:")
    for org, count in Counter(snap["pull_requests"]).most_common():
        print(f"- {org}: {count} PRs")
    print(f"\nPull Requests by type in {repo_name} in the last {months} months:")
    for pr_type, org_counts in sorted(snap["pull_requests_by_type"].items()):
        total = sum(org_counts.values())
        print(f"- {pr_type}: {total} PRs")
    print(f"\nCode Reviews performed in {repo_name} in the last {months} months by Organization:")
    for org, count in Counter(snap["total_reviews"]).most_common():
        print(f"- {org}: {count} total reviews")
    print(f"\nUnique Code Reviews by Organization in {repo_name} in the last {months} months:")
    for org, count in Counter(snap["unique_reviews"]).most_common():
        print(f"- {org}: {count} unique reviews")


def refresh_all_caches(email_to_org):
    """Refresh every repo's cache from the GitHub API."""
    g = Github(GITHUB_TOKEN)
    for repo_name in REPO_LIST:
        repo = g.get_repo(f"{REPO_OWNER}/{repo_name}")
        cache = load_cache(repo_name)
        refresh_repo_cache(repo, cache, email_to_org)
        # Persist the email mapping after each repo so mid-run failures preserve progress.
        save_email_to_org_mapping(email_to_org)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--months", type=int, default=6,
                        help="Rolling window in months (default: 6).")
    parser.add_argument("--as-of", dest="as_of", type=str, default=None,
                        help="Treat this YYYY-MM-DD date as 'now'. Default: today. "
                             "Historical runs write only the dated snapshot, not results.json.")
    parser.add_argument("--no-refresh", dest="refresh", action="store_false",
                        help="Skip the GitHub cache refresh and compute snapshot from disk only.")
    parser.add_argument("--refresh-only", action="store_true",
                        help="Refresh the cache and exit; do not compute a snapshot.")
    parser.set_defaults(refresh=True)
    args = parser.parse_args()

    if args.as_of is None:
        as_of = datetime.now(timezone.utc)
        is_today = True
    else:
        as_of = datetime.strptime(args.as_of, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        is_today = args.as_of == datetime.now(timezone.utc).strftime("%Y-%m-%d")

    github_to_org = load_github_to_org_mapping()
    email_to_org = load_email_to_org_mapping()

    if args.refresh:
        refresh_all_caches(email_to_org)

    if args.refresh_only:
        save_email_to_org_mapping(email_to_org)
        save_github_to_org_mapping(github_to_org)
        print("Cache refreshed; snapshot skipped (--refresh-only).")
        raise SystemExit(0)

    results = {
        "generated_at": as_of.isoformat(),
        "months": args.months,
        "repos": {},
    }
    for repo_name in REPO_LIST:
        cache = load_cache(repo_name)
        snap = snapshot_repo(cache, github_to_org, email_to_org, args.months, as_of)
        results["repos"][repo_name] = snap
        _print_repo_summary(repo_name, snap, args.months)

    if is_today:
        with open("results.json", "w") as f:
            json.dump(results, f, indent=2)
        print("\nResults saved to results.json")
    else:
        print(f"\nHistorical run (as-of {args.as_of}); results.json not modified.")

    os.makedirs("history", exist_ok=True)
    snapshot_date = as_of.strftime("%Y-%m-%d")
    snapshot_path = f"history/results-{snapshot_date}.json"
    with open(snapshot_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"Snapshot archived to {snapshot_path}")

    save_email_to_org_mapping(email_to_org)
    save_github_to_org_mapping(github_to_org)
    print("Mapping files updated.")
