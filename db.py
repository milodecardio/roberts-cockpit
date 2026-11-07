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
import secrets
import sqlite3
import threading
import time

CORE = ("col", "cat", "prio", "t", "note", "src", "sub", "owner")
SCHEMA_VERSION = 4

# Gültige Werte (wie im Frontend: COLS, CATS, PRIOS)
COLUMNS = {"eingang": "📥 Eingang", "heute": "🔴 Heute / Dringend", "woche": "🟢 Diese Woche",
           "naechstes": "🔵 Als Nächstes", "spaeter": "🟣 Später", "geparkt": "⚪ Geparkt (Vision)",
           "erledigt": "✅ Erledigt"}
CATEGORIES = {"b1": "B1 Beruflich & Projekte", "b2": "B2 Recht & Verwaltung", "b3": "B3 Technik",
              "b4": "B4 Medizin", "b5": "B5 Pflege Mutter", "b6": "B6 Privates", "b7": "B7 Sonstiges",
              "b8": "B8 Themen ohne Aufgabe", "vision": "Vision"}
PRIORITIES = ("Hoch", "Mittel", "Niedrig")

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

-- Etappe 3: ausgelesener Text, Vektor-Abschnitte, Importquellen
CREATE TABLE IF NOT EXISTS doc_text (
    doc_id       INTEGER PRIMARY KEY REFERENCES documents (id),
    text         TEXT NOT NULL,
    extracted_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
    id         INTEGER PRIMARY KEY,
    owner_kind TEXT NOT NULL CHECK (owner_kind IN ('doc', 'card')),
    owner_id   TEXT NOT NULL,
    idx        INTEGER NOT NULL,
    text       TEXT NOT NULL,
    embedding  BLOB NOT NULL,
    model      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_owner ON chunks (owner_kind, owner_id);
CREATE TABLE IF NOT EXISTS sources (
    source  TEXT NOT NULL,
    ref     TEXT NOT NULL,
    doc_id  INTEGER NOT NULL REFERENCES documents (id),
    mtime   REAL,
    size    INTEGER,
    seen_at REAL NOT NULL,
    PRIMARY KEY (source, ref)
);
CREATE INDEX IF NOT EXISTS sources_doc ON sources (doc_id);

-- Etappe 4: Anmeldung von claude.ai (OAuth) und Protokoll der KI-Änderungen
CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id     TEXT PRIMARY KEY,
    name          TEXT,
    redirect_uris TEXT NOT NULL,
    created_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_codes (
    code_hash    TEXT PRIMARY KEY,
    client_id    TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    challenge    TEXT NOT NULL,
    expires_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_tokens (
    token_hash TEXT PRIMARY KEY,
    kind       TEXT NOT NULL CHECK (kind IN ('access', 'refresh')),
    client_id  TEXT NOT NULL,
    grant_id   TEXT NOT NULL,
    expires_at REAL NOT NULL,
    created_at REAL NOT NULL,
    last_used  REAL
);
CREATE INDEX IF NOT EXISTS oauth_tokens_grant ON oauth_tokens (grant_id);
CREATE TABLE IF NOT EXISTS activity (
    id     INTEGER PRIMARY KEY,
    ts     REAL NOT NULL,
    actor  TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT
);
"""

# Spalten, die nach Version 2 dazugekommen sind (ALTER TABLE ist nicht „IF NOT EXISTS“-fähig)
_ADD_COLUMNS = [
    ("documents", "text_status", "TEXT NOT NULL DEFAULT 'pending'"),
    ("documents", "text_error", "TEXT"),
    ("documents", "processed_at", "REAL"),
    ("documents", "origin", "TEXT"),
    ("documents", "origin_path", "TEXT"),
    ("cards", "embedded_at", "REAL"),
]


def _migrate_columns(conn):
    for table, col, decl in _ADD_COLUMNS:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)}
        if col not in cols:
            conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, decl))
    conn.execute("CREATE INDEX IF NOT EXISTS documents_status ON documents (text_status, deleted_at)")

_write_lock = threading.Lock()


class EmptySaveRefused(Exception):
    """Ein Speichern ohne Karten würde alles als gelöscht markieren."""


class Conflict(Exception):
    """Der Stand wurde inzwischen woanders geändert (anderes Gerät oder Claude)."""


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
        old_version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.executescript(_SCHEMA)
        _migrate_columns(conn)
        conn.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)
        count = conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
        if old_version < SCHEMA_VERSION:
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


def _get_ts(conn):
    row = conn.execute("SELECT value FROM meta WHERE key='_ts'").fetchone()
    try:
        return json.loads(row["value"]) if row else 0
    except (TypeError, ValueError):
        return 0


def _next_ts(conn):
    """Neue Versionsnummer des Kartenstands (Millisekunden, streng steigend)."""
    ts = max(int(time.time() * 1000), int(float(_get_ts(conn) or 0)) + 1)
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('_ts', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (json.dumps(ts),))
    return ts


def save_state(path, state):
    """Übernimmt den kompletten Stand aus dem Frontend (wie bisher die JSON-Datei).

    Schickt das Frontend `_base` mit (die Version, auf der seine Änderung beruht), wird
    abgelehnt (Conflict), falls inzwischen jemand anderes gespeichert hat – das Frontend
    führt dann zusammen und speichert erneut. Gibt die neue Version zurück."""
    validate(state)
    base = state.get("_base")
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
            if base is not None and float(base or 0) != float(_get_ts(conn) or 0):
                raise Conflict()
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
            new_ts = _next_ts(conn)
            _reindex_cards(conn)
            conn.commit()
            return new_ts
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


def _doc_index_row(d, text=None):
    keys = d.keys()
    extra = d["origin_path"] if "origin_path" in keys else None
    body = " ".join(x for x in (d["filename"], d["url"], d["body"], extra, text) if x)
    return ("doc", str(d["id"]), d["title"] or "", body)


def _reindex_doc(conn, doc_id):
    conn.execute("DELETE FROM search WHERE kind = 'doc' AND ref = ?", (str(doc_id),))
    d = conn.execute(
        "SELECT d.*, t.text AS fulltext FROM documents d LEFT JOIN doc_text t ON t.doc_id = d.id "
        "WHERE d.id = ? AND d.deleted_at IS NULL", (doc_id,)
    ).fetchone()
    if d:
        conn.execute("INSERT INTO search (kind, ref, title, body) VALUES (?, ?, ?, ?)",
                     _doc_index_row(d, d["fulltext"]))


def _reindex_docs(conn):
    conn.execute("DELETE FROM search WHERE kind = 'doc'")
    rows = conn.execute(
        "SELECT d.*, t.text AS fulltext FROM documents d LEFT JOIN doc_text t ON t.doc_id = d.id "
        "WHERE d.deleted_at IS NULL"
    ).fetchall()
    conn.executemany(
        "INSERT INTO search (kind, ref, title, body) VALUES (?, ?, ?, ?)",
        [_doc_index_row(d, d["fulltext"]) for d in rows],
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
    keys = d.keys()
    out = {
        "id": d["id"], "kind": d["kind"], "title": d["title"], "filename": d["filename"],
        "mime": d["mime"], "size": d["size"], "url": d["url"], "body": d["body"],
        "created_at": d["created_at"],
    }
    for k in ("text_status", "text_error", "origin", "origin_path"):
        if k in keys:
            out[k] = d[k]
    return out


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
                    "created_at, updated_at, origin) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'upload')",
                    (kind, title or "", filename, mime, size, sha256, url, body, now, now),
                )
                doc_id = cur.lastrowid
            conn.execute(
                "INSERT OR IGNORE INTO card_documents (card_id, doc_id, created_at) VALUES (?, ?, ?)",
                (card_id, doc_id, now),
            )
            d = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
            _reindex_doc(conn, doc_id)
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
            imported = conn.execute(
                "SELECT 1 FROM sources WHERE doc_id = ? LIMIT 1", (doc_id,)
            ).fetchone()
            if left == 0 and not imported:  # importierte Dateien bleiben in der Bibliothek
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


def attach_doc(path, card_id, doc_id):
    """Hängt ein vorhandenes Material (z. B. aus der Bibliothek) an eine Karte."""
    now = time.time()
    with _write_lock:
        conn = _connect(path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            _require_card(conn, card_id)
            d = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
            if not d:
                raise NotFound("Material nicht gefunden")
            if d["deleted_at"] is not None:
                conn.execute("UPDATE documents SET deleted_at = NULL, updated_at = ? WHERE id = ?",
                             (now, doc_id))
            conn.execute(
                "INSERT OR IGNORE INTO card_documents (card_id, doc_id, created_at) VALUES (?, ?, ?)",
                (card_id, doc_id, now),
            )
            _reindex_doc(conn, doc_id)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


# ─── Bibliothek & Import (Etappe 3) ───────────────────────────────────

def library(path, q="", origin="", status="", limit=100, offset=0, folder="", unlinked=False):
    where = ["d.deleted_at IS NULL"]
    args = []
    if folder:
        where.append("instr(lower(coalesce(d.origin_path, '')), lower(?)) > 0")
        args.append(folder)
    if unlinked:
        where.append("NOT EXISTS (SELECT 1 FROM card_documents cd JOIN cards c ON c.id = cd.card_id "
                     "WHERE cd.doc_id = d.id AND c.deleted_at IS NULL)")
    if origin:
        where.append("d.origin = ?")
        args.append(origin)
    if status:
        where.append("d.text_status = ?")
        args.append(status)
    if q.strip():
        fq = _fts_query(q)
        if fq:
            where.append("CAST(d.id AS TEXT) IN (SELECT ref FROM search WHERE kind = 'doc' AND search MATCH ?)")
            args.append(fq)
    sql_where = " AND ".join(where)
    conn = _connect(path)
    try:
        total = conn.execute("SELECT COUNT(*) FROM documents d WHERE " + sql_where, args).fetchone()[0]
        rows = conn.execute(
            "SELECT d.*, (SELECT COUNT(*) FROM card_documents cd JOIN cards c ON c.id = cd.card_id "
            "             WHERE cd.doc_id = d.id AND c.deleted_at IS NULL) AS ncards "
            "FROM documents d WHERE " + sql_where + " ORDER BY d.created_at DESC, d.id DESC LIMIT ? OFFSET ?",
            args + [limit, offset],
        ).fetchall()
        items = []
        for r in rows:
            item = _doc_dict(r)
            item["cards"] = [x["card_id"] for x in conn.execute(
                "SELECT cd.card_id FROM card_documents cd JOIN cards c ON c.id = cd.card_id "
                "WHERE cd.doc_id = ? AND c.deleted_at IS NULL", (r["id"],))]
            items.append(item)
    finally:
        conn.close()
    return {"total": total, "items": items}


def status(path):
    conn = _connect(path)
    try:
        by = {r[0]: r[1] for r in conn.execute(
            "SELECT text_status, COUNT(*) FROM documents WHERE deleted_at IS NULL GROUP BY text_status")}
        origins = {(r[0] or "upload"): r[1] for r in conn.execute(
            "SELECT origin, COUNT(*) FROM documents WHERE deleted_at IS NULL GROUP BY origin")}
        chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        cards_todo = conn.execute(
            "SELECT COUNT(*) FROM cards WHERE deleted_at IS NULL AND "
            "(embedded_at IS NULL OR embedded_at < updated_at)").fetchone()[0]
        size = conn.execute(
            "SELECT coalesce(SUM(size), 0) FROM documents WHERE deleted_at IS NULL AND kind = 'file'"
        ).fetchone()[0]
    finally:
        conn.close()
    return {"docs": by, "origins": origins, "chunks": chunks, "cards_pending": cards_todo, "bytes": size}


def import_known(path, source):
    conn = _connect(path)
    try:
        return {r["ref"]: [r["mtime"], r["size"]] for r in conn.execute(
            "SELECT ref, mtime, size FROM sources WHERE source = ?", (source,))}
    finally:
        conn.close()


def add_import_doc(path, source, ref, mtime, size, filename, mime, sha256, origin_path):
    """Datei aus einer Quelle (Mac, OneDrive, …) in die Bibliothek aufnehmen.
    Gleicher Inhalt → vorhandenes Material wird wiederverwendet."""
    now = time.time()
    with _write_lock:
        conn = _connect(path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            d = conn.execute(
                "SELECT * FROM documents WHERE kind = 'file' AND sha256 = ? "
                "ORDER BY deleted_at IS NOT NULL, id LIMIT 1", (sha256,)
            ).fetchone()
            if d:
                doc_id = d["id"]
                conn.execute(
                    "UPDATE documents SET deleted_at = NULL, updated_at = ?, "
                    "origin = coalesce(origin, ?), origin_path = coalesce(origin_path, ?) WHERE id = ?",
                    (now, source, origin_path, doc_id))
                created = False
            else:
                cur = conn.execute(
                    "INSERT INTO documents (kind, title, filename, mime, size, sha256, created_at, "
                    "updated_at, origin, origin_path) VALUES ('file', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (os.path.splitext(filename)[0], filename, mime, size, sha256, now, now,
                     source, origin_path))
                doc_id = cur.lastrowid
                created = True
            conn.execute(
                "INSERT INTO sources (source, ref, doc_id, mtime, size, seen_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(source, ref) DO UPDATE SET doc_id = excluded.doc_id, mtime = excluded.mtime, "
                "size = excluded.size, seen_at = excluded.seen_at",
                (source, ref, doc_id, mtime, size, now))
            _reindex_doc(conn, doc_id)
            conn.commit()
            return {"id": doc_id, "created": created}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


# ─── Verarbeitung: Text & Vektoren (für worker.py) ────────────────────

def pending_docs(path, limit=5):
    conn = _connect(path)
    try:
        return conn.execute(
            "SELECT * FROM documents WHERE text_status = 'pending' AND deleted_at IS NULL "
            "ORDER BY id LIMIT ?", (limit,)).fetchall()
    finally:
        conn.close()


def store_doc_result(path, doc_id, status_, text=None, error=None, chunks=(), model=None):
    """chunks: Liste von (text, embedding_bytes)."""
    now = time.time()
    with _write_lock:
        conn = _connect(path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            if text is not None:
                conn.execute(
                    "INSERT INTO doc_text (doc_id, text, extracted_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(doc_id) DO UPDATE SET text = excluded.text, extracted_at = excluded.extracted_at",
                    (doc_id, text, now))
            conn.execute("DELETE FROM chunks WHERE owner_kind = 'doc' AND owner_id = ?", (str(doc_id),))
            conn.executemany(
                "INSERT INTO chunks (owner_kind, owner_id, idx, text, embedding, model) VALUES ('doc', ?, ?, ?, ?, ?)",
                [(str(doc_id), i, t, e, model) for i, (t, e) in enumerate(chunks)])
            conn.execute(
                "UPDATE documents SET text_status = ?, text_error = ?, processed_at = ? WHERE id = ?",
                (status_, error, now, doc_id))
            _reindex_doc(conn, doc_id)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def cards_to_embed(path, limit=32):
    conn = _connect(path)
    try:
        return conn.execute(
            "SELECT * FROM cards WHERE deleted_at IS NULL AND "
            "(embedded_at IS NULL OR embedded_at < updated_at) ORDER BY pos LIMIT ?", (limit,)).fetchall()
    finally:
        conn.close()


def store_card_vectors(path, items, model):
    """items: Liste von (card_id, text, embedding_bytes, updated_at_beim_lesen)."""
    with _write_lock:
        conn = _connect(path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            for cid, text, emb, upd in items:
                conn.execute("DELETE FROM chunks WHERE owner_kind = 'card' AND owner_id = ?", (cid,))
                conn.execute(
                    "INSERT INTO chunks (owner_kind, owner_id, idx, text, embedding, model) VALUES ('card', ?, 0, ?, ?, ?)",
                    (cid, text, emb, model))
                conn.execute("UPDATE cards SET embedded_at = ? WHERE id = ?", (upd, cid))
            # Vektoren gelöschter Karten entfernen
            conn.execute(
                "DELETE FROM chunks WHERE owner_kind = 'card' AND owner_id IN "
                "(SELECT id FROM cards WHERE deleted_at IS NOT NULL)")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def vector_signature(path):
    conn = _connect(path)
    try:
        r = conn.execute("SELECT COUNT(*), coalesce(MAX(id), 0) FROM chunks").fetchone()
        return (r[0], r[1])
    finally:
        conn.close()


def all_vectors(path):
    conn = _connect(path)
    try:
        return conn.execute("SELECT id, owner_kind, owner_id, embedding FROM chunks ORDER BY id").fetchall()
    finally:
        conn.close()


def chunk_texts(path, ids):
    if not ids:
        return {}
    conn = _connect(path)
    try:
        q = "SELECT id, text FROM chunks WHERE id IN (%s)" % ",".join("?" * len(ids))
        return {r["id"]: r["text"] for r in conn.execute(q, list(ids))}
    finally:
        conn.close()


def docs_by_ids(path, ids):
    if not ids:
        return {}
    conn = _connect(path)
    try:
        q = "SELECT * FROM documents WHERE deleted_at IS NULL AND id IN (%s)" % ",".join("?" * len(ids))
        out = {}
        for r in conn.execute(q, [int(i) for i in ids]):
            item = _doc_dict(r)
            item["cards"] = [x["card_id"] for x in conn.execute(
                "SELECT cd.card_id FROM card_documents cd JOIN cards c ON c.id = cd.card_id "
                "WHERE cd.doc_id = ? AND c.deleted_at IS NULL", (r["id"],))]
            out[str(r["id"])] = item
        return out
    finally:
        conn.close()


def alive_card_ids(path):
    conn = _connect(path)
    try:
        return {r[0] for r in conn.execute("SELECT id FROM cards WHERE deleted_at IS NULL")}
    finally:
        conn.close()


def card_doc_ids(path, card_id):
    conn = _connect(path)
    try:
        return {str(r[0]) for r in conn.execute(
            "SELECT doc_id FROM card_documents WHERE card_id = ?", (card_id,))}
    finally:
        conn.close()


def card_vector(path, card_id):
    conn = _connect(path)
    try:
        r = conn.execute(
            "SELECT embedding FROM chunks WHERE owner_kind = 'card' AND owner_id = ?", (card_id,)).fetchone()
        return r["embedding"] if r else None
    finally:
        conn.close()


# ─── Karten einzeln bearbeiten (Etappe 4: Claude) ─────────────────────

def _row_to_card(r):
    card = {"id": r["id"]}
    for k in CORE:
        if r[k] is not None:
            card[k] = r[k]
    card.update(json.loads(r["extra"] or "{}"))
    card["created_at"] = r["created_at"]
    card["updated_at"] = r["updated_at"]
    return card


def _check_fields(f):
    if "col" in f and f["col"] not in COLUMNS:
        raise ValueError("Unbekannte Spalte '%s'. Erlaubt: %s" % (f["col"], ", ".join(COLUMNS)))
    if "cat" in f and f["cat"] not in CATEGORIES:
        raise ValueError("Unbekannte Kategorie '%s'. Erlaubt: %s" % (f["cat"], ", ".join(CATEGORIES)))
    if "prio" in f and f["prio"] not in PRIORITIES:
        raise ValueError("Unbekannte Priorität '%s'. Erlaubt: %s" % (f["prio"], ", ".join(PRIORITIES)))
    for k, v in f.items():
        if k not in CORE:
            raise ValueError("Unbekanntes Feld: %s" % k)
        if not isinstance(v, str):
            raise ValueError("Feld %s muss Text sein" % k)
        if len(v) > 20000:
            raise ValueError("Feld %s ist zu lang" % k)


def log_activity(conn, actor, action, detail):
    conn.execute("INSERT INTO activity (ts, actor, action, detail) VALUES (?, ?, ?, ?)",
                 (time.time(), actor, action, json.dumps(detail, ensure_ascii=False)))


def log(path, actor, action, detail):
    with _write_lock:
        conn = _connect(path)
        try:
            log_activity(conn, actor, action, detail)
            conn.commit()
        finally:
            conn.close()


def get_card(path, card_id):
    conn = _connect(path)
    try:
        r = conn.execute("SELECT * FROM cards WHERE id = ? AND deleted_at IS NULL", (card_id,)).fetchone()
    finally:
        conn.close()
    if not r:
        raise NotFound("Karte '%s' nicht gefunden" % card_id)
    return _row_to_card(r)


def list_cards(path):
    conn = _connect(path)
    try:
        return [_row_to_card(r) for r in conn.execute(
            "SELECT * FROM cards WHERE deleted_at IS NULL ORDER BY pos")]
    finally:
        conn.close()


def create_card(path, fields, actor="claude"):
    f = {"col": "eingang", "prio": "Mittel", "owner": "Robert", "sub": "", "note": "", "src": ""}
    f.update({k: v for k, v in fields.items() if v is not None})
    if not (f.get("t") or "").strip():
        raise ValueError("Titel fehlt")
    if not f.get("cat"):
        raise ValueError("Kategorie fehlt")
    _check_fields(f)
    now = time.time()
    with _write_lock:
        conn = _connect(path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            while True:
                cid = "c" + secrets.token_hex(4)
                if not conn.execute("SELECT 1 FROM cards WHERE id = ?", (cid,)).fetchone():
                    break
            pos = conn.execute("SELECT coalesce(max(pos), -1) + 1 FROM cards").fetchone()[0]
            conn.execute(
                "INSERT INTO cards (id, pos, %s, extra, created_at, updated_at) VALUES (?, ?, %s, ?, ?, ?)"
                % (", ".join(CORE), ", ".join("?" for _ in CORE)),
                (cid, pos) + tuple(f.get(k) for k in CORE) + (json.dumps({"tools": []}), now, now))
            _next_ts(conn)
            _reindex_cards(conn)
            log_activity(conn, actor, "karte_anlegen", {"id": cid, "t": f["t"]})
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    return get_card(path, cid)


def update_card(path, card_id, changes, note_append=None, actor="claude"):
    changes = {k: v for k, v in changes.items() if v is not None}
    _check_fields(changes)
    if not changes and not note_append:
        raise ValueError("Keine Änderung angegeben")
    now = time.time()
    with _write_lock:
        conn = _connect(path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            r = conn.execute("SELECT * FROM cards WHERE id = ? AND deleted_at IS NULL", (card_id,)).fetchone()
            if not r:
                raise NotFound("Karte '%s' nicht gefunden" % card_id)
            if note_append:
                old = changes.get("note", r["note"] or "")
                changes["note"] = (old.rstrip() + "\n\n" if old.strip() else "") + note_append.strip()
            before = {k: r[k] for k in changes}
            sets = ", ".join("%s = ?" % k for k in changes)
            conn.execute("UPDATE cards SET %s, updated_at = ? WHERE id = ?" % sets,
                         tuple(changes.values()) + (now, card_id))
            _next_ts(conn)
            _reindex_cards(conn)
            log_activity(conn, actor, "karte_aendern", {"id": card_id, "vorher": before, "nachher": changes})
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    return get_card(path, card_id)


def get_doc(path, doc_id):
    """Material mit ausgelesenem Text und verknüpften Karten."""
    conn = _connect(path)
    try:
        d = conn.execute("SELECT d.*, t.text AS fulltext FROM documents d "
                         "LEFT JOIN doc_text t ON t.doc_id = d.id "
                         "WHERE d.id = ? AND d.deleted_at IS NULL", (doc_id,)).fetchone()
        if not d:
            raise NotFound("Dokument %s nicht gefunden" % doc_id)
        item = _doc_dict(d)
        item["sha256"] = d["sha256"]
        item["text"] = d["fulltext"]
        item["cards"] = [dict(id=x["id"], t=x["t"]) for x in conn.execute(
            "SELECT c.id, c.t FROM card_documents cd JOIN cards c ON c.id = cd.card_id "
            "WHERE cd.doc_id = ? AND c.deleted_at IS NULL", (doc_id,))]
        return item
    finally:
        conn.close()


def recent_activity(path, limit=50):
    conn = _connect(path)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT ts, actor, action, detail FROM activity ORDER BY id DESC LIMIT ?", (limit,))]
    finally:
        conn.close()


# ─── OAuth-Speicher (Etappe 4) ────────────────────────────────────────

def oauth_exec(path, sql, args=()):
    with _write_lock:
        conn = _connect(path)
        try:
            cur = conn.execute(sql, args)
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()


def oauth_query(path, sql, args=()):
    conn = _connect(path)
    try:
        return [dict(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


def oauth_take_code(path, code_hash):
    """Code holen und sofort löschen (nur einmal verwendbar)."""
    with _write_lock:
        conn = _connect(path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            r = conn.execute("SELECT * FROM oauth_codes WHERE code_hash = ?", (code_hash,)).fetchone()
            conn.execute("DELETE FROM oauth_codes WHERE code_hash = ? OR expires_at < ?",
                         (code_hash, time.time()))
            conn.commit()
            return dict(r) if r else None
        finally:
            conn.close()


def oauth_take_refresh(path, token_hash):
    """Refresh-Token einlösen: gültig → löschen und Daten zurückgeben (Rotation)."""
    with _write_lock:
        conn = _connect(path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            r = conn.execute("SELECT * FROM oauth_tokens WHERE token_hash = ? AND kind = 'refresh'",
                             (token_hash,)).fetchone()
            if r:
                conn.execute("DELETE FROM oauth_tokens WHERE token_hash = ?", (token_hash,))
            conn.execute("DELETE FROM oauth_tokens WHERE expires_at < ?", (time.time(),))
            conn.commit()
            return dict(r) if r else None
        finally:
            conn.close()
