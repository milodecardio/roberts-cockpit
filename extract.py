"""Text aus Dateien gewinnen (Etappe 3).

extract(path, mime, filename) -> (status, text, hinweis)
  status: "done"    Text gewonnen
          "skipped" Typ wird (noch) nicht ausgelesen, z. B. Audio/Video
          "failed"  Fehler beim Auslesen

Nutzt Kommandozeilen-Werkzeuge, wenn vorhanden (Server):
  pdftotext/pdftoppm (poppler-utils), tesseract (+deu), catdoc/xls2csv, heif-convert.
Word/Excel/PowerPoint (docx/xlsx/pptx/odt) werden mit Python-Bordmitteln gelesen.
"""
import csv
import email
import email.policy
import html
import io
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
import xml.etree.ElementTree as ET

MAX_CHARS = 2_000_000          # mehr Text pro Datei wird abgeschnitten
OCR_MAX_PAGES = 40             # Scans: höchstens so viele Seiten per OCR
OCR_LANG = "deu+eng"
TIMEOUT = 600                  # Sekunden pro Werkzeug-Aufruf


class Skip(Exception):
    pass


def _run(args, timeout=TIMEOUT, input_bytes=None):
    p = subprocess.run(args, input=input_bytes, capture_output=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError("%s: %s" % (args[0], p.stderr.decode("utf-8", "replace")[:300]))
    return p.stdout


def _have(tool):
    return shutil.which(tool) is not None


def _clean(text):
    text = text.replace("\x00", " ").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()[:MAX_CHARS]


def _decode(b):
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", "replace")


# ─── einzelne Formate ─────────────────────────────────────────────────

def _ocr_image(path):
    if not _have("tesseract"):
        raise Skip("Texterkennung (tesseract) nicht installiert")
    return _decode(_run(["tesseract", path, "-", "-l", OCR_LANG, "--psm", "3"]))


def _pdf(path):
    if not _have("pdftotext"):
        raise Skip("pdftotext nicht installiert")
    text = _decode(_run(["pdftotext", "-enc", "UTF-8", path, "-"]))
    pages = max(1, text.count("\f") + 1)
    # Kaum Text → vermutlich gescannt → Texterkennung
    if len(text.strip()) < 40 * pages and _have("pdftoppm") and _have("tesseract"):
        with tempfile.TemporaryDirectory() as tmp:
            _run(["pdftoppm", "-r", "200", "-gray", "-png", "-l", str(OCR_MAX_PAGES),
                  path, os.path.join(tmp, "p")])
            parts = []
            for f in sorted(os.listdir(tmp)):
                parts.append(_ocr_image(os.path.join(tmp, f)))
            ocr = "\n\f".join(parts)
            if len(ocr.strip()) > len(text.strip()):
                text = ocr
    return text


def _image(path, mime, filename):
    ext = os.path.splitext(filename)[1].lower()
    if mime in ("image/heic", "image/heif") or ext in (".heic", ".heif"):
        conv = shutil.which("heif-dec") or shutil.which("heif-convert")
        if not conv:
            raise Skip("HEIC-Umwandlung nicht installiert")
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "bild.jpg")
            _run([conv, path, out])
            if not os.path.exists(out):  # manche Versionen hängen -1 an
                cands = [f for f in os.listdir(tmp) if f.endswith(".jpg")]
                if not cands:
                    raise RuntimeError("HEIC-Umwandlung ohne Ergebnis")
                out = os.path.join(tmp, cands[0])
            return _ocr_image(out)
    return _ocr_image(path)


