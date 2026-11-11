"""Mails ins Cockpit übernehmen (Etappe 5).

Ein gemeinsamer Weg für alle Mails – egal ob aus Outlook (Microsoft Graph),
per Upload/Drag & Drop als .eml oder als .msg (Outlook-Format):

  - Original:  die ganze Mail als Datei (.eml bzw. .msg) – Text wird vom Worker ausgelesen
               und ist damit durchsuchbar und vektorisiert.
  - Anhänge:   jeder Anhang als eigenes Material (PDF, Word, Fotos …), ebenfalls durchsuchbar.
               Kleine eingebettete Bilder (Logos in Signaturen) werden übersprungen.
  - Kein doppelter Import: Schlüssel ist die Message-ID der Mail (Tabelle sources,
    Quelle "mail" bzw. "mail-anhang"). Dieselbe Mail aus Outlook und später noch einmal
    per Drag & Drop → wird nur erneut verknüpft, nicht neu gespeichert.
Nur Python-Bordmittel (.msg zusätzlich mit `msgconvert`, Paket libemail-outlook-message-perl).
"""
import email
import email.policy
import email.utils
import hashlib
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.parse

import db
import msgraph

SOURCE = "mail"
SOURCE_ATT = "mail-anhang"
MAX_MAIL = 150 * 1024 * 1024
SKIP_INLINE_IMAGE = 60 * 1024     # eingebettete Bilder unter 60 KB = Logos/Signaturen
MAIL_EXT = (".eml", ".msg")


class MailError(Exception):
    pass


# ─── Dateien ablegen ──────────────────────────────────────────────────

def store_bytes(files_dir, data):
    """Wie Uploads: Ablage nach SHA-256 unter files/ab/cd/abcd…"""
    sha = hashlib.sha256(data).hexdigest()
    dest = os.path.join(files_dir, sha[:2], sha[2:4], sha)
    if not os.path.exists(dest):
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp_dir = os.path.join(files_dir, ".tmp")
        os.makedirs(tmp_dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=tmp_dir)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, dest)
        except Exception:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise
    return sha


def _safe_name(s, maxlen=90):
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", s or "")
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s[:maxlen].rstrip(" .") or "Mail"


# ─── Mail zerlegen ────────────────────────────────────────────────────

def normalize_id(mid):
    return (mid or "").strip().strip("<>").strip().lower()


def msg_to_eml(raw):
    """Outlook-.msg → .eml (über msgconvert). None, wenn nicht möglich."""
    tool = shutil.which("msgconvert")
    if not tool:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "mail.msg")
        with open(src, "wb") as f:
            f.write(raw)
        try:
            subprocess.run([tool, "--outfile", os.path.join(tmp, "mail.eml"), src], cwd=tmp,
                           capture_output=True, timeout=120, check=False)
        except subprocess.TimeoutExpired:
            return None
        for name in os.listdir(tmp):
            if name.endswith(".eml"):
                with open(os.path.join(tmp, name), "rb") as f:
                    return f.read()
    return None


def _header_info(msg):
    subject = str(msg.get("Subject", "") or "").strip() or "(ohne Betreff)"
    name, addr = email.utils.parseaddr(str(msg.get("From", "") or ""))
    sender = name or addr or "?"
    try:
        dt = email.utils.parsedate_to_datetime(str(msg.get("Date")))
        ts = dt.timestamp()
    except (TypeError, ValueError, IndexError):
        ts = time.time()
    return subject, sender, addr, ts


def _attachments(msg):
    """(dateiname, mime, bytes) aller echten Anhänge."""
    out = []
    for i, part in enumerate(msg.iter_attachments()):
        ctype = part.get_content_type()
        name = part.get_filename()
        if ctype == "message/rfc822":
            inner = part.get_payload(0) if part.is_multipart() else None
            try:
                data = (inner or part.get_content()).as_bytes()
            except Exception:  # noqa: BLE001
                continue
            if not name:
                sub = str((inner or part.get_content()).get("Subject", "") or "Weitergeleitete Mail")
                name = _safe_name(sub) + ".eml"
        else:
            data = part.get_payload(decode=True)
            if data is None:
                continue
            is_inline = part.get_content_disposition() == "inline" or (part.get("Content-ID") and not name)
            if ctype.startswith("image/") and is_inline and len(data) < SKIP_INLINE_IMAGE:
                continue
            if not name:
                name = "anhang-%d%s" % (i + 1, mimetypes.guess_extension(ctype) or ".bin")
        name = os.path.basename(name.replace("\\", "/")) or "anhang"
        if ctype in ("application/octet-stream", "") or "/" not in ctype:
            ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if data:
            out.append((name, ctype, data))
    return out


# ─── Import ───────────────────────────────────────────────────────────

def _link_existing(path, card_id, mid, actor):
    main = db.source_docs(path, SOURCE, mid)
    if main is None:
        return None
    atts = db.source_docs_prefix(path, SOURCE_ATT, mid + "#")
    if card_id:
        for did in [main] + atts:
            db.attach_doc(path, card_id, did)
    try:
        title = db.get_doc(path, main)["title"]
    except db.NotFound:
        title = ""
    db.log(path, actor, "mail_verknuepfen", {"karte": card_id, "dok": main, "message_id": mid})
    return {"already": True, "doc_id": main, "title": title, "attachments": atts, "card": card_id}


