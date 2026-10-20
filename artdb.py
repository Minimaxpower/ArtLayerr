"""Small SQLite store for ArtLayerr (standard library only).

Two tables:
  journal    one row per image saved (download, URL, upload, re-download):
             text only, never a copy of the image itself
  snapshots  one row per day, library and kind of art: how many are done / total,
             the data behind the progress curve

The file lives next to the state file (in the assets folder), so it survives
updates of the image. Connections are short-lived (open, use, close), which
keeps it safe with gunicorn's threads.
"""
import os
import sqlite3
import threading
import time
from contextlib import closing

_PATH = None
_READY = False
_LOCK = threading.Lock()

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS journal (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        rating_key INTEGER,
        season_rating_key INTEGER,
        section_key TEXT,
        section_title TEXT,
        item_title TEXT,
        item_type TEXT,
        art_type TEXT NOT NULL,
        season TEXT,
        episode TEXT,
        file TEXT,
        size INTEGER,
        action TEXT,
        source TEXT,
        replaced INTEGER DEFAULT 0
    )""",
    "CREATE INDEX IF NOT EXISTS journal_ts ON journal (ts DESC)",
    "CREATE INDEX IF NOT EXISTS journal_item ON journal (rating_key, art_type)",
    """CREATE TABLE IF NOT EXISTS snapshots (
        day TEXT NOT NULL,
        section_key TEXT NOT NULL,
        section_title TEXT,
        kind TEXT NOT NULL,
        done INTEGER NOT NULL,
        total INTEGER NOT NULL,
        PRIMARY KEY (day, section_key, kind)
    )""",
)


def configure(path):
    """Set the database file (called once, at start-up)."""
    global _PATH, _READY
    _PATH = path
    _READY = False


def _connect():
    conn = sqlite3.connect(_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure():
    global _READY
    if _READY:
        return
    if not _PATH:
        raise RuntimeError("artdb is not configured")
    with _LOCK:
        if _READY:
            return
        with closing(_connect()) as conn:
            for statement in _SCHEMA:
                conn.execute(statement)
            conn.commit()
        _READY = True


# ---- journal ---------------------------------------------------------------
_JOURNAL_FIELDS = (
    "ts", "rating_key", "season_rating_key", "section_key", "section_title",
    "item_title", "item_type", "art_type", "season", "episode", "file", "size",
    "action", "source", "replaced",
)


def add_journal(**fields):
    _ensure()
    fields.setdefault("ts", time.time())
    values = [fields.get(name) for name in _JOURNAL_FIELDS]
    marks = ", ".join("?" for _ in _JOURNAL_FIELDS)
    with closing(_connect()) as conn:
        cur = conn.execute(
            f"INSERT INTO journal ({', '.join(_JOURNAL_FIELDS)}) VALUES ({marks})", values
        )
        conn.commit()
        return cur.lastrowid


def get_journal(entry_id):
    _ensure()
    with closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM journal WHERE id = ?", (entry_id,)).fetchone()
    return dict(row) if row else None


def list_journal(limit=25, offset=0, section_key=None, art_type=None, query=None):
    """(rows, total) of the journal, newest first, with optional filters."""
    _ensure()
    where, params = [], []
    if section_key:
        where.append("section_key = ?")
        params.append(section_key)
    if art_type:
        where.append("art_type = ?")
        params.append(art_type)
    if query:
        where.append("item_title LIKE ? ESCAPE '\\'")
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params.append(f"%{escaped}%")
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    with closing(_connect()) as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM journal{clause}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM journal{clause} ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
    return [dict(r) for r in rows], total


def journal_for_item(rating_key, art_type, season, episode, limit=10):
    """Journal rows of one item, for one kind of art at one level (show,
    season or episode). `IS` makes a missing season / episode match NULL."""
    _ensure()
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM journal WHERE rating_key = ? AND art_type = ? "
            "AND season IS ? AND episode IS ? ORDER BY ts DESC, id DESC LIMIT ?",
            (rating_key, art_type, season, episode, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def journal_libraries():
    """[(section_key, section_title)] seen in the journal, for the filter."""
    _ensure()
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT section_key, MAX(section_title) AS title FROM journal "
            "WHERE section_key IS NOT NULL GROUP BY section_key ORDER BY title COLLATE NOCASE"
        ).fetchall()
    return [(r["section_key"], r["title"] or r["section_key"]) for r in rows]


# ---- snapshots (progress curve) ---------------------------------------------
def save_snapshot(day, rows):
    """rows: (section_key, section_title, kind, done, total). One row per day,
    library and kind: saving again the same day replaces the numbers."""
    _ensure()
    with closing(_connect()) as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO snapshots (day, section_key, section_title, kind, done, total) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(day, k, t, kind, int(done), int(total)) for k, t, kind, done, total in rows],
        )
        conn.commit()


def has_snapshots():
    _ensure()
    with closing(_connect()) as conn:
        return conn.execute("SELECT 1 FROM snapshots LIMIT 1").fetchone() is not None


def snapshots(since_day=None):
    """All snapshot rows from `since_day` (YYYY-MM-DD) on, oldest first."""
    _ensure()
    with closing(_connect()) as conn:
        if since_day:
            rows = conn.execute(
                "SELECT * FROM snapshots WHERE day >= ? ORDER BY day", (since_day,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM snapshots ORDER BY day").fetchall()
    return [dict(r) for r in rows]
