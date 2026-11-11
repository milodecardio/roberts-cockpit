"""Eingeschränkte Schnittstelle für den Bewerbungs-Agenten (/agent-api/…).

Sicherheitsregeln (serverseitig durchgesetzt, nicht nur im Agenten):
- Zugriff nur mit Bearer-Token aus AGENT_API_TOKEN und nur direkt vom Server selbst
  (Anfragen, die über Caddy von außen kommen, tragen X-Forwarded-For und werden abgelehnt).
- Lesen: alle Karten, Materialien und Dateien.
- Schreiben: nur Karten mit Quelle AGENT_SRC, die in Kategorie b1 / Unterkategorie B1.3 liegen.
  Neue Karten werden immer mit Quelle AGENT_SRC, Kategorie b1 und Unterkategorie
  "B1.3 Bewerbungen" angelegt. Kategorie, Unterkategorie und Quelle lassen sich nicht ändern.
- Kein Löschen.
"""
import hmac
import os
import re
import urllib.parse

import db

AGENT_SRC = "Bewerbungs-Agent"
AGENT_SUB = "B1.3 Bewerbungen"
TOKEN = os.environ.get("AGENT_API_TOKEN", "")
EDITABLE = ("t", "col", "prio", "note", "owner")


class Forbidden(Exception):
    pass


def authorized(headers):
    if not TOKEN or len(TOKEN) < 32:
        return False
    if headers.get("X-Forwarded-For") or headers.get("X-Forwarded-Host"):
        return False
    h = headers.get("Authorization", "")
    return h.startswith("Bearer ") and hmac.compare_digest(h[7:].strip(), TOKEN)


def _own_card(path, card_id):
    c = db.get_card(path, card_id)
    if c.get("src") != AGENT_SRC or c.get("cat") != "b1" or not (c.get("sub") or "").startswith("B1.3"):
        raise Forbidden("Karte %s gehört nicht dem Bewerbungs-Agenten" % card_id)
    return c


def handle(path, method, url, read_json, store_upload, send_json, send_file):
    """Gibt True zurück, wenn der Pfad zu /agent-api gehört (dann ist geantwortet)."""
    parts = urllib.parse.urlsplit(url)
    p = parts.path
    if not p.startswith("/agent-api/"):
        return False
    qs = {k: v[0] for k, v in urllib.parse.parse_qs(parts.query).items()}
    try:
        if method == "GET" and p == "/agent-api/cards":
            cards = db.list_cards(path)
            if qs.get("cat"):
                cards = [c for c in cards if c.get("cat") == qs["cat"]]
            if qs.get("sub"):
                cards = [c for c in cards if (c.get("sub") or "").lower().startswith(qs["sub"].lower())]
            if qs.get("src"):
                cards = [c for c in cards if c.get("src") == qs["src"]]
            send_json(200, cards)
        elif method == "GET" and p == "/agent-api/library":
            send_json(200, db.library(path, q=qs.get("q", ""), folder=qs.get("folder", ""),
                                      limit=min(int(qs.get("limit", "200")), 500), offset=int(qs.get("offset", "0"))))
        elif method == "POST" and p == "/agent-api/cards":
            data = read_json()
            f = {k: data[k] for k in ("t", "col", "prio", "note") if data.get(k) is not None}
            f.update(cat="b1", sub=AGENT_SUB, src=AGENT_SRC, owner=data.get("owner") or "Robert")
            c = db.create_card(path, f, actor="bewerbungs-agent")
            send_json(200, c)
        else:
            m = re.fullmatch(r"/agent-api/cards/([^/]+)(?:/(docs|notes|files))?", p)
            d = re.fullmatch(r"/agent-api/docs/(\d+)(/file)?", p)
            if m:
                cid, what = urllib.parse.unquote(m.group(1)), m.group(2)
                if method == "GET" and not what:
                    c = db.get_card(path, cid)
                    c["docs"] = db.card_docs(path, cid)
                    send_json(200, c)
                elif method == "PATCH" and not what:
                    _own_card(path, cid)
                    data = read_json()
                    changes = {k: data[k] for k in EDITABLE if data.get(k) is not None}
                    send_json(200, db.update_card(path, cid, changes, note_append=data.get("note_append"),
                                                  actor="bewerbungs-agent"))
                elif method == "POST" and what == "notes":
                    _own_card(path, cid)
                    data = read_json()
                    body = (data.get("body") or "").strip()
                    if not body:
                        raise ValueError("Notiz ist leer")
                    send_json(200, db.add_doc(path, cid, "note", title=(data.get("title") or "").strip()
                                              or body.split("\n", 1)[0][:80], body=body))
                elif method == "POST" and what == "files":
                    _own_card(path, cid)
                    sha, filename, mime, length = store_upload()
                    send_json(200, db.add_doc(path, cid, "file", title=os.path.splitext(filename)[0],
                                              filename=filename, mime=mime, size=length, sha256=sha))
                else:
                    send_json(405, {"error": "Methode nicht erlaubt"})
            elif d and method == "GET":
                if d.group(2):
                    send_file(int(d.group(1)))
                else:
                    send_json(200, db.get_doc(path, int(d.group(1))))
            else:
                send_json(404, {"error": "unbekannter Pfad"})
    except Forbidden as e:
        send_json(403, {"error": str(e)})
    except db.NotFound as e:
        send_json(404, {"error": str(e)})
    except ValueError as e:
        send_json(400, {"error": str(e)})
    except Exception as e:  # noqa: BLE001
        send_json(500, {"error": "%s: %s" % (type(e).__name__, e)})
    return True
