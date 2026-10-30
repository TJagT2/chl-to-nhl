"""
CHL analysis: players' share of their team's scoring, and how CHL numbers
line up with NHL careers. Reads the chl_stats database (load_database.py).

Forwards only by default, since the analysis is about offensive production:
deciles, percentiles and correlations are calculated among forwards. The database
still holds every position; use --position all (or D) to compare.

Reports (in CHL_Data_Full/Analysis):
    CHL Goal Share Breakdown.xlsx   every player season's team/conference shares and
                                    percentiles; summaries by league-season, position, era
    CHL to NHL Correlation.xlsx     correlation matrix of CHL stats vs. NHL stats, by
                                    position, how well each predicts 200+ NHL games,
                                    outcomes by team point share decile
    CHL Top Decile Breakdown.xlsx   NHL outcomes for every decile of team point share,
                                    goal share, assist share, points per game and
                                    best-season percentile; top-10% groups compared
plus charts (PNG) in the same folder.

Run from VS Code (open the file, click Run) or a terminal:
    python chl_analysis.py                         # all three reports
    python chl_analysis.py --report correlation    # just one: breakdown, correlation, deciles
    python chl_analysis.py --position all          # every position (files get "(All Positions)")
"""

import argparse
import statistics

import matplotlib
matplotlib.use("Agg")                                   # draw charts without opening windows
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.colors import LinearSegmentedColormap
from scipy import stats

from common import (ANALYSIS_DIR, BLUE, DIVERGING, GRID, INK, INK_MUTED, ORANGE, add_db_args, connect,
                    db_settings, query_df, save_workbook, sheet, style_axes)

MIN_GP = 20                 # breakdown: ignore tiny samples in summaries and top lists
LAST_COHORT_SEASON = 2011   # NHL comparisons: last CHL season began by 2011, so careers have
                            # had 14+ seasons to play out
MIN_NHL_GP_FOR_PPG = 100

CHL_MEASURES = {
    "chl_gp": "CHL games played",
    "chl_goals": "CHL goals",
    "chl_assists": "CHL assists",
    "chl_points": "CHL points",
    "chl_goals_per_game": "CHL goals per game",
    "chl_assists_per_game": "CHL assists per game",
    "chl_points_per_game": "CHL points per game",
    "team_goal_share": "Team-adjusted goals (share of team goals)",
    "team_assist_share": "Team-adjusted assists (share of team goals)",
    "team_point_share": "Team-adjusted points (share of team goals)",
    "peak_point_share": "Best season team point share",
    "peak_point_share_pctile": "Best season percentile in league",
}
NHL_MEASURES = {
    "nhl_gp": "NHL games played",
    "nhl_goals": "NHL goals",
    "nhl_assists": "NHL assists",
    "nhl_points": "NHL points",
    "nhl_points_per_game": f"NHL points per game ({MIN_NHL_GP_FOR_PPG}+ GP)",
}
DECILE_MEASURES = {
    "team_point_share": "Team point share",
    "team_goal_share": "Team goal share",
    "team_assist_share": "Team assist share",
    "chl_points_per_game": "Points per game",
    "peak_point_share_pctile": "Best-season percentile",
}

POSITION_LABELS = {"F": "forwards", "D": "defencemen", "all": "all positions"}
POSITION = "F"                       # set from --position in main()


def position_filter(df):
    """Keep the chosen position group ('F' or 'D'); 'all' keeps everyone."""
    if POSITION == "all":
        return df.copy()
    return df[df["position_group"] == POSITION].copy()


def percent_rank(df, col, season_col):
    """Percentile within each league season, same as SQL PERCENT_RANK (0 = lowest, 1 = best)."""
    g = df.groupby(["league", season_col])[col]
    r, n = g.rank(method="min"), g.transform("count")
    return ((r - 1) / (n - 1)).where(n > 1, 0.0).round(3)


def out_path(name):
    """Report file path; non-default position runs get a suffix so they don't overwrite."""
    suffix = "" if POSITION == "F" else f" ({POSITION_LABELS[POSITION].title()})"
    stem, ext = name.rsplit(".", 1)
    return ANALYSIS_DIR / f"{stem}{suffix}.{ext}"


