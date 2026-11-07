"""MCP-Schnittstelle für Claude (Etappe 4).

Claude (claude.ai, Desktop-App, Handy-App, Claude Code) spricht über das
Model Context Protocol mit dem Cockpit: POST /mcp mit JSON-RPC 2.0,
Antwort als application/json („Streamable HTTP“ ohne Sitzung/Stream).

Werkzeuge: Überblick, Suche (Wortlaut + Bedeutung), Karten auflisten/lesen,
Dokumente lesen, Bibliothek durchstöbern, Karten anlegen/ändern,
Materialien/Notizen/Links anhängen. Löschen gibt es bewusst nicht.
Nur Python-Bordmittel.
"""
import base64
import json
import os
import re
import time

import db

SERVER_INFO = {"name": "roberts-cockpit", "title": "Roberts Cockpit", "version": "1.0"}
PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
IMAGE_MIME = {"image/jpeg", "image/png", "image/gif", "image/webp"}
MAX_IMAGE = 4_500_000

INSTRUCTIONS = """Roberts Cockpit: persönliches Kanban-Board und Wissensarchiv von Robert Beckert
(Musiklehrer, Tenor/Gitarre/Ukulele). Karten = Aufgaben/Themen; Materialien = Dateien,
Notizen, Links (Bibliothek, durchsuchbar inkl. Texterkennung von Scans).
Spalten: eingang, heute, woche, naechstes, spaeter, geparkt, erledigt.
Kategorien: b1 Beruflich & Projekte, b2 Recht & Verwaltung, b3 Technik, b4 Medizin,
b5 Pflege Mutter, b6 Privates, b7 Sonstiges, b8 Themen ohne Aufgabe, vision.
Hinweise: Antworte auf Deutsch. Für Fragen zuerst `suchen` benutzen, dann gezielt
`dokument_lesen`/`karte_lesen`. Quellen immer mit Dokument- bzw. Karten-ID nennen.
Vor dem Anlegen einer Karte prüfen, ob es schon eine passende gibt. Änderungen an
Karten nur, wenn Robert darum bittet. B4/B5 enthalten Gesundheitsdaten – sorgsam behandeln."""


def _s(desc, **extra):
    d = {"type": "string", "description": desc}
    d.update(extra)
    return d


def _i(desc, default=None, minimum=0, maximum=None):
    d = {"type": "integer", "description": desc, "minimum": minimum}
    if default is not None:
        d["default"] = default
    if maximum is not None:
        d["maximum"] = maximum
    return d


COL_ENUM = list(db.COLUMNS)
CAT_ENUM = list(db.CATEGORIES)
PRIO_ENUM = list(db.PRIORITIES)

RO = {"readOnlyHint": True, "openWorldHint": False}
RW = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}

