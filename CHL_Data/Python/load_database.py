"""
Build the chl_stats PostgreSQL database from all the project data. Run this
after collect_data.py, and again whenever any data changes.

Loads, in order:
    1. CHL player stats: the Elite Prospects top-100 files in CHL_Data/WHL, /OHL, /QMJHL
       (e.g. "2000-2001 OHL Player Stats Regular Season.xlsx"; other files are skipped)
    2. CHL standings (Wikipedia), matched to the team names in the player files
    3. NHL careers, with each CHL player season linked to its NHL player
    4. NHL draft picks 1986-2016, each linked to the player's draft-year CHL season

The table design is in schema.sql. The script rebuilds every table from scratch,
in one transaction, so re-running it always gives a clean copy.

Run from VS Code (open the file, click Run) or a terminal:
    python load_database.py
The PostgreSQL password comes from PGPASSWORD or is asked for.
"""

import argparse
import difflib
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path

import openpyxl
import psycopg

from common import (CHL_DATA_DIR, DRAFT_FILE, LEAGUES, NHL_CAREERS_FILE, PYTHON_DIR, STANDINGS_FILE,
                    add_db_args, db_settings, first_names_compatible, person_key)
GAME_TYPES = {"Regular Season": "regular", "Playoffs": "playoffs"}

FILE_PATTERN = re.compile(
    r"^(\d{4})-(\d{4}) (WHL|OHL|QMJHL) Player Stats (Regular Season|Playoffs)\.xlsx$"
)
NAME_POSITION = re.compile(r"^(.*?)\s*\(([^)]*)\)\s*$")   # "Jason Spezza (C)"
EXPECTED_HEADER = ["#", "Player", "Team", "GP", "G", "A", "TP", "P/GP", "PIM", "+/-"]



# ---------------------------------------------------------------------
# Reading and cleaning the Excel files
# ---------------------------------------------------------------------

def to_int(value):
    """Convert a cell like '35', '-5', '' or None to an int or None."""
    if value is None:
        return None
    text = str(value).strip()
    if text == "":
        return None
    return int(float(text))


def clean_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def stat_line(row):
    """GP, G, A, TP, PIM, +/- from a data row (P/GP is recalculated in SQL)."""
    return {
        "gp": to_int(row[3]),
        "goals": to_int(row[4]),
        "assists": to_int(row[5]),
        "points": to_int(row[6]),
        "pim": to_int(row[8]),
        "plus_minus": to_int(row[9]),
    }


def parse_file(path):
    """
    Return a list of player records from one export file:
        {name, position, rank, totals: {...}, teams: [(team_name, {...}), ...]}

    In the exports, a traded player's main line shows team 'totals' and is
    followed by one unranked line per team. Everyone else has a single line.
    """
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    rows = list(wb.worksheets[0].iter_rows(values_only=True))
    wb.close()

    header_idx = next(
        (i for i, r in enumerate(rows) if r and clean_text(r[0]) == "#"), None
    )
    if header_idx is None:
        raise ValueError("header row starting with '#' not found")
    header = [clean_text(c) for c in rows[header_idx][:10]]
    if header != EXPECTED_HEADER:
        raise ValueError(f"unexpected columns: {header}")

    players, current = [], None
    for row in rows[header_idx + 1:]:
        if row is None or all(clean_text(c) == "" for c in row):
            continue
        row = list(row) + [None] * (10 - len(row))
        rank, player_cell, team = clean_text(row[0]), clean_text(row[1]), clean_text(row[2])

        if rank:                                   # a ranked player line
            match = NAME_POSITION.match(player_cell)
            name, position = (match.group(1), match.group(2)) if match else (player_cell, None)
            current = {
                "name": clean_text(name),
                "position": position or None,
                "rank": to_int(rank.rstrip(".")),
                "totals": stat_line(row),
                "teams": [],
            }
            players.append(current)
            if team.lower() != "totals":           # single-team season
                current["teams"].append((team, stat_line(row)))
        elif current is not None and team:         # team split of a traded player
            current["teams"].append((team, stat_line(row)))

    for p in players:                              # merge repeated lines for the same team
        p["teams"] = merge_team_lines(p["teams"])
    return players


