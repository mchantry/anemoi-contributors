"""Render an unlisted page celebrating individual contributors.

Reads the per-repo cache directly (the snapshots only hold per-organisation
data) and uses the same window as the latest `results.json`, so it lines up
with the main page. Writes `docs/people.html`, which nothing links to.
"""
import json
from collections import Counter, defaultdict
from datetime import datetime
from html import escape

import plotly.graph_objects as go
from dateutil.relativedelta import relativedelta

from cache import load_cache
from main import (
    REPO_LIST,
    REPO_OWNER,
    _pr_identities,
    build_first_contribution,
    classify_pr_type,
    load_github_to_org_mapping,
    load_org_config,
)
from plot import site_color_map

OUTPUT_PATH = "docs/people.html"
LEADERBOARD_SIZE = 10
# Merged PRs plus PRs reviewed needed before someone's review ratio is shown.
MIN_RATIO_ACTIVITY = 20
VETERAN_YEARS = 2

# (emoji, name, description, key into the per-person badge data, minimum)
BADGES = [
    ("🌍", "Grand tour", f"Merged or reviewed PRs in all {len(REPO_LIST)} repositories", "repos", len(REPO_LIST)),
    ("🗺️", "Explorer", f"Active in {len(REPO_LIST) - 1} of the {len(REPO_LIST)} repositories", "repos_almost", 1),
    ("🌉", "Bridge builder", "Reviewed PRs from 3 or more other organisations", "orgs_reviewed", 3),
    ("👋", "Welcoming committee", "Reviewed the first PR of 3 or more newcomers", "newcomers_reviewed", 3),
    ("🐛", "Bug squasher", "10 or more merged <code>fix</code> PRs", "fix", 10),
    ("📚", "Docs champion", "3 or more merged <code>docs</code> PRs", "docs", 3),
    ("🧪", "Test guardian", "3 or more merged <code>test</code> PRs", "test", 3),
    ("🧹", "Spring cleaning", "Removed more lines than they added, across 5 or more merged PRs", "net_negative", 1),
    ("🔎", "Issue spotter", "Opened 10 or more issues", "issues", 10),
    ("💯", "Centurion", "100 or more merged PRs since the project began", "lifetime_prs", 100),
    ("🏛️", "Veteran", f"First merged PR more than {VETERAN_YEARS} years ago, and still contributing", "veteran", 1),
]


def collect(caches, github_to_org, exclude, since, as_of):
    """Per-person activity in the window, plus the lifetime facts some badges need."""
    first_seen = build_first_contribution(caches)
    person = defaultdict(lambda: {
        "merged": 0, "reviewed": set(), "cross_org_reviewed": set(), "first_responder": 0,
        "issues": 0, "repos": set(), "orgs_reviewed": set(), "newcomers_reviewed": set(),
        "types": Counter(), "additions": 0, "deletions": 0, "lifetime_prs": 0,
    })
    newcomers = {}

    def is_person(identity):
        return identity and not identity.startswith("email:") and github_to_org.get(identity) not in exclude

    for repo, cache in caches.items():
        for number, pr in cache["prs"].items():
            opener = pr.get("user")
            opener_org = github_to_org.get(opener, "Unknown")
            merged_at = datetime.fromisoformat(pr["merged_at"]) if pr.get("merged_at") else None
            created_at = datetime.fromisoformat(pr["created_at"]) if pr.get("created_at") else None
            credited = {i for i in _pr_identities(pr) if is_person(i)} if pr.get("merged") and merged_at else set()

            for identity in credited:
                if merged_at <= as_of:
                    person[identity]["lifetime_prs"] += 1

            # The opener's first merged PR anywhere in Anemoi, merged in the window.
            opener_first = merged_at and first_seen.get(opener) == merged_at and since <= merged_at <= as_of
            if opener_first and is_person(opener):
                newcomers[opener] = (repo, number, pr.get("title") or "", merged_at)

            reviews = sorted(
                (r for r in pr.get("reviews", [])
                 if r.get("submitted_at") and r.get("user") != opener and is_person(r.get("user"))),
                key=lambda r: r["submitted_at"],
            )
            if reviews and created_at and since <= created_at <= as_of:
                person[reviews[0]["user"]]["first_responder"] += 1
            for review in reviews:
                if not (since <= datetime.fromisoformat(review["submitted_at"]) <= as_of):
                    continue
                p = person[review["user"]]
                p["reviewed"].add((repo, number))
                p["repos"].add(repo)
                if opener_org != github_to_org.get(review["user"], "Unknown"):
                    p["cross_org_reviewed"].add((repo, number))
                    if opener_org not in exclude | {"Unknown", "Other"}:
                        p["orgs_reviewed"].add(opener_org)
                if opener_first:
                    p["newcomers_reviewed"].add(opener)

            if not (created_at and merged_at and since <= created_at <= as_of and merged_at <= as_of):
                continue
            pr_type = classify_pr_type(pr.get("title"))
            for identity in credited:
                p = person[identity]
                p["merged"] += 1
                p["repos"].add(repo)
                p["types"][pr_type] += 1
                p["additions"] += pr.get("additions") or 0
                p["deletions"] += pr.get("deletions") or 0

        for issue in cache["issues"].values():
            user = issue.get("user")
            if issue.get("is_pull_request") or not is_person(user) or not issue.get("created_at"):
                continue
            if since <= datetime.fromisoformat(issue["created_at"]) <= as_of:
                person[user]["issues"] += 1

    active = {login: p for login, p in person.items()
              if p["merged"] or p["reviewed"] or p["issues"]}
    return active, newcomers, first_seen


