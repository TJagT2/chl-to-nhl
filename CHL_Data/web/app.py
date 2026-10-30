"""
CHL Prospect Explorer: a small Flask web app over the chl_stats database.

Pages
    /                     search for any CHL player (search-as-you-type)
    /player/<id>          his CHL seasons, team share of scoring, percentile among
                          forwards, draft slot vs. expectation, and NHL career
    /leaders              top single seasons by team point share (or goal share,
                          assist share, points per game), filterable
    /draft                what forwards drafted in each decile became, and the
                          late-round categories
    /draft/category/<n>   the players in one late-round category

Run from VS Code (open this file, click Run) or a terminal, from the web folder:
    python app.py
then open http://127.0.0.1:5000 (it opens automatically). Stop it with Ctrl+C.
It runs only on your computer. Needs: pip install flask (plus the project's requirements).
"""

import argparse
import sys
import threading
import webbrowser
from functools import lru_cache
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request

# Reuse the project's shared code and draft analysis from the Python folder
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "Python"))
from common import LEAGUES, add_db_args, connect, db_settings, person_key, query_df  # noqa: E402
import draft_analysis  # noqa: E402

app = Flask(__name__)
SETTINGS, DB_NAME = None, "chl_stats"          # filled in by main()

METRICS = {
    "point_share_gp_adj": "Team point share",
    "goal_share_gp_adj": "Team goal share",
    "assist_share_gp_adj": "Team assist share",
    "points_per_game": "Points per game",
}

# Percentile among forwards in the same league season (the view's own percentile
# ranks every position together)
FORWARD_SEASONS_CTE = """
WITH fwd AS (
    SELECT player_id, league, season,
           ROUND(PERCENT_RANK() OVER (PARTITION BY league, season
                                      ORDER BY point_share_gp_adj)::NUMERIC, 3) AS fwd_pctile
    FROM v_player_season_shares
    WHERE position_group = 'F'
)
"""


def db():
    return connect(SETTINGS, DB_NAME)


def records(df):
    """DataFrame -> list of dicts for templates (NaN becomes None)."""
    return df.astype(object).where(df.notna(), None).to_dict("records")


# ---------------------------------------------------------------------
# Cached data (computed once, on first use)
# ---------------------------------------------------------------------

@lru_cache(maxsize=1)
def player_index():
    with db() as conn:
        df = query_df(conn, """
            SELECT p.player_id, p.full_name,
                   STRING_AGG(DISTINCT l.code, ' / ') AS leagues,
                   MIN(s.start_year) AS first_year, MAX(s.start_year) AS last_year,
                   SUM(ps.points) FILTER (WHERE ps.game_type = 'regular') AS points,
                   BOOL_OR(x.nhl_player_id IS NOT NULL) AS reached_nhl
            FROM players p
            JOIN player_seasons ps USING (player_id)
            JOIN leagues l USING (league_id)
            JOIN seasons s USING (season_id)
            LEFT JOIN player_season_nhl x USING (player_season_id)
            GROUP BY p.player_id, p.full_name""")
    df["key"] = df["full_name"].map(person_key)
    return df


@lru_cache(maxsize=1)
def draft_data():
    """Draft picks, forward decile table, expected careers and late-round categories."""
    with db() as conn:
        picks = draft_analysis.load_picks(conn)
        table, players, _ = draft_analysis.compute_outliers(conn, picks)
        ids = query_df(conn, "SELECT player_season_id, player_id FROM player_seasons")
    players = players.merge(ids, left_on="draft_season_id", right_on="player_season_id",
                            how="left", suffixes=("", "_ps"))
    forwards = picks[picks["is_forward"]]
    deciles = draft_analysis.summarize(forwards, ["draft_decile"])
    expected = (forwards.groupby(["draft_decile", "era"])[["nhl_gp", "nhl_points"]]
                .mean().reset_index())
    return {"picks": picks, "deciles": deciles, "expected": expected,
            "categories": table, "players": players}


# ---------------------------------------------------------------------
# Template helpers
# ---------------------------------------------------------------------

@app.template_filter("pct")
def pct(value, digits=1):
    return "–" if value is None else f"{value * 100:.{digits}f}%"


@app.template_filter("num")
def num(value, digits=0):
    if value is None:
        return "–"
    return f"{value:,.{digits}f}"


