import json
import glob
import os
from collections import Counter
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots

# --- Shared configuration -----------------------------------------------------
metric_keys = ["issues", "pull_requests", "total_reviews", "unique_reviews"]
metric_labels = ["Issues", "PRs", "Total Reviews", "Unique Reviews"]
HIDDEN_BY_DEFAULT = {"Bots", "CodeAgents"}
KNOWN_TYPE_ORDER = ["feat", "fix", "refactor", "perf", "test", "docs", "ci", "build", "chore", "style"]


def collect_orgs(snap):
    """Return sorted list of all orgs appearing anywhere in a snapshot."""
    orgs = set()
    for repo_data in snap["repos"].values():
        for key in metric_keys:
            orgs.update(repo_data.get(key, {}).keys())
        for org_counts in repo_data.get("pull_requests_by_type", {}).values():
            orgs.update(org_counts.keys())
    return sorted(orgs)


def build_main_figure(snap, all_orgs, color_map, include_plotlyjs):
    """Stacked-bar figure with one subplot per repo plus an aggregate."""
    repos = list(snap["repos"].keys())
    months = snap.get("months", 3)

    aggregated = {key: Counter() for key in metric_keys}
    for repo_data in snap["repos"].values():
        for key in metric_keys:
            for org, count in repo_data.get(key, {}).items():
                aggregated[key][org] += count

    all_panels = ["All Repos"] + repos
    cols = 3
    rows = -(-len(all_panels) // cols)
    fig = make_subplots(rows=rows, cols=cols, subplot_titles=all_panels)

    for panel_idx, panel in enumerate(all_panels):
        row = panel_idx // cols + 1
        col = panel_idx % cols + 1
        for org in all_orgs:
            if panel == "All Repos":
                counts = [aggregated[key].get(org, 0) for key in metric_keys]
            else:
                counts = [snap["repos"][panel].get(key, {}).get(org, 0) for key in metric_keys]
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
        title=f"Anemoi Contributors (last {months} month{'s' if months != 1 else ''}) — {snap['generated_at'][:10]}",
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
    return fig.to_html(full_html=False, include_plotlyjs=include_plotlyjs), rows, cols, all_panels


def build_pr_types_figure(snap, all_orgs, color_map, rows, cols, all_panels):
    """Stacked-bar PR-type figure mirroring the main layout."""
    repos = list(snap["repos"].keys())
    months = snap.get("months", 3)

    pr_types_all = set()
    agg_by_type = {}
    per_repo_by_type = {r: snap["repos"][r].get("pull_requests_by_type", {}) for r in repos}
    for repo_by_type in per_repo_by_type.values():
        for pr_type, org_counts in repo_by_type.items():
            pr_types_all.add(pr_type)
            bucket = agg_by_type.setdefault(pr_type, Counter())
            for org, c in org_counts.items():
                bucket[org] += c

    pr_type_labels = [t for t in KNOWN_TYPE_ORDER if t in pr_types_all]
    pr_type_labels += sorted(t for t in pr_types_all if t not in KNOWN_TYPE_ORDER and t != "other")
    if "other" in pr_types_all:
        pr_type_labels.append("other")

    if not pr_type_labels:
        return ""

    fig = make_subplots(rows=rows, cols=cols, subplot_titles=all_panels)
    for panel_idx, panel in enumerate(all_panels):
        row = panel_idx // cols + 1
        col = panel_idx % cols + 1
        for org in all_orgs:
            if panel == "All Repos":
                counts = [agg_by_type.get(t, {}).get(org, 0) for t in pr_type_labels]
            else:
                by_type = per_repo_by_type.get(panel, {})
                counts = [by_type.get(t, {}).get(org, 0) for t in pr_type_labels]
            fig.add_trace(
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
    fig.update_layout(
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
    return fig.to_html(full_html=False, include_plotlyjs=False)


def render_snapshot_page(snap, output_path):
    """Render a standalone HTML page for a single historical snapshot."""
    all_orgs = collect_orgs(snap)
    colors = px.colors.qualitative.Plotly
    color_map = {org: colors[i % len(colors)] for i, org in enumerate(all_orgs)}

    plot_div, rows, cols, all_panels = build_main_figure(snap, all_orgs, color_map, include_plotlyjs="cdn")
    types_div = build_pr_types_figure(snap, all_orgs, color_map, rows, cols, all_panels)

    months = snap.get("months", 3)
    repos = list(snap["repos"].keys())
    generated = snap["generated_at"][:10]

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Anemoi Contributors — {generated}</title>
  <style>
    body {{ font-family: sans-serif; max-width: 1100px; margin: 40px auto; padding: 0 20px; color: #333; }}
    h2 {{ margin-top: 2em; }}
    ul {{ line-height: 1.8; }}
  </style>
</head>
<body>
  <p><a href="../index.html">&larr; Back to latest</a></p>
  <h1>Snapshot from {generated}</h1>

  {plot_div}

  {"<h2>Merged PR Types</h2>" + types_div if types_div else ""}

  <h2>Methodology</h2>
  <p>Data collected from the GitHub API via PyGithub, covering activity in the {months * 30} days prior to {generated}
  across the following repositories: {", ".join(repos)}.</p>
</body>
</html>"""

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w") as f:
        f.write(html)


# --- Load the latest snapshot ------------------------------------------------
with open("results.json") as f:
    data = json.load(f)

months = data.get("months", 3)
repos = list(data["repos"].keys())
all_orgs = collect_orgs(data)

colors = px.colors.qualitative.Plotly
color_map = {org: colors[i % len(colors)] for i, org in enumerate(all_orgs)}

# Build main + PR-types figures for the latest snapshot
plot_div, rows, cols, all_panels = build_main_figure(data, all_orgs, color_map, include_plotlyjs="cdn")
plot_div_types = build_pr_types_figure(data, all_orgs, color_map, rows, cols, all_panels)

# --- Time-series trend figure + per-snapshot pages ---------------------------
snapshot_files = sorted(glob.glob("history/results-*.json"))
trend_div = ""
history_entries = []
if snapshot_files:
    snapshots = []
    for path in snapshot_files:
        with open(path) as f:
            snap = json.load(f)
        date = os.path.basename(path).replace("results-", "").replace(".json", "")
        snapshots.append((date, snap))
        history_entries.append(date)

    # Aggregate each metric across all repos within each snapshot
    trend_dates = [d for d, _ in snapshots]
    trend_orgs = sorted({org for _, snap in snapshots for org in collect_orgs(snap)})
    trend = {key: {org: [] for org in trend_orgs} for key in metric_keys}
    for _, snap in snapshots:
        for key in metric_keys:
            totals = Counter()
            for repo_data in snap.get("repos", {}).values():
                for org, count in repo_data.get(key, {}).items():
                    totals[org] += count
            for org in trend_orgs:
                trend[key][org].append(totals.get(org, 0))

    fig_trend = make_subplots(rows=2, cols=2, subplot_titles=metric_labels)
    trend_color_map = {org: colors[i % len(colors)] for i, org in enumerate(trend_orgs)}
    for metric_idx, key in enumerate(metric_keys):
        row = metric_idx // 2 + 1
        col = metric_idx % 2 + 1
        for org in trend_orgs:
            fig_trend.add_trace(
                go.Scatter(
                    name=org,
                    x=trend_dates,
                    y=trend[key][org],
                    mode="lines+markers",
                    legendgroup=org,
                    showlegend=(metric_idx == 0),
                    visible="legendonly" if org in HIDDEN_BY_DEFAULT else True,
                    line=dict(color=trend_color_map[org]),
                    hovertemplate=f"{org}: %{{y}} on %{{x}}<extra></extra>",
                ),
                row=row,
                col=col,
            )
    fig_trend.update_layout(
        title=f"Trends across snapshots ({len(snapshots)} runs, each covering a {months}-month window)",
        height=700,
        legend_title="Organisation",
    )
    trend_div = fig_trend.to_html(full_html=False, include_plotlyjs=False)

    # Write a standalone page for each historical snapshot
    for date, snap in snapshots:
        render_snapshot_page(snap, f"docs/history/{date}.html")

# --- History nav block for the main page (newest first) ----------------------
if history_entries:
    items = "\n    ".join(
        f'<li><a href="history/{d}.html">{d}</a></li>' for d in reversed(history_entries)
    )
    history_nav = f"""
  <h2>Browse History</h2>
  <p>Each archived snapshot can be opened on its own page:</p>
  <ul>
    {items}
  </ul>
"""
else:
    history_nav = ""

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

  {"<h2>Merged PR Types</h2>" + plot_div_types if plot_div_types else ""}

  {"<h2>Trends Over Time</h2>" + trend_div if trend_div else ""}

  {history_nav}

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
    <li><strong>Trends Over Time:</strong> each run of the data-collection script archives a dated snapshot under
    <code>history/</code>. The trend figure plots the per-org totals (summed across all repositories) of each metric
    against the snapshot date, so you can see how contribution patterns evolve. Note that each snapshot is a rolling
    {months}-month window, so adjacent points share overlapping data.</li>
    <li><strong>Browse History:</strong> each archived snapshot is also rendered as a standalone page showing the data
    as it was at that point in time.</li>
  </ul>
  <p>Organisation attribution is based on a manually maintained mapping of GitHub usernames to organisations which may not be exhaustive.</p>
</body>
</html>"""

with open("docs/index.html", "w") as f:
    f.write(html)
