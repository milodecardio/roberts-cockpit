#!/usr/bin/env python3
"""Server für Roberts Cockpit.
Serviert cockpit.html + PWA-Dateien, speichert/lädt die Karten in cockpit.db (SQLite).
Nur Python-Bordmittel, keine Installation nötig.

Lokal (macOS, Doppelklick-Flow): unverändert wie bisher, öffnet den Browser.
Produktion (VPS, hinter Caddy/nginx): COCKPIT_ENV=production setzen, dann
läuft der Server ohne Browser-Auto-Open, gebunden an 127.0.0.1.

Env-Variablen:
  COCKPIT_USER, COCKPIT_PASS   HTTP-Basic-Auth-Zugangsdaten. Wenn eine der
                                beiden fehlt, läuft der Server ohne Login
                                (nur für lokale Nutzung empfohlen).
  COCKPIT_HOST                 Bind-Adresse, Default 127.0.0.1
  COCKPIT_PORT                 Port, Default 8077
  COCKPIT_ENV                  "production" unterdrückt den Browser-Auto-Open
"""
import base64
import hashlib
import hmac
import http.server
import json
import mimetypes
import os
import re
import socket
import socketserver
import tempfile
import threading
import urllib.parse
import webbrowser

try:
    import numpy as np  # schnelle Vektorsuche (Server: python3-numpy); ohne geht es langsamer
except ImportError:
    np = None

import db
import embed

HERE = os.path.dirname(os.path.abspath(__file__))
# Übergangsweise weiter mitgeschrieben (Rückweg + lesbares Backup).
STATE = os.path.join(HERE, "cockpit_state.json")
DB_PATH = os.environ.get("COCKPIT_DB", os.path.join(HERE, "cockpit.db"))
# Hochgeladene Dateien (später ggf. Hetzner Storage Box an diesem Pfad einhängen)
FILES_DIR = os.environ.get("COCKPIT_FILES", os.path.join(HERE, "files"))
MAX_UPLOAD = int(os.environ.get("COCKPIT_MAX_UPLOAD_MB", "1024")) * 1024 * 1024

# Diese Typen darf der Browser direkt anzeigen; alles andere (z. B. HTML, SVG)
# wird nur als Download ausgeliefert – Schutz vor eingeschleustem Skriptcode.
INLINE_MIME = re.compile(r"^(image/(png|jpeg|gif|webp|heic|heif|avif)|application/pdf|"
                         r"audio/.+|video/.+|text/plain)$")

HOST = os.environ.get("COCKPIT_HOST", "127.0.0.1")
PORT = int(os.environ.get("COCKPIT_PORT", "8077"))
PRODUCTION = os.environ.get("COCKPIT_ENV") == "production"
URL = "http://%s:%d/" % (HOST, PORT)

AUTH_USER = os.environ.get("COCKPIT_USER")
AUTH_PASS = os.environ.get("COCKPIT_PASS")

STATIC_FILES = {
    "/": ("cockpit.html", "text/html; charset=utf-8"),
    "/index.html": ("cockpit.html", "text/html; charset=utf-8"),
    "/manifest.json": ("manifest.json", "application/manifest+json"),
    "/service-worker.js": ("service-worker.js", "application/javascript"),
    "/icons/icon-192.png": ("icons/icon-192.png", "image/png"),
    "/icons/icon-512.png": ("icons/icon-512.png", "image/png"),
}