# =====================================================================
# Data
# =====================================================================

SEASONS_SQL = """
SELECT v.player_id, v.full_name, v.league, v.start_year, v.position_group,
       v.gp, v.goals, v.assists, v.points, v.team_gf_in_gp,
       v.point_share_gp_adj, v.point_share_pctile,
       x.nhl_player_id
FROM v_player_season_shares v
JOIN seasons s ON s.label = v.season
JOIN leagues l ON l.code = v.league
JOIN player_seasons ps ON ps.player_id = v.player_id AND ps.season_id = s.season_id
                      AND ps.league_id = l.league_id AND ps.game_type = 'regular'
LEFT JOIN player_season_nhl x ON x.player_season_id = ps.player_season_id
"""
NHL_SQL = """SELECT nhl_player_id, full_name AS nhl_name, birth_date, draft_overall,
                    nhl_gp, nhl_goals, nhl_assists, nhl_points FROM nhl_players"""


def prospect_ids(seasons):
    """
    One prospect per CHL career. Seasons linked to an NHL player are grouped by that
    NHL player. Unlinked seasons are grouped by CHL player, split where there is a gap
    of more than a season (the CHL files match players by name, so two different
    players with the same name years apart are kept separate).
    """
    seasons = seasons.sort_values(["player_id", "start_year"]).copy()
    ids, last = [], {}
    for row in seasons.itertuples():
        if pd.notna(row.nhl_player_id):
            ids.append(f"NHL-{int(row.nhl_player_id)}")
            continue
        prev = last.get(row.player_id)
        segment = prev[1] + 1 if prev and row.start_year - prev[0] > 2 else (prev[1] if prev else 0)
        last[row.player_id] = (row.start_year, segment)
        ids.append(f"CHL-{row.player_id}-{segment}")
    seasons["prospect_id"] = ids
    return seasons


def build_prospects(seasons, nhl):
    """One row per prospect: CHL career (top-100 seasons) plus NHL career (0 if none)."""
    g = seasons.groupby("prospect_id")
    p = pd.DataFrame({
        "name": g["full_name"].first().str.replace(r"\s*\[.*\]", "", regex=True),
        "league": g["league"].agg(lambda s: " / ".join(sorted(set(s)))),
        "position_group": g["position_group"].agg(lambda s: s.mode().iat[0] if s.notna().any() else None),
        "first_chl_season": g["start_year"].min(),
        "last_chl_season": g["start_year"].max(),
        "chl_top100_seasons": g.size(),
        "chl_gp": g["gp"].sum(),
        "chl_goals": g["goals"].sum(),
        "chl_assists": g["assists"].sum(),
        "chl_points": g["points"].sum(),
        "team_gf_in_gp": g["team_gf_in_gp"].sum(),
        "peak_point_share": g["point_share_gp_adj"].max(),
        "peak_point_share_pctile": g["point_share_pctile"].max(),
        "nhl_player_id": g["nhl_player_id"].first(),
    })
    for stat in ("goals", "assists", "points"):
        p[f"chl_{stat}_per_game"] = p[f"chl_{stat}"] / p["chl_gp"]
        p[f"team_{stat[:-1]}_share"] = p[f"chl_{stat}"] / p["team_gf_in_gp"]

    p = p.merge(nhl, on="nhl_player_id", how="left")
    for col in ("nhl_gp", "nhl_goals", "nhl_assists", "nhl_points"):
        p[col] = p[col].fillna(0).astype(int)
    p["nhl_points_per_game"] = np.where(p["nhl_gp"] >= MIN_NHL_GP_FOR_PPG,
                                        p["nhl_points"] / p["nhl_gp"].clip(lower=1), np.nan)
    p["made_nhl"] = p["nhl_gp"] > 0
    p["nhl_200_gp"] = p["nhl_gp"] >= 200
    return p


