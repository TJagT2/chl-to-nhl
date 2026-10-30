"""
NHL draft analysis, 1986-2016. Reads the chl_stats database (load_database.py).

Reports (in CHL_Data_Full/Analysis):
    NHL Draft Deciles 1986-2016.xlsx
        Each draft split into ten equal groups by overall pick (decile 1 = first 10% of
        all picks that year). Average NHL games, goals, assists and points per FORWARD
        pick, with picks who never played counted as 0; also by era, CHL vs. other,
        by round, and a forwards vs. defencemen comparison.
    Late Round Outliers 1986-2016.xlsx
        CHL forwards drafted in the second half of the draft (deciles 6-10), grouped into
        categories based on their draft-year CHL season, compared with the NHL career
        normally expected from their draft slot.
plus charts (PNG) in the same folder.

Run from VS Code (open the file, click Run) or a terminal:
    python draft_analysis.py                     # both reports
    python draft_analysis.py --report outliers   # just one: deciles or outliers
"""

import argparse

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from common import (ANALYSIS_DIR, BLUE, GRID, INK, INK_MUTED, LEAGUES, add_db_args, connect,
                    db_settings, query_df, save_workbook, sheet, style_axes)

OUTCOMES = ["nhl_gp", "nhl_goals", "nhl_assists", "nhl_points"]

# Late-round outlier categories
MAX_HEIGHT_IN = 70                     # 5'10"
MIN_GOAL_SHARE = 0.17                  # category 7: scored 17%+ of team goals
MIN_POINT_SHARE = 0.30                 # category 8: in on 30%+ of team goals
CATEGORIES = {
    "cat1": "1. Top 2 on team in goals, below-average team",
    "cat2": "2. Top 2 on team in points, below-average team",
    "cat3": "3. Top 10% of league in goals, below-average team",
    "cat4": "4. Top 10% of league in points, below-average team",
    "cat5": "5. Top 10% of league in team goal share, 5'10\" or shorter",
    "cat6": "6. Top 10% of league in team point share, 5'10\" or shorter",
    "cat7": "7. Goal share 17% or more, any team",
    "cat8": "8. Point share 30% or more, any team",
}


# =====================================================================
# Data
# =====================================================================

PICKS_SQL = """
SELECT d.draft_year, d.round, d.overall_pick, d.picks_that_year, d.draft_decile, d.drafted_by,
       d.nhl_player_id, d.player, d.position, d.height_in, d.birth_date,
       d.amateur_team, d.amateur_league, d.draft_season_id,
       COALESCE(n.nhl_gp, 0) AS nhl_gp, COALESCE(n.nhl_goals, 0) AS nhl_goals,
       COALESCE(n.nhl_assists, 0) AS nhl_assists, COALESCE(n.nhl_points, 0) AS nhl_points
FROM draft_picks d
LEFT JOIN nhl_players n ON n.nhl_player_id = d.nhl_player_id
"""

# Each regular-season CHL player-team line with his team's record and goals
SEASON_LINES_SQL = """
SELECT ps.player_season_id, l.code AS league, s.start_year, t.name AS team,
       pst.gp, pst.goals, pst.assists, pst.points,
       ts.gp AS team_gp, ts.gf AS team_gf, ts.record_pct AS team_record_pct
FROM player_season_teams pst
JOIN player_seasons ps ON ps.player_season_id = pst.player_season_id
JOIN leagues l   ON l.league_id = ps.league_id
JOIN seasons s   ON s.season_id = ps.season_id
JOIN teams t     ON t.team_id = pst.team_id
JOIN team_seasons ts ON ts.team_id = pst.team_id AND ts.season_id = ps.season_id
WHERE ps.game_type = 'regular'
"""
LEAGUE_RECORD_SQL = """
SELECT l.code AS league, s.start_year, AVG(ts.record_pct) AS league_record_pct
FROM team_seasons ts JOIN leagues l USING (league_id) JOIN seasons s USING (season_id)
GROUP BY l.code, s.start_year
"""