def badge_value(login, p, key, first_seen, as_of):
    if key == "repos":
        return len(p["repos"])
    if key == "repos_almost":
        return int(len(p["repos"]) == len(REPO_LIST) - 1)
    if key in ("orgs_reviewed", "newcomers_reviewed"):
        return len(p[key])
    if key in ("fix", "docs", "test"):
        return p["types"][key]
    if key == "net_negative":
        return int(p["merged"] >= 5 and p["deletions"] > p["additions"])
    if key == "veteran":
        first = first_seen.get(login)
        return int(bool(first and p["merged"] and first < as_of - relativedelta(years=VETERAN_YEARS)))
    return p[key]


def avatar(login, org, size=28):
    return (f'<a class="who" href="https://github.com/{escape(login)}">'
            f'<img src="https://github.com/{escape(login)}.png?size={size * 2}" width="{size}" height="{size}" alt="">'
            f'<span>{escape(login)}</span></a><small>{escape(org)}</small>')


def leaderboard(title, blurb, people, github_to_org, value):
    ranked = sorted(((value(p), login) for login, p in people.items() if value(p)), key=lambda t: (-t[0], t[1]))
    rows = ""
    rank = 0
    previous = None
    for position, (score, login) in enumerate(ranked, start=1):
        if score != previous:
            rank = position
            previous = score
        if rank > LEADERBOARD_SIZE:
            break
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, str(rank))
        rows += (f'<tr><td class="rank">{medal}</td><td>{avatar(login, github_to_org.get(login, "Unknown"))}</td>'
                 f'<td class="num">{score}</td></tr>')
    return f"""<div class="board"><h3>{title}</h3><p class="lead">{blurb}</p>
      <table>{rows}</table></div>"""


def ratio_section(people, github_to_org, color_map):
    eligible = {login: p for login, p in people.items()
                if p["merged"] + len(p["reviewed"]) >= MIN_RATIO_ACTIVITY}
    if not eligible:
        return ""

    fig = go.Figure()
    by_org = defaultdict(list)
    for login, p in eligible.items():
        by_org[github_to_org.get(login, "Unknown")].append(login)
    for org, logins in sorted(by_org.items()):
        fig.add_trace(go.Scatter(
            name=org, mode="markers+text", textposition="top center",
            x=[eligible[l]["merged"] for l in logins], y=[len(eligible[l]["reviewed"]) for l in logins],
            text=logins, marker=dict(size=11, color=color_map.get(org)),
            hovertemplate="%{text}: %{x} merged, %{y} reviewed<extra>" + org + "</extra>",
        ))
    top = max(max(p["merged"], len(p["reviewed"])) for p in eligible.values()) * 1.1
    fig.add_trace(go.Scatter(x=[0, top], y=[0, top], mode="lines", showlegend=False, hoverinfo="skip",
                             line=dict(color="#c5ccd5", dash="dash")))
    fig.update_layout(height=520, xaxis_title="PRs merged", yaxis_title="PRs reviewed",
                      xaxis=dict(range=[0, top]), yaxis=dict(range=[0, top]), legend_title="Organisation",
                      margin=dict(t=30))

    ranked = sorted(eligible.items(),
                    key=lambda kv: (-len(kv[1]["reviewed"]) / max(kv[1]["merged"], 1), kv[0]))
    rows = ""
    for login, p in ranked:
        reviewed = len(p["reviewed"])
        ratio = f"{reviewed / p['merged']:.1f}" if p["merged"] else "reviews only"
        share = 100 * reviewed / (reviewed + p["merged"])
        rows += (f'<tr><td>{avatar(login, github_to_org.get(login, "Unknown"))}</td>'
                 f'<td class="num">{p["merged"]}</td><td class="num">{reviewed}</td><td class="num"><b>{ratio}</b></td>'
                 f'<td><div class="balance"><i style="width:{share:.0f}%"></i></div></td></tr>')
    return f"""{fig.to_html(full_html=False, include_plotlyjs="cdn")}
  <table class="ratio">
    <thead><tr><th>Person</th><th>PRs merged</th><th>PRs reviewed</th><th>Reviews per PR</th>
    <th><span class="key-review">Reviewing</span> / <span class="key-write">writing</span></th></tr></thead>
    <tbody>{rows}</tbody>
  </table>"""


