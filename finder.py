"""Suchfeld mit Befehlsverständnis (Etappe 5).

Zerlegt eine Eingabe in Was (Objekttyp), Wer (Absender), Wo (Bereich, Spalte,
Priorität) und den eigentlichen Suchbegriff – ohne KI, sofort, auf dem eigenen Server:

  "Karte Friedberg"                                → Karten, Wortlaut „Friedberg“
  "karten: b1 heute"                               → Karten in B1, Spalte Heute
  "Gib mir alle Karten, die mit der Bewerbung bei
   der Stadt Friedberg zu tun haben"               → Karten, nach Bedeutung + Wortlaut
  "Mails und Anhänge von Frau Häfner"              → Mails + Anhänge, Absender „Häfner“
  "Mails von Häfner in Outlook"                    → zusätzlich live in Outlook suchen
  "Friedberg"                                      → alles (Karten filtern, Materialien als Zeile)
Nur Python-Bordmittel.
"""
import re
import unicodedata

import db

TYPE_WORDS = {
    "card": {"karte", "karten", "aufgabe", "aufgaben"},
    "mail": {"mail", "mails", "e-mail", "e-mails", "email", "emails", "nachricht", "nachrichten"},
    "attachment": {"anhang", "anhänge", "anhaenge", "anlage", "anlagen"},
    "file": {"datei", "dateien", "dokument", "dokumente", "pdf", "pdfs", "scan", "scans"},
    "note": {"notiz", "notizen"},
    "link": {"link", "links"},
}
TYPE_OF = {w: t for t, ws in TYPE_WORDS.items() for w in ws}
TYPE_LABEL = {"card": "Karten", "mail": "Mails", "attachment": "Anhänge", "file": "Dateien",
              "note": "Notizen", "link": "Links"}
COL_WORDS = {"eingang": "eingang", "heute": "heute", "dringend": "heute", "dringende": "heute",
             "woche": "woche", "nächstes": "naechstes", "naechstes": "naechstes", "nächste": "naechstes",
             "später": "spaeter", "spaeter": "spaeter", "geparkt": "geparkt", "geparkte": "geparkt",
             "erledigt": "erledigt", "erledigte": "erledigt", "offen": "offen", "offene": "offen"}
PRIO_WORDS = {"hoch": "Hoch", "mittel": "Mittel", "niedrig": "Niedrig"}
CAT_NAMES = {"vision": "vision", "medizin": "b4", "pflege": "b5", "technik": "b3", "privat": "b6",
             "privates": "b6", "beruflich": "b1", "recht": "b2", "verwaltung": "b2", "sonstiges": "b7"}
HONORIFIC = {"frau", "herr", "herrn", "dr", "dr.", "prof", "prof.", "familie"}
STOP = set("""gib gibt mir zeig zeige zeigt alle allen alles die der das den dem des mit zu zum zur tun haben hat
welche welcher welches welchen und oder bitte such suche suchen finde finden find mich meine mein meinen
meiner über ueber bei für fuer ein eine einen einem einer im in auf aus was wo es etwas irgendwas alle
sind ist war waren wird werden gehören gehoeren gehört gehoert betreffen betrifft betreffend bezüglich
bezueglich thema themen zusammenhang rund um sowie auch noch nur ganz dazu davon darüber darueber
spalte bereich kategorie prio priorität prioritaet liste listen von from an ich du sie""".split())
NATURAL = re.compile(r"\b(zu tun|thema|bezüglich|bezueglich|betreff|zusammenhang|rund um|ähnlich|aehnlich|"
                     r"passend|gehör|gehoer|was weiß|was weiss|alles über|alles ueber)", re.I)


