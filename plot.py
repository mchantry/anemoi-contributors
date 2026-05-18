import json
from collections import Counter
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots

with open("results.json") as f:
    data = json.load(f)

months = data.get("months", 3)

repos = list(data["repos"].keys())
metric_keys  = ["issues", "pull_requests", "total_reviews", "unique_reviews"]
metric_labels = ["Issues", "PRs", "Total Reviews", "Unique Reviews"]

# Collect all orgs across all repos and metrics
all_orgs = set()
for repo_data in data["repos"].values():
    for key in metric_keys:
        all_orgs.update(repo_data[key].keys())
all_orgs = sorted(all_orgs)

colors = px.colors.qualitative.Plotly
color_map = {org: colors[i % len(colors)] for i, org in enumerate(all_orgs)}

# Orgs hidden by default (still toggleable via the legend)
HIDDEN_BY_DEFAULT = {"Bots", "CodeAgents"}

# Aggregate each metric across all repos
aggregated = {key: Counter() for key in metric_keys}
for repo_data in data["repos"].values():
    for key in metric_keys:
        for org, count in repo_data[key].items():
            aggregated[key][org] += count

# Build a single figure: "All Repos" aggregate first, then per-repo subplots
all_panels = ["All Repos"] + repos
cols = 3
rows = -(-len(all_panels) // cols)  # ceiling division
fig = make_subplots(rows=rows, cols=cols, subplot_titles=all_panels)

for panel_idx, panel in enumerate(all_panels):
    row = panel_idx // cols + 1
    col = panel_idx % cols + 1

    for org in all_orgs:
        if panel == "All Repos":
            counts = [aggregated[key].get(org, 0) for key in metric_keys]
        else:
            counts = [data["repos"][panel].get(key, {}).get(org, 0) for key in metric_keys]
        fig.add_trace(
            go.Bar(
                name=org,
                x=metric_labels,
                y=counts,
                legendgroup=org,
                showlegend=(panel_idx == 0),
                visible="legendonly" if org in HIDDEN_BY_DEFAULT else True,
                hovertemplate=f"{org}: %{{y}}<extra></extra>",
                marker_color=color_map[org],
            ),
            row=row,
            col=col,
        )

fig.update_layout(
    barmode="stack",
    title=f"Anemoi Contributors (last {months} month{'s' if months != 1 else ''}) — {data['generated_at'][:10]}",
    height=300 * rows,
    legend_title="Organisation",
    updatemenus=[
        dict(
            type="buttons",
            direction="left",
            x=0.0,
            y=1.12,
            buttons=[
                dict(label="Counts", method="relayout", args=[{"barnorm": ""}]),
                dict(label="Percentage", method="relayout", args=[{"barnorm": "percent"}]),
            ],
        )
    ],
)

plot_div = fig.to_html(full_html=False, include_plotlyjs="cdn")

# === PR-type breakdown figure: subplot per panel, x = PR type, y = count, stacked by org ===
# Aggregate PR-type data across all repos
pr_types_all = set()
agg_by_type = {}  # {pr_type: {org: count}}
per_repo_by_type = {repo: data["repos"][repo].get("pull_requests_by_type", {}) for repo in repos}
for repo_by_type in per_repo_by_type.values():
    for pr_type, org_counts in repo_by_type.items():
        pr_types_all.add(pr_type)
        bucket = agg_by_type.setdefault(pr_type, Counter())
        for org, c in org_counts.items():
            bucket[org] += c

# Stable ordering: known conventional prefixes first, then any extras, "other" last
known_order = ["feat", "fix", "refactor", "perf", "test", "docs", "ci", "build", "chore", "style"]
pr_type_labels = [t for t in known_order if t in pr_types_all]
pr_type_labels += sorted(t for t in pr_types_all if t not in known_order and t != "other")
if "other" in pr_types_all:
    pr_type_labels.append("other")

fig_types = make_subplots(rows=rows, cols=cols, subplot_titles=all_panels)
for panel_idx, panel in enumerate(all_panels):
    row = panel_idx // cols + 1
    col = panel_idx % cols + 1
    for org in all_orgs:
        if panel == "All Repos":
            counts = [agg_by_type.get(t, {}).get(org, 0) for t in pr_type_labels]
        else:
            by_type = per_repo_by_type.get(panel, {})
            counts = [by_type.get(t, {}).get(org, 0) for t in pr_type_labels]
        fig_types.add_trace(
            go.Bar(
                name=org,
                x=pr_type_labels,
                y=counts,
                legendgroup=org,
                showlegend=(panel_idx == 0),
                visible="legendonly" if org in HIDDEN_BY_DEFAULT else True,
                hovertemplate=f"{org}: %{{y}}<extra></extra>",
                marker_color=color_map[org],
            ),
            row=row,
            col=col,
        )

fig_types.update_layout(
    barmode="stack",
    title=f"Merged PR Types by Organisation (last {months} month{'s' if months != 1 else ''})",
    height=300 * rows,
    legend_title="Organisation",
    updatemenus=[
        dict(
            type="buttons",
            direction="left",
            x=0.0,
            y=1.12,
            buttons=[
                dict(label="Counts", method="relayout", args=[{"barnorm": ""}]),
                dict(label="Percentage", method="relayout", args=[{"barnorm": "percent"}]),
            ],
        )
    ],
)

plot_div_types = fig_types.to_html(full_html=False, include_plotlyjs=False)

html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Anemoi Contributors</title>
  <style>
    body {{ font-family: sans-serif; max-width: 1100px; margin: 40px auto; padding: 0 20px; color: #333; }}
    h2 {{ margin-top: 2em; }}
    ul {{ line-height: 1.8; }}
  </style>
</head>
<body>
  {plot_div}

  <h2>Merged PR Types</h2>
  {plot_div_types}

  <h2>Methodology</h2>
  <p>Data collected from the GitHub API via PyGithub, covering activity in the {months * 30} days prior to {data['generated_at'][:10]}
  across the following repositories: {", ".join(repos)}.</p>
  <ul>
    <li><strong>Issues:</strong> count of issues opened, grouped by the organisation of the issue author.</li>
    <li><strong>Pull Requests:</strong> count of merged PRs where an organisation had at least one contributor.
    Contributors are identified as: the PR opener, any commit author, and any <code>Co-authored-by:</code> trailer
    entries in commit messages (resolved via GitHub's noreply address format or a manually maintained email mapping).
    Each organisation is counted at most once per PR, even if multiple members contributed.</li>
    <li><strong>Total Reviews:</strong> all review submissions on PRs created in the period, grouped by the reviewer's organisation.</li>
    <li><strong>Unique Reviews:</strong> as above, but each reviewer is counted at most once per PR.</li>
    <li><strong>Merged PR Types:</strong> the same merged PR counts above broken down by Conventional Commits prefix
    (<code>feat</code>, <code>fix</code>, <code>refactor</code>, <code>docs</code>, etc.) parsed from the PR title.
    PRs whose title does not match a known prefix are grouped as <code>other</code>.</li>
  </ul>
  <p>Organisation attribution is based on a manually maintained mapping of GitHub usernames to organisations which may not be exhaustive.</p>
</body>
</html>"""

with open("docs/index.html", "w") as f:
    f.write(html)