TOOLS = [
    {"name": "cockpit_uebersicht", "title": "Cockpit-Überblick",
     "description": "Überblick: Anzahl Karten je Spalte und Kategorie, vorhandene Unterkategorien, "
                    "Stand der Bibliothek. Gut als erster Schritt.",
     "inputSchema": {"type": "object", "properties": {}}, "annotations": RO},
    {"name": "suchen", "title": "Suchen",
     "description": "Durchsucht Karten und alle Materialien (auch Text aus PDFs, Word, Excel, Scans) – "
                    "nach Wortlaut UND nach Bedeutung. Liefert Karten- und Dokument-IDs mit Textausschnitt.",
     "inputSchema": {"type": "object", "required": ["anfrage"], "properties": {
         "anfrage": _s("Suchbegriffe oder eine Frage in natürlicher Sprache"),
         "anzahl": _i("Höchstzahl Treffer je Art", 10, 1, 30)}}, "annotations": RO},
    {"name": "karten_auflisten", "title": "Karten auflisten",
     "description": "Listet Karten, gefiltert nach Spalte, Kategorie, Unterkategorie, Priorität oder Text.",
     "inputSchema": {"type": "object", "properties": {
         "spalte": _s("Spalte", enum=COL_ENUM),
         "kategorie": _s("Kategorie", enum=CAT_ENUM),
         "unterkategorie": _s("Teil des Unterkategorie-Namens, z. B. 'B1.4'"),
         "prioritaet": _s("Priorität", enum=PRIO_ENUM),
         "text": _s("Muss im Titel/Notiz vorkommen (einfacher Textfilter)"),
         "anzahl": _i("Höchstzahl", 100, 1, 400)}}, "annotations": RO},
    {"name": "karte_lesen", "title": "Karte lesen",
     "description": "Liest eine Karte vollständig mit allen angehängten Materialien und Vorschlägen, "
                    "welche Dokumente/Karten inhaltlich dazu passen.",
     "inputSchema": {"type": "object", "required": ["karten_id"], "properties": {
         "karten_id": _s("ID der Karte, z. B. 'c12'")}}, "annotations": RO},
    {"name": "dokument_lesen", "title": "Dokument lesen",
     "description": "Liest den Text eines Materials (Datei, Notiz, Link). Lange Texte abschnittsweise "
                    "mit ab_zeichen. Fotos/Scans können zusätzlich als Bild angezeigt werden.",
     "inputSchema": {"type": "object", "required": ["dokument_id"], "properties": {
         "dokument_id": _i("ID des Dokuments", minimum=1),
         "ab_zeichen": _i("Ab welchem Zeichen lesen (für lange Dokumente)", 0),
         "max_zeichen": _i("Wie viele Zeichen höchstens", 20000, 500, 60000),
         "bild_anzeigen": {"type": "boolean", "default": False,
                           "description": "Bei Fotos (JPG/PNG): das Bild selbst mitschicken"}}},
     "annotations": RO},
    {"name": "bibliothek", "title": "Bibliothek durchstöbern",
     "description": "Listet Materialien der Bibliothek, neueste zuerst; filterbar nach Ordner "
                    "(Herkunftspfad), Quelle oder Stichwort.",
     "inputSchema": {"type": "object", "properties": {
         "ordner": _s("Teil des Herkunftsordners, z. B. 'Admin' oder '004 Bewerbungen'"),
         "quelle": _s("Quelle, z. B. 'onedrive-getintune', 'mac-schreibtisch', 'upload'"),
         "stichwort": _s("Stichwort (Volltext)"),
         "nur_ohne_karte": {"type": "boolean", "default": False,
                            "description": "Nur Dokumente, die noch an keiner Karte hängen"},
         "anzahl": _i("Höchstzahl", 30, 1, 200),
         "ab": _i("Überspringen (Blättern)", 0)}}, "annotations": RO},
    {"name": "karte_anlegen", "title": "Karte anlegen",
     "description": "Legt eine neue Karte an (Standard: Spalte 'eingang', Priorität 'Mittel').",
     "inputSchema": {"type": "object", "required": ["titel", "kategorie"], "properties": {
         "titel": _s("Kurzer Titel"),
         "kategorie": _s("Kategorie", enum=CAT_ENUM),
         "spalte": _s("Spalte", enum=COL_ENUM),
         "prioritaet": _s("Priorität", enum=PRIO_ENUM),
         "notiz": _s("Beschreibung / nächste Schritte"),
         "unterkategorie": _s("Unterkategorie, möglichst eine vorhandene (siehe cockpit_uebersicht)"),
         "zustaendig": _s("Zuständig (Standard: Robert)")}}, "annotations": RW},
    {"name": "karte_aendern", "title": "Karte ändern",
     "description": "Ändert Felder einer Karte, z. B. verschieben (spalte), Priorität, Titel. "
                    "'notiz' ersetzt die Notiz komplett, 'notiz_anhaengen' fügt unten Text an.",
     "inputSchema": {"type": "object", "required": ["karten_id"], "properties": {
         "karten_id": _s("ID der Karte"),
         "titel": _s("Neuer Titel"),
         "spalte": _s("Neue Spalte", enum=COL_ENUM),
         "kategorie": _s("Neue Kategorie", enum=CAT_ENUM),
         "prioritaet": _s("Neue Priorität", enum=PRIO_ENUM),
         "unterkategorie": _s("Neue Unterkategorie"),
         "zustaendig": _s("Zuständig"),
         "notiz": _s("Ersetzt die Notiz vollständig"),
         "notiz_anhaengen": _s("Text, der unten an die Notiz angefügt wird")}}, "annotations": RW},
    {"name": "material_verknuepfen", "title": "Material an Karte hängen",
     "description": "Hängt ein vorhandenes Dokument aus der Bibliothek an eine Karte.",
     "inputSchema": {"type": "object", "required": ["karten_id", "dokument_id"], "properties": {
         "karten_id": _s("ID der Karte"),
         "dokument_id": _i("ID des Dokuments", minimum=1)}}, "annotations": RW},
    {"name": "notiz_anhaengen", "title": "Notiz/Link an Karte hängen",
     "description": "Legt eine Notiz (z. B. Zusammenfassung, Entwurf) oder einen Link als Material "
                    "an einer Karte an. Die Notiz wird durchsuchbar.",
     "inputSchema": {"type": "object", "required": ["karten_id", "titel"], "properties": {
         "karten_id": _s("ID der Karte"),
         "titel": _s("Titel der Notiz bzw. des Links"),
         "text": _s("Inhalt der Notiz"),
         "url": _s("Statt Notiz: Link (https://…)")}}, "annotations": RW},
]


