"""agentd end to end, as a real process on a real unix socket, with a fake agent CLI."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from lumbridge_factory.agentd.client import AgentdClient
from lumbridge_factory.core.executor import (
    EntryKind,
    EventType,
    Executor,
    ExecutorError,
    InputKind,
    PromptParts,
    ReportStatus,
    Role,
    StartRequest,
    TurnEvent,
    TurnInput,
    TurnOutcome,
    TurnRequest,
    TurnResult,
    WorktreeSpec,
    to_wire,
)

REPO = Path(__file__).resolve().parent.parent
FAKE = Path(__file__).resolve().with_name("test_agentd_fake_cli.py")
TOKEN = "FAKE-TOKEN-a1b2c3d4e5f6-do-not-leak"
GIT_ENV = {
    "PATH": os.environ["PATH"],
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}


def git(cwd, *args) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True, env=GIT_ENV).stdout.strip()


def alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


class AgentdCase(unittest.TestCase):
    extra_config: dict = {}

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="agentd-")
        self.addCleanup(self._cleanup)
        self.root = Path(os.path.realpath(self._tmp.name))
        self.home = self.root / "home"
        self.home.mkdir()
        self.origin = self.root / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True, env=GIT_ENV)
        seed = self.root / "seed"
        subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True, env=GIT_ENV)
        (seed / "README.md").write_text("hello\n")
        git(seed, "add", ".")
        git(seed, "commit", "-qm", "init")
        git(seed, "remote", "add", "origin", str(self.origin))
        git(seed, "push", "-q", "origin", "main")
        self.seed_sha = git(seed, "rev-parse", "HEAD")
        secrets = self.root / "secrets"
        secrets.mkdir()
        self.token_file = secrets / "tokens.env"
        self.token_file.write_text(f'# comment\nexport OTHER=x\nexport CLAUDE_CODE_OAUTH_TOKEN="{TOKEN}"\n')
        self.socket_path = str(self.root / "run" / "agentd.sock")
        self.config = {
            "socket": self.socket_path,
            "state_dir": str(self.root / "state"),
            "worktrees_dir": str(self.root / "worktrees"),
            "repos_dir": str(self.root / "repos"),
            "agent_home": str(self.home),
            "repos": {"acme/sandbox": str(self.origin)},
            "tools": {"claude": {"enabled": True, "bin": str(FAKE)}, "codex": {"enabled": False, "bin": "/nonexistent/codex"}},
            "accounts": {
                "claude-test": {"tool": "claude", "env_file": str(self.token_file), "env_var": "CLAUDE_CODE_OAUTH_TOKEN"},
                "codex-test": {"tool": "codex", "codex_home": str(self.root / "codex-home")},
            },
            "child_path": os.environ["PATH"],
            "child_env": {
                "GIT_AUTHOR_NAME": "bot",
                "GIT_AUTHOR_EMAIL": "bot@example.invalid",
                "GIT_COMMITTER_NAME": "bot",
                "GIT_COMMITTER_EMAIL": "bot@example.invalid",
            },
            "kill_grace_seconds": 0.5,
            "post_result_grace_seconds": 0.5,
            **self.extra_config,
        }
        self.proc = None
        self.client = AgentdClient(self.socket_path)
        self.start_daemon()
        self.counter = 0

    def _cleanup(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        # Reviewer worktrees are read-only and would stop rmtree.
        for dirpath, dirnames, _ in os.walk(self.root):
            for name in dirnames:
                try:
                    os.chmod(os.path.join(dirpath, name), 0o755)
                except OSError:
                    pass
            try:
                os.chmod(dirpath, 0o755)
            except OSError:
                pass
        self._tmp.cleanup()

    def start_daemon(self):
        config_path = self.root / "agentd.json"
        config_path.write_text(json.dumps(self.config))
        env = {"PATH": os.environ["PATH"], "PYTHONPATH": str(REPO), "SECRET_FROM_PARENT": "must-not-reach-a-child"}
        log = open(self.root / "agentd.log", "a")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "lumbridge_factory.agentd", "--config", str(config_path)], env=env, stdout=log, stderr=log, cwd=REPO
        )
        log.close()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                self.client.hello()
                return
            except ExecutorError:
                self.assertIsNone(self.proc.poll(), (self.root / "agentd.log").read_text())
                time.sleep(0.02)
        self.fail("agentd didn't come up")

    def stop_daemon(self, sig=signal.SIGTERM):
        self.proc.send_signal(sig)
        self.proc.wait(30)

    # --- helpers -------------------------------------------------------------

    def session(self, name="abc", role=Role.BUILDER, wt_name=None, **spec) -> str:
        spec = {"repo": "acme/sandbox", "name": wt_name or name, "branch": f"factory/{name}", **spec}
        return self.client.start(
            StartRequest(
                ticket=f"ticket-{name}",
                assignment=f"assign-{name}",
                role=role,
                worktree=WorktreeSpec(**spec),
                prompt_parts=PromptParts(system="You are a builder.", rule_instructions="Rule: be small."),
                account="claude-test",
                model="sonnet",
            )
        )

    def request(self, session, prompt, native=None, turn_id=None, limit=60, account="claude-test") -> TurnRequest:
        self.counter += 1
        return TurnRequest(
            turn_id=turn_id or f"turn-{self.counter}",
            session_id=session,
            native_session_id=native,
            inputs=(TurnInput(InputKind.ASSIGNMENT, prompt),),
            account=account,
            model="sonnet",
            limit_seconds=limit,
        )

    def run_turn(self, *args, **kw) -> tuple[list[TurnEvent], TurnResult]:
        items = list(self.client.turn(self.request(*args, **kw)))
        self.assertIsInstance(items[-1], TurnResult)
        return items[:-1], items[-1]

    def start_turn_bg(self, session, prompt, **kw) -> tuple[str, "object"]:
        request = self.request(session, prompt, **kw)
        stream = self.client.turn(request)
        return request.turn_id, stream

    def wait_for_file(self, path: Path, seconds=10.0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if path.exists() and path.stat().st_size:
                return
            time.sleep(0.02)
        self.fail(f"{path} never appeared; log:\n{(self.root / 'agentd.log').read_text()[-800:]}")

    def dump(self, native) -> dict:
        return json.loads((self.home / f"dump-{native}.json").read_text())

    def turn_dir(self, turn_id) -> Path:
        return self.root / "state" / "turns" / turn_id


class ProtocolTests(AgentdCase):
    def test_client_is_an_executor(self):
        self.assertIsInstance(self.client, Executor)

    def test_hello_lists_enabled_tools_only(self):
        self.assertEqual(self.client.hello(), {"protocol": 1, "tools": ["claude"]})

    def raw(self, line: bytes) -> list[dict]:
        sock = socket.socket(socket.AF_UNIX)
        sock.connect(self.socket_path)
        sock.sendall(line)
        reader = sock.makefile("rb")
        out = [json.loads(x) for x in reader]
        sock.close()
        return out

    def test_wrong_version(self):
        reply = self.raw(b'{"v": 99, "op": "hello"}\n')[0]
        self.assertEqual(reply["error"]["code"], "protocol_mismatch")
        self.assertFalse(reply["error"]["retryable"])

    def test_garbage_and_unknown_op(self):
        self.assertEqual(self.raw(b"not json\n")[0]["error"]["code"], "bad_request")
        self.assertEqual(self.raw(b'{"v":1,"op":"nope"}\n')[0]["error"]["code"], "bad_request")
        self.assertEqual(self.raw(b'{"v":1,"op":"_ok"}\n')[0]["error"]["code"], "bad_request")
        self.assertEqual(self.raw(b'{"v":1,"op":"start","args":{}}\n')[0]["error"]["code"], "bad_request")

    def test_socket_mode(self):
        self.assertEqual(stat.S_IMODE(os.stat(self.socket_path).st_mode), 0o660)

    def test_stream_is_events_then_one_result(self):
        session = self.session()
        request = self.request(session, "ok go")
        payload = json.dumps({"v": 1, "op": "turn", "args": to_wire(request)})
        lines = self.raw(payload.encode() + b"\n")
        self.assertTrue(all("event" in line for line in lines[:-1]))
        self.assertTrue(lines[-1]["ok"])
        self.assertEqual(lines[-1]["result"]["outcome"], "completed")

    def test_unknown_session(self):
        with self.assertRaises(ExecutorError) as caught:
            list(self.client.turn(self.request("s-00000000000000000000", "ok")))
        self.assertEqual(caught.exception.code, "unknown_session")
        with self.assertRaises(ExecutorError) as caught:
            list(self.client.turn(self.request("../../etc", "ok")))
        self.assertEqual(caught.exception.code, "unknown_session")

    def test_bad_turn_ids_and_accounts(self):
        session = self.session()
        with self.assertRaises(ExecutorError) as caught:
            list(self.client.turn(self.request(session, "ok", turn_id="../escape")))
        self.assertEqual(caught.exception.code, "bad_request")
        with self.assertRaises(ExecutorError) as caught:
            list(self.client.turn(self.request(session, "ok", account="nobody")))
        self.assertEqual(caught.exception.code, "bad_request")
        for op in (self.client.collect, self.client.forget):
            with self.assertRaises(ExecutorError):
                op("../escape")

    def test_second_agentd_refuses_a_live_socket(self):
        config_path = self.root / "agentd.json"
        out = subprocess.run(
            [sys.executable, "-m", "lumbridge_factory.agentd", "--config", str(config_path)],
            env={"PATH": os.environ["PATH"], "PYTHONPATH": str(REPO)},
            capture_output=True,
            text=True,
            cwd=REPO,
            timeout=30,
        )
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("already listening", out.stderr)
        self.assertEqual(self.client.hello()["protocol"], 1)


class SessionTests(AgentdCase):
    def test_start_prepares_worktree_and_runs_no_model(self):
        session = self.session()
        wt = self.root / "worktrees" / "abc"
        self.assertEqual(git(wt, "symbolic-ref", "--short", "HEAD"), "factory/abc")
        self.assertEqual(git(wt, "rev-parse", "HEAD"), self.seed_sha)
        self.assertEqual(git(wt, "remote", "get-url", "origin"), str(self.origin))
        self.assertEqual(list(self.home.glob("dump-*")), [])
        session_dir = self.root / "state" / "sessions" / session
        for name in ("policy.json", "settings.json", "role.md", "session.json"):
            self.assertTrue((session_dir / name).exists(), name)

    def test_start_is_idempotent_and_keeps_work(self):
        session = self.session()
        wt = self.root / "worktrees" / "abc"
        (wt / "work.txt").write_text("in progress\n")
        self.assertEqual(self.session(), session)
        self.assertEqual((wt / "work.txt").read_text(), "in progress\n")

    def test_start_survives_a_restart_without_network(self):
        session = self.session()
        self.stop_daemon()
        shutil.move(str(self.origin), str(self.origin) + ".gone")
        self.start_daemon()
        self.assertEqual(self.session(), session)

    def test_pre_trust_only_in_agent_home(self):
        self.session()
        data = json.loads((self.home / ".claude.json").read_text())
        project = data["projects"][str(self.root / "worktrees" / "abc")]
        self.assertTrue(project["hasTrustDialogAccepted"])
        self.assertTrue(data["hasCompletedOnboarding"])

    def test_pre_trust_keeps_existing_data(self):
        (self.home / ".claude.json").write_text(json.dumps({"keep": 1, "projects": {"/x": {"y": 2}}}))
        self.session()
        data = json.loads((self.home / ".claude.json").read_text())
        self.assertEqual(data["keep"], 1)
        self.assertEqual(data["projects"]["/x"], {"y": 2})

    def test_new_worktree_from_a_pushed_branch(self):
        seed = self.root / "seed"
        git(seed, "checkout", "-qb", "factory/back")
        (seed / "more.txt").write_text("x\n")
        git(seed, "add", ".")
        git(seed, "commit", "-qm", "more")
        git(seed, "push", "-q", "origin", "factory/back")
        sha = git(seed, "rev-parse", "HEAD")
        self.session("back")
        self.assertEqual(git(self.root / "worktrees" / "back", "rev-parse", "HEAD"), sha)

    def test_reviewer_worktree_is_pinned_and_read_only(self):
        self.session("rev", role=Role.REVIEWER, branch="", pinned_sha=self.seed_sha, read_only=True)
        wt = self.root / "worktrees" / "rev"
        self.assertEqual(git(wt, "rev-parse", "HEAD"), self.seed_sha)
        with self.assertRaises(subprocess.CalledProcessError):
            git(wt, "symbolic-ref", "-q", "HEAD")
        with self.assertRaises(PermissionError):
            (wt / "new.txt").write_text("x")
        with self.assertRaises(PermissionError):
            (wt / "README.md").write_text("x")
        session_dir = next(iter((self.root / "state" / "sessions").iterdir()))
        policy = json.loads((session_dir / "policy.json").read_text())
        self.assertTrue(policy["read_only"])
        self.assertEqual(policy["push_branches"], [])
        settings = json.loads((session_dir / "settings.json").read_text())
        self.assertNotIn("Write", settings["permissions"]["allow"])
        self.assertNotIn("Edit", settings["permissions"]["allow"])

    def test_bad_specs(self):
        for spec in (
            {"wt_name": "../escape"},
            {"wt_name": "a/b"},
            {"branch": "../main"},
            {"branch": "--force"},
            {"repo": "acme/unknown"},
            {"repo": "../x"},
            {"base": "no-such-branch"},
            {"pinned_sha": "not-hex"},
        ):
            with self.subTest(spec=spec), self.assertRaises(ExecutorError) as caught:
                self.session("bad", **spec)
            self.assertEqual(caught.exception.code, "bad_request")

    def test_worktree_never_holds_agent_files_or_tokens(self):
        session = self.session()
        _, result = self.run_turn(session, "ok go")
        self.assertEqual(result.outcome, TurnOutcome.COMPLETED)
        self.assertEqual(git(self.root / "worktrees" / "abc", "status", "--porcelain"), "")


class TurnTests(AgentdCase):
    def test_a_turn_reports_through_the_real_mcp_server(self):
        session = self.session()
        events, result = self.run_turn(session, "ok go")
        self.assertEqual(result.outcome, TurnOutcome.COMPLETED)
        self.assertEqual(result.session_id, session)
        self.assertTrue(result.native_session_id)
        self.assertEqual([e.kind for e in result.entries], [EntryKind.REPLY, EntryKind.REPORT])
        self.assertEqual([e.seq for e in result.entries], [1, 2])
        report = result.entries[1].report
        self.assertEqual(report.status, ReportStatus.READY)
        self.assertEqual(report.pr, 7)
        self.assertEqual(report.summary, "all done")
        self.assertEqual(result.entries[0].purpose, "ack")
        self.assertEqual(result.tokens, 150)  # final usage, cache reads not counted
        types = [e.type for e in events]
        self.assertEqual(types[0], EventType.STARTED)
        self.assertEqual(events[0].data["native_session_id"], result.native_session_id)
        self.assertIn(EventType.TEXT, types)
        self.assertIn(EventType.TOOL, types)
        self.assertIn(EventType.USAGE, types)
        self.assertEqual(types.count(EventType.INBOX), 2)
        self.assertEqual([e.seq for e in events], list(range(1, len(events) + 1)))

    def test_inputs_are_joined_verbatim_as_the_user_message(self):
        session = self.session()
        request = TurnRequest(
            turn_id="joined",
            session_id=session,
            native_session_id=None,
            inputs=(TurnInput(InputKind.ASSIGNMENT, "ok first  line"), TurnInput(InputKind.TRIGGER, "```\nsecond\n```")),
            account="claude-test",
            model="sonnet",
        )
        result = list(self.client.turn(request))[-1]
        self.assertEqual(self.dump(result.native_session_id)["prompt"], "ok first  line\n\n```\nsecond\n```")

    def test_first_turn_argv(self):
        session = self.session()
        _, result = self.run_turn(session, "ok go")
        argv = self.dump(result.native_session_id)["argv"]
        self.assertIn("-p", argv)
        self.assertEqual(argv[argv.index("--output-format") + 1], "stream-json")
        self.assertIn("--verbose", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "")
        self.assertIn("--strict-mcp-config", argv)
        self.assertNotIn("--resume", argv)
        role = Path(argv[argv.index("--append-system-prompt-file") + 1]).read_text()
        self.assertIn("You are a builder.", role)
        self.assertLess(role.index("You are a builder."), role.index("Rule: be small."))
        self.assertEqual(self.dump(result.native_session_id)["cwd"], str(self.root / "worktrees" / "abc"))

    def test_resume(self):
        session = self.session()
        _, first = self.run_turn(session, "ok the-code-word-is-heron")
        events, second = self.run_turn(session, "recallme what was it", native=first.native_session_id)
        self.assertEqual(second.outcome, TurnOutcome.COMPLETED)
        self.assertEqual(second.native_session_id, first.native_session_id)
        argv = self.dump(first.native_session_id)["argv"]
        self.assertEqual(argv[argv.index("--resume") + 1], first.native_session_id)
        texts = [e.data["text"] for e in events if e.type == EventType.TEXT]
        self.assertTrue(any("heron" in t for t in texts), texts)
        self.assertEqual([e.seq for e in second.entries], [1])  # seq counts per turn

    def test_resume_failure_is_reported_not_papered_over(self):
        session = self.session()
        _, result = self.run_turn(session, "resume-missing", native="11111111-1111-1111-1111-111111111111")
        self.assertEqual(result.outcome, TurnOutcome.RESUME_FAILED)

    def test_outcomes(self):
        session = self.session()
        for prompt, outcome in (
            ("overloaded", TurnOutcome.TRANSIENT),
            ("limit", TurnOutcome.ACCOUNT_LIMIT),
            ("exit3", TurnOutcome.FAILED),
            ("silent", TurnOutcome.FAILED),
            ("crash", TurnOutcome.INTERRUPTED),
        ):
            with self.subTest(prompt=prompt):
                _, result = self.run_turn(session, prompt)
                self.assertEqual(result.outcome, outcome, result.error)
                self.assertEqual(self.client.running(), [])
        _, failed = self.run_turn(session, "exit3")
        self.assertIn("boom", failed.error)

    def test_a_turn_without_a_report_still_completes(self):
        # Sending the reminder turn is factoryd's job (PROTOCOL.md).
        _, result = self.run_turn(self.session(), "no-report")
        self.assertEqual(result.outcome, TurnOutcome.COMPLETED)
        self.assertEqual(result.entries, ())

    def test_bad_report_goes_back_to_the_model_and_not_into_the_inbox(self):
        events, result = self.run_turn(self.session(), "bad-report")
        self.assertEqual(result.entries, ())
        self.assertTrue(any(e.data.get("text") == "error:True" for e in events if e.type == EventType.TEXT))

    def test_forged_inbox_lines_get_no_seq(self):
        session = self.session()
        request = self.request(session, "report-then-hang", limit=30)
        stream = self.client.turn(request)
        for item in stream:
            if isinstance(item, TurnEvent) and item.type == EventType.INBOX:
                break
        inbox = self.turn_dir(request.turn_id) / "inbox.jsonl"
        with inbox.open("a") as fh:
            fh.write('{"kind":"nonsense"}\nnot json\n{"kind":"report","report":{"status":"bogus"}}\n')
            fh.write('{"kind":"report","report":{"status":"ready","summary":"late"}}\n')
        for item in stream:
            if isinstance(item, TurnEvent) and item.type == EventType.INBOX:
                break
        self.client.stop(session)
        result = self.finish(request.turn_id)
        self.assertEqual([e.seq for e in result.entries], [1, 2])
        self.assertEqual(result.entries[1].report.summary, "late")

    def finish(self, turn_id, seconds=20.0) -> TurnResult:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            result = self.client.collect(turn_id)
            if result is not None:
                return result
            time.sleep(0.02)
        self.fail("no result")

    def test_recall_round_trip(self):
        session = self.session()
        request = self.request(session, "recall", limit=30)
        answered = False
        result = None
        texts = []
        for item in self.client.turn(request):
            if isinstance(item, TurnEvent):
                if item.type == EventType.RECALL_REQUEST:
                    self.assertEqual(item.data["object_id"], "trigger-9")
                    self.client.answer_recall(session, item.data["request_id"], "the full body")
                    answered = True
                elif item.type == EventType.TEXT:
                    texts.append(item.data["text"])
            else:
                result = item
        self.assertTrue(answered)
        self.assertEqual(result.outcome, TurnOutcome.COMPLETED)
        self.assertIn("answer=the full body", texts)
        self.assertEqual(result.entries, ())  # recall is not an entry

    def test_no_two_turns_on_one_session(self):
        session = self.session()
        turn_id, stream = self.start_turn_bg(session, "hang", limit=30)
        next(iter(stream))
        with self.assertRaises(ExecutorError) as caught:
            list(self.client.turn(self.request(session, "ok")))
        self.assertEqual(caught.exception.code, "unavailable")
        self.assertTrue(caught.exception.retryable)
        self.client.stop(session)
        self.finish(turn_id)

    def test_two_sessions_run_at_once(self):
        a, b = self.session("aaa"), self.session("bbb")
        ta, sa = self.start_turn_bg(a, "hang", limit=30)
        tb, sb = self.start_turn_bg(b, "hang", limit=30)
        next(iter(sa)), next(iter(sb))
        self.assertEqual({t.turn_id for t in self.client.running()}, {ta, tb})
        self.client.stop(a)
        self.assertEqual(self.finish(ta).outcome, TurnOutcome.STOPPED)
        self.assertEqual({t.turn_id for t in self.client.running()}, {tb})
        self.client.stop(b)
        self.finish(tb)

    def test_failed_spawn_is_a_clean_result(self):
        self.stop_daemon()
        self.config["tools"]["claude"]["bin"] = "/nonexistent/claude"
        self.start_daemon()
        _, result = self.run_turn(self.session(), "ok")
        self.assertEqual(result.outcome, TurnOutcome.FAILED)
        self.assertIn("can't start", result.error)


class IdempotencyTests(AgentdCase):
    def test_finished_turn_replays_and_never_respawns(self):
        session = self.session()
        request = self.request(session, "ok go")
        first = list(self.client.turn(request))
        dumps = list(self.home.glob("dump-*"))
        second = list(self.client.turn(request))
        self.assertEqual(first[-1], second[-1])
        self.assertEqual(first[:-1], second[:-1])
        self.assertEqual(list(self.home.glob("dump-*")), dumps)

    def test_attach_to_a_running_turn_after_dropping_the_connection(self):
        session = self.session()
        request = self.request(session, "report-then-hang", limit=30)
        stream = self.client.turn(request)
        first_events = []
        for item in stream:
            first_events.append(item)
            if item.type == EventType.INBOX:
                break
        stream.close()  # a dropped client doesn't stop a turn
        self.assertEqual([t.turn_id for t in self.client.running()], [request.turn_id])
        attached = self.client.turn(request)
        replayed = [next(attached) for _ in first_events]
        self.assertEqual(replayed, first_events)
        self.client.stop(session)
        rest = list(attached)
        self.assertEqual(rest[-1].outcome, TurnOutcome.STOPPED)
        self.assertEqual(rest[-1].entries[0].report.summary, "before the hang")

    def test_collect_and_forget(self):
        session = self.session()
        request = self.request(session, "ok go")
        self.assertIsNone(self.client.collect(request.turn_id))
        result = list(self.client.turn(request))[-1]
        self.assertEqual(self.client.collect(request.turn_id), result)
        self.client.forget(request.turn_id)
        self.assertIsNone(self.client.collect(request.turn_id))
        self.assertFalse(self.turn_dir(request.turn_id).exists())
        self.client.forget(request.turn_id)  # idempotent

    def test_forget_leaves_a_running_turn_alone(self):
        session = self.session()
        turn_id, stream = self.start_turn_bg(session, "hang", limit=30)
        next(iter(stream))
        self.client.forget(turn_id)
        self.assertTrue(self.turn_dir(turn_id).exists())
        self.client.stop(session)
        self.assertEqual(list(stream)[-1].outcome, TurnOutcome.STOPPED)

    def test_journal_is_written_before_the_spawn(self):
        self.stop_daemon()
        self.config["tools"]["claude"]["bin"] = "/nonexistent/claude"
        self.start_daemon()
        session = self.session()
        request = self.request(session, "ok")
        list(self.client.turn(request))
        journal = json.loads((self.turn_dir(request.turn_id) / "request.json").read_text())
        self.assertEqual(journal["turn_id"], request.turn_id)


class StopAndTimeoutTests(AgentdCase):
    def kids(self) -> dict:
        self.wait_for_file(self.home / "kids.json")
        return json.loads((self.home / "kids.json").read_text())

    def assertAllGone(self, pids, seconds=5.0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and any(alive(p) for p in pids):
            time.sleep(0.02)
        self.assertEqual([p for p in pids if alive(p)], [])

    def marked(self, turn_id) -> list[int]:
        from lumbridge_factory.agentd.procs import pids_with_marker

        return pids_with_marker(turn_id)

    def test_stop(self):
        session = self.session()
        turn_id, stream = self.start_turn_bg(session, "hang", limit=30)
        self.assertEqual(next(iter(stream)).type, EventType.STARTED)
        self.client.stop(session)
        result = list(stream)[-1]
        self.assertEqual(result.outcome, TurnOutcome.STOPPED)
        self.assertTrue(result.native_session_id)  # so a later turn can still resume
        self.assertEqual(self.client.running(), [])

    def test_stop_is_idempotent_and_fine_when_idle(self):
        session = self.session()
        self.client.stop(session)
        self.client.stop(session)
        self.client.stop("s-00000000000000000000")

    def test_stop_escalates_past_an_ignored_sigterm(self):
        session = self.session()
        turn_id, stream = self.start_turn_bg(session, "hang-ignore-term", limit=30)
        next(iter(stream))
        time.sleep(0.2)
        began = time.monotonic()
        self.client.stop(session)
        result = list(stream)[-1]
        self.assertEqual(result.outcome, TurnOutcome.STOPPED)
        self.assertLess(time.monotonic() - began, 10)
        self.assertEqual(self.marked(turn_id), [])

    def test_stop_leaves_no_orphans_in_or_out_of_the_group(self):
        session = self.session()
        turn_id, stream = self.start_turn_bg(session, "kids hang", limit=30)
        next(iter(stream))
        kids = self.kids()
        self.assertGreater(len(self.marked(turn_id)), 2)
        self.client.stop(session)
        self.assertEqual(list(stream)[-1].outcome, TurnOutcome.STOPPED)
        self.assertAllGone(kids.values())
        self.assertEqual(self.marked(turn_id), [])

    def test_a_clean_exit_also_sweeps_background_jobs(self):
        session = self.session()
        request = self.request(session, "kids exit", limit=30)
        result = list(self.client.turn(request))[-1]
        self.assertEqual(result.outcome, TurnOutcome.COMPLETED)
        self.assertAllGone(self.kids().values())
        self.assertEqual(self.marked(request.turn_id), [])

    def test_timeout(self):
        session = self.session()
        request = self.request(session, "kids hang", limit=1)
        began = time.monotonic()
        result = list(self.client.turn(request))[-1]
        self.assertEqual(result.outcome, TurnOutcome.TIMEOUT)
        self.assertLess(time.monotonic() - began, 15)
        self.assertIn("limit", result.error)
        self.assertGreaterEqual(result.wall_seconds, 1.0)
        self.assertAllGone(self.kids().values())
        self.assertEqual(self.marked(request.turn_id), [])

    def test_timeout_past_an_ignored_sigterm(self):
        session = self.session()
        result = list(self.client.turn(self.request(session, "hang-ignore-term", limit=1)))[-1]
        self.assertEqual(result.outcome, TurnOutcome.TIMEOUT)

    def test_a_finished_model_with_a_lingering_process_is_still_completed(self):
        session = self.session()
        request = self.request(session, "linger", limit=30)
        began = time.monotonic()
        result = list(self.client.turn(request))[-1]
        self.assertEqual(result.outcome, TurnOutcome.COMPLETED)
        self.assertLess(time.monotonic() - began, 15)
        self.assertEqual(self.marked(request.turn_id), [])

    def test_stop_after_the_result_doesnt_change_completed(self):
        session = self.session()
        turn_id, stream = self.start_turn_bg(session, "linger", limit=30)
        for item in stream:
            if isinstance(item, TurnEvent) and item.type == EventType.USAGE and item.data["tokens"] == 150:
                break
        self.client.stop(session)
        self.assertEqual(list(stream)[-1].outcome, TurnOutcome.COMPLETED)

    def test_a_killed_turn_is_interrupted_and_its_reports_kept(self):
        session = self.session()
        request = self.request(session, "report-then-hang", limit=30)
        stream = self.client.turn(request)
        for item in stream:
            if item.type == EventType.INBOX:
                break
        from lumbridge_factory.agentd.procs import pids_with_marker

        for pid in pids_with_marker(request.turn_id):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        result = list(stream)[-1]
        self.assertEqual(result.outcome, TurnOutcome.INTERRUPTED)
        self.assertEqual(result.entries[0].report.summary, "before the hang")


class RestartTests(AgentdCase):
    def test_sigterm_ends_turns_interrupted_with_no_orphans(self):
        session = self.session()
        request = self.request(session, "kids hang", limit=60)
        stream = self.client.turn(request)
        next(iter(stream))
        self.wait_for_file(self.home / "kids.json")
        kids = json.loads((self.home / "kids.json").read_text())
        self.stop_daemon(signal.SIGTERM)
        self.assertEqual(self.proc.returncode, 0)
        self.assertEqual([p for p in kids.values() if alive(p)], [])
        self.assertFalse(os.path.exists(self.socket_path))
        self.start_daemon()
        self.assertEqual(self.client.running(), [])
        self.assertEqual(self.client.collect(request.turn_id).outcome, TurnOutcome.INTERRUPTED)

    def test_sigkill_then_restart_reports_interrupted_and_kills_survivors(self):
        from lumbridge_factory.agentd.procs import pids_with_marker

        session = self.session()
        request = self.request(session, "kids hang", limit=600)
        stream = self.client.turn(request)
        native = next(iter(stream)).data["native_session_id"]
        self.wait_for_file(self.home / "kids.json")
        self.stop_daemon(signal.SIGKILL)
        self.assertTrue(pids_with_marker(request.turn_id), "the turn should outlive a SIGKILLed agentd")
        self.start_daemon()
        self.assertEqual(pids_with_marker(request.turn_id), [])
        self.assertEqual(self.client.running(), [])
        result = self.client.collect(request.turn_id)
        self.assertEqual(result.outcome, TurnOutcome.INTERRUPTED)
        self.assertEqual(result.native_session_id, native)
        self.assertIn("restarted", result.error)

    def test_turn_after_restart_replays_interrupted_and_never_respawns(self):
        session = self.session()
        request = self.request(session, "report-then-hang", limit=600)
        stream = self.client.turn(request)
        for item in stream:
            if item.type == EventType.INBOX:
                break
        self.stop_daemon(signal.SIGKILL)
        self.start_daemon()
        dumps = set(self.home.glob("dump-*"))
        items = list(self.client.turn(request))
        self.assertEqual(items[-1].outcome, TurnOutcome.INTERRUPTED)
        self.assertEqual(items[-1].entries[0].report.summary, "before the hang")
        self.assertTrue(any(isinstance(i, TurnEvent) and i.type == EventType.STARTED for i in items))
        self.assertEqual(set(self.home.glob("dump-*")), dumps)

    def test_finished_results_survive_a_restart(self):
        session = self.session()
        request = self.request(session, "ok go")
        result = list(self.client.turn(request))[-1]
        self.stop_daemon(signal.SIGKILL)
        self.start_daemon()
        self.assertEqual(self.client.collect(request.turn_id), result)
        self.assertEqual(self.session(), session)

    def test_resume_after_restart(self):
        session = self.session()
        _, first = self.run_turn(session, "ok code-word-otter")
        self.stop_daemon(signal.SIGKILL)
        self.start_daemon()
        events, second = self.run_turn(session, "recallme", native=first.native_session_id)
        self.assertEqual(second.outcome, TurnOutcome.COMPLETED)
        self.assertTrue(any("otter" in e.data.get("text", "") for e in events))

    def test_journal_with_torn_request_is_treated_as_never_seen(self):
        self.stop_daemon(signal.SIGKILL)
        torn = self.turn_dir("torn")
        torn.mkdir(parents=True)
        (torn / "request.json").write_text('{"turn_id": "to')
        self.start_daemon()
        self.assertIsNone(self.client.collect("torn"))
        self.assertFalse(torn.exists())


class EnvironmentTests(AgentdCase):
    def all_files_text(self, *roots) -> list[tuple[Path, bytes]]:
        out = []
        for root in roots:
            for path in Path(root).rglob("*"):
                if path.is_file() and not path.is_symlink():
                    out.append((path, path.read_bytes()))
        return out

    def test_child_env_is_cleared_to_an_allowlist(self):
        _, result = self.run_turn(self.session(), "ok go")
        dump = self.dump(result.native_session_id)
        self.assertEqual(
            set(dump["env_keys"]),
            {
                "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TERM", "SHELL", "TMPDIR", "GIT_TERMINAL_PROMPT",
                "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL",
                "LUMBRIDGE_FACTORY_TURN", "CLAUDE_CODE_OAUTH_TOKEN",
                "CLAUDE_CODE_SHELL_PREFIX", "CLAUDE_CODE_DISABLE_AUTO_MEMORY",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "DISABLE_AUTOUPDATER", "DISABLE_TELEMETRY", "DISABLE_ERROR_REPORTING",
                "BASH_MAX_TIMEOUT_MS",
            },
        )
        self.assertNotIn("SECRET_FROM_PARENT", dump["env_keys"])
        self.assertNotIn("PYTHONPATH", dump["env_keys"])
        self.assertEqual(dump["token_len"], len(TOKEN))

    def test_the_token_reaches_no_file_agentd_writes(self):
        session = self.session()
        self.run_turn(session, "ok go")
        needle = TOKEN.encode()
        secrets = self.root / "secrets"
        leaks = [
            str(path)
            for path, data in self.all_files_text(self.root / "state", self.root / "worktrees", self.home)
            if needle in data and not path.is_relative_to(secrets)
        ]
        self.assertEqual(leaks, [])

    def test_a_token_the_agent_prints_is_redacted_from_everything(self):
        session = self.session()
        events, result = self.run_turn(session, "leak")
        needle = TOKEN.encode()
        wire = json.dumps([e.data for e in events]) + json.dumps(result.entries[0].report.summary)
        self.assertNotIn(TOKEN, wire)
        self.assertIn("[redacted]", wire)
        leaks = [str(p) for p, data in self.all_files_text(self.turn_dir(result.turn_id)) if needle in data and p.name != "inbox.jsonl"]
        self.assertEqual(leaks, [])

    def test_a_rotated_token_is_picked_up_on_the_next_turn(self):
        session = self.session()
        _, first = self.run_turn(session, "ok go")
        self.token_file.write_text("CLAUDE_CODE_OAUTH_TOKEN=rotated\n")
        _, second = self.run_turn(session, "ok go")
        self.assertEqual(self.dump(second.native_session_id)["token_len"], len("rotated"))

    def test_a_missing_token_fails_cleanly_and_never_names_a_value(self):
        session = self.session()
        self.token_file.write_text("OTHER=x\n")
        _, result = self.run_turn(session, "ok go")
        self.assertEqual(result.outcome, TurnOutcome.FAILED)
        self.assertIn("not set", result.error)
        self.token_file.unlink()
        _, result = self.run_turn(session, "ok go")
        self.assertEqual(result.outcome, TurnOutcome.FAILED)
        self.assertIn("env file", result.error)
        self.assertEqual(list(self.home.glob("dump-*")), [])

    def test_state_files_are_private(self):
        session = self.session()
        _, result = self.run_turn(session, "ok go")
        for path in [*(self.root / "state" / "sessions" / session).iterdir(), self.turn_dir(result.turn_id) / "result.json"]:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode) & 0o077, 0, path)


class CodexDisabledTests(AgentdCase):
    def test_codex_refuses_at_start_and_at_turn(self):
        with self.assertRaises(ExecutorError) as caught:
            self.client.start(
                StartRequest(
                    ticket="t",
                    assignment="a",
                    role=Role.BUILDER,
                    worktree=WorktreeSpec(repo="acme/sandbox", name="cx", branch="factory/cx"),
                    prompt_parts=PromptParts(system="s"),
                    account="codex-test",
                    model="m",
                )
            )
        self.assertEqual(caught.exception.code, "unavailable")
        self.assertFalse(caught.exception.retryable)
        session = self.session()
        with self.assertRaises(ExecutorError) as caught:
            list(self.client.turn(self.request(session, "ok", account="codex-test")))
        self.assertEqual(caught.exception.code, "unavailable")
        self.assertEqual(self.client.running(), [])


class PinnedVersionTests(AgentdCase):
    extra_config = {}

    def test_wrong_pinned_version_makes_the_tool_unavailable(self):
        self.stop_daemon()
        self.config["tools"]["claude"]["bin"] = "/bin/true"
        self.config["tools"]["claude"]["expected_version"] = "2.1.310"
        self.start_daemon()
        self.assertEqual(self.client.hello()["tools"], [])
        with self.assertRaises(ExecutorError) as caught:
            self.session()
        self.assertEqual(caught.exception.code, "unavailable")
        self.assertFalse(caught.exception.retryable)


class PeerTests(AgentdCase):
    def test_a_peer_outside_the_allowlist_is_refused(self):
        self.stop_daemon()
        self.config["allowed_peer_uids"] = [os.geteuid() + 1]
        self.start_daemon_unprobed()
        deadline = time.monotonic() + 15
        last = None
        while time.monotonic() < deadline:
            try:
                self.client.hello()
                self.fail("should be refused")
            except ExecutorError as exc:
                last = exc
                if exc.code == "bad_request":
                    break
                time.sleep(0.02)
        self.assertEqual(last.code, "bad_request")
        self.assertIn("peer", last.message)

    def start_daemon_unprobed(self):
        (self.root / "agentd.json").write_text(json.dumps(self.config))
        env = {"PATH": os.environ["PATH"], "PYTHONPATH": str(REPO)}
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "lumbridge_factory.agentd", "--config", str(self.root / "agentd.json")],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=REPO,
        )


if __name__ == "__main__":
    unittest.main()


class RemoveSessionTests(AgentdCase):
    def test_a_builder_goes_with_its_worktree_branch_and_cli_state(self):
        session = self.session()
        wt = self.root / "worktrees" / "abc"
        (wt / "work.txt").write_text("x\n")
        slug = re.sub(r"[^A-Za-z0-9]", "-", str(wt))
        store = self.home / ".claude" / "projects" / slug
        store.mkdir(parents=True)
        (store / "s.jsonl").write_text("{}\n")
        self.assertIn(str(wt), json.loads((self.home / ".claude.json").read_text())["projects"])

        self.client.remove_session(session, True)

        self.assertFalse(wt.exists())
        self.assertFalse((self.root / "state" / "sessions" / session).exists())
        self.assertFalse(store.exists())
        self.assertNotIn(str(wt), json.loads((self.home / ".claude.json").read_text())["projects"])
        clone = self.root / "repos" / "acme__sandbox.git"
        self.assertEqual(git(clone, "branch", "--list", "factory/abc"), "")
        self.assertNotIn("abc", git(clone, "worktree", "list"))

    def test_a_read_only_reviewer_tree_goes_too(self):
        session = self.session("rev", role=Role.REVIEWER, branch="", pinned_sha=self.seed_sha, read_only=True)
        wt = self.root / "worktrees" / "rev"
        self.assertFalse(os.access(wt, os.W_OK))
        self.client.remove_session(session, True)
        self.assertFalse(wt.exists())

    def test_without_worktree_the_tree_stays_for_the_next_session(self):
        session = self.session()
        wt = self.root / "worktrees" / "abc"
        self.client.remove_session(session, False)
        self.assertTrue(wt.exists())
        self.assertFalse((self.root / "state" / "sessions" / session).exists())
        self.assertEqual(self.session("next", wt_name="abc", branch="factory/abc") != session, True)
        self.assertTrue(wt.exists())

    def test_it_is_idempotent_and_a_stranger_id_is_fine(self):
        session = self.session()
        self.client.remove_session(session, True)
        self.client.remove_session(session, True)
        self.client.remove_session("s-" + "0" * 20, True)

    def test_a_running_turn_blocks_it(self):
        session = self.session()
        turn_id, stream = self.start_turn_bg(session, "hang")
        next(stream)
        self.wait_for_file(self.turn_dir(turn_id) / "request.json")
        with self.assertRaises(ExecutorError) as caught:
            self.client.remove_session(session, True)
        self.assertEqual(caught.exception.code, "unavailable")
        self.assertTrue(caught.exception.retryable)
        self.assertTrue((self.root / "worktrees" / "abc").exists())
        self.client.stop(session)
        list(stream)
        self.client.remove_session(session, True)
        self.assertFalse((self.root / "worktrees" / "abc").exists())

    def test_a_resume_after_removal_starts_from_what_was_pushed(self):
        session = self.session()
        wt = self.root / "worktrees" / "abc"
        (wt / "pushed.txt").write_text("kept\n")
        git(wt, "add", ".")
        git(wt, "commit", "-qm", "work")
        git(wt, "push", "-q", "origin", "HEAD:factory/abc")
        (wt / "unpushed.txt").write_text("gone\n")
        self.client.remove_session(session, True)
        self.session("again", wt_name="abc", branch="factory/abc")
        self.assertTrue((wt / "pushed.txt").exists())
        self.assertFalse((wt / "unpushed.txt").exists())

    def test_a_path_outside_the_worktrees_directory_is_refused(self):
        session = self.session()
        record = self.root / "state" / "sessions" / session / "session.json"
        data = json.loads(record.read_text())
        outside = self.root / "precious"
        outside.mkdir()
        data["worktree"] = str(outside)
        record.write_text(json.dumps(data))
        with self.assertRaises(ExecutorError):
            self.client.remove_session(session, True)
        self.assertTrue(outside.exists())