def load_picks(conn):
    picks = query_df(conn, PICKS_SQL)
    if picks.empty:
        raise SystemExit("No draft picks in the database. Run collect_data.py, then load_database.py.")
    picks["era"] = pd.cut(picks["draft_year"], [1985, 1995, 2005, 2016],
                          labels=["1986-1995", "1996-2005", "2006-2016"]).astype(str)
    picks["is_forward"] = picks["position"].notna() & ~picks["position"].isin(["D", "G"])
    picks["chl_draftee"] = np.where(picks["amateur_league"].isin(LEAGUES), "CHL", "Other")
    picks["played_nhl"] = picks["nhl_gp"] > 0
    picks["nhl_200_gp"] = picks["nhl_gp"] >= 200
    return picks


def chl_draft_seasons(conn):
    """One row per CHL player season with team ranks, shares and league top-10% flags."""
    lines = query_df(conn, SEASON_LINES_SQL)
    lines["team_gf_in_gp"] = lines["team_gf"] / lines["team_gp"] * lines["gp"]
    grp = lines.groupby(["league", "start_year", "team"])
    lines["team_goal_rank"] = grp["goals"].rank(method="min", ascending=False)
    lines["team_point_rank"] = grp["points"].rank(method="min", ascending=False)

    # Traded players: totals across teams, team details from the team he played most for
    main = lines.sort_values("gp", ascending=False).drop_duplicates("player_season_id")
    totals = lines.groupby("player_season_id")[["gp", "goals", "assists", "points", "team_gf_in_gp"]].sum()
    seasons = main.drop(columns=list(totals.columns)).merge(totals, left_on="player_season_id",
                                                            right_index=True)
    seasons["goal_share"] = seasons["goals"] / seasons["team_gf_in_gp"]
    seasons["point_share"] = seasons["points"] / seasons["team_gf_in_gp"]
    seasons = seasons.merge(query_df(conn, LEAGUE_RECORD_SQL), on=["league", "start_year"], how="left")
    seasons["below_avg_team"] = seasons["team_record_pct"] < seasons["league_record_pct"]
    g = seasons.groupby(["league", "start_year"])
    for stat in ("goals", "points", "goal_share", "point_share"):
        seasons[f"top10_league_{stat}"] = seasons[stat] >= g[stat].transform(lambda s: s.quantile(0.9))
    return seasons


# =====================================================================
# Report 1: draft deciles
# =====================================================================

def summarize(df, by):
    t = df.groupby(by, observed=True).agg(
        picks=("player", "size"),
        first_pick=("overall_pick", "min"),
        last_pick=("overall_pick", "max"),
        avg_nhl_gp=("nhl_gp", "mean"),
        avg_nhl_goals=("nhl_goals", "mean"),
        avg_nhl_assists=("nhl_assists", "mean"),
        avg_nhl_points=("nhl_points", "mean"),
        median_nhl_gp=("nhl_gp", "median"),
        pct_played_nhl=("played_nhl", "mean"),
        pct_200_plus_gp=("nhl_200_gp", "mean"),
    ).reset_index()
    played = df[df["played_nhl"]].groupby(by, observed=True).agg(
        avg_gp_if_played=("nhl_gp", "mean"), avg_points_if_played=("nhl_points", "mean")).reset_index()
    return t.merge(played, on=by, how="left")


