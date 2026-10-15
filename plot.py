import json
import glob
import os
from collections import Counter, defaultdict
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots

# --- Shared configuration -----------------------------------------------------
metric_keys = ["issues", "pull_requests", "prs_reviewed"]
metric_labels = ["Issues", "PRs", "PRs Reviewed"]

with open("orgs.json") as _f:
    _org_display = json.load(_f)
# Orgs excluded from charts entirely (still present in results.json for anyone who wants them).
EXCLUDED_ORGS = set(_org_display.get("exclude", []))
# Orgs that keep their own colour and legend entry however small their contribution.
ALWAYS_SHOW = set(_org_display.get("always_show", []))
OTHER_LABEL = "Other organisations"
OTHER_COLOR = "#9aa5b1"
KNOWN_TYPE_ORDER = ["feat", "fix", "refactor", "perf", "test", "docs", "ci", "build", "chore", "style"]
# Orgs need at least this many merged PRs before their median turnaround is plotted.
MIN_PRS_FOR_LATENCY = 3
# One palette, assigned by sorted org name, so an org keeps its colour across
# every figure and every archived page.
PALETTE = px.colors.qualitative.Dark24 + px.colors.qualitative.Light24

REVIEW_STATE_ORDER = ["APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED"]
REVIEW_STATE_COLORS = {
    "APPROVED": "#2a9d4a",
    "CHANGES_REQUESTED": "#d1495b",
    "COMMENTED": "#8d99ae",
    "DISMISSED": "#c9c9c9",
}
# GitHub overwrites a review's state when it is dismissed, so DISMISSED is not a
# kind of review anyone gives: it is an approval or change-request that a later
# push (or a manual dismissal) invalidated. "Superseded" says that plainly.
REVIEW_STATE_LABELS = {
    "APPROVED": "Approved",
    "CHANGES_REQUESTED": "Changes requested",
    "COMMENTED": "Commented",
    "DISMISSED": "Superseded",
}

PCT_BUTTONS = [
    dict(
        type="buttons",
        direction="left",
        # Right-anchored so the buttons never sit on top of the centred title.
        x=1.0,
        xanchor="right",
        y=1.08,
        buttons=[
            dict(label="Counts", method="relayout", args=[{"barnorm": ""}]),
            dict(label="Percentage", method="relayout", args=[{"barnorm": "percent"}]),
        ],
    )
]


def fold_org(org):
    """Display name for an org: itself if it earns a legend entry, else the shared bucket."""
    return org if org in ALWAYS_SHOW else OTHER_LABEL


def fold_counts(counts):
    """Collapse an {org: number} mapping onto display names, dropping excluded orgs."""
    folded = Counter()
    for org, value in counts.items():
        if org in EXCLUDED_ORGS:
            continue
        folded[fold_org(org)] += value
    return folded


def collect_orgs(snap):
    """Return sorted display names appearing anywhere in a snapshot, minus excluded orgs."""
    orgs = set()
    for repo_data in snap["repos"].values():
        for key in metric_keys:
            orgs.update(repo_data.get(key, {}).keys())
        for org_counts in repo_data.get("pull_requests_by_type", {}).values():
            orgs.update(org_counts.keys())
    return sorted({fold_org(o) for o in orgs - EXCLUDED_ORGS})


def tail_orgs(agg):
    """The real organisation names hidden behind the shared bucket, with their totals."""
    totals = Counter()
    for key in metric_keys:
        for org, value in agg.get(key, {}).items():
            if org not in EXCLUDED_ORGS and org not in ALWAYS_SHOW:
                totals[org] += value
    return totals


def build_color_map(orgs):
    """Stable org -> colour assignment, shared by every figure on every page."""
    colors = {org: PALETTE[i % len(PALETTE)] for i, org in enumerate(sorted(orgs))}
    colors[OTHER_LABEL] = OTHER_COLOR
    return colors


def aggregate_section(snap):
    """Cross-repo totals: the exact aggregate when present, else summed per-repo counters.

    Snapshots archived before the aggregate existed only support the original
    count metrics; distinct-people metrics cannot be recovered by summing.
    """
    if "aggregate" in snap:
        return snap["aggregate"]
    agg = {key: Counter() for key in metric_keys}
    for repo_data in snap["repos"].values():
        for key in metric_keys:
            agg[key].update(repo_data.get(key, {}))
    return {key: dict(counter) for key, counter in agg.items()}


