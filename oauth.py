"""OAuth 2.1 für den Claude-Connector (Etappe 4).

claude.ai unterstützt bei eigenen Connectors keine Basic-Auth, sondern OAuth:
  1. claude.ai liest /.well-known/oauth-protected-resource und
     /.well-known/oauth-authorization-server
  2. registriert sich selbst (POST /oauth/register, „Dynamic Client Registration“)
  3. schickt Robert auf /oauth/authorize – dort meldet er sich mit dem
     Cockpit-Passwort an (Basic-Auth wie beim Cockpit) und klickt „Erlauben“
  4. tauscht den Code gegen Zugangs-Token (POST /oauth/token, PKCE S256)
  5. ruft /mcp mit „Authorization: Bearer …“ auf

Gespeichert werden nur SHA-256-Hashes der Codes und Token.
Nur Python-Bordmittel.

Verwaltung auf dem Server:
  python3 oauth.py --liste        verbundene Clients anzeigen
  python3 oauth.py --trennen      alle Verbindungen sofort trennen (Token löschen)
"""
import base64
import hashlib
import html
import json
import os
import re
import secrets
import threading
import time
import urllib.parse

import db

ACCESS_TTL = 8 * 3600          # Zugangs-Token: 8 Stunden
REFRESH_TTL = 90 * 24 * 3600   # Erneuerungs-Token: 90 Tage (wird bei jeder Nutzung erneuert)
CODE_TTL = 300                 # Anmelde-Code: 5 Minuten
MAX_CLIENTS = 50

# Nur Rücksprung-Adressen von Claude (Web/Desktop/Handy über claude.ai) und
# lokale Adressen (Claude Code auf dem Mac) sind erlaubt.
ALLOWED_REDIRECT = re.compile(
    r"^(https://(claude\.ai|claude\.com)/api/mcp/auth_callback"
    r"|http://(localhost|127\.0\.0\.1)(:\d{1,5})?/[^\s#]*)$"
)


class OAuthError(Exception):
    def __init__(self, error, description, status=400):
        super().__init__(description)
        self.error, self.description, self.status = error, description, status

    def body(self):
        return {"error": self.error, "error_description": self.description}


def _h(s):
    return hashlib.sha256(s.encode()).hexdigest()


def resource_url(base):
    return base.rstrip("/") + "/mcp"


def protected_resource_metadata(base):
    return {
        "resource": resource_url(base),
        "authorization_servers": [base.rstrip("/")],
        "bearer_methods_supported": ["header"],
        "scopes_supported": ["cockpit"],
        "resource_name": "Roberts Cockpit",
    }


def authorization_server_metadata(base):
    b = base.rstrip("/")
    return {
        "issuer": b,
        "authorization_endpoint": b + "/oauth/authorize",
        "token_endpoint": b + "/oauth/token",
        "registration_endpoint": b + "/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": ["cockpit"],
    }


# ─── Registrierung ────────────────────────────────────────────────────

def register(path, data):
    uris = data.get("redirect_uris")
    if not isinstance(uris, list) or not uris or len(uris) > 10:
        raise OAuthError("invalid_redirect_uri", "redirect_uris fehlt")
    for u in uris:
        if not isinstance(u, str) or not ALLOWED_REDIRECT.match(u):
            raise OAuthError("invalid_redirect_uri", "Rücksprung-Adresse nicht erlaubt: %s" % u)
    now = time.time()
    # Aufräumen: Registrierungen ohne Anmeldung nach einem Tag löschen
    db.oauth_exec(path, "DELETE FROM oauth_clients WHERE created_at < ? AND client_id NOT IN "
                        "(SELECT client_id FROM oauth_tokens)", (now - 86400,))
    if len(db.oauth_query(path, "SELECT client_id FROM oauth_clients")) >= MAX_CLIENTS:
        raise OAuthError("temporarily_unavailable", "Zu viele Registrierungen", 429)
    name = str(data.get("client_name") or "Unbekannt")[:100]
    client_id = "cc_" + secrets.token_urlsafe(18)
    db.oauth_exec(path, "INSERT INTO oauth_clients (client_id, name, redirect_uris, created_at) "
                        "VALUES (?, ?, ?, ?)", (client_id, name, json.dumps(uris), now))
    return {
        "client_id": client_id,
        "client_id_issued_at": int(now),
        "client_name": name,
        "redirect_uris": uris,
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }


def _client(path, client_id):
    rows = db.oauth_query(path, "SELECT * FROM oauth_clients WHERE client_id = ?", (client_id or "",))
    return rows[0] if rows else None


# ─── Anmeldung (Zustimmungsseite) ─────────────────────────────────────

_pending = {}            # nonce → (params, expires)
_pending_lock = threading.Lock()


class PageError(Exception):
    """Fehler, bei dem nicht zurückgeleitet werden darf (Client/Adresse unbekannt)."""


