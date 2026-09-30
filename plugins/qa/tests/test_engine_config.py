"""Behavioral checks for marketplace config, trust, and the origin guard.

Run: uv run python -m unittest discover -s plugins/qa/tests -p test_engine_config.py
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/engine/scripts"
sys.path.insert(0, str(SCRIPTS))

import av_config
from qa_engine.config import Config
from qa_engine.targets import Targets

BASE = 'version = 1\n[env.targets]\napi = "http://localhost:8000"\n[qa]\n'


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.state = Path(self.tmp.name) / "state"

    def put(self, text: str, name: str = ".av/config.toml") -> Path:
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def config(self) -> Config:
        return Config(self.repo, state_home=self.state)

    def cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "qa.py"), *args, "--repo", str(self.repo)],
            env={**os.environ, "XDG_STATE_HOME": str(self.state)},
            capture_output=True, text=True, check=False,
        )

    def test_states_and_merge_provenance(self) -> None:
        self.assertEqual(self.config().report()["state"], "missing-file")
        self.put("version = 1\n")
        self.assertEqual(self.config().report()["state"], "missing-table")
        self.put('version = 2\n[qa]\n')
        self.assertEqual(self.config().report()["state"], "invalid")
        self.put(BASE + '[qa.budget]\niterations = 3\nminutes = 30\n')
        self.put('[qa.budget]\niterations = 5\n', ".av/local.toml")
        report = self.config().report()
        self.assertEqual(report["state"], "ok")
        self.assertEqual(report["budget"]["iterations"], 5)
        self.assertEqual(report["budget"]["minutes"], 30)
        self.assertEqual(report["provenance"]["qa.budget.iterations"], ".av/local.toml")

    def test_local_overrides_without_shared_file_need_bootstrap(self) -> None:
        self.put('[qa.budget]\niterations = 5\n', ".av/local.toml")
        report = self.config().report()
        self.assertEqual(report["state"], "missing-file")
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["budget"]["iterations"], 5)
        self.assertEqual(report["provenance"]["qa.budget.iterations"], ".av/local.toml")
        supplied = Config(self.repo, state_home=self.state, config_text=BASE)
        self.assertEqual(supplied.state, "ok")
        self.assertEqual(supplied.budget["iterations"], 5)

    def test_local_qa_table_does_not_replace_shared_table(self) -> None:
        without_qa = 'version = 1\n[env.targets]\napi = "http://localhost:8000"\n'
        self.put('[qa.budget]\niterations = 5\n', ".av/local.toml")
        for shared_text, config_text in ((without_qa, None), (BASE, without_qa)):
            with self.subTest(config_text=config_text):
                self.put(shared_text)
                report = Config(self.repo, state_home=self.state, config_text=config_text).report()
                self.assertEqual(report["state"], "missing-table")
                self.assertEqual(report["errors"], [])
                self.assertEqual(report["budget"]["iterations"], 5)
        self.put(without_qa)
        result = self.cli("config")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["state"], "missing-table")

    def test_local_defaults_without_shared_file_need_bootstrap(self) -> None:
        self.put('[qa.defaults]\nbe_target = "api"\n', ".av/local.toml")
        report = self.config().report()
        result = self.cli("config")
        self.assertEqual(report["state"], "missing-file")
        self.assertEqual(report["errors"], [])
        self.assertEqual(result.returncode, 0, result.stderr)
        cli_report = json.loads(result.stdout)
        self.assertEqual(cli_report["state"], "missing-file")
        self.assertEqual(cli_report["errors"], [])

    def test_validation_errors_identify_file_and_key_without_values(self) -> None:
        cases = [
            ('[qa]\n', "version"), ('version = true\n[qa]\n', "version"),
            ('version = 2\n[qa]\n', "version"), ('version = 1\nqa = "sensitive"\n', "qa"),
            (BASE + 'surprise = "sensitive"\n', "qa.surprise"),
            (BASE + '[qa.policy]\nfix = "sensitive"\n', "qa.policy.fix"),
            (BASE + '[qa.policy]\nmutations = "sensitive"\n', "qa.policy.mutations"),
            (BASE + '[qa.policy]\ndirty_tree = "sensitive"\n', "qa.policy.dirty_tree"),
            (BASE + '[qa.policy]\nmin_severity = "sensitive"\n', "qa.policy.min_severity"),
            (BASE + '[qa.policy]\nmutations = "allow"\n', "qa.policy.disposable_data"),
            (BASE + '[qa.policy]\ndisposable_data = "sensitive"\n', "qa.policy.disposable_data"),
            (BASE + '[qa.budget]\niterations = "sensitive"\n', "qa.budget.iterations"),
            (BASE + '[qa.defaults]\nbe_target = "sensitive"\n', "qa.defaults.be_target"),
            (BASE + '[qa.accounts]\npersonas = ["Bad"]\n', "qa.accounts.personas"),
            (BASE + '[qa.accounts]\npersonas = ["user", "user"]\n', "qa.accounts.personas"),
            (BASE + '[qa.accounts.create]\nkind="http"\ntarget="sensitive"\nmethod="POST"\npath="/"\nexpect=[201]\n', "qa.accounts.create.target"),
            (BASE + '[qa.accounts.delete]\nkind="http"\ntarget="api"\nmethod="DELETE"\npath="/users/{password}"\nexpect=[204]\n', "qa.accounts.delete.path"),
            (BASE + '[qa.accounts.login]\nkind="http"\ntarget="api"\nmethod="POST"\npath="/"\nexpect=[200]\nheaders={x="{secret.MISSING}"}\n', "qa.accounts.login.headers.x"),
            (BASE + '[qa.accounts.login]\nkind="http"\ntarget="api"\nmethod="POST"\npath="/"\nexpect=[200]\njson={x="{value.MISSING}"}\n', "qa.accounts.login.json.x"),
            (BASE + '[env.services]\nhealth=["missing:/"]\n', "env.services.health"),
            (BASE + '[env.services]\nunknown="sensitive"\n', "env.services.unknown"),
            (BASE + '[env.database]\nkind="sensitive"\n', "env.database.kind"),
            (BASE + '[env.database]\nkind="sqlite"\npath="db"\nunknown="sensitive"\n', "env.database.unknown"),
            ('version=1\nenv="sensitive"\n[qa]\n', "env"),
            (BASE + '[qa.defaults]\nunknown="sensitive"\n', "qa.defaults.unknown"),
            (BASE + '[qa.budget]\nunknown=1\n', "qa.budget.unknown"),
            (BASE + '[qa.accounts]\nunknown="sensitive"\n', "qa.accounts.unknown"),
            (BASE + '[qa.accounts]\nemail=1\n', "qa.accounts.email"),
            (BASE + '[qa.accounts]\nemail="{unknown}"\n', "qa.accounts.email"),
            (BASE + '[qa.accounts]\npassword="sensitive"\n', "qa.accounts.password"),
            (BASE + '[qa.accounts.static.Bad]\nemail="env:QA_X"\npassword="env:QA_Y"\n', "qa.accounts.static.Bad"),
            (BASE + '[qa.accounts.static.user]\nemail="env:QA_X"\n', "qa.accounts.static.user.password"),
            (BASE + '[qa.accounts.static.user]\nemail="env:QA_X"\npassword="env:QA_Y"\nunknown="sensitive"\n', "qa.accounts.static.user.unknown"),
            (BASE + '[qa.accounts.create]\nkind="sensitive"\n', "qa.accounts.create.kind"),
            (BASE + '[qa.accounts.login]\nkind="command"\nrun=1\n', "qa.accounts.login.run"),
            (BASE + '[qa.accounts.login]\nkind="command"\nrun="true"\noutputs={unknown=true}\n', "qa.accounts.login.outputs.unknown"),
            (BASE + '[qa.accounts.login]\nkind="command"\nrun="true"\noutputs={token="sensitive"}\n', "qa.accounts.login.outputs.token"),
            (BASE + '[qa.accounts.login]\nkind="http"\ntarget="api"\nmethod="sensitive"\npath="https://evil.test/"\nexpect=["sensitive"]\nheaders={x=1}\ntoken="sensitive"\ncookies=["---"]\n', "qa.accounts.login.method"),
            (BASE + '[qa.accounts.login]\nkind="http"\ntarget="api"\nmethod="POST"\npath="//evil.test/"\nexpect=[200]\n', "qa.accounts.login.path"),
            (BASE + '[qa.accounts.login]\nkind="http"\ntarget="api"\nmethod="POST"\npath="/"\nexpect=[true]\n', "qa.accounts.login.expect"),
            (BASE + '[qa.accounts.login]\nkind="http"\ntarget="api"\nmethod="POST"\npath="/"\nexpect=[200]\nheaders={x=1}\n', "qa.accounts.login.headers.x"),
            (BASE + '[qa.accounts.login]\nkind="http"\ntarget="api"\nmethod="POST"\npath="/"\nexpect=[200]\ntoken="sensitive"\n', "qa.accounts.login.token"),
            (BASE + '[qa.accounts.login]\nkind="http"\ntarget="api"\nmethod="POST"\npath="/"\nexpect=[200]\ncookies=["---"]\n', "qa.accounts.login.cookies"),
            (BASE + '[env.services]\nhealth=1\nup=1\ndown=1\nprepare=[1]\n', "env.services.health"),
            (BASE + '[env.services]\nhealth=["sensitive"]\n', "env.services.health"),
            (BASE + '[env.database]\nkind="sqlite"\n', "env.database.path"),
            (BASE + '[env.database]\nkind="postgres"\nhost="localhost"\nuser="user"\nname="db"\npassword="env:AV_DB"\nport=true\n', "env.database.port"),
            (BASE + '[env.values]\n"bad-name"="env:AV_X"\n', "env.values.bad-name"),
            (BASE + '[env.values]\nX="sensitive"\n', "env.values.X"),
        ]
        for text, key in cases:
            with self.subTest(key=key, text=text):
                self.put(text)
                report = self.config().report()
                self.assertEqual(report["state"], "invalid")
                self.assertTrue(any(e["file"] == ".av/config.toml" and e["key"] == key for e in report["errors"]), report["errors"])
                self.assertNotIn("sensitive", json.dumps(report))

    def test_target_validation(self) -> None:
        for origin in ("ftp://localhost", "http://user:pass@localhost", "http://localhost/", "http://localhost?q=x", "http://localhost#x", "http://localhost:bad", "http://localhost:70000"):
            with self.subTest(origin=origin):
                self.put(f'version=1\n[env.targets]\napi={json.dumps(origin)}\n[qa]\n')
                self.assertTrue(any(e["key"] == "env.targets.api" for e in self.config().errors))

    def test_unknown_env_and_other_plugin_are_ignored_for_trust(self) -> None:
        self.put(BASE + '[env.browser]\nengine = "chromium"\n')
        cfg = self.config()
        self.assertEqual(cfg.report()["state"], "ok")
        self.assertEqual(cfg.warnings[0]["key"], "env.browser")
        before = cfg.trust_hash
        self.put(BASE + '[env.browser]\nengine = "chromium"\n[delivery]\nanything="cmd:evil"\n')
        self.assertEqual(self.config().trust_hash, before)
        self.assertEqual(self.config().report()["state"], "ok")

    def test_source_restrictions_by_file(self) -> None:
        for source in ("env:GH_TOKEN", "file:/tmp/private.env#X"):
            with self.subTest(source=source):
                self.put(BASE + f'[env.values]\nX={json.dumps(source)}\n')
                self.assertTrue(any(e["key"] == "env.values.X" for e in self.config().errors))
                self.put(BASE)
                self.put(f'[env.values]\nX={json.dumps(source)}\n', ".av/local.toml")
                self.assertEqual(self.config().report()["state"], "ok")
                (self.repo / ".av/local.toml").unlink()
        self.put(BASE + '[qa.accounts.static.user]\nemail="env:QA_USER_EMAIL"\npassword="literal:secret"\n')
        self.assertTrue(any(e["key"] == "qa.accounts.static.user.password" for e in self.config().errors))
        self.put(BASE + '[env.secrets]\nX="literal:secret"\n')
        self.assertTrue(self.config().errors)
        self.put(BASE + '[env.database]\nkind="postgres"\nhost="localhost"\nport=5432\nuser="user"\nname="db"\npassword="literal:secret"\n')
        self.assertFalse(self.config().errors)
        self.put((self.repo / ".av/config.toml").read_text().replace('host="localhost"', 'host="db.example.com"'))
        self.assertTrue(any(e["key"] == "env.database.password" for e in self.config().errors))

    def test_trust_transitions_and_stale_accept(self) -> None:
        self.put(BASE)
        self.assertEqual(self.config().trust, "not-required")
        self.put(BASE + '[env.values]\nX="cmd:printf x"\n')
        cfg = self.config()
        self.assertEqual(cfg.trust, "new")
        cfg.accept(cfg.trust_hash)
        self.assertEqual(self.config().trust, "trusted")
        self.put('# changed comment\n' + BASE + '[env.values]\nX="cmd:printf x"\n')
        self.assertEqual(self.config().trust, "trusted")
        for fragment in ('[env.values]\nX="cmd:printf y"\n', '[env.values]\nX="env:AV_X"\n', '[env.values]\nX="env:AV_Y"\n', '[qa.policy]\nfix="auto"\n'):
            with self.subTest(fragment=fragment):
                old = self.config().trust_hash
                self.put(BASE + fragment)
                cfg = self.config()
                self.assertEqual(cfg.trust, "changed")
                with self.assertRaises(av_config.ConfigError):
                    cfg.accept(old)
                cfg.accept(cfg.trust_hash)
        cfg = self.config()
        old = cfg.trust_hash
        self.put(BASE + '[qa.policy]\nfix="off"\n')
        with self.assertRaises(av_config.ConfigError):
            cfg.accept(old)

    def test_local_password_changes_hash_but_is_never_displayed(self) -> None:
        self.put(BASE)
        self.put('[qa.accounts.static.user]\nemail="literal:user@test.local"\npassword="literal:never-display-me"\n', ".av/local.toml")
        cfg = self.config()
        old = cfg.trust_hash
        self.assertNotIn("never-display-me", json.dumps(cfg.report()))
        result = self.cli("config")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("never-display-me", result.stdout + result.stderr)
        proposal = {"config_text": BASE, "gitignore_add": [], "allowed_keys": []}
        self.assertNotIn("never-display-me", json.dumps(cfg.preview(proposal)))
        self.put('[qa.accounts.static.user]\nemail="literal:user@test.local"\npassword="literal:different"\n', ".av/local.toml")
        self.assertNotEqual(self.config().trust_hash, old)
        self.put('[qa.accounts.static.user]\npassword="literal:never-display-me"\nunknown="literal:never-display-me"\n', ".av/local.toml")
        result = self.cli("config")
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("never-display-me", result.stdout + result.stderr)

    def test_names_capabilities_cookie_normalization_and_collisions(self) -> None:
        text = BASE + '[qa.accounts]\npersonas=["user"]\n[qa.accounts.create]\nkind="command"\nrun="true"\noutputs={id=true}\n[qa.accounts.login]\nkind="command"\nrun="true"\noutputs={token=true,cookies=["__Host-session", "connect.sid"]}\n'
        self.put(text)
        cfg = self.config()
        self.assertFalse(cfg.errors)
        self.assertEqual(cfg.persona_fields("user"), {"EMAIL", "PASSWORD", "ID", "TOKEN", "COOKIE", "COOKIE_HOST_SESSION", "COOKIE_CONNECT_SID"})
        self.assertIn("QA_USER_COOKIE_HOST_SESSION", cfg.report()["exposed"])
        self.put(text.replace('"connect.sid"', '"host_session"'))
        self.assertTrue(any("cookies" in e["key"] for e in self.config().errors))
        self.put(text + '[env.values]\nUSER_TOKEN="env:AV_X"\n')
        self.assertTrue(any(e["key"] == "env.values.USER_TOKEN" for e in self.config().errors))
        self.put(BASE + '[env.values]\nx="env:AV_X"\nX="env:AV_Y"\n')
        self.assertTrue(any(e["key"] == "env.values.X" for e in self.config().errors))
        self.put(BASE + '[qa.accounts]\npersonas=["user"]\n[env.values]\nUSER_COOKIE_SESSION="env:AV_X"\n')
        self.assertTrue(any(e["key"] == "env.values.USER_COOKIE_SESSION" for e in self.config().errors))

    def test_preview_section_guard_and_no_writes(self) -> None:
        original = BASE + '[qa.budget]\niterations=3\n[delivery]\n# keep this comment\nmode="old"\n'
        self.put(original)
        cfg = self.config()
        proposal = {"config_text": original.replace("iterations=3", "iterations=4"), "gitignore_add": [], "allowed_keys": ["qa.budget.iterations"]}
        preview = cfg.preview(proposal)
        self.assertTrue(preview["ok"], preview["errors"])
        self.assertEqual((self.repo / ".av/config.toml").read_text(), original)
        self.assertFalse((self.repo / ".gitignore").exists())
        for replacement in ('mode="new"', '# changed comment\nmode="old"'):
            with self.subTest(replacement=replacement):
                changed = {**proposal, "config_text": proposal["config_text"].replace('mode="old"', replacement)}
                self.assertFalse(cfg.preview(changed)["ok"])
        inline = 'version=1\n[qa]\nbudget={iterations=3, dispatches=50}\n[delivery]\n# protected\nmode="old"\n'
        self.put(inline)
        proposal = {"config_text": inline.replace("iterations=3", "iterations=4"), "gitignore_add": [], "allowed_keys": ["qa.budget.iterations"]}
        self.assertTrue(self.config().preview(proposal)["ok"])
        proposal["config_text"] = proposal["config_text"].replace("dispatches=50", "dispatches=20")
        self.assertFalse(self.config().preview(proposal)["ok"])
        self.put(BASE)
        proposal = {"config_text": BASE + '[qa.budget]\niterations=4\n', "gitignore_add": [], "allowed_keys": ["qa.budget.iterations"]}
        self.assertTrue(self.config().preview(proposal)["ok"])
        quoted = 'version=1\n[qa]\n["qa.extra"]\n# another plugin table\nvalue="old"\n'
        self.put(quoted)
        proposal = {"config_text": quoted.replace('value="old"', 'value="new"'), "gitignore_add": [], "allowed_keys": ["qa"]}
        self.assertFalse(self.config().preview(proposal)["ok"])

    def test_create_preview_accepts_bootstrap_layout_and_root_comments(self) -> None:
        proposals = (
            'version = 1\n\n[qa]\n',
            '# Project configuration\nversion = 1\n\n[qa]\n',
            'version = 1\n\n# ---- shared environment (any plugin) ----\n\n'
            '[env.targets]  # named origins\napi = "http://localhost:8000"\n\n'
            '# ---- QA ----\n\n[qa.defaults]\nbe_target = "api"\n',
        )
        for text in proposals:
            with self.subTest(text=text):
                proposal = {"config_text": text, "gitignore_add": [], "allowed_keys": ["version", "env", "qa"]}
                preview = self.config().preview(proposal)
                self.assertTrue(preview["ok"], preview["errors"])
                self.assertFalse((self.repo / ".av/config.toml").exists())
                self.assertFalse((self.repo / ".gitignore").exists())
                proposal["config_text"] = text + '[delivery]\nmode = "auto"\n'
                self.assertFalse(self.config().preview(proposal)["ok"])

    def test_extend_preview_accepts_separators_and_qa_header_comments(self) -> None:
        original = 'version = 1\n\n[env.targets]\napi = "http://localhost:8000"\n'
        self.put(original)
        cfg = self.config()
        self.assertEqual(cfg.state, "missing-table")
        for separator in ("\n", "\n# QA defaults\n# Derived from settings.py\n"):
            with self.subTest(separator=separator):
                text = original + separator + '[qa.defaults]\nbe_target = "api"\n'
                proposal = {"config_text": text, "gitignore_add": [], "allowed_keys": ["qa"]}
                preview = cfg.preview(proposal)
                self.assertTrue(preview["ok"], preview["errors"])
                self.assertEqual((self.repo / ".av/config.toml").read_text(), original)
                self.assertFalse((self.repo / ".gitignore").exists())

    def test_preview_ignores_blank_lines_but_protects_header_comments(self) -> None:
        original = BASE + '\n# Delivery settings\n# Derived from delivery.toml\n[delivery]\nmode = "old"\n'
        self.put(original)
        text = original.replace('[delivery]\n', '\n[delivery]\n\n \t\n')
        proposal = {"config_text": text, "gitignore_add": [], "allowed_keys": ["qa"]}
        preview = self.config().preview(proposal)
        self.assertTrue(preview["ok"], preview["errors"])
        proposal["config_text"] = original.replace("# Delivery settings", "# Changed delivery settings")
        self.assertFalse(self.config().preview(proposal)["ok"])

    def first_proposal(self) -> dict[str, object]:
        return {"config_text": BASE + '[env.values]\nX="file:.av/secrets.local.env#X"\n', "gitignore_add": [".av/local.toml", ".av/secrets.local.env"], "allowed_keys": ["version", "env", "qa"]}

    def test_first_run_virtual_ignore_and_apply(self) -> None:
        cfg = self.config()
        proposal = self.first_proposal()
        preview = cfg.preview(proposal)
        self.assertTrue(preview["ok"], preview["errors"])
        self.assertFalse((self.repo / ".av/config.toml").exists())
        result = cfg.apply(proposal, preview["snapshot"], preview["trust_hash"])
        self.assertTrue(result["applied"])
        self.assertEqual((self.repo / ".av/config.toml").read_text(), proposal["config_text"])
        self.assertIn(".av/secrets.local.env", (self.repo / ".gitignore").read_text())
        self.assertEqual(self.config().trust, "trusted")

    def test_cas_no_write_for_either_changed_file(self) -> None:
        for name in (".av/config.toml", ".gitignore"):
            with self.subTest(name=name):
                cfg = self.config()
                proposal = self.first_proposal()
                preview = cfg.preview(proposal)
                self.put("# concurrent\n", name)
                before = {p: p.read_bytes() if p.exists() else None for p in (self.repo / ".av/config.toml", self.repo / ".gitignore")}
                with self.assertRaises(av_config.ConfigError):
                    cfg.apply(proposal, preview["snapshot"], preview["trust_hash"])
                self.assertEqual(before, {p: p.read_bytes() if p.exists() else None for p in before})
                (self.repo / name).unlink()

    def test_failed_postwrite_rolls_back_only_owned_bytes(self) -> None:
        for changed_file in (None, ".av/config.toml", ".gitignore"):
            with self.subTest(changed_file=changed_file):
                cfg = self.config()
                proposal = self.first_proposal()
                preview = cfg.preview(proposal)

                def reject_after_write(text: str, ignore: str) -> dict[str, object]:
                    report = Config(self.repo, state_home=self.state, config_text=text, gitignore_text=ignore).report()
                    if (self.repo / ".av/config.toml").exists():
                        if changed_file is not None:
                            (self.repo / changed_file).write_text("# concurrent\n")
                        report["errors"] = [{"file": ".av/config.toml", "key": "version", "error": "post-write validation failed"}]
                    return report

                transaction = av_config.ConfigTransaction(self.repo, reject_after_write, cfg.accept)
                with self.assertRaises(av_config.ConfigError):
                    transaction.apply(proposal, preview["snapshot"], preview["trust_hash"])
                for name in (".av/config.toml", ".gitignore"):
                    path = self.repo / name
                    if name == changed_file:
                        self.assertEqual(path.read_text(), "# concurrent\n")
                        path.unlink()
                    else:
                        self.assertFalse(path.exists())

    def test_sources_resolve_and_errors_do_not_echo_values(self) -> None:
        self.put('X="private-value"\n', ".av/secrets.local.env")
        self.put('.av/secrets.local.env\n', ".gitignore")
        resolver = av_config.ValueSources(self.repo, plugin_prefix="QA_", environ={"AV_X": "private-value"})
        for source in ("literal:private-value", "env:AV_X", "file:.av/secrets.local.env#X", "cmd:printf private-value"):
            with self.subTest(source=source):
                self.assertEqual(resolver.resolve(source, "env.values.X", trusted=True), "private-value")
        for source in ("env:MISSING", "file:.av/secrets.local.env#MISSING", "cmd:printf private-value; exit 1", "cmd:true"):
            with self.subTest(source=source):
                with self.assertRaises(av_config.ConfigError) as error:
                    resolver.resolve(source, "env.values.X", trusted=True)
                self.assertIn("env.values.X", str(error.exception))
                self.assertNotIn("private-value", str(error.exception))
        with self.assertRaises(av_config.ConfigError):
            resolver.resolve("cmd:touch should-not-exist", "env.values.X", trusted=False)
        self.assertFalse((self.repo / "should-not-exist").exists())
        timed = av_config.ValueSources(self.repo, plugin_prefix="QA_", timeout=0.2)
        with self.assertRaises(av_config.ConfigError) as error:
            timed.resolve("cmd:/bin/sleep 10", "env.values.X", trusted=True)
        self.assertIn("timed out", str(error.exception))

    def test_cli_preview_apply_trust_tools_and_usage(self) -> None:
        proposal = self.first_proposal()
        path = self.put(json.dumps(proposal), "proposal.json")
        result = self.cli("config", "preview", str(path))
        self.assertEqual(result.returncode, 0, result.stderr)
        preview = json.loads(result.stdout)
        result = self.cli("config", "apply", str(path), "--snapshot", preview["snapshot"], "--approved-hash", preview["trust_hash"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout)["applied"])
        result = self.cli("trust", "accept", "stale")
        self.assertEqual(result.returncode, 1)
        self.assertIn("error", json.loads(result.stdout))
        tools = json.loads(self.cli("tools").stdout)
        self.assertEqual(set(tools), {"curl", "jq", "perl_json_pp", "psql", "mysql", "sqlite3", "httpie"})
        result = self.cli("no-such-command", "literal:do-not-echo")
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("do-not-echo", result.stdout + result.stderr)
        self.assertIn("error", json.loads(result.stdout))

    def test_hash_mismatch_restores_existing_snapshot(self) -> None:
        original = BASE + '[qa.policy]\nfix="approve"\n'
        self.put(original)
        self.put("# original ignore\n", ".gitignore")
        cfg = self.config()
        proposal = {"config_text": original.replace('fix="approve"', 'fix="auto"'), "gitignore_add": [".av/local.toml"], "allowed_keys": ["qa.policy.fix"]}
        preview = cfg.preview(proposal)
        with self.assertRaises(av_config.ConfigError):
            cfg.apply(proposal, preview["snapshot"], "stale")
        self.assertEqual((self.repo / ".av/config.toml").read_text(), original)
        self.assertEqual((self.repo / ".gitignore").read_text(), "# original ignore\n")

    def test_sources_are_not_executed_by_config_or_preview(self) -> None:
        text = BASE + '[env.values]\nX="cmd:touch must-not-exist; printf value"\n'
        self.put(text)
        cfg = self.config()
        self.assertEqual(cfg.state, "ok")
        self.assertTrue(cfg.preview({"config_text": text, "gitignore_add": [], "allowed_keys": []})["ok"])
        self.assertEqual(self.cli("config").returncode, 0)
        self.assertFalse((self.repo / "must-not-exist").exists())

    def test_trust_is_scoped_to_plugin_and_real_repository(self) -> None:
        self.put(BASE + '[qa.policy]\nfix="auto"\n')
        cfg = self.config()
        cfg.accept(cfg.trust_hash)
        other = av_config.TrustStore(self.repo, "delivery", self.state)
        self.assertEqual(other.status({"delivery.policy.fix": "auto"}), "new")
        alias = Path(self.tmp.name) / "alias"
        alias.symlink_to(self.repo, target_is_directory=True)
        self.assertEqual(Config(alias, state_home=self.state).trust, "trusted")
        another = Path(self.tmp.name) / "another"
        another.mkdir()
        self.assertEqual(av_config.TrustStore(another, "qa", self.state).status(cfg.trust_subset), "new")
        self.put(BASE + '[qa.policy]\nfix="auto"\n[delivery]\npolicy={fix="auto"}\n')
        self.assertEqual(self.config().trust, "trusted")

    def test_gitignore_semantics_and_parameterized_source_prefix(self) -> None:
        source = "file:.av/secrets.local.env#X"
        resolver = av_config.ValueSources(self.repo, plugin_prefix="DELIVERY_")
        self.assertIsNotNone(resolver.validate(source, "env.values.X"))
        self.put(".av/*.env\n!.av/secrets.local.env\n", ".gitignore")
        self.assertIsNotNone(resolver.validate(source, "env.values.X"))
        virtual = av_config.ValueSources(self.repo, plugin_prefix="DELIVERY_", gitignore_text=".av/*.env\n")
        self.assertIsNone(virtual.validate(source, "env.values.X"))
        self.assertIsNone(resolver.validate("env:DELIVERY_TOKEN", "delivery.token"))
        self.assertIsNotNone(resolver.validate("env:DELIVERY_TOKEN", "env.values.X"))
        self.assertIsNotNone(resolver.validate("file:../secret.env#X", "delivery.token", local=True))

    def test_malformed_types_and_documents_do_not_crash_or_echo(self) -> None:
        for text in ('version=1\n[qa.policy]\nfix=[]\n', 'version=1\n[qa.accounts.login]\nkind=[]\n', 'version=1\n[env.database]\nkind=[]\n', 'version=1\n[qa]\nbudget=1\n', 'version=1\n[qa]\naccounts=1\n', 'version=1\n[env]\nvalues=1\n', 'version=1\n[qa]\nbudget="sensitive\n'):
            with self.subTest(text=text):
                self.put(text)
                result = self.cli("config")
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(json.loads(result.stdout)["state"], "invalid")
                self.assertNotIn("sensitive", result.stdout + result.stderr)

    def test_sensitive_env_sources_and_qa_gates_are_pinned(self) -> None:
        cases = ('[env.browser]\nsource="cmd:printf value"\n', '[env.browser]\nsources=["env:AV_BROWSER"]\n', '[env.targets]\nremote="https://example.com"\n', '[env.database]\nkind="postgres"\nhost="db.example.com"\nuser="user"\nname="db"\npassword="env:AV_DB"\n', '[qa.policy]\nfix="off"\n', '[qa.policy]\ndirty_tree="abort"\n', '[qa.policy]\nmutations="deny"\n', '[qa.policy]\ndisposable_data=true\n')
        for section in cases:
            with self.subTest(section=section):
                self.put(('version=1\n[qa]\n' if section.startswith("[env.targets]") else BASE) + section)
                cfg = self.config()
                self.assertFalse(cfg.errors)
                self.assertNotEqual(cfg.trust, "not-required")

    def test_recipe_json_rejects_non_json_toml_values(self) -> None:
        for value in ("2000-01-01", "nan", "inf"):
            with self.subTest(value=value):
                self.put(BASE + f'[qa.accounts.login]\nkind="http"\ntarget="api"\nmethod="POST"\npath="/"\nexpect=[200]\njson={{x={value}}}\n')
                result = self.cli("config")
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                report = json.loads(result.stdout)
                self.assertTrue(any(error["key"] == "qa.accounts.login.json.x" for error in report["errors"]))

    def test_nested_placeholder_and_shell_environment_syntax(self) -> None:
        self.put(BASE + '[qa.accounts.login]\nkind="http"\ntarget="api"\nmethod="POST"\npath="/"\nexpect=[200]\njson={items=[{secret="{secret.MISSING}"}]}\n')
        self.assertTrue(any("json.items" in error["key"] for error in self.config().errors))
        self.put(BASE + '[qa.accounts.login]\nkind="command"\nrun="printf \\"${QA_EMAIL}\\""\noutputs=["token"]\n')
        self.assertFalse(self.config().errors)

    def test_command_recipe_braces_are_not_http_placeholders(self) -> None:
        cases = (
            ("login", "curl -s localhost | jq -c '{token: .access_token}'"),
            ("login", 'printf \'{"token":"%s"}\' "$T"'),
            ("delete", "echo {password}"),
        )
        for recipe, command in cases:
            with self.subTest(recipe=recipe, command=command):
                self.put(BASE + f'[qa.accounts.{recipe}]\nkind="command"\nrun={json.dumps(command)}\n')
                report = self.config().report()
                self.assertEqual(report["state"], "ok", report["errors"])
                self.assertEqual(report["errors"], [])

    def test_cli_rejects_malformed_transaction_with_usage_exit(self) -> None:
        path = self.put('{"config_text":"literal:never-display-me"}', "bad-proposal.json")
        result = self.cli("config", "preview", str(path))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("error", json.loads(result.stdout))
        self.assertNotIn("never-display-me", result.stdout + result.stderr)
        proposal = {"config_text": 'version=1\n[qa..policy]\npassword="literal:never-display-me"\n', "gitignore_add": [], "allowed_keys": ["version", "qa"]}
        path.write_text(json.dumps(proposal))
        result = self.cli("config", "preview", str(path))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertFalse(json.loads(result.stdout)["ok"])
        self.assertNotIn("never-display-me", result.stdout + result.stderr)

    def test_empty_xdg_state_home_keeps_trust_outside_repository(self) -> None:
        self.put(BASE + '[qa.policy]\nfix="auto"\n')
        home = Path(self.tmp.name) / "home"
        home.mkdir()
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "qa.py"), "trust", "accept", self.config().trust_hash, "--repo", str(self.repo)],
            cwd=self.repo, env={**os.environ, "XDG_STATE_HOME": "", "HOME": str(home)},
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.repo / "av-marketplace").exists())
        self.assertEqual(Config(self.repo, state_home=home / ".local/state").trust, "trusted")



class OriginTests(unittest.TestCase):
    def test_loopback_edges(self) -> None:
        for host, expected in (("127.0.0.1.evil.com", False), ("0.0.0.0", False), ("::1", True), ("app.localhost", True), ("LOCALHOST", True)):
            with self.subTest(host=host):
                self.assertEqual(av_config.is_loopback(host), expected)
        self.assertEqual(av_config.parse_origin("http://[::1]:8000"), ("http", "::1", 8000))
        self.assertEqual(av_config.parse_origin("https://EXAMPLE.com"), ("https", "example.com", 443))

    def test_resolve_and_exact_origin_guard(self) -> None:
        targets = Targets({"api": "http://localhost:8000", "remote": "https://example.com"}, trusted=False)
        self.assertEqual(targets.resolve("/users", "api"), "http://localhost:8000/users")
        self.assertTrue(targets.allowed("http://localhost:8000"))
        self.assertFalse(targets.allowed("http://localhost:8001"))
        self.assertFalse(targets.allowed("https://example.com"))
        for url in ("http://localhost:8001/users", "http://u:p@localhost:8000/users", "//example.com/users"):
            with self.subTest(url=url):
                with self.assertRaises(av_config.ConfigError):
                    targets.resolve(url, "api")
        trusted = Targets({"remote": "https://example.com"}, trusted=True)
        self.assertEqual(trusted.resolve("https://EXAMPLE.com:443/path?q=x", "remote"), "https://EXAMPLE.com:443/path?q=x")


if __name__ == "__main__":
    unittest.main()
