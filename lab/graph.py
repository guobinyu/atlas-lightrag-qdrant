"""Plot actual stored nodes and edges, with inspectable relation midpoints."""
import html

import networkx as nx
import plotly.graph_objects as go

COLORS = ["#168b7d", "#5e8bc3", "#d9a35e", "#a684b9", "#69a68c", "#d2847c"]


def subgraph(nodes, edges, focus="", depth=1, entity_type="全部", limit=160):
    graph = nx.Graph()
    graph.add_nodes_from(n["id"] for n in nodes)
    graph.add_edges_from((e["source"], e["target"]) for e in edges)
    allowed = {n["id"] for n in nodes if entity_type == "全部" or n.get("entity_type") == entity_type}
    if focus and focus in graph:
        allowed &= set(nx.single_source_shortest_path_length(graph, focus, cutoff=depth))
    ranked = sorted(allowed, key=lambda n: (n != focus, -graph.degree(n), n))[:limit]
    chosen = set(ranked)
    return [n for n in nodes if n["id"] in chosen], [e for e in edges if e["source"] in chosen and e["target"] in chosen]


def graph_figure(nodes, edges, selected=None):
    graph = nx.Graph()
    graph.add_nodes_from(n["id"] for n in nodes)
    graph.add_edges_from((e["source"], e["target"]) for e in edges)
    positions = nx.spring_layout(graph, seed=23, iterations=80) if nodes else {}
    fig = go.Figure()
    xs, ys, mids, hover, ids = [], [], [], [], []
    for index, edge in enumerate(edges):
        a, b = positions[edge["source"]], positions[edge["target"]]
        xs += [a[0], b[0], None]
        ys += [a[1], b[1], None]
        mids.append(((a[0] + b[0]) / 2, (a[1] + b[1]) / 2))
        hover.append(html.escape(f"{edge['source']} ↔ {edge['target']}") + "<br>" + html.escape(edge.get("keywords", "")))
        ids.append(["edge", index])
    fig.add_trace(go.Scattergl(x=xs, y=ys, mode="lines", line={"color": "#c7dedd", "width": 1.5}, hoverinfo="skip", showlegend=False))
    if mids:
        fig.add_trace(go.Scattergl(x=[p[0] for p in mids], y=[p[1] for p in mids], mode="markers", marker={"size": 7, "color": "#9ebfbc", "symbol": "diamond"}, name="关系", customdata=ids, hovertext=hover, hovertemplate="%{hovertext}<extra>点击检查关系</extra>"))
    types = sorted({n.get("entity_type", "未知") for n in nodes})
    for index, kind in enumerate(types):
        group = [n for n in nodes if n.get("entity_type", "未知") == kind]
        fig.add_trace(go.Scattergl(
            x=[positions[n["id"]][0] for n in group], y=[positions[n["id"]][1] for n in group],
            mode="markers+text", text=[html.escape(n["id"][:25]) for n in group], textposition="top center", textfont={"size": 10},
            customdata=[["node", n["id"]] for n in group], name=kind,
            marker={"size": [min(32, 12 + graph.degree(n["id"]) * 2) for n in group], "color": COLORS[index % len(COLORS)],
                    "line": {"color": "#183e48", "width": [3 if selected == n["id"] else 0 for n in group]}},
            hovertext=[html.escape(n.get("description", "")[:180]) for n in group], hovertemplate="%{text}<br>%{hovertext}<extra></extra>",
        ))
    fig.update_layout(height=590, margin={"l": 10, "r": 10, "t": 30, "b": 15},
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="#fbfdfd", font={"color": "#42616b"},
                      xaxis={"visible": False}, yaxis={"visible": False},
                      legend={"orientation": "h", "y": 1.1}, clickmode="event+select", dragmode="pan")
    return fig