def norm(s):
    """Für Vergleiche: klein, Umlaute → ae/oe/ue, ohne Akzente."""
    s = (s or "").casefold().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def parse(q):
    raw = (q or "").strip()
    toks = re.findall(r"[\wÄÖÜäöüß@.\-]+:?|:", raw)
    # "karten:" / "von:" / "spalte:heute" → Schlüssel und Wert trennen
    words, one_word = [], set()
    for t in toks:
        if ":" in t and t != ":":
            k, _, v = t.partition(":")
            words.append(k)
            one_word.add(len(words))         # „von:mueller“ → nur das eine folgende Wort
            if v:
                words.append(v)
        elif t != ":":
            words.append(t)
    low = [w.lower().strip(".-") for w in words]
    types = []
    for w in low:
        t = TYPE_OF.get(w)
        if t and t not in types:
            types.append(t)
    res = {"types": types, "from": None, "col": None, "cat": None, "sub": None, "prio": None,
           "outlook": False, "text": "", "semantic": False}
    text, i = [], 0
    while i < len(low):
        w, orig = low[i], words[i].strip(".,;!?")
        prev = low[i - 1] if i else ""
        if w in TYPE_OF:
            pass
        elif w in ("von", "from") and i + 1 < len(low):
            j, who = i + 1, []
            while j < len(low) and low[j] in HONORIFIC:
                j += 1
            # nach Frau/Herr/Dr. oder „von:name“ folgt meist nur der Nachname
            maxw = 1 if (j in one_word or j > i + 1) else 2
            while j < len(low) and len(who) < maxw and low[j] not in STOP and low[j] not in TYPE_OF \
                    and low[j] != "outlook":
                who.append(words[j].strip(".,;!?"))
                j += 1
            if who:
                res["from"] = " ".join(who)
                i = j
                continue
        elif w == "outlook":
            res["outlook"] = True
        elif re.fullmatch(r"b[1-8](\.\d+)?", w):
            res["cat"] = w[:2]
            if "." in w:
                res["sub"] = w.upper()
        elif w in COL_WORDS and ("card" in types or prev in ("spalte", "in", "im")):
            res["col"] = COL_WORDS[w]
        elif w in PRIO_WORDS and prev in ("prio", "priorität", "prioritaet"):
            res["prio"] = PRIO_WORDS[w]
        elif w in CAT_NAMES and (prev in ("bereich", "kategorie") or w == "vision"):
            res["cat"] = CAT_NAMES[w]
        elif w in STOP or w in ("diese", "dieser", "als", "frau", "herr", "herrn"):
            pass
        elif orig:
            text.append(orig)
        i += 1
    res["text"] = " ".join(text)
    natural = len(words) >= 6 or bool(NATURAL.search(raw))
    res["semantic"] = bool(res["text"]) and natural
    if not types and res["from"]:
        res["types"] = types = ["mail", "attachment"]
        res["mode"] = "docs"
    elif not types:
        res["mode"] = "all"
    elif types == ["card"]:
        res["mode"] = "cards"
    elif "card" in types:
        res["mode"] = "both"
    else:
        res["mode"] = "docs"
    res["label"] = _label(res)
    if res["outlook"] or ("mail" in types and res["from"]):
        kql = []
        if res["from"]:
            kql.append("from:" + res["from"].split()[-1])
        if res["text"]:
            kql.append(res["text"])
        res["outlook_query"] = " ".join(kql)
    return res


def _label(r):
    bits = [" + ".join(TYPE_LABEL[t] for t in r["types"]) or "Alles"]
    if r["from"]:
        bits.append("von „%s“" % r["from"])
    if r["cat"]:
        bits.append(db.CATEGORIES.get(r["cat"], r["cat"]).split(" ")[0] + (" · " + r["sub"] if r["sub"] else ""))
    if r["col"]:
        bits.append("offen" if r["col"] == "offen" else db.COLUMNS.get(r["col"], r["col"]))
    if r["prio"]:
        bits.append("Prio " + r["prio"])
    if r["text"]:
        bits.append("„%s“" % r["text"])
    bits.append("nach Bedeutung + Wortlaut" if r["semantic"] else ("Wortlaut" if r["text"] else ""))
    if r["outlook"]:
        bits.append("+ Outlook")
    return " · ".join(b for b in bits if b)


# ─── Suchen ───────────────────────────────────────────────────────────

SEM_CARD = 0.50
SEM_DOC = 0.45


def find_cards(ctx, r, limit=300):
    cards = db.list_cards(ctx.path)
    if r["cat"]:
        cards = [c for c in cards if c.get("cat") == r["cat"]]
    if r["sub"]:
        cards = [c for c in cards if (c.get("sub") or "").upper().startswith(r["sub"])]
    if r["col"] == "offen":
        cards = [c for c in cards if c.get("col") != "erledigt"]
    elif r["col"]:
        cards = [c for c in cards if c.get("col") == r["col"]]
    if r["prio"]:
        cards = [c for c in cards if c.get("prio") == r["prio"]]
    if not r["text"]:
        return [c["id"] for c in cards][:limit], {}
    allowed = {c["id"] for c in cards}
    words = [norm(w) for w in r["text"].split()]
    hit, why = [], {}
    for c in cards:      # Wortlaut in der Karte selbst (alle Wörter, Wortanfang reicht)
        hay = norm(" ".join(str(c.get(k) or "") for k in ("t", "note", "sub", "src", "owner")))
        if all(w in hay for w in words):
            hit.append(c["id"])
            why[c["id"]] = "wort"
    for cid in db.search(ctx.path, r["text"], limit=200)["cards"]:   # auch über Materialien
        if cid in allowed and cid not in why:
            hit.append(cid)
            why[cid] = "material"
    if r["semantic"]:
        sem = ctx.vectors.search_text(r["text"])
        for h in sorted(sem.get("cards", []), key=lambda x: -x["score"]):
            if h["score"] >= SEM_CARD and h["id"] in allowed and h["id"] not in why:
                hit.append(h["id"])
                why[h["id"]] = "bedeutung %.2f" % h["score"]
        for d in sem.get("docs", []):
            if d["score"] >= SEM_DOC + 0.05:
                for cid in d.get("cards", []):
                    if cid in allowed and cid not in why:
                        hit.append(cid)
                        why[cid] = "material (bedeutung)"
    return hit[:limit], why


