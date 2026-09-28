import html

import plotly.graph_objects as go


def short_title(content, length=24):
    first = next((line.lstrip("# ").strip() for line in content.splitlines() if line.strip()), "未命名片段")
    return first[:length] + ("…" if len(first) > length else "")


def scatter(plot, hits, selected_id=None, show_query=True, show_hits=True, links=False, exploration=False):
    figure = go.Figure()
    points = plot.get("points", [])
    hit_by_id = {h["point_id"]: h for h in hits} if show_hits else {}
    background = [p for p in points if p["point_id"] not in hit_by_id]
    matched = [p for p in points if p["point_id"] in hit_by_id]
    query = plot.get("query") if show_query else None
    if links and query and matched:
        xs, ys = [], []
        for p in matched:
            xs += [query[0], p["x"], None]
            ys += [query[1], p["y"], None]
        figure.add_trace(go.Scattergl(x=xs, y=ys, mode="lines", line={"color": "#c8e4df", "width": 1}, hoverinfo="skip", showlegend=False))
    if background:
        figure.add_trace(go.Scattergl(
            x=[p["x"] for p in background], y=[p["y"] for p in background],
            name="库内向量" if exploration else "背景样本", mode="markers",
            customdata=[[p["point_id"], html.escape(short_title(p["content"], 35))] for p in background],
            marker={"size": [13 if p["point_id"] == selected_id else 9 for p in background], "color": ["#6e8994" if p["point_id"] == selected_id else "#bac9d0" for p in background], "opacity": 0.75},
            hovertemplate="%{customdata[1]}<br>当前集合中的向量<extra></extra>" if exploration else "%{customdata[1]}<br>背景样本 · 未在本次响应中返回<extra></extra>",
        ))
    if matched:
        figure.add_trace(go.Scattergl(
            x=[p["x"] for p in matched], y=[p["y"] for p in matched],
            name="本次匹配", mode="markers+text",
            text=[str(hit_by_id[p["point_id"]]["rank"]) for p in matched],
            textposition="top center", textfont={"size": 12, "color": "#087b72"},
            customdata=[[p["point_id"], html.escape(short_title(p["content"], 35)), hit_by_id[p["point_id"]]["score"]] for p in matched],
            marker={"size": [20 if p["point_id"] == selected_id else 14 for p in matched],
                    "color": ["#087e75" if hit_by_id[p["point_id"]]["rank"] == 1 else "#3bbbad" for p in matched],
                    "line": {"color": "#134e4a", "width": [2 if p["point_id"] == selected_id else 0 for p in matched]}},
            hovertemplate="%{customdata[1]}<br>Cosine %{customdata[2]:.5f}<extra></extra>",
        ))
    if query is not None:
        figure.add_trace(go.Scattergl(
            x=[query[0]], y=[query[1]], name="当前问题", mode="markers+text", text=["QUERY"],
            textposition="bottom center", textfont={"color": "#db7753", "size": 11},
            marker={"symbol": "diamond", "size": 17, "color": "#ea906c", "line": {"color": "#ffffff", "width": 2}},
            hovertemplate="本次实际查询向量的投影<extra></extra>", customdata=[["__query__", "当前问题"]],
        ))
    # Keep axes fixed across replay frames; never refit on the query vector.
    all_coords = [[p["x"], p["y"]] for p in points]
    if plot.get("query") is not None:
        all_coords.append(plot["query"])
    def axis_range(index):
        vals = [p[index] for p in all_coords] or [0]
        padding = max((max(vals) - min(vals)) * 0.18, 0.04)
        return [min(vals) - padding, max(vals) + padding]
    figure.update_layout(
        height=490, margin={"l": 10, "r": 10, "t": 12, "b": 10},
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="#fbfdfd",
        font={"family": "Inter, -apple-system, PingFang SC, sans-serif", "color": "#587079"},
        xaxis={"title": "主成分 1", "range": axis_range(0), "gridcolor": "#eef3f3", "zeroline": False, "showticklabels": False},
        yaxis={"title": "主成分 2", "range": axis_range(1), "gridcolor": "#eef3f3", "zeroline": False, "showticklabels": False},
        legend={"orientation": "h", "y": 1.09, "x": 0, "font": {"size": 11}},
        clickmode="event+select", dragmode="pan", hovermode="closest",
        uirevision=str(plot.get("version", "initial")),
    )
    return figure
