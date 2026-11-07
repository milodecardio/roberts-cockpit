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
import hmac
import http.server
import json
import os
import socket
import socketserver
import webbrowser

import db

HERE = os.path.dirname(os.path.abspath(__file__))
# Übergangsweise weiter mitgeschrieben (Rückweg + lesbares Backup).
STATE = os.path.join(HERE, "cockpit_state.json")
DB_PATH = os.environ.get("COCKPIT_DB", os.path.join(HERE, "cockpit.db"))

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

    def do_POST(self):
        if not self._authorized():
            self._require_auth()
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