@app.context_processor
def globals_for_templates():
    return {"leagues": list(LEAGUES), "metrics": METRICS}


# ---------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------

@app.route("/")
def home():
    idx = player_index()
    stars = (idx[idx["reached_nhl"]].sort_values("points", ascending=False).head(12))
    return render_template("index.html", total=len(idx), stars=records(stars))


@app.route("/api/search")
def api_search():
    q = person_key(request.args.get("q", ""))
    if len(q) < 2:
        return jsonify([])
    idx = player_index()
    hits = idx[idx["key"].str.contains(q, regex=False)]
    starts = hits["key"].str.startswith(q) | hits["key"].str.contains(" " + q, regex=False)
    hits = hits.assign(_rank=~starts).sort_values(["_rank", "points"], ascending=[True, False]).head(15)
    return jsonify([{"id": int(r.player_id), "name": r.full_name, "leagues": r.leagues,
                     "years": f"{int(r.first_year)}-{str(int(r.last_year) + 1)[-2:]}",
                     "nhl": bool(r.reached_nhl)} for r in hits.itertuples()])


@app.route("/player/<int:player_id>")
def player(player_id):
    with db() as conn:
        info = query_df(conn, "SELECT player_id, full_name FROM players WHERE player_id = %s", (player_id,))
        if info.empty:
            abort(404)
        seasons = query_df(conn, FORWARD_SEASONS_CTE + """
            SELECT v.league, v.season, v.start_year, v.teams, v.position, v.position_group,
                   v.gp, v.goals, v.assists, v.points, v.points_per_game,
                   v.goal_share_gp_adj, v.assist_share_gp_adj, v.point_share_gp_adj,
                   v.best_team_scoring_rank, f.fwd_pctile,
                   v.playoff_gp, v.playoff_goals, v.playoff_assists, v.playoff_points
            FROM v_player_season_shares v
            LEFT JOIN fwd f ON f.player_id = v.player_id AND f.league = v.league AND f.season = v.season
            WHERE v.player_id = %s
            ORDER BY v.start_year""", (player_id,))
        nhl = query_df(conn, """
            SELECT DISTINCT n.nhl_player_id, n.full_name, n.birth_date, n.position, n.height_in,
                   n.weight_lb, n.nhl_gp, n.nhl_goals, n.nhl_assists, n.nhl_points, n.hall_of_fame
            FROM player_season_nhl x
            JOIN player_seasons ps USING (player_season_id)
            JOIN nhl_players n USING (nhl_player_id)
            WHERE ps.player_id = %s""", (player_id,))
        draft = query_df(conn, """
            SELECT DISTINCT d.draft_year, d.round, d.overall_pick, d.draft_decile, d.drafted_by,
                   d.amateur_team, d.height_in, d.nhl_player_id
            FROM draft_picks d
            LEFT JOIN player_seasons ps ON ps.player_season_id = d.draft_season_id
            WHERE ps.player_id = %s OR d.nhl_player_id = ANY(%s)
            ORDER BY d.draft_year""", (player_id, [int(i) for i in nhl["nhl_player_id"]]))

    # Draft slot vs. expectation (forwards drafted in the same decile and era)
    picks_out = []
    exp = draft_data()["expected"]
    for d in records(draft):
        era = "1986-1995" if d["draft_year"] <= 1995 else ("1996-2005" if d["draft_year"] <= 2005 else "2006-2016")
        row = exp[(exp["draft_decile"] == d["draft_decile"]) & (exp["era"] == era)]
        d["expected_gp"] = float(row["nhl_gp"].iat[0]) if len(row) else None
        d["expected_points"] = float(row["nhl_points"].iat[0]) if len(row) else None
        career = nhl[nhl["nhl_player_id"] == d["nhl_player_id"]]
        d["actual_gp"] = int(career["nhl_gp"].iat[0]) if len(career) else 0
        d["actual_points"] = int(career["nhl_points"].iat[0]) if len(career) else 0
        picks_out.append(d)

    chart = [{"label": f"{r['season'][2:4]}-{r['season'][-2:]}", "league": r["league"],
              "value": r["point_share_gp_adj"], "points": r["points"], "gp": r["gp"]}
             for r in records(seasons)]
    return render_template("player.html", name=info["full_name"].iat[0].split(" [")[0],
                           seasons=records(seasons), nhl=records(nhl), picks=picks_out, chart=chart)


