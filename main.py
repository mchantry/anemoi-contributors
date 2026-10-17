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
from collections import Counter, defaultdict
from datetime import datetime, timezone
from statistics import median

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


def load_org_config():
    """The `exclude` list and `host` organisation from orgs.json."""
    with open("orgs.json", "r") as f:
        config = json.load(f)
    return set(config.get("exclude", [])), config.get("host", "ECMWF")


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


def _pr_identities(pr):
    """Everyone credited on a PR: opener, commit authors, co-authors.

    Co-authors known only by a hashed email still count as distinct people, so
    they get a synthetic identity that cannot collide with a GitHub login.
    """
    ids = set()
    if pr.get("user"):
        ids.add(pr["user"])
    ids.update(pr.get("commit_authors", []))
    ids.update(pr.get("coauthor_logins", []))
    ids.update(f"email:{h}" for h in pr.get("coauthor_email_hashes", []))
    return ids


def merge_caches(caches):
    """Combine per-repo caches into one pseudo-cache for cross-repo aggregation.

    Keys are namespaced because PR and issue numbers repeat across repos.
    """
    merged = {"repo": "all", "prs": {}, "issues": {}}
    for name, cache in caches.items():
        for num, pr in cache["prs"].items():
            merged["prs"][f"{name}#{num}"] = pr
        for num, issue in cache["issues"].items():
            merged["issues"][f"{name}#{num}"] = issue
    return merged


def build_first_contribution(caches):
    """Map each contributor identity to the date their first PR was merged.

    Computed over all repos and all time, so it stays correct for historical
    snapshots: a first contribution can only move earlier, never later.
    """
    first = {}
    for cache in caches.values():
        for pr in cache["prs"].values():
            if not pr.get("merged") or not pr.get("merged_at"):
                continue
            when = datetime.fromisoformat(pr["merged_at"])
            for identity in _pr_identities(pr):
                if identity not in first or when < first[identity]:
                    first[identity] = when
    return first


def _resolve_identity(identity, github_to_org, email_to_org):
    """Org for a login or a synthetic `email:<hash>` co-author identity."""
    if identity.startswith("email:"):
        return email_to_org.get(identity[len("email:"):], "Unknown")
    return _resolve_login(identity, github_to_org)


def _hours_between(start, end):
    return (end - start).total_seconds() / 3600