def deciles_chart(by_decile, path):
    fig, ax = plt.subplots(figsize=(9, 4.8), dpi=150)
    x, y = by_decile["draft_decile"], by_decile["avg_nhl_gp"]
    ax.bar(x, y, width=0.6, color=BLUE)
    for xi, yi in zip(x, y):
        if xi in (1, 2, 5, 10):
            ax.text(xi, yi + max(y) * 0.015, f"{yi:.0f}", ha="center", fontsize=9, color=INK)
    ax.set_xticks(x, [f"D{d}" for d in x])
    ax.set_xlabel("Draft decile (D1 = first 10% of picks, D10 = last 10%)", color=INK_MUTED, fontsize=9)
    ax.set_ylabel("Average NHL games played per forward pick", color=INK_MUTED, fontsize=9)
    ax.yaxis.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    style_axes(ax)
    ax.set_title("NHL games per forward draft pick by draft decile, 1986-2016",
                 loc="left", fontsize=12, color=INK)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def report_deciles(picks):
    # Deciles are by overall pick across the whole draft (a slot is a slot, whatever the
    # position); the averages use forwards only, since the analysis is about scoring.
    forwards = picks[picks["is_forward"]]
    skaters = picks[picks["position"].isin(["D"]) | picks["is_forward"]].copy()
    skaters["position_group"] = np.where(skaters["is_forward"], "Forwards", "Defencemen")
    by_decile = summarize(forwards, ["draft_decile"])
    d1, d10 = by_decile.iloc[0], by_decile.iloc[-1]
    summary = [
        ("Draft picks 1986-2016", f"{len(picks):,} in total; {len(forwards):,} forwards analysed "
                                  "(defencemen and goalies excluded from the main tables)"),
        ("Forward picks who played in the NHL", f"{forwards['played_nhl'].mean():.0%}"),
        ("First 10% of picks (decile 1)",
         f"avg {d1['avg_nhl_gp']:.0f} games, {d1['avg_nhl_goals']:.0f} goals, {d1['avg_nhl_assists']:.0f} "
         f"assists, {d1['avg_nhl_points']:.0f} points; {d1['pct_played_nhl']:.0%} played, "
         f"{d1['pct_200_plus_gp']:.0%} reached 200 games"),
        ("Last 10% of picks (decile 10)",
         f"avg {d10['avg_nhl_gp']:.0f} games, {d10['avg_nhl_points']:.0f} points; "
         f"{d10['pct_played_nhl']:.0%} played, {d10['pct_200_plus_gp']:.0%} reached 200 games"),
        ("How deciles work", "Each draft is split into ten equal groups by overall pick across ALL "
                             "positions, so decile 1 is the first 10% of picks that year (roughly 21-29 "
                             "picks depending on draft size). Averages then use the forwards taken there."),
        ("Averages", "Picks who never played count as 0, so averages show the expected return of a "
                     "forward pick in that range. 'if_played' columns show averages for those who made it."),
        ("Forwards vs Defencemen", "Comparison sheet showing how much less defencemen score from the "
                                   "same draft slots."),
        ("Link to outlier report", "These forward averages (by decile and era) are the 'expected career' "
                                   "used in Late Round Outliers."),
        ("Limits", "NHL regular-season careers only, through 2025-26. Players drafted 2013-2016 may still "
                   "be adding games. A player drafted twice (re-entered the draft) counts once per pick."),
        ("Sources", "Draft: NHL records API. Careers: NHL stats API."),
    ]
    png = ANALYSIS_DIR / "NHL Draft Deciles.png"
    ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
    deciles_chart(by_decile, png)
    pct = ["pct_played_nhl", "pct_200_plus_gp"]
    path = save_workbook(ANALYSIS_DIR / "NHL Draft Deciles 1986-2016.xlsx", {
        "Summary": sheet(pd.DataFrame(summary, columns=["Item", "Result"])),
        "By Decile": sheet(by_decile.round(3)),
        "Decile x Era": sheet(summarize(forwards, ["era", "draft_decile"]).round(3)),
        "CHL vs Other": sheet(summarize(forwards, ["chl_draftee", "draft_decile"]).round(3)),
        "By Round": sheet(summarize(forwards, ["round"]).round(3)),
        "Forwards vs Defencemen": sheet(summarize(skaters, ["position_group", "draft_decile"]).round(3)),
        "All Picks": sheet(picks.sort_values(["draft_year", "overall_pick"])
                           .drop(columns=["era", "draft_season_id", "is_forward"])),
    }, images={"By Decile": (png, "A14")}, wide={"Summary": {"B": 110}},
        percent_cols={s: pct for s in ("By Decile", "Decile x Era", "CHL vs Other", "By Round",
                                       "Forwards vs Defencemen")})

    print(f"Saved {path.name} (forwards)")
    print(by_decile[["draft_decile", "picks", "avg_nhl_gp", "avg_nhl_goals", "avg_nhl_assists",
                     "avg_nhl_points", "pct_played_nhl"]].to_string(
        index=False, formatters={c: "{:.1f}".format for c in
                                 ("avg_nhl_gp", "avg_nhl_goals", "avg_nhl_assists", "avg_nhl_points")}
        | {"pct_played_nhl": "{:.0%}".format}))


# =====================================================================
# Report 2: late-round outliers
# =====================================================================