def load_prospects(conn):
    """All prospects, plus the cohort used for NHL comparisons."""
    seasons = position_filter(query_df(conn, SEASONS_SQL))
    if seasons["nhl_player_id"].notna().sum() == 0:
        raise SystemExit("No CHL seasons are linked to NHL players. Run collect_data.py, then "
                         "load_database.py, first.")
    num = ["gp", "goals", "assists", "points", "team_gf_in_gp", "point_share_gp_adj", "point_share_pctile"]
    seasons[num] = seasons[num].astype(float)
    seasons["point_share_pctile"] = percent_rank(seasons, "point_share_gp_adj", "start_year")
    prospects = build_prospects(prospect_ids(seasons), query_df(conn, NHL_SQL))
    return prospects, prospects[prospects["last_chl_season"] <= LAST_COHORT_SEASON].copy()


# =====================================================================
# Report 1: goal share breakdown
# =====================================================================

BREAKDOWN_NOTES = [
    ("goal_share", "Player's goals / team's goals for (regular season)."),
    ("assist_share", "Player's assists / team's goals for, i.e. the share of team goals he assisted on."),
    ("point_share", "Player's points / team's goals for: share of team goals he was involved in."),
    ("*_gp_adj", "Same, but the team's goals are counted only for the games he played "
                 "(team goals per game x his games). Fairer for injured or traded players."),
    ("group_*_share", "Player's goals/assists/points / all goals scored in his conference "
                      "(or division, or whole league if neither existed that season)."),
    ("point_share_vs_group_avg", "Points / an average team's goals in his conference or division. "
                                 "Comparable across groups of different sizes."),
    ("team_scoring_rank", "His rank in team scoring among that team's top-100 league scorers."),
    ("point_share_pctile", "Percentile of point_share_gp_adj within that league and season (1.0 = best)."),
    ("ppg_pctile", "Percentile of raw points per game within that league and season."),
    ("Players", "Forwards only by default (percentiles rank forwards against forwards). "
                "Run with --position all to include defencemen."),
    ("", ""),
    ("LIMITATIONS", ""),
    ("Top 100 only", "Player files hold each league's top-100 scorers per season, so lower scorers "
                     "(and most defencemen and all goalies) are missing. Percentiles compare players "
                     "with other top-100 scorers, not the whole league."),
    ("Regular season only", "No free source lists team playoff goals, so shares are regular-season. "
                            "Playoff points are shown separately."),
    ("Even pace", "GP-adjusted shares assume the team scored at the same rate in every game."),
    ("Standings source", "Team goals from Wikipedia season pages (CC BY-SA). A few seasons have small "
                         "goals-for vs. goals-against gaps; see the Checks sheet in the standings file."),
    ("Min. games", f"Summaries and the Top 100 use player seasons with at least {MIN_GP} games."),
]


def describe(values):
    values = sorted(v for v in values.dropna())
    if not values:
        return [0, None, None, None]
    p90 = statistics.quantiles(values, n=10)[-1] if len(values) >= 10 else values[-1]
    return [len(values), round(statistics.mean(values), 4), round(statistics.median(values), 4), round(p90, 4)]


def summarize(df, keys, measures):
    """Players, average, median and 90th percentile of each measure, per group."""
    rows = []
    for key, g in df.groupby(keys, sort=True):
        line = list(key if isinstance(key, tuple) else (key,))
        for i, m in enumerate(measures):
            d = describe(g[m])
            line += d if i == 0 else d[1:]
        rows.append(line)
    header = list(keys) + ["players"] + [f"{m}_{s}" for m in measures for s in ("avg", "median", "p90")]
    return pd.DataFrame(rows, columns=header)