def ranked_orgs(mapping, limit=None):
    """Orgs present in `mapping`, excluded ones dropped, ordered by value descending."""
    items = [(org, value) for org, value in mapping.items() if org not in EXCLUDED_ORGS]
    items.sort(key=lambda kv: (-kv[1], kv[0]))
    if limit:
        items = items[:limit]
    return [org for org, _ in items]


def build_main_figure(snap, all_orgs, color_map, include_plotlyjs):
    """Stacked-bar figure with one subplot per repo plus an aggregate."""
    repos = list(snap["repos"].keys())
    months = snap.get("months", 3)

    per_repo = {r: {key: fold_counts(snap["repos"][r].get(key, {})) for key in metric_keys}
                for r in repos}
    aggregated = {key: Counter() for key in metric_keys}
    for repo_counts in per_repo.values():
        for key in metric_keys:
            aggregated[key].update(repo_counts[key])

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
                counts = [per_repo[panel][key].get(org, 0) for key in metric_keys]
            fig.add_trace(
                go.Bar(
                    name=org,
                    x=metric_labels,
                    y=counts,
                    legendgroup=org,
                    showlegend=(panel_idx == 0),
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
        updatemenus=PCT_BUTTONS,
    )
    return fig.to_html(full_html=False, include_plotlyjs=include_plotlyjs), rows, cols, all_panels


def build_pr_types_figure(snap, all_orgs, color_map, rows, cols, all_panels):
    """Stacked-bar PR-type figure mirroring the main layout."""
    repos = list(snap["repos"].keys())
    months = snap.get("months", 3)

    pr_types_all = set()
    agg_by_type = {}
    per_repo_by_type = {
        r: {t: fold_counts(c) for t, c in snap["repos"][r].get("pull_requests_by_type", {}).items()}
        for r in repos
    }
    for repo_by_type in per_repo_by_type.values():
        for pr_type, org_counts in repo_by_type.items():
            pr_types_all.add(pr_type)
            agg_by_type.setdefault(pr_type, Counter()).update(org_counts)

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
        updatemenus=PCT_BUTTONS,
    )
    return fig.to_html(full_html=False, include_plotlyjs=False)


def build_people_figure(agg, months):
    """How many distinct people each org puts on the project, and how many are new."""
    contributors = fold_counts(agg.get("contributors", {}))
    if not contributors:
        return ""
    new_contributors = fold_counts(agg.get("new_contributors", {}))
    reviewers = fold_counts(agg.get("reviewers", {}))

    orgs = ranked_orgs(contributors)
    if not orgs:
        return ""
    returning = [contributors[o] - new_contributors.get(o, 0) for o in orgs]
    first_time = [new_contributors.get(o, 0) for o in orgs]

    reviewer_orgs = ranked_orgs(reviewers)

    fig = make_subplots(
        rows=1, cols=2,
        subplot_titles=("People authoring merged PRs", "People reviewing PRs"),
    )
    fig.add_trace(
        go.Bar(name="Returning", x=orgs, y=returning, marker_color="#3b6ea5",
               hovertemplate="%{x}: %{y} returning<extra></extra>"),
        row=1, col=1,
    )
    fig.add_trace(
        go.Bar(name="First-time", x=orgs, y=first_time, marker_color="#f4a261",
               hovertemplate="%{x}: %{y} first-time<extra></extra>"),
        row=1, col=1,
    )
    fig.add_trace(
        go.Bar(name="Reviewers", x=reviewer_orgs,
               y=[reviewers[o] for o in reviewer_orgs], marker_color="#2a9d8f",
               showlegend=True, hovertemplate="%{x}: %{y} reviewers<extra></extra>"),
        row=1, col=2,
    )
    fig.update_layout(
        barmode="stack",
        title=f"People per organisation (last {months} month{'s' if months != 1 else ''}, deduplicated across repos)",
        height=450,
        legend_title="",
    )
    fig.update_yaxes(title_text="distinct people", row=1, col=1)
    return fig.to_html(full_html=False, include_plotlyjs=False)


