"""Run: python3 -m unittest discover -s tests"""

import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import taxmod_printservice as app  # noqa: E402


class Done:
    def __init__(self, code=0):
        self.returncode = code


class Recorder:
    def __init__(self):
        self.commands = []

    def __call__(self, command, **_):
        self.commands.append(command)
        return Done()


def answer(body):
    response = io.BytesIO(json.dumps(body).encode("utf-8"))
    response.__enter__ = lambda self=response: self
    response.__exit__ = lambda *a: None
    return response


class Versions(unittest.TestCase):
    def test_a_release_is_newer_only_by_its_numbers(self):
        self.assertTrue(app.is_newer("v0.4.1", "0.4.0"))
        self.assertTrue(app.is_newer("v0.10.0", "0.9.9"))
        self.assertFalse(app.is_newer("v0.4.0", "0.4.0"))
        self.assertFalse(app.is_newer("v0.3.9", "0.4.0"))
        self.assertFalse(app.is_newer("nightly", "0.4.0"))

    def test_the_version_is_the_agents(self):
        self.assertEqual(app.agent.VERSION, "taxmod-print-agent " + app.version())

    def test_the_package_script_reads_the_same_version(self):
        script = (pathlib.Path(__file__).parent.parent / "scripts" / "build-deb.sh").read_text()
        self.assertIn('s/^VERSION = "taxmod-print-agent \\(.*\\)"$/\\1/p', script)


class NewestRelease(unittest.TestCase):
    def test_takes_the_tag_and_the_deb(self):
        body = {"tag_name": "v0.5.0", "assets": [{"name": "notes.txt", "browser_download_url": "x"},
                                                  {"name": "taxmod-printservice_0.5.0_all.deb", "browser_download_url": "https://dl/deb"}]}
        self.assertEqual(app.newest_release(lambda *a, **k: answer(body)), ("v0.5.0", "https://dl/deb"))

    def test_no_release_yet_is_not_an_error(self):
        def missing(*_a, **_k):
            raise app.urllib.error.HTTPError("u", 404, "Not Found", {}, None)
        self.assertEqual(app.newest_release(missing), ("", ""))


class Update(unittest.TestCase):
    def test_up_to_date_installs_nothing(self):
        run = Recorder()
        with mock.patch.object(app, "newest_release", return_value=(f"v{app.version()}", "https://dl/deb")):
            self.assertEqual(app.update(False, run=run), 0)
        self.assertEqual(run.commands, [])

    def test_check_only_installs_nothing(self):
        run = Recorder()
        with mock.patch.object(app, "newest_release", return_value=("v99.0.0", "https://dl/deb")):
            self.assertEqual(app.update(True, run=run), 0)
        self.assertEqual(run.commands, [])

    def test_a_newer_release_goes_through_apt_with_the_password(self):
        run = Recorder()
        with mock.patch.object(app, "newest_release", return_value=("v99.0.0", "https://dl/deb")), \
             mock.patch.object(app.urllib.request, "urlopen", return_value=answer({})), \
             mock.patch.object(app, "elevate", return_value=["pkexec"]):
            self.assertEqual(app.update(False, run=run), 0)
        command = run.commands[0]
        self.assertEqual(command[:4], ["pkexec", "apt-get", "install", "-y"])
        self.assertTrue(command[4].endswith("taxmod-printservice_99.0.0_all.deb"))


class Setup(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        patcher = mock.patch.dict(os.environ, {"HOME": self.home.name, "USER": "stefan"})
        patcher.start()
        self.addCleanup(patcher.stop)
        example = pathlib.Path(self.home.name) / "example.json"
        example.write_text(json.dumps({"rest": "", "token": ""}))
        patcher = mock.patch.object(app, "EXAMPLE", str(example))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config = pathlib.Path(self.home.name) / ".config" / "taxmod-print-agent" / "config.json"

    def test_without_a_config_it_writes_one_and_starts_nothing(self):
        run = Recorder()
        self.assertEqual(app.setup(run=run), 1)
        self.assertTrue(self.config.exists())
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        self.assertEqual(run.commands, [])

    def test_turns_off_the_hand_installed_service_and_starts_the_package(self):
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps({"rest": "https://example.test/wp-json/taxmod/v1", "token": "t"}))
        old = pathlib.Path(self.home.name) / ".config" / "systemd" / "user" / app.OLD_UNIT
        old.parent.mkdir(parents=True)
        old.write_text("[Unit]")
        run = Recorder()
        self.assertEqual(app.setup(run=run), 0)
        self.assertIn(["systemctl", "--user", "disable", "--now", app.OLD_UNIT], run.commands)
        self.assertFalse(old.exists())
        self.assertIn(["systemctl", "--user", "enable", "--now", app.UNIT], run.commands)
        self.assertIn(["loginctl", "enable-linger", "stefan"], run.commands)


class Package(unittest.TestCase):
    @unittest.skipUnless(subprocess.run(["which", "dpkg-deb"], capture_output=True).returncode == 0, "dpkg-deb missing")
    def test_the_deb_holds_the_service_the_command_and_the_unit(self):
        root = pathlib.Path(__file__).parent.parent
        subprocess.run([str(root / "scripts" / "build-deb.sh")], check=True, capture_output=True)
        deb = root / "dist" / f"taxmod-printservice_{app.version()}_all.deb"
        listing = subprocess.run(["dpkg-deb", "--contents", str(deb)], check=True, capture_output=True, text=True).stdout
        for path in ("./usr/bin/taxmod-printservice", "./usr/lib/taxmod-printservice/taxmod_print_agent.py",
                     "./usr/lib/systemd/user/taxmod-printservice.service", "./usr/share/taxmod-printservice/config.example.json"):
            self.assertIn(path, listing)


if __name__ == "__main__":
    unittest.main()
