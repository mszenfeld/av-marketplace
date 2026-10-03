"""Process-bound user channel, registration ledger and cleanup contracts."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
from threading import Thread
import unittest
from urllib.parse import parse_qs
from urllib.parse import urlsplit
from urllib.request import Request

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/engine/scripts"
sys.path.insert(0, str(SCRIPTS))

from av_config.errors import ConfigError
from qa_engine.recipes import http_request

PLAN = '''# Test Plan
## Users
- admin: existing — administrator
- owner: registered — plain user
## BE Test Scenarios
### BE-01: Request
- **Writes:** yes
- **Preconditions:** Register qa+$QA_TAG-owner@test.local with $QA_NEW_PASSWORD; login with $QA_ADMIN_EMAIL and $QA_ADMIN_PASSWORD.
- **Method:** GET /items
- **Expected:** 200 returned. (src/app.py:1)
'''
USER = '''[qa.users.admin]
email = "literal:admin@test.local"
password = "env:QA_ADMIN_SECRET"
id = "literal:admin-1"
description = "administrator"
'''
CLEANUP = '''[qa.cleanup]
kind = "http"
target = "backend"
method = "DELETE"
path = "/users/{email}"
headers = { apikey = "{secret.ADMIN}" }
expect = [200, 404]
'''


class AccountServer(ThreadingHTTPServer):
    events: list[tuple[str, dict[str, object]]]
    mode: str
    health_status: int

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), AccountHandler)
        self.events = []
        self.mode = "ok"
        self.health_status = 200


class AccountHandler(BaseHTTPRequestHandler):
    server: AccountServer

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        self.reply(self.server.health_status, {})

    def do_DELETE(self) -> None:
        self.server.events.append((self.path, {name.lower(): value for name, value in self.headers.items()}))
        if self.server.mode == "unavailable":
            self.close_connection = True
        elif self.server.mode == "redirect":
            self.reply(302, {}, location="/other")
        elif self.server.mode == "500":
            self.reply(500, {})
        elif self.server.mode == "missing":
            self.reply(404, {})
        else:
            self.reply(200 if self.headers.get("apikey") == "admin-secret" else 403, {})

    def reply(self, status: int, payload: object, *, location: str | None = None) -> None:
        self.send_response(status)
        if location:
            self.send_header("Location", location)
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())


class UserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.env = {**os.environ, "TMPDIR": str(self.root), "XDG_STATE_HOME": str(self.root / "state"),
                    "AV_ADMIN": "admin-secret", "QA_ADMIN_SECRET": "private-password"}
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-c", "user.name=QA", "-c", "user.email=qa@test.local", "commit", "--allow-empty", "-qm", "fixture"], cwd=self.repo, check=True)
        self.put(".av/local.toml\n", ".git/info/exclude")
        self.server = self.new_server()
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        self.base = f'''version = 1
[env.targets]
backend = "{self.origin}"
ui = "http://localhost:5173"
[env.secrets]
ADMIN = "env:AV_ADMIN"
[qa]
fix = "approve"
mutations = "allow"
'''
        self.config = self.base + USER + CLEANUP
        self.put(self.config, ".av/config.toml")
        self.plan = self.put(PLAN, "docs/testing/plans/users-plan.md")
        self.trust()

    def new_server(self) -> AccountServer:
        server = AccountServer()
        Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def put(self, text: str, name: str) -> Path:
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def cli(self, *args: str, code: int = 0) -> dict[str, object]:
        process = subprocess.run([sys.executable, str(SCRIPTS / "qa.py"), *args, "--repo", str(self.repo)],
                                 env=self.env, capture_output=True, text=True, check=False)
        self.assertEqual(process.returncode, code, process.stdout + process.stderr)
        for value in ("admin-secret", "private-password", "Aa1!"):
            self.assertNotIn(value, process.stdout + process.stderr)
        return json.loads(process.stdout)

    def trust(self) -> None:
        self.cli("trust", "accept", str(self.cli("config")["trust_hash"]))

    def start(self) -> tuple[str, Path]:
        result = self.cli("run", "start", str(self.plan))
        run, directory = str(result["run"]), Path(str(result["dir"]))
        self.cli("users", "provision", "--run", run)
        return run, directory

    def dispatch(self, run: str, section: str = "BE") -> dict[str, object]:
        return self.cli("dispatch", "--run", run, "tester", "--section", section, "--phase", "baseline")

    def ledger(self) -> list[dict[str, object]]:
        return json.loads((self.root / "state/av-marketplace/qa-accounts.json").read_text())[str(self.repo.resolve())]

    def channel(self, directory: Path) -> dict[str, str]:
        return json.loads((directory / "secrets.json").read_text())

    def ingest(self, run: str, dispatch: dict[str, object], accounts: object) -> dict[str, object]:
        data = {"section": "BE", "scenarios": [{"id": "BE-01", "status": "PASS", "observed_status": 200}], "accounts": accounts}
        path = self.put("```json qa-results\n" + json.dumps(data) + "\n```\n", "results.md")
        return self.cli("ingest", "--run", run, "--dispatch", str(dispatch["dispatch"]), str(path))

    def register(self, run: str, dispatch: dict[str, object], *, id: str | None = "u1") -> str:
        email = f"qa+{dispatch['tag']}-owner@test.local"
        options = ["--id", id] if id is not None else []
        self.cli("users", "record", "--run", run, "--dispatch", str(dispatch["dispatch"]), "--email", email, *options)
        return email

    def test_provision_writes_users_values_and_new_password_only_for_plan_tokens(self) -> None:
        run, directory = self.start()
        result = self.cli("users", "provision", "--run", run)
        values = self.channel(directory)
        self.assertEqual(set(values), {"QA_ADMIN_EMAIL", "QA_ADMIN_PASSWORD", "QA_NEW_PASSWORD"})
        self.assertRegex(values["QA_NEW_PASSWORD"], r"^[A-Za-z0-9_-]{24}Aa1!$")
        self.assertEqual((directory / "redact-names").read_text().splitlines(), sorted(values))
        process = subprocess.run(["sh", "-c", f". {shlex.quote(str(directory / 'load.sh'))} QA_ADMIN_EMAIL && printf '%s' \"$QA_ADMIN_EMAIL\""], env=self.env, capture_output=True, text=True)
        self.assertEqual(process.stdout, "admin@test.local")
        self.assertEqual(result["users"], ["admin"])

    def test_dispatch_issues_a_distinct_tag_per_dispatch_and_no_refresh_key(self) -> None:
        run, directory = self.start()
        first, second = self.dispatch(run), self.dispatch(run)
        self.assertNotEqual(first["tag"], second["tag"])
        self.assertRegex(first["tag"], r"^[0-9a-f]{8}$")
        self.assertNotIn("refreshed", first)
        state = json.loads((directory / "run.json").read_text())
        sidecar = json.loads(Path(state["sidecar"]).read_text())
        self.assertEqual(sidecar["dispatches"][first["dispatch"]]["tag"], first["tag"])

    def test_dispatch_before_provision_can_be_retried(self) -> None:
        for partial_capture in (False, True):
            with self.subTest(partial_capture=partial_capture):
                started = self.cli("run", "start", str(self.plan))
                run, directory = str(started["run"]), Path(str(started["dir"]))
                captured = directory / "results" / "D001"
                result = self.cli("dispatch", "--run", run, "tester", "--section", "BE", "--phase", "baseline", code=1)
                self.assertEqual(result["error"], "run users provision before dispatch")
                self.assertFalse(captured.exists())
                self.cli("users", "provision", "--run", run)
                if partial_capture:
                    captured.mkdir(parents=True)
                    captured.chmod(0o755)
                    (captured / "captured.env").write_text("stale credentials")
                    (captured / "redact-names").write_text("stale names")
                dispatch = self.dispatch(run)
                self.assertEqual(dispatch["dispatch"], "D001")
                self.assertEqual(captured.stat().st_mode & 0o777, 0o700)
                self.assertEqual((captured / "captured.env").read_bytes(), b"")
                self.assertEqual((captured / "captured.env").stat().st_mode & 0o777, 0o600)
                self.assertEqual((captured / "redact-names").read_bytes(), (directory / "redact-names").read_bytes())
                self.assertEqual((captured / "redact-names").stat().st_mode & 0o777, 0o600)
                self.cli("run", "end", "--run", run)

    def test_ingest_records_tagged_accounts_and_rejects_the_rest(self) -> None:
        run, _ = self.start()
        dispatch = self.dispatch(run)
        tag = str(dispatch["tag"])
        bad = f"qa+{tag}-bad@test.local"
        result = self.ingest(run, dispatch, [
            {"email": f"qa+{tag}-owner@test.local", "id": "u1"},
            {"email": f"qa+{tag.upper()}-x@test.local", "id": None},
            {"email": "stranger@test.local", "id": None},
            {"email": "admin@test.local", "id": None},
            {"email": bad, "id": "x;drop"},
        ])
        self.assertEqual(result["accounts"], {"recorded": 2, "rejected": sorted(["admin@test.local", bad, "stranger@test.local"])})
        self.assertEqual(result["verdicts"], {"BE-01": "pass"})
        records = self.ledger()
        self.assertEqual(len(records), 2)
        for record in records:
            self.assertEqual((record["status"], record["attempts"], record["run_id"], record["tag"], record["dispatch"]), ("pending", 0, run, tag, dispatch["dispatch"]))

    def test_capture_helper_records_accounts_and_credentials_before_ingest(self) -> None:
        run, directory = self.start()
        dispatch = self.dispatch(run)
        captured = directory / "results" / str(dispatch["dispatch"])
        self.assertEqual(captured.stat().st_mode & 0o777, 0o700)
        self.assertEqual((captured / "captured.env").stat().st_mode & 0o777, 0o600)
        self.assertEqual((captured / "redact-names").read_text(), (directory / "redact-names").read_text())
        helper = ["sh", str(directory / "capture.sh"), str(dispatch["dispatch"])]
        value = "tok'en\\x"
        process = subprocess.run([*helper, "QA_CAPTURED_OWNER_TOKEN"], input=value + "\n", env=self.env, capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        sourced = subprocess.run(["sh", "-c", f". {shlex.quote(str(captured / 'captured.env'))}; printf '%s' \"$QA_CAPTURED_OWNER_TOKEN\""], capture_output=True, text=True)
        self.assertEqual(sourced.stdout, value)
        before = (captured / "captured.env").read_bytes()
        invalid = subprocess.run([*helper, "FOO"], input="invalid", capture_output=True, text=True)
        self.assertEqual(invalid.returncode, 1)
        self.assertEqual((captured / "captured.env").read_bytes(), before)
        email = f"qa+{dispatch['tag']}-owner@test.local"
        recorded = subprocess.run([*helper, "--account", email, ""], env=self.env, capture_output=True, text=True)
        self.assertEqual(recorded.returncode, 0, recorded.stdout + recorded.stderr)
        self.assertIsNone(self.ledger()[0]["id"])
        self.assertEqual(self.ledger()[0]["destination"], self.origin)
        subprocess.run([*helper, "--account", email, "u1"], env=self.env, check=True, capture_output=True)
        self.ingest(run, dispatch, [{"email": email, "id": "u1"}])
        self.assertEqual(len(self.ledger()), 1)
        self.assertEqual(self.ledger()[0]["id"], "u1")
        bad = subprocess.run([*helper, "--account", "stranger@test.local"], env=self.env, capture_output=True, text=True)
        self.assertEqual(bad.returncode, 1)
        self.assertIn("email not tagged for this dispatch", bad.stdout)
        self.cli("run", "end", "--run", run)
        run, _ = self.start()
        dispatch = self.dispatch(run)
        email2 = self.register(run, dispatch)
        result = self.cli("users", "teardown", "--run", run)
        self.assertEqual(result["deleted"], sorted([email, email2]))
        self.cli("run", "end", "--run", run)
        self.assertEqual(len(self.ledger()), 2)

    def test_invalid_accounts_value_invalidates_the_block(self) -> None:
        for accounts in ("nope", [{}], [{"email": 1}]):
            with self.subTest(accounts=accounts):
                run, directory = self.start()
                result = self.ingest(run, self.dispatch(run), accounts)
                self.assertIn("error", result)
                state = json.loads(Path(json.loads((directory / "run.json").read_text())["sidecar"]).read_text())
                self.assertEqual(state["scenario_reason"]["BE-01"], "cannot-confirm")
                self.cli("run", "end", "--run", run)

    def test_teardown_http_cleanup_deletes_by_email_and_reports_outcomes(self) -> None:
        for path in ("/users/{email}", "/users?email={email}"):
            with self.subTest(path=path):
                self.put(self.config.replace("/users/{email}", path), ".av/config.toml")
                self.trust()
                run, _ = self.start()
                dispatch = self.dispatch(run)
                email = self.register(run, dispatch)
                result = self.cli("users", "teardown", "--run", run)
                self.assertEqual(result, {"deleted": [email], "left": [], "manual": []})
                actual, headers = self.server.events[-1]
                self.assertEqual(headers["apikey"], "admin-secret")
                if "?" in path:
                    self.assertEqual(parse_qs(urlsplit(actual).query)["email"], [email])
                else:
                    self.assertEqual(actual, f"/users/qa%2B{dispatch['tag']}-owner%40test.local")
                self.assertEqual(self.cli("users", "teardown", "--run", run), {"deleted": [], "left": [], "manual": []})
                self.assertTrue(self.ledger()[-1]["deleted"])
                self.cli("run", "end", "--run", run)

    def test_record_rejects_invalid_and_configured_identities(self) -> None:
        run, _ = self.start()
        dispatch = self.dispatch(run)
        tagged = f"qa+{dispatch['tag']}-owner@test.local"
        for email, account_id, reason in (
            (f"qa+{dispatch['tag']}/owner@test.local", None, "invalid email"),
            (tagged, "u1;delete", "invalid id"),
            ("admin@test.local", None, "configured user"),
            ("stranger@test.local", None, "email not tagged for this dispatch"),
        ):
            with self.subTest(reason=reason):
                options = ["--id", account_id] if account_id is not None else []
                result = self.cli("users", "record", "--run", run, "--dispatch", str(dispatch["dispatch"]), "--email", email, *options, code=1)
                self.assertEqual(result["error"], reason)
        self.assertEqual(self.register(run, dispatch), tagged)
        self.assertEqual([record["email"] for record in self.ledger()], [tagged])

    def test_record_only_fills_missing_id_and_preserves_cleanup_state(self) -> None:
        run, _ = self.start()
        dispatch = self.dispatch(run)
        email = self.register(run, dispatch, id=None)
        self.server.mode = "500"
        self.assertEqual(self.cli("users", "teardown", "--run", run)["left"], [email])
        self.register(run, dispatch, id="u1")
        self.assertEqual((self.ledger()[0]["id"], self.ledger()[0]["attempts"]), ("u1", 1))
        self.server.mode = "ok"
        self.assertEqual(self.cli("users", "teardown", "--run", run)["deleted"], [email])
        self.register(run, dispatch, id="u2")
        record = self.ledger()[0]
        self.assertEqual((record["id"], record["attempts"], record["status"], record["deleted"]), ("u1", 1, "deleted", True))
        self.assertEqual(self.cli("users", "teardown", "--run", run), {"deleted": [], "left": [], "manual": []})

    def test_http_failures_count_attempts_and_never_follow_redirects(self) -> None:
        run, _ = self.start()
        email = self.register(run, self.dispatch(run))
        for attempt, mode in enumerate(("unavailable", "redirect", "500"), 1):
            self.server.mode = mode
            result = self.cli("users", "teardown", "--run", run)
            self.assertEqual(self.ledger()[0]["attempts"], attempt)
            self.assertEqual(result, {"deleted": [], "left": [email] if attempt < 3 else [], "manual": [email] if attempt == 3 else []})
            self.assertEqual(len(self.server.events), attempt)
        self.assertFalse(any(path == "/other" for path, _ in self.server.events))

    def test_http_cleanup_without_required_id_waits_then_uses_recorded_id(self) -> None:
        self.put(self.config.replace("/users/{email}", "/users/{email}/{id}"), ".av/config.toml")
        self.trust()
        run, _ = self.start()
        dispatch = self.dispatch(run)
        email = self.register(run, dispatch, id=None)
        self.assertEqual(self.cli("users", "teardown", "--run", run)["left"], [email])
        self.assertEqual(self.server.events, [])
        self.assertEqual(self.ledger()[0]["attempts"], 1)
        self.register(run, dispatch, id="u1")
        self.assertEqual(self.cli("users", "teardown", "--run", run)["deleted"], [email])
        self.assertTrue(self.server.events[-1][0].endswith("/u1"))

    def test_teardown_command_cleanup_sees_email_id_tag_and_no_password(self) -> None:
        self.put('test "$QA_EMAIL" = "qa+$QA_TAG-owner@test.local" && test "$QA_ID" = u1 && test -z "${QA_PASSWORD:-}" && test -z "${QA_NEW_PASSWORD:-}"\n', "scripts/cleanup.sh")
        self.put(self.base + USER + '[qa.cleanup]\nkind="command"\nrun="sh scripts/cleanup.sh"\n', ".av/config.toml")
        self.env.update(QA_PASSWORD="inherited-secret", QA_NEW_PASSWORD="inherited-new-secret")
        self.trust()
        run, directory = self.start()
        email = self.register(run, self.dispatch(run))
        self.assertEqual(self.cli("users", "teardown", "--run", run)["deleted"], [email])
        self.assertNotIn("private-password", (directory / "engine.log").read_text())

    def test_cleanup_destination_provenance_is_honoured(self) -> None:
        server2 = self.new_server()
        origin2 = f"http://127.0.0.1:{server2.server_port}"
        config = self.config.replace('[env.secrets]', f'backend2="{origin2}"\n[env.secrets]')
        self.put(config, ".av/config.toml")
        self.trust()
        run, _ = self.start()
        email = self.register(run, self.dispatch(run))
        self.put(config.replace('target = "backend"', 'target = "backend2"'), ".av/config.toml")
        self.trust()
        self.assertEqual(self.cli("users", "teardown", "--run", run)["left"], [email])
        self.assertEqual(self.ledger()[0]["attempts"], 0)
        self.put(config, ".av/config.toml")
        self.trust()
        self.assertEqual(self.cli("users", "teardown", "--run", run)["deleted"], [email])

    def test_failed_cleanup_counts_attempts_and_stops_after_three(self) -> None:
        self.put(self.base + USER + '[qa.cleanup]\nkind="command"\nrun="exit 1"\n', ".av/config.toml")
        self.trust()
        for attempt in range(1, 5):
            run, _ = self.start()
            if attempt == 1:
                email = self.register(run, self.dispatch(run))
            result = self.cli("users", "teardown", "--run", run)
            self.assertEqual(self.ledger()[0]["attempts"], min(attempt, 3))
            self.assertEqual(result["left"], [email] if attempt < 3 else [])
            self.assertEqual(result["manual"], [email] if attempt == 3 else [])
            self.cli("run", "end", "--run", run)
        self.assertEqual(self.ledger()[0]["status"], "manual-cleanup")

    def test_missing_cleanup_is_a_soft_gap_that_leaves_accounts(self) -> None:
        self.put(self.base + USER, ".av/config.toml")
        self.trust()
        checked = self.cli("plan", "check", str(self.plan))
        self.assertTrue(checked["ok"])
        self.assertTrue(checked["missing"]["cleanup"])
        self.assertEqual(checked["registrations"], ["owner"])
        run, _ = self.start()
        email = self.register(run, self.dispatch(run))
        self.assertEqual(self.cli("users", "teardown", "--run", run)["left"], [email])
        self.assertEqual(self.ledger()[0]["attempts"], 0)
        self.assertIsNone(self.ledger()[0]["destination"])
        self.put(self.config, ".av/config.toml")
        self.trust()
        self.assertEqual(self.cli("users", "teardown", "--run", run)["deleted"], [email])
        self.assertEqual(self.ledger()[0]["destination"], self.origin)

    def test_untrusted_config_leaves_accounts_without_counting_attempts(self) -> None:
        run, _ = self.start()
        email = self.register(run, self.dispatch(run))
        self.put(self.config.replace('fix = "approve"', 'fix = "off"'), ".av/config.toml")
        self.assertEqual(self.cli("users", "teardown", "--run", run)["left"], [email])
        self.assertEqual(self.ledger()[0]["attempts"], 0)
        self.trust()
        self.assertEqual(self.cli("users", "teardown", "--run", run)["deleted"], [email])

    def test_invalid_config_leaves_accounts_without_interpreting_destinations(self) -> None:
        for target in ("1", json.dumps(f"{self.origin}/x")):
            with self.subTest(target=target):
                run, directory = self.start()
                dispatch = self.dispatch(run)
                email = self.register(run, dispatch)
                self.put(self.config.replace(f'backend = "{self.origin}"', f"backend = {target}"), ".av/config.toml")
                self.assertEqual(self.cli("users", "teardown", "--run", run), {"deleted": [], "left": [email], "manual": []})
                ingested = f"qa+{dispatch['tag']}-ingested@test.local"
                result = self.ingest(run, dispatch, [{"email": ingested, "id": "u2"}])
                self.assertEqual(result["accounts"], {"recorded": 1, "rejected": []})
                self.assertEqual(result["verdicts"], {"BE-01": "pass"})
                state = json.loads(Path(json.loads((directory / "run.json").read_text())["sidecar"]).read_text())
                self.assertEqual(state["current"]["BE-01"], "pass")
                second = f"qa+{dispatch['tag']}-second@test.local"
                self.assertEqual(self.cli("users", "record", "--run", run, "--dispatch", str(dispatch["dispatch"]), "--email", second),
                                 {"recorded": True})
                records = {record["email"]: record for record in self.ledger() if record["run_id"] == run}
                self.assertEqual(set(records), {email, ingested, second})
                self.assertEqual(records[email]["destination"], self.origin)
                self.assertIsNone(records[ingested]["destination"])
                self.assertIsNone(records[second]["destination"])
                self.assertTrue(all(record["attempts"] == 0 for record in records.values()))
                self.put(self.config, ".av/config.toml")
                self.assertEqual(self.cli("users", "teardown", "--run", run)["deleted"], sorted([email, ingested, second]))
                self.cli("run", "end", "--run", run)

    def test_cleanup_destination_must_be_locked_by_this_run(self) -> None:
        run, _ = self.start()
        email = self.register(run, self.dispatch(run))
        server2 = self.new_server()
        config = self.config.replace(self.origin, f"http://127.0.0.1:{server2.server_port}")
        self.put(config, ".av/config.toml")
        self.trust()
        self.assertEqual(self.cli("users", "teardown", "--run", run)["left"], [email])
        self.assertEqual(self.ledger()[0]["attempts"], 0)
        self.put(self.config, ".av/config.toml")
        self.trust()
        self.assertEqual(self.cli("users", "teardown", "--run", run)["deleted"], [email])

    def test_repeated_provision_keeps_the_run_password(self) -> None:
        run, directory = self.start()
        password = self.channel(directory)["QA_NEW_PASSWORD"]
        self.cli("users", "provision", "--run", run)
        self.assertEqual(self.channel(directory)["QA_NEW_PASSWORD"], password)

    def test_http_cleanup_must_reference_email_and_refuses_cleartext_off_loopback(self) -> None:
        self.put(self.config.replace("/users/{email}", "/users/{id}"), ".av/config.toml")
        errors = self.cli("config", code=2)["errors"]
        self.assertIn({"file": ".av/config.toml", "key": "qa.cleanup", "error": "cleanup recipe must use {email}"}, errors)
        self.put(self.config.replace(self.origin, "http://staging.example.com"), ".av/config.toml")
        self.assertTrue(any(error["key"] == "env.targets.backend" for error in self.cli("config", code=2)["errors"]))
        with self.assertRaises(ConfigError):
            http_request(Request("http://staging.example.com/users", headers={"Authorization": "private"}))

    def test_zero_users_and_values_still_write_channel(self) -> None:
        self.plan.write_text('### BE-01: Fetch\n- **Writes:** no\n- **Method:** GET /items\n- **Expected:** 200. (src/app.py:1)\n')
        run, directory = self.start()
        self.assertEqual(set(self.channel(directory)), {"QA_NEW_PASSWORD"})
        self.assertEqual(self.dispatch(run)["scenarios"], ["BE-01"])
        self.cli("run", "end", "--run", run)
        self.plan.write_text(self.plan.read_text() + '- **DB Check:** SELECT 1\n')
        self.put(self.config + '[env.database]\nkind="sqlite"\npath="db.sqlite"\n', ".av/config.toml")
        self.trust()
        _, directory = self.start()
        self.assertEqual(self.channel(directory)["SQLITE_DB"], str(self.repo.resolve() / "db.sqlite"))

    def test_loader_fails_closed_and_shell_quotes_password(self) -> None:
        self.env["QA_ADMIN_SECRET"] = "pa'ss\\word"
        run, directory = self.start()
        command = f". {shlex.quote(str(directory / 'load.sh'))} QA_ADMIN_PASSWORD && printf '%s' \"$QA_ADMIN_PASSWORD\""
        process = subprocess.run(["sh", "-c", command], env=self.env, capture_output=True, text=True)
        self.assertEqual(process.stdout, "pa'ss\\word")
        (directory / "secrets.env").unlink()
        process = subprocess.run(["sh", "-c", command], env={**self.env, "QA_ADMIN_PASSWORD": "inherited"}, capture_output=True, text=True)
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(process.stdout, "")

    def test_services_run_with_inherited_environment_and_down_only_after_up(self) -> None:
        self.put(self.config + '[env.services]\nhealth=["backend:/"]\nup="printf \'%s\' \\\"$AV_ADMIN\\\" > up.txt"\nprepare=["touch prepared"]\ndown="touch down.txt"\n', ".av/config.toml")
        self.trust()
        run, _ = self.start()
        self.assertEqual(self.cli("services", "check", "--run", run), {"up": True, "probes": [{"target": "backend", "path": "/", "status": 200}]})
        self.server.health_status = 503
        self.assertEqual(self.cli("services", "check", "--run", run), {"up": False, "probes": [{"target": "backend", "path": "/", "status": 503}]})
        self.assertFalse(self.cli("services", "down", "--run", run)["ran"])
        self.cli("services", "up", "--run", run)
        self.assertEqual((self.repo / "up.txt").read_text(), "admin-secret")
        self.cli("services", "prepare", "--run", run)
        self.assertTrue((self.repo / "prepared").exists())
        self.cli("services", "down", "--run", run)
        self.assertTrue((self.repo / "down.txt").exists())

    def test_drift_refuses_provision_but_cleanup_uses_recorded_commands(self) -> None:
        self.put(self.config + '[env.services]\nhealth=["backend:/"]\nup="true"\ndown="touch down.txt"\n', ".av/config.toml")
        self.trust()
        run, _ = self.start()
        self.cli("services", "up", "--run", run)
        self.put(self.config.replace('fix = "approve"', 'fix = "off"'), ".av/config.toml")
        self.assertEqual(self.cli("users", "provision", "--run", run, code=1)["error"], "config changed during run")
        self.cli("services", "down", "--run", run)
        self.assertTrue((self.repo / "down.txt").exists())


if __name__ == "__main__":
    unittest.main()