class ToolError(Exception):
    pass


# ─── Formatierung ─────────────────────────────────────────────────────

def _card_line(c, ndocs=None):
    parts = [db.CATEGORIES.get(c.get("cat"), c.get("cat") or "?")]
    if c.get("sub"):
        parts.append(c["sub"])
    line = "[%s] %s — %s · %s · %s" % (c["id"], c.get("t") or "", " / ".join(parts),
                                      db.COLUMNS.get(c.get("col"), c.get("col")), c.get("prio") or "")
    if c.get("owner") and c["owner"] != "Robert":
        line += " · 👤 " + c["owner"]
    if ndocs:
        line += " · 📎%d" % ndocs
    return line


def _doc_line(d):
    name = d.get("title") or d.get("filename") or d.get("url") or ""
    bits = ["[Dok %s] %s" % (d["id"], name)]
    if d.get("filename") and d["filename"] != name:
        bits.append("(%s)" % d["filename"])
    if d.get("origin_path"):
        bits.append("· Ordner: " + d["origin_path"])
    st = d.get("text_status")
    if d["kind"] == "file" and st and st != "done":
        bits.append("· Text: " + {"pending": "wird noch ausgelesen", "failed": "nicht lesbar",
                                  "skipped": "kein Text (Audio/Video/Noten)"}.get(st, st))
    return " ".join(bits)


def _clean_snip(s):
    return re.sub(r"\s+", " ", (s or "").replace("\x02", "»").replace("\x03", "«")).strip()


def _date(ts):
    return time.strftime("%d.%m.%Y", time.localtime(ts)) if ts else "?"


# ─── Werkzeuge ────────────────────────────────────────────────────────

def t_uebersicht(ctx, a):
    cards = db.list_cards(ctx.path)
    out = ["Stand: %s · %d Karten" % (time.strftime("%d.%m.%Y %H:%M"), len(cards)), "", "Spalten:"]
    for k, label in db.COLUMNS.items():
        n = sum(1 for c in cards if c.get("col") == k)
        out.append("  %s (%s): %d" % (label, k, n))
    out += ["", "Kategorien (offen = nicht erledigt):"]
    for k, label in db.CATEGORIES.items():
        cs = [c for c in cards if c.get("cat") == k]
        if not cs:
            continue
        out.append("  %s (%s): %d, davon offen %d" % (label, k, len(cs),
                                                     sum(1 for c in cs if c.get("col") != "erledigt")))
        subs = {}
        for c in cs:
            if c.get("sub"):
                subs[c["sub"]] = subs.get(c["sub"], 0) + 1
        for s_, n in sorted(subs.items()):
            out.append("      · %s (%d)" % (s_, n))
    st = db.status(ctx.path)
    out += ["", "Bibliothek: %s" % json.dumps(st, ensure_ascii=False)]
    return "\n".join(out)


