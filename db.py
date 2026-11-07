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
SCHEMA_VERSION = 2

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

-- Etappe 2: Materialien (Dateien, Links, Notizen) und ihre Zuordnung zu Karten
CREATE TABLE IF NOT EXISTS documents (
    id         INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL CHECK (kind IN ('file', 'link', 'note')),
    title      TEXT NOT NULL DEFAULT '',
    filename   TEXT,
    mime       TEXT,
    size       INTEGER,
    sha256     TEXT,
    url        TEXT,
    body       TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    deleted_at REAL
);
CREATE INDEX IF NOT EXISTS documents_sha ON documents (sha256);
CREATE TABLE IF NOT EXISTS card_documents (
    card_id    TEXT NOT NULL REFERENCES cards (id),
    doc_id     INTEGER NOT NULL REFERENCES documents (id),
    created_at REAL NOT NULL,
    PRIMARY KEY (card_id, doc_id)
);
CREATE INDEX IF NOT EXISTS card_documents_doc ON card_documents (doc_id);

-- Volltextsuche über Karten (kind='card', ref=id) und Materialien (kind='doc', ref=id)
CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5 (
    kind UNINDEXED, ref UNINDEXED, title, body,
    tokenize = 'unicode61 remove_diacritics 2'
);
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
        _reindex_cards(conn)
        _reindex_docs(conn)
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
            _reindex_cards(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


# ─── Suche ────────────────────────────────────────────────────────────

def _reindex_cards(conn):
    """Karten-Einträge im Suchindex neu aufbauen (einige hundert Zeilen – schnell)."""
    conn.execute("DELETE FROM search WHERE kind = 'card'")
    conn.execute(
        "INSERT INTO search (kind, ref, title, body) "
        "SELECT 'card', id, coalesce(t, ''), "
        "       coalesce(note, '') || ' ' || coalesce(sub, '') || ' ' || "
        "       coalesce(src, '') || ' ' || coalesce(owner, '') "
        "FROM cards WHERE deleted_at IS NULL"
    )


def _doc_index_row(d):
    body = " ".join(x for x in (d["filename"], d["url"], d["body"]) if x)
    return ("doc", str(d["id"]), d["title"] or "", body)


def _reindex_docs(conn):
    conn.execute("DELETE FROM search WHERE kind = 'doc'")
    rows = conn.execute("SELECT * FROM documents WHERE deleted_at IS NULL").fetchall()
    conn.executemany(
        "INSERT INTO search (kind, ref, title, body) VALUES (?, ?, ?, ?)",
        [_doc_index_row(d) for d in rows],
    )


def _fts_query(q):
    """Freitext → FTS5-Abfrage: jedes Wort als Präfix, alle Wörter müssen vorkommen."""
    words = [w for w in "".join(ch if ch.isalnum() else " " for ch in q).split() if w]
    return " ".join('"%s"*' % w for w in words[:12])


def search(path, q, limit=50):
    fq = _fts_query(q)
    if not fq:
        return {"cards": [], "docs": []}
    conn = _connect(path)
    try:
        hits = conn.execute(
            "SELECT kind, ref, snippet(search, -1, char(2), char(3), '…', 14) AS snip "
            "FROM search WHERE search MATCH ? ORDER BY rank LIMIT ?",
            (fq, limit),
        ).fetchall()
        card_ids = {h["ref"] for h in hits if h["kind"] == "card"}
        docs = []
        for h in hits:
            if h["kind"] != "doc":
                continue
            d = conn.execute(
                "SELECT * FROM documents WHERE id = ? AND deleted_at IS NULL", (int(h["ref"]),)
            ).fetchone()
            if not d:
                continue
            cards = [
                r["card_id"]
                for r in conn.execute(
                    "SELECT cd.card_id FROM card_documents cd JOIN cards c ON c.id = cd.card_id "
                    "WHERE cd.doc_id = ? AND c.deleted_at IS NULL",
                    (d["id"],),
                )
            ]
            card_ids.update(cards)
            item = _doc_dict(d)
            item["cards"] = cards
            item["snippet"] = h["snip"]
            docs.append(item)
    finally:
        conn.close()
    return {"cards": sorted(card_ids), "docs": docs}


# ─── Materialien ──────────────────────────────────────────────────────

class NotFound(Exception):
    pass


def _doc_dict(d):
    return {
        "id": d["id"], "kind": d["kind"], "title": d["title"], "filename": d["filename"],
        "mime": d["mime"], "size": d["size"], "url": d["url"], "body": d["body"],
        "created_at": d["created_at"],
    }


def _require_card(conn, card_id):
    if not conn.execute(
        "SELECT 1 FROM cards WHERE id = ? AND deleted_at IS NULL", (card_id,)
    ).fetchone():
        raise NotFound("Karte nicht gefunden – bitte die Karte zuerst speichern.")


def card_docs(path, card_id):
    conn = _connect(path)
    try:
        rows = conn.execute(
            "SELECT d.* FROM documents d JOIN card_documents cd ON cd.doc_id = d.id "
            "WHERE cd.card_id = ? AND d.deleted_at IS NULL ORDER BY cd.created_at",
            (card_id,),
        ).fetchall()
    finally:
        conn.close()
    return [_doc_dict(d) for d in rows]


def doc_counts(path):
    conn = _connect(path)
    try:
        rows = conn.execute(
            "SELECT cd.card_id, COUNT(*) AS n FROM card_documents cd "
            "JOIN documents d ON d.id = cd.doc_id "
            "WHERE d.deleted_at IS NULL GROUP BY cd.card_id"
        ).fetchall()
    finally:
        conn.close()
    return {r["card_id"]: r["n"] for r in rows}


def add_doc(path, card_id, kind, title="", filename=None, mime=None, size=None,
            sha256=None, url=None, body=None):
    """Legt ein Material an und hängt es an die Karte.
    Gleiche Datei (gleicher Hash) wird nicht doppelt gespeichert, sondern wiederverwendet."""
    now = time.time()
    with _write_lock:
        conn = _connect(path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            _require_card(conn, card_id)
            existing = None
            if kind == "file" and sha256:
                existing = conn.execute(
                    "SELECT * FROM documents WHERE kind = 'file' AND sha256 = ? "
                    "ORDER BY deleted_at IS NOT NULL, id LIMIT 1",
                    (sha256,),
                ).fetchone()
            if existing:
                doc_id = existing["id"]
                if existing["deleted_at"] is not None:
                    conn.execute(
                        "UPDATE documents SET deleted_at = NULL, updated_at = ? WHERE id = ?",
                        (now, doc_id),
                    )
            else:
                cur = conn.execute(
                    "INSERT INTO documents (kind, title, filename, mime, size, sha256, url, body, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (kind, title or "", filename, mime, size, sha256, url, body, now, now),
                )
                doc_id = cur.lastrowid
            conn.execute(
                "INSERT OR IGNORE INTO card_documents (card_id, doc_id, created_at) VALUES (?, ?, ?)",
                (card_id, doc_id, now),
            )
            d = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
            conn.execute("DELETE FROM search WHERE kind = 'doc' AND ref = ?", (str(doc_id),))
            conn.execute(
                "INSERT INTO search (kind, ref, title, body) VALUES (?, ?, ?, ?)", _doc_index_row(d)
            )
            conn.commit()
            return _doc_dict(d)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def unlink_doc(path, card_id, doc_id):
    """Löst ein Material von der Karte. Hängt es an keiner Karte mehr, wird es
    als gelöscht markiert (Datei bleibt auf dem Server – Archiv)."""
    now = time.time()
    with _write_lock:
        conn = _connect(path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "DELETE FROM card_documents WHERE card_id = ? AND doc_id = ?", (card_id, doc_id)
            )
            if cur.rowcount == 0:
                raise NotFound("Verknüpfung nicht gefunden")
            left = conn.execute(
                "SELECT COUNT(*) FROM card_documents WHERE doc_id = ?", (doc_id,)
            ).fetchone()[0]
            if left == 0:
                conn.execute(
                    "UPDATE documents SET deleted_at = ?, updated_at = ? WHERE id = ?",
                    (now, now, doc_id),
                )
                conn.execute("DELETE FROM search WHERE kind = 'doc' AND ref = ?", (str(doc_id),))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def get_file_doc(path, doc_id):
    conn = _connect(path)
    try:
        d = conn.execute(
            "SELECT * FROM documents WHERE id = ? AND kind = 'file' AND deleted_at IS NULL",
            (doc_id,),
        ).fetchone()
    finally:
        conn.close()
    if not d:
        raise NotFound("Datei nicht gefunden")
    return d
