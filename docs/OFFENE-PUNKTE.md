# Offene Punkte – Stand 11.11.2026 (Ende der Sitzung)

Letzter Stand auf dem Server: Commit `7c4f949` (cockpit.robert-beckert.de).
Letzte DB-Sicherung: `backups/cockpit-2026-11-11-…-vor-erledigt-datum.db` (280 Karten, ok).

## Zum Testen (von Robert, noch nicht rückgemeldet)
- [ ] Erledigt-Datum: Kalender am Mac (Pfeiltasten, Enter, Esc, Mausrad am Jahr) und am Samsung (Wischen auf der Jahreszahl).
- [ ] Spalte „Erledigt“ nach Datum absteigend; alte erledigte Karten ohne Datum stehen unten → beim Öffnen echtes Datum eintragen oder Feld leeren.
- [ ] Claude öffnet Karten (`karte_oeffnen`): Connector in claude.ai neu laden, dann „öffne die Karte … im Cockpit“.
- [ ] Karten-Dialog (Kopfzeile, ✕/Esc mit Rückfrage, Notiz-Popup, zugeklappte Listen, Notiz-Vorschau bei Hover/langem Tippen).
- [ ] Karte c19 „Assessment-System ausbauen“: Claude meldet „Später“, im Board unter Eingang? Spalte prüfen.

## Neu am 11.11. abends: Upload ohne Karte + OneDrive-Scan-Eingang
- [ ] Robert: „✉️ Outlook“ → Trennen → Verbinden (holt Files.ReadWrite ins Token), dann Hinweis in 📚 weg?
- [ ] Testscan in „AAA Cockpit-Eingang“ → nach ≤ 10 Min. in 📚 (Quelle onedrive-scan), Datei in „übernommen“.
- [ ] 📚 Upload testen auf Mac (Ziehen), Windows, iPad (Ziehen aus Dateien-App), iPhone (Knopf → Datei auswählen → ⋯ → Dokumente scannen).
- [ ] Später (C): Teilen-Menü am iPhone (PWA share target) → Bibliothek. Hinweis: iOS-Safari unterstützt
      share_target für Web-Apps derzeit nicht; auf Android geht es. Für das iPhone ggf. Kurzbefehl (Shortcuts) → /api/import/file.

## Entscheidungen, die Robert treffen muss
- [ ] **Claude im Cockpit (Variante A)**: eigenes Claude-Fenster im Cockpit über die API (ohne App-Wechsel, ohne Zusatztipp).
  Kosten ca. 1–3 Cent pro Frage mit Sonnet 5.5; Monatsgrenze z. B. 10 €. Braucht API-Schlüssel von console.anthropic.com
  (Robert trägt ihn selbst in /opt/roberts-cockpit/.env ein). Bisher: Teilen-Menü am Handy funktioniert, Rest ist Handarbeit.
- [ ] **Gmail milodecardio@googlemail.com**: Weg wählen – (a) eigene Google-App mit `gmail.readonly` (empfohlen) oder (b) IMAP mit App-Passwort.
- [ ] Reihenfolge der nächsten Bausteine (A / Postfächer / Android-Hülle).

## Weitere Postfächer anbinden
- [ ] gitarre@, tenor@, ebay@robert-beckert.de: Art klären (freigegebenes Postfach → `Mail.Read.Shared`; eigenes Benutzerkonto → zweite Anmeldung; Gruppe → anderer Weg).
  Dafür Screenshot aus dem Microsoft 365 Admin Center (Benutzer bzw. „Freigegebene Postfächer“).
- [ ] Danach: Suche und `mail_anhaengen` über alle Postfächer; Feld „Postfach“ in mail_meta ist schon vorhanden.
- [ ] Gmail nach Entscheidung oben.

## Geplante Bausteine
- [ ] **Etappe 5, Schritt 3**: Outlook-Ordner „Cockpit“ automatisch übernehmen, für neue Mails Karten vorschlagen.
- [ ] **Android-Hülle** (Samsung Handy/Tablet): Cockpit + claude.ai in einer App, Teilung 100 / 75:25 / 50:50 / 25:75 umschaltbar,
  hoch (oben/unten) und quer (links/rechts); Teilen-Ziel „an Cockpit“. Google-Login geht im WebView nicht → E-Mail-Code.
- [ ] Ansicht „Bewerbungen“ – erst nach Abstimmung mit der Sitzung des Bewerbungs-Agenten.

## Kleinere Verbesserungen (bekannt)
- [ ] Bibliothek: doppelte Titel (z. B. bei „Führerschein“) zusammenführen bzw. Dubletten erkennen.
- [ ] Alte erledigte Karten einmalig mit Datum versehen (falls Robert die Daten kennt), sonst bleiben sie unten.
- [ ] Oberfläche wurde nur per Syntaxprüfung getestet, nicht automatisiert im Browser.

## Regeln (bleiben)
- `agent_api.py` und `/agent-api/…` gehören zum Bewerbungs-Agenten (andere Sitzung): nicht ändern, Sicherheitsregeln erhalten.
- `/opt/bewerbungs-agent` auf dem Server nicht anfassen.
- Vor jeder Umstellung die Datenbank sichern (Ablauf in docs/OUTLOOK.md bzw. Deploy-Notiz); Sicherungen `chmod 600`.
