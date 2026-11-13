#!/usr/bin/env python3
"""Taxmod print service: fetches label jobs from WordPress and prints them through CUPS (D-1032, stage 2 of D-1100).

It never polls WordPress. It waits for a push on its ntfy topic, looks at a small static wake file every few minutes
as a fallback, and only then asks WordPress once for every waiting job. Standard library only.

    python3 taxmod_print_agent.py --config ~/.config/taxmod-print-agent/config.json
    python3 taxmod_print_agent.py --config ... --once     # fetch and print once, then stop
"""

from __future__ import annotations

import argparse
import base64
import collections
import datetime
import hashlib
import json
import logging
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

VERSION = "taxmod-print-agent 0.6.0"
TOKEN_HEADER = "X-Taxmod-Print-Token"

# Per label format: the CUPS options. The browser test showed the Brother driver wants landscape for labels as wide
# as the roll; the PageSize names are the ones in the queue's PPD. Override any of them in config.json → "formats".
DEFAULT_FORMATS = {
    "roll62": {"PageSize": "62x100"},
    "holder62": {"PageSize": "62x50", "orientation-requested": "4"},
    "material62": {"PageSize": "62x30", "orientation-requested": "4"},
    "roll102": {"PageSize": "102x62", "orientation-requested": "4"},
}

log = logging.getLogger("taxmod-print-agent")


TOKEN_FILE = "token.enc"
STATUS_FILE = "~/.local/state/taxmod-printservice/status.json"


def secret_key() -> str:
    """The key of the token file: this machine and this user. A copy of the file elsewhere opens nothing (D-1332)."""
    try:
        with open("/etc/machine-id", encoding="ascii") as handle:
            machine = handle.read().strip()
    except OSError:
        machine = socket.gethostname()
    return hashlib.sha256(f"taxmod-printservice:{machine}:{os.getuid()}".encode()).hexdigest()


def openssl(arguments: list[str], data: bytes) -> bytes:
    done = subprocess.run(["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-salt", "-pass", "env:TAXMOD_KEY", *arguments],
                          input=data, capture_output=True, env={**os.environ, "TAXMOD_KEY": secret_key()}, timeout=30)
    if done.returncode != 0:
        raise ValueError("the token file cannot be read: " + done.stderr.decode("utf-8", "replace").strip())
    return done.stdout


def save_token(folder: str, token: str) -> None:
    """D-1332: the token is never kept in clear; config.json holds everything else."""
    path = os.path.join(os.path.expanduser(folder), TOKEN_FILE)
    sealed = openssl(["-e", "-a", "-A"], token.encode("utf-8"))
    descriptor = os.open(path + ".new", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(sealed)
    os.replace(path + ".new", path)


def read_token(folder: str) -> str:
    path = os.path.join(os.path.expanduser(folder), TOKEN_FILE)
    if not os.path.exists(path):
        return ""
    with open(path, "rb") as handle:
        return openssl(["-d", "-a", "-A"], handle.read()).decode("utf-8").strip()


def load_config(path: str) -> dict:
    with open(os.path.expanduser(path), encoding="utf-8") as handle:
        config = json.load(handle)
    if not config.get("token"):
        config["token"] = read_token(os.path.dirname(os.path.expanduser(path)))
    for key in ("rest", "token"):
        if not config.get(key):
            raise ValueError(f"config.json needs «{key}»")
    config.setdefault("printers", {})
    config.setdefault("ntfy", "")
    config.setdefault("wake_url", "")
    config.setdefault("wake_seconds", 300)
    config.setdefault("alive_seconds", 3600)
    # D-1234: how long a job is watched in CUPS after lp took it, and how often it is looked at.
    config.setdefault("watch_seconds", 900)
    config.setdefault("watch_every", 5)
    # D-1332: a printer with «Automatisch wiederholen» on gets this many new tries, this far apart (Bluetooth: «Host is down»).
    config.setdefault("retries", 3)
    config.setdefault("retry_seconds", 15)
    formats = dict(DEFAULT_FORMATS)
    for key, options in (config.get("formats") or {}).items():
        formats[key] = options
    config["formats"] = formats
    return config


def printer_for(config: dict, roll_mm: float) -> str:
    """The CUPS queue for a roll width; config "printers" maps "62" / "102" to queue names."""
    queue = config["printers"].get(str(int(round(roll_mm))))
    if not queue:
        raise LookupError(f"no printer for the {roll_mm:g} mm roll in config.json")
    return queue


# D-1190: queue and options can come from WordPress, so they are held to plain names before they reach lp.
PLAIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,126}$")


