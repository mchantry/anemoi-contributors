from github import Github
from datetime import datetime, timezone
from dateutil.relativedelta import relativedelta
import argparse
import hashlib
import os
import re
import json
from dotenv import load_dotenv
from collections import Counter

# Load environment variables
load_dotenv()

# Get GitHub token from environment variables
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

# Repository details
REPO_OWNER = "ecmwf"

# Load the GitHub-to-organization mapping
def load_github_to_org_mapping():
    with open("github_to_org.json", "r") as f:
        return json.load(f)

def load_email_to_org_mapping():
    """Load the manually maintained email-to-organisation cache."""
    try:
        with open("email_to_org.json", "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def hash_email(email):
    """Normalise and hash an email address so we never store the plaintext on disk."""
    return hashlib.sha256(email.lower().strip().encode("utf-8")).hexdigest()

def resolve_coauthors(commit_message, email_to_org):
    """Extract Co-authored-by contributors from a commit message.

    Returns (logins, orgs):
    - logins: resolved via GitHub noreply format, fed through github_to_org as normal
    - orgs:   resolved directly from email_to_org.json (keyed by SHA-256 hash of the email)

    Unresolved emails are added to email_to_org as 'Unknown' for manual follow-up.
    Only hashes are stored on disk — plaintext emails appear in stdout for the human
    maintainer to look up, but never persist in the repository.
    """
    logins = set()
    orgs = set()
    for match in re.finditer(
        r"Co-authored-by:[^<]*<([^>]+)>", commit_message, re.IGNORECASE
    ):
        email = match.group(1).strip()
        # 1. GitHub noreply: digits+login@users.noreply.github.com → extract login
        noreply = re.match(r"(?:\d+\+)?([^@]+)@users\.noreply\.github\.com", email)
        if noreply:
            logins.add(noreply.group(1))
            continue
        # 2. Direct email → org mapping, keyed by SHA-256 hash of the email
        email_hash = hash_email(email)
        if email_hash in email_to_org:
            orgs.add(email_to_org[email_hash])
            continue
        # Unresolved — add to cache as Unknown for manual follow-up
        print(f"Unresolved Co-authored-by email: {email} (hash {email_hash[:12]}…)")
        email_to_org[email_hash] = "Unknown"
        orgs.add("Unknown")
    return logins, orgs

def aggregate_by_organization(user_contributions, github_to_org):
    """Aggregate contributions by organization."""
    org_contributions = Counter()
    for user, count in user_contributions.items():
        org = github_to_org.get(user, "Unknown")  # Default to "Unknown" if user not in mapping
        if org == "Unknown" and user not in github_to_org:
            print(f"Unknown GitHub user: {user}")  # Print unknown users for future assignment
            github_to_org[user] = "Unknown"
        org_contributions[org] += count
    return org_contributions

def get_contributors(repo):
    """Fetch contributors from the GitHub repository using PyGithub."""
    # Fetch contributors
    contributors = repo.get_contributors()
    return contributors

def get_issues_last_n_months(repo, github_to_org, months, as_of):
    """Fetch issues touched in the N months ending at as_of, aggregated by organisation."""
    since = as_of - relativedelta(months=months)
    issues = repo.get_issues(state="all", since=since)
    user_issue_count = Counter()

    for issue in issues:
        # Skip issues updated after as_of so historical snapshots are not polluted by future activity.
        if issue.updated_at and issue.updated_at > as_of:
            continue
        if issue.user:
            user_issue_count[issue.user.login] += 1

    # Aggregate by organization
    org_issue_count = aggregate_by_organization(user_issue_count, github_to_org)
    return org_issue_count

PR_TYPE_PREFIXES = ("feat", "fix", "chore", "docs", "refactor", "test", "ci", "perf", "build", "style")

def classify_pr_type(title):
    """Categorise a PR by its Conventional Commits prefix in the title."""
    t = title.lower().lstrip()
    for prefix in PR_TYPE_PREFIXES:
        # Match "feat:", "feat(scope):", "feat!:"
        if re.match(rf"{prefix}\s*(\([^)]*\))?\s*!?:", t):
            return prefix
    return "other"

def get_pull_requests_last_n_months(repo, github_to_org, email_to_org, months, as_of):
    """Fetch merged pull requests in the N months ending at as_of.
    Each org is counted once per PR, even if multiple authors from that org contributed.
    Authors are identified via: PR opener, commit authors, and Co-authored-by trailers.

    Returns (org_pr_count, org_pr_count_by_type):
    - org_pr_count: {org: total_prs}
    - org_pr_count_by_type: {pr_type: {org: count}}
    """
    since = as_of - relativedelta(months=months)
    pulls = repo.get_pulls(state="all")
    org_pr_count = Counter()
    org_pr_count_by_type = {}

    for pr in pulls:
        if pr.created_at < since or pr.created_at > as_of:
            continue
        if not pr.merged:
            continue
        # Skip PRs merged after as_of so historical snapshots ignore future merges.
        if pr.merged_at is None or pr.merged_at > as_of:
            continue

        # Collect login-based authors and directly-resolved orgs from co-author trailers
        authors = {pr.user.login}
        direct_orgs = set()
        for commit in pr.get_commits():
            if commit.author:
                authors.add(commit.author.login)
            coauthor_logins, coauthor_orgs = resolve_coauthors(commit.commit.message, email_to_org)
            authors |= coauthor_logins
            direct_orgs |= coauthor_orgs

        # Map logins to orgs, deduplicating per PR so each org is counted at most once
        orgs = set(direct_orgs)
        for user in authors:
            org = github_to_org.get(user, "Unknown")
            if org == "Unknown" and user not in github_to_org:
                print(f"Unknown GitHub user: {user}")
                github_to_org[user] = "Unknown"
            orgs.add(org)

        pr_type = classify_pr_type(pr.title)
        if pr_type == "other":
            print(f"Uncategorised PR: #{pr.number} - {pr.title}")

        for org in orgs:
            org_pr_count[org] += 1
            org_pr_count_by_type.setdefault(pr_type, Counter())[org] += 1

    return org_pr_count, org_pr_count_by_type

def get_reviews_last_n_months(repo, github_to_org, months, as_of):
    """
    Fetch code reviews in the N months ending at as_of and calculate both:
    - Total reviews (all reviews by all users)
    - Unique reviews (1 review per PR per user)
    Aggregate both by organization.
    """
    since = as_of - relativedelta(months=months)
    pulls = repo.get_pulls(state="all")
    user_total_review_count = Counter()
    user_unique_review_count = Counter()

    for pr in pulls:
        if not (since <= pr.created_at <= as_of):
            continue
        reviews = pr.get_reviews()
        users_reviewed = set()  # Track users who have reviewed this PR
        for review in reviews:
            # Skip reviews submitted after as_of; drop pending reviews with no timestamp.
            if review.submitted_at is None or review.submitted_at > as_of:
                continue
            if review.user:
                user_total_review_count[review.user.login] += 1
                if review.user.login not in users_reviewed:
                    user_unique_review_count[review.user.login] += 1
                    users_reviewed.add(review.user.login)

    # Aggregate both by organization
    org_total_review_count = aggregate_by_organization(user_total_review_count, github_to_org)
    org_unique_review_count = aggregate_by_organization(user_unique_review_count, github_to_org)

    return org_total_review_count, org_unique_review_count

def main(REPO_NAME, g, github_to_org, email_to_org, months, as_of):
    # Get the repository
    repo = g.get_repo(f"{REPO_OWNER}/{REPO_NAME}")

    print(f"------------------------------")
    print(f" {REPO_NAME}:")
    print(f"------------------------------")

    # Fetch issues created in the last N months
    org_issue_count = get_issues_last_n_months(repo, github_to_org, months, as_of)
    print(f"\nIssues created in {REPO_NAME} in the last {months} months by Organization (sorted):")
    for org, count in org_issue_count.most_common():
        print(f"- {org}: {count} issues")

    # Fetch PRs created in the last N months
    org_pr_count, org_pr_count_by_type = get_pull_requests_last_n_months(repo, github_to_org, email_to_org, months, as_of)
    print(f"\nPull Requests merged in {REPO_NAME} in the last {months} months by Organization (sorted):")
    for org, count in org_pr_count.most_common():
        print(f"- {org}: {count} PRs")

    print(f"\nPull Requests by type in {REPO_NAME} in the last {months} months:")
    for pr_type, org_counts in sorted(org_pr_count_by_type.items()):
        total = sum(org_counts.values())
        print(f"- {pr_type}: {total} PRs")

    # Fetch reviews performed in the last N months
    org_total_review_count, org_unique_review_count = get_reviews_last_n_months(repo, github_to_org, months, as_of)
    print(f"\nCode Reviews performed in {REPO_NAME} in the last {months} months by Organization (sorted):")
    for org, count in org_total_review_count.most_common():
        print(f"- {org}: {count} total reviews")

    print(f"\nUnique Code Reviews by Organization in {REPO_NAME} in the last {months} months (sorted):")
    for org, count in org_unique_review_count.most_common():
        print(f"- {org}: {count} unique reviews")

    return {
        "issues": dict(org_issue_count),
        "pull_requests": dict(org_pr_count),
        "pull_requests_by_type": {t: dict(c) for t, c in org_pr_count_by_type.items()},
        "total_reviews": dict(org_total_review_count),
        "unique_reviews": dict(org_unique_review_count),
    }

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Collect Anemoi contributor statistics.")
    parser.add_argument("--months", type=int, default=6,
                        help="Number of months of history to analyse (default: 6)")
    parser.add_argument("--as-of", dest="as_of", type=str, default=None,
                        help="Treat this YYYY-MM-DD date as 'now'. Default: today. "
                             "Historical runs write only the dated snapshot, not results.json.")
    args = parser.parse_args()

    if args.as_of is None:
        as_of = datetime.now(timezone.utc)
        is_today = True
    else:
        as_of = datetime.strptime(args.as_of, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        is_today = args.as_of == datetime.now(timezone.utc).strftime("%Y-%m-%d")

    g = Github(GITHUB_TOKEN)
    github_to_org = load_github_to_org_mapping()
    email_to_org = load_email_to_org_mapping()

    repo_list = ["anemoi", "anemoi-core", "anemoi-datasets",
                 "anemoi-inference", "anemoi-transform",
                 "anemoi-utils"]

    results = {
        "generated_at": as_of.isoformat(),
        "months": args.months,
        "repos": {},
    }
    for REPO_NAME in repo_list:
        results["repos"][REPO_NAME] = main(REPO_NAME, g, github_to_org, email_to_org, args.months, as_of)

    # Only refresh the "latest" pointer when running for today, so historical
    # backfills never clobber the current snapshot.
    if is_today:
        with open("results.json", "w") as f:
            json.dump(results, f, indent=2)
        print("\nResults saved to results.json")
    else:
        print(f"\nHistorical run (as-of {args.as_of}); results.json not modified.")

    # Archive a dated snapshot so we can build a time series across runs
    os.makedirs("history", exist_ok=True)
    snapshot_date = as_of.strftime("%Y-%m-%d")
    snapshot_path = f"history/results-{snapshot_date}.json"
    with open(snapshot_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"Snapshot archived to {snapshot_path}")

    with open("email_to_org.json", "w") as f:
        json.dump(email_to_org, f, indent=2)
    print("Email-to-org cache saved to email_to_org.json")

    with open("github_to_org.json", "w") as f:
        json.dump(github_to_org, f, indent=2)
    print("GitHub-to-org mapping saved to github_to_org.json")