def snapshot_repo(cache, github_to_org, email_to_org, months, as_of, first_seen=None):
    """Compute a per-repo snapshot from cached raw events.

    The original keys are unchanged so archived snapshots stay comparable; newer
    keys (people, review matrix, latency, size) are additive.
    """
    since = as_of - relativedelta(months=months)
    first_seen = first_seen or {}

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
    org_contributors = defaultdict(set)
    org_new_contributors = defaultdict(set)
    org_size = defaultdict(Counter)
    org_pr_additions = defaultdict(list)
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

        orgs = set()
        for identity in _pr_identities(pr):
            org = _resolve_identity(identity, github_to_org, email_to_org)
            if not org:
                continue
            orgs.add(org)
            org_contributors[org].add(identity)
            first = first_seen.get(identity)
            if first and since <= first <= as_of:
                org_new_contributors[org].add(identity)

        pr_type = classify_pr_type(pr.get("title"))
        if pr_type == "other":
            print(f"Uncategorised PR: #{pr['number']} - {pr.get('title')}")

        for org in orgs:
            org_pr_count[org] += 1
            org_pr_count_by_type.setdefault(pr_type, Counter())[org] += 1
            org_size[org]["additions"] += pr.get("additions") or 0
            org_size[org]["deletions"] += pr.get("deletions") or 0
            org_size[org]["changed_files"] += pr.get("changed_files") or 0
            org_pr_additions[org].append(pr.get("additions") or 0)

    # --- Reviews submitted in-window, whenever the PR itself was opened ---
    # Self-reviews are dropped: an author commenting on their own PR is not
    # review effort. Counts are per org per PR, matching how PRs are credited,
    # so a chatty reviewer and a terse one score the same on one PR.
    org_reviewers = defaultdict(set)
    review_matrix = defaultdict(Counter)
    org_review_states = defaultdict(Counter)
    org_prs_reviewed = defaultdict(set)
    for pr_key, pr in cache["prs"].items():
        opener = pr.get("user")
        author_org = _resolve_login(opener, github_to_org) if opener else None
        for review in pr.get("reviews", []):
            user = review.get("user")
            submitted_iso = review.get("submitted_at")
            if not user or not submitted_iso or user == opener:
                continue
            submitted_at = datetime.fromisoformat(submitted_iso)
            if not (since <= submitted_at <= as_of):
                continue
            reviewer_org = _resolve_login(user, github_to_org)
            if not reviewer_org:
                continue
            org_reviewers[reviewer_org].add(user)
            org_review_states[reviewer_org][review.get("state") or "UNKNOWN"] += 1
            org_prs_reviewed[reviewer_org].add(pr_key)
            if author_org:
                review_matrix[reviewer_org][(author_org, pr_key)] = 1

    org_prs_reviewed_count = {org: len(prs) for org, prs in org_prs_reviewed.items()}
    # Collapse the (author org, PR) keys down to one count of distinct PRs per pair.
    review_matrix = {
        reviewer: Counter(author for author, _ in pairs)
        for reviewer, pairs in review_matrix.items()
    }

    # --- Turnaround on PRs merged in-window, by the opener's org ---
    # Merged-in-window (rather than created-in-window) avoids censoring: every
    # PR counted here has a complete, observed lifetime.
    merge_hours = defaultdict(list)
    first_review_hours = defaultdict(list)
    for pr in cache["prs"].values():
        if not pr.get("merged"):
            continue
        created_iso = pr.get("created_at")
        merged_iso = pr.get("merged_at")
        opener = pr.get("user")
        if not created_iso or not merged_iso or not opener:
            continue
        created_at = datetime.fromisoformat(created_iso)
        merged_at = datetime.fromisoformat(merged_iso)
        if not (since <= merged_at <= as_of):
            continue
        org = _resolve_login(opener, github_to_org)
        if not org:
            continue
        merge_hours[org].append(_hours_between(created_at, merged_at))

        review_times = [
            datetime.fromisoformat(r["submitted_at"])
            for r in pr.get("reviews", [])
            if r.get("submitted_at") and r.get("user") and r["user"] != opener
        ]
        if review_times:
            first_review_hours[org].append(_hours_between(created_at, min(review_times)))

    latency = {}
    for org in set(merge_hours) | set(first_review_hours):
        entry = {"merged_prs": len(merge_hours.get(org, []))}
        if merge_hours.get(org):
            entry["median_hours_to_merge"] = round(median(merge_hours[org]), 1)
        if first_review_hours.get(org):
            entry["median_hours_to_first_review"] = round(median(first_review_hours[org]), 1)
            entry["reviewed_prs"] = len(first_review_hours[org])
        latency[org] = entry

    # --- Attribution quality: share of credits we could not place with an org ---
    credit_total = sum(org_pr_count.values()) + sum(org_issue_count.values()) + sum(org_prs_reviewed_count.values())
    credit_unknown = (org_pr_count.get("Unknown", 0) + org_issue_count.get("Unknown", 0)
                      + org_prs_reviewed_count.get("Unknown", 0))

    return {
        "issues": dict(org_issue_count),
        "pull_requests": dict(org_pr_count),
        "pull_requests_by_type": {t: dict(c) for t, c in org_pr_count_by_type.items()},
        "prs_reviewed": dict(org_prs_reviewed_count),
        "contributors": {org: len(people) for org, people in org_contributors.items()},
        "new_contributors": {org: len(people) for org, people in org_new_contributors.items()},
        "reviewers": {org: len(people) for org, people in org_reviewers.items()},
        "pr_size": {
            org: {
                **dict(counts),
                "median_additions": round(median(org_pr_additions[org]), 1),
            }
            for org, counts in org_size.items()
        },
        "review_matrix": {reviewer: dict(targets) for reviewer, targets in review_matrix.items()},
        "review_states": {org: dict(states) for org, states in org_review_states.items()},
        "latency": latency,
        "attribution": {"total_credits": credit_total, "unknown_credits": credit_unknown},
    }