def merge_team_lines(team_lines):
    """Some exports list the same team twice for one player; add those lines together."""
    merged = {}
    for team, stats in team_lines:
        if team not in merged:
            merged[team] = dict(stats)
            continue
        for key, value in stats.items():
            if value is not None:
                merged[team][key] = (merged[team][key] or 0) + value
    return list(merged.items())


def find_files(data_dir):
    """Yield (path, start_year, end_year, league, game_type); report skipped files."""
    found, skipped = [], []
    for league in LEAGUES:
        folder = data_dir / league
        if not folder.is_dir():
            print(f"  ! Folder not found, skipping: {folder}")
            continue
        for path in sorted(folder.glob("*.xlsx")):
            m = FILE_PATTERN.match(path.name)
            if not m or m.group(3) != league:
                skipped.append(path)
                continue
            found.append((path, int(m.group(1)), int(m.group(2)), league, GAME_TYPES[m.group(4)]))
    return found, skipped


# ---------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------

def ensure_database(conn_args, db_name):
    """Create the database if it doesn't exist yet."""
    with psycopg.connect(**conn_args, dbname="postgres", autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (db_name,)
        ).fetchone()
        if not exists:
            conn.execute(f'CREATE DATABASE "{db_name}"')
            print(f"Created database '{db_name}'")