def badges_section(people, github_to_org, first_seen, as_of):
    cards = ""
    for emoji, name, description, key, minimum in BADGES:
        holders = sorted(
            ((badge_value(login, p, key, first_seen, as_of), login) for login, p in people.items()),
            key=lambda t: (-t[0], t[1]),
        )
        holders = [login for value, login in holders if value >= minimum]
        if holders:
            names = "".join(f"<li>{avatar(l, github_to_org.get(l, 'Unknown'), 22)}</li>" for l in holders)
        else:
            names = '<li class="none">Up for grabs</li>'
        cards += f"""<div class="badge"><div class="emoji">{emoji}</div><h3>{name}</h3>
      <p>{description}</p><ul>{names}</ul></div>"""
    return f'<div class="badges">{cards}</div>'


def newcomers_section(newcomers, github_to_org):
    if not newcomers:
        return ""
    items = ""
    for login, (repo, number, title, merged_at) in sorted(newcomers.items(), key=lambda kv: kv[1][3]):
        items += (f'<li>{avatar(login, github_to_org.get(login, "Unknown"))} '
                  f'<a class="pr" href="https://github.com/{REPO_OWNER}/{repo}/pull/{number}">'
                  f'{escape(title)}</a> <small>{repo} · {merged_at:%d %b %Y}</small></li>')
    return f'<ul class="newcomers">{items}</ul>'


STYLE = """
    body { font-family: sans-serif; max-width: 1100px; margin: 40px auto; padding: 0 20px; color: #333; }
    h2 { margin-top: 2em; }
    p.lead { color: #555; margin-top: -0.5em; }
    a.who { display: inline-flex; align-items: center; gap: 6px; color: #1d3557; text-decoration: none; font-weight: 600; }
    a.who img { border-radius: 50%; background: #e3e6ea; }
    a.who + small { margin-left: 6px; color: #8d99ae; }
    .boards { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 20px; }
    .board { background: #f5f7fa; border-radius: 8px; padding: 14px 16px; }
    .board h3 { margin: 0 0 8px; }
    .board p.lead { margin: 0 0 10px; font-size: 0.85em; }
    .board table { width: 100%; border-collapse: collapse; }
    .board td { padding: 4px 0; }
    td.rank { width: 32px; text-align: center; color: #8d99ae; }
    td.num { text-align: right; font-weight: 600; color: #1d3557; }
    table.ratio { border-collapse: collapse; width: 100%; margin-top: 12px; font-size: 0.92em; }
    table.ratio th, table.ratio td { padding: 6px 10px 6px 0; text-align: left; border-bottom: 1px solid #e3e6ea; }
    table.ratio th:nth-child(2), table.ratio th:nth-child(3), table.ratio th:nth-child(4),
    table.ratio td.num { text-align: right; padding-right: 24px; }
    .key-review { color: #2a9d8f; }
    .key-write { color: #e07b39; }
    .balance { width: 160px; height: 10px; background: #f4a261; border-radius: 5px; overflow: hidden; }
    .balance i { display: block; height: 100%; background: #2a9d8f; }
    .badges { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 16px; }
    .badge { background: #f5f7fa; border-radius: 8px; padding: 14px 16px; }
    .badge .emoji { font-size: 2em; }
    .badge h3 { margin: 4px 0; }
    .badge p { margin: 0 0 8px; font-size: 0.85em; color: #555; }
    .badge ul, ul.newcomers { list-style: none; padding: 0; margin: 0; line-height: 2; }
    .badge li.none { color: #b0b7c0; font-style: italic; }
    ul.newcomers li { padding: 4px 0; border-bottom: 1px solid #e3e6ea; }
    ul.newcomers a.pr { margin-left: 10px; color: #333; }
    ul.newcomers small { color: #8d99ae; margin-left: 6px; }
"""


