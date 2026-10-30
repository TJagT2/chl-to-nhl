-- =====================================================================
-- CHL player stats database (WHL, OHL, QMJHL), 1985-86 to 2015-16
-- Source: Elite Prospects top-100 scoring exports, one file per
--         league / season / game type (regular season or playoffs).
--
-- Re-runnable: drops and rebuilds every object, so running the loader
-- again always gives a clean copy of the data.
-- =====================================================================

DROP VIEW  IF EXISTS v_player_season_shares, v_goal_shares, v_player_season_stats CASCADE;
DROP TABLE IF EXISTS draft_picks, player_season_nhl, nhl_players, team_seasons, player_season_teams, player_seasons, players, teams, seasons, leagues CASCADE;

-- The three CHL member leagues
CREATE TABLE leagues (
    league_id   SMALLSERIAL PRIMARY KEY,
    code        VARCHAR(10) NOT NULL UNIQUE,      -- 'WHL', 'OHL', 'QMJHL'
    name        TEXT        NOT NULL
);

-- One row per hockey season, e.g. start_year 2000 = '2000-2001'
CREATE TABLE seasons (
    season_id   SMALLSERIAL PRIMARY KEY,
    start_year  SMALLINT    NOT NULL UNIQUE,
    label       CHAR(9)     NOT NULL UNIQUE
);

-- Teams belong to a league. Relocated / renamed franchises appear
-- under each name they played under.
CREATE TABLE teams (
    team_id     SERIAL      PRIMARY KEY,
    league_id   SMALLINT    NOT NULL REFERENCES leagues,
    name        TEXT        NOT NULL,
    UNIQUE (league_id, name)
);

-- One row per player. The source files have no player ID or birth
-- date, so players are matched across seasons by name.
CREATE TABLE players (
    player_id   SERIAL      PRIMARY KEY,
    full_name   TEXT        NOT NULL UNIQUE
);

-- A player's total line for one league, season and game type.
-- For traded players this is the combined line across all teams.
CREATE TABLE player_seasons (
    player_season_id SERIAL     PRIMARY KEY,
    player_id        INT        NOT NULL REFERENCES players,
    season_id        SMALLINT   NOT NULL REFERENCES seasons,
    league_id        SMALLINT   NOT NULL REFERENCES leagues,
    game_type        VARCHAR(10) NOT NULL CHECK (game_type IN ('regular', 'playoffs')),
    scoring_rank     SMALLINT   NOT NULL,          -- rank in that league's top-100
    position         VARCHAR(10),                  -- e.g. 'C', 'LW/RW', 'D'
    gp               SMALLINT   NOT NULL CHECK (gp >= 0),
    goals            SMALLINT   NOT NULL CHECK (goals >= 0),
    assists          SMALLINT   NOT NULL CHECK (assists >= 0),
    points           SMALLINT   NOT NULL CHECK (points >= 0),
    pim              SMALLINT,
    plus_minus       SMALLINT,                     -- not tracked in early seasons
    UNIQUE (player_id, season_id, league_id, game_type)
);

-- Team-by-team split of each player_seasons row. A player who stayed
-- on one team has a single row here; a traded player has one per team.
CREATE TABLE player_season_teams (
    player_season_id INT      NOT NULL REFERENCES player_seasons ON DELETE CASCADE,
    team_id          INT      NOT NULL REFERENCES teams,
    gp               SMALLINT NOT NULL,
    goals            SMALLINT NOT NULL,
    assists          SMALLINT NOT NULL,
    points           SMALLINT NOT NULL,
    pim              SMALLINT,
    plus_minus       SMALLINT,
    PRIMARY KEY (player_season_id, team_id)
);

CREATE INDEX idx_player_seasons_lookup ON player_seasons (season_id, league_id, game_type);
CREATE INDEX idx_player_seasons_player ON player_seasons (player_id);
CREATE INDEX idx_player_season_teams_team ON player_season_teams (team_id);

-- Flat, readable view for analysis: one row per player season with
-- names instead of IDs, points per game, and the team(s) played for.
CREATE VIEW v_player_season_stats AS
SELECT
    ps.player_season_id,
    p.player_id,
    p.full_name,
    l.code                                   AS league,
    s.label                                  AS season,
    s.start_year,
    ps.game_type,
    ps.scoring_rank,
    ps.position,
    ps.gp,
    ps.goals,
    ps.assists,
    ps.points,
    ROUND(ps.points::NUMERIC / NULLIF(ps.gp, 0), 2) AS points_per_game,
    ps.pim,
    ps.plus_minus,
    COUNT(pst.team_id)                       AS team_count,
    STRING_AGG(t.name, ' / ' ORDER BY t.name) AS teams
FROM player_seasons ps
JOIN players  p ON p.player_id = ps.player_id
JOIN leagues  l ON l.league_id = ps.league_id
JOIN seasons  s ON s.season_id = ps.season_id
LEFT JOIN player_season_teams pst ON pst.player_season_id = ps.player_season_id
LEFT JOIN teams t ON t.team_id = pst.team_id
GROUP BY ps.player_season_id, p.player_id, p.full_name, l.code, s.label, s.start_year;

