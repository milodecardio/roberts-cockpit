#!/usr/bin/env python3
"""OneDrive-Scan-Eingang für Roberts Cockpit.

Robert scannt mit dem Handy (OneDrive-App → „Scannen“) in den Ordner
„AAA Cockpit-Eingang“ seines OneDrive (rb@robert-beckert.de). Der Server schaut
alle 10 Minuten nach, nimmt neue Dateien ohne Karte in die Bibliothek auf
(Quelle „onedrive-scan“; Text auslesen und Vektoren macht wie immer der Worker)
und verschiebt sie danach nach „AAA Cockpit-Eingang/übernommen“.
Gelöscht wird nichts – weder in OneDrive noch im Cockpit.

Braucht die delegierte Berechtigung Files.ReadWrite (msgraph.SCOPES). Fehlt sie im
Token, steht in /api/status → onedrive_scan.needs_relogin = true.

Ablauf pro Datei (wiederholbar ohne Dubletten):
  1. herunterladen → files/ nach SHA-256 (gleicher Inhalt wird nicht doppelt gespeichert)
  2. db.add_import_doc(source="onedrive-scan", ref=<OneDrive-Element-ID>)
  3. nach „übernommen“ verschieben (Namenskonflikt → Zeitstempel anhängen)
Schlägt 3. fehl, wird beim nächsten Lauf nur das Verschieben wiederholt.

Kommandozeile (auf dem Server als Nutzer cockpit):  python3 onedrive_inbox.py --einmal
Nur Python-Bordmittel.
"""
import calendar
import hashlib
import json
import mimetypes
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import db
import msgraph

SOURCE = "onedrive-scan"
FOLDER = os.environ.get("ONEDRIVE_INBOX", "AAA Cockpit-Eingang").strip("/ ")
DONE_NAME = "übernommen"
INTERVAL = int(os.environ.get("ONEDRIVE_INBOX_INTERVAL", "600"))
MIN_AGE = 120          # Dateien, die gerade erst hochgeladen werden, noch nicht anfassen
MAX_BYTES = int(os.environ.get("COCKPIT_MAX_UPLOAD_MB", "1024")) * 1024 * 1024
META_KEY = "onedrive_scan"

_wake = threading.Event()
_run_lock = threading.Lock()
_state = {}


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _enc(p):
    return urllib.parse.quote(p, safe="/")


def status(path):
    """Zustand für /api/status (auch nach Neustart: aus der Datenbank)."""
    st = dict(_state) if _state else None
    if st is None:
        raw = db.meta_get(path, META_KEY)
        try:
            st = json.loads(raw) if raw else {}
        except ValueError:
            st = {}
    st["folder"] = FOLDER
    st["interval_s"] = INTERVAL
    st["needs_relogin"] = bool(msgraph.configured() and msgraph.status(path)["connected"]
                               and not msgraph.has_files_access(path))
    return st


def _save_state(path, **kw):
    _state.update(kw)
    try:
        db.meta_set(path, META_KEY, json.dumps(_state, ensure_ascii=False))
    except Exception:  # noqa: BLE001  – Status ist nur Anzeige
        pass


def _folder(path, rel):
    try:
        return msgraph.get_json(path, "/me/drive/root:/%s" % _enc(rel))
    except msgraph.GraphError as e:
        if e.code == 404:
            return None
        raise


def _done_folder(path, inbox):
    d = _folder(path, FOLDER + "/" + DONE_NAME)
    if d:
        return d
    try:
        return msgraph.send_json(path, "POST", "/me/drive/items/%s/children" % inbox["id"],
                                 {"name": DONE_NAME, "folder": {},
                                  "@microsoft.graph.conflictBehavior": "fail"})
    except msgraph.GraphError as e:
        if e.code == 409:   # gleichzeitig angelegt
            return _folder(path, FOLDER + "/" + DONE_NAME)
        raise


def _children(path, folder_id):
    rel = "/me/drive/items/%s/children?$top=200" % folder_id
    while rel:
        page = msgraph.get_json(path, rel)
        for it in page.get("value", []):
            yield it
        rel = page.get("@odata.nextLink")