def load(conn, files):
    cur = conn.cursor()
    league_ids, season_ids, team_ids, player_ids = {}, {}, {}, {}
    seen_keys = set()          # (player_id, season_id, league_id, game_type)
    warnings = []
    counts = Counter()

    for code, name in LEAGUES.items():
        league_ids[code] = cur.execute(
            "INSERT INTO leagues (code, name) VALUES (%s, %s) RETURNING league_id",
            (code, name),
        ).fetchone()[0]

    def season_id(start, end):
        if start not in season_ids:
            season_ids[start] = cur.execute(
                "INSERT INTO seasons (start_year, label) VALUES (%s, %s) RETURNING season_id",
                (start, f"{start}-{end}"),
            ).fetchone()[0]
        return season_ids[start]

    def team_id(league, team_name):
        key = (league, team_name)
        if key not in team_ids:
            team_ids[key] = cur.execute(
                "INSERT INTO teams (league_id, name) VALUES (%s, %s) RETURNING team_id",
                (league_ids[league], team_name),
            ).fetchone()[0]
        return team_ids[key]

    def player_id(name):
        if name not in player_ids:
            player_ids[name] = cur.execute(
                "INSERT INTO players (full_name) VALUES (%s) RETURNING player_id", (name,)
            ).fetchone()[0]
        return player_ids[name]

    for path, start, end, league, game_type in files:
        try:
            records = parse_file(path)
        except Exception as exc:  # report and keep going with the other files
            warnings.append(f"{path.name}: could not read ({exc})")
            continue

        sid, lid = season_id(start, end), league_ids[league]
        for rec in records:
            t = rec["totals"]
            if None in (t["gp"], t["goals"], t["assists"], t["points"]):
                warnings.append(f"{path.name}: {rec['name']} has missing GP/G/A/TP, skipped")
                continue
            if t["goals"] + t["assists"] != t["points"]:
                warnings.append(f"{path.name}: {rec['name']} G + A != TP")
            split_points = sum((s["points"] or 0) for _, s in rec["teams"])
            if rec["teams"] and split_points != t["points"]:
                warnings.append(f"{path.name}: {rec['name']} team splits don't add up "
                                f"to the total ({split_points} vs {t['points']} pts)")

            # Two different players with the same name in the same league and
            # season: keep them apart by adding their team to the name.
            name = rec["name"]
            if (player_ids.get(name), sid, lid, game_type) in seen_keys:
                team_label = rec["teams"][0][0] if rec["teams"] else "unknown team"
                name = f"{name} [{team_label}]"
                warnings.append(f"{path.name}: duplicate name, stored as '{name}'")
            pid = player_id(name)
            seen_keys.add((pid, sid, lid, game_type))

            ps_id = cur.execute(
                """INSERT INTO player_seasons
                       (player_id, season_id, league_id, game_type, scoring_rank, position,
                        gp, goals, assists, points, pim, plus_minus)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   RETURNING player_season_id""",
                (pid, sid, lid, game_type, rec["rank"], rec["position"],
                 t["gp"], t["goals"], t["assists"], t["points"], t["pim"], t["plus_minus"]),
            ).fetchone()[0]
            counts["player_seasons"] += 1

            for team_name, s in rec["teams"]:
                cur.execute(
                    """INSERT INTO player_season_teams
                           (player_season_id, team_id, gp, goals, assists, points, pim, plus_minus)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    (ps_id, team_id(league, team_name), s["gp"] or 0, s["goals"] or 0,
                     s["assists"] or 0, s["points"] or 0, s["pim"], s["plus_minus"]),
                )
                counts["team_splits"] += 1
        counts["files"] += 1

    counts["players"], counts["teams"], counts["seasons"] = len(player_ids), len(team_ids), len(season_ids)
    ids = {"leagues": league_ids, "seasons": season_ids, "teams": team_ids}
    return counts, warnings, ids


# ---------------------------------------------------------------------
# Team standings (Wikipedia) and matching team names
# ---------------------------------------------------------------------

# Spellings that differ between Elite Prospects and Wikipedia
TEAM_ALIASES = [
    (r"\bsoo\b", "sault ste marie"),
    (r"\bjr\b", "junior"),
    (r"\bprince edward island\b", "pei"),
    (r"\bdukes of hamilton\b", "hamilton dukes"),
]


def team_key(name):
    """Simplify a team name for matching: no accents, punctuation, case or spaces."""
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    text = re.sub(r"[^a-z0-9 ]", " ", text.replace(".", ""))
    for pattern, replacement in TEAM_ALIASES:
        text = re.sub(pattern, replacement, text)
    return re.sub(r"\s+", "", text)


def match_teams(ep_names, wiki_names):
    """
    Pair Elite Prospects team names with Wikipedia team names for one league season.
    Returns ({ep_name: wiki_name}, notes). Exact matches (after simplifying) first,
    then the closest remaining name if it is clearly the best candidate.
    """
    matches, notes = {}, []
    wiki_by_key = {team_key(w): w for w in wiki_names}
    leftover = []
    for ep in ep_names:
        wiki = wiki_by_key.pop(team_key(ep), None)
        if wiki:
            matches[ep] = wiki
        else:
            leftover.append(ep)
    for ep in leftover:
        scored = sorted(((difflib.SequenceMatcher(None, team_key(ep), k).ratio(), k)
                         for k in wiki_by_key), reverse=True)
        if scored and scored[0][0] >= 0.6 and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.1):
            wiki = wiki_by_key.pop(scored[0][1])
            matches[ep] = wiki
            notes.append(f"matched '{ep}' to '{wiki}' by similarity ({scored[0][0]:.2f})")
        else:
            notes.append(f"no standings match for '{ep}', its players get no goal shares")
    return matches, notes


def load_standings(conn, path, ids):
    """Load the Wikipedia standings workbook into team_seasons and link it to teams."""
    rows = list(openpyxl.load_workbook(path, read_only=True).active.iter_rows(values_only=True))
    header = [str(h) for h in rows[0]]
    records = [dict(zip(header, r)) for r in rows[1:] if r and r[0]]

    # Elite Prospects team names seen in each league season of the player files
    ep_teams = {}
    for (league, name), tid in ids["teams"].items():
        ep_teams.setdefault(league, {})[name] = tid
    seasons_played = {}
    for league, start, names in conn.execute(
        """SELECT l.code, s.start_year, ARRAY_AGG(DISTINCT t.name)
           FROM player_season_teams pst
           JOIN player_seasons ps ON ps.player_season_id = pst.player_season_id
           JOIN teams t ON t.team_id = pst.team_id
           JOIN leagues l ON l.league_id = ps.league_id
           JOIN seasons s ON s.season_id = ps.season_id
           GROUP BY l.code, s.start_year"""):
        seasons_played[(league, start)] = names

    by_season = {}
    for r in records:
        start = int(str(r["season"])[:4])
        by_season.setdefault((r["league"], start), []).append(r)

    notes, counts = [], Counter()
    for (league, start), teams in sorted(by_season.items()):
        sid = ids["seasons"].get(start)
        lid = ids["leagues"].get(league)
        if sid is None or lid is None:
            continue                                   # no player files for this season
        matches, match_notes = match_teams(seasons_played.get((league, start), []),
                                           [t["team"] for t in teams])
        notes += [f"{league} {start}-{start + 1}: {n}" for n in match_notes]
        wiki_to_tid = {w: ep_teams[league][ep] for ep, w in matches.items()}
        for t in teams:
            group = t["conference"] or t["division"] or "League"
            pts = t["pts"] if t["pts"] is not None else (
                2 * (t["w"] or 0) + (t["t"] or 0) + (t["otl"] or 0) + (t["sol"] or 0))
            record_pct = round(pts / (2 * t["gp"]), 4) if t["gp"] else None
            conn.execute(
                """INSERT INTO team_seasons
                       (league_id, season_id, team_id, wiki_team_name, conference, division,
                        share_group, gp, w, l, t, otl, sol, pts, gf, ga, record_pct)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (lid, sid, wiki_to_tid.get(t["team"]), t["team"], t["conference"], t["division"],
                 group, t["gp"], t["w"], t["l"], t["t"], t["otl"], t["sol"], t["pts"],
                 t["gf"], t["ga"], record_pct),
            )
            counts["team_seasons"] += 1
        counts["matched"] += len(matches)
    return counts, notes


