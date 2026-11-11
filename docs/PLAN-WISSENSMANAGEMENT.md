# Plan: Cockpit als Wissensmanagement

Stand: 07.11.2026 · Entscheidungen von Robert in diesem Dokument festgehalten.

## Ziel

Das Cockpit wird zum zentralen Wissens- und Aufgabensystem: Zu jeder offenen
und erledigten Aufgabe gibt es Verweise auf die zugehörigen Materialien. Alle
Materialien sind nach **Bedeutung** durchsuchbar. Claude dient als Assistent,
Perplexity für öffentliche Web-Recherche.

## Entscheidungen

| Thema | Entscheidung |
|---|---|
| Server | Bleibt vorerst **Hetzner CX22** (2 vCPU, 4 GB RAM, 40 GB), Nürnberg, mit Hetzner-Backups. Kein GPU-/Großserver. |
| Vektorisieren | **Lokal auf dem Server** mit Ollama + **bge-m3** (getestet 07.11.: deutsch gut, ~0,4 s pro Karte, max. ~1,5 GB RAM). |
| Sprachmodell | **Claude** (Robert hat **Claude Pro**). Kein lokales Sprachmodell. |
| Recherche | **Perplexity** nur für öffentliche Fragen; private Inhalte gehen nicht dorthin. |
| Datenschutz | Claude **darf alle Bereiche lesen**, auch B4 Medizin und B5 Pflege Mutter. |
| Quellen | Mac, OneDrive, Outlook. Papier wird später digitalisiert (Scans). |
| Dateitypen | PDF, Word, Excel, Fotos, Scans, Noten, Video, Audio. |

## Architektur

```
 Mac ─────────┐
 OneDrive ────┤                    Hetzner-Server
 Outlook ─────┼──▶ Einlesen ──▶ ┌─────────────────────────────┐
 Scans/Handy ─┘   (Text holen,  │ SQLite: Karten, Dokumente,  │ ◀──▶ Cockpit (Browser/App)
                  OCR, Trans-   │         Verweise, Abschnitte│
                  kription)     │ Dateien (später Storage Box)│
                                │ Ollama + bge-m3 (Vektoren)  │ ◀──▶ Claude (MCP-Connector)
                                └─────────────────────────────┘
                                                                ──▶ Perplexity (Web-Recherche)
```

## Etappen

### Etappe 1 – Fundament: SQLite statt JSON-Datei ✅ live seit 07.11.2026
- Karten liegen in `cockpit.db` (SQLite) statt in `cockpit_state.json`.
- Die Schnittstelle `/state` bleibt gleich → im Cockpit ändert sich nichts.
- Gelöschte Karten werden nur als gelöscht markiert (bleiben fürs Wissensarchiv erhalten).
- Übergangsweise wird weiterhin `cockpit_state.json` mitgeschrieben (einfacher Rückweg).

### Etappe 2 – Materialien an Karten ✅ live seit 07.11.2026
Umgesetzt: Dateien (Upload, Foto/Scan per Handy-Kamera, Drag&Drop), Links, Notizen;
📎-Zähler auf Karten; Suche findet Karten auch über Titel/Notizen/Links ihrer Materialien.
Dateien liegen nach Inhalt (SHA-256) in `files/` – gleiche Datei wird nur einmal gespeichert.
Gelöste Materialien bleiben im Archiv. Text *in* Dateien wird erst in Etappe 3 durchsuchbar.
Noch offen: Material an weitere Karte hängen (außer durch erneuten Upload), Notizen bearbeiten.
- Dateien hochladen (Browser, Handy-Kamera), Links und Notizen an Karten hängen.
- Tabellen `documents` und `card_documents` (eine Datei kann zu mehreren Karten gehören).
- Volltextsuche (SQLite FTS5) über Karten und Dokumente.
- Ab einigen GB Dateien: **Hetzner Storage Box** (1 TB) einbinden – 40 GB reichen nicht für Video/Audio.

### Etappe 3 – Text gewinnen und vektorisieren ✅ live seit 07.11.2026
Umgesetzt: `extract.py` (PDF inkl. OCR für Scans, Word/Excel/PowerPoint, ODF, alte .doc/.xls, CSV, E-Mail,
Fotos inkl. HEIC per OCR), `worker.py` als Dienst `cockpit-worker` (niedrige Priorität, max. 1,2 GB RAM),
Vektoren mit bge-m3 für Materialien und Karten, Bedeutungssuche (`/api/semantic`), „Passend dazu“ in Karten,
📚 Bibliothek (Dateien ohne Karte, Filter nach Quelle/Status, an Karte hängen).
Import vom Mac/OneDrive: `scripts/cockpit_import.py` (Probelauf, Schlüsselbund, überspringt Unverändertes).
Jede Datei wird in Text umgewandelt, in Abschnitte geteilt und mit bge-m3 vektorisiert:

