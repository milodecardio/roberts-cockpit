#!/usr/bin/env python3
"""Dateien vom Mac / aus OneDrive in die Cockpit-Bibliothek übertragen.

Läuft auf dem Mac, nur Python-Bordmittel. Bereits übertragene, unveränderte Dateien
werden übersprungen – das Skript kann also beliebig oft laufen (z. B. täglich).

Einrichten (einmalig):
    python3 scripts/cockpit_import.py --setup
      → legt ~/.config/cockpit-import.json an (Ordnerliste zum Bearbeiten)
      → speichert das Cockpit-Passwort im macOS-Schlüsselbund

Benutzen:
    python3 scripts/cockpit_import.py                 Probelauf: zeigt nur, was passieren würde
    python3 scripts/cockpit_import.py --los           wirklich übertragen
    python3 scripts/cockpit_import.py --los --max 50  höchstens 50 Dateien (zum Testen)
    python3 scripts/cockpit_import.py --quelle onedrive-getintune --los
"""
import argparse
import base64
import getpass
import json
import os
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

CONFIG = os.path.expanduser("~/.config/cockpit-import.json")
LOGFILE = os.path.expanduser("~/Library/Logs/cockpit-import.log")
KEYCHAIN_SERVICE = "cockpit-import"
SF_DATALESS = 0x40000000  # macOS: Datei liegt nur in der Cloud (OneDrive „bei Bedarf“)

GROUPS = {
    "dokumente": [".pdf", ".doc", ".docx", ".odt", ".rtf", ".txt", ".md", ".pages"],
    "tabellen": [".xls", ".xlsx", ".csv", ".ods", ".numbers"],
    "praesentationen": [".ppt", ".pptx", ".key", ".odp"],
    "bilder": [".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".webp"],
    "noten": [".mscz", ".musicxml", ".mxl", ".sib", ".gp", ".gpx", ".gp5", ".mid", ".midi"],
    "mails": [".eml"],
    "audio": [".mp3", ".m4a", ".wav", ".aif", ".aiff", ".flac", ".ogg"],
    "video": [".mp4", ".mov", ".m4v", ".avi", ".mkv"],
}

DEFAULT_CONFIG = {
    "server": "https://cockpit.robert-beckert.de",
    "user": "robert",
    "max_mb": 200,
    "gruppen": ["dokumente", "tabellen", "praesentationen", "bilder", "noten", "mails"],
    "_hinweis_gruppen": "audio und video erst aufnehmen, wenn die Hetzner Storage Box eingerichtet ist",
    "ordner_ausschliessen": [".git", "node_modules", "Library", ".Trash", "__pycache__",
                             ".venv", "venv", "Applications"],
    "quellen": [
        {"name": "onedrive-getintune", "beschriftung": "OneDrive (Getintune)",
         "pfad": "~/Library/CloudStorage/OneDrive-Getintuneorg", "aktiv": True},
        {"name": "onedrive-freigegeben", "beschriftung": "OneDrive (Freigegeben)",
         "pfad": "~/Library/CloudStorage/OneDrive-FreigegebeneBibliotheken–onedrive", "aktiv": True},
        {"name": "mac-dokumente", "beschriftung": "Mac Dokumente", "pfad": "~/Documents", "aktiv": True},
        {"name": "mac-schreibtisch", "beschriftung": "Mac Schreibtisch", "pfad": "~/Desktop", "aktiv": False},
        {"name": "mac-downloads", "beschriftung": "Mac Downloads", "pfad": "~/Downloads", "aktiv": False},
    ],
}


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOGFILE), exist_ok=True)
        with open(LOGFILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def fmt(n):
    return "%.1f MB" % (n / 1e6) if n < 1e9 else "%.2f GB" % (n / 1e9)


# ─── Zugangsdaten ─────────────────────────────────────────────────────

def get_password(user):
    p = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", user, "-w"],
                       capture_output=True, text=True)
    if p.returncode != 0:
        sys.exit("Kein Passwort im Schlüsselbund. Bitte zuerst:  python3 %s --setup" % sys.argv[0])
    return p.stdout.strip()


def setup():
    if not os.path.exists(CONFIG):
        os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
        with open(CONFIG, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2, ensure_ascii=False)
        print("Konfiguration angelegt:", CONFIG)
    else:
        print("Konfiguration vorhanden:", CONFIG)
    cfg = load_config()
    pw = getpass.getpass("Cockpit-Passwort für '%s' (Eingabe unsichtbar): " % cfg["user"])
    if not pw:
        sys.exit("Abgebrochen.")
    p = subprocess.run(["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE,
                        "-a", cfg["user"], "-w", pw], text=True, capture_output=True)
    if p.returncode != 0:
        sys.exit("Schlüsselbund-Fehler: " + p.stderr)
    api = Api(cfg, pw)
    try:
        st = api.get("/api/status")
        print("✅ Verbindung ok – Bibliothek enthält %d Materialien." % sum(st["docs"].values()))
    except Exception as e:  # noqa: BLE001
        print("⚠️ Passwort gespeichert, aber Verbindung fehlgeschlagen:", e)


def load_config():
    if not os.path.exists(CONFIG):
        sys.exit("Keine Konfiguration. Bitte zuerst:  python3 %s --setup" % sys.argv[0])
    with open(CONFIG, encoding="utf-8") as f:
        return json.load(f)


# ─── Server ───────────────────────────────────────────────────────────