# ---------------------------------------------------------------------
# NHL careers and matching CHL player seasons to NHL players
# ---------------------------------------------------------------------

def load_nhl(conn, path):
    """Load NHL careers, then link each CHL player season to an NHL player where possible."""
    rows = list(openpyxl.load_workbook(path, read_only=True).active.iter_rows(values_only=True))
    header = [str(h) for h in rows[0]]
    nhl = [dict(zip(header, r)) for r in rows[1:] if r and r[0]]
    cols = ["nhl_player_id", "full_name", "birth_date", "birth_country", "nationality", "position",
            "shoots", "height_in", "weight_lb", "draft_year", "draft_round", "draft_overall",
            "first_nhl_season", "nhl_gp", "nhl_goals", "nhl_assists", "nhl_points", "hall_of_fame"]
    for p in nhl:
        p["hall_of_fame"] = p.get("hall_of_fame") == "Y"
        conn.execute(f"INSERT INTO nhl_players ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})",
                     [p.get(c) for c in cols])

    by_name, by_last = {}, {}
    for p in nhl:
        key = person_key(p["full_name"])
        p["_birth_year"] = int(str(p["birth_date"])[:4]) if p.get("birth_date") else None
        p["_first_year"] = int(str(p["first_nhl_season"])[:4]) if p.get("first_nhl_season") else None
        by_name.setdefault(key, []).append(p)
        by_last.setdefault(key.split()[-1], []).append(p)

    def plausible(p, season_start):
        """Could this NHL player have played junior in this season?"""
        if p["_birth_year"] is not None and not 15 <= season_start - p["_birth_year"] <= 21:
            return False
        if p["_first_year"] is not None and p["_first_year"] < season_start - 3:
            return False                             # already an NHL regular years before
        return True

    counts, notes = Counter(), []
    for ps_id, name, start in conn.execute(
            """SELECT ps.player_season_id, p.full_name, s.start_year
               FROM player_seasons ps JOIN players p USING (player_id)
               JOIN seasons s USING (season_id)""").fetchall():
        key = person_key(name)
        exact = [p for p in by_name.get(key, []) if plausible(p, start)]
        if len(exact) == 1:
            match, method = exact[0], "exact name"
        elif len(exact) > 1:
            counts["ambiguous"] += 1
            notes.append(f"{name} {start}: {len(exact)} NHL players with this name and a fitting age")
            continue
        else:
            first, last = key.split()[0], key.split()[-1]
            variants = [p for p in by_last.get(last, [])
                        if first_names_compatible(first, person_key(p["full_name"]).split()[0])
                        and plausible(p, start)]
            if len(variants) != 1:
                counts["no NHL match"] += 1
                continue
            match, method = variants[0], "name variant"
        conn.execute("INSERT INTO player_season_nhl VALUES (%s, %s, %s)",
                     (ps_id, match["nhl_player_id"], method))
        counts[method] += 1
    counts["nhl_players"] = len(nhl)
    return counts, notes


