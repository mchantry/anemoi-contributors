"""Persistent per-repo cache of the raw signals we aggregate into snapshots.

We store PRs (with reviews, commit authors, co-author email hashes, LOC) and
issues to `cache/{repo}.json`. Snapshots are then in-memory pivots over the
cache — no GitHub API calls needed. Weekly refreshes only touch PRs still
open at last cache time plus any created since, so runtime stays bounded
regardless of history size.
"""
import hashlib
import json
import os
import re
from datetime import datetime, timezone

from dateutil.relativedelta import relativedelta

CACHE_DIR = "cache"
SCHEMA_VERSION = 1

_COAUTHOR_RE = re.compile(r"Co-authored-by:[^<]*<([^>]+)>", re.IGNORECASE)
_NOREPLY_RE = re.compile(r"(?:\d+\+)?([^@]+)@users\.noreply\.github\.com")


def hash_email(email):
    """Normalise and hash an email address so plaintext never persists on disk."""
    return hashlib.sha256(email.lower().strip().encode("utf-8")).hexdigest()


def _cache_path(repo_name):
    return os.path.join(CACHE_DIR, f"{repo_name}.json")


def _empty_cache(repo_name):
    return {
        "schema_version": SCHEMA_VERSION,
        "repo": repo_name,
        "last_refreshed_at": None,
        "last_issues_since": None,
        "prs": {},
        "issues": {},
    }


def load_cache(repo_name):
    """Load the per-repo cache, returning an empty skeleton if none exists."""
    path = _cache_path(repo_name)
    if not os.path.exists(path):
        return _empty_cache(repo_name)
    with open(path, "r") as f:
        data = json.load(f)
    if data.get("schema_version") != SCHEMA_VERSION:
        return _empty_cache(repo_name)
    return data


def save_cache(repo_name, cache):
    """Atomic write so a mid-refresh crash never leaves a truncated file."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = _cache_path(repo_name)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cache, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def _pr_to_dict(pr, email_to_org):
    """Serialise a PyGithub PullRequest. `email_to_org` is updated in-place with
    any newly-seen non-noreply co-author email hashes (marked 'Unknown')."""
    commit_authors = set()
    coauthor_logins = set()
    coauthor_email_hashes = set()

    for commit in pr.get_commits():
        if commit.author:
            commit_authors.add(commit.author.login)
        message = commit.commit.message or ""
        for match in _COAUTHOR_RE.finditer(message):
            email = match.group(1).strip()
            noreply = _NOREPLY_RE.match(email)
            if noreply:
                coauthor_logins.add(noreply.group(1))
                continue
            h = hash_email(email)
            if h not in email_to_org:
                # Log the plaintext once for a human to look up; only the hash persists.
                print(f"Unresolved Co-authored-by email: {email} (hash {h[:12]}…)")
                email_to_org[h] = "Unknown"
            coauthor_email_hashes.add(h)

    reviews = []
    for review in pr.get_reviews():
        reviews.append({
            "user": review.user.login if review.user else None,
            "submitted_at": review.submitted_at.isoformat() if review.submitted_at else None,
            "state": review.state,
        })

    return {
        "number": pr.number,
        "title": pr.title,
        "state": pr.state,
        "created_at": pr.created_at.isoformat() if pr.created_at else None,
        "merged": bool(pr.merged),
        "merged_at": pr.merged_at.isoformat() if pr.merged_at else None,
        "closed_at": pr.closed_at.isoformat() if pr.closed_at else None,
        "user": pr.user.login if pr.user else None,
        "additions": pr.additions,
        "deletions": pr.deletions,
        "changed_files": pr.changed_files,
        "commit_authors": sorted(commit_authors),
        "coauthor_logins": sorted(coauthor_logins),
        "coauthor_email_hashes": sorted(coauthor_email_hashes),
        "reviews": reviews,
    }


def _issue_to_dict(issue):
    return {
        "number": issue.number,
        "created_at": issue.created_at.isoformat() if issue.created_at else None,
        "updated_at": issue.updated_at.isoformat() if issue.updated_at else None,
        "state": issue.state,
        "user": issue.user.login if issue.user else None,
        # PyGithub's get_issues returns PRs too; the snapshot layer skips those.
        "is_pull_request": issue.pull_request is not None,
    }


def refresh_repo_cache(repo, cache, email_to_org, save_every=25):
    """Update the cache in-place with newest PRs and recently-updated issues.

    Saves intermediate progress every `save_every` new PRs so a timeout on a
    large initial build doesn't lose work.
    """
    repo_name = cache["repo"]
    print(f"[cache] refreshing {repo_name}...")

    # Re-check PRs that were still open at last cache time — they may have merged/closed.
    open_prs = [n for n, p in cache["prs"].items() if p.get("state") == "open"]
    for i, n in enumerate(open_prs, 1):
        pr = repo.get_pull(int(n))
        cache["prs"][n] = _pr_to_dict(pr, email_to_org)
        if i % save_every == 0:
            cache["last_refreshed_at"] = datetime.now(timezone.utc).isoformat()
            save_cache(repo_name, cache)
    if open_prs:
        print(f"[cache]   re-checked {len(open_prs)} previously-open PR(s)")

    # Fetch new PRs (PR numbers are monotonic, so anything > max_num is new).
    max_num = max((int(n) for n in cache["prs"]), default=0)
    added = 0
    for pr in repo.get_pulls(state="all", sort="created", direction="desc"):
        if pr.number <= max_num:
            break
        cache["prs"][str(pr.number)] = _pr_to_dict(pr, email_to_org)
        added += 1
        if added % save_every == 0:
            print(f"[cache]   added {added} new PRs so far, saving progress...")
            cache["last_refreshed_at"] = datetime.now(timezone.utc).isoformat()
            save_cache(repo_name, cache)
    print(f"[cache]   added {added} new PR(s)")

    # Issues: use PyGithub's since= to fetch anything touched since our last window.
    since_iso = cache.get("last_issues_since")
    if since_iso:
        since = datetime.fromisoformat(since_iso)
    else:
        since = datetime(2000, 1, 1, tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    fetched = 0
    for issue in repo.get_issues(state="all", since=since):
        cache["issues"][str(issue.number)] = _issue_to_dict(issue)
        fetched += 1
    # 1h overlap on next run to absorb clock skew and edits at the boundary.
    cache["last_issues_since"] = (now - relativedelta(hours=1)).isoformat()
    cache["last_refreshed_at"] = now.isoformat()
    print(f"[cache]   refreshed {fetched} issue(s) since {since.isoformat()}")

    save_cache(repo_name, cache)
    return cache