def _xml_text(data, para_tags):
    """Text aus Office-XML: Absätze/Zellen als Zeilen."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return ""
    out = []
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag in ("t", "instrText") and el.text:
            out.append(el.text)
        elif tag == "tab":
            out.append("\t")
        elif tag in ("br", "cr"):
            out.append("\n")
        if tag in para_tags:
            out.append("\n")
    # ElementTree liefert Elemente in Dokumentreihenfolge, Absatzumbruch kommt am Anfang des
    # nächsten Absatzes – für die Suche ist das unerheblich.
    return "".join(out)


def _docx(path):
    with zipfile.ZipFile(path) as z:
        names = [n for n in z.namelist() if re.match(
            r"word/(document|header\d*|footer\d*|footnotes|endnotes|comments)\.xml$", n)]
        names.sort(key=lambda n: (n != "word/document.xml", n))
        return "\n".join(_xml_text(z.read(n), {"p"}) for n in names)


def _pptx(path):
    with zipfile.ZipFile(path) as z:
        slides = sorted((n for n in z.namelist() if re.match(r"ppt/(slides/slide|notesSlides/notesSlide)\d+\.xml$", n)),
                        key=lambda n: (("notes" in n), int(re.findall(r"\d+", n)[-1])))
        return "\n\n".join(_xml_text(z.read(n), {"p"}) for n in slides)


def _xlsx(path):
    with zipfile.ZipFile(path) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root:
                shared.append("".join(t.text or "" for t in si.iter() if t.tag.endswith("}t")))
        sheet_names = []
        if "xl/workbook.xml" in z.namelist():
            wb = ET.fromstring(z.read("xl/workbook.xml"))
            sheet_names = [s.get("name") for s in wb.iter() if s.tag.endswith("}sheet")]
        out = []
        sheets = sorted((n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n)),
                        key=lambda n: int(re.findall(r"\d+", n)[-1]))
        for i, n in enumerate(sheets):
            out.append("## " + (sheet_names[i] if i < len(sheet_names) else n))
            root = ET.fromstring(z.read(n))
            for row in root.iter():
                if not row.tag.endswith("}row"):
                    continue
                cells = []
                for c in row:
                    if not c.tag.endswith("}c"):
                        continue
                    v = None
                    for sub in c.iter():
                        if sub.tag.endswith("}v"):
                            v = sub.text
                        elif sub.tag.endswith("}t") and c.get("t") == "inlineStr":
                            v = sub.text
                    if v is None:
                        continue
                    if c.get("t") == "s":
                        try:
                            v = shared[int(v)]
                        except (ValueError, IndexError):
                            pass
                    cells.append(v)
                if cells:
                    out.append(" | ".join(cells))
        return "\n".join(out)


def _odf(path):
    with zipfile.ZipFile(path) as z:
        return _xml_text(z.read("content.xml"), {"p", "h", "table-row"})


def _csv(path):
    with open(path, "rb") as f:
        raw = _decode(f.read(MAX_CHARS * 2))
    try:
        dialect = csv.Sniffer().sniff(raw[:5000], delimiters=",;\t")
        rows = csv.reader(io.StringIO(raw), dialect)
        return "\n".join(" | ".join(r) for r in rows)
    except csv.Error:
        return raw


def _html_to_text(text):
    text = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h\d)>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return html.unescape(text)


def _eml_bytes(raw):
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    head = "\n".join("%s: %s" % (h, msg.get(h, "")) for h in ("From", "To", "Cc", "Date", "Subject")
                     if msg.get(h))
    body = msg.get_body(preferencelist=("plain", "html"))
    try:
        text = body.get_content() if body else ""
    except (LookupError, ValueError):
        text = (body.get_payload(decode=True) or b"").decode("utf-8", "replace") if body else ""
    if body and body.get_content_type() == "text/html":
        text = _html_to_text(text)
    names = [p.get_filename() for p in msg.iter_attachments() if p.get_filename()]
    if names:
        head += "\nAnhänge: " + ", ".join(names)
    return head + "\n\n" + text


def _eml(path):
    with open(path, "rb") as f:
        return _eml_bytes(f.read())


def _msg(path):
    if not _have("msgconvert"):
        raise Skip("Outlook-.msg: msgconvert nicht installiert")
    with tempfile.TemporaryDirectory() as tmp:
        _run(["msgconvert", "--outfile", os.path.join(tmp, "mail.eml"), path], timeout=120)
        with open(os.path.join(tmp, "mail.eml"), "rb") as f:
            return _eml_bytes(f.read())


def _rtf(path):
    with open(path, "rb") as f:
        raw = _decode(f.read())
    raw = re.sub(r"\\'[0-9a-f]{2}", " ", raw)
    raw = re.sub(r"\\[a-z]+-?\d* ?|[{}]", " ", raw)
    return raw


# ─── Einstieg ─────────────────────────────────────────────────────────

AV = re.compile(r"^(audio|video)/")
SCORE_EXT = {".mscz", ".mscx", ".sib", ".gp", ".gpx", ".gp5", ".mid", ".midi", ".musicxml", ".mxl"}


def extract(path, mime, filename):
    mime = (mime or "").lower()
    ext = os.path.splitext(filename or "")[1].lower()
    try:
        if AV.match(mime) or ext in (".mp3", ".m4a", ".wav", ".aif", ".aiff", ".flac", ".ogg",
                                     ".mp4", ".mov", ".m4v", ".avi", ".mkv"):
            raise Skip("Audio/Video – Transkription folgt später")
        if ext in SCORE_EXT:
            raise Skip("Notensatz-Datei – nur Titel/Dateiname durchsuchbar")
        if mime == "application/pdf" or ext == ".pdf":
            text = _pdf(path)
        elif mime.startswith("image/") or ext in (".jpg", ".jpeg", ".png", ".tif", ".tiff",
                                                   ".heic", ".heif", ".webp", ".gif", ".bmp"):
            text = _image(path, mime, filename)
        elif ext in (".docx", ".docm", ".dotx"):
            text = _docx(path)
        elif ext in (".xlsx", ".xlsm"):
            text = _xlsx(path)
        elif ext in (".pptx",):
            text = _pptx(path)
        elif ext in (".odt", ".ods", ".odp"):
            text = _odf(path)
        elif ext == ".doc":
            if not _have("catdoc"):
                raise Skip("catdoc nicht installiert")
            text = _decode(_run(["catdoc", "-d", "utf-8", path]))
        elif ext == ".xls":
            if not _have("xls2csv"):
                raise Skip("xls2csv nicht installiert")
            text = _decode(_run(["xls2csv", "-d", "utf-8", path]))
        elif ext in (".csv", ".tsv"):
            text = _csv(path)
        elif ext == ".eml" or mime == "message/rfc822":
            text = _eml(path)
        elif ext == ".msg" or mime == "application/vnd.ms-outlook":
            text = _msg(path)
        elif ext == ".rtf":
            text = _rtf(path)
        elif mime.startswith("text/") or ext in (".txt", ".md", ".json", ".xml", ".html", ".htm"):
            with open(path, "rb") as f:
                text = _decode(f.read(MAX_CHARS * 2))
            if ext in (".html", ".htm") or mime == "text/html":
                text = _html_to_text(text)
        else:
            raise Skip("Dateityp wird nicht ausgelesen")
    except Skip as e:
        return "skipped", None, str(e)
    except zipfile.BadZipFile:
        return "failed", None, "Datei beschädigt oder kein Office-Format"
    except subprocess.TimeoutExpired:
        return "failed", None, "Zeitüberschreitung beim Auslesen"
    except Exception as e:  # noqa: BLE001 – Fehler eines Dokuments soll den Rest nicht stoppen
        return "failed", None, "%s: %s" % (type(e).__name__, str(e)[:300])
    text = _clean(text or "")
    if not text:
        return "done", "", "kein Text gefunden"
    return "done", text, None


def chunk(text, size=1200, overlap=150, max_chunks=400):
    """Text in überlappende Abschnitte teilen, bevorzugt an Absatz-/Satzgrenzen."""
    text = text.strip()
    if not text:
        return []
    out, start, n = [], 0, len(text)
    while start < n and len(out) < max_chunks:
        end = min(n, start + size)
        if end < n:
            window = text[start:end]
            cut = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind(". "))
            if cut > size * 0.5:
                end = start + cut + 1
        piece = text[start:end].strip()
        if piece:
            out.append(piece)
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return out