def t_suchen(ctx, a):
    q = (a.get("anfrage") or "").strip()
    if not q:
        raise ToolError("anfrage fehlt")
    k = max(1, min(int(a.get("anzahl") or 10), 30))
    cards = {c["id"]: c for c in db.list_cards(ctx.path)}
    counts = db.doc_counts(ctx.path)
    fts = db.search(ctx.path, q, limit=60)
    sem = ctx.vectors.search_text(q)
    out = ["Suche: %s" % q]

    seen_cards, card_lines = set(), []
    for cid in fts["cards"]:
        if cid in cards and cid not in seen_cards:
            seen_cards.add(cid)
            card_lines.append(_card_line(cards[cid], counts.get(cid)) + "  (Wortlaut)")
    for h in sem.get("cards", []):
        if h["score"] >= 0.45 and h["id"] in cards and h["id"] not in seen_cards:
            seen_cards.add(h["id"])
            card_lines.append(_card_line(cards[h["id"]], counts.get(h["id"])) + "  (Bedeutung %.2f)" % h["score"])
    out += ["", "KARTEN:"] + (card_lines[:k] or ["  keine"])

    seen_docs, doc_lines = set(), []
    for d in fts["docs"]:
        if d["id"] in seen_docs:
            continue
        seen_docs.add(d["id"])
        doc_lines.append(_doc_line(d) + "  (Wortlaut)\n    „%s“" % _clean_snip(d.get("snippet")))
    for d in sem.get("docs", []):
        if d["score"] < 0.40 or d["id"] in seen_docs:
            continue
        seen_docs.add(d["id"])
        doc_lines.append(_doc_line(d) + "  (Bedeutung %.2f)\n    „%s“" % (d["score"], _clean_snip(d.get("snippet"))[:250]))
    out += ["", "DOKUMENTE:"] + (doc_lines[:k] or ["  keine"])
    if not sem.get("available", True):
        out.append("\n(Bedeutungssuche gerade nicht verfügbar – nur Wortlaut-Treffer.)")
    out.append("\nDetails mit karte_lesen bzw. dokument_lesen.")
    return "\n".join(out)


def t_karten_auflisten(ctx, a):
    cards = db.list_cards(ctx.path)
    counts = db.doc_counts(ctx.path)
    if a.get("spalte"):
        cards = [c for c in cards if c.get("col") == a["spalte"]]
    if a.get("kategorie"):
        cards = [c for c in cards if c.get("cat") == a["kategorie"]]
    if a.get("prioritaet"):
        cards = [c for c in cards if c.get("prio") == a["prioritaet"]]
    if a.get("unterkategorie"):
        u = a["unterkategorie"].lower()
        cards = [c for c in cards if u in (c.get("sub") or "").lower()]
    if a.get("text"):
        t = a["text"].lower()
        cards = [c for c in cards if t in ((c.get("t") or "") + " " + (c.get("note") or "")).lower()]
    n = max(1, min(int(a.get("anzahl") or 100), 400))
    lines = [_card_line(c, counts.get(c["id"])) for c in cards[:n]]
    head = "%d Karten gefunden%s:" % (len(cards), " (zeige %d)" % n if len(cards) > n else "")
    return "\n".join([head] + lines)


def t_karte_lesen(ctx, a):
    c = db.get_card(ctx.path, str(a.get("karten_id") or ""))
    docs = db.card_docs(ctx.path, c["id"])
    out = [_card_line(c, len(docs)), ""]
    out.append("Titel: %s" % c.get("t", ""))
    out.append("Spalte: %s · Kategorie: %s · Unterkategorie: %s · Priorität: %s · Zuständig: %s"
               % (c.get("col"), c.get("cat"), c.get("sub") or "–", c.get("prio"), c.get("owner") or "–"))
    if c.get("src"):
        out.append("Quelle: %s" % c["src"])
    out.append("Angelegt: %s · geändert: %s" % (_date(c.get("created_at")), _date(c.get("updated_at"))))
    out += ["", "Notiz:", c.get("note") or "(leer)", "", "MATERIALIEN (%d):" % len(docs)]
    for d in docs:
        line = "  " + _doc_line(d)
        if d["kind"] == "link":
            line += " → " + (d.get("url") or "")
        if d["kind"] == "note" and d.get("body"):
            line += "\n    " + d["body"][:500].replace("\n", "\n    ")
        out.append(line)
    rel = ctx.vectors.related_to_card(c["id"])
    rd = [d for d in rel.get("docs", []) if d["score"] >= 0.40]
    rc = [x for x in rel.get("cards", []) if x["score"] >= 0.50]
    if rd or rc:
        cards = {x["id"]: x for x in db.list_cards(ctx.path)}
        out += ["", "PASSEND DAZU (inhaltlich ähnlich, nicht verknüpft):"]
        out += ["  " + _doc_line(d) + " (%.2f)" % d["score"] for d in rd]
        out += ["  " + _card_line(cards[x["id"]]) + " (%.2f)" % x["score"] for x in rc if x["id"] in cards]
    return "\n".join(out)