-- =====================================================================
-- Team standings (source: Wikipedia season pages)
-- =====================================================================

-- One row per team per regular season, with the team's goals for/against
-- and its conference and division. team_id links to the team name used in
-- the Elite Prospects player files (matched by the loader); it is empty for
-- teams that had no player in the top-100 scoring lists.
CREATE TABLE team_seasons (
    team_season_id   SERIAL     PRIMARY KEY,
    league_id        SMALLINT   NOT NULL REFERENCES leagues,
    season_id        SMALLINT   NOT NULL REFERENCES seasons,
    team_id          INT        REFERENCES teams,
    wiki_team_name   TEXT       NOT NULL,
    conference       TEXT,
    division         TEXT,
    share_group      TEXT       NOT NULL,   -- conference, else division, else 'League'
    gp               SMALLINT   NOT NULL,
    w                SMALLINT,
    l                SMALLINT,
    t                SMALLINT,
    otl              SMALLINT,
    sol              SMALLINT,
    pts              SMALLINT,
    gf               SMALLINT   NOT NULL CHECK (gf >= 0),
    ga               SMALLINT,
    record_pct       NUMERIC(5,4),           -- standings points / possible points (team's record)
    UNIQUE (league_id, season_id, wiki_team_name),
    UNIQUE (team_id, season_id)
);

-- =====================================================================
-- Goal shares: how much of his team's and conference's scoring a player
-- produced. Regular season only (no free source has team playoff goals).
-- =====================================================================

-- One row per player per team per season (traded players have a row per team)
CREATE VIEW v_goal_shares AS
WITH base AS (
    SELECT
        ps.player_season_id,
        p.player_id,
        p.full_name,
        l.code                                         AS league,
        s.label                                        AS season,
        s.start_year,
        ps.position,
        CASE WHEN ps.position LIKE 'D%' THEN 'D'
             WHEN ps.position IS NULL THEN NULL
             ELSE 'F' END                              AS position_group,
        t.name                                         AS team,
        ts.conference,
        ts.division,
        ts.share_group,
        pst.gp, pst.goals, pst.assists, pst.points,
        ts.gp                                          AS team_gp,
        ts.gf                                          AS team_gf,
        g.group_gf,
        g.group_teams,
        -- team goals scored in the games this player played (assumes an even pace)
        ts.gf::NUMERIC / NULLIF(ts.gp, 0) * pst.gp     AS team_gf_in_gp
    FROM player_season_teams pst
    JOIN player_seasons ps ON ps.player_season_id = pst.player_season_id
    JOIN players p         ON p.player_id = ps.player_id
    JOIN leagues l         ON l.league_id = ps.league_id
    JOIN seasons s         ON s.season_id = ps.season_id
    JOIN teams t           ON t.team_id = pst.team_id
    JOIN team_seasons ts   ON ts.team_id = pst.team_id AND ts.season_id = ps.season_id
    JOIN (SELECT league_id, season_id, share_group, SUM(gf) AS group_gf, COUNT(*) AS group_teams
          FROM team_seasons GROUP BY league_id, season_id, share_group) g
         ON g.league_id = ts.league_id AND g.season_id = ts.season_id
        AND g.share_group = ts.share_group
    WHERE ps.game_type = 'regular'
)
SELECT
    base.*,
    ROUND(goals   / NULLIF(team_gf, 0)::NUMERIC, 4)      AS goal_share,
    ROUND(assists / NULLIF(team_gf, 0)::NUMERIC, 4)      AS assist_share,
    ROUND(points  / NULLIF(team_gf, 0)::NUMERIC, 4)      AS point_share,
    ROUND(goals   / NULLIF(team_gf_in_gp, 0), 4)         AS goal_share_gp_adj,
    ROUND(assists / NULLIF(team_gf_in_gp, 0), 4)         AS assist_share_gp_adj,
    ROUND(points  / NULLIF(team_gf_in_gp, 0), 4)         AS point_share_gp_adj,
    ROUND(goals   / NULLIF(group_gf, 0)::NUMERIC, 4)     AS group_goal_share,
    ROUND(assists / NULLIF(group_gf, 0)::NUMERIC, 4)     AS group_assist_share,
    ROUND(points  / NULLIF(group_gf, 0)::NUMERIC, 4)     AS group_point_share,
    -- points as a share of an average team's goals in the conference/division
    -- (comparable across groups of different sizes)
    ROUND(points * group_teams / NULLIF(group_gf, 0)::NUMERIC, 4) AS point_share_vs_group_avg,
    RANK() OVER (PARTITION BY league, season, team ORDER BY points DESC) AS team_scoring_rank
FROM base;

-- One row per player season (traded players' teams combined), with
-- percentile ranks inside each league season so different eras compare fairly.
CREATE VIEW v_player_season_shares AS
WITH combined AS (
    SELECT
        player_id, full_name, league, season, start_year,
        MAX(position)                                  AS position,
        MAX(position_group)                            AS position_group,
        STRING_AGG(team, ' / ' ORDER BY team)          AS teams,
        MAX(share_group)                               AS share_group,
        MIN(team_scoring_rank)                         AS best_team_scoring_rank,
        SUM(gp) AS gp, SUM(goals) AS goals, SUM(assists) AS assists, SUM(points) AS points,
        SUM(team_gf_in_gp)                             AS team_gf_in_gp
    FROM v_goal_shares
    GROUP BY player_id, full_name, league, season, start_year
),
shares AS (
    SELECT
        combined.*,
        ROUND(points::NUMERIC / NULLIF(gp, 0), 2)      AS points_per_game,
        ROUND(goals   / NULLIF(team_gf_in_gp, 0), 4)   AS goal_share_gp_adj,
        ROUND(assists / NULLIF(team_gf_in_gp, 0), 4)   AS assist_share_gp_adj,
        ROUND(points  / NULLIF(team_gf_in_gp, 0), 4)   AS point_share_gp_adj
    FROM combined
)
SELECT
    shares.*,
    ps_po.gp     AS playoff_gp,
    ps_po.goals  AS playoff_goals,
    ps_po.assists AS playoff_assists,
    ps_po.points AS playoff_points,
    ROUND(PERCENT_RANK() OVER (PARTITION BY league, season ORDER BY point_share_gp_adj)::NUMERIC, 3)
                                                   AS point_share_pctile,
    ROUND(PERCENT_RANK() OVER (PARTITION BY league, season ORDER BY points_per_game)::NUMERIC, 3)
                                                   AS ppg_pctile
FROM shares
LEFT JOIN seasons s_   ON s_.label = shares.season
LEFT JOIN leagues l_   ON l_.code = shares.league
LEFT JOIN player_seasons ps_po
       ON ps_po.player_id = shares.player_id AND ps_po.season_id = s_.season_id
      AND ps_po.league_id = l_.league_id AND ps_po.game_type = 'playoffs';

-- =====================================================================
-- NHL careers (source: NHL stats API) and links to CHL player seasons
-- =====================================================================

CREATE TABLE nhl_players (
    nhl_player_id    INT        PRIMARY KEY,
    full_name        TEXT       NOT NULL,
    birth_date       DATE,
    birth_country    TEXT,
    nationality      TEXT,
    position         TEXT,
    shoots           TEXT,
    height_in        SMALLINT,
    weight_lb        SMALLINT,
    draft_year       SMALLINT,
    draft_round      SMALLINT,
    draft_overall    SMALLINT,
    first_nhl_season INT,                    -- e.g. 19981999
    nhl_gp           INT        NOT NULL,
    nhl_goals        INT        NOT NULL,
    nhl_assists      INT        NOT NULL,
    nhl_points       INT        NOT NULL,
    hall_of_fame     BOOLEAN
);

-- Which NHL player each CHL player season belongs to (if he reached the NHL).
-- Matched by name plus a birth-year check (he must have been 15-21 that season).
CREATE TABLE player_season_nhl (
    player_season_id INT  PRIMARY KEY REFERENCES player_seasons ON DELETE CASCADE,
    nhl_player_id    INT  NOT NULL REFERENCES nhl_players,
    match_method     TEXT NOT NULL            -- 'exact name' or 'name variant'
);
CREATE INDEX idx_player_season_nhl_player ON player_season_nhl (nhl_player_id);

-- =====================================================================
-- NHL draft picks 1986-2016 (source: NHL records API)
-- =====================================================================

-- Every pick, including players who never played in the NHL. draft_season_id
-- links CHL draftees to their draft-year regular season (the season just before
-- the draft) when they were in the top-100 scoring files.
CREATE TABLE draft_picks (
    pick_id          SERIAL     PRIMARY KEY,
    draft_year       SMALLINT   NOT NULL,
    round            SMALLINT   NOT NULL,
    pick_in_round    SMALLINT,
    overall_pick     SMALLINT   NOT NULL,
    picks_that_year  SMALLINT   NOT NULL,
    draft_decile     SMALLINT   NOT NULL CHECK (draft_decile BETWEEN 1 AND 10),
    drafted_by       TEXT,
    nhl_player_id    INT,                      -- NHL's ID; links to nhl_players if he played
    player           TEXT       NOT NULL,
    position         TEXT,
    height_in        SMALLINT,
    weight_lb        SMALLINT,
    birth_date       DATE,
    amateur_team     TEXT,
    amateur_league   TEXT,
    draft_season_id  INT        REFERENCES player_seasons ON DELETE SET NULL,
    match_method     TEXT,
    UNIQUE (draft_year, overall_pick)
);
CREATE INDEX idx_draft_picks_nhl ON draft_picks (nhl_player_id);
