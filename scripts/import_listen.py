#!/usr/bin/env python3
"""Importiert die Aufgabenlisten (Liste-1 … Liste-8) ins Cockpit.

Liest einen Cockpit-Stand (Backup-JSON), ordnet die alten Karten den neuen
Bereichen B1–B8 + Vision zu und hängt alle Aufgaben aus den Listen an.
Listen-Aufgaben landen in der Spalte „Eingang“ (Status setzt Robert selbst),
Liste 8 (Themen ohne Aufgabe) als Info-Karten in „Geparkt“.

Idempotent: Karten, deren ID (= Kennung, z. B. B1.7-01) schon existiert,
werden nicht noch einmal angelegt.

Aufruf:
  python3 scripts/import_listen.py <backup.json> <listen-ordner> <ausgabe.json>
"""
import glob
import json
import os
import re
import sys
import time

OWNERS = {"Robert", "Claude", "Claude Code", "Rahmi", "Mutter", "Dritte"}
STATUS = {"Offen": "offen", "Unklar": "unklar", "Erledigt": "erledigt", "Überholt": "überholt"}

# Alte Karten → (Bereich, Unterthema-Kennung, mögliche Dublette)
LEGACY = {
    "c1": ("b2", None, None),
    "c2": ("b1", "B1.2", "B1.2-04"),
    "c3": ("b1", "B1.4", None), "c4": ("b1", "B1.4", None), "c5": ("b1", "B1.4", None),
    "c6": ("b1", "B1.4", "B1.4-15"), "c7": ("b1", "B1.4", None),
    "c8": ("b1", "B1.4", "B1.4-16"), "c9": ("b1", "B1.4", None),
    "c10": ("b1", "B1.4", "B1.4-15"),
    "c11": ("b1", "B1.9", "B1.9-01"), "c12": ("b1", "B1.10", None),
    "c13": ("b1", "B1.10", None), "c14": ("b1", "B1.9", None),
    "c15": ("b1", "B1.9", None), "c16": ("b1", "B1.9", None),
    "c17": ("b1", "B1.10", "B1.10-01/-02"), "c18": ("b1", "B1.9", "B1.9-01"),
    "c19": ("b1", "B1.4", "B1.4-16"), "c20": ("b1", "B1.12", None),
    "c21": ("b1", "B1.9", None),
    "c22": ("vision", None, None), "c23": ("vision", None, None),
    "c24": ("vision", None, None), "c25": ("vision", None, None),
    "c26": ("vision", None, None), "c27": ("vision", None, None),
    "c28": ("b1", "B1.10", None), "c29": ("b1", "B1.10", None),
    "c30": ("b1", "B1.9", None), "c31": ("b1", "B1.9", None),
    "c32": ("b1", "B1.4", None), "c33": ("b1", "B1.9", None),
}

# Sammelpunkt B1.9-15 bis B1.9-23 → einzelne Anforderungen
B19_REQS = [
    "Todos ins Kanban", "Kanban in Claude", "Geplante Aufgaben", "Artefakte",
    "Websuche mit Dateierstellung", "Bildverarbeitung und OCR", "Custom Skills",
    "Code-Ausführung", "Dokumente aus fremden KI-Systemen",
]

TASK_RE = re.compile(r"^- (B\d(?:\.\d+)?-\d+) (.+)$")
HEAD_RE = re.compile(r"^## (B\d\.\d+) (.+?) \((\d+)(, alle offen)?\)$")
INFO_RE = re.compile(r"^- \*\*(C\d+), ([\d.]+):\*\*\s*(.*)$")


def split_owner(text):
    m = re.search(r"\s*\(([^()]+)\)$", text)
    if m:
        names = [n.strip() for n in m.group(1).split(",")]
        if all(n in OWNERS for n in names):
            return text[: m.start()].strip(), ", ".join(names)
    return text, "Robert"


def make_task(kid, text, status, sub, src):
    text, owner = split_owner(text)
    title, _, rest = text.partition("; ")
    note = (rest[:1].upper() + rest[1:] + " · " if rest else "") + "Status laut Liste: " + status
    return {"id": kid, "col": "eingang", "cat": "b" + kid[1], "sub": sub, "owner": owner,
            "prio": "Mittel", "t": kid + " " + title, "note": note, "src": src, "tools": []}


def parse_list(path):
    src = os.path.basename(path)
    cards, subs = [], {}
    sub, status = "", "offen"
    for line in open(path, encoding="utf-8").read().splitlines():
        if line.startswith("**Hinweis"):  # Hinweise nennen Kennungen nur, sind keine Aufgaben
            break
        h = HEAD_RE.match(line)
        if h:
            sub = h.group(1) + " " + h.group(2)
            subs[h.group(1)] = sub
            status = "offen"
            continue
        s = re.match(r"^\*\*(\w+)\*\*$", line)
        if s and s.group(1) in STATUS:
            status = STATUS[s.group(1)]
            continue
        if line.startswith("- B1.9-15 bis B1.9-23"):
            for i, req in enumerate(B19_REQS, start=15):
                cards.append(make_task("B1.9-%02d" % i, "Anforderung: " + req, status, sub, src))
            continue
        t = TASK_RE.match(line)
        if t:
            cards.append(make_task(t.group(1), t.group(2), status, sub, src))
    return cards, subs


def parse_info(path):
    src = os.path.basename(path)
    cards, sub, cur = [], "", None
    for line in open(path, encoding="utf-8").read().splitlines():
        if line.startswith("## "):
            sub = line[3:].strip()
            continue
        m = INFO_RE.match(line)
        if m:
            cur = {"id": m.group(1), "col": "geparkt", "cat": "b8", "sub": sub, "owner": "",
                   "prio": "Niedrig", "t": ("%s (%s) %s" % (m.group(1), m.group(2), m.group(3))).strip(),
                   "note": "", "src": src, "tools": []}
            cards.append(cur)
            continue
        if cur and line.startswith("  - "):
            cur["note"] += ("\n" if cur["note"] else "") + "• " + line[4:].strip()
        elif not line.strip():
            continue
        else:
            cur = None
    for c in cards:  # Karten ohne Text im Kopf: erste Unterzeile wird Titel
        if c["t"].endswith(")") and c["note"]:
            c["t"] += " " + c["note"].split("\n")[0][2:]
        c["note"] = (c["note"] + "\n" if c["note"] else "") + "Info, keine Aufgabe"
    return cards


def main(backup, folder, out):
    state = json.load(open(backup, encoding="utf-8"))
    new_cards, subs = [], {}
    for path in sorted(glob.glob(os.path.join(folder, "Liste-*.md"))):
        if os.path.basename(path).startswith("Liste-8"):
            new_cards += parse_info(path)
        else:
            c, s = parse_list(path)
            new_cards += c
            subs.update(s)

    for c in state["cards"]:
        if c["id"] in LEGACY and "sub" not in c:  # noch nicht umgestellt
            cat, subkey, dup = LEGACY[c["id"]]
            c["cat"] = cat
            c["sub"] = subs.get(subkey, "") if subkey else ""
            c.setdefault("owner", "")
            if dup and "Dublette" not in (c.get("note") or ""):
                c["note"] = (c.get("note") or "") + " · ⚠ mögliche Dublette: " + dup

    existing = {c["id"] for c in state["cards"]}
    added = [c for c in new_cards if c["id"] not in existing]
    state["cards"] += added
    state["_ts"] = int(time.time() * 1000)
    json.dump(state, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("%d neue Karten, %d gesamt → %s" % (len(added), len(state["cards"]), out))


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    main(*sys.argv[1:])
