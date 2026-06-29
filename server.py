#!/usr/bin/env python3
"""Lokaler Launcher für Roberts Cockpit.
Serviert cockpit.html, speichert/lädt cockpit_state.json und bietet Git-Sync-Routen.
Nur Python-Bordmittel, keine Installation nötig. macOS."""
import http.server, socketserver, subprocess, os, webbrowser, urllib.parse, json, socket

HERE  = os.path.dirname(os.path.abspath(__file__))
HTML  = os.path.join(HERE, "cockpit.html")
STATE = os.path.join(HERE, "cockpit_state.json")
PORT  = 8077
URL   = "http://127.0.0.1:%d/" % PORT

def run_tool(tool):
    try:
        if tool == "claude":
            subprocess.Popen(["open", "-a", "Claude"])
            return "Claude wird gestartet…"
        if tool == "chatgpt":
            subprocess.Popen(["open", "https://chatgpt.com"])
            return "ChatGPT (Web) wird geöffnet…"
        if tool == "claudecode":
            cmd = 'cd ~/AI/"claude code"/generated-agents/mail-crm-agent 2>/dev/null || cd ~/AI; clear; claude'
            osa = 'tell application "Terminal"\nactivate\ndo script "%s"\nend tell' % cmd
            subprocess.Popen(["osascript", "-e", osa])
            return "Claude Code im Terminal wird gestartet…"
        if tool == "mailcrm":
            app = os.path.expanduser("~/AI/claude code/generated-agents/mail-crm-agent/MailCRM.app")
            if os.path.exists(app):
                subprocess.Popen(["open", app])
                return "MailCRM.app wird gestartet…"
            subprocess.Popen(["open", "-a", "MailCRM"])
            return "Versuche MailCRM zu starten…"
        return "Unbekanntes Tool: %s" % tool
    except Exception as e:
        return "Fehler beim Start: %s" % e

def git(args, timeout=20):
    return subprocess.run(["git", "-C", HERE] + args,
                          capture_output=True, text=True, timeout=timeout)

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, ctype, data):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, "application/json", json.dumps(obj).encode())

    # ── POST ────────────────────────────────────────────────
    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/state":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                with open(STATE, "wb") as f:
                    f.write(body)
                self._json(200, {"ok": True})
            except Exception as e:
                self._json(500, {"error": str(e)})
        else:
            self._send(404, "text/plain", b"not found")

    # ── GET ─────────────────────────────────────────────────
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        p = u.path

        if p in ("/", "/index.html"):
            try:
                with open(HTML, "rb") as f:
                    self._send(200, "text/html; charset=utf-8", f.read())
            except Exception as e:
                self._send(500, "text/plain; charset=utf-8", str(e).encode())

        elif p == "/launch":
            tool = urllib.parse.parse_qs(u.query).get("tool", [""])[0]
            self._json(200, {"ok": True, "msg": run_tool(tool)})

        elif p == "/state":
            try:
                with open(STATE, "rb") as f:
                    self._send(200, "application/json", f.read())
            except FileNotFoundError:
                self._send(404, "application/json", b"null")

        elif p == "/check_sync":
            try:
                git(["fetch"], timeout=10)
                ahead  = git(["rev-list", "--count", "origin/main..HEAD"]).stdout.strip()
                behind = git(["rev-list", "--count", "HEAD..origin/main"]).stdout.strip()
                self._json(200, {"ahead": int(ahead or 0), "behind": int(behind or 0)})
            except Exception as e:
                self._json(200, {"error": str(e)})

        elif p == "/do_pull":
            try:
                r = git(["pull"], timeout=30)
                self._json(200, {"ok": True, "msg": r.stdout.strip()})
            except Exception as e:
                self._json(200, {"ok": False, "error": str(e)})

        elif p == "/do_push":
            try:
                git(["add", "cockpit_state.json"])
                commit = git(["commit", "-m", "cockpit: sync state"])
                if "nothing to commit" in (commit.stdout + commit.stderr):
                    self._json(200, {"ok": True, "msg": "Keine Änderungen"})
                    return
                push = git(["push"], timeout=30)
                ok = push.returncode == 0
                self._json(200, {"ok": ok, "msg": "Push OK" if ok else push.stderr.strip()})
            except Exception as e:
                self._json(200, {"ok": False, "error": str(e)})

        else:
            self._send(404, "text/plain", b"not found")


def port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0

def main():
    if port_in_use(PORT):
        webbrowser.open(URL)
        return
    socketserver.TCPServer.allow_reuse_address = True
    httpd = socketserver.TCPServer(("127.0.0.1", PORT), Handler)
    webbrowser.open(URL)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()

if __name__ == "__main__":
    main()