class Api:
    def __init__(self, cfg, pw):
        self.base = cfg["server"].rstrip("/")
        token = base64.b64encode(("%s:%s" % (cfg["user"], pw)).encode()).decode()
        self.auth = {"Authorization": "Basic " + token}

    def get(self, path):
        req = urllib.request.Request(self.base + path, headers=self.auth)
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)

    def upload(self, source, ref, mtime, filepath, size, origin_path):
        q = urllib.parse.quote
        headers = dict(self.auth)
        headers.update({
            "Content-Type": "application/octet-stream",
            "Content-Length": str(size),
            "X-Source": source,
            "X-Source-Ref": q(ref),
            "X-Source-Mtime": repr(mtime),
            "X-Filename": q(os.path.basename(filepath)),
            "X-Origin-Path": q(origin_path),
        })
        with open(filepath, "rb") as f:
            req = urllib.request.Request(self.base + "/api/import/file", data=f, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=max(120, size // 200_000)) as r:
                return json.load(r)


# ─── Dateien finden ───────────────────────────────────────────────────

def scan(src, exts, exclude, max_bytes):
    root = os.path.expanduser(src["pfad"])
    if not os.path.isdir(root):
        log("⚠️ Ordner fehlt: %s" % root)
        return
    for dp, dns, fns in os.walk(root):
        dns[:] = sorted(d for d in dns if not d.startswith(".") and d not in exclude
                        and not d.endswith((".app", ".photoslibrary", ".musiclibrary", ".fcpbundle")))
        for fn in sorted(fns):
            if fn.startswith((".", "~$")):
                continue
            if os.path.splitext(fn)[1].lower() not in exts:
                continue
            full = os.path.join(dp, fn)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode) or st.st_size == 0:
                continue
            rel = os.path.relpath(full, root)
            yield {
                "path": full, "ref": rel, "size": st.st_size, "mtime": st.st_mtime,
                "cloud_only": bool(getattr(st, "st_flags", 0) & SF_DATALESS),
                "too_big": st.st_size > max_bytes,
                "origin_path": os.path.join(src.get("beschriftung", src["name"]), os.path.dirname(rel)),
            }


def main():
    ap = argparse.ArgumentParser(description="Dateien in die Cockpit-Bibliothek übertragen")
    ap.add_argument("--setup", action="store_true", help="Konfiguration + Passwort einrichten")
    ap.add_argument("--los", action="store_true", help="wirklich übertragen (sonst nur Probelauf)")
    ap.add_argument("--max", type=int, default=0, help="höchstens so viele Dateien übertragen")
    ap.add_argument("--quelle", help="nur diese Quelle (name aus der Konfiguration)")
    ap.add_argument("--nur-lokal", action="store_true",
                    help="OneDrive-Dateien, die nur in der Cloud liegen, überspringen (kein Download)")
    a = ap.parse_args()
    if a.setup:
        return setup()

    cfg = load_config()
    exts = {e for g in cfg.get("gruppen", []) for e in GROUPS.get(g, [])}
    exclude = set(cfg.get("ordner_ausschliessen", []))
    max_bytes = int(cfg.get("max_mb", 200)) * 1024 * 1024
    api = Api(cfg, get_password(cfg["user"]))
    log("=== %s – Gruppen: %s ===" % ("ÜBERTRAGUNG" if a.los else "PROBELAUF (nichts wird übertragen)",
                                      ", ".join(cfg.get("gruppen", []))))
    sent = 0
    for src in cfg["quellen"]:
        if a.quelle and src["name"] != a.quelle:
            continue
        if not a.quelle and not src.get("aktiv", True):
            continue
        known = api.get("/api/import/known?source=" + urllib.parse.quote(src["name"]))
        new = changed = same = big = cloud = errors = 0
        new_b = 0
        for f in scan(src, exts, exclude, max_bytes):
            k = known.get(f["ref"])
            if k and abs((k[0] or 0) - f["mtime"]) < 2 and k[1] == f["size"]:
                same += 1
                continue
            if f["too_big"]:
                big += 1
                continue
            if f["cloud_only"] and a.nur_lokal:
                cloud += 1
                continue
            if k:
                changed += 1
            else:
                new += 1
            new_b += f["size"]
            if not a.los:
                continue
            if a.max and sent >= a.max:
                continue
            for attempt in range(3):
                try:
                    r = api.upload(src["name"], f["ref"], f["mtime"], f["path"], f["size"], f["origin_path"])
                    sent += 1
                    log("  ⬆︎ %s (%s)%s" % (f["ref"], fmt(f["size"]), "" if r.get("created") else " – Inhalt schon vorhanden"))
                    break
                except (urllib.error.URLError, OSError, TimeoutError) as e:
                    if attempt == 2:
                        errors += 1
                        log("  ⚠️ %s: %s" % (f["ref"], e))
                    else:
                        time.sleep(5 * (attempt + 1))
        log("%s: %d neu, %d geändert (%s) · %d unverändert · %d zu groß (> %d MB)%s%s" % (
            src.get("beschriftung", src["name"]), new, changed, fmt(new_b), same, big, cfg.get("max_mb", 200),
            " · %d nur in Cloud übersprungen" % cloud if cloud else "",
            " · %d Fehler" % errors if errors else ""))
    if a.los:
        log("Fertig: %d Dateien übertragen. Der Server liest sie jetzt im Hintergrund aus." % sent)
    else:
        log("Probelauf beendet. Zum Übertragen:  python3 %s --los" % sys.argv[0])


if __name__ == "__main__":
    main()