def report_breakdown(conn):
    seasons = query_df(conn, """
        SELECT full_name, league, season, start_year, position, position_group, teams,
               share_group, gp, goals, assists, points, points_per_game,
               goal_share_gp_adj, assist_share_gp_adj, point_share_gp_adj,
               best_team_scoring_rank, point_share_pctile, ppg_pctile,
               playoff_gp, playoff_goals, playoff_assists, playoff_points
        FROM v_player_season_shares
        ORDER BY league, start_year, point_share_gp_adj DESC""")
    splits = query_df(conn, """
        SELECT full_name, league, season, team, position, conference, division, share_group,
               gp, goals, assists, points, team_gp, team_gf, team_scoring_rank,
               goal_share, assist_share, point_share,
               goal_share_gp_adj, assist_share_gp_adj, point_share_gp_adj,
               group_teams, group_gf, group_goal_share, group_assist_share, group_point_share,
               point_share_vs_group_avg
        FROM v_goal_shares
        ORDER BY league, start_year, team, points DESC""")
    league_gf = query_df(conn, """
        SELECT l.code AS league, s.label AS season,
               ROUND(SUM(ts.gf)::NUMERIC / NULLIF(SUM(ts.gp), 0), 2) AS league_goals_per_team_game
        FROM team_seasons ts JOIN leagues l USING (league_id) JOIN seasons s USING (season_id)
        GROUP BY l.code, s.label""")
    if seasons.empty:
        raise SystemExit("No goal-share data found. Run load_database.py (with the standings file) first.")
    seasons = position_filter(seasons)
    seasons["point_share_pctile"] = percent_rank(seasons, "point_share_gp_adj", "season")
    seasons["ppg_pctile"] = percent_rank(seasons, "points_per_game", "season")
    splits["position_group"] = np.where(splits["position"].str.startswith("D", na=False), "D",
                                        np.where(splits["position"].isna(), None, "F"))
    splits = position_filter(splits).drop(columns="position_group")

    qualified = seasons[seasons["gp"] >= MIN_GP].copy()
    measures = ["point_share_gp_adj", "goal_share_gp_adj", "assist_share_gp_adj", "points_per_game"]
    top = qualified.sort_values("point_share_gp_adj", ascending=False, kind="stable").head(100).reset_index(drop=True)
    top.insert(0, "rank", top.index + 1)
    by_season = summarize(qualified, ["league", "season"], measures).merge(
        league_gf, on=["league", "season"], how="left")
    by_position = summarize(qualified, ["league", "position_group"], measures)
    start = qualified["start_year"].astype(int)
    era_start = start - (start - 1985) % 5
    qualified["era"] = era_start.astype(str) + "-" + (era_start + 4).astype(str)
    by_era = summarize(qualified, ["league", "era"], ["points_per_game", "point_share_gp_adj"])

    share_cols = [c for c in set(seasons.columns) | set(splits.columns)
                  if "share" in c and c != "share_group" and "pctile" not in c]
    path = save_workbook(out_path("CHL Goal Share Breakdown.xlsx"), {
        "Notes": sheet(pd.DataFrame(BREAKDOWN_NOTES, columns=["Measure", "Meaning"])),
        "Player Seasons": sheet(seasons),
        "Team Splits": sheet(splits),
        "Top 100": sheet(top),
        "By League-Season": sheet(by_season),
        "By Position": sheet(by_position),
        "Era Comparison": sheet(by_era),
    }, wide={"Notes": {"A": 26, "B": 110}},
        percent_cols={s: share_cols for s in ("Player Seasons", "Team Splits", "Top 100")})

    print(f"Saved {path.name}: {len(seasons):,} player seasons, {POSITION_LABELS[POSITION]} "
          f"({len(qualified):,} with {MIN_GP}+ games)")
    print(f"  Top 5 share of team scoring (GP-adjusted, {MIN_GP}+ games):")
    for r in top.head(5).itertuples():
        print(f"   {r.rank}. {r.full_name:<24} {r.league:<6} {r.season}  {r.points:>3} pts  "
              f"{r.point_share_gp_adj:.1%} of team goals")


# =====================================================================
# Report 2: CHL to NHL correlation
# =====================================================================

def correlation_matrix(df, method):
    out = pd.DataFrame(index=list(CHL_MEASURES), columns=list(NHL_MEASURES), dtype=float)
    for c in CHL_MEASURES:
        for n in NHL_MEASURES:
            pair = df[[c, n]].dropna()
            out.loc[c, n] = pair[c].corr(pair[n], method=method) if len(pair) > 10 else np.nan
    return out


def auc(scores, labels):
    """Area under the ROC curve: chance a random 200+ GP player outscores a random other prospect."""
    pos, neg = scores[labels], scores[~labels]
    if len(pos) == 0 or len(neg) == 0:
        return np.nan
    return stats.mannwhitneyu(pos, neg, alternative="two-sided").statistic / (len(pos) * len(neg))