def import_raw(path, files_dir, raw, card_id=None, filename=None, mime=None, origin_path=None,
               actor="robert", ref=None):
    """Mail (als .eml- oder .msg-Bytes) übernehmen. Gibt Zusammenfassung zurück."""
    if card_id:
        db.get_card(path, card_id)   # wirft NotFound, bevor etwas gespeichert wird
    if len(raw) > MAX_MAIL:
        raise MailError("Mail ist zu groß")
    is_msg = (filename or "").lower().endswith(".msg") or raw[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    eml = msg_to_eml(raw) if is_msg else raw
    if eml is None:
        raise MailError("Outlook-.msg kann auf diesem Server nicht gelesen werden (msgconvert fehlt)")
    msg = email.message_from_bytes(eml, policy=email.policy.default)
    subject, sender, addr, ts = _header_info(msg)
    mid = normalize_id(ref or msg.get("Message-ID"))
    if not mid:
        mid = "sha:" + hashlib.sha256(eml).hexdigest()
    done = _link_existing(path, card_id, mid, actor)
    if done:
        return done

    day = time.strftime("%Y-%m-%d", time.localtime(ts))
    title = "✉️ %s – %s (%s)" % (subject, sender, time.strftime("%d.%m.%Y", time.localtime(ts)))
    ext = ".msg" if is_msg else ".eml"
    fname = "%s %s%s" % (day, _safe_name(subject, 80), ext)
    mail_mime = "application/vnd.ms-outlook" if is_msg else "message/rfc822"
    sha = store_bytes(files_dir, raw)
    main = db.add_import_doc(path, SOURCE, mid, ts, len(raw), fname, mail_mime, sha,
                             origin_path or "E-Mail", title=title)["id"]
    att_ids, att_names = [], []
    for i, (name, ctype, data) in enumerate(_attachments(msg)):
        asha = store_bytes(files_dir, data)
        r = db.add_import_doc(path, SOURCE_ATT, "%s#%03d" % (mid, i), ts, len(data), name, ctype, asha,
                              "E-Mail: %s (%s)" % (subject, sender))
        att_ids.append(r["id"])
        att_names.append(name)
    if card_id:
        for did in [main] + att_ids:
            db.attach_doc(path, card_id, did)
    db.log(path, actor, "mail_importieren", {"karte": card_id, "dok": main, "betreff": subject,
                                             "von": addr or sender, "anhaenge": att_names})
    return {"already": False, "doc_id": main, "title": title, "attachments": att_ids,
            "attachment_names": att_names, "card": card_id}


def is_mail_file(filename, mime=None):
    return (filename or "").lower().endswith(MAIL_EXT) or (mime or "") in ("message/rfc822",
                                                                          "application/vnd.ms-outlook")


# ─── Outlook (Microsoft Graph) ────────────────────────────────────────

SELECT = "id,subject,from,receivedDateTime,hasAttachments,internetMessageId,bodyPreview,parentFolderId"


def _kql(q):
    return '"%s"' % re.sub(r'["\\]', " ", q).strip()


def search(path, q="", n=10):
    """Mails in Outlook suchen (alle Ordner). Leere Suche → neueste Mails."""
    n = max(1, min(int(n or 10), 25))
    if (q or "").strip():
        rel = "/me/messages?$search=%s&$top=%d&$select=%s" % (urllib.parse.quote(_kql(q)), n, SELECT)
    else:
        rel = "/me/messages?$top=%d&$orderby=receivedDateTime%%20desc&$select=%s" % (n, SELECT)
    items = msgraph.get_json(path, rel).get("value", [])
    out = []
    for m in items:
        fr = (m.get("from") or {}).get("emailAddress") or {}
        mid = normalize_id(m.get("internetMessageId"))
        out.append({
            "id": m["id"], "subject": m.get("subject") or "(ohne Betreff)",
            "from": fr.get("name") or fr.get("address") or "?", "from_addr": fr.get("address") or "",
            "date": m.get("receivedDateTime"), "has_attachments": bool(m.get("hasAttachments")),
            "preview": (m.get("bodyPreview") or "")[:240],
            "imported": bool(mid and db.source_docs(path, SOURCE, mid) is not None),
        })
    out.sort(key=lambda x: x["date"] or "", reverse=True)
    return out


_folders = {}


def _folder_name(path, folder_id):
    if not folder_id:
        return ""
    if folder_id not in _folders:
        try:
            _folders[folder_id] = msgraph.get_json(
                path, "/me/mailFolders/%s?$select=displayName" % urllib.parse.quote(folder_id, safe="")
            ).get("displayName", "")
        except msgraph.GraphError:
            _folders[folder_id] = ""
    return _folders[folder_id]


def import_graph(path, files_dir, message_id, card_id=None, actor="robert"):
    """Mail aus Outlook holen (Original im MIME-Format inkl. Anhängen) und übernehmen."""
    if card_id:
        db.get_card(path, card_id)
    q = urllib.parse.quote(message_id, safe="")
    meta = msgraph.get_json(path, "/me/messages/%s?$select=%s" % (q, SELECT))
    mid = normalize_id(meta.get("internetMessageId"))
    if mid:
        done = _link_existing(path, card_id, mid, actor)
        if done:
            return done
    raw = msgraph.get_bytes(path, "/me/messages/%s/$value" % q, max_bytes=MAX_MAIL)
    folder = _folder_name(path, meta.get("parentFolderId"))
    return import_raw(path, files_dir, raw, card_id=card_id, filename="mail.eml",
                      origin_path="Outlook" + ("/" + folder if folder else ""), actor=actor, ref=mid or None)
