"""SQLite-Speicher für Roberts Cockpit (Etappe 1 – Fundament).

Die Karten liegen in einer SQLite-Datenbank statt in einer JSON-Datei.
Nach außen bleibt alles gleich: load_state()/save_state() arbeiten mit dem
bisherigen Format {"cards": [...], "_ts": ...}.

- Bekannte Felder (col, cat, prio, …) sind eigene Spalten.
- Alle übrigen Felder (z. B. "tools") landen unverändert in `extra` (JSON),
  damit nichts verloren geht, wenn das Frontend neue Felder bekommt.
- Gelöschte Karten werden nur markiert (deleted_at), nicht entfernt –
  fürs Wissensarchiv und als Schutz vor versehentlichem Löschen.
Nur Python-Bordmittel.
"""
import json
import os
import sqlite3
import threading
import time

CORE = ("col", "cat", "prio", "t", "note", "src", "sub", "owner")
SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS cards (
    id         TEXT PRIMARY KEY,
    pos        INTEGER NOT NULL DEFAULT 0,
    col        TEXT, cat TEXT, prio TEXT, t TEXT,
    note       TEXT, src TEXT, sub TEXT, owner TEXT,
    extra      TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    deleted_at REAL
);
CREATE INDEX IF NOT EXISTS cards_alive ON cards (deleted_at, pos);
"""

_write_lock = threading.Lock()


class EmptySaveRefused(Exception):
    """Ein Speichern ohne Karten würde alles als gelöscht markieren."""


def _connect(path):
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init(path, legacy_json=None):
    """Legt die Datenbank an und übernimmt einmalig die alte JSON-Datei."""
    conn = _connect(path)
    try:
        conn.executescript(_SCHEMA)
        conn.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)
        count = conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
        conn.commit()
    finally:
        conn.close()
    if count == 0 and legacy_json and os.path.exists(legacy_json):
        with open(legacy_json, "r", encoding="utf-8") as f:
            state = json.load(f)
        if isinstance(state, dict) and state.get("cards"):
            save_state(path, state)
            return len(state["cards"])
    return 0


def has_any_cards(path):
    conn = _connect(path)
    try:
        return conn.execute("SELECT 1 FROM cards LIMIT 1").fetchone() is not None
    finally:
        conn.close()


def load_state(path):
    conn = _connect(path)
    try:
        rows = conn.execute(
            "SELECT * FROM cards WHERE deleted_at IS NULL ORDER BY pos"
        ).fetchall()
        ts_row = conn.execute("SELECT value FROM meta WHERE key='_ts'").fetchone()
    finally:
        conn.close()
    cards = []
    for r in rows:
        card = {"id": r["id"]}
        for k in CORE:
            if r[k] is not None:
                card[k] = r[k]
        card.update(json.loads(r["extra"] or "{}"))
        cards.append(card)
    ts = json.loads(ts_row["value"]) if ts_row else 0
    return {"cards": cards, "_ts": ts}


def validate(state):
    if not isinstance(state, dict) or not isinstance(state.get("cards"), list):
        raise ValueError("Erwartet {\"cards\": [...]}")
    seen = set()
    for c in state["cards"]:
        if not isinstance(c, dict) or not isinstance(c.get("id"), str) or not c["id"]:
            raise ValueError("Jede Karte braucht eine Text-id")
        if c["id"] in seen:
            raise ValueError("Doppelte Karten-id: %s" % c["id"])
        seen.add(c["id"])


def save_state(path, state):
    """Übernimmt den kompletten Stand aus dem Frontend (wie bisher die JSON-Datei)."""
    validate(state)
    now = time.time()
    rows = []
    for pos, c in enumerate(state["cards"]):
        extra = {k: v for k, v in c.items() if k != "id" and k not in CORE}
        rows.append(
            (c["id"], pos)
            + tuple(c.get(k) for k in CORE)
            + (json.dumps(extra, ensure_ascii=False, sort_keys=True), now, now)
        )
    changed_expr = " OR ".join("cards.%s IS NOT excluded.%s" % (k, k) for k in CORE + ("extra",))
    with _write_lock:
        conn = _connect(path)
        try:
            alive = conn.execute(
                "SELECT COUNT(*) FROM cards WHERE deleted_at IS NULL"
            ).fetchone()[0]
            if not rows and alive > 0:
                raise EmptySaveRefused()
            conn.execute("BEGIN IMMEDIATE")
            conn.executemany(
                """INSERT INTO cards (id, pos, %s, extra, created_at, updated_at)
                   VALUES (?, ?, %s, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     pos = excluded.pos,
                     %s,
                     extra = excluded.extra,
                     updated_at = CASE WHEN cards.deleted_at IS NOT NULL OR %s
                                       THEN excluded.updated_at ELSE cards.updated_at END,
                     deleted_at = NULL"""
                % (
                    ", ".join(CORE),
                    ", ".join("?" for _ in CORE),
                    ", ".join("%s = excluded.%s" % (k, k) for k in CORE),
                    changed_expr,
                ),
                rows,
            )
            conn.execute("CREATE TEMP TABLE IF NOT EXISTS incoming (id TEXT PRIMARY KEY)")
            conn.execute("DELETE FROM incoming")
            conn.executemany("INSERT INTO incoming (id) VALUES (?)", [(r[0],) for r in rows])
            conn.execute(
                "UPDATE cards SET deleted_at = ? "
                "WHERE deleted_at IS NULL AND id NOT IN (SELECT id FROM incoming)",
                (now,),
            )
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('_ts', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (json.dumps(state.get("_ts", 0)),),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