def t_dokument_lesen(ctx, a):
    d = db.get_doc(ctx.path, int(a.get("dokument_id") or 0))
    start = max(0, int(a.get("ab_zeichen") or 0))
    n = max(500, min(int(a.get("max_zeichen") or 20000), 60000))
    head = [_doc_line(d)]
    meta = []
    if d.get("mime"):
        meta.append(d["mime"])
    if d.get("size"):
        meta.append("%.1f MB" % (d["size"] / 1e6) if d["size"] >= 1e5 else "%d KB" % max(1, d["size"] // 1000))
    meta.append("hinzugefügt %s" % _date(d.get("created_at")))
    head.append(" · ".join(meta))
    if d["cards"]:
        head.append("Hängt an: " + "; ".join("[%s] %s" % (c["id"], c["t"]) for c in d["cards"]))
    if d["kind"] == "link":
        head.append("Link: " + (d.get("url") or ""))
    text = d.get("text") if d["kind"] == "file" else (d.get("body") or "")
    content = []
    if text:
        part = text[start:start + n]
        head.append("Text (Zeichen %d–%d von %d):" % (start, start + len(part), len(text)))
        body = "\n".join(head) + "\n\n" + part
        if start + n < len(text):
            body += "\n\n[… weiter mit ab_zeichen=%d]" % (start + n)
        content.append({"type": "text", "text": body})
    else:
        why = {"pending": "Der Text wird gerade noch ausgelesen – bitte später erneut versuchen.",
               "failed": "Der Text ließ sich nicht auslesen: %s" % (d.get("text_error") or ""),
               "skipped": "Für diesen Dateityp gibt es (noch) keinen Text."}.get(d.get("text_status"), "Kein Text vorhanden.")
        content.append({"type": "text", "text": "\n".join(head) + "\n\n" + why})
    if a.get("bild_anzeigen") and d["kind"] == "file" and d.get("mime") in IMAGE_MIME:
        if (d.get("size") or 0) > MAX_IMAGE:
            content.append({"type": "text", "text": "(Bild zu groß zum Anzeigen.)"})
        else:
            with open(ctx.blob_path(d["sha256"]), "rb") as f:
                content.append({"type": "image", "mimeType": d["mime"],
                                "data": base64.b64encode(f.read()).decode()})
    return content


def t_bibliothek(ctx, a):
    n = max(1, min(int(a.get("anzahl") or 30), 200))
    r = db.library(ctx.path, q=a.get("stichwort") or "", origin=a.get("quelle") or "",
                   folder=a.get("ordner") or "", unlinked=bool(a.get("nur_ohne_karte")),
                   limit=n, offset=max(0, int(a.get("ab") or 0)))
    out = ["%d Dokumente gefunden (zeige %d ab %d):" % (r["total"], len(r["items"]), int(a.get("ab") or 0))]
    for d in r["items"]:
        line = "  " + _doc_line(d) + " · " + _date(d.get("created_at"))
        if d.get("cards"):
            line += " · an Karte " + ", ".join(d["cards"])
        out.append(line)
    return "\n".join(out)


def _fields(a, mapping):
    return {dst: a[src] for src, dst in mapping.items() if a.get(src) is not None}


FIELD_MAP = {"titel": "t", "kategorie": "cat", "spalte": "col", "prioritaet": "prio",
             "notiz": "note", "unterkategorie": "sub", "zustaendig": "owner"}


def t_karte_anlegen(ctx, a):
    f = _fields(a, FIELD_MAP)
    f["src"] = "Claude"
    c = db.create_card(ctx.path, f)
    return "✅ Karte angelegt:\n" + _card_line(c)


def t_karte_aendern(ctx, a):
    f = _fields(a, FIELD_MAP)
    c = db.update_card(ctx.path, str(a.get("karten_id") or ""), f, note_append=a.get("notiz_anhaengen"))
    return "✅ Karte geändert:\n" + _card_line(c)


def t_material_verknuepfen(ctx, a):
    cid, did = str(a.get("karten_id") or ""), int(a.get("dokument_id") or 0)
    db.attach_doc(ctx.path, cid, did)
    db.log(ctx.path, "claude", "material_verknuepfen", {"karte": cid, "dok": did})
    return "✅ Dokument %d hängt jetzt an Karte %s." % (did, cid)


def t_notiz_anhaengen(ctx, a):
    cid = str(a.get("karten_id") or "")
    title = (a.get("titel") or "").strip()
    url = (a.get("url") or "").strip()
    if url:
        if not re.match(r"^https?://", url, re.I):
            raise ToolError("Link muss mit http:// oder https:// beginnen")
        d = db.add_doc(ctx.path, cid, "link", title=title or url, url=url)
    else:
        body = (a.get("text") or "").strip()
        if not body:
            raise ToolError("text fehlt")
        d = db.add_doc(ctx.path, cid, "note", title=title, body=body)
    db.log(ctx.path, "claude", "notiz_anhaengen", {"karte": cid, "dok": d["id"], "titel": title})
    return "✅ %s an Karte %s angehängt (Dok %d)." % ("Link" if url else "Notiz", cid, d["id"])


HANDLERS = {
    "cockpit_uebersicht": t_uebersicht, "suchen": t_suchen, "karten_auflisten": t_karten_auflisten,
    "karte_lesen": t_karte_lesen, "dokument_lesen": t_dokument_lesen, "bibliothek": t_bibliothek,
    "karte_anlegen": t_karte_anlegen, "karte_aendern": t_karte_aendern,
    "material_verknuepfen": t_material_verknuepfen, "notiz_anhaengen": t_notiz_anhaengen,
}


# ─── JSON-RPC ─────────────────────────────────────────────────────────

def _err(mid, code, msg):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": msg}}


def handle(ctx, msg):
    """Eine JSON-RPC-Nachricht verarbeiten. Gibt Antwort zurück oder None (Benachrichtigung)."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _err(None, -32600, "Ungültige Anfrage")
    mid, method = msg.get("id"), msg.get("method")
    if method is None:          # Antwort des Clients auf eine Server-Anfrage – gibt es hier nicht
        return None
    if "id" not in msg:         # Benachrichtigung (z. B. notifications/initialized)
        return None
    params = msg.get("params") or {}
    if method == "initialize":
        want = params.get("protocolVersion")
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": want if want in PROTOCOLS else PROTOCOLS[1],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": INSTRUCTIONS}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        name = params.get("name")
        fn = HANDLERS.get(name)
        if not fn:
            return _err(mid, -32602, "Unbekanntes Werkzeug: %s" % name)
        try:
            res = fn(ctx, params.get("arguments") or {})
            content = res if isinstance(res, list) else [{"type": "text", "text": res}]
            return {"jsonrpc": "2.0", "id": mid, "result": {"content": content}}
        except (ToolError, ValueError, db.NotFound) as e:
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": "Fehler: %s" % e}], "isError": True}}
        except Exception as e:  # noqa: BLE001
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": "Interner Fehler: %s: %s" % (type(e).__name__, e)}],
                "isError": True}}
    if method in ("resources/list", "prompts/list"):
        key = method.split("/")[0]
        return {"jsonrpc": "2.0", "id": mid, "result": {key: []}}
    return _err(mid, -32601, "Unbekannte Methode: %s" % method)


def handle_body(ctx, payload):
    """Ganzen POST-Body verarbeiten (einzelne Nachricht oder Liste)."""
    if isinstance(payload, list):
        out = [r for r in (handle(ctx, m) for m in payload) if r is not None]
        return out or None
    return handle(ctx, payload)