def build_review_matrix_figure(agg, months):
    """Who reviews whose code, normalised by row (effort given) or column (review received)."""
    raw_matrix = agg.get("review_matrix")
    if not raw_matrix:
        return ""

    # Fold both axes. A PR reviewed by two different folded orgs counts twice in
    # the bucket's row; with one-PR organisations that is vanishingly rare.
    matrix = defaultdict(Counter)
    for reviewer, targets in raw_matrix.items():
        if reviewer in EXCLUDED_ORGS:
            continue
        matrix[fold_org(reviewer)].update(fold_counts(targets))

    reviewer_totals = {rev: sum(t.values()) for rev, t in matrix.items()}
    reviewers = ranked_orgs(reviewer_totals)
    # Only count authors reviewed by orgs that survive the exclusion filter,
    # otherwise bot-only review targets appear as all-zero columns.
    author_totals = Counter()
    for rev in reviewers:
        author_totals.update(matrix[rev])
    authors = ranked_orgs(author_totals)
    if not reviewers or not authors:
        return ""

    counts = [[matrix.get(rev, {}).get(author, 0) for author in authors] for rev in reviewers]
    text = [[str(v) for v in row] for row in counts]
    row_totals = [sum(row) or 1 for row in counts]
    col_totals = [sum(row[j] for row in counts) or 1 for j in range(len(authors))]

    z_row = [[100 * v / row_totals[i] for v in row] for i, row in enumerate(counts)]
    z_col = [[100 * v / col_totals[j] for j, v in enumerate(row)] for row in counts]

    base_title = f"Who reviews whom (last {months} month{'s' if months != 1 else ''}) — cell = PRs reviewed, colour = "
    hover_row = "%{y} reviewed %{x}: %{text} PRs (%{z:.0f}% of %{y}'s reviewing)<extra></extra>"
    hover_col = "%{y} reviewed %{x}: %{text} PRs (%{z:.0f}% of the review %{x} received)<extra></extra>"

    fig = go.Figure(
        go.Heatmap(
            z=z_col, x=authors, y=reviewers, text=text, texttemplate="%{text}",
            colorscale="Blues", zmin=0, zmax=100,
            colorbar=dict(title="% of<br>review<br>received"),
            hovertemplate=hover_col,
        )
    )
    fig.update_layout(
        title=base_title + "share of the review that org received",
        xaxis_title="Organisation whose PR was reviewed",
        yaxis_title="Reviewing organisation",
        height=170 + 34 * len(reviewers),
        yaxis=dict(autorange="reversed"),
        updatemenus=[
            dict(
                type="buttons",
                direction="left",
                x=1.0,
                xanchor="right",
                y=1.10,
                buttons=[
                    dict(
                        label="Normalise by column",
                        method="update",
                        args=[
                            {"z": [z_col], "hovertemplate": hover_col,
                             "colorbar.title.text": "% of<br>review<br>received"},
                            {"title.text": base_title + "share of the review that org received"},
                        ],
                    ),
                    dict(
                        label="Normalise by row",
                        method="update",
                        args=[
                            {"z": [z_row], "hovertemplate": hover_row,
                             "colorbar.title.text": "% of<br>reviewer's<br>effort"},
                            {"title.text": base_title + "share of that reviewer's effort"},
                        ],
                    ),
                ],
            )
        ],
    )
    return fig.to_html(full_html=False, include_plotlyjs=False)


def build_review_outcomes_figure(agg, months):
    """Approvals vs change requests vs plain comments, per reviewing org."""
    raw_states = agg.get("review_states")
    if not raw_states:
        return ""
    states = defaultdict(Counter)
    for org, counts in raw_states.items():
        if org not in EXCLUDED_ORGS:
            states[fold_org(org)].update(counts)
    totals = {org: sum(counts.values()) for org, counts in states.items()}
    orgs = ranked_orgs(totals)
    if not orgs:
        return ""

    seen_states = [s for s in REVIEW_STATE_ORDER if any(states[o].get(s) for o in orgs)]
    fig = go.Figure()
    for state in seen_states:
        label = REVIEW_STATE_LABELS.get(state, state.replace("_", " ").title())
        fig.add_trace(
            go.Bar(
                name=label,
                x=orgs,
                y=[states[o].get(state, 0) for o in orgs],
                marker_color=REVIEW_STATE_COLORS.get(state),
                hovertemplate=f"{label} by %{{x}}: %{{y}}<extra></extra>",
            )
        )
    fig.update_layout(
        barmode="stack",
        title=(f"Review outcomes by reviewing organisation "
               f"(last {months} month{'s' if months != 1 else ''}, self-reviews excluded)"),
        height=450,
        xaxis_title="Reviewing organisation",
        yaxis_title="review submissions",
        updatemenus=PCT_BUTTONS,
    )
    return fig.to_html(full_html=False, include_plotlyjs=False)