def main():
    with open("results.json") as f:
        latest = json.load(f)
    as_of = datetime.fromisoformat(latest["generated_at"])
    months = latest.get("months", 6)
    since = as_of - relativedelta(months=months)

    github_to_org = load_github_to_org_mapping()
    exclude, _ = load_org_config()
    caches = {repo: load_cache(repo) for repo in REPO_LIST}
    people, newcomers, first_seen = collect(caches, github_to_org, exclude, since, as_of)

    # The site's organisation colours, so people match their organisation elsewhere.
    color_map = site_color_map(latest=latest)

    period = f"the last {months} month{'s' if months != 1 else ''}"
    boards = [
        leaderboard("Most PRs reviewed", "Distinct PRs reviewed, not counting their own.",
                    people, github_to_org, lambda p: len(p["reviewed"])),
        leaderboard("Most PRs merged", "Merged PRs they opened, committed to or co-authored.",
                    people, github_to_org, lambda p: p["merged"]),
        leaderboard("Across organisations", "PRs reviewed for someone from another organisation.",
                    people, github_to_org, lambda p: len(p["cross_org_reviewed"])),
        leaderboard("First to review", "PRs where theirs was the first review.",
                    people, github_to_org, lambda p: p["first_responder"]),
        leaderboard("Most issues opened", "Bug reports, ideas and questions.",
                    people, github_to_org, lambda p: p["issues"]),
        leaderboard("Widest reach", f"How many of the {len(REPO_LIST)} repositories they merged or reviewed in.",
                    people, github_to_org, lambda p: len(p["repos"])),
    ]

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="robots" content="noindex, nofollow">
  <title>Anemoi Contributors — People</title>
  <style>{STYLE}</style>
</head>
<body>
  <p><a href="index.html">&larr; Organisation view</a></p>
  <h1>The people behind Anemoi</h1>
  <p class="lead">Celebrating individual contributions in {period}, up to {as_of:%d %B %Y}.
  {len(people)} people merged, reviewed or reported something in that time — thank you all.</p>

  <h2>Leaderboards</h2>
  <div class="boards">{"".join(boards)}</div>

  <h2>Reviewing and writing</h2>
  <p class="lead">Reviews given for every PR merged, for everyone with at least {MIN_RATIO_ACTIVITY} merged PRs and
  reviews combined. Above the dashed line, someone reviews more than they write. Neither side is better —
  a healthy project needs both.</p>
  {ratio_section(people, github_to_org, color_map)}

  <h2>Achievements</h2>
  <p class="lead">Earned in {period}, except <em>Centurion</em> and <em>Veteran</em>, which count the whole history
  for anyone still active.</p>
  {badges_section(people, github_to_org, first_seen, as_of)}

  <h2>New faces</h2>
  <p class="lead">Everyone whose first merged PR anywhere in Anemoi landed in {period}. Welcome!</p>
  {newcomers_section(newcomers, github_to_org)}

  <h2>Methodology</h2>
  <p>Built from the same cached GitHub data and the same {months}-month window as the
  <a href="index.html">organisation view</a>, across {", ".join(REPO_LIST)}. A merged PR is credited to its opener,
  every commit author and every <code>Co-authored-by:</code> GitHub user; co-authors known only by email address are
  left out, as they have no public profile to show. PRs reviewed counts distinct PRs, not review comments, and never a
  person's own PR. Bots and code agents are excluded throughout.</p>
</body>
</html>"""
    with open(OUTPUT_PATH, "w") as f:
        f.write(html)
    print(f"Wrote {OUTPUT_PATH} ({len(people)} people)")


if __name__ == "__main__":
    main()