# PWA-Dateien ohne Login ausliefern: Chrome/Android lädt Manifest und Icons
# ohne Zugangsdaten – mit 401 gilt die App als „nicht installierbar“.
# Enthalten keine persönlichen Daten.
PUBLIC_PATHS = {
    "/manifest.json",
    "/service-worker.js",
    "/icons/icon-192.png",
    "/icons/icon-512.png",
}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, ctype, data, extra_headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, "application/json", json.dumps(obj).encode())

    def _authorized(self):
        if not AUTH_USER or not AUTH_PASS:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
            user, _, pw = decoded.partition(":")
        except Exception:
            return False
        return hmac.compare_digest(user, AUTH_USER) and hmac.compare_digest(pw, AUTH_PASS)

    def _require_auth(self):
        self._send(
            401, "text/plain", b"Login erforderlich",
            {"WWW-Authenticate": 'Basic realm="Roberts Cockpit"'},
        )

    # ─── Materialien & Suche (Etappe 2) ───────────────────────────────

    def _read_json(self, limit=2 * 1024 * 1024):
        length = int(self.headers.get("Content-Length", 0))
        if length > limit:
            raise ValueError("Anfrage zu groß")
        return json.loads(self.rfile.read(length) or b"{}")

    def _api(self, method):
        """Gibt True zurück, wenn die Anfrage hier behandelt wurde."""
        parts = urllib.parse.urlsplit(self.path)
        p = parts.path
        try:
            if method == "GET" and p == "/api/doc-counts":
                self._json(200, db.doc_counts(DB_PATH))
                return True
            qs = {k: v[0] for k, v in urllib.parse.parse_qs(parts.query).items()}
            if method == "GET" and p == "/api/search":
                self._json(200, db.search(DB_PATH, qs.get("q", "")))
                return True
            # ─── Etappe 3: Bedeutungssuche, Bibliothek, Import ───
            if method == "GET" and p == "/api/semantic":
                self._json(200, VECTORS.search_text(qs.get("q", "")))
                return True
            if method == "GET" and p == "/api/status":
                self._json(200, db.status(DB_PATH))
                return True
            if method == "GET" and p == "/api/library":
                self._json(200, db.library(DB_PATH, q=qs.get("q", ""), origin=qs.get("origin", ""),
                                           status=qs.get("status", ""),
                                           limit=min(int(qs.get("limit", "100")), 500),
                                           offset=int(qs.get("offset", "0"))))
                return True
            if method == "GET" and p == "/api/import/known":
                self._json(200, db.import_known(DB_PATH, qs.get("source", "")))
                return True
            if method == "POST" and p == "/api/import/file":
                self._json(200, self._receive_import())
                return True
            m = re.fullmatch(r"/api/cards/([^/]+)/(related|attach)", p)
            if m:
                card_id = urllib.parse.unquote(m.group(1))
                if method == "GET" and m.group(2) == "related":
                    self._json(200, VECTORS.related_to_card(card_id))
                elif method == "POST" and m.group(2) == "attach":
                    db.attach_doc(DB_PATH, card_id, int(self._read_json().get("doc_id", 0)))
                    self._json(200, {"ok": True})
                else:
                    self._send(405, "text/plain", b"method not allowed")
                return True
            m = re.fullmatch(r"/files/(\d+)(?:/.*)?", p)
            if method == "GET" and m:
                self._send_file(int(m.group(1)))
                return True
            m = re.fullmatch(r"/api/cards/([^/]+)/(docs|files|links|notes)(?:/(\d+))?", p)
            if not m:
                return False
            card_id = urllib.parse.unquote(m.group(1))
            what, doc_id = m.group(2), m.group(3)
            if method == "GET" and what == "docs" and not doc_id:
                self._json(200, db.card_docs(DB_PATH, card_id))
            elif method == "DELETE" and what == "docs" and doc_id:
                db.unlink_doc(DB_PATH, card_id, int(doc_id))
                self._json(200, {"ok": True})
            elif method == "POST" and what == "files" and not doc_id:
                self._json(200, self._receive_file(card_id))
            elif method == "POST" and what == "links" and not doc_id:
                data = self._read_json()
                url = (data.get("url") or "").strip()
                if not re.match(r"^https?://", url, re.I):
                    raise ValueError("Link muss mit http:// oder https:// beginnen")
                self._json(200, db.add_doc(DB_PATH, card_id, "link",
                                           title=(data.get("title") or "").strip() or url, url=url))
            elif method == "POST" and what == "notes" and not doc_id:
                data = self._read_json()
                body = (data.get("body") or "").strip()
                title = (data.get("title") or "").strip() or body.split("\n", 1)[0][:80]
                if not body and not title:
                    raise ValueError("Notiz ist leer")
                self._json(200, db.add_doc(DB_PATH, card_id, "note", title=title, body=body))
            else:
                self._send(405, "text/plain", b"method not allowed")
            return True
        except db.NotFound as e:
            self._json(404, {"error": str(e)})
        except ValueError as e:
            self._json(400, {"error": str(e)})
        except Exception as e:
            self._json(500, {"error": str(e)})
        return True

    def _receive_file(self, card_id):
        sha, filename, mime, length = self._store_upload()
        title = os.path.splitext(filename)[0]
        return db.add_doc(DB_PATH, card_id, "file", title=title, filename=filename,
                          mime=mime, size=length, sha256=sha)

    def _receive_import(self):
        """Datei aus dem Import-Skript (Mac/OneDrive/…) in die Bibliothek aufnehmen."""
        source = (self.headers.get("X-Source") or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9_-]{1,40}", source):
            raise ValueError("X-Source fehlt oder ungültig")
        ref = urllib.parse.unquote(self.headers.get("X-Source-Ref", "")).strip()
        if not ref:
            raise ValueError("X-Source-Ref fehlt")
        try:
            mtime = float(self.headers.get("X-Source-Mtime", "0"))
        except ValueError:
            mtime = 0.0
        origin_path = urllib.parse.unquote(self.headers.get("X-Origin-Path", "")).strip() or None
        sha, filename, mime, length = self._store_upload()
        return db.add_import_doc(DB_PATH, source, ref, mtime, length, filename, mime, sha, origin_path)

    def _store_upload(self):
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            raise ValueError("Leere Datei")
        if length > MAX_UPLOAD:
            raise ValueError("Datei zu groß (max. %d MB)" % (MAX_UPLOAD // 1024 // 1024))
        filename = urllib.parse.unquote(self.headers.get("X-Filename", "")).strip()
        filename = os.path.basename(filename.replace("\\", "/")) or "datei"
        mime = (self.headers.get("Content-Type") or "").split(";")[0].strip()
        if not mime or mime == "application/octet-stream":
            mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        tmp_dir = os.path.join(FILES_DIR, ".tmp")
        os.makedirs(tmp_dir, exist_ok=True)
        h = hashlib.sha256()
        fd, tmp = tempfile.mkstemp(dir=tmp_dir)
        try:
            with os.fdopen(fd, "wb") as f:
                left = length
                while left > 0:
                    chunk = self.rfile.read(min(1024 * 1024, left))
                    if not chunk:
                        raise ValueError("Upload abgebrochen")
                    h.update(chunk)
                    f.write(chunk)
                    left -= len(chunk)
            sha = h.hexdigest()
            dest = _blob_path(sha)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            if os.path.exists(dest):
                os.remove(tmp)
            else:
                os.replace(tmp, dest)
        except Exception:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise
        return sha, filename, mime, length

    def _send_file(self, doc_id):
        d = db.get_file_doc(DB_PATH, doc_id)
        path = _blob_path(d["sha256"])
        size = os.path.getsize(path)
        mime = d["mime"] or "application/octet-stream"
        inline = bool(INLINE_MIME.match(mime))
        disp = "inline" if inline else "attachment"
        headers = {
            "Content-Disposition": "%s; filename*=UTF-8''%s" % (disp, urllib.parse.quote(d["filename"] or "datei")),
            "X-Content-Type-Options": "nosniff",
            "Accept-Ranges": "bytes",
            "Cache-Control": "private, max-age=86400",
        }
        if not inline:
            mime = "application/octet-stream"
        if mime != "application/pdf":
            # Chromes PDF-Betrachter verträgt keine Sandbox; alle anderen Typen schon.
            headers["Content-Security-Policy"] = (
                "sandbox; default-src 'none'; img-src 'self'; media-src 'self'; style-src 'unsafe-inline'")
        start, end = 0, size - 1
        rng = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", "").strip())
        code = 200
        if rng and size > 0:
            a, b = rng.group(1), rng.group(2)
            if a:
                start = int(a)
                end = min(int(b), size - 1) if b else size - 1
            elif b:
                start = max(0, size - int(b))
            if start > end or start >= size:
                self._send(416, "text/plain", b"", {"Content-Range": "bytes */%d" % size})
                return
            code = 206
            headers["Content-Range"] = "bytes %d-%d/%d" % (start, end, size)
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(end - start + 1))
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(1024 * 1024, left))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return  # Browser hat abgebrochen (z. B. beim Vorspulen)
                left -= len(chunk)

    def do_DELETE(self):
        if not self._authorized():
            self._require_auth()
            return
        if not self._api("DELETE"):
            self._send(404, "text/plain", b"not found")

    def do_POST(self):
        if not self._authorized():
            self._require_auth()
            return
        if self._api("POST"):
            return
        if self.path == "/state":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                state = json.loads(body)
                db.save_state(DB_PATH, state)
            except db.EmptySaveRefused:
                self._json(409, {"error": "Leerer Stand abgelehnt (würde alle Karten löschen)"})
                return
            except ValueError as e:
                self._json(400, {"error": str(e)})
                return
            except Exception as e:
                self._json(500, {"error": str(e)})
                return
            try:
                write_json_mirror()
            except Exception:
                pass  # Spiegel ist nur Komfort, Datenbank ist maßgeblich
            self._json(200, {"ok": True})
        else:
            self._send(404, "text/plain", b"not found")

    def do_GET(self):
        if self.path not in PUBLIC_PATHS and not self._authorized():
            self._require_auth()
            return

        if self._api("GET"):
            return

        if self.path in STATIC_FILES:
            rel, ctype = STATIC_FILES[self.path]
            try:
                with open(os.path.join(HERE, rel), "rb") as f:
                    self._send(200, ctype, f.read())
            except Exception as e:
                self._send(500, "text/plain; charset=utf-8", str(e).encode())
            return

        if self.path == "/state":
            try:
                if not db.has_any_cards(DB_PATH):
                    self._send(404, "application/json", b"null")
                    return
                data = json.dumps(db.load_state(DB_PATH), ensure_ascii=False).encode("utf-8")
                self._send(200, "application/json", data)
            except Exception as e:
                self._json(500, {"error": str(e)})
            return

        self._send(404, "text/plain", b"not found")


