"""Formula memory system — teacher formulas with 35-day active recall unlock.

Stores formulas categorized by subject/chapter/topic. A formula is ``locked``
for the first ``UNLOCK_DAYS`` (35) after it is added; once ``unlock_at`` passes
it becomes ``due_for_recall`` and can be drilled. Recalls increment
``recall_count``, stamp ``recalled_at`` and move mastery ``new -> learning``
(``mastered`` only via an explicit ``mastered=True`` recall).

Everything is deterministic SQLite following the repo's local-module pattern
(``DEFAULT_DB_PATH``, ``_connect``, ``init_db``, WAL, ``busy_timeout``,
``sqlite3.Row``). The table lives in ``sqlite_mirror.db``; wiring into
``bot.py`` / ``config.ownership`` happens separately later.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "sqlite_mirror.db"

FORMULAS_TABLE = "learn_formulas"

# Days before a formula becomes available for active recall.
UNLOCK_DAYS = 35

# Mastery ladder.
MASTERY_STATES: tuple[str, ...] = ("new", "learning", "mastered")

_MAX_LIST_LIMIT = 100
_MAX_DUE_LIMIT = 50


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# SQLite schema
# ---------------------------------------------------------------------------

def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(f"""
        CREATE TABLE IF NOT EXISTS {FORMULAS_TABLE} (
            formula_id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject TEXT NOT NULL,
            chapter TEXT,
            topic TEXT,
            formula_text TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'self',
            created_at TEXT NOT NULL,
            unlock_at TEXT NOT NULL,
            recalled_at TEXT,
            recall_count INTEGER NOT NULL DEFAULT 0,
            mastery TEXT NOT NULL DEFAULT 'new',
            notes TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_learn_formulas_subject
            ON learn_formulas(subject);
        CREATE INDEX IF NOT EXISTS idx_learn_formulas_unlock
            ON learn_formulas(unlock_at);
        CREATE INDEX IF NOT EXISTS idx_learn_formulas_mastery
            ON learn_formulas(mastery);
    """)
    conn.commit()


def _connect(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    init_db(conn)
    return conn


def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def add(
    subject: str,
    chapter: str | None,
    topic: str | None,
    formula_text: str,
    source: str = "self",
    days_to_unlock: int = UNLOCK_DAYS,
    notes: str = "",
    db_path: str | Path = DEFAULT_DB_PATH,
) -> dict:
    """Insert a formula; ``unlock_at`` = now + ``days_to_unlock``.

    Returns the new row as a dict. ``subject`` and ``formula_text`` are
    validated non-empty (ValueError otherwise). Subject/formula_text are stored
    stripped; chapter/topic are stored as given.
    """
    subject = (subject or "").strip()
    formula_text = (formula_text or "").strip()
    if not subject:
        raise ValueError("subject must not be empty")
    if not formula_text:
        raise ValueError("formula_text must not be empty")
    now = dt.datetime.now(dt.timezone.utc)
    created_at = now.isoformat()
    unlock_at = (now + dt.timedelta(days=int(days_to_unlock))).isoformat()
    with _connect(db_path) as conn:
        cur = conn.execute(
            f"INSERT INTO {FORMULAS_TABLE} "
            "(subject, chapter, topic, formula_text, source, created_at, "
            "unlock_at, recall_count, mastery, notes) "
            "VALUES (?,?,?,?,?,?,?,0,'new',?)",
            (subject, chapter, topic, formula_text, source, created_at, unlock_at, notes),
        )
        conn.commit()
        row = conn.execute(
            f"SELECT * FROM {FORMULAS_TABLE} WHERE formula_id=?",
            (cur.lastrowid,),
        ).fetchone()
    return dict(row)


def get(
    formula_id: int, db_path: str | Path = DEFAULT_DB_PATH,
) -> dict | None:
    """Fetch a single formula by id, or None when unknown."""
    with _connect(db_path) as conn:
        row = conn.execute(
            f"SELECT * FROM {FORMULAS_TABLE} WHERE formula_id=?", (formula_id,)
        ).fetchone()
    return _row_to_dict(row)


def list(
    subject: str | None = None,
    chapter: str | None = None,
    topic: str | None = None,
    limit: int = 50,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> list[dict]:
    """Filtered formulas, newest first; ``limit`` capped at 100.

    Filters match exactly as stored (no case folding). ``None`` filters are
    ignored.
    """
    clauses: list[str] = []
    params: list[object] = []
    for column, value in (("subject", subject), ("chapter", chapter), ("topic", topic)):
        if value is not None:
            clauses.append(f"{column} = ?")
            params.append(value)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(int(limit), _MAX_LIST_LIMIT)))
    with _connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM {FORMULAS_TABLE} {where} "
            "ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def browse(
    subject: str | None = None, db_path: str | Path = DEFAULT_DB_PATH,
) -> list[dict]:
    """Category counts ``[{subject, chapter, topic, count}]`` grouped.

    Ordered by subject then chapter. With ``subject`` given, only that
    subject's categories are returned.
    """
    if subject is not None:
        sql = (
            f"SELECT subject, chapter, topic, COUNT(*) AS count "
            f"FROM {FORMULAS_TABLE} WHERE subject=? "
            "GROUP BY subject, chapter, topic ORDER BY subject, chapter"
        )
        params: tuple = (subject,)
    else:
        sql = (
            f"SELECT subject, chapter, topic, COUNT(*) AS count "
            f"FROM {FORMULAS_TABLE} "
            "GROUP BY subject, chapter, topic ORDER BY subject, chapter"
        )
        params = ()
    with _connect(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Recall flow
# ---------------------------------------------------------------------------

def due_for_recall(
    now: str | None = None, limit: int = 10, db_path: str | Path = DEFAULT_DB_PATH,
) -> list[dict]:
    """Formulas unlocked for recall, oldest unlock first; ``limit`` capped at 50.

    A formula is due when ``unlock_at <= now`` and it is not ``mastered``.
    ``now`` defaults to the current UTC ISO time.
    """
    if now is None:
        now = _now()
    limit = max(1, min(int(limit), _MAX_DUE_LIMIT))
    with _connect(db_path) as conn:
        rows = conn.execute(
            f"SELECT * FROM {FORMULAS_TABLE} "
            "WHERE unlock_at <= ? AND mastery != 'mastered' "
            "ORDER BY unlock_at ASC LIMIT ?",
            (now, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_recalled(
    formula_id: int, mastered: bool = False, db_path: str | Path = DEFAULT_DB_PATH,
) -> dict | None:
    """Record a recall: bump ``recall_count``, stamp ``recalled_at``.

    Mastery becomes ``learning`` by default, ``mastered`` when ``mastered=True``.
    Returns the updated row, or None for an unknown id.
    """
    mastery = "mastered" if mastered else "learning"
    with _connect(db_path) as conn:
        cur = conn.execute(
            f"UPDATE {FORMULAS_TABLE} "
            "SET recall_count = recall_count + 1, recalled_at = ?, mastery = ? "
            "WHERE formula_id = ?",
            (_now(), mastery, formula_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            return None
        row = conn.execute(
            f"SELECT * FROM {FORMULAS_TABLE} WHERE formula_id=?", (formula_id,)
        ).fetchone()
    return dict(row)


# ---------------------------------------------------------------------------
# Aggregates + delete
# ---------------------------------------------------------------------------

def stats(db_path: str | Path = DEFAULT_DB_PATH) -> dict:
    """Totals: ``total``, ``by_subject``, ``by_mastery``, ``due_for_recall``, ``locked``."""
    now = _now()
    with _connect(db_path) as conn:
        total = int(conn.execute(
            f"SELECT COUNT(*) AS c FROM {FORMULAS_TABLE}"
        ).fetchone()["c"])
        by_subject = {
            str(r["subject"]): int(r["c"])
            for r in conn.execute(
                f"SELECT subject, COUNT(*) AS c FROM {FORMULAS_TABLE} "
                "GROUP BY subject ORDER BY subject"
            )
        }
        by_mastery = {
            str(r["mastery"]): int(r["c"])
            for r in conn.execute(
                f"SELECT mastery, COUNT(*) AS c FROM {FORMULAS_TABLE} "
                "GROUP BY mastery ORDER BY mastery"
            )
        }
        due = int(conn.execute(
            f"SELECT COUNT(*) AS c FROM {FORMULAS_TABLE} "
            "WHERE unlock_at <= ? AND mastery != 'mastered'",
            (now,),
        ).fetchone()["c"])
        locked = int(conn.execute(
            f"SELECT COUNT(*) AS c FROM {FORMULAS_TABLE} WHERE unlock_at > ?",
            (now,),
        ).fetchone()["c"])
    return {
        "total": total,
        "by_subject": by_subject,
        "by_mastery": by_mastery,
        "due_for_recall": due,
        "locked": locked,
    }


def delete_formula(
    formula_id: int, db_path: str | Path = DEFAULT_DB_PATH,
) -> bool:
    """Remove a formula row. Returns True when the row existed and was deleted.

    The schema has no archive column, so the row is hard-deleted (the recall
    history of a removed formula is not preserved).
    """
    with _connect(db_path) as conn:
        cur = conn.execute(
            f"DELETE FROM {FORMULAS_TABLE} WHERE formula_id=?", (formula_id,)
        )
        conn.commit()
        return cur.rowcount == 1
