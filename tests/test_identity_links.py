"""Tests for linking hashed co-author emails to GitHub logins (email_to_login.json).

Independent of the real mapping file: each test installs a fake map in
`main.EMAIL_TO_LOGIN`. Run from the repository root, since `main` and `people`
read their config files relative to the working directory.
"""

import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

import main
import people

LINKED_HASH = "ab" * 32
UNLINKED_HASH = "ff" * 32
LOGIN = "linked-dev"
OPENER = "opener-dev"
REVIEWER = "reviewer-dev"

AS_OF = datetime(2026, 10, 1, tzinfo=timezone.utc)
SINCE = datetime(2026, 4, 1, tzinfo=timezone.utc)

GITHUB_TO_ORG = {OPENER: "OrgA", LOGIN: "OrgA", REVIEWER: "OrgB"}

REVIEWS = [
    {"user": REVIEWER, "submitted_at": "2026-08-01T10:00:00+00:00", "state": "APPROVED"},
    {"user": LOGIN, "submitted_at": "2026-08-01T11:00:00+00:00", "state": "COMMENTED"},
]


def make_pr(**overrides):
    pr = dict(
        user=OPENER,
        commit_authors=[],
        coauthor_logins=[],
        coauthor_email_hashes=[],
        merged=True,
        created_at="2026-08-01T00:00:00+00:00",
        merged_at="2026-08-02T00:00:00+00:00",
        title="feat: something",
        reviews=[],
        number=1,
    )
    pr.update(overrides)
    return pr


class LinkedIdentityTestCase(unittest.TestCase):
    """Base class: installs a fake email→login map and silences stdout."""

    def setUp(self):
        patcher = mock.patch.object(main, "EMAIL_TO_LOGIN", {LINKED_HASH: LOGIN})
        patcher.start()
        self.addCleanup(patcher.stop)
        stdout = contextlib.redirect_stdout(io.StringIO())
        stdout.__enter__()
        self.addCleanup(stdout.__exit__, None, None, None)


class PrIdentitiesTests(LinkedIdentityTestCase):
    def test_linked_hash_yields_login_and_unlinked_stays_email(self):
        """A linked hash becomes the login; an unlinked hash stays `email:<hash>`."""
        ids = main._pr_identities(make_pr(coauthor_email_hashes=[LINKED_HASH]))
        self.assertEqual(ids, {OPENER, LOGIN})
        ids = main._pr_identities(make_pr(coauthor_email_hashes=[UNLINKED_HASH]))
        self.assertEqual(ids, {OPENER, f"email:{UNLINKED_HASH}"})

    def test_login_and_linked_hash_are_one_identity(self):
        """A login plus its linked hash on one PR is a single identity, other hashes kept."""
        ids = main._pr_identities(make_pr(coauthor_logins=[LOGIN], coauthor_email_hashes=[LINKED_HASH]))
        self.assertEqual(ids, {OPENER, LOGIN})
        ids = main._pr_identities(
            make_pr(commit_authors=[LOGIN], coauthor_email_hashes=[LINKED_HASH, UNLINKED_HASH])
        )
        self.assertEqual(ids, {OPENER, LOGIN, f"email:{UNLINKED_HASH}"})


class FirstContributionTests(LinkedIdentityTestCase):
    def test_first_contribution_is_earliest_across_login_and_email(self):
        """The earlier merge date wins whichever identity came first; unmerged PRs are ignored."""
        early = "2026-01-05T00:00:00+00:00"
        late = "2026-03-05T00:00:00+00:00"
        for login_date, email_date in ((early, late), (late, early)):
            pr_login = make_pr(number=1, coauthor_logins=[LOGIN], merged_at=login_date)
            pr_email = make_pr(number=2, coauthor_email_hashes=[LINKED_HASH], merged_at=email_date)
            caches = {"repo": {"prs": {"1": pr_login, "2": pr_email}, "issues": {}}}
            first = main.build_first_contribution(caches)
            self.assertEqual(first[LOGIN], datetime.fromisoformat(early))
            self.assertNotIn(f"email:{LINKED_HASH}", first)

        unmerged = make_pr(coauthor_email_hashes=[LINKED_HASH], merged=False)
        caches = {"repo": {"prs": {"1": unmerged}, "issues": {}}}
        self.assertNotIn(LOGIN, main.build_first_contribution(caches))


class PeopleCollectTests(LinkedIdentityTestCase):
    def test_collect_credits_login_for_email_only_coauthored_pr(self):
        """people.collect credits the login for a PR co-authored by linked email only."""
        pr = make_pr(coauthor_email_hashes=[LINKED_HASH, UNLINKED_HASH], reviews=REVIEWS)
        caches = {"anemoi": {"prs": {"1": pr}, "issues": {}}}
        persons = people.collect(caches, dict(GITHUB_TO_ORG), {"Bots"}, SINCE, AS_OF)[0]

        self.assertEqual(persons[LOGIN]["merged"], 1)
        self.assertEqual(persons[LOGIN]["lifetime_prs"], 1)
        self.assertFalse([k for k in persons if k.startswith("email:")])
        self.assertEqual(len(persons[REVIEWER]["reviewed"]), 1)
        self.assertEqual(len(persons[LOGIN]["reviewed"]), 1)
        self.assertEqual(persons[REVIEWER]["merged"], 0)


class SnapshotRepoTests(LinkedIdentityTestCase):
    def _snapshot(self, cache):
        first_seen = main.build_first_contribution({"repo": cache})
        return main.snapshot_repo(cache, dict(GITHUB_TO_ORG), {}, 6, AS_OF, first_seen)

    def test_snapshot_dedupes_contributors_and_leaves_reviews_unchanged(self):
        """Login+linked hash count once; reviews are identical with and without links."""
        pr = make_pr(coauthor_logins=[LOGIN], coauthor_email_hashes=[LINKED_HASH], reviews=REVIEWS)
        cache = {"prs": {"1": pr}, "issues": {}}

        with_links = self._snapshot(cache)
        self.assertEqual(with_links["contributors"]["OrgA"], 2)  # opener + linked login
        self.assertNotIn("Unknown", with_links["contributors"])

        with mock.patch.object(main, "EMAIL_TO_LOGIN", {}):
            without_links = self._snapshot(cache)
        self.assertEqual(without_links["contributors"]["OrgA"], 2)
        self.assertEqual(without_links["contributors"]["Unknown"], 1)  # the bare email

        for key in ("reviewers", "prs_reviewed", "review_matrix", "review_states", "latency"):
            self.assertEqual(with_links[key], without_links[key], key)


class LoaderTests(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(os.chdir, self._cwd)
        os.chdir(self._tmp.name)

    def test_loader_handles_missing_file_and_ignores_underscore_keys(self):
        """A missing file gives {}; `_`-prefixed note keys are dropped, links kept."""
        self.assertEqual(main.load_email_to_login_mapping(), {})

        with open("email_to_login.json", "w") as f:
            json.dump({"_note": "ignored", LINKED_HASH: LOGIN}, f)
        self.assertEqual(main.load_email_to_login_mapping(), {LINKED_HASH: LOGIN})


if __name__ == "__main__":
    unittest.main()