def plain(text: str, what: str) -> str:
    if not PLAIN.match(text):
        raise ValueError(f"{what} «{text}» is not a plain name")
    return text


def lp_command(config: dict, job: dict, pdf_path: str) -> list[str]:
    """D-1191: a job printed on a printer of the model brings queue and CUPS options; otherwise roll width and format decide."""
    queue = plain(str(job["queue"]), "queue") if job.get("queue") else printer_for(config, float(job["roll"]))
    command = ["lp", "-d", queue, "-t", f"taxmod-{job['id']}-{job.get('code', '')}"]
    options = job["options"] if isinstance(job.get("options"), dict) and job["options"] else config["formats"].get(job["format"], {})
    for name, value in options.items():
        name, value = plain(str(name), "option"), plain(str(value), "value") if value != "" else ""
        command += ["-o", f"{name}={value}" if value != "" else name]
    return command + [pdf_path]


# D-1234: what CUPS says about one job — «request id is Phomemo_M220L-228 (1 file(s))», and ipptool's job attributes.
REQUEST_ID = re.compile(r"request id is (\S+)-(\d+)")
JOB_TEST = "/usr/share/cups/ipptool/get-job-attributes.test"


def cups_job(output: str) -> tuple[str, int] | None:
    found = REQUEST_ID.search(output or "")
    return (found.group(1), int(found.group(2))) if found else None


def job_state(output: str) -> tuple[str, str]:
    """job-state and job-printer-state-message from «ipptool -tv»; ('', '') when neither is there."""
    values = {}
    for line in (output or "").splitlines():
        name, _, value = line.strip().partition(" = ")
        key = name.split(" (")[0]
        if key in ("job-state", "job-printer-state-message"):
            values[key] = value.strip()
    return values.get("job-state", ""), values.get("job-printer-state-message", "")


def now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class WordPress:
    def __init__(self, config: dict):
        self.base = config["rest"].rstrip("/") + "/"
        self.token = config["token"]

    def call(self, path: str, body: dict) -> dict:
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", TOKEN_HEADER: self.token, "User-Agent": VERSION},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))


