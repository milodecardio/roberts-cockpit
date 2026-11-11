#!/usr/bin/env python3
"""Microsoft Graph für Roberts Cockpit (Etappe 5) – nur Lesezugriff auf Mails.

Eigene App-Registrierung „Roberts Cockpit“ im Microsoft-Konto (Entra ID):
  - Berechtigungen (delegiert): Mail.Read, offline_access, User.Read – sonst nichts.
  - „Öffentliche Clientflows zulassen“ = Ja (Anmeldung per Gerätecode, kein Geheimnis nötig).
In .env:  MS_CLIENT_ID=<Anwendungs-ID>   MS_TENANT=common (oder Mandanten-ID / organizations)

Anmeldung per Gerätecode: Das Cockpit zeigt einen Code, Robert gibt ihn auf
https://microsoft.com/devicelogin ein und meldet sich an. Das Cockpit erhält ein
Refresh-Token (liegt in der Datenbank, Tabelle meta) und kann danach Mails lesen.
Schreiben, Senden, Löschen ist mit diesen Berechtigungen technisch unmöglich.

Kommandozeile (auf dem Server als Nutzer cockpit):
  python3 msgraph.py --status
  python3 msgraph.py --anmelden
  python3 msgraph.py --abmelden
Nur Python-Bordmittel.
"""
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import db

CLIENT_ID = os.environ.get("MS_CLIENT_ID", "").strip()
TENANT = os.environ.get("MS_TENANT", "common").strip() or "common"
SCOPES = "https://graph.microsoft.com/Mail.Read https://graph.microsoft.com/User.Read offline_access"
LOGIN = "https://login.microsoftonline.com/%s/oauth2/v2.0" % urllib.parse.quote(TENANT)
GRAPH = "https://graph.microsoft.com/v1.0"
META_KEY = "ms_auth"
UA = "RobertsCockpit/1.0"


class GraphError(Exception):
    pass


class NotConnected(GraphError):
    pass


def configured():
    return bool(CLIENT_ID)


def _post_form(url, form, timeout=30):
    data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/x-www-form-urlencoded", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read() or b"{}")
        except ValueError:
            return {"error": "http_%d" % e.code}


# ─── Token-Speicher ───────────────────────────────────────────────────

_lock = threading.Lock()
_access = {}     # db_path -> (token, gültig_bis)
_login = {}      # db_path -> Zustand der laufenden Gerätecode-Anmeldung


def _load(path):
    raw = db.meta_get(path, META_KEY)
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def _save(path, data):
    db.meta_set(path, META_KEY, json.dumps(data, ensure_ascii=False) if data else None)


def _store_tokens(path, tok, account=None):
    data = _load(path) or {}
    if tok.get("refresh_token"):
        data["refresh_token"] = tok["refresh_token"]
    data["scope"] = tok.get("scope", data.get("scope", ""))
    data["saved_at"] = time.time()
    if account:
        data["account"] = account
    _save(path, data)
    if tok.get("access_token"):
        _access[path] = (tok["access_token"], time.time() + int(tok.get("expires_in", 3600)) - 120)


def access_token(path):
    if not configured():
        raise NotConnected("Microsoft-Anbindung ist nicht eingerichtet (MS_CLIENT_ID fehlt).")
    with _lock:
        tok, until = _access.get(path, (None, 0))
        if tok and until > time.time():
            return tok
        data = _load(path)
        if not data or not data.get("refresh_token"):
            raise NotConnected("Nicht bei Microsoft angemeldet – im Cockpit unter ⚙︎ „Outlook verbinden“.")
        res = _post_form(LOGIN + "/token", {
            "client_id": CLIENT_ID, "grant_type": "refresh_token",
            "refresh_token": data["refresh_token"], "scope": SCOPES})
        if "access_token" not in res:
            if res.get("error") in ("invalid_grant", "interaction_required"):
                _save(path, None)
                raise NotConnected("Microsoft-Anmeldung abgelaufen – bitte neu verbinden.")
            raise GraphError("Token-Erneuerung fehlgeschlagen: %s" % (res.get("error_description") or res.get("error")))
        _store_tokens(path, res)
        return res["access_token"]


def status(path):
    data = _load(path) or {}
    st = {"configured": configured(), "connected": bool(data.get("refresh_token")),
          "account": data.get("account"), "since": data.get("saved_at")}
    lg = _login.get(path)
    if lg and lg.get("expires_at", 0) > time.time() - 300:
        st["login"] = {k: lg.get(k) for k in ("state", "user_code", "verification_uri", "error", "expires_at")}
    return st


def logout(path):
    with _lock:
        _access.pop(path, None)
        _save(path, None)
    db.log(path, "robert", "outlook_abmelden", {})


# ─── Anmeldung per Gerätecode ─────────────────────────────────────────