| Typ | Weg zum Text | Hinweis |
|---|---|---|
| PDF, Word, Excel | direkt auslesen | schnell |
| Scans, Fotos | OCR (Tesseract, deutsch) | auf dem Server machbar |
| Audio, Video | Transkription (Whisper) | **auf dem CX22 sehr langsam** → auf dem Mac transkribieren oder nachts in kleinen Mengen |
| Noten | Titel, Komponist, Notizen, ggf. Text aus PDF | Notenbild selbst ist nicht sinnvoll durchsuchbar |

- „Ähnliche Materialien“ direkt an jeder Karte.
- Suche nach Bedeutung über alles.

### Etappe 4 – Claude als Assistent ✅ live (07.11.2026)
- Adresse für den Connector: `https://cockpit.robert-beckert.de/mcp`
- Werkzeuge: cockpit_uebersicht, suchen, karten_auflisten, karte_lesen, dokument_lesen,
  bibliothek, karte_anlegen, karte_aendern, material_verknuepfen, notiz_anhaengen (kein Löschen).
- Jede Änderung durch Claude steht in der Tabelle `activity` (`/api/activity`).
- Verbindungen anzeigen/trennen: `cd /opt/roberts-cockpit && sudo -u cockpit python3 oauth.py --liste|--trennen`
- Konfliktschutz: Frontend speichert mit `_base`; veralteter Stand → 409 → feldweises Zusammenführen.
- Perplexity: Knopf „🔎 Recherchieren“ in der Karte (Frage vor dem Absenden editierbar).

Ursprünglicher Plan:
- **MCP-Server** auf dem Hetzner-Server, in claude.ai als *eigener Connector* eingebunden
  (funktioniert mit Claude Pro, Nutzung zählt zu den Pro-Limits, keine API-Kosten).
- Werkzeuge für Claude: Wissen durchsuchen, Karten lesen/anlegen/verschieben,
  Dokumente lesen.
- Anmeldung des Connectors per OAuth (Basic-Auth wird von claude.ai nicht unterstützt).
- Perplexity: Button „Recherchieren“ an Karten (öffnet Perplexity mit vorformulierter Frage).

### Etappe 5 – Automatisch einlesen
Reihenfolge (festgelegt 11.11.2026):
1. ✅ gebaut (11.11.2026) – **Outlook lesen:** eigene Microsoft-App „Roberts Cockpit“, nur
   Mail.Read/offline_access/User.Read, Anmeldung per Gerätecode (`msgraph.py`).
   Mail-Import mit Original, Text und Anhängen, ohne doppelten Import (Message-ID) – `mail.py`.
   Im Cockpit: ✉️ Outlook (verbinden), Karte → „✉️ Mail aus Outlook“.
   MCP-Werkzeug `mail_anhaengen` für die Sprachsteuerung. Einrichtung: `docs/OUTLOOK.md`.
2. ✅ gebaut (11.11.2026) – **Drag & Drop** von Dateien, Mails (.eml/.msg) und Links direkt auf
   Karten im Board. Mails gehen denselben Weg wie aus Outlook (Anhänge einzeln, keine Duplikate).
3. Später: Outlook-Ordner „Cockpit“ automatisch übernehmen und Karten für neue Mails vorschlagen.

Ursprüngliche Idee:
- **OneDrive + Outlook:** über Microsoft Graph (App-Registrierung im Microsoft-365-Konto).
  Z. B. ein OneDrive-Ordner „Cockpit“ und eine Outlook-Kategorie/Ordner „Cockpit“.
- **Mac:** Ordner, der automatisch auf den Server synchronisiert wird.
- **Papier:** Scan-App am Handy → Upload ins Cockpit.

## Datenschutz-Hinweise
- Gesundheitsdaten gehen bei Fragen an Claude in die USA (Anthropic). Robert hat das freigegeben.
- **In claude.ai → Einstellungen → Datenschutz** prüfen, dass Chats **nicht zum Training**
  verwendet werden („Help improve Claude“ aus).
- Vektorisieren und Speicherung bleiben komplett auf dem eigenen Server.
- Das GitHub-Repo ist öffentlich → Datenbank, Dateien und `.env` nie ins Repo.