def heatmap_png(matrix, path):
    labels_r = [CHL_MEASURES[i] for i in matrix.index]
    labels_c = [NHL_MEASURES[c].replace(" (", "\n(") for c in matrix.columns]
    fig, ax = plt.subplots(figsize=(10, 7.5), dpi=150)
    vals = matrix.values.astype(float)
    ax.imshow(vals, cmap=LinearSegmentedColormap.from_list("div", DIVERGING), vmin=-0.8, vmax=0.8,
              aspect="auto")
    for i in range(vals.shape[0]):
        for j in range(vals.shape[1]):
            if not np.isnan(vals[i, j]):
                ax.text(j, i, f"{vals[i, j]:.2f}", ha="center", va="center", fontsize=9,
                        color="white" if abs(vals[i, j]) > 0.45 else INK)
    ax.set_xticks(range(len(labels_c)), labels_c, fontsize=9, color=INK)
    ax.set_yticks(range(len(labels_r)), labels_r, fontsize=9, color=INK)
    ax.xaxis.tick_top()
    ax.set_xticks(np.arange(-.5, vals.shape[1]), minor=True)
    ax.set_yticks(np.arange(-.5, vals.shape[0]), minor=True)
    ax.grid(which="minor", color="white", linewidth=2)
    ax.tick_params(which="both", length=0)
    for s in ax.spines.values():
        s.set_visible(False)
    ax.set_title("How CHL stats correlate with NHL careers", loc="left", fontsize=12, color=INK, pad=40)
    fig.text(0.01, 0.01, "Spearman rank correlation. Blue = higher CHL value goes with more NHL success; "
             f"red = less. CHL {POSITION_LABELS[POSITION]} whose last CHL season began by "
             f"{LAST_COHORT_SEASON}; non-NHL players count as 0.", fontsize=8, color=INK_MUTED)
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    fig.savefig(path)
    plt.close(fig)