def compare(group, label):
    row = {"category": label, "players": len(group)}
    if not len(group):
        return row
    for o in OUTCOMES:
        actual, exp = group[o].mean(), group[f"exp_{o}"].mean()
        short = o.replace("nhl_", "")
        row[f"avg_{short}"] = actual
        row[f"expected_{short}"] = exp
        row[f"{short}_vs_expected"] = actual / exp if exp else np.nan
    row["pct_played_nhl"] = group["played_nhl"].mean()
    row["expected_pct_played"] = group["exp_played_nhl"].mean()
    row["pct_200_plus_gp"] = group["nhl_200_gp"].mean()
    row["expected_pct_200_gp"] = group["exp_nhl_200_gp"].mean()
    return row


def outliers_chart(table, path):
    t = table[table["category"].str.match(r"^\d\.")].iloc[::-1]
    fig, ax = plt.subplots(figsize=(10, 5.5), dpi=150)
    y = np.arange(len(t))
    ax.barh(y, t["gp_vs_expected"], height=0.55, color=BLUE)
    ax.axvline(1.0, color=INK_MUTED, linewidth=1, linestyle="--")
    for yi, (v, n) in enumerate(zip(t["gp_vs_expected"], t["players"])):
        if pd.notna(v):
            ax.text(v + 0.03, yi, f"{v:.2f}x  (n={int(n)})", va="center", fontsize=9, color=INK)
    ax.set_yticks(y, t["category"], fontsize=9, color=INK)
    ax.set_xlabel("NHL games played vs. expected for draft slot (1.0 = as expected)",
                  color=INK_MUTED, fontsize=9)
    ax.xaxis.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    style_axes(ax)
    ax.set_xlim(0, max(1.5, (t["gp_vs_expected"].max() or 1) * 1.25))
    ax.set_title("Second-half CHL forward picks: NHL games vs. draft-slot expectation",
                 loc="left", fontsize=12, color=INK)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def compute_outliers(conn, picks):
    """
    Returns (table, players, all_late): the category comparison table, the
    second-half CHL forwards with their draft-year stats and category flags, and
    every second-half CHL forward pick (including those not in the top-100 data).
    """
    # Expected career = average of all forwards drafted in the same decile and era
    expected = (picks[picks["is_forward"]].groupby(["draft_decile", "era"])
                [OUTCOMES + ["played_nhl", "nhl_200_gp"]].mean().add_prefix("exp_").reset_index())
    picks = picks.merge(expected, on=["draft_decile", "era"], how="left")
    df = picks[(picks["draft_decile"] >= 6) & picks["is_forward"] & (picks["chl_draftee"] == "CHL")]
    df = df.merge(chl_draft_seasons(conn), left_on="draft_season_id", right_on="player_season_id",
                  how="left")
    df["in_chl_data"] = df["draft_season_id"].notna()

    d = df[df["in_chl_data"]].copy()
    short = d["height_in"].notna() & (d["height_in"] <= MAX_HEIGHT_IN)
    below = d["below_avg_team"].astype(bool)
    d["cat1"] = (d["team_goal_rank"] <= 2) & below
    d["cat2"] = (d["team_point_rank"] <= 2) & below
    d["cat3"] = d["top10_league_goals"].astype(bool) & below
    d["cat4"] = d["top10_league_points"].astype(bool) & below
    d["cat5"] = d["top10_league_goal_share"].astype(bool) & short
    d["cat6"] = d["top10_league_point_share"].astype(bool) & short
    d["cat7"] = d["goal_share"] >= MIN_GOAL_SHARE
    d["cat8"] = d["point_share"] >= MIN_POINT_SHARE
    d["any_category"] = d[list(CATEGORIES)].any(axis=1)

    table = pd.DataFrame([compare(d[d[c]], label) for c, label in CATEGORIES.items()] + [
        compare(d[d["any_category"]], f"Any of the {len(CATEGORIES)} categories"),
        compare(d[~d["any_category"]], "Top-100 CHL scorers in none of the categories"),
        compare(df[df["in_chl_data"]], "All 2nd-half CHL forwards who were top-100 scorers"),
        compare(df[~df["in_chl_data"]], "2nd-half CHL forwards NOT in top-100 data"),
        compare(df, "All 2nd-half CHL forward picks"),
    ])
    return table, d, df