@lru_cache(maxsize=1)
def season_table():
    """Every player season with shares, percentile among forwards and NHL career (cached)."""
    with db() as conn:
        df = query_df(conn, """
            SELECT player_id, full_name, league, season, start_year, teams, position,
                   position_group, gp, goals, assists, points, points_per_game,
                   goal_share_gp_adj, assist_share_gp_adj, point_share_gp_adj, point_share_pctile
            FROM v_player_season_shares""")
        nhl = query_df(conn, """
            SELECT DISTINCT ps.player_id, n.nhl_gp, n.nhl_points
            FROM player_season_nhl x
            JOIN player_seasons ps USING (player_season_id)
            JOIN nhl_players n USING (nhl_player_id)""")
    # percentile among forwards in the same league season (same as SQL PERCENT_RANK)
    fwd = df[df["position_group"] == "F"]
    g = fwd.groupby(["league", "season"])["point_share_gp_adj"]
    rank, count = g.rank(method="min"), g.transform("count")
    df["fwd_pctile"] = ((rank - 1) / (count - 1)).where(count > 1, 0.0).round(3)
    nhl = nhl.sort_values("nhl_gp", ascending=False).drop_duplicates("player_id")
    df = df.merge(nhl, on="player_id", how="left")
    df["full_name"] = df["full_name"].str.replace(r"\s*\[.*\]", "", regex=True)
    return df


@app.route("/leaders")
def leaders():
    league = request.args.get("league", "all")
    season = request.args.get("season", "all")
    metric = request.args.get("metric", "point_share_gp_adj")
    position = request.args.get("position", "F")
    min_gp = request.args.get("min_gp", 40, type=int)
    if metric not in METRICS or position not in ("F", "D", "all"):
        abort(400)

    df = season_table()
    rows = df[df["gp"] >= min_gp]
    if league in LEAGUES:
        rows = rows[rows["league"] == league]
    if season != "all":
        rows = rows[rows["season"] == season]
    if position != "all":
        rows = rows[rows["position_group"] == position]
    rows = rows.sort_values(metric, ascending=False, na_position="last").head(100).copy()
    # percentile among forwards when viewing forwards, otherwise among all top-100 scorers
    rows["pctile"] = rows["fwd_pctile"] if position == "F" else rows["point_share_pctile"]
    seasons = df.drop_duplicates("season").sort_values("start_year")["season"].tolist()
    return render_template("leaders.html", rows=records(rows), seasons=seasons,
                           f={"league": league, "season": season, "metric": metric,
                              "position": position, "min_gp": min_gp})


@app.route("/draft")
def draft():
    data = draft_data()
    cats = data["categories"].copy()
    cats["number"] = cats["category"].str.extract(r"^(\d)\.")[0]
    return render_template("draft.html", deciles=records(data["deciles"]), categories=records(cats))


@app.route("/draft/category/<int:number>")
def category(number):
    key = f"cat{number}"
    if key not in draft_analysis.CATEGORIES:
        abort(404)
    data = draft_data()
    players = data["players"]
    rows = players[players[key]].sort_values("nhl_gp", ascending=False)
    summary = data["categories"].iloc[number - 1]
    return render_template("category.html", title=draft_analysis.CATEGORIES[key],
                           summary=summary.to_dict(), rows=records(rows))


@app.errorhandler(404)
def not_found(_):
    return render_template("message.html", title="Not found",
                           message="That page or player doesn't exist."), 404


def main():
    global SETTINGS, DB_NAME
    parser = add_db_args(argparse.ArgumentParser(description="CHL Prospect Explorer web app."))
    parser.add_argument("--web-port", type=int, default=5000, help="web address port (default 5000)")
    parser.add_argument("--no-browser", action="store_true", help="don't open the browser automatically")
    args = parser.parse_args()
    SETTINGS, DB_NAME = db_settings(args), args.db
    with db() as conn:                                   # fail early if the password is wrong
        conn.execute("SELECT 1 FROM draft_picks LIMIT 1")
    url = f"http://127.0.0.1:{args.web_port}"
    print(f"\nCHL Prospect Explorer running at {url}  (press Ctrl+C to stop)\n")
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=args.web_port, debug=False)


if __name__ == "__main__":
    main()
