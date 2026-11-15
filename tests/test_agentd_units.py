"""The executors' argv and parsers, config, the MCP server. No processes except where noted."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from lumbridge_factory.agentd import childenv, claude, codex, config, mcp_tools, procs
from lumbridge_factory.agentd.config import Account, ConfigError
from lumbridge_factory.core.executor import EntryKind, EventType, ReportStatus, TurnOutcome, Verdict, entry_from_wire


def minimal_config(**extra) -> dict:
    return {"socket": "/tmp/s", "state_dir": "/tmp/st", "worktrees_dir": "/tmp/w", "repos_dir": "/tmp/r", "agent_home": "/tmp/h", **extra}


class ConfigTests(unittest.TestCase):
    def test_codex_is_off_by_default(self):
        self.assertFalse(config.from_dict(minimal_config()).tools["codex"].enabled)
        self.assertTrue(config.from_dict(minimal_config()).tools["claude"].enabled)

    def test_missing_keys(self):
        with self.assertRaises(ConfigError):
            config.from_dict({"socket": "x"})

    def test_a_token_value_in_config_is_refused(self):
        with self.assertRaises(ConfigError) as caught:
            config.from_dict(minimal_config(accounts={"a": {"tool": "claude", "token": "sk-not-for-here"}}))
        self.assertNotIn("sk-not-for-here", str(caught.exception))

    def test_unknown_tool(self):
        with self.assertRaises(ConfigError):
            config.from_dict(minimal_config(accounts={"a": {"tool": "gemini"}}))


class ChildEnvTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "x.env")

    def test_env_file_forms(self):
        Path(self.path).write_text("# c\nexport A=1\nB='two'\nC=\"three\"\n  export D=4=5\nE=\n")
        self.assertEqual(childenv.read_env_file(self.path, "A"), "1")
        self.assertEqual(childenv.read_env_file(self.path, "B"), "two")
        self.assertEqual(childenv.read_env_file(self.path, "C"), "three")
        self.assertEqual(childenv.read_env_file(self.path, "D"), "4=5")
        self.assertEqual(childenv.read_env_file(self.path, "E"), "")
        self.assertIsNone(childenv.read_env_file(self.path, "Z"))

    def test_token_errors_never_hold_the_value(self):
        Path(self.path).write_text("OTHER=sekrit\n")
        account = Account("a", "claude", env_file=self.path, env_var="MISSING")
        with self.assertRaises(childenv.TokenError) as caught:
            childenv.account_token(account)
        self.assertNotIn("sekrit", str(caught.exception))

    def test_token_from_agentds_own_environment(self):
        os.environ["LUMBRIDGE_FACTORY_TEST_TOKEN"] = "from-env"
        self.addCleanup(os.environ.pop, "LUMBRIDGE_FACTORY_TEST_TOKEN")
        self.assertEqual(childenv.account_token(Account("a", "claude", env_var="LUMBRIDGE_FACTORY_TEST_TOKEN")), "from-env")

    def test_child_env_cant_smuggle_credentials_or_home(self):
        for key in ("SOME_TOKEN", "OPENAI_API_KEY", "MY_SECRET", "LD_PRELOAD", "HOME", "PATH", "CLAUDE_CONFIG_DIR"):
            cfg = config.from_dict(minimal_config(child_env={key: "x"}))
            with self.subTest(key=key), self.assertRaises(childenv.TokenError):
                childenv.base_env(cfg)

    def test_nothing_is_inherited(self):
        os.environ["LUMBRIDGE_FACTORY_TEST_LEAK"] = "x"
        self.addCleanup(os.environ.pop, "LUMBRIDGE_FACTORY_TEST_LEAK")
        Path(self.path).write_text("T=v\n")
        cfg = config.from_dict(minimal_config())
        env, secrets = childenv.turn_env(cfg, Account("a", "claude", env_file=self.path, env_var="T"), "turn-1")
        self.assertNotIn("LUMBRIDGE_FACTORY_TEST_LEAK", env)
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "v")
        self.assertNotIn("CLAUDE_CODE_SUBPROCESS_ENV_SCRUB", env)  # it forces a sandbox; see scrub_shell.sh
        self.assertEqual(env["CLAUDE_CODE_SHELL_PREFIX"], str(childenv.SCRUB_SHELL))
        self.assertEqual(env["LUMBRIDGE_FACTORY_TURN"], "turn-1")
        self.assertEqual(secrets, ["v"])
        self.assertEqual(env["BASH_MAX_TIMEOUT_MS"], "1800000", "a long suite runs in the foreground, inside the turn")

    def test_scrub_shell_drops_credentials_and_keeps_the_rest(self):
        self.assertTrue(os.access(childenv.SCRUB_SHELL, os.X_OK))
        out = subprocess.run(
            [str(childenv.SCRUB_SHELL), 'echo "[$CLAUDE_CODE_OAUTH_TOKEN][$ANTHROPIC_API_KEY][$KEEP]"; exit 2'],
            capture_output=True,
            text=True,
            env={"PATH": os.environ["PATH"], "CLAUDE_CODE_OAUTH_TOKEN": "x", "ANTHROPIC_API_KEY": "y", "KEEP": "z"},
        )
        self.assertEqual(out.stdout.strip(), "[][][z]")
        self.assertEqual(out.returncode, 2)  # a hook's refusal must survive the wrapper

    def test_a_missing_wrapper_disables_claude_instead_of_failing_open(self):
        from unittest import mock

        from lumbridge_factory.agentd.turns import TurnManager

        with tempfile.TemporaryDirectory() as tmp:
            cfg = config.from_dict(minimal_config(state_dir=tmp + "/s", agent_home=tmp + "/h", worktrees_dir=tmp + "/w", repos_dir=tmp + "/r"))
            self.assertEqual(TurnManager(cfg).tool_problems, {})
            with mock.patch.object(childenv, "SCRUB_SHELL", Path(tmp) / "missing.sh"):
                self.assertIn("claude", TurnManager(cfg).tool_problems)

    def test_redact(self):
        self.assertEqual(childenv.redact("a TOK b TOK", ["TOK"]), "a [redacted] b [redacted]")
        self.assertEqual(childenv.redact("abc", [""]), "abc")


class TurnMarkerTests(unittest.TestCase):
    def test_a_stray_is_found_by_the_new_marker_and_by_its_name_before_0_1_0(self):
        for var in (procs.TURN_VAR, procs.OLD_TURN_VAR):
            marker = f"marker-test-{var.lower()}-{os.getpid()}"
            child = subprocess.Popen(["sleep", "30"], env={"PATH": os.environ["PATH"], var: marker})
            try:
                self.assertEqual(procs.pids_with_marker(marker), [child.pid], var)
            finally:
                child.kill()
                child.wait()
            self.assertEqual(procs.pids_with_marker(marker), [], var)


class ClaudeTests(unittest.TestCase):
    def argv(self, native=None):
        return claude.build_argv("claude", model="sonnet", settings="/s.json", mcp_config="/m.json", role_file="/r.md", native_session_id=native)

    def test_resume_flag(self):
        self.assertNotIn("--resume", self.argv())
        argv = self.argv("abc")
        self.assertEqual(argv[argv.index("--resume") + 1], "abc")

    def test_no_prompt_on_the_command_line(self):
        # stdin, so a long prompt can't hit ARG_MAX and can't be read from `ps`.
        self.assertNotIn("the assignment", " ".join(self.argv()))

    def test_parser_stream(self):
        parser = claude.Parser()
        self.assertEqual(parser.feed({"type": "system", "subtype": "init", "session_id": "S"}), [(EventType.STARTED, {"native_session_id": "S"})])
        self.assertEqual(parser.feed({"type": "system", "subtype": "x", "session_id": "S"}), [])
        events = parser.feed(
            {
                "type": "assistant",
                "session_id": "S",
                "message": {
                    "id": "m1",
                    "content": [{"type": "text", "text": "hello"}, {"type": "tool_use", "name": "Bash", "input": {"command": "x"}}],
                    "usage": {"input_tokens": 3, "output_tokens": 4, "cache_read_input_tokens": 99},
                },
            }
        )
        self.assertEqual(events, [(EventType.TEXT, {"text": "hello"}), (EventType.TOOL, {"name": "Bash"}), (EventType.USAGE, {"tokens": 7})])
        self.assertFalse(parser.done)
        parser.feed({"type": "result", "subtype": "success", "is_error": False, "session_id": "S", "usage": {"input_tokens": 5, "output_tokens": 5}})
        self.assertTrue(parser.done)
        self.assertEqual(parser.tokens, 10)

    def test_parser_counts_a_repeated_message_once(self):
        parser = claude.Parser()
        message = {"id": "m1", "content": [], "usage": {"input_tokens": 3, "output_tokens": 4}}
        parser.feed({"type": "assistant", "message": message})
        parser.feed({"type": "assistant", "message": message})
        self.assertEqual(parser.tokens, 7)

    def test_malformed_stream_is_harmless(self):
        parser = claude.Parser()
        for obj in ({}, {"type": "assistant"}, {"type": "assistant", "message": {"content": ["x", 3]}}, {"type": "result"}):
            parser.feed(obj)

    def test_the_session_id_survives_a_later_change(self):
        parser = claude.Parser(native_session_id="old")
        parser.feed({"type": "system", "session_id": "new"})
        self.assertEqual(parser.native_session_id, "new")

    def classify(self, final=None, exit_code=1, stderr="", resumed=False):
        parser = claude.Parser()
        parser.final = final
        return parser.classify(exit_code, stderr, resumed)[0]

    def test_classify(self):
        ok = {"subtype": "success", "is_error": False}
        self.assertEqual(self.classify(ok, 0), TurnOutcome.COMPLETED)
        self.assertEqual(self.classify(ok, 143), TurnOutcome.COMPLETED)
        self.assertEqual(self.classify(None, -9), TurnOutcome.INTERRUPTED)
        self.assertEqual(self.classify(None, 1, "No conversation found with session ID: x", True), TurnOutcome.RESUME_FAILED)
        self.assertEqual(self.classify(None, 1, "No conversation found with session ID: x", False), TurnOutcome.FAILED)
        self.assertEqual(self.classify({"is_error": True, "result": "Claude usage limit reached"}), TurnOutcome.ACCOUNT_LIMIT)
        self.assertEqual(self.classify({"is_error": True, "result": "You've hit your usage limit · resets 3pm"}), TurnOutcome.ACCOUNT_LIMIT)
        self.assertEqual(self.classify({"is_error": True, "api_error_status": 529, "result": "x"}), TurnOutcome.TRANSIENT)
        self.assertEqual(self.classify({"is_error": True, "api_error_status": 408, "result": "x"}), TurnOutcome.TRANSIENT)
        self.assertEqual(self.classify({"is_error": True, "api_error_status": 400, "result": "x"}), TurnOutcome.FAILED)
        self.assertEqual(self.classify(None, 1, "read ECONNRESET"), TurnOutcome.TRANSIENT)
        self.assertEqual(self.classify({"subtype": "error_max_turns", "is_error": True}), TurnOutcome.FAILED)
        self.assertEqual(self.classify(None, 1, "Invalid API key · Please run /login"), TurnOutcome.FAILED)
        self.assertEqual(self.classify(None, 0), TurnOutcome.FAILED)


class CodexTests(unittest.TestCase):
    def argv(self, native=None):
        return codex.build_argv(
            "codex",
            model="gpt",
            mcp_argv=["/py", "-I", "/m.py", "--turn-dir", "/t", "--role", "builder"],
            role_text='be "careful"\nplease',
            last_message_file="/l.txt",
            git_common_dir="/git",
            native_session_id=native,
        )

    def test_first_turn_and_resume_shape(self):
        first = self.argv()
        self.assertEqual(first[:2], ["codex", "exec"])
        self.assertEqual(first[-1], "-")
        self.assertIn("--json", first)
        resumed = self.argv("thread-1")
        self.assertEqual(resumed[:3], ["codex", "exec", "resume"])
        self.assertEqual(resumed[-2:], ["thread-1", "-"])

    def test_config_values_are_valid_toml(self):
        import tomllib

        values = [a for i, a in enumerate(self.argv()) if i and self.argv()[i - 1] == "-c"]
        for item in values:
            tomllib.loads(item)
        joined = tomllib.loads("\n".join(values))
        server = joined["mcp_servers"]["factory-tools"]
        self.assertEqual(server["command"], "/py")
        self.assertEqual(server["args"][-1], "builder")
        self.assertEqual(joined["developer_instructions"], 'be "careful"\nplease')
        self.assertEqual(joined["sandbox_workspace_write"]["writable_roots"], ["/git"])

    def test_parser(self):
        parser = codex.Parser()
        self.assertEqual(parser.feed({"type": "thread.started", "thread_id": "T"}), [(EventType.STARTED, {"native_session_id": "T"})])
        self.assertEqual(parser.feed({"type": "item.completed", "item": {"type": "agent_message", "text": "hi"}}), [(EventType.TEXT, {"text": "hi"})])
        self.assertEqual(parser.feed({"type": "item.completed", "item": {"type": "command_execution"}}), [(EventType.TOOL, {"name": "command_execution"})])
        self.assertEqual(parser.feed({"type": "turn.completed", "usage": {"input_tokens": 2, "output_tokens": 3}}), [(EventType.USAGE, {"tokens": 5})])
        self.assertTrue(parser.done)
        self.assertEqual(parser.classify(0, "", False)[0], TurnOutcome.COMPLETED)

    def test_classify(self):
        def classify(failure, stderr="", exit_code=1, resumed=False):
            parser = codex.Parser()
            parser.feed({"type": "turn.failed", "error": {"message": failure}})
            return parser.classify(exit_code, stderr, resumed)[0]

        self.assertEqual(classify("You've hit your usage limit"), TurnOutcome.ACCOUNT_LIMIT)
        self.assertEqual(classify("stream disconnected before completion"), TurnOutcome.TRANSIENT)
        self.assertEqual(classify("no rollout found for thread", resumed=True), TurnOutcome.RESUME_FAILED)
        self.assertEqual(classify("something else"), TurnOutcome.FAILED)
        self.assertEqual(codex.Parser().classify(-9, "", False)[0], TurnOutcome.INTERRUPTED)

    @unittest.skipUnless(shutil.which("codex"), "codex CLI not installed")
    def test_exec_policy_against_the_real_cli(self):
        """No model and no account involved: `codex execpolicy check` is local."""
        with tempfile.TemporaryDirectory() as tmp:
            rules = codex.install_exec_policy(tmp)

            def decision(*command):
                out = subprocess.run(
                    ["codex", "execpolicy", "check", "--rules", rules, *command],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    env={"PATH": os.environ["PATH"], "HOME": tmp, "CODEX_HOME": tmp},
                )
                self.assertEqual(out.returncode, 0, out.stderr[-300:])
                return json.loads(out.stdout).get("decision")

            self.assertEqual(decision("git", "push", "--force", "origin", "x"), "forbidden")
            self.assertEqual(decision("git", "push", "origin", "main"), "forbidden")
            self.assertEqual(decision("gh", "pr", "merge", "1"), "forbidden")
            self.assertEqual(decision("git", "tag", "v1"), "forbidden")
            self.assertNotEqual(decision("git", "status"), "forbidden")
            self.assertNotEqual(decision("git", "push", "origin", "factory/x"), "forbidden")


class McpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        os.makedirs(os.path.join(self.dir, "recall"))
        self.server = mcp_tools.Server(self.dir, "builder")
        self.next = 0

    def call(self, name, server=None, **args):
        self.next += 1
        msg = {"jsonrpc": "2.0", "id": self.next, "method": "tools/call", "params": {"name": name, "arguments": args}}
        return (server or self.server).handle(msg)["result"]

    def inbox(self):
        path = os.path.join(self.dir, "inbox.jsonl")
        if not os.path.exists(path):
            return []
        return [json.loads(line) for line in open(path)]

    def test_lists_stay_in_step_with_the_contract(self):
        self.assertEqual(set(mcp_tools.STATUSES), {s.value for s in ReportStatus})
        self.assertEqual(set(mcp_tools.VERDICTS), {v.value for v in Verdict})

    def test_initialize_echoes_the_clients_version(self):
        reply = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2099-01-01"}})
        self.assertEqual(reply["result"]["protocolVersion"], "2099-01-01")
        self.assertEqual(reply["result"]["serverInfo"]["name"], "factory-tools")
        self.assertIn("tools", reply["result"]["capabilities"])

    def test_tools_list(self):
        reply = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual({t["name"] for t in reply["result"]["tools"]}, {"report", "reply", "follow", "unrelated", "handback", "recall"})
        for tool in reply["result"]["tools"]:
            self.assertEqual(tool["inputSchema"]["type"], "object")

    def test_notifications_get_no_reply(self):
        self.assertIsNone(self.server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_ping_and_unknown_method(self):
        self.assertEqual(self.server.handle({"id": 1, "method": "ping"})["result"], {})
        self.assertEqual(self.server.handle({"id": 2, "method": "nope"})["error"]["code"], -32601)
        self.assertEqual(self.server.handle({"id": 3})["error"]["code"], -32600)

    def test_unknown_tool_and_private_attribute(self):
        for name in ("nope", "_append", "__init__", "serve", None):
            msg = {"id": 1, "method": "tools/call", "params": {"name": name, "arguments": {}}}
            self.assertEqual(self.server.handle(msg)["error"]["code"], -32602, name)

    def test_every_status_writes_a_valid_entry(self):
        for status in mcp_tools.STATUSES:
            self.assertNotIn("isError", self.call("report", status=status, summary="s"))
        entries = [entry_from_wire({**r, "seq": i}) for i, r in enumerate(self.inbox(), 1)]
        self.assertEqual([e.report.status.value for e in entries], list(mcp_tools.STATUSES))

    def test_report_validation(self):
        for args in (
            {"status": "nonsense", "summary": "x"},
            {"summary": "x"},
            {"status": "ready"},
            {"status": "ready", "summary": "   "},
            {"status": "ready", "summary": 5},
            {"status": "ready", "summary": "x", "pr": "12"},
            {"status": "ready", "summary": "x", "pr": 0},
            {"status": "ready", "summary": "x", "pr": True},
            {"status": "ready", "summary": "x", "question": "why?"},
            {"status": "blocked", "summary": "x", "question": ["one", "two"]},
            {"status": "blocked", "summary": "x", "question": " "},
            {"status": "ready", "summary": "x", "verdict": "pass"},
            {"status": "ready", "summary": "x", "findings": "y"},
            {"status": "ready", "summary": "x" * 9000},
        ):
            with self.subTest(args=args):
                self.assertTrue(self.call("report", **args).get("isError"))
        self.assertEqual(self.inbox(), [])

    def test_one_question(self):
        self.assertNotIn("isError", self.call("report", status="blocked", summary="s", question="which one?"))
        self.assertNotIn("isError", self.call("report", status="waiting", summary="s", question="now?"))
        self.assertEqual(self.inbox()[0]["report"]["question"], "which one?")

    def test_reviewer(self):
        reviewer = mcp_tools.Server(self.dir, "reviewer")
        self.assertTrue(self.call("report", reviewer, status="ready", summary="s").get("isError"))
        self.assertTrue(self.call("report", reviewer, status="ready", summary="s", verdict="maybe").get("isError"))
        self.assertTrue(self.call("report", reviewer, status="ready", summary="s", verdict="changes_requested").get("isError"))
        self.assertNotIn("isError", self.call("report", reviewer, status="ready", summary="s", verdict="pass"))
        self.assertNotIn("isError", self.call("report", reviewer, status="ready", summary="s", verdict="changes_requested", findings="f"))
        self.assertNotIn("isError", self.call("report", reviewer, status="blocked", summary="s"))
        self.assertEqual(entry_from_wire({**self.inbox()[1], "seq": 1}).report.verdict, Verdict.CHANGES_REQUESTED)

    def test_a_reviewer_is_offered_only_report_and_recall(self):
        reviewer = mcp_tools.Server(self.dir, "reviewer")
        reply = reviewer.handle({"id": 1, "method": "tools/list"})
        self.assertEqual({t["name"] for t in reply["result"]["tools"]}, {"report", "recall"})
        for name in ("reply", "follow", "unrelated", "handback"):
            msg = {"id": 2, "method": "tools/call", "params": {"name": name, "arguments": {"purpose": "ack", "text": "t", "thread": "t"}}}
            self.assertEqual(reviewer.handle(msg)["error"]["code"], -32602)
        self.assertEqual(self.inbox(), [])

    def test_other_tools_match_the_contract(self):
        self.call("reply", purpose="answer", text="t")
        self.call("follow", thread="th1")
        self.call("unrelated", thread="th2")
        self.call("handback", trigger="tr1", reason="out of scope")
        entries = [entry_from_wire({**r, "seq": i}) for i, r in enumerate(self.inbox(), 1)]
        self.assertEqual(
            [e.kind for e in entries], [EntryKind.REPLY, EntryKind.FOLLOW, EntryKind.UNRELATED, EntryKind.HANDBACK]
        )
        self.assertEqual(entries[0].text, "t")
        self.assertEqual(entries[2].thread, "th2")
        self.assertEqual((entries[3].trigger, entries[3].text), ("tr1", "out of scope"))
        self.assertTrue(self.call("reply", purpose="spam", text="t").get("isError"))
        self.assertTrue(self.call("follow").get("isError"))

    def test_recall_blocks_until_answered(self):
        out = {}
        thread = threading.Thread(target=lambda: out.update(self.call("recall", id="x")))
        thread.start()
        while not self.inbox():
            pass
        record = self.inbox()[0]
        self.assertEqual(record["kind"], "recall")
        self.assertEqual(record["object_id"], "x")
        Path(self.dir, "recall", record["request_id"]).write_text("body")
        thread.join(5)
        self.assertEqual(out["content"][0]["text"], "body")

    def test_recall_times_out(self):
        server = mcp_tools.Server(self.dir, "builder", recall_timeout=0.05)
        result = self.call("recall", server, id="x")
        self.assertTrue(result["isError"])
        self.assertIn("timed out", result["content"][0]["text"])

    def test_unwritable_inbox_is_an_error_not_a_crash(self):
        server = mcp_tools.Server(os.path.join(self.dir, "missing"), "builder")
        self.assertTrue(self.call("report", server, status="ready", summary="s").get("isError"))

    def test_serve_loop_over_stdio(self):
        stdin = io.StringIO('{"jsonrpc":"2.0","id":1,"method":"ping"}\n\nnot json\n[1]\n{"jsonrpc":"2.0","method":"notifications/x"}\n')
        stdout = io.StringIO()
        self.server.serve(stdin, stdout)
        lines = [json.loads(x) for x in stdout.getvalue().splitlines()]
        self.assertEqual(lines[0]["result"], {})
        self.assertEqual(lines[1]["error"]["code"], -32700)
        self.assertEqual(lines[2]["error"]["code"], -32600)
        self.assertEqual(len(lines), 3)

    def test_runs_under_python_I_and_drops_credentials(self):
        path = Path(mcp_tools.__file__).resolve()
        out = subprocess.run(
            [sys.executable, "-I", str(path), "--turn-dir", self.dir, "--role", "builder"],
            input='{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"report","arguments":{"status":"ready","summary":"s"}}}\n',
            capture_output=True,
            text=True,
            env={"CLAUDE_CODE_OAUTH_TOKEN": "x"},
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("isError", json.loads(out.stdout)["result"])
        self.assertEqual(self.inbox()[0]["report"]["status"], "ready")

    def test_bad_arguments_exit_nonzero(self):
        path = Path(mcp_tools.__file__).resolve()
        out = subprocess.run([sys.executable, "-I", str(path)], capture_output=True, text=True)
        self.assertEqual(out.returncode, 2)


if __name__ == "__main__":
    unittest.main()
