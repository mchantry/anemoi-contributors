from github import Github
from datetime import datetime, timedelta, timezone
import argparse
import os
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

def aggregate_by_organization(user_contributions, github_to_org):
    """Aggregate contributions by organization."""
    org_contributions = Counter()
    for user, count in user_contributions.items():
        org = github_to_org.get(user, "Unknown")  # Default to "Unknown" if user not in mapping
        org_contributions[org] += count
        if org == "Unknown":
            print(f"Unknown GitHub user: {user}")  # Print unknown users for future assignment
    return org_contributions

def get_contributors(repo):
    """Fetch contributors from the GitHub repository using PyGithub."""
    # Fetch contributors
    contributors = repo.get_contributors()
    return contributors

def get_issues_last_n_months(repo, github_to_org, months):
    """Fetch issues created in the last N months and aggregate by organization."""
    three_months_ago = datetime.now(timezone.utc) - timedelta(days=months * 30)
    issues = repo.get_issues(state="all", since=three_months_ago)
    user_issue_count = Counter()

    for issue in issues:
        if issue.user:  # Ensure the issue has a user (not a bot)
            user_issue_count[issue.user.login] += 1

    # Aggregate by organization
    org_issue_count = aggregate_by_organization(user_issue_count, github_to_org)
    return org_issue_count

def get_pull_requests_last_n_months(repo, github_to_org, months):
    """Fetch merged pull requests in the last N months.
    Each org is counted once per PR, even if multiple authors from that org contributed."""
    three_months_ago = datetime.now(timezone.utc) - timedelta(days=months * 30)
    pulls = repo.get_pulls(state="all")
    org_pr_count = Counter()

    for pr in pulls:
        if pr.created_at < three_months_ago or not pr.merged:
            continue

        # Collect all unique authors: PR opener + all commit authors
        authors = {pr.user.login}
        for commit in pr.get_commits():
            if commit.author:
                authors.add(commit.author.login)

        # Map to orgs, deduplicating per PR so each org is counted at most once
        orgs = set()
        for user in authors:
            org = github_to_org.get(user, "Unknown")
            if org == "Unknown":
                print(f"Unknown GitHub user: {user}")
            orgs.add(org)

        for org in orgs:
            org_pr_count[org] += 1

    return org_pr_count

def get_reviews_last_n_months(repo, github_to_org, months):
    """
    Fetch code reviews performed in the last N months and calculate both:
    - Total reviews (all reviews by all users)
    - Unique reviews (1 review per PR per user)
    Aggregate both by organization.
    """
    three_months_ago = datetime.now(timezone.utc) - timedelta(days=months * 30)
    pulls = repo.get_pulls(state="all")
    user_total_review_count = Counter()
    user_unique_review_count = Counter()

    for pr in pulls:
        if pr.created_at >= three_months_ago:  # Only consider PRs created in the last 3 months
            reviews = pr.get_reviews()
            users_reviewed = set()  # Track users who have reviewed this PR
            for review in reviews:
                if review.user:  # Ensure the review has a user (not a bot)
                    # Count total reviews
                    user_total_review_count[review.user.login] += 1
                    # Count unique reviews (only 1 review per user per PR)
                    if review.user.login not in users_reviewed:
                        user_unique_review_count[review.user.login] += 1
                        users_reviewed.add(review.user.login)

    # Aggregate both by organization
    org_total_review_count = aggregate_by_organization(user_total_review_count, github_to_org)
    org_unique_review_count = aggregate_by_organization(user_unique_review_count, github_to_org)

    return org_total_review_count, org_unique_review_count

def main(REPO_NAME, g, github_to_org, months):
    # Get the repository
    repo = g.get_repo(f"{REPO_OWNER}/{REPO_NAME}")

    print(f"------------------------------")
    print(f" {REPO_NAME}:")
    print(f"------------------------------")

    # Fetch issues created in the last N months
    org_issue_count = get_issues_last_n_months(repo, github_to_org, months)
    print(f"\nIssues created in {REPO_NAME} in the last {months} months by Organization (sorted):")
    for org, count in org_issue_count.most_common():
        print(f"- {org}: {count} issues")

    # Fetch PRs created in the last N months
    org_pr_count = get_pull_requests_last_n_months(repo, github_to_org, months)
    print(f"\nPull Requests merged in {REPO_NAME} in the last {months} months by Organization (sorted):")
    for org, count in org_pr_count.most_common():
        print(f"- {org}: {count} PRs")

    # Fetch reviews performed in the last N months
    org_total_review_count, org_unique_review_count = get_reviews_last_n_months(repo, github_to_org, months)
    print(f"\nCode Reviews performed in {REPO_NAME} in the last {months} months by Organization (sorted):")
    for org, count in org_total_review_count.most_common():
        print(f"- {org}: {count} total reviews")

    print(f"\nUnique Code Reviews by Organization in {REPO_NAME} in the last {months} months (sorted):")
    for org, count in org_unique_review_count.most_common():
        print(f"- {org}: {count} unique reviews")

    return {
        "issues": dict(org_issue_count),
        "pull_requests": dict(org_pr_count),
        "total_reviews": dict(org_total_review_count),
        "unique_reviews": dict(org_unique_review_count),
    }

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Collect Anemoi contributor statistics.")
    parser.add_argument("--months", type=int, default=3,
                        help="Number of months of history to analyse (default: 3)")
    args = parser.parse_args()

    g = Github(GITHUB_TOKEN)
    github_to_org = load_github_to_org_mapping()

    repo_list = ["anemoi-core", "anemoi-datasets",
                 "anemoi-inference", "anemoi-transform",
                 "anemoi-utils"]

    results = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "months": args.months,
        "repos": {},
    }
    for REPO_NAME in repo_list:
        results["repos"][REPO_NAME] = main(REPO_NAME, g, github_to_org, args.months)

    with open("results.json", "w") as f:
        json.dump(results, f, indent=2)
    print("\nResults saved to results.json")