def deciles_png(table, path):
    fig, ax = plt.subplots(figsize=(9, 4.8), dpi=150)
    x, y = table["decile"].astype(int), table["pct_200_plus_gp"] * 100
    ax.bar(x, y, width=0.6, color=BLUE)
    for xi, yi in zip(x, y):
        if xi in (1, 5, 10):
            ax.text(xi, yi + 0.8, f"{yi:.0f}%", ha="center", fontsize=9, color=INK)
    ax.set_xticks(x, [f"D{v}" for v in x])
    ax.set_ylabel("Share who played 200+ NHL games (%)", color=INK_MUTED, fontsize=9)
    ax.set_xlabel("CHL team point share decile (D1 = lowest, D10 = highest)", color=INK_MUTED, fontsize=9)
    ax.yaxis.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    style_axes(ax)
    ax.set_title("Higher share of team scoring in the CHL, more NHL careers", loc="left", fontsize=12, color=INK)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def report_correlation(cohort, all_prospects):
    spearman = correlation_matrix(cohort, "spearman")
    pearson = correlation_matrix(cohort, "pearson")
    names = {**CHL_MEASURES, **NHL_MEASURES}
    full = cohort[list(names)].corr(method="spearman").rename(index=names, columns=names)

    present = [p for p in ("F", "D") if (cohort["position_group"] == p).sum() >= 30]
    by_pos = pd.DataFrame({
        pos: [cohort.loc[cohort["position_group"] == pos, [c, "nhl_gp"]].corr(method="spearman").iat[0, 1]
              for c in CHL_MEASURES]
        for pos in present}, index=list(CHL_MEASURES.values()))
    by_pos.insert(0, "All", spearman["nhl_gp"].values)

    labels = cohort["nhl_200_gp"].to_numpy()
    aucs = pd.DataFrame({
        "measure": list(CHL_MEASURES.values()),
        "auc_200_plus_nhl_gp": [auc(cohort[c].to_numpy(), labels) for c in CHL_MEASURES],
    }).sort_values("auc_200_plus_nhl_gp", ascending=False)

    d = cohort.dropna(subset=["team_point_share"]).copy()
    d["decile"] = pd.qcut(d["team_point_share"].rank(method="first"), 10, labels=range(1, 11))
    dec = d.groupby("decile", observed=True).agg(
        prospects=("nhl_gp", "size"),
        team_point_share_from=("team_point_share", "min"),
        team_point_share_to=("team_point_share", "max"),
        pct_reached_nhl=("made_nhl", "mean"),
        pct_200_plus_gp=("nhl_200_gp", "mean"),
        avg_nhl_gp=("nhl_gp", "mean"),
        avg_nhl_points=("nhl_points", "mean"),
    ).reset_index()

    heat = out_path("CHL to NHL Correlation Heatmap.png")
    bars = out_path("CHL Point Share Deciles.png")
    ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
    heatmap_png(spearman, heat)
    deciles_png(dec, bars)

    r = spearman["nhl_gp"]
    raw_best = r[["chl_goals", "chl_assists", "chl_points", "chl_points_per_game"]].idxmax()
    adj_best = r[["team_goal_share", "team_assist_share", "team_point_share",
                  "peak_point_share", "peak_point_share_pctile"]].idxmax()
    summary = [
        ("Prospects analysed", f"{len(cohort):,} ({POSITION_LABELS[POSITION]})"),
        ("...who reached the NHL", int(cohort["made_nhl"].sum())),
        ("...who played 200+ NHL games", int(cohort["nhl_200_gp"].sum())),
        ("Cohort", f"Last CHL top-100 season began 1985 to {LAST_COHORT_SEASON}"),
        ("Best raw CHL measure for NHL games", f"{CHL_MEASURES[raw_best]} (Spearman {r[raw_best]:.2f})"),
        ("Best team-adjusted measure for NHL games", f"{CHL_MEASURES[adj_best]} (Spearman {r[adj_best]:.2f})"),
        ("Points per game vs. team point share", f"{r['chl_points_per_game']:.2f} vs. {r['team_point_share']:.2f}"),
        ("Best single predictor of 200+ NHL games (AUC)",
         f"{aucs.iloc[0]['measure']} ({aucs.iloc[0]['auc_200_plus_nhl_gp']:.3f}; 0.5 = coin flip, 1.0 = perfect)"),
        ("", ""),
        ("How to read a correlation", "1 = perfect, 0 = no relationship, negative = opposite. In sports "
                                      "prediction, 0.3 is meaningful and 0.5 is strong."),
        ("Spearman", "Compares rankings rather than raw values, so a few superstars or the many "
                     "players with 0 NHL games don't distort it. Used for the main matrix."),
        ("Limits", "Only seasons in each league's top-100 scorers are included, so most defencemen "
                   "and lower scorers are missing; CHL totals partly reflect how many top-100 seasons "
                   "a player had. NHL links are by name plus birth year; a few may be wrong."),
    ]
    nice = lambda m: m.rename(index=CHL_MEASURES, columns=NHL_MEASURES).round(3)
    cols = ["name", "league", "position_group", "first_chl_season", "last_chl_season",
            "chl_top100_seasons"] + list(CHL_MEASURES) + ["nhl_name", "birth_date", "draft_overall"] \
        + list(NHL_MEASURES)
    path = save_workbook(out_path("CHL to NHL Correlation.xlsx"), {
        "Summary": sheet(pd.DataFrame(summary, columns=["Item", "Result"])),
        "Correlation Matrix": sheet(nice(spearman), index=True),
        "Pearson Matrix": sheet(nice(pearson), index=True),
        "Full Matrix": sheet(full.round(3), index=True),
        "By Position": sheet(by_pos.round(3), index=True),
        "Predict 200 GP": sheet(aucs.round(3)),
        "Deciles": sheet(dec.round(4)),
        "Prospects": sheet(all_prospects.sort_values("nhl_gp", ascending=False)[cols].round(4)),
    }, images={"Correlation Matrix": (heat, "A16"), "Deciles": (bars, "A14")},
        wide={"Summary": {"B": 100}})

    print(f"Saved {path.name}: {len(cohort):,} prospects ({POSITION_LABELS[POSITION]}), "
          f"{int(cohort['made_nhl'].sum()):,} reached the NHL")
    print("  Spearman correlation with NHL games played:")
    for c in r.sort_values(ascending=False).index:
        print(f"   {CHL_MEASURES[c]:<44}{r[c]:>6.2f}")


# =====================================================================
# Report 3: top-decile breakdown
# =====================================================================