def _mail_docs(ctx, r, kinds):
    rows = db.mail_meta_rows(ctx.path)
    want_mail, want_att = "mail" in kinds, "attachment" in kinds
    rows = [m for m in rows if (want_mail and m["doc_id"] == m["mail_doc_id"])
            or (want_att and m["doc_id"] != m["mail_doc_id"])]
    if r["from"]:
        parts = norm(r["from"]).split()
        match = lambda ps: [m for m in rows if all(p in norm((m["from_name"] or "") + " " + (m["from_addr"] or ""))
                                                   for p in ps)]
        hit = match(parts)
        if not hit and len(parts) > 1:      # „von Müller Rechnung“: 2. Wort ist wohl Suchbegriff
            hit = match(parts[:1])
            r = dict(r, text=(r["text"] + " " + r["from"].split()[1]).strip())
        rows = hit
    if r["text"]:
        ids = db.fts_doc_ids(ctx.path, r["text"])
        # Anhänge auch dann, wenn nur ihre Mail passt (und umgekehrt)
        mails_hit = {m["mail_doc_id"] for m in rows if m["doc_id"] in ids}
        rows = [m for m in rows if m["doc_id"] in ids or m["mail_doc_id"] in mails_hit]
    rows.sort(key=lambda m: (-(m["date"] or 0), m["mail_doc_id"], m["doc_id"] != m["mail_doc_id"]))
    return rows


def find_docs(ctx, r, limit=100):
    kinds = [t for t in r["types"] if t != "card"] or ["mail", "attachment", "file", "note", "link"]
    out, seen = [], set()
    meta_by_doc = {}
    if "mail" in kinds or "attachment" in kinds:
        for m in _mail_docs(ctx, r, kinds):
            meta_by_doc[m["doc_id"]] = m
    plain = [k for k in kinds if k in ("file", "note", "link")]
    lib_ids = []
    if plain and not r["from"]:
        lib = db.library(ctx.path, q=r["text"], kinds=plain, limit=limit) if r["text"] else {"items": []}
        lib_ids = [d["id"] for d in lib["items"]]
    sem_ids = []
    if r["semantic"] and r["text"]:
        for d in ctx.vectors.search_text(r["text"]).get("docs", []):
            if d["score"] >= SEM_DOC:
                sem_ids.append((int(d["id"]), d["score"]))
    order = list(meta_by_doc) + lib_ids + [i for i, _ in sem_ids]
    docs = db.docs_by_ids(ctx.path, [i for i in order]) if order else {}
    allmeta = {m["doc_id"]: m for m in db.mail_meta_rows(ctx.path)} if order else {}
    for did in order:
        if did in seen or str(did) not in docs:
            continue
        d = docs[str(did)]
        is_sem = did not in meta_by_doc and did not in lib_ids
        if is_sem:   # semantische Treffer nur, wenn Art passt
            m = allmeta.get(did)
            k = ("mail" if m["doc_id"] == m["mail_doc_id"] else "attachment") if m else d["kind"]
            if k not in kinds or (r["from"] and not m):
                continue
            meta_by_doc.setdefault(did, m)
        seen.add(did)
        m = meta_by_doc.get(did) or allmeta.get(did)
        d["mail"] = ({"from": m["from_name"] or m["from_addr"], "from_addr": m["from_addr"],
                      "date": m["date"], "subject": m["subject"], "mailbox": m["mailbox"],
                      "is_attachment": m["doc_id"] != m["mail_doc_id"], "mail_doc_id": m["mail_doc_id"]}
                     if m else None)
        d["why"] = "bedeutung %.2f" % dict(sem_ids)[did] if is_sem else "wort"
        out.append(d)
        if len(out) >= limit:
            break
    return out


def find(ctx, q):
    r = parse(q)
    res = {"q": q, "interp": r, "cards": None, "why": {}, "docs": []}
    if r["mode"] in ("all", "cards", "both"):
        res["cards"], res["why"] = find_cards(ctx, r)
    if r["mode"] in ("docs", "both") or (r["mode"] == "all" and r["text"]):
        res["docs"] = find_docs(ctx, r)
    return res