class Agent:
    def __init__(self, config: dict, run=subprocess.run, spawn=None, sleep=time.sleep):
        self.config = config
        self.wordpress = WordPress(config)
        self.run = run
        self.sleep = sleep
        self.spawn = spawn or (lambda work: threading.Thread(target=work, daemon=True).start())
        self.wake = threading.Event()
        self.lock = threading.Lock()
        self.status_path = os.path.expanduser(STATUS_FILE)
        self.status = {"version": VERSION, "host": socket.gethostname(), "last_pull": "", "last_error": "", "queues": [], "jobs": []}
        self.recent = collections.deque(maxlen=30)

    def note(self, **changes) -> None:
        """What the settings page shows: the state of the service, written next to it after every change."""
        self.status.update(changes, jobs=list(self.recent), written=now())
        try:
            os.makedirs(os.path.dirname(self.status_path), exist_ok=True)
            with open(self.status_path + ".new", "w", encoding="utf-8") as handle:
                json.dump(self.status, handle, ensure_ascii=False)
            os.replace(self.status_path + ".new", self.status_path)
        except OSError as error:
            log.warning("status: %s", error)

    def track(self, job: dict, state: str, message: str) -> None:
        for entry in self.recent:
            if entry["id"] == job.get("id"):
                entry.update(state=state, message=message, time=now())
                break
        else:
            self.recent.appendleft({"id": job.get("id"), "code": job.get("code", ""), "queue": job.get("queue", ""), "state": state, "message": message, "time": now()})
        self.note()

    def printers(self) -> list[str]:
        return [f"{roll} mm: {queue}" for roll, queue in sorted(self.config["printers"].items())]

    def queues(self) -> list[str]:
        """D-1336: the CUPS queues of this computer; WordPress hands over only the jobs for these. Without them no pull,
        so a second computer never takes jobs it cannot print."""
        done = self.run(["lpstat", "-e"], capture_output=True, text=True, timeout=30)
        names = [line.strip() for line in (done.stdout or "").splitlines() if PLAIN.match(line.strip())]
        if done.returncode != 0 or not names:
            raise OSError(f"lpstat -e lists no CUPS queue: {(done.stderr or '').strip() or done.returncode}")
        return names

    def fetch_and_print(self) -> int:
        """One pull, then every job printed and answered. Returns how many jobs came."""
        with self.lock:
            try:
                queues = self.queues()
                answer = self.wordpress.call("print/pull", {"agent": VERSION, "host": socket.gethostname(), "printers": self.printers(), "queues": queues})
            except (urllib.error.URLError, OSError, ValueError) as error:
                self.note(last_error=f"{now()} {error}")
                raise
            self.note(last_pull=now(), last_error="", queues=queues)
            jobs = answer.get("jobs", [])
            for job in jobs:
                self.track(job, "printing", "")
                ok, message = self.print_job(job)
                taken = cups_job(message) if ok else None
                if taken is None:
                    self.result(job, ok, message)
                    continue
                # D-1234: lp only took it; the job is printed when CUPS says so, not before.
                self.answer(job["id"], "progress", {"message": f"CUPS {taken[0]}-{taken[1]}"})
                # D-1333: a printer that needs a pause gets its jobs one after the other, the pause after each.
                pause = max(0, int(job.get("pause") or 0))
                if pause:
                    self.watch(job, taken[0], taken[1])
                    self.sleep(pause)
                else:
                    self.spawn(lambda job=job, taken=taken: self.watch(job, taken[0], taken[1]))
            return len(jobs)

    def result(self, job: dict, ok: bool, message: str) -> None:
        self.track(job, "printed" if ok else "failed", message)
        self.answer(job["id"], "result", {"ok": ok, "message": message})

    def answer(self, job_id: int, step: str, body: dict) -> None:
        try:
            self.wordpress.call(f"print/jobs/{job_id}/{step}", body)
        except urllib.error.URLError as error:
            log.error("job %s: %s not delivered: %s", job_id, step, error)

    def state_of(self, number: int) -> tuple[str, str]:
        try:
            done = self.run(["ipptool", "-tv", f"ipp://localhost/jobs/{number}", JOB_TEST], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as error:
            return "", str(error)
        return job_state(done.stdout)

    def watch(self, job: dict, queue: str, number: int) -> None:
        """D-1234: follows one CUPS job to its end. Retrying off (the default): the first failure ends it as failed. On
        (D-1332): the job is taken out of CUPS and sent again, up to «retries» times, «retry_seconds» apart."""
        name, said, retry, tries = f"{queue}-{number}", "", bool(job.get("retry")), 0
        deadline = time.monotonic() + int(self.config["watch_seconds"])
        while time.monotonic() < deadline:
            state, message = self.state_of(number)
            if state == "completed":
                self.result(job, True, f"Sent to the printer ({name})" + (f" after {tries} new tries" if tries else ""))
                return
            failed = state == "pending-held" or (state == "pending" and message != "") or state in ("aborted", "canceled")
            if failed and retry and tries < int(self.config["retries"]):
                tries += 1
                if state not in ("aborted", "canceled"):
                    self.run(["cancel", name], capture_output=True, text=True, timeout=30)
                self.answer(job["id"], "progress", {"message": f"{message or state} ({name}) · new try {tries}/{self.config['retries']} in {self.config['retry_seconds']} s"})
                self.sleep(int(self.config["retry_seconds"]))
                ok, output = self.print_job(job)
                taken = cups_job(output) if ok else None
                if taken is None:
                    self.result(job, False, output)
                    return
                queue, number = taken
                name, said = f"{queue}-{number}", ""
                continue
            if state in ("aborted", "canceled"):
                self.result(job, False, f"{message or state} ({name})")
                return
            if failed:
                self.run(["cancel", name], capture_output=True, text=True, timeout=30)
                self.result(job, False, f"{message or 'CUPS held the job'} ({name})")
                return
            if message and message != said:
                said = message
                self.answer(job["id"], "progress", {"message": f"{message} ({name})"})
            self.sleep(int(self.config["watch_every"]))
        self.result(job, False, f"No end after {self.config['watch_seconds']} s: {said or 'no word from CUPS'} ({name})")

    def print_job(self, job: dict) -> tuple[bool, str]:
        try:
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as handle:
                handle.write(base64.b64decode(job["pdf"]))
                path = handle.name
            try:
                command = lp_command(self.config, job, path)
                done = self.run(command, capture_output=True, text=True, timeout=60)
            finally:
                os.unlink(path)
        except (LookupError, ValueError, OSError, subprocess.SubprocessError) as error:
            log.error("job %s: %s", job.get("id"), error)
            return False, str(error)
        output = (done.stdout or done.stderr or "").strip()
        log.info("job %s %s: %s", job["id"], job["format"], output)
        return done.returncode == 0, (output or f"lp exit {done.returncode}") + " · " + " ".join(command[:-1])

    def listen_ntfy(self) -> None:
        url = self.config["ntfy"].rstrip("/") + "/json"
        delay = 5
        while True:
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": VERSION}), timeout=90) as stream:
                    delay = 5
                    for line in stream:
                        if json.loads(line or b"{}").get("event") == "message":
                            self.wake.set()
            except (urllib.error.URLError, OSError, ValueError) as error:
                log.warning("ntfy: %s, again in %ss", error, delay)
                time.sleep(delay)
                delay = min(delay * 2, 300)

    def watch_file(self) -> None:
        last = None
        while True:
            try:
                request = urllib.request.Request(self.config["wake_url"], headers={"User-Agent": VERSION, "Cache-Control": "no-cache"})
                with urllib.request.urlopen(request, timeout=30) as response:
                    counter = response.read(32).decode("ascii", "replace").strip()
                if last is not None and counter != last:
                    self.wake.set()
                last = counter
            except (urllib.error.URLError, OSError) as error:
                log.warning("wake file: %s", error)
            time.sleep(int(self.config["wake_seconds"]))

    def serve(self) -> None:
        if self.config["ntfy"]:
            threading.Thread(target=self.listen_ntfy, daemon=True).start()
        if self.config["wake_url"]:
            threading.Thread(target=self.watch_file, daemon=True).start()
        self.wake.set()  # once at start: whatever waits from before
        while True:
            woken = self.wake.wait(timeout=int(self.config["alive_seconds"]))
            self.wake.clear()
            try:
                count = self.fetch_and_print()
                if count or woken:
                    log.info("pull: %d job(s)", count)
            except (urllib.error.URLError, OSError, ValueError) as error:
                log.error("pull: %s", error)
                time.sleep(30)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default="~/.config/taxmod-print-agent/config.json")
    parser.add_argument("--once", action="store_true", help="fetch and print once, then stop")
    arguments = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    agent = Agent(load_config(arguments.config))
    if arguments.once:
        print(f"{agent.fetch_and_print()} job(s)")
        return 0
    agent.serve()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