class VectorIndex:
    """Hält alle Vektoren im Speicher (numpy, falls vorhanden) und lädt neu, wenn sich
    die Datenbank geändert hat. Bei einigen zehntausend Abschnitten völlig ausreichend."""

    def __init__(self):
        self.lock = threading.Lock()
        self.sig = None
        self.rows = []      # (chunk_id, owner_kind, owner_id)
        self.mat = None     # numpy-Matrix oder Liste von array('f')

    def _refresh(self):
        sig = db.vector_signature(DB_PATH)
        if sig == self.sig:
            return
        data = db.all_vectors(DB_PATH)
        rows = [(r["id"], r["owner_kind"], r["owner_id"]) for r in data]
        vecs = [embed.from_blob(r["embedding"]) for r in data]
        if np is not None and vecs:
            mat = np.vstack([np.frombuffer(v.tobytes(), dtype=np.float32) for v in vecs])
        else:
            mat = vecs
        self.rows, self.mat, self.sig = rows, mat, sig

    def _scores(self, qv):
        if np is not None and self.mat is not None and len(self.rows):
            return (self.mat @ np.asarray(qv, dtype=np.float32)).tolist()
        return [sum(a * b for a, b in zip(v, qv)) for v in (self.mat or [])]

    def _rank(self, qv, exclude_card=None, exclude_docs=(), k_cards=8, k_docs=12):
        with self.lock:
            self._refresh()
            scores = self._scores(qv)
            rows = self.rows
        best = {}
        for (cid, kind, oid), s in zip(rows, scores):
            if kind == "card" and oid == exclude_card:
                continue
            if kind == "doc" and oid in exclude_docs:
                continue
            key = (kind, oid)
            if key not in best or s > best[key][0]:
                best[key] = (s, cid)
        ranked = sorted(best.items(), key=lambda kv: -kv[1][0])
        alive = db.alive_card_ids(DB_PATH)
        cards = [(oid, s, cid) for (kind, oid), (s, cid) in ranked if kind == "card" and oid in alive][:k_cards]
        docs_r = [(oid, s, cid) for (kind, oid), (s, cid) in ranked if kind == "doc"][:k_docs * 2]
        docs_meta = db.docs_by_ids(DB_PATH, [oid for oid, _, _ in docs_r])
        docs_r = [x for x in docs_r if x[0] in docs_meta][:k_docs]
        texts = db.chunk_texts(DB_PATH, [cid for _, _, cid in cards + docs_r])
        out_docs = []
        for oid, s, cid in docs_r:
            item = docs_meta[oid]
            item["score"] = round(s, 3)
            item["snippet"] = (texts.get(cid) or "")[:300]
            out_docs.append(item)
        return {
            "available": True,
            "cards": [{"id": oid, "score": round(s, 3)} for oid, s, _ in cards],
            "docs": out_docs,
        }

    def search_text(self, q):
        q = (q or "").strip()
        if not q:
            return {"available": True, "cards": [], "docs": []}
        try:
            qv = embed.embed([q], timeout=60)[0]
        except embed.Unavailable:
            return {"available": False, "cards": [], "docs": []}
        return self._rank(qv)

    def related_to_card(self, card_id):
        blob = db.card_vector(DB_PATH, card_id)
        if blob is None:
            return {"available": False, "cards": [], "docs": []}
        qv = list(embed.from_blob(blob))
        return self._rank(qv, exclude_card=card_id,
                          exclude_docs=db.card_doc_ids(DB_PATH, card_id), k_cards=5, k_docs=6)


VECTORS = VectorIndex()


def _blob_path(sha):
    """Dateien liegen nach Inhalt (SHA-256) abgelegt: files/ab/cd/abcd…"""
    if not re.fullmatch(r"[0-9a-f]{64}", sha or ""):
        raise db.NotFound("Datei nicht gefunden")
    return os.path.join(FILES_DIR, sha[:2], sha[2:4], sha)


def write_json_mirror():
    """Schreibt den aktuellen Stand zusätzlich als JSON (atomar)."""
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db.load_state(DB_PATH), f, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE)


def port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex((HOST, port)) == 0


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    if not PRODUCTION and port_in_use(PORT):
        webbrowser.open(URL)
        return
    imported = db.init(DB_PATH, legacy_json=STATE)
    if imported:
        print("Datenbank angelegt, %d Karten aus %s übernommen." % (imported, STATE), flush=True)
    httpd = ThreadingServer((HOST, PORT), Handler)
    if not PRODUCTION:
        webbrowser.open(URL)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


if __name__ == "__main__":
    main()