def start_login(path):
    """Startet die Gerätecode-Anmeldung und wartet im Hintergrund auf Robert."""
    if not configured():
        raise GraphError("MS_CLIENT_ID fehlt in der .env – App-Registrierung siehe docs/OUTLOOK.md")
    lg = _login.get(path)
    if lg and lg.get("state") == "pending" and lg.get("expires_at", 0) > time.time() + 30:
        return status(path)["login"]
    res = _post_form(LOGIN + "/devicecode", {"client_id": CLIENT_ID, "scope": SCOPES})
    if "device_code" not in res:
        raise GraphError("Microsoft lehnt die Anmeldung ab: %s" % (res.get("error_description") or res.get("error")))
    lg = {"state": "pending", "user_code": res["user_code"],
          "verification_uri": res.get("verification_uri", "https://microsoft.com/devicelogin"),
          "expires_at": time.time() + int(res.get("expires_in", 900)),
          "interval": int(res.get("interval", 5)), "device_code": res["device_code"]}
    _login[path] = lg
    threading.Thread(target=_poll, args=(path, lg), daemon=True).start()
    return {k: lg[k] for k in ("state", "user_code", "verification_uri", "expires_at")}


def _poll(path, lg):
    interval = lg["interval"]
    while time.time() < lg["expires_at"] and _login.get(path) is lg:
        time.sleep(interval)
        res = _post_form(LOGIN + "/token", {
            "client_id": CLIENT_ID, "device_code": lg["device_code"],
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
        err = res.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5
            continue
        if "access_token" in res:
            try:
                me = _get_json(res["access_token"], "/me?$select=displayName,userPrincipalName,mail")
                account = me.get("mail") or me.get("userPrincipalName") or me.get("displayName")
            except GraphError:
                account = None
            with _lock:
                _store_tokens(path, res, account)
            lg.update(state="done", account=account)
            lg.pop("device_code", None)
            db.log(path, "robert", "outlook_verbinden", {"konto": account})
            return
        lg.update(state="failed", error={"expired_token": "Code abgelaufen",
                                         "authorization_declined": "Anmeldung abgelehnt"}.get(
                                             err, res.get("error_description") or err))
        lg.pop("device_code", None)
        return
    if lg.get("state") == "pending":
        lg.update(state="failed", error="Code abgelaufen")
        lg.pop("device_code", None)


# ─── Graph-Aufrufe (nur GET) ──────────────────────────────────────────

def _request(token, rel, accept="application/json", timeout=60, max_bytes=None):
    url = rel if rel.startswith("https://") else GRAPH + rel
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token, "Accept": accept, "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if max_bytes:
                data = r.read(max_bytes + 1)
                if len(data) > max_bytes:
                    raise GraphError("Mail ist zu groß (über %d MB)" % (max_bytes // 1024 // 1024))
                return data
            return r.read()
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read() or b"{}").get("error", {}).get("message", "")
        except ValueError:
            msg = ""
        if e.code == 404:
            raise GraphError("Mail nicht gefunden (evtl. verschoben oder gelöscht)")
        raise GraphError("Microsoft Graph %d: %s" % (e.code, msg or e.reason))
    except urllib.error.URLError as e:
        raise GraphError("Keine Verbindung zu Microsoft: %s" % e.reason)


def _get_json(token, rel):
    return json.loads(_request(token, rel) or b"{}")


def get_json(path, rel):
    return _get_json(access_token(path), rel)


def get_bytes(path, rel, max_bytes=None, timeout=120):
    return _request(access_token(path), rel, accept="*/*", timeout=timeout, max_bytes=max_bytes)


# ─── Kommandozeile ────────────────────────────────────────────────────

def _cli():
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.environ.get("COCKPIT_DB", os.path.join(here, "cockpit.db"))
    db.init(path)
    arg = sys.argv[1] if len(sys.argv) > 1 else "--status"
    if arg == "--anmelden":
        lg = start_login(path)
        print("Bitte im Browser %s öffnen und diesen Code eingeben:  %s" % (lg["verification_uri"], lg["user_code"]))
        print("(Anmelden mit dem Microsoft-Konto, dessen Mails ins Cockpit sollen.) Warte …", flush=True)
        while True:
            time.sleep(2)
            st = _login[path]
            if st["state"] == "done":
                print("✅ Verbunden mit", st.get("account") or "Microsoft-Konto")
                return 0
            if st["state"] == "failed":
                print("❌", st.get("error"))
                return 1
    if arg == "--abmelden":
        logout(path)
        print("Abgemeldet – das Cockpit kann keine Mails mehr lesen.")
        return 0
    st = status(path)
    print(json.dumps(st, ensure_ascii=False, indent=2))
    if st["connected"]:
        me = get_json(path, "/me?$select=displayName,userPrincipalName")
        print("Test: angemeldet als", me.get("displayName"), "<%s>" % me.get("userPrincipalName"))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
