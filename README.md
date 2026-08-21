# Roberts Cockpit

Kanban-Board zur Steuerung & Priorisierung. Läuft als kleiner Python-Server
(nur Standardbibliothek, keine Abhängigkeiten) und liefert eine installierbare
PWA aus, damit die App auf Handy und Notebook wie eine eigenständige App
aussieht.

## Architektur

- **`server.py`** – Serviert `cockpit.html` + PWA-Dateien, speichert den
  Board-Zustand in `cockpit_state.json`. Alle Geräte, die dieselbe URL
  öffnen, sehen denselben Stand (Polling alle 10s + sofortiges Speichern bei
  jeder Änderung).
- **PWA** – `manifest.json` + `service-worker.js` + `icons/` machen die App
  "installierbar": Browser → "Zum Home-Bildschirm hinzufügen" (iPhone) bzw.
  "App installieren" (Chrome/Edge auf Notebook/Android) → startet danach ohne
  Browser-Chrome wie eine eigenständige App.
- **Basic-Auth** – Wenn `COCKPIT_USER`/`COCKPIT_PASS` gesetzt sind, verlangt
  der Server einen Login (Browser fragt einmalig danach, merkt es sich dann).

## Lokal starten (Entwicklung/Test)

```bash
python3 server.py
```

Öffnet automatisch `http://127.0.0.1:8077`. Ohne `COCKPIT_USER`/`COCKPIT_PASS`
läuft es ohne Login (nur für lokale Nutzung gedacht).

## Produktivbetrieb auf einem VPS

### 1. Hetzner-Server anlegen

1. Account auf [hetzner.com/cloud](https://www.hetzner.com/cloud) anlegen.
2. Neues Projekt → "Server hinzufügen".
3. Standort egal (z.B. Nürnberg/Falkenstein), **Image: Ubuntu 24.04**,
   **Typ: CX22** (günstigste Stufe reicht völlig für dieses Board).
4. SSH-Key hinzufügen (falls noch keiner hinterlegt: auf deinem Mac
   `ssh-keygen -t ed25519` ausführen, dann den Inhalt von
   `~/.ssh/id_ed25519.pub` bei Hetzner einfügen).
5. Server erstellen → du bekommst eine öffentliche IP-Adresse.

### 2. DNS-Eintrag anlegen

Im Kundenkonto deines Domain-Anbieters für `robert-beckert.de` (dort, wo du
die Nameserver/DNS-Einträge verwaltest – **nicht** zwingend derselbe Anbieter
wie dein E-Mail-Postfach) einen **A-Eintrag** anlegen:

| Typ | Name | Wert |
|---|---|---|
| A | `cockpit` | die IP-Adresse deines Hetzner-Servers |

Ergebnis: `cockpit.robert-beckert.de` zeigt auf den Server. Kann je nach
Anbieter bis zu ein paar Stunden dauern, bis es weltweit wirkt.

> Hinweis: `robert-beckert.de` läuft aktuell über die Nameserver
> `ns1/ns2.dns-parking.com` – das ist eine generische "Domain geparkt"-DNS,
> wie sie viele Registrare als Vorbelegung nutzen. Falls du dich nicht mehr
> erinnerst, bei welchem Registrar du die Domain hast: in der
> Bestätigungs-E-Mail vom Domain-Kauf nachsehen, oder bei üblichen deutschen
> Anbietern (IONOS, Strato, INWX, united-domains, netcup, Checkdomain)
> nacheinander mit deiner E-Mail-Adresse einloggen.

### 3. Server einrichten

Per SSH auf den neuen Server verbinden:

```bash
ssh root@<server-ip>
```

Caddy installieren (übernimmt Reverse-Proxy + automatisches HTTPS-Zertifikat):

```bash
apt update && apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list
apt update && apt install -y caddy
```

Eigenen Nutzer für die App anlegen und Repo klonen:

```bash
useradd -r -s /usr/sbin/nologin cockpit
mkdir -p /opt/roberts-cockpit
git clone https://github.com/milodecardio/roberts-cockpit.git /opt/roberts-cockpit
```

`.env` mit Zugangsdaten anlegen (Passwort selbst wählen, z.B. mit
`openssl rand -base64 18`):

```bash
cat > /opt/roberts-cockpit/.env <<'EOF'
COCKPIT_USER=robert
COCKPIT_PASS=<dein-passwort-hier>
EOF
chmod 600 /opt/roberts-cockpit/.env
chown -R cockpit:cockpit /opt/roberts-cockpit
```

systemd-Service einrichten:

```bash
cp /opt/roberts-cockpit/deploy/roberts-cockpit.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now roberts-cockpit
systemctl status roberts-cockpit   # sollte "active (running)" zeigen
```

Caddy konfigurieren:

```bash
cp /opt/roberts-cockpit/deploy/Caddyfile /etc/caddy/Caddyfile
systemctl reload caddy
```

Firewall (nur SSH, HTTP, HTTPS erlauben):

```bash
ufw allow 22
ufw allow 80
ufw allow 443
ufw enable
```

Nach ein paar Minuten (sobald DNS wirkt) ist die App unter
**`https://cockpit.robert-beckert.de`** erreichbar, mit gültigem
HTTPS-Zertifikat (Caddy holt es automatisch von Let's Encrypt) und
Basic-Auth-Login.

### 4. Als App installieren

- **iPhone/iPad:** Safari → URL öffnen, einloggen → Teilen-Button →
  "Zum Home-Bildschirm". Danach startet die App ohne Safari-Oberfläche.
- **Mac/Windows-Notebook (Chrome/Edge):** URL öffnen, einloggen →
  Adressleiste → App-Installieren-Symbol (oder Menü → "App installieren").
  Startet danach als eigenes Fenster ohne Browser-Chrome, per Dock/Startmenü.
- **Android:** Chrome → URL öffnen → Menü → "App installieren".

### Updates ausrollen

```bash
ssh root@<server-ip>
cd /opt/roberts-cockpit
git pull
systemctl restart roberts-cockpit
```

## Datenschutz

Falls hier künftig personenbezogene Daten (Schüler-/Kundendaten) landen:
gleiche Überlegung wie bei symphony-studio – rechtliche Prüfung vor
produktivem Einsatz, nicht nur technische.
