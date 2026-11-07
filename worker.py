#!/usr/bin/env python3
"""Hintergrund-Verarbeitung für Roberts Cockpit (Etappe 3).

Läuft als eigener Dienst (deploy/cockpit-worker.service) und arbeitet in einer Schleife:
  1. Neue Materialien: Text auslesen (PDF, Office, OCR für Scans/Fotos), in Abschnitte
     teilen, mit bge-m3 vektorisieren.
  2. Geänderte Karten: Titel + Notiz vektorisieren.
Läuft mit niedriger Priorität, damit das Cockpit flüssig bleibt.

  python3 worker.py          Dauerbetrieb
  python3 worker.py --once   einmal alles Offene abarbeiten, dann beenden
"""
import os
import sys
import time
import traceback

import db
import embed
import extract

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("COCKPIT_DB", os.path.join(HERE, "cockpit.db"))
FILES_DIR = os.environ.get("COCKPIT_FILES", os.path.join(HERE, "files"))
BATCH = 8
IDLE_SLEEP = 20


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def blob_path(sha):
    return os.path.join(FILES_DIR, sha[:2], sha[2:4], sha)


def embed_chunks(title, pieces):
    """Abschnitte vektorisieren; der Titel wird vorangestellt, damit Treffer Kontext haben."""
    out = []
    for i in range(0, len(pieces), BATCH):
        batch = pieces[i:i + BATCH]
        vecs = embed.embed(["%s\n%s" % (title, p) if title else p for p in batch])
        out.extend(zip(batch, (embed.to_blob(v) for v in vecs)))
    return out


def process_doc(d):
    title = d["title"] or d["filename"] or ""
    if d["kind"] == "note":
        status, text, hint = "done", "\n".join(x for x in (d["title"], d["body"]) if x), None
    elif d["kind"] == "link":
        status, text, hint = "done", "%s\n%s" % (d["title"] or "", d["url"] or ""), None
    else:
        path = blob_path(d["sha256"] or "")
        if not os.path.exists(path):
            db.store_doc_result(DB_PATH, d["id"], "failed", error="Datei fehlt auf dem Server")
            return "failed"
        t0 = time.time()
        status, text, hint = extract.extract(path, d["mime"], d["filename"] or "")
        log("doc %s %r: %s (%d Zeichen, %.1fs)%s" % (
            d["id"], d["filename"], status, len(text or ""), time.time() - t0,
            " – " + hint if hint else ""))
    if status != "done":
        # Auch ohne Inhaltstext: Titel/Dateiname vektorisieren, damit die Datei auffindbar ist
        base = " ".join(x for x in (d["title"], d["filename"], d["origin_path"]) if x)
        chunks = embed_chunks("", [base]) if base else []
        db.store_doc_result(DB_PATH, d["id"], status, error=hint, chunks=chunks, model=embed.MODEL)
        return status
    pieces = extract.chunk(text) or [title]
    chunks = embed_chunks(title, pieces)
    db.store_doc_result(DB_PATH, d["id"], "done", text=text, error=hint, chunks=chunks, model=embed.MODEL)
    return "done"


def card_text(c):
    return "\n".join(x for x in (c["t"], c["sub"], c["note"]) if x)


def process_cards():
    cards = db.cards_to_embed(DB_PATH, limit=64)
    if not cards:
        return 0
    items = []
    for i in range(0, len(cards), BATCH):
        batch = cards[i:i + BATCH]
        texts = [card_text(c) or "(leer)" for c in batch]
        vecs = embed.embed(texts)
        for c, t, v in zip(batch, texts, vecs):
            items.append((c["id"], t, embed.to_blob(v), c["updated_at"]))
    db.store_card_vectors(DB_PATH, items, embed.MODEL)
    log("%d Karten vektorisiert" % len(items))
    return len(items)


def run(once=False):
    db.init(DB_PATH)
    try:
        os.nice(10)
    except OSError:
        pass
    log("Worker gestartet (DB %s, Modell %s)" % (DB_PATH, embed.MODEL))
    while True:
        worked = 0
        try:
            worked += process_cards()
            for d in db.pending_docs(DB_PATH, limit=5):
                try:
                    process_doc(d)
                except embed.Unavailable:
                    raise
                except Exception as e:  # noqa: BLE001
                    log("doc %s Fehler: %s" % (d["id"], e))
                    traceback.print_exc()
                    db.store_doc_result(DB_PATH, d["id"], "failed", error=str(e)[:300])
                worked += 1
        except embed.Unavailable as e:
            log(str(e), "– neuer Versuch in 60 s")
            if once:
                return 1
            time.sleep(60)
            continue
        if not worked:
            if once:
                return 0
            time.sleep(IDLE_SLEEP)


if __name__ == "__main__":
    sys.exit(run(once="--once" in sys.argv))