def check_authorize(path, base, qs):
    """Prüft die Anfrage. Gibt (params, client_name) zurück.
    Wirft PageError (nicht umleiten) oder (params_mit_fehler) via OAuthError."""
    client = _client(path, qs.get("client_id"))
    if not client:
        raise PageError("Unbekannte Anwendung. Bitte den Connector in Claude neu hinzufügen.")
    redirect_uri = qs.get("redirect_uri") or ""
    if redirect_uri not in json.loads(client["redirect_uris"]):
        raise PageError("Rücksprung-Adresse passt nicht zur Anwendung.")
    params = {"client_id": client["client_id"], "redirect_uri": redirect_uri,
              "state": qs.get("state", "")}
    if qs.get("response_type") != "code":
        raise OAuthError("unsupported_response_type", "Nur response_type=code")
    if qs.get("code_challenge_method") != "S256" or not qs.get("code_challenge"):
        raise OAuthError("invalid_request", "PKCE (S256) ist Pflicht")
    res = qs.get("resource")
    if res and res.rstrip("/") not in (resource_url(base), base.rstrip("/")):
        raise OAuthError("invalid_target", "Unbekannte Ressource")
    params["code_challenge"] = qs["code_challenge"]
    return params, client["name"]


def error_redirect(params, err):
    q = {"error": err.error, "error_description": err.description}
    if params.get("state"):
        q["state"] = params["state"]
    return _add_query(params["redirect_uri"], q)


def _add_query(uri, q):
    return uri + ("&" if "?" in uri else "?") + urllib.parse.urlencode(q)


def consent_page(params, client_name):
    nonce = secrets.token_urlsafe(24)
    now = time.time()
    with _pending_lock:
        for k in [k for k, (_, exp) in _pending.items() if exp < now]:
            del _pending[k]
        _pending[nonce] = (params, now + 600)
    host = urllib.parse.urlsplit(params["redirect_uri"]).netloc
    return PAGE % {
        "client": html.escape(client_name),
        "host": html.escape(host),
        "nonce": html.escape(nonce),
    }


def decide(path, form):
    """Formular abgeschickt → Ziel-URL für die Weiterleitung."""
    nonce = form.get("nonce", "")
    with _pending_lock:
        item = _pending.pop(nonce, None)
    if not item or item[1] < time.time():
        raise PageError("Die Anmeldung ist abgelaufen. Bitte in Claude erneut verbinden.")
    params = item[0]
    if form.get("decision") != "allow":
        return error_redirect(params, OAuthError("access_denied", "Abgelehnt"))
    code = secrets.token_urlsafe(32)
    db.oauth_exec(path, "INSERT INTO oauth_codes (code_hash, client_id, redirect_uri, challenge, expires_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                  (_h(code), params["client_id"], params["redirect_uri"], params["code_challenge"],
                   time.time() + CODE_TTL))
    q = {"code": code}
    if params.get("state"):
        q["state"] = params["state"]
    return _add_query(params["redirect_uri"], q)


# ─── Token ────────────────────────────────────────────────────────────

def _issue(path, client_id, grant_id):
    now = time.time()
    access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    db.oauth_exec(path, "INSERT INTO oauth_tokens (token_hash, kind, client_id, grant_id, expires_at, created_at) "
                        "VALUES (?, 'access', ?, ?, ?, ?)", (_h(access), client_id, grant_id, now + ACCESS_TTL, now))
    db.oauth_exec(path, "INSERT INTO oauth_tokens (token_hash, kind, client_id, grant_id, expires_at, created_at) "
                        "VALUES (?, 'refresh', ?, ?, ?, ?)", (_h(refresh), client_id, grant_id, now + REFRESH_TTL, now))
    return {"access_token": access, "token_type": "Bearer", "expires_in": ACCESS_TTL,
            "refresh_token": refresh, "scope": "cockpit"}


def token(path, form):
    grant = form.get("grant_type")
    if grant == "authorization_code":
        code = db.oauth_take_code(path, _h(form.get("code", "")))
        if not code or code["expires_at"] < time.time():
            raise OAuthError("invalid_grant", "Code ungültig oder abgelaufen")
        if form.get("client_id") and form["client_id"] != code["client_id"]:
            raise OAuthError("invalid_grant", "Code gehört zu einer anderen Anwendung")
        if form.get("redirect_uri", code["redirect_uri"]) != code["redirect_uri"]:
            raise OAuthError("invalid_grant", "redirect_uri passt nicht")
        verifier = form.get("code_verifier", "")
        calc = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        if not verifier or not secrets.compare_digest(calc, code["challenge"]):
            raise OAuthError("invalid_grant", "PKCE-Prüfung fehlgeschlagen")
        return _issue(path, code["client_id"], secrets.token_hex(8))
    if grant == "refresh_token":
        r = db.oauth_take_refresh(path, _h(form.get("refresh_token", "")))
        if not r or r["expires_at"] < time.time():
            raise OAuthError("invalid_grant", "Erneuerungs-Token ungültig oder abgelaufen")
        if form.get("client_id") and form["client_id"] != r["client_id"]:
            raise OAuthError("invalid_grant", "Token gehört zu einer anderen Anwendung")
        # alte Zugangs-Token dieser Verbindung ersetzen
        db.oauth_exec(path, "DELETE FROM oauth_tokens WHERE grant_id = ? AND kind = 'access'", (r["grant_id"],))
        return _issue(path, r["client_id"], r["grant_id"])
    raise OAuthError("unsupported_grant_type", "Nur authorization_code und refresh_token")