def outcomes(group):
    nhl_regulars = group[group["nhl_gp"] >= MIN_NHL_GP_FOR_PPG]
    return pd.Series({
        "prospects": len(group),
        "pct_reached_nhl": group["made_nhl"].mean(),
        "pct_200_plus_gp": group["nhl_200_gp"].mean(),
        "avg_nhl_gp": group["nhl_gp"].mean(),
        "median_nhl_gp": group["nhl_gp"].median(),
        "avg_nhl_points": group["nhl_points"].mean(),
        f"nhl_ppg_({MIN_NHL_GP_FOR_PPG}+_gp)": nhl_regulars["nhl_points_per_game"].mean(),
    })


def decile_chart(table, path):
    series = {"team_point_share": (BLUE, "o", 6), "chl_points_per_game": (ORANGE, "s", -18)}
    fig, ax = plt.subplots(figsize=(9, 5), dpi=150)
    for m, (color, marker, dy) in series.items():
        t = table[table["measure"] == DECILE_MEASURES[m]]
        x, y = t["decile"].astype(int), t["pct_200_plus_gp"] * 100
        ax.plot(x, y, color=color, linewidth=2, marker=marker, markersize=7,
                markeredgecolor="white", markeredgewidth=2, label=DECILE_MEASURES[m])
        ax.annotate(f"{DECILE_MEASURES[m]}: {y.iloc[-1]:.0f}%", (x.iloc[-1], y.iloc[-1]),
                    xytext=(-14, dy), textcoords="offset points", ha="right", fontsize=9, color=INK)
    ax.set_xticks(range(1, 11), [f"D{d}" for d in range(1, 11)])
    ax.set_xlabel("CHL decile (D1 = lowest 10%, D10 = top 10%)", color=INK_MUTED, fontsize=9)
    ax.set_ylabel("Share who played 200+ NHL games (%)", color=INK_MUTED, fontsize=9)
    ax.yaxis.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.set_ylim(bottom=0)
    style_axes(ax)
    ax.legend(frameon=False, fontsize=9, loc="upper left")
    ax.set_title("NHL careers by CHL decile: team point share vs. points per game",
                 loc="left", fontsize=12, color=INK)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def report_deciles(cohort):
    df = cohort.copy()
    for m in DECILE_MEASURES:
        df[f"{m}_decile"] = pd.qcut(df[m].rank(method="first"), 10, labels=range(1, 11)).astype("Int64")

    parts = []
    for m, label in DECILE_MEASURES.items():
        t = df.groupby(f"{m}_decile", observed=True).apply(outcomes, include_groups=False).reset_index()
        t = t.rename(columns={f"{m}_decile": "decile"})
        rng = df.groupby(f"{m}_decile", observed=True)[m].agg(["min", "max"]).reset_index(drop=True)
        t.insert(1, "measure", label)
        t.insert(2, "range_from", rng["min"].values)
        t.insert(3, "range_to", rng["max"].values)
        parts.append(t)
    by_measure = pd.concat(parts, ignore_index=True)

    share_top = df["team_point_share_decile"] == 10
    ppg_top = df["chl_points_per_game_decile"] == 10
    groups = {f"Top 10%: {label}": df[df[f"{m}_decile"] == 10] for m, label in DECILE_MEASURES.items()}
    groups.update({
        "Top 10% in BOTH team point share and PPG": df[share_top & ppg_top],
        "Top 10% team point share, NOT top 10% PPG": df[share_top & ~ppg_top],
        "Top 10% PPG, NOT top 10% team point share": df[ppg_top & ~share_top],
        "Everyone else (bottom 90% in both)": df[~share_top & ~ppg_top],
        "All prospects": df,
    })
    compared = pd.DataFrame({k: outcomes(v) for k, v in groups.items()}).T.reset_index(names="group")

    top = df[share_top]
    by_group = []
    for col, label in (("position_group", "Position"), ("league", "League")):
        t = top.groupby(col).apply(outcomes, include_groups=False).reset_index().rename(columns={col: "group"})
        t.insert(0, "split", label)
        by_group.append(t[t["prospects"] >= 10])
    by_group = pd.concat(by_group, ignore_index=True)
    top_corr = correlation_matrix(top, "spearman").rename(index=CHL_MEASURES, columns=NHL_MEASURES).round(3)

    c = compared.set_index("group")
    share_only = c.loc["Top 10% team point share, NOT top 10% PPG"]
    ppg_only = c.loc["Top 10% PPG, NOT top 10% team point share"]
    top_share, everyone = c.loc["Top 10%: Team point share"], c.loc["All prospects"]
    summary = [
        ("Prospects analysed", f"{len(df):,} {POSITION_LABELS[POSITION]} (last CHL top-100 season began "
                               f"1985 to {LAST_COHORT_SEASON})"),
        ("Top 10% by team point share",
         f"{int(top_share['prospects'])} players; {top_share['pct_reached_nhl']:.0%} reached the NHL and "
         f"{top_share['pct_200_plus_gp']:.0%} played 200+ games (all prospects: "
         f"{everyone['pct_reached_nhl']:.0%} and {everyone['pct_200_plus_gp']:.0%})"),
        ("Top 10% team share but NOT top 10% PPG",
         f"{int(share_only['prospects'])} players; {share_only['pct_200_plus_gp']:.0%} played 200+ NHL games. "
         "These are players team share flags that raw scoring rate misses."),
        ("Top 10% PPG but NOT top 10% team share",
         f"{int(ppg_only['prospects'])} players; {ppg_only['pct_200_plus_gp']:.0%} played 200+ NHL games."),
        ("How to read it", "If the share-only group beats the PPG-only group, team share adds information "
                           "beyond points per game. If not, PPG already captures it."),
        ("Note", "Points-per-game deciles mix eras (1980s scoring was higher), while team share is "
                 "era-neutral. Best-season percentile is ranked within each league season."),
    ]
    player_cols = ["name", "league", "position_group", "first_chl_season", "last_chl_season",
                   "chl_gp", "chl_points", "chl_points_per_game", "team_point_share",
                   "team_goal_share", "team_assist_share", "chl_points_per_game_decile",
                   "nhl_name", "draft_overall", "nhl_gp", "nhl_goals", "nhl_assists", "nhl_points"]

    png = out_path("CHL Decile Comparison.png")
    ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
    decile_chart(by_measure, png)
    path = save_workbook(out_path("CHL Top Decile Breakdown.xlsx"), {
        "Summary": sheet(pd.DataFrame(summary, columns=["Item", "Result"])),
        "Deciles by Measure": sheet(by_measure.round(4)),
        "Top 10% Compared": sheet(compared.round(4)),
        "Top 10% by Group": sheet(by_group.round(4)),
        "Top 10% Correlations": sheet(top_corr, index=True),
        "Top 10% Players": sheet(top.sort_values("nhl_gp", ascending=False)[player_cols].round(4)),
    }, images={"Top 10% Compared": (png, "A13")}, wide={"Summary": {"B": 110}})

    print(f"Saved {path.name}")
    print(compared[["group", "prospects", "pct_reached_nhl", "pct_200_plus_gp", "avg_nhl_gp"]].to_string(
        index=False, formatters={"pct_reached_nhl": "{:.0%}".format, "pct_200_plus_gp": "{:.0%}".format,
                                 "avg_nhl_gp": "{:.0f}".format, "prospects": "{:.0f}".format}))


# =====================================================================

def main():
    parser = add_db_args(argparse.ArgumentParser(description="CHL goal-share and CHL-to-NHL reports."))
    parser.add_argument("--report", choices=["breakdown", "correlation", "deciles"],
                        help="run just one report (default: all three)")
    parser.add_argument("--position", choices=["F", "D", "all"], default="F",
                        help="F = forwards (default), D = defencemen, all = every position")
    args = parser.parse_args()
    global POSITION
    POSITION = args.position
    print(f"Players: {POSITION_LABELS[POSITION]}\n")
    with connect(db_settings(args), args.db) as conn:
        if args.report in (None, "breakdown"):
            report_breakdown(conn)
            print()
        if args.report in (None, "correlation", "deciles"):
            prospects, cohort = load_prospects(conn)
            if args.report in (None, "correlation"):
                report_correlation(cohort, prospects)
                print()
            if args.report in (None, "deciles"):
                report_deciles(cohort)


if __name__ == "__main__":
    main()
