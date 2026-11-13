#!/usr/bin/env python3
"""Taxmod print service as an installed app (D-1332): run the service, set it up for this user, update it from GitHub.

    taxmod-printservice run            the service itself (what systemd starts)
    taxmod-printservice once           fetch and print once, then stop
    taxmod-printservice setup          for this user: config, service on, keeps running without a login
    taxmod-printservice update         install the newest release (asks for the password)
    taxmod-printservice update --check only say whether there is a newer one
    taxmod-printservice ui             the settings page on http://127.0.0.1:8631 (from the menu)
    taxmod-printservice pair SITE CODE couple with Taxmod by the one-time code (for a computer without a desktop)
    taxmod-printservice version
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import taxmod_print_agent as agent  # noqa: E402

REPOSITORY = "sfambach/taxmod_printservice"
UNIT = "taxmod-printservice.service"
# The unit the hand-copied script used before the package; setup turns it off so only one service prints.
OLD_UNIT = "taxmod-print-agent.service"
CONFIG = "~/.config/taxmod-print-agent/config.json"
EXAMPLE = "/usr/share/taxmod-printservice/config.example.json"


def version() -> str:
    return agent.VERSION.rsplit(" ", 1)[-1]


def numbers(text: str) -> tuple[int, ...]:
    found = re.match(r"^v?(\d+(?:\.\d+)*)", text.strip())
    return tuple(int(part) for part in found.group(1).split(".")) if found else ()


def newest_release(fetch=urllib.request.urlopen) -> tuple[str, str]:
    """Tag and .deb download link of the newest release; ('', '') when there is none."""
    request = urllib.request.Request(
        f"https://api.github.com/repos/{REPOSITORY}/releases/latest",
        headers={"Accept": "application/vnd.github+json", "User-Agent": agent.VERSION},
    )
    try:
        with fetch(request, timeout=30) as response:
            release = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return "", ""
        raise
    link = next((asset["browser_download_url"] for asset in release.get("assets", []) if str(asset.get("name", "")).endswith(".deb")), "")
    return str(release.get("tag_name", "")), link


def is_newer(tag: str, current: str) -> bool:
    return numbers(tag) > numbers(current)


def elevate() -> list[str]:
    """pkexec asks for the password in a window on a desktop and in the terminal elsewhere; sudo where there is no pkexec."""
    if shutil.which("pkexec"):
        return ["pkexec"]
    if shutil.which("sudo"):
        return ["sudo"]
    raise RuntimeError("neither pkexec nor sudo is installed")


def update(check_only: bool, run=subprocess.run) -> int:
    tag, link = newest_release()
    current = version()
    if not tag or not is_newer(tag, current):
        print(f"Up to date ({current}).")
        return 0
    if not link:
        print(f"Release {tag} has no .deb yet; try again in a few minutes.")
        return 1
    print(f"Newer release: {tag} (installed {current}).")
    if check_only:
        return 0
    with tempfile.TemporaryDirectory() as folder:
        package = pathlib.Path(folder) / f"taxmod-printservice_{tag.lstrip('v')}_all.deb"
        request = urllib.request.Request(link, headers={"User-Agent": agent.VERSION})
        with urllib.request.urlopen(request, timeout=120) as response, open(package, "wb") as handle:
            shutil.copyfileobj(response, handle)
        package.chmod(0o644)
        os.chmod(folder, 0o755)  # apt reads the file as its own user
        done = run([*elevate(), "apt-get", "install", "-y", str(package)])
    return done.returncode


def systemctl(*arguments: str, run=subprocess.run) -> int:
    return run(["systemctl", "--user", *arguments]).returncode


def setup(run=subprocess.run) -> int:
    config = pathlib.Path(os.path.expanduser(CONFIG))
    if not config.exists():
        config.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(EXAMPLE, config)
        config.chmod(0o600)
        print(f"Wrote {config}. Couple it with Taxmod: open «Taxmod Druckdienst» from the menu and enter the code from")
        print("Taxmod › Configuration › Printing, or run «taxmod-printservice pair https://… CODE»; then run setup again.")
        return 1
    seal_token(config)
    agent.load_config(str(config))  # fails here, before the service starts, when rest or token is missing

    old = pathlib.Path(os.path.expanduser("~/.config/systemd/user")) / OLD_UNIT
    if old.exists():
        systemctl("disable", "--now", OLD_UNIT, run=run)
        old.rename(old.with_name(old.name + ".before-package"))
        print(f"The hand-installed {OLD_UNIT} is off; its unit is kept as {old.name}.before-package.")

    systemctl("daemon-reload", run=run)
    if systemctl("enable", "--now", UNIT, run=run) != 0:
        return 1
    systemctl("restart", UNIT, run=run)
    # Keeps the service running without anyone logged in (Earth after a restart, the server always).
    run(["loginctl", "enable-linger", os.environ.get("USER", "")])
    print(f"Running: journalctl --user -u {UNIT} -f")
    return 0


def seal_token(config: pathlib.Path) -> None:
    """D-1332: a token still in config.json in clear moves into the sealed token file."""
    with open(config, encoding="utf-8") as handle:
        data = json.load(handle)
    if not data.get("token"):
        return
    agent.save_token(str(config.parent), str(data.pop("token")))
    descriptor = os.open(str(config) + ".new", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
    os.replace(str(config) + ".new", config)
    print(f"The token now lives sealed in {config.parent / agent.TOKEN_FILE}, no longer in config.json.")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="taxmod-printservice", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "once"):
        command = commands.add_parser(name)
        command.add_argument("--config", default=CONFIG)
    commands.add_parser("setup")
    command = commands.add_parser("update")
    command.add_argument("--check", action="store_true")
    commands.add_parser("version")
    command = commands.add_parser("ui")
    command.add_argument("--config", default=CONFIG)
    command.add_argument("--no-browser", action="store_true", help="only print the address (over ssh: ssh -L 8631:127.0.0.1:8631)")
    command = commands.add_parser("pair")
    command.add_argument("site", help="e.g. https://www.fambach.net")
    command.add_argument("code")
    command.add_argument("--config", default=CONFIG)
    arguments = parser.parse_args(argv)

    if arguments.command == "version":
        print(version())
        return 0
    if arguments.command == "update":
        return update(arguments.check)
    if arguments.command == "setup":
        return setup()
    if arguments.command in ("ui", "pair"):
        import taxmod_printservice_ui as ui
        if arguments.command == "ui":
            return ui.serve(arguments.config, not arguments.no_browser)
        try:
            print(ui.Page(arguments.config).pair({"site": arguments.site, "code": arguments.code}))
        except ValueError as error:
            print(error)
            return 1
        return 0
    return agent.main(["--config", arguments.config] + (["--once"] if arguments.command == "once" else []))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