def check_bearer(path, header):
    """True, wenn ein gültiges Zugangs-Token mitgeschickt wurde."""
    if not header or not header.startswith("Bearer "):
        return False
    th = _h(header[7:].strip())
    rows = db.oauth_query(path, "SELECT expires_at, last_used FROM oauth_tokens "
                                "WHERE token_hash = ? AND kind = 'access'", (th,))
    if not rows or rows[0]["expires_at"] < time.time():
        return False
    if not rows[0]["last_used"] or rows[0]["last_used"] < time.time() - 300:
        db.oauth_exec(path, "UPDATE oauth_tokens SET last_used = ? WHERE token_hash = ?", (time.time(), th))
    return True


PAGE = """<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cockpit verbinden</title>
<style>
:root{--bg:#f4f5f8;--card:#fff;--fg:#1d2230;--mut:#5d6578;--line:#dde1ea;--acc:#3b6fd8}
@media (prefers-color-scheme:dark){:root{--bg:#12151c;--card:#1b2029;--fg:#e8ebf2;--mut:#9aa3b5;--line:#2c3340;--acc:#6ea8fe}}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 -apple-system,system-ui,sans-serif}
.box{max-width:460px;margin:8vh auto;padding:28px 24px;background:var(--card);border:1px solid var(--line);border-radius:14px}
h1{font-size:21px;margin:0 0 6px}p{color:var(--mut);margin:8px 0}ul{padding-left:20px;margin:14px 0}
li{margin:4px 0}.row{display:flex;gap:10px;margin-top:22px}
button{flex:1;font:inherit;padding:11px;border-radius:9px;border:1px solid var(--line);background:transparent;color:var(--fg);cursor:pointer}
button.ok{background:var(--acc);border-color:var(--acc);color:#fff;font-weight:600}
@media (max-width:520px){.box{margin:16px;}}
</style></head><body><div class="box">
<h1>🧭 Cockpit mit Claude verbinden?</h1>
<p><b>%(client)s</b> (%(host)s) möchte auf dein Cockpit zugreifen und darf dann:</p>
<ul><li>Karten und Materialien suchen und lesen – auch Gesundheitsdaten (B4, B5)</li>
<li>Dokumente aus der Bibliothek lesen</li>
<li>Karten anlegen, ändern und verschieben, Notizen und Materialien anhängen</li></ul>
<p>Löschen kann Claude nichts. Jede Änderung wird protokolliert.</p>
<form method="post" action="/oauth/authorize"><input type="hidden" name="nonce" value="%(nonce)s">
<div class="row"><button name="decision" value="deny">Ablehnen</button>
<button class="ok" name="decision" value="allow">Erlauben</button></div></form>
</div></body></html>"""

ERROR_PAGE = """<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Cockpit verbinden</title>
<style>body{font:16px/1.5 system-ui,sans-serif;max-width:460px;margin:10vh auto;padding:0 16px;background:#f4f5f8;color:#1d2230}
@media (prefers-color-scheme:dark){body{background:#12151c;color:#e8ebf2}}</style></head>
<body><h1>⚠️ Verbindung nicht möglich</h1><p>%s</p></body></html>"""


def _cli():
    import argparse
    ap = argparse.ArgumentParser(description="Claude-Verbindungen verwalten")
    ap.add_argument("--liste", action="store_true")
    ap.add_argument("--trennen", action="store_true", help="alle Token löschen")
    a = ap.parse_args()
    path = os.environ.get("COCKPIT_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "cockpit.db"))
    if a.trennen:
        n = db.oauth_exec(path, "DELETE FROM oauth_tokens")
        print("Getrennt: %d Token gelöscht. Claude muss sich neu anmelden." % n)
        return
    rows = db.oauth_query(path, "SELECT c.name, c.client_id, c.created_at, "
                                "max(t.last_used) AS last_used, count(t.token_hash) AS n "
                                "FROM oauth_clients c LEFT JOIN oauth_tokens t ON t.client_id = c.client_id "
                                "GROUP BY c.client_id ORDER BY c.created_at")
    for r in rows:
        fmt = lambda t: time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else "–"
        print("%-30s angelegt %s  zuletzt %s  aktive Token %d" % (r["name"], fmt(r["created_at"]),
                                                                 fmt(r["last_used"]), r["n"]))
    if not rows:
        print("Keine Verbindungen.")


if __name__ == "__main__":
    _cli()
