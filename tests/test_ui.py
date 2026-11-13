"""Run: python3 -m unittest discover -s tests"""

import http.client
import http.server
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import taxmod_print_agent as agent  # noqa: E402
import taxmod_printservice_ui as ui  # noqa: E402

agent.STATUS_FILE = os.path.join(tempfile.mkdtemp(), "status.json")


class Runner:
    def __init__(self):
        self.commands = []

    def __call__(self, command, **_):
        self.commands.append(command)
        out = {"lpstat": "printer Phomemo_M220L is idle.  enabled since x\nprinter QL-810W_take disabled since y -\n", "systemctl": "active\n"}
        return subprocess.CompletedProcess(command, 0, stdout=out.get(command[0], "request id is Phomemo_M220L-9 (1 file(s))"), stderr="")


class Answer(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class PageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.mkdtemp()
        cls.runner = Runner()
        cls.sent = []

        def fetch(request, timeout=0):
            cls.sent.append(request)
            if "releases" in request.full_url:
                return Answer(json.dumps({"tag_name": "v9.9.9", "assets": []}).encode())
            body = json.loads(request.data)
            if body["code"] != "K7QM-3XPD":
                raise ui.urllib.error.HTTPError(request.full_url, 403, "no", {}, None)
            return Answer(json.dumps({"rest": "https://site.test/wp-json/taxmod/v1/", "token": "frisch-456", "ntfy": "https://ntfy.sh/t", "wake_url": "https://site.test/w.txt", "name": "Server"}).encode())

        cls.page = ui.Page(os.path.join(cls.folder, "config.json"), run=cls.runner, fetch=fetch, update=lambda check: 0)
        cls.server = http.server.ThreadingHTTPServer((ui.HOST, 0), ui.handler_for(cls.page))
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def call(self, method, path, body=None, host=None, origin=None, cookie=True):
        connection = http.client.HTTPConnection(ui.HOST, self.port, timeout=10)
        headers = {"Host": host or f"{ui.HOST}:{ui.PORT}"}
        if cookie:
            headers["Cookie"] = f"taxmod_ui={self.page.secret}"
        if origin is not None:
            headers["Origin"] = origin
        data = None
        if body is not None:
            data = json.dumps(body)
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=data, headers=headers)
        response = connection.getresponse()
        return response.status, response.read(), response

    def test_the_page_opens_only_with_the_secret_from_the_launcher(self):
        self.assertEqual(self.call("GET", "/", cookie=False)[0], 403)
        status, _, response = self.call("GET", f"/?s={self.page.secret}", cookie=False)
        self.assertEqual(status, 303)
        self.assertIn("HttpOnly; SameSite=Strict", response.getheader("Set-Cookie"))
        status, body, _ = self.call("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Taxmod Druckdienst", body)

    def test_a_foreign_host_name_is_refused(self):
        # DNS rebinding: evil.example pointing at 127.0.0.1 still sends its own Host.
        self.assertEqual(self.call("GET", "/api/status", host="evil.example:8631")[0], 403)

    def test_a_change_from_another_site_is_refused(self):
        self.assertEqual(self.call("POST", "/api/restart", {}, origin=None)[0], 403)
        self.assertEqual(self.call("POST", "/api/restart", {}, origin="https://evil.example")[0], 403)
        self.assertEqual(self.call("POST", "/api/restart", {}, origin=f"http://{ui.HOST}:{ui.PORT}", cookie=False)[0], 403)
        self.assertEqual(self.call("POST", "/api/restart", {}, origin=f"http://{ui.HOST}:{ui.PORT}")[0], 200)

    def test_the_status_shows_service_printers_and_newer_release(self):
        status, body, _ = self.call("GET", "/api/status")
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(data["service"], "active")
        self.assertEqual([p["queue"] for p in data["printers"]], ["Phomemo_M220L", "QL-810W_take"])
        self.assertEqual(data["printers"][1]["state"], "disabled")
        self.assertEqual(data["newest"], "v9.9.9")

    def test_pairing_stores_the_token_sealed_and_the_rest_in_config(self):
        origin = f"http://{ui.HOST}:{ui.PORT}"
        status, body, _ = self.call("POST", "/api/pair", {"site": "https://site.test", "code": "falsch"}, origin=origin)
        self.assertEqual(status, 400)
        status, body, _ = self.call("POST", "/api/pair", {"site": "https://site.test/", "code": "k7qm-3xpd"}, origin=origin)
        self.assertEqual(status, 200, body)
        self.assertIn("Server", json.loads(body)["message"])
        self.assertEqual(self.sent[-1].full_url, "https://site.test/wp-json/taxmod/v1/print/pair")
        with open(os.path.join(self.folder, "config.json"), encoding="utf-8") as handle:
            config = json.load(handle)
        self.assertNotIn("token", config)
        self.assertEqual(config["rest"], "https://site.test/wp-json/taxmod/v1/")
        self.assertEqual(agent.read_token(self.folder), "frisch-456")
        self.assertEqual(agent.load_config(os.path.join(self.folder, "config.json"))["token"], "frisch-456")

    def test_pairing_needs_https(self):
        status, _, _ = self.call("POST", "/api/pair", {"site": "http://site.test", "code": "X"}, origin=f"http://{ui.HOST}:{ui.PORT}")
        self.assertEqual(status, 400)

    def test_settings_are_held_to_their_range(self):
        origin = f"http://{ui.HOST}:{ui.PORT}"
        self.assertEqual(self.call("POST", "/api/settings", {"retries": 99}, origin=origin)[0], 400)
        self.assertEqual(self.call("POST", "/api/settings", {"retries": 5, "retry_seconds": 20}, origin=origin)[0], 200)
        with open(os.path.join(self.folder, "config.json"), encoding="utf-8") as handle:
            config = json.load(handle)
        self.assertEqual((config["retries"], config["retry_seconds"]), (5, 20))
        self.assertIn(["systemctl", "--user", "restart", ui.UNIT], self.runner.commands)

    def test_a_test_page_goes_only_to_a_plain_queue_name(self):
        origin = f"http://{ui.HOST}:{ui.PORT}"
        self.assertEqual(self.call("POST", "/api/test", {"queue": "-h evil"}, origin=origin)[0], 400)


if __name__ == "__main__":
    unittest.main()