def report_outliers(conn, picks):
    table, d, df = compute_outliers(conn, picks)
    overlap = pd.DataFrame({CATEGORIES[a][:2].strip(): [int((d[a] & d[b]).sum()) for b in CATEGORIES]
                            for a in CATEGORIES}, index=list(CATEGORIES.values()))

    t = table.set_index("category")
    everyone = t.loc["All 2nd-half CHL forward picks"]
    best = table[table["category"].str.match(r"^\d\.")].sort_values("gp_vs_expected", ascending=False).iloc[0]
    summary = [
        ("Second-half CHL forward picks, 1986-2016", f"{len(df):,} ({int(df['in_chl_data'].sum()):,} were "
                                                     "top-100 scorers in their draft-year season)"),
        ("All of them vs. expectation", f"{everyone['gp_vs_expected']:.2f}x the NHL games expected for "
                                        "their draft slot"),
        ("Best category", f"{best['category']}: {best['gp_vs_expected']:.2f}x expected NHL games "
                          f"({int(best['players'])} players)"),
        ("How to read 'vs_expected'", "1.00 = careers exactly as expected for where they were drafted; "
                                      "1.50 = 50% more games/goals/points than expected."),
        ("Expected career", "Average of all forwards drafted in the same draft decile and era "
                            "(1986-95, 1996-2005, 2006-16), counting those who never played as 0."),
        ("Below-average team", "Team's record (standings points / possible points) below its league's "
                               "average that season."),
        ("Top 10% of league", "Among the players in our data (each league's top-100 scorers), so roughly "
                              "the top 5% of all scorers. Shares are adjusted for games played."),
        ("Small samples", "Treat categories with fewer than ~30 players as anecdotal."),
        ("Limits", "Draft picks are matched to CHL stats by name, league and season. Players drafted "
                   "2013-2016 may still be adding NHL games. Height is height at the draft."),
    ]
    player_cols = ["draft_year", "round", "overall_pick", "draft_decile", "drafted_by", "player",
                   "position", "height_in", "league", "team", "gp", "goals", "assists", "points",
                   "goal_share", "point_share", "team_goal_rank", "team_point_rank",
                   "team_record_pct", "league_record_pct", "nhl_gp", "nhl_goals", "nhl_assists",
                   "nhl_points", "exp_nhl_gp", "exp_nhl_points"]

    png = ANALYSIS_DIR / "Late Round Outliers.png"
    ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
    outliers_chart(table, png)
    sheets = {"Summary": sheet(pd.DataFrame(summary, columns=["Item", "Result"])),
              "Categories": sheet(table.round(3)),
              "Overlap": sheet(overlap, index=True)}
    for c in CATEGORIES:
        sheets[f"Category {c[-1]} Players"] = sheet(
            d[d[c]].sort_values("nhl_gp", ascending=False)[player_cols].round(3))
    pct = [c for c in table.columns if "pct" in c]
    path = save_workbook(ANALYSIS_DIR / "Late Round Outliers 1986-2016.xlsx", sheets,
                         images={"Categories": (png, "A17")},
                         wide={"Summary": {"B": 110}, "Categories": {"A": 58}},
                         percent_cols={"Categories": pct})

    print(f"Saved {path.name}")
    print(table[["category", "players", "avg_gp", "expected_gp", "gp_vs_expected",
                 "avg_points", "expected_points", "points_vs_expected"]].to_string(
        index=False, formatters={c: "{:.1f}".format for c in ("avg_gp", "expected_gp", "avg_points",
                                                              "expected_points")}
        | {c: "{:.2f}x".format for c in ("gp_vs_expected", "points_vs_expected")}))


# =====================================================================

def main():
    parser = add_db_args(argparse.ArgumentParser(description="NHL draft decile and late-round reports."))
    parser.add_argument("--report", choices=["deciles", "outliers"],
                        help="run just one report (default: both)")
    args = parser.parse_args()
    with connect(db_settings(args), args.db) as conn:
        picks = load_picks(conn)
        if args.report in (None, "deciles"):
            report_deciles(picks)
            print()
        if args.report in (None, "outliers"):
            report_outliers(conn, picks)


if __name__ == "__main__":
    main()
