#!/usr/bin/env python3
"""A stand-in for `claude -p` that really commits and pushes, for the process-level test. It speaks the
real MCP config, so its report goes through mcp_tools.py and agentd's inbox. Not a test module.

A builder holds until the file named by LUMBRIDGE_FACTORY_TEST_GO exists, so a test can kill things mid-turn."""

import json
import os
import subprocess
import sys
import time
import uuid

GO = os.path.join(os.environ["HOME"], "go")
STARTED = os.path.join(os.environ["HOME"], "started")


def flag(name):
    for i, arg in enumerate(sys.argv):
        if arg == name:
            return sys.argv[i + 1]
        if arg.startswith(name + "="):
            return arg.split("=", 1)[1]
    return None


def emit(**obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def report(**arguments):
    config = json.load(open(flag("--mcp-config")))["mcpServers"]["factory-tools"]
    proc = subprocess.Popen([config["command"], *config["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    for i, (method, params) in enumerate((("initialize", {"protocolVersion": "2025-06-18"}), ("tools/call", {"name": "report", "arguments": arguments}))):
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params}) + "\n")
        proc.stdin.flush()
        proc.stdout.readline()
    proc.stdin.close()
    proc.wait()


prompt = sys.stdin.read()
native = flag("--resume") or str(uuid.uuid4())
emit(type="system", subtype="init", session_id=native)
role = open(flag("--append-system-prompt-file")).read()
if "independent reviewer" in role:
    sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    report(status="ready", summary=f"Reviewed {sha}", verdict="pass")
else:
    with open(STARTED, "a") as fh:
        fh.write(native + "\n")
    deadline = time.time() + 60
    while not os.path.exists(GO) and time.time() < deadline:
        time.sleep(0.05)
    with open("work.txt", "a") as fh:
        fh.write(prompt[:40].replace("\n", " ") + "\n")
    subprocess.run(["git", "add", "work.txt"], check=True)
    subprocess.run(["git", "commit", "-qm", "Work"], check=True)
    branch = subprocess.run(["git", "symbolic-ref", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "push", "-q", "origin", f"HEAD:{branch}"], check=True)
    report(status="ready", summary="Did the work.")
emit(type="result", subtype="success", is_error=False, session_id=native, usage={"input_tokens": 1, "output_tokens": 1}, result="done")
