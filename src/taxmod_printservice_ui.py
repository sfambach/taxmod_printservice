"""The settings page of the print service (D-1332): a small web page on http://127.0.0.1:8631, for this computer only.

It runs as its own process, started from the menu, so «Update» can ask for the password in the desktop's dialog. It reads
what the service writes (status file, journal), changes config.json and restarts the service. Every request must carry the
session secret the launcher put into the URL, and every change must come from the page itself (Origin), so no other web
page in the browser can print, pair or update through it.
"""

from __future__ import annotations

import http.server
import json
import os
import secrets
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

import taxmod_print_agent as agent

HOST = "127.0.0.1"
PORT = 8631
ORIGINS = {f"http://{HOST}:{PORT}", f"http://localhost:{PORT}"}
UNIT = "taxmod-printservice.service"
SESSION_FILE = "~/.local/state/taxmod-printservice/ui.json"
TEST_PAGE = "/usr/share/cups/data/testprint"
# What the page may change in config.json; everything else (printers, formats) lives in the Taxmod model.
SETTINGS = {
    "retries": (0, 10),
    "retry_seconds": (1, 300),
    "watch_seconds": (60, 3600),
    "wake_seconds": (60, 3600),
}


class Page:
    """What the handler needs, in one place, so tests can give it a fake runner and paths."""

    def __init__(self, config_path: str, run=subprocess.run, fetch=urllib.request.urlopen, update=None):
        self.config_path = os.path.expanduser(config_path)
        self.run = run
        self.fetch = fetch
        self.update = update
        self.secret = secrets.token_urlsafe(24)
        self.newest = ("", "")
        self.newest_at = 0.0

    # ---- reading -------------------------------------------------------------------------------------------------
    def config(self) -> dict:
        try:
            with open(self.config_path, encoding="utf-8") as handle:
                config = json.load(handle)
        except (OSError, ValueError):
            config = {}
        config.pop("token", None)
        return config

    def paired(self) -> bool:
        folder = os.path.dirname(self.config_path)
        return bool(self.config().get("rest")) and (os.path.exists(os.path.join(folder, agent.TOKEN_FILE)) or self.raw_token_in_config())

    def raw_token_in_config(self) -> bool:
        try:
            with open(self.config_path, encoding="utf-8") as handle:
                return bool(json.load(handle).get("token"))
        except (OSError, ValueError):
            return False

    def quietly(self, command: list[str]) -> subprocess.CompletedProcess:
        """A command for the page; a missing tool shows as empty output, never as a broken page."""
        try:
            return self.run(command, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as error:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr=str(error))

    def printers(self) -> list[dict]:
        done = self.quietly(["lpstat", "-p"])
        printers = []
        for line in (done.stdout or "").splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[0] in ("printer", "Drucker"):
                state = "disabled" if "disabled" in line or "deaktiviert" in line else ("printing" if "printing" in line or "druckt" in line else "idle")
                printers.append({"queue": parts[1], "state": state, "line": line.strip()})
        return printers

    def service_state(self) -> str:
        done = self.quietly(["systemctl", "--user", "is-active", UNIT])
        return (done.stdout or "").strip() or "unknown"

    def status(self) -> dict:
        try:
            with open(os.path.expanduser(agent.STATUS_FILE), encoding="utf-8") as handle:
                state = json.load(handle)
        except (OSError, ValueError):
            state = {}
        config = self.config()
        return {
            "version": agent.VERSION.rsplit(" ", 1)[-1],
            "host": socket.gethostname(),
            "service": self.service_state(),
            "paired": self.paired(),
            "site": config.get("rest", ""),
            "settings": {key: config.get(key, default) for key, default in (("retries", 3), ("retry_seconds", 15), ("watch_seconds", 900), ("wake_seconds", 300))},
            "last_pull": state.get("last_pull", ""),
            "last_error": state.get("last_error", ""),
            "jobs": state.get("jobs", []),
            "printers": self.printers(),
            "newest": self.newest_release(),
        }

    def newest_release(self) -> str:
        """The newest release tag, looked up at most once an hour."""
        if time.monotonic() - self.newest_at > 3600 or not self.newest_at:
            try:
                import taxmod_printservice as app
                self.newest = app.newest_release(self.fetch)
            except (urllib.error.URLError, OSError, ValueError):
                self.newest = ("", "")
            self.newest_at = time.monotonic()
        return self.newest[0]

    def log(self) -> str:
        done = self.quietly(["journalctl", "--user", "-u", UNIT, "-n", "200", "--no-pager", "-o", "short-iso"])
        return done.stdout or done.stderr or ""

    # ---- acting --------------------------------------------------------------------------------------------------
    def write_config(self, config: dict) -> None:
        descriptor = os.open(self.config_path + ".new", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(config, handle, indent=2, ensure_ascii=False)
        os.replace(self.config_path + ".new", self.config_path)

    def restart(self) -> str:
        self.run(["systemctl", "--user", "restart", UNIT], capture_output=True, text=True, timeout=60)
        return "Druckdienst neu gestartet."

    def save_settings(self, body: dict) -> str:
        config = self.config_with_token_kept()
        for key, (low, high) in SETTINGS.items():
            if key in body:
                value = int(body[key])
                if not low <= value <= high:
                    raise ValueError(f"{key}: {low} bis {high}")
                config[key] = value
        self.write_config(config)
        return "Gespeichert. " + self.restart()

    def config_with_token_kept(self) -> dict:
        try:
            with open(self.config_path, encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return {}

    def pair(self, body: dict) -> str:
        """D-1332: the one-time code from Taxmod › Configuration › Printing brings the access; the token goes sealed to disk."""
        site = str(body.get("site", "")).strip().rstrip("/")
        code = str(body.get("code", "")).strip().upper()
        if not site.startswith("https://") or not code:
            raise ValueError("Website (https://…) und Kopplungscode angeben.")
        request = urllib.request.Request(
            site + "/wp-json/taxmod/v1/print/pair",
            data=json.dumps({"code": code, "host": socket.gethostname()}).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": agent.VERSION},
            method="POST",
        )
        try:
            with self.fetch(request, timeout=30) as response:
                access = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            raise ValueError("Kopplung abgelehnt: Code falsch, abgelaufen oder schon benutzt.") from error
        for key in ("rest", "token"):
            if not access.get(key):
                raise ValueError("Die Website hat keinen Zugang geschickt.")
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        agent.save_token(os.path.dirname(self.config_path), str(access["token"]))
        config = self.config_with_token_kept()
        config.pop("token", None)
        config.update({key: str(access.get(key, "")) for key in ("rest", "ntfy", "wake_url")})
        self.write_config(config)
        self.run(["systemctl", "--user", "enable", UNIT], capture_output=True, text=True, timeout=60)
        return f"Gekoppelt als «{access.get('name') or socket.gethostname()}». " + self.restart()

    def test_print(self, body: dict) -> str:
        queue = agent.plain(str(body.get("queue", "")), "queue")
        if not os.path.exists(TEST_PAGE):
            raise ValueError("Keine CUPS-Testseite auf diesem Rechner.")
        done = self.run(["lp", "-d", queue, "-t", "taxmod-testseite", TEST_PAGE], capture_output=True, text=True, timeout=60)
        if done.returncode != 0:
            raise ValueError((done.stderr or done.stdout or "lp ging nicht").strip())
        return (done.stdout or "An CUPS übergeben.").strip()

    def install_update(self) -> str:
        import taxmod_printservice as app
        code = (self.update or app.update)(False)
        return "Update installiert." if code == 0 else "Update nicht installiert (Passwort abgebrochen oder Fehler, siehe Protokoll)."


def handler_for(page: Page):
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "taxmod-printservice"

        def log_message(self, *_):
            pass

        def allowed_host(self) -> bool:
            # DNS rebinding: a foreign name that points at 127.0.0.1 brings its own Host header.
            return self.headers.get("Host", "") in (f"{HOST}:{PORT}", f"localhost:{PORT}")

        def has_session(self) -> bool:
            cookie = self.headers.get("Cookie", "")
            sent = dict(part.strip().split("=", 1) for part in cookie.split(";") if "=" in part).get("taxmod_ui", "")
            return secrets.compare_digest(sent, page.secret)

        def send(self, code: int, body: bytes, kind: str, extra: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'unsafe-inline'; frame-ancestors 'none'")
            self.send_header("Referrer-Policy", "no-referrer")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def json(self, code: int, data: dict) -> None:
            self.send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def do_GET(self):
            if not self.allowed_host():
                return self.send(403, b"forbidden", "text/plain")
            url = urllib.parse.urlsplit(self.path)
            if url.path == "/":
                given = urllib.parse.parse_qs(url.query).get("s", [""])[0]
                if given and secrets.compare_digest(given, page.secret):
                    cookie = f"taxmod_ui={page.secret}; HttpOnly; SameSite=Strict; Path=/"
                    return self.send(303, b"", "text/plain", {"Location": "/", "Set-Cookie": cookie})
                if not self.has_session():
                    return self.send(403, "Bitte über das Menü «Taxmod Druckdienst» öffnen.".encode(), "text/plain; charset=utf-8")
                return self.send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            if not self.has_session():
                return self.json(403, {"error": "no session"})
            if url.path == "/api/status":
                return self.json(200, page.status())
            if url.path == "/api/log":
                return self.json(200, {"log": page.log()})
            return self.json(404, {"error": "not found"})

        def do_POST(self):
            # Every change: right host, our session cookie, and sent by our own page — never by another site.
            if not self.allowed_host() or not self.has_session() or self.headers.get("Origin", "") not in ORIGINS:
                return self.json(403, {"error": "forbidden"})
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self.json(415, {"error": "json only"})
            length = min(int(self.headers.get("Content-Length", "0") or 0), 65536)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self.json(400, {"error": "bad json"})
            actions = {
                "/api/settings": page.save_settings,
                "/api/pair": page.pair,
                "/api/test": page.test_print,
                "/api/restart": lambda _body: page.restart(),
                "/api/update": lambda _body: page.install_update(),
                "/api/quit": lambda _body: threading.Thread(target=self.server.shutdown, daemon=True).start() or "Beendet.",
            }
            action = actions.get(urllib.parse.urlsplit(self.path).path)
            if action is None:
                return self.json(404, {"error": "not found"})
            try:
                return self.json(200, {"message": action(body)})
            except (ValueError, OSError, subprocess.SubprocessError, LookupError) as error:
                return self.json(400, {"error": str(error)})

    return Handler


def serve(config_path: str, open_browser: bool = True) -> int:
    session = os.path.expanduser(SESSION_FILE)
    page = Page(config_path)
    try:
        server = http.server.ThreadingHTTPServer((HOST, PORT), handler_for(page))
    except OSError:
        # Already running: open the page of the one that runs.
        try:
            with open(session, encoding="utf-8") as handle:
                url = json.load(handle)["url"]
        except (OSError, ValueError, KeyError):
            print(f"Port {PORT} is taken by something else.")
            return 1
        print(url)
        if open_browser:
            webbrowser.open(url)
        return 0
    url = f"http://{HOST}:{PORT}/?s={page.secret}"
    os.makedirs(os.path.dirname(session), exist_ok=True)
    descriptor = os.open(session, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump({"url": url, "pid": os.getpid()}, handle)
    print(url)
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        try:
            os.unlink(session)
        except OSError:
            pass
    return 0


PAGE = """<!doctype html>
<html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Taxmod Druckdienst</title>
<style>
:root{--bg:#f6f7f7;--card:#fff;--ink:#1d2327;--quiet:#646970;--line:#dcdcde;--accent:#2271b1;--good:#00a32a;--bad:#d63638;--warn:#dba617}
@media (prefers-color-scheme:dark){:root{--bg:#1d2327;--card:#2c3338;--ink:#f0f0f1;--quiet:#a7aaad;--line:#3c434a;--accent:#72aee6}}
*{box-sizing:border-box}body{margin:0;font:15px/1.45 system-ui,sans-serif;background:var(--bg);color:var(--ink)}
main{max-width:960px;margin:0 auto;padding:16px}h1{font-size:1.4em;margin:.2em 0 .6em}h2{font-size:1.05em;margin:0 0 .6em}
section{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px 16px;margin:0 0 14px}
table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:5px 6px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--quiet);font-weight:600;font-size:.9em}.quiet{color:var(--quiet)}.dot{display:inline-block;width:.7em;height:.7em;border-radius:50%;margin-right:.4em}
.good{background:var(--good)}.bad{background:var(--bad)}.warn{background:var(--warn)}
button{font:inherit;padding:5px 12px;border-radius:6px;border:1px solid var(--accent);background:var(--accent);color:#fff;cursor:pointer}
button.ghost{background:transparent;color:var(--accent)}input{font:inherit;padding:5px 8px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--ink)}
label{display:inline-block;margin:0 14px 8px 0}.row{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
#msg{position:sticky;top:0;padding:8px 12px;border-radius:6px;display:none;margin-bottom:12px}#msg.ok{display:block;background:#edfaef;color:#00450c}#msg.err{display:block;background:#fcf0f1;color:#8a2424}
pre{white-space:pre-wrap;font-size:.8em;max-height:360px;overflow:auto;margin:0}
</style></head><body><main>
<h1>Taxmod Druckdienst</h1><div id="msg"></div>
<section><h2>Zustand</h2><div id="state" class="quiet">lädt …</div>
<div class="row" style="margin-top:10px"><button class="ghost" onclick="act('/api/restart')">Neu starten</button><span id="update"></span></div></section>
<section id="pairing"><h2>Mit Taxmod koppeln</h2>
<p class="quiet">In WordPress: Konfiguration › Druck › «Add print service» oder «Kopplungscode». Den Code hier eingeben; er gilt 15 Minuten und nur einmal.</p>
<div class="row"><label>Website <input id="site" size="34" value="https://www.fambach.net"></label><label>Kopplungscode <input id="code" size="12" autocomplete="off"></label>
<button onclick="act('/api/pair',{site:val('site'),code:val('code')})">Koppeln</button></div></section>
<section><h2>Drucker in CUPS</h2><table><thead><tr><th>Warteschlange</th><th>Zustand</th><th></th></tr></thead><tbody id="printers"></tbody></table>
<p class="quiet">Welche Etiketten auf welchen Drucker gehen, steht im Taxmod unter Logistik › Drucker.</p></section>
<section><h2>Letzte Aufträge</h2><table><thead><tr><th>Nr.</th><th>Code</th><th>Drucker</th><th>Stand</th><th>Meldung</th><th>Zeit</th></tr></thead><tbody id="jobs"></tbody></table></section>
<section><h2>Einstellungen dieses Rechners</h2>
<div class="row"><label>Neuversuche <input id="retries" type="number" min="0" max="10" style="width:5em"></label>
<label>Abstand (s) <input id="retry_seconds" type="number" min="1" max="300" style="width:5em"></label>
<label>Auftrag beobachten bis (s) <input id="watch_seconds" type="number" min="60" max="3600" style="width:6em"></label>
<label>Weck-Datei prüfen alle (s) <input id="wake_seconds" type="number" min="60" max="3600" style="width:6em"></label>
<button onclick="saveSettings()">Speichern</button></div>
<p class="quiet">Neuversuche gelten nur für Drucker, bei denen im Taxmod «Automatisch wiederholen» an ist (z. B. bei «Host is down» am Bluetooth-Drucker).</p></section>
<section><h2>Protokoll</h2><div class="row" style="margin-bottom:8px"><button class="ghost" onclick="loadLog()">Laden</button></div><pre id="log" class="quiet"></pre></section>
<p class="quiet"><button class="ghost" onclick="act('/api/quit')">Seite beenden</button> Der Druckdienst läuft weiter.</p>
</main><script>
const $=id=>document.getElementById(id), val=id=>$(id).value.trim();
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function say(text,ok){const m=$('msg');m.textContent=text;m.className=ok?'ok':'err';}
async function act(path,body){say('…',true);try{const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});const d=await r.json();say(d.message||d.error,r.ok);}catch(e){say(String(e),false);}setTimeout(load,1500);}
function saveSettings(){const b={};for(const k of ['retries','retry_seconds','watch_seconds','wake_seconds'])b[k]=Number(val(k));act('/api/settings',b);}
const stateWords={printed:'gedruckt',failed:'Fehler',printing:'druckt'};
async function load(){try{const s=await (await fetch('/api/status')).json();
const running=s.service==='active';
$('state').innerHTML=`<p><span class="dot ${running?'good':'bad'}"></span>Dienst ${running?'läuft':esc(s.service)} · Version ${esc(s.version)} · Rechner ${esc(s.host)}</p>`+
`<p><span class="dot ${s.paired?'good':'warn'}"></span>${s.paired?'Gekoppelt mit '+esc(s.site):'Noch nicht gekoppelt'}</p>`+
`<p class="quiet">Zuletzt abgeholt: ${esc(s.last_pull||'—')}${s.last_error?' · <span style="color:var(--bad)">'+esc(s.last_error)+'</span>':''}</p>`;
$('pairing').style.display=s.paired?'none':'';
$('update').innerHTML=s.newest&&s.newest.replace(/^v/,'')!==s.version?`<button onclick="act('/api/update')">Update auf ${esc(s.newest)} installieren</button>`:'<span class="quiet">Aktuell</span>';
$('printers').innerHTML=s.printers.map(p=>`<tr><td>${esc(p.queue)}</td><td><span class="dot ${p.state==='disabled'?'bad':'good'}"></span>${p.state==='disabled'?'gestoppt':p.state==='printing'?'druckt':'bereit'}</td><td><button class="ghost" data-q="${esc(p.queue)}">Testseite</button></td></tr>`).join('')||'<tr><td colspan="3" class="quiet">CUPS nennt keinen Drucker.</td></tr>';
$('printers').querySelectorAll('button').forEach(b=>b.onclick=()=>act('/api/test',{queue:b.dataset.q}));
$('jobs').innerHTML=s.jobs.map(j=>`<tr><td>${esc(j.id)}</td><td>${esc(j.code)}</td><td>${esc(j.queue)}</td><td>${esc(stateWords[j.state]||j.state)}</td><td class="quiet">${esc(j.message)}</td><td class="quiet">${esc(j.time)}</td></tr>`).join('')||'<tr><td colspan="6" class="quiet">Seit dem Start noch keiner.</td></tr>';
if(document.activeElement.tagName!=='INPUT')for(const k in s.settings)$(k).value=s.settings[k];
}catch(e){say('Keine Verbindung zur Seite: '+e,false);}}
async function loadLog(){const d=await (await fetch('/api/log')).json();$('log').textContent=d.log;}
load();setInterval(load,10000);
</script></body></html>
"""