def build_latency_figure(agg, months):
    """What contributors get back: how long their PRs wait."""
    latency = agg.get("latency")
    if not latency:
        return ""
    eligible = {
        org: entry for org, entry in latency.items()
        if org not in EXCLUDED_ORGS and entry.get("merged_prs", 0) >= MIN_PRS_FOR_LATENCY
    }
    if not eligible:
        return ""

    orgs = sorted(eligible, key=lambda o: eligible[o].get("median_hours_to_merge") or 0)
    labels = [f"{o} (n={eligible[o]['merged_prs']})" for o in orgs]

    fig = go.Figure()
    fig.add_trace(
        go.Bar(
            name="Median days to first review", y=labels, orientation="h",
            x=[(eligible[o].get("median_hours_to_first_review") or 0) / 24 for o in orgs],
            marker_color="#2a9d8f",
            hovertemplate="%{y}<br>first review after %{x:.1f} days<extra></extra>",
        )
    )
    fig.add_trace(
        go.Bar(
            name="Median days to merge", y=labels, orientation="h",
            x=[(eligible[o].get("median_hours_to_merge") or 0) / 24 for o in orgs],
            marker_color="#3b6ea5",
            hovertemplate="%{y}<br>merged after %{x:.1f} days<extra></extra>",
        )
    )
    fig.update_layout(
        barmode="group",
        title=(f"Turnaround on merged PRs (last {months} month{'s' if months != 1 else ''}) — "
               f"organisations with at least {MIN_PRS_FOR_LATENCY} merged PRs"),
        xaxis_title="days (median)",
        height=140 + 40 * len(orgs),
        legend=dict(orientation="h", y=1.08),
    )
    return fig.to_html(full_html=False, include_plotlyjs=False)


