# Outlook-Anbindung (Etappe 5, Schritt 1)

Das Cockpit liest Mails über eine **eigene Microsoft-App „Roberts Cockpit“** – nur mit
Leserecht. Senden, Löschen, Verschieben oder Markieren ist mit diesen Berechtigungen
technisch nicht möglich.

## 1. App registrieren (einmalig, ca. 5 Minuten)

1. <https://entra.microsoft.com> öffnen und mit dem Microsoft-Konto anmelden, dessen
   Mails ins Cockpit sollen (bzw. als Administrator des Getintune-Mandanten).
2. **Identität → Anwendungen → App-Registrierungen → Neue Registrierung**
   - Name: `Roberts Cockpit`
   - Unterstützte Kontotypen: *Nur Konten in diesem Organisationsverzeichnis*
     (bei privatem Outlook.com-Konto: *… und persönliche Microsoft-Konten*)
   - Umleitungs-URI: leer lassen → **Registrieren**
3. Auf der Übersichtsseite notieren:
   - **Anwendungs-ID (Client-ID)**
   - **Verzeichnis-ID (Mandanten-ID)**
4. **Authentifizierung** → ganz unten *Öffentliche Clientflows zulassen* → **Ja** → Speichern.
   (Nötig für die Anmeldung per Gerätecode; die App hat dadurch kein Geheimnis/Passwort.)
5. **API-Berechtigungen** → *Berechtigung hinzufügen* → Microsoft Graph →
   **Delegierte Berechtigungen**: `Mail.Read`, `offline_access`, `User.Read`
   (`User.Read` ist meist schon da). Keine weiteren Berechtigungen hinzufügen.
   Falls angeboten: *Administratorzustimmung erteilen*.

## 2. Server einrichten

```bash
ssh root@<server-ip>
cd /opt/roberts-cockpit
# Sicherung der Datenbank vor jeder Umstellung
sudo -u cockpit mkdir -p backups
sudo -u cockpit python3 -c "import sqlite3,time;sqlite3.connect('cockpit.db').backup(sqlite3.connect('backups/cockpit-%s.db'%time.strftime('%Y%m%d-%H%M')))"
git pull
apt install -y libemail-outlook-message-perl     # msgconvert: Outlook-.msg-Dateien lesen
cat >> .env <<'EOF'
MS_CLIENT_ID=<Anwendungs-ID>
MS_TENANT=<Mandanten-ID>
EOF
systemctl restart roberts-cockpit cockpit-worker
```

`MS_TENANT` darf auch `common` (Standard) oder `organizations` sein; mit der Mandanten-ID
ist es am eindeutigsten.

## 3. Verbinden

Im Cockpit oben **✉️ Outlook → Verbinden**. Es erscheint ein Code: auf
<https://microsoft.com/devicelogin> eingeben, anmelden, zustimmen – fertig.

Alternativ auf dem Server:

```bash
cd /opt/roberts-cockpit
sudo -u cockpit bash -c 'set -a; . ./.env; python3 msgraph.py --anmelden'   # --status, --abmelden
```

Das Refresh-Token liegt in der Datenbank (Tabelle `meta`, Schlüssel `ms_auth`) und ist nur
für den Nutzer `cockpit` lesbar. Trennen: **✉️ Outlook → Trennen**; zusätzlich kann die
Zustimmung unter <https://myapps.microsoft.com> bzw. in Entra widerrufen werden.

## Benutzen

- **Im Cockpit:** Karte öffnen → *✉️ Mail aus Outlook* → suchen → *Anhängen*.
- **Per Claude / Sprache:** „Häng die Mail von Frau Müller zum Zeugnis an die Karte Zeugnisse.“
  → Werkzeug `mail_anhaengen` (sucht, fragt bei mehreren Treffern nach, hängt an).
  Nach dem Update in claude.ai den Connector einmal neu laden, damit das Werkzeug erscheint.
- **Drag & Drop:** .eml- oder .msg-Dateien auf eine Karte im Board oder in den
  Materialien-Bereich ziehen.

Jede Mail wird gespeichert als
- **Original** (.eml bzw. .msg) – der Text wird ausgelesen und ist durchsuchbar,
- **Anhänge** als eigene Materialien (PDF, Word, Fotos … mit Texterkennung);
  kleine eingebettete Bilder (Logos in Signaturen) werden übersprungen.

**Kein doppelter Import:** Erkennungsmerkmal ist die Message-ID der Mail. Dieselbe Mail
noch einmal (auch per Drag & Drop statt aus Outlook) wird nur mit der Karte verknüpft.
Gleiche Anhänge (gleicher Inhalt) werden ohnehin nur einmal gespeichert.

## Protokoll

Alle Mail-Aktionen stehen in `/api/activity` (`mail_importieren`, `mail_verknuepfen`,
`outlook_verbinden`, `outlook_abmelden`), mit Akteur `robert` bzw. `claude`.