def project_totals(cache, github_to_org, email_to_org, months, as_of, exclude, host):
    """Project-wide headline figures that cannot be derived from per-org counts.

    Per-org counts credit a co-authored PR to every org on it, and medians do
    not pool, so these are computed directly over distinct PRs. Activity by
    excluded orgs (bots, code agents) is ignored here, so automated reviews do
    not shorten the time to first review.
    """
    since = as_of - relativedelta(months=months)
    merged_prs = partner_prs = 0
    reviewed_prs = set()
    merge_hours = []
    first_review_hours = []

    for pr_key, pr in cache["prs"].items():
        opener = pr.get("user")
        human_reviews = []
        for review in pr.get("reviews", []):
            user = review.get("user")
            if not user or not review.get("submitted_at") or user == opener:
                continue
            if _resolve_login(user, github_to_org) in exclude:
                continue
            submitted_at = datetime.fromisoformat(review["submitted_at"])
            human_reviews.append(submitted_at)
            if since <= submitted_at <= as_of:
                reviewed_prs.add(pr_key)

        if not pr.get("merged") or not pr.get("created_at") or not pr.get("merged_at"):
            continue
        created_at = datetime.fromisoformat(pr["created_at"])
        merged_at = datetime.fromisoformat(pr["merged_at"])

        # Same window as the per-org PR counts: opened in-window, merged by as_of.
        if since <= created_at <= as_of and merged_at <= as_of:
            orgs = {_resolve_identity(i, github_to_org, email_to_org) for i in _pr_identities(pr)}
            orgs = {o for o in orgs if o and o not in exclude}
            if orgs:
                merged_prs += 1
                if orgs - {host, "Unknown"}:
                    partner_prs += 1

        # Same window as the per-org latency: merged in-window.
        if since <= merged_at <= as_of and opener and _resolve_login(opener, github_to_org) not in exclude:
            merge_hours.append(_hours_between(created_at, merged_at))
            if human_reviews:
                first_review_hours.append(_hours_between(created_at, min(human_reviews)))

    totals = {
        "host": host,
        "merged_prs": merged_prs,
        "partner_prs": partner_prs,
        "prs_reviewed": len(reviewed_prs),
    }
    if merge_hours:
        totals["median_hours_to_merge"] = round(median(merge_hours), 1)
    if first_review_hours:
        totals["median_hours_to_first_review"] = round(median(first_review_hours), 1)
    return totals


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
    print(f"\nPRs reviewed in {repo_name} in the last {months} months by Organization:")
    for org, count in Counter(snap["prs_reviewed"]).most_common():
        print(f"- {org}: {count} PRs reviewed")


def _print_aggregate_summary(agg, months):
    print(f"\n==============================")
    print(f" All repos combined ({months} months):")
    print(f"==============================")
    print("\nPeople active per organisation (distinct, deduplicated across repos):")
    for org, count in Counter(agg["contributors"]).most_common():
        new = agg["new_contributors"].get(org, 0)
        reviewers = agg["reviewers"].get(org, 0)
        suffix = f", {new} first-time" if new else ""
        print(f"- {org}: {count} contributor(s){suffix}, {reviewers} reviewer(s)")

    print("\nMedian turnaround on merged PRs by the opener's organisation:")
    for org, entry in sorted(agg["latency"].items(), key=lambda kv: -kv[1]["merged_prs"]):
        merge = entry.get("median_hours_to_merge")
        review = entry.get("median_hours_to_first_review")
        merge_s = f"{merge / 24:.1f}d to merge" if merge is not None else "n/a"
        review_s = f"{review / 24:.1f}d to first review" if review is not None else "no reviews"
        print(f"- {org}: {merge_s}, {review_s} (n={entry['merged_prs']})")

    attribution = agg["attribution"]
    if attribution["total_credits"]:
        known = 100 * (1 - attribution["unknown_credits"] / attribution["total_credits"])
        print(f"\nAttribution: {known:.1f}% of credits map to a known organisation "
              f"({attribution['unknown_credits']} of {attribution['total_credits']} unknown).")


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

    caches = {r: load_cache(r) for r in REPO_LIST}
    first_seen = build_first_contribution(caches)

    results = {
        "generated_at": as_of.isoformat(),
        "months": args.months,
        "repos": {},
    }
    for repo_name in REPO_LIST:
        snap = snapshot_repo(caches[repo_name], github_to_org, email_to_org,
                             args.months, as_of, first_seen)
        results["repos"][repo_name] = snap
        _print_repo_summary(repo_name, snap, args.months)

    # Distinct-people metrics cannot be summed across repos, so the cross-repo
    # view is pivoted from the combined cache instead.
    merged = merge_caches(caches)
    results["aggregate"] = snapshot_repo(merged, github_to_org, email_to_org,
                                         args.months, as_of, first_seen)
    _print_aggregate_summary(results["aggregate"], args.months)
    exclude, host = load_org_config()
    results["project"] = project_totals(merged, github_to_org, email_to_org,
                                        args.months, as_of, exclude, host)

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
