import json
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots

with open("results.json") as f:
    data = json.load(f)

repos = list(data["repos"].keys())
metrics = [
    ("issues", "Issues"),
    ("pull_requests", "Pull Requests"),
    ("total_reviews", "Total Reviews"),
    ("unique_reviews", "Unique Reviews"),
]

# Collect all orgs across all repos and metrics
all_orgs = set()
for repo_data in data["repos"].values():
    for metric, _ in metrics:
        all_orgs.update(repo_data[metric].keys())
all_orgs = sorted(all_orgs)

colors = px.colors.qualitative.Plotly
color_map = {org: colors[i % len(colors)] for i, org in enumerate(all_orgs)}

fig = make_subplots(rows=2, cols=2, subplot_titles=[title for _, title in metrics])

for idx, (metric, title) in enumerate(metrics):
    row = idx // 2 + 1
    col = idx % 2 + 1

    for org in all_orgs:
        counts = [data["repos"][repo].get(metric, {}).get(org, 0) for repo in repos]
        fig.add_trace(
            go.Bar(
                name=org,
                x=repos,
                y=counts,
                legendgroup=org,
                showlegend=(idx == 0),  # show legend entry only once
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
)

fig.show()
