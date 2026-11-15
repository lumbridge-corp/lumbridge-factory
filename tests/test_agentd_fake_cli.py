#!/usr/bin/env python3
"""A stand-in for `claude -p`. The prompt's first word picks what it does.

Not a test module despite the name (the name keeps it inside tests/test_agentd*); it
defines no TestCase. It speaks the real MCP config, so a report goes through the real
mcp_tools.py.
"""

import json
import os
import signal
import subprocess
import sys
import time
import uuid


def emit(**obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def flag(name):
    for i, arg in enumerate(sys.argv):
        if arg == name:
            return sys.argv[i + 1]
        if arg.startswith(name + "="):
            return arg.split("=", 1)[1]
    return None


class Mcp:
    def __init__(self):
        config = json.load(open(flag("--mcp-config")))["mcpServers"]["factory-tools"]
        self.proc = subprocess.Popen(
            [config["command"], *config["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
        )
        self.next_id = 0
        self.call("initialize", {"protocolVersion": "2025-06-18"})

    def call(self, method, params):
        self.next_id += 1
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params}) + "\n")
        self.proc.stdin.flush()
        return json.loads(self.proc.stdout.readline())

    def tool(self, name, **args):
        return self.call("tools/call", {"name": name, "arguments": args})["result"]


SESSION = ""


def text(words):
    emit(type="assistant", session_id=SESSION, message={"id": str(uuid.uuid4()), "content": [{"type": "text", "text": words}], "usage": {"input_tokens": 10, "output_tokens": 5}})


def tool_use(name):
    emit(type="assistant", session_id=SESSION, message={"id": str(uuid.uuid4()), "content": [{"type": "tool_use", "name": name}]})


def success(words="done"):
    emit(type="result", subtype="success", is_error=False, session_id=SESSION, result=words, usage={"input_tokens": 100, "cache_creation_input_tokens": 20, "cache_read_input_tokens": 9000, "output_tokens": 30})


def main():
    global SESSION
    prompt = sys.stdin.read()
    scenario = prompt.split()[0] if prompt.split() else "empty"
    resume = flag("--resume")
    SESSION = resume or str(uuid.uuid4())
    home = os.environ["HOME"]

    with open(os.path.join(home, f"dump-{SESSION}.json"), "w") as fh:
        json.dump(
            {
                "argv": sys.argv[1:],
                "cwd": os.getcwd(),
                "prompt": prompt,
                "env_keys": sorted(os.environ),
                "token_len": len(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")),
            },
            fh,
        )

    if scenario == "resume-missing":
        print(f"No conversation found with session ID: {resume}", file=sys.stderr)
        sys.exit(1)

    memory = os.path.join(home, f"memory-{SESSION}.txt")
    emit(type="system", subtype="init", session_id=SESSION)

    if scenario == "ok":
        text("working")
        tool_use("mcp__factory-tools__report")
        mcp = Mcp()
        mcp.tool("reply", purpose="ack", text="on it")
        mcp.tool("report", status="ready", summary="all done", pr=7)
        with open(memory, "w") as fh:
            fh.write(prompt)
        success()
    elif scenario == "recallme":
        print(prompt, file=sys.stderr)
        with open(memory) as fh:
            text("I remember: " + fh.read())
        Mcp().tool("report", status="progress", summary="remembered")
        success()
    elif scenario == "no-report":
        success()
    elif scenario == "two-reports":
        mcp = Mcp()
        mcp.tool("report", status="progress", summary="first")
        mcp.tool("report", status="ready", summary="second")
        success()
    elif scenario == "bad-report":
        result = Mcp().tool("report", status="nonsense", summary="x")
        text("error:" + str(result.get("isError")))
        success()
    elif scenario == "recall":
        result = Mcp().tool("recall", id="trigger-9")
        text("answer=" + result["content"][0]["text"])
        success()
    elif scenario == "hang":
        time.sleep(1000)
    elif scenario == "hang-ignore-term":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(1000)
    elif scenario == "kids":
        # One child stays in the group; one leaves it with setsid.
        a = subprocess.Popen(["sleep", "1000"])
        b = subprocess.Popen(["setsid", "sleep", "1001"])
        time.sleep(0.2)
        pids = {"group": a.pid, "setsid_parent": b.pid}
        with open(os.path.join(home, "kids.json"), "w") as fh:
            json.dump(pids, fh)
        text("kids started")
        if "exit" in prompt:
            success()
            sys.exit(0)
        time.sleep(1000)
    elif scenario == "linger":
        text("the model is done, the process isn't")
        success()
        time.sleep(1000)
    elif scenario == "report-then-hang":
        Mcp().tool("report", status="progress", summary="before the hang")
        time.sleep(1000)
    elif scenario == "crash":
        text("about to die")
        os.kill(os.getpid(), signal.SIGKILL)
    elif scenario == "exit3":
        print("boom: something broke", file=sys.stderr)
        sys.exit(3)
    elif scenario == "silent":
        sys.exit(0)
    elif scenario == "overloaded":
        emit(type="result", subtype="error_during_execution", is_error=True, session_id=SESSION, result="API Error: 529 overloaded", api_error_status=529)
        sys.exit(1)
    elif scenario == "limit":
        emit(type="result", subtype="success", is_error=True, session_id=SESSION, result="Claude usage limit reached. Your limit will reset at 7pm.")
        sys.exit(1)
    elif scenario == "leak":
        token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
        text("the token is " + token)
        print("stderr token " + token, file=sys.stderr)
        Mcp().tool("report", status="progress", summary="here it is: " + token)
        success()
    elif scenario == "write-token-file":
        # What a careless agent might do. agentd can't stop this; the test proves agentd itself doesn't.
        success()
    else:
        text("unknown scenario " + scenario)
        success()


if __name__ == "__main__":
    main()
