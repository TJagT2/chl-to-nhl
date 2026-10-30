"""
Shared settings and helpers used by all the project scripts:
folder locations, the database connection, name matching, and
Excel / chart formatting.

Project layout:
    CHL to NHL Model/
        CHL_Data/            Elite Prospects top-100 files (WHL, OHL, QMJHL folders)
            Python/          these scripts
        CHL_Data_Full/
            Standings/       Wikipedia standings     (collect_data.py)
            NHL/             NHL careers + draft     (collect_data.py)
            Analysis/        reports and charts      (chl_analysis.py, draft_analysis.py)
            _cache/          saved downloads
"""

import argparse
import getpass
import os
import re
import unicodedata
from decimal import Decimal
from pathlib import Path

import pandas as pd
import psycopg

# ---------------------------------------------------------------------
# Folders and files
# ---------------------------------------------------------------------

PYTHON_DIR = Path(__file__).resolve().parent
CHL_DATA_DIR = PYTHON_DIR.parent
DATA_DIR = CHL_DATA_DIR.parent / "CHL_Data_Full"
CACHE_DIR = DATA_DIR / "_cache"
ANALYSIS_DIR = DATA_DIR / "Analysis"
STANDINGS_FILE = DATA_DIR / "Standings" / "CHL Standings 1985-2016.xlsx"
NHL_CAREERS_FILE = DATA_DIR / "NHL" / "NHL Careers 1986-2026.xlsx"
DRAFT_FILE = DATA_DIR / "NHL" / "NHL Draft Picks 1986-2016.xlsx"

LEAGUES = {
    "WHL": "Western Hockey League",
    "OHL": "Ontario Hockey League",
    "QMJHL": "Quebec Major Junior Hockey League",
}

# Chart colours (blue / orange categorical, red-gray-blue diverging, text inks)
BLUE, ORANGE = "#2a78d6", "#eb6834"
INK, INK_MUTED, GRID = "#0b0b0b", "#52514e", "#e4e3df"
DIVERGING = ["#a8302f", "#e34948", "#f0efec", "#5598e7", "#1c5cab", "#0d366b"]


# ---------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------

def add_db_args(parser):
    parser.add_argument("--db", default="chl_stats", help="database name (default: chl_stats)")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--user", default="postgres")
    return parser


def db_settings(args):
    """Connection settings; the password comes from PGPASSWORD or is asked for once."""
    password = os.environ.get("PGPASSWORD") or getpass.getpass(f"PostgreSQL password for '{args.user}': ")
    return dict(host=args.host, port=args.port, user=args.user, password=password)


def connect(settings, dbname):
    return psycopg.connect(**settings, dbname=dbname)


def query_df(conn, sql, params=None):
    """Run a query and return a DataFrame (database decimals become floats)."""
    cur = conn.execute(sql, params)
    df = pd.DataFrame(cur.fetchall(), columns=[d.name for d in cur.description])
    for col in df.columns:
        if df[col].map(lambda v: isinstance(v, Decimal)).any():
            df[col] = df[col].astype(float)
    return df


# ---------------------------------------------------------------------
# Matching names across sources
# ---------------------------------------------------------------------

# Common first-name variants (Elite Prospects vs. NHL spellings)
NAME_VARIANTS = [{"mike", "michael", "mikey"}, {"andy", "andrew", "drew"}, {"bill", "william", "will"},
                 {"bob", "robert", "rob", "bobby"}, {"dmitri", "dimitri", "dmitry"},
                 {"fyodor", "fedor"}, {"jim", "james", "jimmy"}, {"joe", "joseph", "joey"},
                 {"tom", "thomas", "tommy"}, {"dick", "richard", "rick", "ricky"},
                 {"ted", "edward", "ed", "eddie"}, {"jake", "jacob"}, {"alex", "alexandre",
                 "alexander", "alexandr", "aleksandr", "alexei", "alexey"}]


def person_key(name):
    """Simplify a person's name: no accents, punctuation, case, or '[team]' suffix."""
    name = re.sub(r"\[.*?\]", "", name or "")
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    text = re.sub(r"[^a-z ]", " ", text.replace("'", "").replace(".", ""))
    return " ".join(text.split())


def first_names_compatible(a, b):
    if a == b:
        return True
    if len(a) >= 2 and len(b) >= 2 and (a.startswith(b) or b.startswith(a)):
        return True                                  # Alex / Alexander, Nic / Nicolas
    if len(a) >= 4 and len(b) >= 4 and a[:3] == b[:3]:
        return True                                  # Sergei / Sergey, Christopher / Chris...
    return any(a in group and b in group for group in NAME_VARIANTS)


# ---------------------------------------------------------------------
# Excel and chart formatting
# ---------------------------------------------------------------------

def save_workbook(path, sheets, images=None, wide=None, percent_cols=None):
    """
    Write {sheet name: DataFrame} to an Excel file with readable formatting.
    images: {sheet name: (png path, top-left cell)}
    wide:   {sheet name: {column letter: width}}
    percent_cols: {sheet name: [column names to show as %]}
    """
    from openpyxl import load_workbook
    from openpyxl.drawing.image import Image as XLImage
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        for name, (df, keep_index) in sheets.items():
            df.to_excel(xw, sheet_name=name, index=keep_index)

    wb = load_workbook(path)
    for ws in wb.worksheets:
        for col in ws.columns:
            width = max(len(str(c.value)) if c.value is not None else 0 for c in col[:300])
            ws.column_dimensions[get_column_letter(col[0].column)].width = min(max(9, width + 2), 48)
        for cell in ws[1]:
            cell.font = Font(bold=True)
        ws.freeze_panes = "A2"
    for name, widths in (wide or {}).items():
        for letter, w in widths.items():
            wb[name].column_dimensions[letter].width = w
    for name, cols in (percent_cols or {}).items():
        ws = wb[name]
        for header in ws[1]:
            if header.value in cols:
                for cell in ws[header.column_letter][1:]:
                    cell.number_format = "0.0%"
    for name, (png, anchor) in (images or {}).items():
        wb[name].add_image(XLImage(str(png)), anchor)
    wb.save(path)
    return path


def sheet(df, index=False):
    """Helper for save_workbook: a DataFrame and whether to write its index."""
    return (df, index)


def style_axes(ax):
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=INK_MUTED, labelsize=9)