def build_size_figure(agg, months):
    """Volume of code changed, next to typical PR size so totals aren't the whole story."""
    raw_sizes = agg.get("pr_size")
    if not raw_sizes:
        return ""
    # Sums fold cleanly; the median is recomputed as the median of the folded
    # organisations' medians, which is close enough for a typical-size read.
    sizes = defaultdict(Counter)
    medians = defaultdict(list)
    for org, data in raw_sizes.items():
        if org in EXCLUDED_ORGS:
            continue
        display = fold_org(org)
        for field in ("additions", "deletions", "changed_files"):
            sizes[display][field] += data.get(field, 0)
        medians[display].append(data.get("median_additions", 0))
    for display, values in medians.items():
        sizes[display]["median_additions"] = sorted(values)[len(values) // 2]

    totals = {org: data["additions"] + data["deletions"] for org, data in sizes.items()}
    orgs = ranked_orgs(totals)
    if not orgs:
        return ""

    fig = make_subplots(
        rows=1, cols=2,
        subplot_titles=("Total lines changed (log scale)", "Typical PR size (median lines added)"),
    )
    fig.add_trace(
        go.Bar(name="Lines added", x=orgs, y=[sizes[o].get("additions", 0) for o in orgs],
               marker_color="#2a9d4a", hovertemplate="%{x}: +%{y}<extra></extra>"),
        row=1, col=1,
    )
    fig.add_trace(
        go.Bar(name="Lines removed", x=orgs, y=[sizes[o].get("deletions", 0) for o in orgs],
               marker_color="#d1495b", hovertemplate="%{x}: -%{y}<extra></extra>"),
        row=1, col=1,
    )
    fig.add_trace(
        go.Bar(name="Median lines added", x=orgs, y=[sizes[o].get("median_additions", 0) for o in orgs],
               marker_color="#3b6ea5", hovertemplate="%{x}: %{y} lines (median PR)<extra></extra>"),
        row=1, col=2,
    )
    fig.update_yaxes(type="log", row=1, col=1)
    fig.update_layout(
        barmode="group",
        title=f"Code volume by organisation (last {months} month{'s' if months != 1 else ''})",
        height=450,
    )
    return fig.to_html(full_html=False, include_plotlyjs=False)


def build_smaller_contributors_table(agg):
    """Name every organisation folded into the shared bucket, so nothing is lost."""
    totals = tail_orgs(agg)
    if not totals:
        return ""
    rows = ""
    for org, _ in sorted(totals.items(), key=lambda kv: (-kv[1], kv[0])):
        people = agg.get("contributors", {}).get(org, 0)
        rows += (f"<tr><td>{org}</td>"
                 f"<td>{agg.get('pull_requests', {}).get(org, 0)}</td>"
                 f"<td>{agg.get('issues', {}).get(org, 0)}</td>"
                 f"<td>{agg.get('prs_reviewed', {}).get(org, 0)}</td>"
                 f"<td>{people}</td></tr>\n    ")
    return f"""
  <table class="tail">
    <thead><tr><th>Organisation</th><th>PRs</th><th>Issues</th><th>PRs reviewed</th><th>People</th></tr></thead>
    <tbody>
    {rows}</tbody>
  </table>"""


def headline_stats(agg):
    """Small summary block: breadth, cross-org review share, attribution coverage."""
    contributors = {o: n for o, n in agg.get("contributors", {}).items() if o not in EXCLUDED_ORGS}
    stats = []
    if contributors:
        stats.append((sum(contributors.values()), "people contributed code"))
        stats.append((len(contributors), "organisations contributed code"))
        new_total = sum(n for o, n in agg.get("new_contributors", {}).items() if o not in EXCLUDED_ORGS)
        stats.append((new_total, "made their first contribution"))

    matrix = agg.get("review_matrix", {})
    cross = same = 0
    for reviewer, targets in matrix.items():
        if reviewer in EXCLUDED_ORGS:
            continue
        for author, count in targets.items():
            if author in EXCLUDED_ORGS:
                continue
            if author == reviewer:
                same += count
            else:
                cross += count
    if cross + same:
        stats.append((f"{100 * cross / (cross + same):.0f}%", "of reviews crossed organisations"))

    attribution = agg.get("attribution", {})
    if attribution.get("total_credits"):
        known = 100 * (1 - attribution["unknown_credits"] / attribution["total_credits"])
        stats.append((f"{known:.0f}%", "of activity attributed to a known organisation"))

    if not stats:
        return ""
    cards = "\n    ".join(
        f'<div class="stat"><strong>{value}</strong><span>{label}</span></div>'
        for value, label in stats
    )
    return f'<div class="stats">\n    {cards}\n  </div>'


PAGE_STYLE = """
    body { font-family: sans-serif; max-width: 1100px; margin: 40px auto; padding: 0 20px; color: #333; }
    h2 { margin-top: 2em; }
    ul { line-height: 1.8; }
    p.lead { color: #555; margin-top: -0.5em; }
    details summary { cursor: pointer; color: #1d3557; }
    table.tail { border-collapse: collapse; margin-top: 8px; font-size: 0.92em; }
    table.tail th, table.tail td { padding: 5px 14px 5px 0; text-align: left; border-bottom: 1px solid #e3e6ea; }
    table.tail td:not(:first-child), table.tail th:not(:first-child) { text-align: right; }
    .stats { display: flex; flex-wrap: wrap; gap: 16px; margin: 24px 0 8px; }
    .stat { flex: 1 1 150px; background: #f5f7fa; border-radius: 8px; padding: 14px 16px; }
    .stat strong { display: block; font-size: 1.7em; line-height: 1.2; color: #1d3557; }
    .stat span { font-size: 0.85em; color: #555; }
"""


def build_extra_sections(agg, months):
    """The people / collaboration / turnaround / volume sections, skipped when absent."""
    sections = [
        ("Who is contributing",
         "Headcount rather than event counts: a six-person organisation and a one-person "
         "organisation can file the same number of PRs.",
         build_people_figure(agg, months)),
        ("Who reviews whom",
         "Review is where organisations actually meet. Cells on the diagonal are organisations "
         "reviewing their own PRs; everything off it is cross-organisation effort. Cells always show "
         "the PR count — the switch changes what the colour means: by column (the default), where the "
         "review an organisation receives comes from; by row, where each organisation directs its own "
         "reviewing.",
         build_review_matrix_figure(agg, months)),
        ("Review outcomes",
         "Whether reviews approve, request changes, or just comment — counted against the organisation "
         "<em>giving</em> the review, not the one whose PR it was. <em>Superseded</em> is an approval or "
         "change request that a later push invalidated, so the approval count is a floor rather than an "
         "exact total.",
         build_review_outcomes_figure(agg, months)),
        ("What contributors get back",
         "How long a PR waits for its first review and for merge, by the organisation that opened it.",
         build_latency_figure(agg, months)),
        ("Code volume",
         "Totals are dominated by large generated or vendored changes, so the median PR size "
         "sits alongside them.",
         build_size_figure(agg, months)),
        ("Smaller contributors",
         "Organisations grouped into <em>Other organisations</em> in the charts above, so that a long "
         "tail of one-off contributors does not crowd out the partners. Their numbers are listed in "
         "full here and kept separately in <code>results.json</code>.",
         build_smaller_contributors_table(agg)),
    ]
    html = ""
    for title, blurb, div in sections:
        if div:
            html += f'\n  <h2>{title}</h2>\n  <p class="lead">{blurb}</p>\n  {div}\n'
    return html


def render_snapshot_page(snap, color_map, generated, output_path):
    """Render a standalone HTML page for a single historical snapshot.

    `generated` comes from the filename rather than the payload: snapshots
    archived by older versions recorded the backfill's run time instead.
    """
    all_orgs = collect_orgs(snap)
    plot_div, rows, cols, all_panels = build_main_figure(snap, all_orgs, color_map, include_plotlyjs="cdn")
    types_div = build_pr_types_figure(snap, all_orgs, color_map, rows, cols, all_panels)

    months = snap.get("months", 3)
    repos = list(snap["repos"].keys())
    agg = aggregate_section(snap)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Anemoi Contributors — {generated}</title>
  <style>{PAGE_STYLE}</style>
</head>
<body>
  <p><a href="../index.html">&larr; Back to latest</a></p>
  <h1>Snapshot from {generated}</h1>

  {headline_stats(agg)}

  {plot_div}

  {"<h2>Merged PR Types</h2>" + types_div if types_div else ""}
  {build_extra_sections(agg, months)}

  <h2>Methodology</h2>
  <p>Data collected from the GitHub API via PyGithub, covering activity in the {months} months prior to {generated}
  across the following repositories: {", ".join(repos)}.</p>
  <p>Bot accounts and automated code agents are excluded from all charts on this page (they remain in the archived JSON).</p>
</body>
</html>"""

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w") as f:
        f.write(html)


def build_trend_figures(snapshots, color_map):
    """Volume trends (all snapshots) and community-health trends (newer snapshots only)."""
    trend_dates = [d for d, _ in snapshots]
    trend_orgs = sorted({org for _, snap in snapshots for org in collect_orgs(snap)})

    trend = {key: {org: [] for org in trend_orgs} for key in metric_keys}
    for _, snap in snapshots:
        for key in metric_keys:
            totals = Counter()
            for repo_data in snap.get("repos", {}).values():
                totals.update(fold_counts(repo_data.get(key, {})))
            for org in trend_orgs:
                trend[key][org].append(totals.get(org, 0))

    fig = make_subplots(rows=1, cols=len(metric_keys), subplot_titles=metric_labels)
    for metric_idx, key in enumerate(metric_keys):
        for org in trend_orgs:
            fig.add_trace(
                go.Scatter(
                    name=org, x=trend_dates, y=trend[key][org], mode="lines+markers",
                    legendgroup=org, showlegend=(metric_idx == 0),
                    line=dict(color=color_map[org]),
                    hovertemplate=f"{org}: %{{y}} on %{{x}}<extra></extra>",
                ),
                row=1, col=metric_idx + 1,
            )
    months = snapshots[-1][1].get("months", 3)
    fig.update_layout(
        title=(f"Trends across snapshots ({len(snapshots)} runs) — "
               f"each point is a rolling {months}-month window ending on that date, so adjacent points overlap"),
        height=450,
        legend_title="Organisation",
    )
    volume_div = fig.to_html(full_html=False, include_plotlyjs=False)

    # --- Community health: only snapshots carrying the newer aggregate block ---
    health = [(d, s["aggregate"]) for d, s in snapshots
              if "aggregate" in s and s["aggregate"].get("contributors")]
    if len(health) < 2:
        return volume_div, ""

    health_dates = [d for d, _ in health]
    # Named orgs only: a pooled line for the shared bucket would mix unrelated
    # organisations, and neither a cross-org share nor a median can be pooled.
    health_orgs = sorted({o for _, a in health for o in a.get("contributors", {})} & ALWAYS_SHOW)
    panels = ["Distinct contributors", "First-time contributors",
              "Share of reviewing done outside own org", "Median days to merge"]
    fig2 = make_subplots(rows=2, cols=2, subplot_titles=panels)

    for org in health_orgs:
        series = {p: [] for p in panels}
        for _, agg in health:
            series[panels[0]].append(agg.get("contributors", {}).get(org, 0))
            series[panels[1]].append(agg.get("new_contributors", {}).get(org, 0))

            targets = {a: c for a, c in agg.get("review_matrix", {}).get(org, {}).items()
                       if a not in EXCLUDED_ORGS}
            total = sum(targets.values())
            series[panels[2]].append(100 * (total - targets.get(org, 0)) / total if total else None)

            entry = agg.get("latency", {}).get(org, {})
            hours = entry.get("median_hours_to_merge")
            thin = entry.get("merged_prs", 0) < MIN_PRS_FOR_LATENCY
            series[panels[3]].append(None if (hours is None or thin) else hours / 24)

        for idx, panel in enumerate(panels):
            fig2.add_trace(
                go.Scatter(
                    name=org, x=health_dates, y=series[panel], mode="lines+markers",
                    legendgroup=org, showlegend=(idx == 0),
                    line=dict(color=color_map[org]), connectgaps=False,
                    hovertemplate=f"{org}: %{{y:.1f}} on %{{x}}<extra></extra>",
                ),
                row=idx // 2 + 1, col=idx % 2 + 1,
            )
    fig2.update_yaxes(title_text="people", row=1, col=1)
    fig2.update_yaxes(title_text="people", row=1, col=2)
    fig2.update_yaxes(title_text="% cross-org", range=[0, 100], row=2, col=1)
    fig2.update_yaxes(title_text="days", row=2, col=2)
    fig2.update_layout(
        title="Community health over time — breadth of participation and how work is shared",
        height=700,
        legend_title="Organisation",
    )
    return volume_div, fig2.to_html(full_html=False, include_plotlyjs=False)


# --- Load the latest snapshot ------------------------------------------------
with open("results.json") as f:
    data = json.load(f)

months = data.get("months", 3)
repos = list(data["repos"].keys())
all_orgs = collect_orgs(data)

snapshot_files = sorted(glob.glob("history/results-*.json"))
snapshots = []
for path in snapshot_files:
    with open(path) as f:
        snap = json.load(f)
    snapshots.append((os.path.basename(path).replace("results-", "").replace(".json", ""), snap))

# One colour map for the whole site, built from every org seen in any snapshot.
site_orgs = set(all_orgs)
for _, snap in snapshots:
    site_orgs.update(collect_orgs(snap))
color_map = build_color_map(site_orgs)

# --- Figures for the latest snapshot -----------------------------------------
plot_div, rows, cols, all_panels = build_main_figure(data, all_orgs, color_map, include_plotlyjs="cdn")
plot_div_types = build_pr_types_figure(data, all_orgs, color_map, rows, cols, all_panels)
latest_agg = aggregate_section(data)
extra_sections = build_extra_sections(latest_agg, months)

# --- Trends + per-snapshot pages ---------------------------------------------
trend_div = health_div = ""
history_entries = [date for date, _ in snapshots]
if snapshots:
    trend_div, health_div = build_trend_figures(snapshots, color_map)
    for date, snap in snapshots:
        render_snapshot_page(snap, color_map, date, f"docs/history/{date}.html")

# --- History nav block for the main page (newest first) ----------------------
if history_entries:
    items = "\n    ".join(
        f'<li><a href="history/{d}.html">{d}</a></li>' for d in reversed(history_entries)
    )
    history_nav = f"""
  <h2>Browse History</h2>
  <details>
    <summary>{len(history_entries)} archived snapshots — each opens on its own page</summary>
    <ul>
    {items}
    </ul>
  </details>
"""
else:
    history_nav = ""

html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Anemoi Contributors</title>
  <style>{PAGE_STYLE}</style>
</head>
<body>
  <h1>Anemoi Contributors</h1>
  <p class="lead">How organisations build Anemoi together — activity in the last {months} month{'s' if months != 1 else ''},
  as of {data['generated_at'][:10]}.</p>

  {headline_stats(latest_agg)}

  {plot_div}

  {"<h2>Merged PR Types</h2>" + plot_div_types if plot_div_types else ""}
  {extra_sections}

  {"<h2>Trends Over Time</h2>" + trend_div if trend_div else ""}
  {health_div}

  {history_nav}

  <h2>Methodology</h2>
  <p>Data collected from the GitHub API via PyGithub, covering activity in the {months} months prior to {data['generated_at'][:10]}
  across the following repositories: {", ".join(repos)}.</p>
  <p>Bot accounts and automated code agents are excluded from all charts on this page (they remain in <code>results.json</code>).</p>
  <ul>
    <li><strong>Which organisations get their own series:</strong> <code>orgs.json</code> lists the organisations that
    keep their own colour and legend entry, however small their contribution — a partner with a single PR stays
    visible, because volume is a poor proxy for how much a contribution matters here. Every other organisation is
    folded into <em>Other organisations</em> for charting only, and named with its numbers under Smaller contributors.
    The underlying data is never folded. The turnaround chart is the one exception: medians cannot be pooled, so it
    shows organisations individually and relies on its own minimum-PR filter instead.</li>
    <li><strong>Issues:</strong> count of issues opened, grouped by the organisation of the issue author.</li>
    <li><strong>Pull Requests:</strong> count of merged PRs where an organisation had at least one contributor.
    Contributors are identified as: the PR opener, any commit author, and any <code>Co-authored-by:</code> trailer
    entries in commit messages (resolved via GitHub's noreply address format or a manually maintained email mapping).
    Each organisation is counted at most once per PR, even if multiple members contributed.</li>
    <li><strong>PRs Reviewed:</strong> PRs an organisation reviewed, counted once per organisation per PR regardless
    of how many of its people reviewed or how many comments they left, and attributed to the period in which the
    <em>review</em> was submitted rather than the period the PR was opened. Authors reviewing their own PRs are not
    counted. This replaces the earlier Total Reviews and Unique Reviews: GitHub files every batch of inline comments
    as its own review record, so two thirds of those counts were comment batches and a further 30% were self-reviews,
    which meant the figures tracked commenting style more than review effort.</li>
    <li><strong>Merged PR Types:</strong> the same merged PR counts above broken down by Conventional Commits prefix
    (<code>feat</code>, <code>fix</code>, <code>refactor</code>, <code>docs</code>, etc.) parsed from the PR title.
    PRs whose title does not match a known prefix are grouped as <code>other</code>.</li>
    <li><strong>Who is contributing:</strong> distinct people, not events. Someone active in several repositories is
    counted once. A contributor is anyone credited on a merged PR; a co-author identified only by email address counts
    as one person via a salted hash of that address. <em>First-time</em> means their earliest merged PR anywhere in
    Anemoi falls inside this window.</li>
    <li><strong>Who reviews whom:</strong> the same reviews as PRs Reviewed, split by the organisation of the PR's
    opener, so each cell is a count of distinct PRs. Raw counts differ by orders of magnitude between organisations,
    so the colour is normalised rather than absolute: <em>by column</em> (the default) each reviewed organisation's
    cells sum to 100%, showing where the review it receives comes from; <em>by row</em> each reviewing organisation's
    cells sum to 100%, showing where it directs its effort.</li>
    <li><strong>Review outcomes:</strong> every review submitted in the period by state — the one place submissions
    are counted individually rather than once per PR, since the question is what verdicts were given — grouped by the
    organisation of the reviewer who submitted it rather than the organisation whose PR was under review;
    self-reviews excluded.
    GitHub records a plain comment as <code>COMMENTED</code>, so it is the most common state by far.
    <em>Superseded</em> is GitHub's <code>DISMISSED</code> state: when a review is dismissed — most often
    automatically, by branch protection retiring stale approvals as soon as new commits are pushed — GitHub
    overwrites the state on the original review, so whether it was an approval or a change request is no longer
    recoverable from the API. Those reviews therefore appear here rather than under Approved, which makes the
    approval count a lower bound. In the current data the same reviewer goes on to approve the same PR again in
    roughly 40% of superseded reviews, which is the signature of the approve, push, re-approve cycle.</li>
    <li><strong>What contributors get back:</strong> medians over PRs <em>merged</em> in the window — measuring merged
    rather than opened PRs means every PR counted has a complete, observed lifetime. Attributed to the opener's
    organisation. Only organisations with at least {MIN_PRS_FOR_LATENCY} merged PRs are shown, as medians over one or
    two PRs are noise.</li>
    <li><strong>Code volume:</strong> additions and deletions on merged PRs. A PR credited to several organisations
    contributes its full size to each, matching how PR counts are attributed. Totals are easily dominated by generated
    or vendored files, which is why the median PR size is shown next to them.</li>
    <li><strong>Trends Over Time:</strong> each run of the data-collection script archives a dated snapshot under
    <code>history/</code>. The trend figure plots the per-org totals (summed across all repositories) of each metric
    against the snapshot date, so you can see how contribution patterns evolve. Note that each snapshot is a rolling
    {months}-month window, so adjacent points share overlapping data.</li>
    <li><strong>Community health over time:</strong> the same snapshots, read for breadth rather than volume. Snapshots
    archived before these metrics existed are skipped; run <code>python backfill.py --regenerate-existing</code> to
    recompute every archived snapshot in place. The merge-time panel omits an organisation on dates where it had fewer
    than {MIN_PRS_FOR_LATENCY} merged PRs, so sparse weeks do not show up as spikes.</li>
    <li><strong>Browse History:</strong> each archived snapshot is also rendered as a standalone page showing the data
    as it was at that point in time.</li>
  </ul>
  <p>Organisation attribution is based on a manually maintained mapping of GitHub usernames to organisations which may not be exhaustive.</p>
</body>
</html>"""

with open("docs/index.html", "w") as f:
    f.write(html)