# ---------------------------------------------------------------------
# NHL draft picks, linked to each player's draft-year CHL season
# ---------------------------------------------------------------------

def load_draft(conn, path):
    rows = list(openpyxl.load_workbook(path, read_only=True).active.iter_rows(values_only=True))
    header = [str(h) for h in rows[0]]
    picks = [dict(zip(header, r)) for r in rows[1:] if r and r[0]]
    picks_per_year = Counter()
    for p in picks:
        picks_per_year[p["draft_year"]] = max(picks_per_year[p["draft_year"]], p["overall_pick"])

    # Draft-year seasons: regular season ending in the draft year, by league
    seasons = {}
    for ps_id, name, league, start in conn.execute(
            """SELECT ps.player_season_id, p.full_name, l.code, s.start_year
               FROM player_seasons ps JOIN players p USING (player_id)
               JOIN leagues l USING (league_id) JOIN seasons s USING (season_id)
               WHERE ps.game_type = 'regular'""").fetchall():
        seasons.setdefault((league, start), []).append((person_key(name), ps_id))

    counts = Counter()
    for p in picks:
        year, pick = p["draft_year"], p["overall_pick"]
        decile = min(10, max(1, -(-10 * pick // picks_per_year[year])))      # ceiling
        ps_id, method = None, None
        pool = seasons.get((p.get("amateur_league"), year - 1), [])
        key = person_key(p.get("player"))
        if pool and key:
            exact = [i for k, i in pool if k == key]
            if len(exact) == 1:
                ps_id, method = exact[0], "exact name"
            elif not exact:
                first, last = key.split()[0], key.split()[-1]
                variants = [i for k, i in pool if k.split()[-1] == last
                            and first_names_compatible(first, k.split()[0])]
                if len(variants) == 1:
                    ps_id, method = variants[0], "name variant"
        counts[method or ("no match" if p.get("amateur_league") in LEAGUES else "not from CHL")] += 1
        conn.execute(
            """INSERT INTO draft_picks
                   (draft_year, round, pick_in_round, overall_pick, picks_that_year, draft_decile,
                    drafted_by, nhl_player_id, player, position, height_in, weight_lb, birth_date,
                    amateur_team, amateur_league, draft_season_id, match_method)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (year, p["round"], p.get("pick_in_round"), pick, picks_per_year[year], decile,
             p.get("drafted_by"), p.get("nhl_player_id"), p.get("player"), p.get("position"),
             p.get("height_in"), p.get("weight_lb"), p.get("birth_date"), p.get("amateur_team"),
             p.get("amateur_league"), ps_id, method))
    counts["picks"] = len(picks)
    return counts


def main():
    parser = argparse.ArgumentParser(description="Load CHL Excel stats into PostgreSQL.")
    parser.add_argument("--data-dir", type=Path, default=CHL_DATA_DIR,
                        help="CHL_Data folder containing WHL, OHL and QMJHL subfolders "
                             "(default: the folder above this script)")
    add_db_args(parser)
    parser.add_argument("--standings", type=Path, default=STANDINGS_FILE)
    parser.add_argument("--nhl", type=Path, default=NHL_CAREERS_FILE)
    parser.add_argument("--draft", type=Path, default=DRAFT_FILE)
    args = parser.parse_args()

    files, skipped = find_files(args.data_dir)
    print(f"Found {len(files)} stat files in {args.data_dir}")
    for path in skipped:
        print(f"  - skipped (not a regular season/playoffs file): {path.parent.name}/{path.name}")
    if not files:
        sys.exit("No files to load. Check --data-dir.")

    conn_args = db_settings(args)

    ensure_database(conn_args, args.db)
    schema_sql = (PYTHON_DIR / "schema.sql").read_text(encoding="utf-8")

    with psycopg.connect(**conn_args, dbname=args.db) as conn:   # one transaction
        conn.execute(schema_sql)
        counts, warnings, ids = load(conn, files)
        match_notes = []
        if args.standings.exists():
            standings_counts, match_notes = load_standings(conn, args.standings, ids)
            counts.update(standings_counts)
        else:
            print(f"\n  ! Standings file not found, skipping team goals: {args.standings}")
        nhl_counts = {}
        if args.nhl.exists():
            nhl_counts, nhl_notes = load_nhl(conn, args.nhl)
        else:
            print(f"\n  ! NHL careers file not found, skipping NHL links: {args.nhl}")
        draft_counts = {}
        if args.draft.exists():
            draft_counts = load_draft(conn, args.draft)
        else:
            print(f"\n  ! Draft file not found, skipping draft picks: {args.draft}")
        conn.commit()
        if nhl_counts:
            print("\nNHL matching (CHL player seasons):")
            for key in ("exact name", "name variant", "ambiguous", "no NHL match"):
                print(f"  {key:<15}{nhl_counts.get(key, 0):>7,}")
        if draft_counts:
            print(f"\nDraft picks: {draft_counts['picks']:,}, linked to draft-year CHL seasons:")
            for key in ("exact name", "name variant", "no match", "not from CHL"):
                print(f"  {key:<15}{draft_counts.get(key, 0):>7,}")

        print("\nLoaded:")
        for key in ("files", "seasons", "teams", "players", "player_seasons", "team_splits",
                    "team_seasons", "matched"):
            print(f"  {key:<15}{counts[key]:>7,}")
        if match_notes:
            print(f"\nTeam name matching ({len(match_notes)} note(s)):")
            for n in match_notes[:15]:
                print(f"  - {n}")
            if len(match_notes) > 15:
                print(f"  ... and {len(match_notes) - 15} more")

        if warnings:
            print(f"\n{len(warnings)} warning(s):")
            for w in warnings[:25]:
                print(f"  ! {w}")
            if len(warnings) > 25:
                print(f"  ... and {len(warnings) - 25} more")

        print("\nTop 5 career CHL regular-season points (in these files):")
        for row in conn.execute(
            """SELECT full_name, COUNT(*) AS seasons, SUM(points) AS points
               FROM v_player_season_stats WHERE game_type = 'regular'
               GROUP BY full_name ORDER BY points DESC LIMIT 5"""
        ):
            print(f"  {row[0]:<28}{row[1]:>3} seasons {row[2]:>5} pts")

    print(f"\nDone. Next: python chl_analysis.py and python draft_analysis.py")


if __name__ == "__main__":
    main()
