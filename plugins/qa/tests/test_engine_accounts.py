"""Process-bound account, service and private-channel integration contracts."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
from threading import Thread
import unittest
from urllib.parse import parse_qs
from urllib.request import Request

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/engine/scripts"
sys.path.insert(0, str(SCRIPTS))

from av_config.errors import ConfigError
from qa_engine.recipes import http_request

PLAN = '''# Test Plan
## BE Test Scenarios
### BE-01: Authenticated request
- **Method:** GET /items
- **Headers:** Authorization: Bearer $QA_USER_TOKEN
- **Expected:** 200 returned. (src/app.py:1)
'''


class AccountServer(ThreadingHTTPServer):
    events: list[tuple[str, dict[str, object]]]
    mode: str
    logins: int
    health_status: int

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), AccountHandler)
        self.events = []
        self.mode = "ok"
        self.logins = 0
        self.health_status = 200


class AccountHandler(BaseHTTPRequestHandler):
    server: AccountServer

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        self.reply(self.server.health_status, {})

    def do_POST(self) -> None:
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        payload = {name: values[0] for name, values in parse_qs(raw.decode()).items()} if self.headers.get("Content-Type") == "application/x-www-form-urlencoded" else json.loads(raw)
        self.server.events.append((self.path, payload))
        if self.server.mode == "unavailable":
            self.close_connection = True
            return
        if self.path == "/create":
            if self.headers.get("x-key") != "admin-secret":
                self.reply(403, {})
            elif self.server.mode == "conflict" and len([p for p, _ in self.server.events if p == "/create"]) == 1:
                self.reply(409, {})
            elif self.server.mode.startswith("500"):
                self.reply(500, {"data": {"items": [{"id": "account-id"}]}} if self.server.mode == "500-id" else {})
            elif self.server.mode == "redirect":
                self.reply(302, {}, location="/login")
            else:
                self.reply(201, {"data": {"items": [{"id": "account-id"}]}})
        else:
            self.server.logins += 1
            if self.headers.get("x-key") != "login-value":
                self.reply(403, {})
            else:
                self.reply(200, {} if self.server.mode == "missing" else {"token": f"token-{self.server.logins}"}, cookies=True)

    def do_DELETE(self) -> None:
        self.server.events.append((self.path, {}))
        self.reply(204 if self.headers.get("x-key") == "admin-secret" else 403, {})

    def reply(self, status: int, payload: object, *, cookies: bool = False, location: str | None = None) -> None:
        self.send_response(status)
        if cookies:
            self.send_header("Set-Cookie", "__Host-session=host-secret; Path=/; Secure")
            if self.server.mode != "missing-cookie":
                self.send_header("Set-Cookie", "connect.sid=sid-secret; Path=/")
        if location:
            self.send_header("Location", location)
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())


class AccountTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.env = {**os.environ, "TMPDIR": str(self.root), "XDG_STATE_HOME": str(self.root / "state"),
                    "AV_ADMIN": "admin-secret", "AV_LOGIN": "login-value", "AV_UNUSED": "unused-secret"}
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-c", "user.name=QA", "-c", "user.email=qa@test.local", "commit", "--allow-empty", "-qm", "fixture"], cwd=self.repo, check=True)
        self.put(".av/local.toml\n", ".git/info/exclude")
        self.server = AccountServer()
        thread = Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        self.config = f'''version = 1
[env.targets]
api = "{self.origin}"
[env.secrets]
ADMIN = "env:AV_ADMIN"
[env.values]
LOGIN = "env:AV_LOGIN"
UNUSED = 'cmd:touch unused-source-ran; printf "%s" "$AV_UNUSED"'
[qa.defaults]
be_target = "api"
fe_target = "api"
[qa.policy]
mutations = "allow"
disposable_data = true
[qa.accounts]
personas = ["user", "other"]
email = "qa+{{run}}-{{persona}}@test.local"
password = "generate"
[qa.accounts.create]
kind = "http"
target = "api"
method = "POST"
path = "/create"
headers = {{x-key = "{{secret.ADMIN}}"}}
json = {{email = "{{email}}", password = "{{password}}"}}
expect = [201]
conflict = [409]
id = ".data.items[0].id"
[qa.accounts.login]
kind = "http"
target = "api"
method = "POST"
path = "/login"
headers = {{x-key = "{{value.LOGIN}}"}}
json = {{email = "{{email}}", password = "{{password}}"}}
expect = [200]
token = ".token"
cookies = ["__Host-session", "connect.sid"]
[qa.accounts.delete]
kind = "http"
target = "api"
method = "DELETE"
path = "/delete/{{id}}?run={{run}}"
headers = {{x-key = "{{secret.ADMIN}}"}}
expect = [204]
'''
        self.put(self.config, ".av/config.toml")
        self.plan = self.put(PLAN, "docs/testing/plans/accounts-plan.md")
        self.trust()

    def put(self, text: str, name: str) -> Path:
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def cli(self, *args: str, code: int = 0, timeout: float | None = None) -> dict[str, object]:
        process = subprocess.run([sys.executable, str(SCRIPTS / "qa.py"), *args, "--repo", str(self.repo)],
                                 env=self.env, capture_output=True, text=True, check=False, timeout=timeout)
        self.assertEqual(process.returncode, code, process.stdout + process.stderr)
        for value in ("admin-secret", "login-value", "host-secret", "sid-secret", "unused-secret", "token-", "Aa1!"):
            self.assertNotIn(value, process.stdout + process.stderr)
        return json.loads(process.stdout)

    def trust(self) -> None:
        self.cli("trust", "accept", str(self.cli("config")["trust_hash"]))

    def start(self) -> tuple[str, Path]:
        result = self.cli("run", "start", str(self.plan))
        return str(result["run"]), Path(str(result["dir"]))

    def dispatch(self, run: str, section: str = "BE") -> dict[str, object]:
        return self.cli("dispatch", "--run", run, "tester", "--section", section, "--phase", "baseline")

    def ledger(self) -> list[dict[str, object]]:
        return json.loads((self.root / "state/av-marketplace/qa-accounts.json").read_text())[str(self.repo.resolve())]

    def channel(self, directory: Path) -> dict[str, str]:
        return json.loads((directory / "secrets.json").read_text())

    def shell_channel(self, directory: Path) -> dict[str, str]:
        return dict(shlex.split(line)[1].split("=", 1) for line in (directory / "secrets.env").read_text().splitlines())

    def test_cleartext_request_rejected_before_sending_credentials(self) -> None:
        request = Request(
            f"http://localhost.:{self.server.server_port}/create",
            data=json.dumps({"password": "generated-test-password"}).encode(),
            headers={"x-key": "admin-secret", "Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(ConfigError):
            http_request(request)
        self.assertEqual(self.server.events, [])

    def test_token_only_lifecycle_across_processes_and_private_state(self) -> None:
        run, directory = self.start()
        self.cli("accounts", "provision", "--run", run)
        private = json.loads((directory / "accounts.private.json").read_text())
        self.assertEqual(self.channel(directory), {"QA_USER_TOKEN": "token-1"})
        self.assertEqual(self.shell_channel(directory), {"QA_USER_TOKEN": "token-1"})
        for expected in (2, 3):
            result = self.dispatch(run)
            self.assertEqual(result["refreshed"], ["user"])
            self.assertEqual(self.channel(directory), {"QA_USER_TOKEN": f"token-{expected}"})
            self.assertEqual(self.shell_channel(directory), {"QA_USER_TOKEN": f"token-{expected}"})
            self.assertNotIn("QA_USER_PASSWORD", (directory / "secrets.env").read_text())
        later = json.loads((directory / "accounts.private.json").read_text())
        self.assertEqual(private["user"]["password"], later["user"]["password"])
        self.assertNotEqual(private["user"]["issued_at"], later["user"]["issued_at"])
        self.assertEqual(len(private["user"]["password"]), 28)
        self.assertNotIn(private["user"]["password"], json.dumps(self.ledger()))
        self.assertNotIn("issued_at", json.dumps(self.ledger()))
        self.assertFalse((self.repo / "unused-source-ran").exists())
        self.assertEqual(self.cli("accounts", "teardown", "--run", run)["deleted"], ["user"])
        self.assertEqual([p.split("?")[0] for p, _ in self.server.events], ["/create", "/login", "/login", "/login", "/delete/account-id"])
        for name in ("secrets.env", "secrets.json", "load.sh", "accounts.private.json", "redact-names"):
            self.assertEqual((directory / name).stat().st_mode & 0o777, 0o600)
        log = (directory / "engine.log").read_text() if (directory / "engine.log").exists() else ""
        for secret in ("admin-secret", "login-value", "token-1", private["user"]["password"]):
            self.assertNotIn(secret, log)

    def test_refresh_preserves_other_sections_values_and_database(self) -> None:
        self.plan.write_text(PLAN + '''- **DB Check:** SELECT 1
- **Request payload:** $QA_LOGIN
## FE Test Scenarios
### FE-01: Login
- **URL:** /items
- **Steps:** Use $QA_OTHER_TOKEN
- **Expected:** Items displayed. (src/app.py:1)
''')
        self.put(self.config + '''[env.database]
kind = "postgres"
host = "127.0.0.1"
port = 54322
user = "tester"
name = "testdb"
password = "literal:db-secret"
''', ".av/config.toml")
        self.trust()
        run, directory = self.start()
        self.cli("accounts", "provision", "--run", run)
        before = {
            "QA_USER_TOKEN": "token-1", "QA_OTHER_TOKEN": "token-2", "QA_LOGIN": "login-value",
            "PGHOST": "127.0.0.1", "PGPORT": "54322", "PGUSER": "tester",
            "PGDATABASE": "testdb", "PGPASSWORD": "db-secret",
        }
        self.assertEqual(self.channel(directory), before)
        self.assertEqual(self.shell_channel(directory), before)
        names = (directory / "redact-names").read_bytes()
        for section, token, expected in (("BE", "QA_USER_TOKEN", "token-3"), ("FE", "QA_OTHER_TOKEN", "token-4")):
            self.dispatch(run, section)
            before[token] = expected
            self.assertEqual(self.channel(directory), before)
            self.assertEqual(self.shell_channel(directory), before)
            self.assertNotIn("QA_USER_PASSWORD", (directory / "secrets.env").read_text())
        self.assertEqual(names, (directory / "redact-names").read_bytes())

    def test_conflict_retries_once_with_distinct_identity(self) -> None:
        self.server.mode = "conflict"
        run, _ = self.start()
        self.cli("accounts", "provision", "--run", run)
        attempts = [payload["email"] for path, payload in self.server.events if path == "/create"]
        self.assertEqual(len(attempts), 2)
        self.assertNotEqual(*attempts)
        self.assertEqual(self.cli("accounts", "teardown", "--run", run)["deleted"], ["user"])

    def test_failed_committed_create_is_pending_never_retried(self) -> None:
        for mode, key in (("500-id", "deleted"), ("500-no-id", "unresolved")):
            with self.subTest(mode=mode):
                self.server.mode = mode
                self.server.events.clear()
                run, directory = self.start()
                self.cli("accounts", "provision", "--run", run, code=1)
                self.assertEqual(len(self.server.events), 1)
                record = self.ledger()[-1]
                self.assertEqual(record["status"], "pending")
                self.assertFalse((directory / "secrets.env").exists())
                self.assertEqual(self.cli("accounts", "teardown", "--run", run)[key], ["user"])
                self.cli("run", "end", "--run", run)
                (self.root / "state/av-marketplace/qa-accounts.json").unlink()

    def test_unavailable_create_service_is_not_an_invalid_request(self) -> None:
        self.server.mode = "unavailable"
        run, _ = self.start()
        result = self.cli("accounts", "provision", "--run", run, code=1, timeout=5)
        self.assertIn("unavailable", result["error"])
        self.assertNotIn("invalid HTTP request", result["error"])

    def test_required_missing_output_does_not_write_or_dispatch(self) -> None:
        self.server.mode = "missing"
        run, directory = self.start()
        self.cli("accounts", "provision", "--run", run, code=1)
        self.assertFalse((directory / "secrets.env").exists())
        self.server.mode = "ok"
        self.cli("accounts", "provision", "--run", run)
        before = (directory / "secrets.env").read_bytes()
        self.server.mode = "missing"
        self.cli("dispatch", "--run", run, "tester", "--section", "BE", "--phase", "baseline", code=1)
        self.assertEqual(before, (directory / "secrets.env").read_bytes())
        sidecar = json.loads(next((self.repo / "docs/testing/reports").glob("*-loop-state.json")).read_text())
        self.assertEqual(sidecar["dispatch_count"], 0)

    def test_shared_literal_password_provisions_and_reaches_create_and_login(self) -> None:
        password = "Pass-1234!"
        self.put(self.config.replace('password = "generate"', f'password = "literal:{password}"'), ".av/config.toml")
        self.plan.write_text(PLAN + '- **Steps:** $QA_USER_PASSWORD\n')
        self.assertEqual(self.cli("config")["state"], "ok")
        self.trust()
        run, directory = self.start()
        self.cli("accounts", "provision", "--run", run)
        self.assertEqual([payload["password"] for path, payload in self.server.events if path in {"/create", "/login"}],
                         [password, password])
        self.assertEqual(self.channel(directory)["QA_USER_PASSWORD"], password)
        self.assertEqual(json.loads((directory / "accounts.private.json").read_text())["user"]["password"], password)
        self.assertNotIn(password, json.dumps(self.ledger()))
        self.cli("accounts", "teardown", "--run", run)
        self.cli("run", "end", "--run", run)

    def test_loader_fails_closed_and_shell_quotes_password_and_cookies(self) -> None:
        self.plan.write_text(PLAN + '- **Steps:** $QA_USER_PASSWORD $QA_USER_COOKIE $QA_USER_COOKIE_HOST_SESSION $QA_USER_COOKIE_CONNECT_SID\n')
        self.put('[qa.accounts]\npassword = "literal:it\'s-a-password"\n', ".av/local.toml")
        self.trust()
        run, directory = self.start()
        self.cli("accounts", "provision", "--run", run)
        loader = directory / "load.sh"
        result = subprocess.run(["/bin/bash", "-c", f'. "{loader}" QA_USER_PASSWORD || exit 1; printf "%s" "$QA_USER_PASSWORD"'], capture_output=True, text=True)
        self.assertEqual(result.stdout, "it's-a-password")
        self.assertEqual(result.returncode, 0, result.stderr)
        channel = self.channel(directory)
        self.assertEqual(channel["QA_USER_COOKIE"], "__Host-session=host-secret; connect.sid=sid-secret")
        self.assertEqual(channel["QA_USER_COOKIE_HOST_SESSION"], "host-secret")
        self.assertEqual(channel["QA_USER_COOKIE_CONNECT_SID"], "sid-secret")
        original = (directory / "secrets.env").read_bytes()
        for mode in ("missing", "unreadable", "symlink", "incomplete"):
            with self.subTest(mode=mode):
                envfile = directory / "secrets.env"
                envfile.unlink(missing_ok=True)
                if mode == "unreadable":
                    envfile.write_bytes(original)
                    envfile.chmod(0)
                elif mode == "symlink":
                    other = directory / "other.env"
                    other.write_bytes(original)
                    envfile.symlink_to(other)
                elif mode == "incomplete":
                    envfile.write_text("export QA_USER_EMAIL='test'\n")
                process = subprocess.run(["/bin/bash", "-c", f'. "{loader}" QA_USER_TOKEN'], env={**self.env, "QA_USER_TOKEN": "inherited-token"}, capture_output=True, text=True)
                self.assertNotEqual(process.returncode, 0)
                self.assertNotIn("inherited-token", process.stdout + process.stderr)

    def test_zero_personas_values_and_db_still_write_channel(self) -> None:
        self.plan.write_text(PLAN.replace("Authorization: Bearer $QA_USER_TOKEN", "$QA_LOGIN") + '- **DB Check:** SELECT 1\n')
        self.put(self.config + '[env.database]\nkind="sqlite"\npath="db.sqlite"\n', ".av/config.toml")
        self.trust()
        run, directory = self.start()
        self.cli("accounts", "provision", "--run", run)
        self.assertEqual(self.channel(directory), {"QA_LOGIN": "login-value", "SQLITE_DB": str(self.repo.resolve() / "db.sqlite")})
        self.assertEqual(self.shell_channel(directory), {"QA_LOGIN": "login-value", "SQLITE_DB": str(self.repo.resolve() / "db.sqlite")})
        self.assertNotIn("QA_USER_PASSWORD", (directory / "secrets.env").read_text())
        self.assertTrue((directory / "load.sh").exists())
        self.assertTrue((directory / "redact-names").exists())
        self.assertFalse((self.repo / "unused-source-ran").exists())

    def test_deny_and_untrusted_and_redirect_refuse(self) -> None:
        self.put(self.config.replace('mutations = "allow"', 'mutations = "deny"'), ".av/config.toml")
        self.trust()
        run, _ = self.start()
        self.cli("accounts", "provision", "--run", run, code=1)
        self.assertEqual(self.server.events, [])
        self.cli("run", "end", "--run", run)
        self.put(self.config, ".av/config.toml")
        self.trust()
        run, _ = self.start()
        self.server.mode = "redirect"
        self.cli("accounts", "provision", "--run", run, code=1)
        self.assertEqual([p for p, _ in self.server.events], ["/create"])

    def test_drift_refuses_provision_but_cleanup_uses_recorded_commands(self) -> None:
        config = self.config + '''[env.services]
health = ["api:/health"]
up = 'touch service-up; printf "%s" "$AV_ADMIN"'
prepare = ["touch prepared"]
down = "touch recorded-down"
'''
        self.put(config, ".av/config.toml")
        self.trust()
        run, directory = self.start()
        self.assertTrue(self.cli("services", "check", "--run", run)["up"])
        self.cli("services", "up", "--run", run)
        self.cli("services", "prepare", "--run", run)
        self.cli("accounts", "provision", "--run", run)
        self.put(config.replace("/delete/{id}", "/different/{id}").replace("touch recorded-down", "touch wrong-down").replace(self.origin, "http://127.0.0.1:1"), ".av/config.toml")
        self.assertEqual(self.cli("accounts", "provision", "--run", run, code=1)["error"], "config changed during run")
        self.trust()
        self.assertEqual(self.cli("accounts", "teardown", "--run", run)["deleted"], ["user"])
        self.cli("services", "down", "--run", run)
        self.assertTrue((self.repo / "recorded-down").exists())
        self.assertFalse((self.repo / "wrong-down").exists())
        self.assertNotIn("admin-secret", (directory / "engine.log").read_text())

    def test_interrupted_run_ledger_survives_rebaseline_and_changed_recipes_are_left(self) -> None:
        for change in ("plan", "delete", "origin"):
            with self.subTest(change=change):
                self.put(self.config, ".av/config.toml")
                self.trust()
                run, _ = self.start()
                self.cli("accounts", "provision", "--run", run)
                self.cli("run", "end", "--run", run)
                self.plan.write_text(PLAN + f"\nchanged {change}\n")
                if change == "delete":
                    self.put(self.config.replace("/delete/{id}", "/other/{id}"), ".av/config.toml")
                if change == "origin":
                    self.put(self.config.replace(f'api = "{self.origin}"', f'api = "http://127.0.0.1:1"\nold = "{self.origin}"'), ".av/config.toml")
                self.trust()
                current, _ = self.start()
                result = self.cli("accounts", "teardown", "--run", current)
                self.assertEqual(result["deleted" if change == "plan" else "left"], ["user"])
                self.cli("run", "end", "--run", current)
                (self.root / "state/av-marketplace/qa-accounts.json").unlink()

    def test_delegated_command_delete_keeps_pending_identity_without_id_unresolved(self) -> None:
        self.put('touch delete-helper-ran\n', "scripts/delete-user.sh")
        config = self.config[:self.config.index("[qa.accounts.create]")] + '''[qa.accounts.create]
kind="command"
run="exit 1"
[qa.accounts.login]
kind="command"
run="printf '{\\"token\\":\\"command-token\\"}'"
outputs=["token"]
[qa.accounts.delete]
kind="command"
run="sh scripts/delete-user.sh"
'''
        self.put(config, ".av/config.toml")
        self.trust()
        run, _ = self.start()
        self.cli("accounts", "provision", "--run", run, code=1)
        result = self.cli("accounts", "teardown", "--run", run)
        self.assertEqual(result, {"deleted": [], "left": [], "unresolved": ["user"]})
        self.assertFalse((self.repo / "delete-helper-ran").exists())
        self.assertEqual(self.ledger()[0]["status"], "pending")

    def test_delegated_login_resolves_declared_dependencies_without_exposing_them(self) -> None:
        self.put('test "$QA_LOGIN" = login-value && test "$AV_admin" = admin-secret && '
                 'printf \'{"token":"command-token"}\'\n', "scripts/login.sh")
        config = self.config[:self.config.index("[qa.accounts.login]")]
        config = config.replace('ADMIN =', 'admin =').replace('{secret.ADMIN}', '{secret.admin}')
        config += '''[qa.accounts.login]
kind="command"
run="sh scripts/login.sh"
env=["value.LOGIN", "secret.admin"]
outputs=["token"]
'''
        self.env["QA_LOGIN"] = "stale-inherited-value"
        self.put(config, ".av/config.toml")
        self.trust()
        run, directory = self.start()
        self.cli("accounts", "provision", "--run", run)
        self.assertEqual(self.dispatch(run)["refreshed"], ["user"])
        self.assertEqual(self.channel(directory), {"QA_USER_TOKEN": "command-token"})
        self.assertFalse((self.repo / "unused-source-ran").exists())

    def test_static_command_recipes_and_delete_environment(self) -> None:
        self.config = '''version=1
[env.targets]
api="http://localhost:8000"
[qa.defaults]
be_target="api"
[qa.accounts]
personas=["user"]
email="qa+{run}-{persona}@test.local"
password="generate"
[qa.accounts.create]
kind="command"
run="printf '{\\"id\\":\\"command-id\\"}'"
outputs=["id"]
[qa.accounts.confirm]
kind="command"
run="test \\\"$QA_ID\\\" = command-id && touch confirmed"
[qa.accounts.login]
kind="command"
run="test -f confirmed && test -n \\\"$QA_EMAIL\\\" && test -n \\\"$QA_PASSWORD\\\" && printf '{\\"token\\":\\"command-token\\"}'"
outputs={token=true}
[qa.accounts.delete]
kind="command"
run="test -z \\\"${QA_PASSWORD+x}\\\" && test \\\"$QA_ID\\\" = command-id && touch deleted"
'''
        self.put(self.config, ".av/config.toml")
        self.trust()
        run, directory = self.start()
        self.cli("accounts", "provision", "--run", run)
        self.assertEqual(self.channel(directory), {"QA_USER_TOKEN": "command-token"})
        self.assertIsNone(self.ledger()[0]["origin"])
        self.assertEqual(self.cli("accounts", "teardown", "--run", run)["deleted"], ["user"])
        self.assertTrue((self.repo / "deleted").exists())
        self.cli("run", "end", "--run", run)
        self.put('[qa.accounts.static.user]\nemail="literal:static@test.local"\npassword="literal:static-secret"\n', ".av/local.toml")
        self.trust()
        run, directory = self.start()
        result = self.cli("accounts", "provision", "--run", run)
        self.assertTrue(result["personas"][0]["static"])
        self.dispatch(run)
        self.assertEqual(len(self.ledger()), 1)

    def test_delete_password_placeholder_is_config_error(self) -> None:
        self.put(self.config.replace("/delete/{id}", "/delete/{password}"), ".av/config.toml")
        report = self.cli("config", code=2)
        self.assertTrue(any(e["key"] == "qa.accounts.delete.path" for e in report["errors"]))

    def test_transitive_command_sources_resolve_in_each_process_without_exposing_dependencies(self) -> None:
        self.put('echo admin >> source-count; printf "%s" "$QA_ADMIN_VALUE"\n', "scripts/admin-source.sh")
        self.put('echo login >> source-count; printf "%s" "$QA_LOGIN_INPUT"\n', "scripts/login-source.sh")
        config = self.config.replace('ADMIN = "env:AV_ADMIN"', 'ADMIN = "cmd:sh scripts/admin-source.sh"')
        config = config.replace('LOGIN = "env:AV_LOGIN"', 'LOGIN = "cmd:sh scripts/login-source.sh"\nADMIN_VALUE = "env:AV_ADMIN"\nLOGIN_INPUT = "env:AV_LOGIN"')
        config += '[env.source_env]\n"env.secrets.ADMIN"=["value.ADMIN_VALUE"]\n"env.values.LOGIN"=["value.LOGIN_INPUT"]\n'
        self.put(config, ".av/config.toml")
        self.trust()
        run, directory = self.start()
        self.cli("accounts", "provision", "--run", run)
        self.dispatch(run)
        self.dispatch(run)
        self.cli("accounts", "teardown", "--run", run)
        counts = (self.repo / "source-count").read_text().splitlines()
        self.assertEqual(counts.count("admin"), 2)
        self.assertEqual(counts.count("login"), 3)
        self.assertEqual(set(self.channel(directory)), {"QA_USER_TOKEN"})
        log = (directory / "engine.log").read_text()
        self.assertNotIn("admin-secret", log)
        self.assertNotIn("login-value", log)

    def test_refresh_updates_all_auth_fields_of_section_personas_without_widening_channel(self) -> None:
        self.plan.write_text(PLAN + '''## FE Test Scenarios
### FE-01: Cookie login
- **URL:** /items
- **Steps:** Use $QA_USER_COOKIE
- **Expected:** Items shown. (src/app.py:1)
''')
        run, directory = self.start()
        self.cli("accounts", "provision", "--run", run)
        channel = directory / "secrets.json"
        values = json.loads(channel.read_text())
        values["QA_USER_COOKIE"] = "obsolete-cookie"
        channel.write_text(json.dumps(values))
        self.dispatch(run)
        self.assertEqual(set(self.channel(directory)), {"QA_USER_TOKEN", "QA_USER_COOKIE"})
        self.assertEqual(self.channel(directory)["QA_USER_COOKIE"], "__Host-session=host-secret; connect.sid=sid-secret")

    def test_empty_requested_cookie_is_not_a_valid_cookie_header(self) -> None:
        self.plan.write_text(PLAN.replace("$QA_USER_TOKEN", "$QA_USER_COOKIE"))
        self.server.mode = "missing-cookie"
        run, directory = self.start()
        self.cli("accounts", "provision", "--run", run, code=1)
        self.assertFalse((directory / "secrets.env").exists())

    def test_untrusted_cleanup_leaves_accounts_but_runs_recorded_down(self) -> None:
        config = self.config + '[env.services]\nup="touch up"\ndown="touch down"\n'
        self.put(config, ".av/config.toml")
        self.trust()
        run, _ = self.start()
        self.cli("accounts", "provision", "--run", run)
        self.cli("services", "up", "--run", run)
        self.put(config.replace("touch up", "touch different"), ".av/config.toml")
        self.assertEqual(self.cli("accounts", "teardown", "--run", run)["left"], ["user"])
        self.cli("services", "down", "--run", run)
        self.assertTrue((self.repo / "down").exists())

    def test_services_resolve_needed_values_and_down_only_after_up(self) -> None:
        config = self.config + '''[env.services]
health=["api:/health"]
up='sh scripts/service.sh up'
prepare=['sh scripts/service.sh prepared']
down="touch down"
env={up=["value.LOGIN", "secret.ADMIN"], prepare=["value.LOGIN", "secret.ADMIN"]}
'''
        self.put('test "$QA_LOGIN" = login-value && test "$AV_ADMIN" = admin-secret || exit 1\n'
                 'touch "$1"\n', "scripts/service.sh")
        self.put(config, ".av/config.toml")
        self.trust()
        run, directory = self.start()
        self.assertFalse(self.cli("services", "down", "--run", run)["ran"])
        self.assertFalse((self.repo / "down").exists())
        self.cli("services", "up", "--run", run)
        self.cli("services", "prepare", "--run", run)
        self.assertTrue((self.repo / "prepared").exists())
        self.assertFalse((self.repo / "unused-source-ran").exists())
        self.assertNotIn("login-value", (directory / "engine.log").read_text())

    def test_accounts_and_health_probes_ignore_environment_proxies(self) -> None:
        with AccountServer() as unused_proxy:
            proxy = f"http://127.0.0.1:{unused_proxy.server_port}"
        self.env.update({"http_proxy": proxy, "HTTP_PROXY": proxy, "all_proxy": proxy,
                         "ALL_PROXY": proxy, "no_proxy": "", "NO_PROXY": ""})
        self.put(self.config + '[env.services]\nhealth=["api:/health"]\n', ".av/config.toml")
        self.trust()
        run, directory = self.start()
        with self.subTest(operation="provision"):
            self.cli("accounts", "provision", "--run", run)
            self.assertEqual([path for path, _ in self.server.events], ["/create", "/login"])
            self.assertEqual(self.shell_channel(directory), {"QA_USER_TOKEN": "token-1"})
        with self.subTest(operation="check"):
            self.assertEqual(self.cli("services", "check", "--run", run),
                             {"up": True, "probes": [{"target": "api", "path": "/health", "status": 200}]})

    def test_services_up_returns_without_killing_background_child(self) -> None:
        self.put(self.config + '''[env.services]
up='sleep 60 & echo $! > service.pid; echo started; printf "%s" "$AV_ADMIN"'
down="kill $(cat service.pid)"
''', ".av/config.toml")
        self.trust()
        run, directory = self.start()
        pid_file = self.repo / "service.pid"
        try:
            self.assertEqual(self.cli("services", "up", "--run", run, timeout=5), {"ran": True, "exit": 0})
            os.kill(int(pid_file.read_text()), 0)
            log = (directory / "engine.log").read_text()
            self.assertNotIn("admin-secret", log)
            self.cli("services", "down", "--run", run)
        finally:
            if pid_file.exists():
                try:
                    os.kill(int(pid_file.read_text()), signal.SIGTERM)
                except ProcessLookupError:
                    pass

    def test_source_failure_has_no_values_in_errors_or_log(self) -> None:
        config = self.config.replace('LOGIN = "env:AV_LOGIN"', 'LOGIN = \'cmd:printf "%s" "$AV_LOGIN"; printf "%s" "$AV_ADMIN" >&2; exit 1\'')
        self.put(config, ".av/config.toml")
        self.trust()
        run, directory = self.start()
        self.cli("accounts", "provision", "--run", run, code=1)
        self.assertFalse((directory / "secrets.env").exists())
        log = (directory / "engine.log").read_text()
        self.assertNotIn("login-value", log)
        self.assertNotIn("admin-secret", log)

    def test_untrusted_subset_cannot_run_accounts_or_services(self) -> None:
        self.put(self.config + '[env.services]\nup="touch untrusted-up"\n', ".av/config.toml")
        self.trust()
        run, _ = self.start()
        (self.root / "state/av-marketplace/trust.json").unlink()
        for command, operation in (("accounts", "provision"), ("services", "check"), ("services", "up"), ("services", "prepare")):
            result = self.cli(command, operation, "--run", run, code=1)
            self.assertEqual(result["error"], "trust required")
        self.assertFalse((self.repo / "untrusted-up").exists())
        self.assertEqual(self.server.events, [])

    def test_form_and_confirm_recipes_execute_before_login(self) -> None:
        config = self.config.replace('json = {email = "{email}", password = "{password}"}', 'form = {email = "{email}", password = "{password}"}')
        config += '''[qa.accounts.confirm]
kind="http"
target="api"
method="POST"
path="/confirm"
headers={x-key="{value.LOGIN}"}
json={id="{id}"}
expect=[200]
'''
        self.put(config, ".av/config.toml")
        self.trust()
        run, _ = self.start()
        self.cli("accounts", "provision", "--run", run)
        self.assertEqual([p for p, _ in self.server.events], ["/create", "/confirm", "/login"])
        self.assertEqual(self.server.events[1][1], {"id": "account-id"})
        self.assertEqual(self.server.events[0][1]["password"], self.server.events[2][1]["password"])

    def test_command_cookie_outputs_refresh_without_widening_channel(self) -> None:
        config = self.config[:self.config.index("[qa.accounts.login]")]
        self.env.update({"AV_COMMAND_TOKEN": "command-token", "AV_HOST_COOKIE": "host-secret", "AV_SID_COOKIE": "sid-secret"})
        command = 'printf \'{"token":"%s","cookies":{"__Host-session":"%s","connect.sid":"%s"}}\' "$AV_COMMAND_TOKEN" "$AV_HOST_COOKIE" "$AV_SID_COOKIE"'
        config += '[qa.accounts.login]\nkind="command"\nrun=' + json.dumps(command) + '\noutputs={token=true,cookies=["__Host-session","connect.sid"]}\n'
        self.put(config, ".av/config.toml")
        self.plan.write_text(PLAN + '- **Steps:** $QA_USER_COOKIE_CONNECT_SID\n')
        self.trust()
        run, directory = self.start()
        self.cli("accounts", "provision", "--run", run)
        self.cli("accounts", "refresh", "--run", run)
        self.assertEqual(self.channel(directory), {"QA_USER_TOKEN": "command-token", "QA_USER_COOKIE_CONNECT_SID": "sid-secret"})

    def test_later_service_process_masks_existing_channel_values_before_tail_truncation(self) -> None:
        self.put("sensitive-start-" + "x" * 9000 + "-sensitive-end", "display.txt")
        config = self.config.replace('[env.values]', '[env.values]\nDISPLAY="cmd:cat display.txt"')
        config += '[env.services]\nup=\'cat "$TMPDIR"/qa-run-*/secrets.json\'\n'
        self.put(config, ".av/config.toml")
        self.plan.write_text(PLAN + '- **Steps:** $QA_DISPLAY\n')
        self.trust()
        run, directory = self.start()
        self.cli("accounts", "provision", "--run", run)
        self.cli("services", "up", "--run", run)
        log = (directory / "engine.log").read_text()
        self.assertNotIn("sensitive-end", log)
        self.assertNotIn("xxx", log)

    def test_takeover_after_interruption_and_plan_rebaseline_deletes_original_run_identity(self) -> None:
        interrupted, old_directory = self.start()
        self.cli("accounts", "provision", "--run", interrupted)
        self.plan.write_text(PLAN + "\nChanged after interruption\n")
        replacement = self.cli("run", "start", str(self.plan), "--takeover", interrupted)
        self.assertEqual(replacement["idempotency"], "rebaseline")
        self.assertFalse(old_directory.exists())
        self.assertEqual(self.cli("accounts", "teardown", "--run", str(replacement["run"]))["deleted"], ["user"])
        self.assertEqual(self.server.events[-1][0], f"/delete/account-id?run={interrupted}")

    def test_health_probe_boundary_is_500(self) -> None:
        self.put(self.config + '[env.services]\nhealth=["api:/health"]\n', ".av/config.toml")
        self.trust()
        run, _ = self.start()
        for status, up in ((200, True), (499, True), (500, False)):
            with self.subTest(status=status):
                self.server.health_status = status
                result = self.cli("services", "check", "--run", run)
                self.assertEqual(result["up"], up)
                self.assertEqual(result["probes"], [{"target": "api", "path": "/health", "status": status}])


if __name__ == "__main__":
    unittest.main()
