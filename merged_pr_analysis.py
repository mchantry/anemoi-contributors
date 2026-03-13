import json
from github import Github
from datetime import datetime, timedelta, timezone
import os
from dotenv import load_dotenv
from collections import Counter

# Load environment variables
load_dotenv()

# Get GitHub token from environment variables
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
# Repository details
REPO_OWNER = "ecmwf"
REPO_NAME = "anemoi-core"

def load_github_to_org_mapping():
    """Load the GitHub-to-organization mapping from a JSON file."""
    with open("github_to_org.json", "r") as f:
        return json.load(f)

def aggregate_prs_by_organization(pr_type_count_by_user, github_to_org):
    """Aggregate PR type counts by organization."""
    org_pr_type_count = {}
    for user, pr_type_count in pr_type_count_by_user.items():
        org = github_to_org.get(user, "Unknown")  # Default to "Unknown" if user not in mapping
        if org not in org_pr_type_count:
            org_pr_type_count[org] = Counter()
        org_pr_type_count[org].update(pr_type_count)
        if org == "Unknown":
            print(f"Unknown GitHub user: {user}")  # Print unknown users for future assignment
    return org_pr_type_count

def get_merged_prs_by_type_and_user(repo):
    """Fetch merged PRs in the last 3 months and categorize them by type for each user."""
    three_months_ago = datetime.now(timezone.utc) - timedelta(days=90)
    pulls = repo.get_pulls(state="all")
    pr_type_count_by_user = {}

    for pr in pulls:
        if pr.merged and pr.created_at >= three_months_ago:  # Only merged PRs in the last 3 months
            title = pr.title.lower()
            pr_type = "other"  # Default type
            if title.startswith("feat"):
                pr_type = "feat"
            elif title.startswith("fix"):
                pr_type = "fix"
            elif title.startswith("chore"):
                pr_type = "chore"
            elif title.startswith("docs"):
                pr_type = "docs"
            elif title.startswith("refactor"):
                pr_type = "refactor"
            elif title.startswith("test"):
                pr_type = "test"
            elif title.startswith("ci"):
                pr_type = "ci"

            # Increment the count for the user
            user = pr.user.login
            if pr_type == "other":  # Check if the PR is uncategorized
                print(f"Uncategorized PR: #{pr.number} - {pr.title}")  # Print PR number and title
            
            if user not in pr_type_count_by_user:
                pr_type_count_by_user[user] = Counter()
            pr_type_count_by_user[user][pr_type] += 1

    return pr_type_count_by_user

def main():
    # Authenticate with GitHub
    g = Github(GITHUB_TOKEN)
    
    # Load GitHub-to-organization mapping
    github_to_org = load_github_to_org_mapping()
    
    # Get the repository
    repo = g.get_repo(f"{REPO_OWNER}/{REPO_NAME}")
    
    # Fetch merged PRs by type for each user
    pr_type_count_by_user = get_merged_prs_by_type_and_user(repo)
    
    # Aggregate PRs by organization
    org_pr_type_count = aggregate_prs_by_organization(pr_type_count_by_user, github_to_org)
    
    # Output the results
    print(f"Merged PRs by Type in {REPO_NAME} (Last 3 Months) by Organization:")
    for org, pr_type_count in org_pr_type_count.items():
        print(f"\nOrganization: {org}")
        for pr_type, count in pr_type_count.most_common():
            print(f"- {pr_type}: {count}")

if __name__ == "__main__":
    main()