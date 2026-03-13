import json
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots

with open("results.json") as f:
    data = json.load(f)

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

cols = 3
rows = -(-len(repos) // cols)  # ceiling division
fig = make_subplots(rows=rows, cols=cols, subplot_titles=repos)

for repo_idx, repo in enumerate(repos):
    row = repo_idx // cols + 1
    col = repo_idx % cols + 1

    for org in all_orgs:
        counts = [data["repos"][repo].get(key, {}).get(org, 0) for key in metric_keys]
        fig.add_trace(
            go.Bar(
                name=org,
                x=metric_labels,
                y=counts,
                legendgroup=org,
                showlegend=(repo_idx == 0),
                hovertemplate=f"{org}: %{{y}}<extra></extra>",
                marker_color=color_map[org],
            ),
            row=row,
            col=col,
        )

fig.update_layout(
    barmode="stack",
    title=f"Anemoi Contributors (last 3 months) — {data['generated_at'][:10]}",
    height=700,
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

  <h2>Methodology</h2>
  <p>Data collected from the GitHub API via PyGithub, covering activity in the 90 days prior to {data['generated_at'][:10]}
  across the following repositories: {", ".join(repos)}.</p>
  <ul>
    <li><strong>Issues:</strong> count of issues opened, grouped by the organisation of the issue author.</li>
    <li><strong>Pull Requests:</strong> count of merged PRs where an organisation had at least one commit author.
    Each organisation is counted at most once per PR, even if multiple members contributed commits.</li>
    <li><strong>Total Reviews:</strong> all review submissions on PRs created in the period, grouped by the reviewer's organisation.</li>
    <li><strong>Unique Reviews:</strong> as above, but each reviewer is counted at most once per PR.</li>
  </ul>
  <p>Organisation attribution is based on a manually maintained mapping of GitHub usernames to organisations.</p>
</body>
</html>"""

with open("docs/index.html", "w") as f:
    f.write(html)

fig.show()