def _download(path, files_dir, item):
    """Lädt die Datei in files/ (nach SHA-256). Gibt (sha, size) zurück."""
    url = item.get("@microsoft.graph.downloadUrl")
    if not url:
        item = msgraph.get_json(path, "/me/drive/items/%s" % item["id"])
        url = item.get("@microsoft.graph.downloadUrl")
    if not url:
        raise msgraph.GraphError("Kein Download-Link für %s" % item.get("name"))
    tmp_dir = os.path.join(files_dir, ".tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=tmp_dir)
    h, size = hashlib.sha256(), 0
    try:
        # vorab signierter Link: ohne Authorization-Kopf abrufen
        req = urllib.request.Request(url, headers={"User-Agent": msgraph.UA})
        with urllib.request.urlopen(req, timeout=300) as r, os.fdopen(fd, "wb") as f:
            while True:
                chunk = r.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_BYTES:
                    raise msgraph.GraphError("%s ist zu groß (max. %d MB)" % (item.get("name"), MAX_BYTES // 1048576))
                h.update(chunk)
                f.write(chunk)
        sha = h.hexdigest()
        dest = os.path.join(files_dir, sha[:2], sha[2:4], sha)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if os.path.exists(dest):
            os.remove(tmp)
        else:
            os.replace(tmp, dest)
        return sha, size
    except urllib.error.URLError as e:
        raise msgraph.GraphError("Download von %s fehlgeschlagen: %s" % (item.get("name"), e))
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _move(path, item, done_id):
    body = {"parentReference": {"id": done_id}}
    try:
        msgraph.send_json(path, "PATCH", "/me/drive/items/%s" % item["id"], body)
    except msgraph.GraphError as e:
        if e.code != 409:
            raise
        # gleicher Name liegt schon in „übernommen“ → mit Zeitstempel ablegen (nichts überschreiben)
        stem, ext = os.path.splitext(item["name"])
        body["name"] = "%s (%s)%s" % (stem, time.strftime("%Y-%m-%d %H%M%S"), ext)
        msgraph.send_json(path, "PATCH", "/me/drive/items/%s" % item["id"], body)


def _mtime(item):
    raw = (item.get("fileSystemInfo") or {}).get("lastModifiedDateTime") or item.get("lastModifiedDateTime")
    if not raw:
        return 0.0
    try:
        return float(calendar.timegm(time.strptime(raw[:19], "%Y-%m-%dT%H:%M:%S")))   # Graph liefert UTC
    except ValueError:
        return 0.0


def run_once(path, files_dir):
    """Ein Durchlauf. Gibt Zusammenfassung zurück; Fehler landen im Status."""
    with _run_lock:
        started = time.time()
        _save_state(path, last_run=_now_iso(), running=True)
        summary = {"imported": 0, "already": 0, "moved": 0, "skipped_young": 0, "errors": []}
        try:
            if not msgraph.configured():
                raise msgraph.NotConnected("Microsoft-Anbindung ist nicht eingerichtet (MS_CLIENT_ID fehlt).")
            if not msgraph.status(path)["connected"]:
                raise msgraph.NotConnected("Nicht bei Microsoft angemeldet – im Cockpit über „✉️ Outlook“ verbinden.")
            if not msgraph.has_files_access(path):
                raise msgraph.NotConnected("Bitte neu anmelden: Berechtigung Files.ReadWrite fehlt noch "
                                           "(„✉️ Outlook“ → Trennen → Verbinden).")
            inbox = _folder(path, FOLDER)
            if not inbox:
                raise msgraph.GraphError("Ordner „%s“ nicht im OneDrive gefunden." % FOLDER)
            done = _done_folder(path, inbox)
            known = db.import_known(path, SOURCE)
            for it in list(_children(path, inbox["id"])):
                if "file" not in it:
                    continue           # Unterordner (z. B. „übernommen“)
                age = time.time() - _mtime(it)
                if _mtime(it) and age < MIN_AGE:
                    summary["skipped_young"] += 1
                    continue
                try:
                    k = known.get(it["id"])
                    if k and k[1] == it.get("size"):
                        summary["already"] += 1   # schon übernommen, nur Verschieben war offen
                    else:
                        sha, size = _download(path, files_dir, it)
                        name = it["name"]
                        mime = ((it.get("file") or {}).get("mimeType")
                                or mimetypes.guess_type(name)[0] or "application/octet-stream")
                        r = db.add_import_doc(path, SOURCE, it["id"], _mtime(it), size, name, mime, sha,
                                              "OneDrive/%s/%s" % (FOLDER, name))
                        summary["imported" if r["created"] else "already"] += 1
                        db.log(path, "onedrive-scan", "scan_uebernehmen",
                               {"dok": r["id"], "datei": name, "neu": r["created"]})
                    _move(path, it, done["id"])
                    summary["moved"] += 1
                except Exception as e:  # noqa: BLE001 – eine kaputte Datei hält die anderen nicht auf
                    summary["errors"].append("%s: %s" % (it.get("name"), e))
            err = "; ".join(summary["errors"])[:1000] or None
            _save_state(path, running=False, ok=not err, error=err, last_summary=summary,
                        last_ok=_now_iso() if not err else _state.get("last_ok"),
                        duration_s=round(time.time() - started, 1))
        except Exception as e:  # noqa: BLE001
            _save_state(path, running=False, ok=False, error=str(e)[:1000], last_summary=summary,
                        duration_s=round(time.time() - started, 1))
        return dict(_state)


def trigger():
    """Sofort einen Durchlauf anstoßen (Knopf „jetzt prüfen“)."""
    _wake.set()


def start(path, files_dir):
    """Hintergrund-Thread im Server: alle INTERVAL Sekunden ein Durchlauf."""
    if not msgraph.configured() or INTERVAL <= 0:
        return None
    try:
        _state.update(json.loads(db.meta_get(path, META_KEY) or "{}"))
    except ValueError:
        pass
    _state["running"] = False

    def loop():
        time.sleep(30)   # Server erst in Ruhe hochfahren lassen
        while True:
            run_once(path, files_dir)
            _wake.wait(INTERVAL)
            _wake.clear()

    t = threading.Thread(target=loop, name="onedrive-inbox", daemon=True)
    t.start()
    return t


def _cli():
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.environ.get("COCKPIT_DB", os.path.join(here, "cockpit.db"))
    files_dir = os.environ.get("COCKPIT_FILES", os.path.join(here, "files"))
    db.init(path)
    print(json.dumps(run_once(path, files_dir), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
