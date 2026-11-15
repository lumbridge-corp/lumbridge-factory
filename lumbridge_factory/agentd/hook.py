#!/usr/bin/env python3
"""PreToolUse policy hook for one factory ticket (ARCHITECTURE §6, §12).

Claude Code runs this before every tool call, with the call as JSON on stdin:

    python3 -I hook.py --policy <run-dir>/policy.json [--audit <run-dir>/hook-denials.jsonl]

Exit 0 allows the call. Exit 2 refuses it, and stderr goes to the model. Claude
Code treats any *other* exit status as "hook broke, carry on", so every failure
in here, parse errors included, must end in exit 2.

This file must stay self-contained and stdlib-only: it runs under `python3 -I`,
so it can't import anything from the package.

This is defense in depth. The git key and branch protection are what really stop
a push to `main`. See docs/AGENTD.md for what this hook can't stop.
"""

from __future__ import annotations

import fnmatch
import glob
import hashlib
import json
import os
import pwd
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


@dataclass
class Policy:
    worktree: str
    worktrees_root: str
    homes: list[str]
    state_dir: str = ""
    common_dir: str = ""
    push_branches: list[str] = field(default_factory=list)  # exact; wins over prefixes
    push_prefixes: list[str] = field(default_factory=lambda: ["factory/"])
    read_only: bool = False
    deny_roots: list[str] = field(default_factory=list)
    base: str = ""  # the branch this worktree came from; a script unchanged since it isn't scanned

    @classmethod
    def load(cls, path: str) -> "Policy":
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in raw.items() if k in known})

    @property
    def home(self) -> str:
        return self.homes[0]


class Deny(Exception):
    """Raised anywhere in the analysis to refuse the tool call."""


# Names directly under a home directory that a worker must never touch.
HOME_SECRET_GLOBS = (
    ".claude",
    ".claude.*",
    ".claude-*",
    ".codex",
    ".codex.*",
    ".codex-*",
    ".config",
    ".ssh",
    ".gnupg",
    ".netrc",
    ".git-credentials",
    ".gitconfig",
    ".aws",
    ".docker",
    ".kube",
    ".npmrc",
    ".pypirc",
    ".bash_history",
    ".zsh_history",
    ".local/share/keyrings",
)
ABS_SECRET_ROOTS = ("/run/secrets", "/etc/shadow", "/etc/gshadow", "/etc/infisical", "/root")
PROC_SECRET = re.compile(r"^/proc/[^/]+/(environ|mem|maps|task/[^/]+/(environ|mem))$")

# Files inside a worktree that change how the agent itself behaves. The CLI is
# started so it ignores them, but a worker has no business editing them either.
AGENT_CONTROL_FILES = (".mcp.json",)
AGENT_CONTROL_GLOBS = (".claude/settings*.json", ".claude/hooks/*", ".codex/*")

# ---------------------------------------------------------------------------
# Path checks
# ---------------------------------------------------------------------------


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _expand_tilde(text: str, policy: Policy) -> str:
    if text == "~" or text.startswith("~/"):
        return policy.home + text[1:]
    if text.startswith("~"):
        name, _, rest = text[1:].partition("/")
        try:
            return pwd.getpwnam(name).pw_dir + ("/" + rest if rest else "")
        except (KeyError, ValueError):
            return text
    return text


def _candidates(text: str, policy: Policy, cwd: str | None) -> list[str]:
    """Lexical and fully resolved forms of a path. Both get checked."""
    text = _expand_tilde(text, policy)
    if not text.startswith("/"):
        if cwd is None:
            return []
        text = os.path.join(cwd, text)
    out = [os.path.normpath(text)]
    try:
        real = os.path.realpath(text)
    except (OSError, ValueError):
        real = out[0]
    if real not in out:
        out.append(real)
    return out


def _secret_reason(path: str, policy: Policy) -> str | None:
    if PROC_SECRET.match(path):
        return f"{path} exposes a process's environment or memory"
    for root in ABS_SECRET_ROOTS:
        if _under(path, root):
            return f"{root} holds secrets"
    for root in policy.deny_roots:
        if _under(path, root):
            return f"{root} is off limits to workers"
    if policy.state_dir and _under(path, policy.state_dir):
        return "agentd's state directory is off limits to workers"
    for home in policy.homes:
        if _under(path, home) and path != home:
            rel = path[len(home.rstrip("/")) + 1 :]
            first = rel.split("/", 1)[0]
            for pattern in HOME_SECRET_GLOBS:
                if "/" in pattern:
                    if rel == pattern or rel.startswith(pattern + "/"):
                        return f"~/{pattern} holds credentials or agent state"
                elif fnmatch.fnmatchcase(first, pattern):
                    return f"~/{first} holds credentials or agent state"
    return None


def _path_reason(text: str, policy: Policy, cwd: str | None) -> str | None:
    """Why `text`, read as a path, is off limits. None if it's fine."""
    for path in _candidates(text, policy, cwd):
        if _under(path, policy.worktree):
            continue
        reason = _secret_reason(path, policy)
        if reason:
            return reason
        if _under(path, policy.worktrees_root) and path != policy.worktrees_root:
            return "other worktrees are off limits"
    # A symlink inside the worktree that points somewhere forbidden.
    candidates = _candidates(text, policy, cwd)
    if len(candidates) == 2 and _under(candidates[0], policy.worktree):
        real = candidates[1]
        if not _under(real, policy.worktree):
            reason = _secret_reason(real, policy)
            if reason:
                return reason
    return None


def _is_ancestor_of_forbidden(path: str, policy: Policy) -> bool:
    """A recursive walk from here would reach forbidden files."""
    if _under(policy.worktree, path) and path != policy.worktree:
        return True
    roots = [*ABS_SECRET_ROOTS, *policy.deny_roots, *policy.homes, policy.worktrees_root]
    if policy.state_dir:
        roots.append(policy.state_dir)
    return any(_under(root, path) for root in roots)


def _control_file_reason(path: str, policy: Policy) -> str | None:
    if policy.common_dir:
        config = os.path.join(policy.common_dir, "config")
        if path == config or _under(path, os.path.join(policy.common_dir, "hooks")):
            return "git's config and hooks are off limits"
    if not _under(path, policy.worktree) or path == policy.worktree:
        return None
    rel = path[len(policy.worktree.rstrip("/")) + 1 :]
    if rel == ".git" or rel.startswith(".git/"):
        return ".git is off limits to file edits"
    if rel in AGENT_CONTROL_FILES or any(fnmatch.fnmatchcase(rel, g) for g in AGENT_CONTROL_GLOBS):
        return f"{rel} configures the agent itself"
    return None


# ---------------------------------------------------------------------------
# A small shell parser. It only has to find commands and words reliably, and
# refuse (rather than guess) when it can't.
# ---------------------------------------------------------------------------


class ParseError(Exception):
    pass


@dataclass
class Word:
    parts: list[tuple[str, str]] = field(default_factory=list)  # ("lit"|"var"|"sub", value)
    has_glob: bool = False  # an unquoted * ? or [
    quoted: bool = False

    def add(self, kind: str, value: str) -> None:
        if kind == "lit" and self.parts and self.parts[-1][0] == "lit":
            self.parts[-1] = ("lit", self.parts[-1][1] + value)
        else:
            self.parts.append((kind, value))

    def __bool__(self) -> bool:
        return bool(self.parts) or self.quoted


@dataclass
class Command:
    words: list[Word] = field(default_factory=list)
    redirects: list[tuple[str, Word]] = field(default_factory=list)
    subs: list[list] = field(default_factory=list)  # nested parsed command lists
    heredocs: list[str] = field(default_factory=list)
    piped: bool = False


GROUP_OPEN = "("
GROUP_CLOSE = ")"
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class Parser:
    def __init__(self, src: str) -> None:
        self.src = src
        self.i = 0

    def parse(self, until: str | None = None) -> list:
        nodes: list = []
        cmd = Command()
        word = Word()
        pending_redirect: str | None = None
        pending_heredocs: list[tuple[str, bool, Command]] = []
        depth = 0
        src = self.src

        def end_word() -> None:
            nonlocal word, pending_redirect
            if word:
                if pending_redirect is not None:
                    if pending_redirect.startswith("<<") and pending_redirect != "<<<":
                        delim = "".join(v for k, v in word.parts if k == "lit")
                        pending_heredocs.append((delim, pending_redirect == "<<-", cmd))
                    else:
                        cmd.redirects.append((pending_redirect, word))
                    pending_redirect = None
                else:
                    cmd.words.append(word)
            word = Word()

        def end_command(piped_next: bool = False) -> None:
            nonlocal cmd
            end_word()
            if cmd.words or cmd.redirects or cmd.subs or cmd.heredocs:
                nodes.append(cmd)
            elif pending_heredocs:
                nodes.append(cmd)
            cmd = Command()
            cmd.piped = piped_next

        while self.i < len(src):
            c = src[self.i]
            if c == "\\":
                if self.i + 1 < len(src) and src[self.i + 1] == "\n":
                    self.i += 2
                    continue
                if self.i + 1 >= len(src):
                    raise ParseError("trailing backslash")
                word.add("lit", src[self.i + 1])
                word.quoted = True
                self.i += 2
            elif c == "'":
                end = src.find("'", self.i + 1)
                if end < 0:
                    raise ParseError("unterminated single quote")
                word.add("lit", src[self.i + 1 : end])
                word.quoted = True
                self.i = end + 1
            elif c == '"':
                self.i += 1
                self._double_quoted(word, cmd)
            elif c == "$":
                self._dollar(word, cmd, in_quotes=False)
            elif c == "`":
                self._backtick(word, cmd)
            elif c in " \t":
                end_word()
                self.i += 1
            elif c == "\n":
                end_command()
                self.i += 1
                if pending_heredocs:
                    self._read_heredocs(pending_heredocs)
                    pending_heredocs = []
            elif c == "#" and not word:
                nl = src.find("\n", self.i)
                self.i = len(src) if nl < 0 else nl
            elif c == ";":
                end_command()
                self.i += 1
            elif c == "|":
                end_command(piped_next=True)
                self.i += 1
                if self.i < len(src) and src[self.i] in "|&":
                    if src[self.i] == "|":
                        cmd.piped = False
                    self.i += 1
            elif c == "&":
                if src[self.i : self.i + 2] == "&>":
                    end_word()
                    self.i += 2
                    if self.i < len(src) and src[self.i] == ">":
                        self.i += 1
                    pending_redirect = ">"
                    continue
                end_command()
                self.i += 1
                if self.i < len(src) and src[self.i] == "&":
                    self.i += 1
            elif c == "(":
                end_command()
                nodes.append(GROUP_OPEN)
                depth += 1
                self.i += 1
            elif c == ")":
                if until == ")" and depth == 0:
                    end_command()
                    self.i += 1
                    if pending_heredocs:
                        raise ParseError("unterminated heredoc")
                    return nodes
                end_command()
                if depth > 0:
                    nodes.append(GROUP_CLOSE)
                    depth -= 1
                self.i += 1
            elif c in "<>":
                if word and all(k == "lit" and v.isdigit() for k, v in word.parts):
                    word = Word()
                else:
                    end_word()
                start = self.i
                while self.i < len(src) and src[self.i] in "<>":
                    self.i += 1
                op = src[start : self.i]
                if self.i < len(src) and src[self.i] == "(":
                    self.i += 1
                    cmd.subs.append(Parser._nested(self))
                    continue
                if op == "<<" and self.i < len(src) and src[self.i] == "-":
                    op = "<<-"
                    self.i += 1
                if self.i < len(src) and src[self.i] == "&":
                    self.i += 1
                pending_redirect = op
                while self.i < len(src) and src[self.i] in " \t":
                    self.i += 1
            else:
                if c in "*?[":
                    word.has_glob = True
                word.add("lit", c)
                self.i += 1

        if until == ")":
            raise ParseError("unterminated command substitution")
        end_command()
        if pending_heredocs:
            self._read_heredocs(pending_heredocs)
        return nodes

    def _nested(self) -> list:
        return Parser.parse(self, until=")")

    def _read_heredocs(self, pending: list[tuple[str, bool, Command]]) -> None:
        for delim, strip_tabs, owner in pending:
            lines = []
            while True:
                if self.i >= len(self.src):
                    # An unterminated heredoc still ends at EOF for the shell.
                    break
                nl = self.src.find("\n", self.i)
                if nl < 0:
                    line, self.i = self.src[self.i :], len(self.src)
                else:
                    line, self.i = self.src[self.i : nl], nl + 1
                check = line.lstrip("\t") if strip_tabs else line
                if check == delim:
                    break
                lines.append(line)
            owner.heredocs.append("\n".join(lines))

    def _double_quoted(self, word: Word, cmd: Command) -> None:
        src = self.src
        word.quoted = True
        while True:
            if self.i >= len(src):
                raise ParseError("unterminated double quote")
            c = src[self.i]
            if c == '"':
                self.i += 1
                return
            if c == "\\" and self.i + 1 < len(src):
                nxt = src[self.i + 1]
                if nxt == "\n":
                    pass
                elif nxt in '$`"\\':
                    word.add("lit", nxt)
                else:
                    word.add("lit", "\\" + nxt)
                self.i += 2
            elif c == "$":
                self._dollar(word, cmd, in_quotes=True)
            elif c == "`":
                self._backtick(word, cmd)
            else:
                word.add("lit", c)
                self.i += 1

    def _backtick(self, word: Word, cmd: Command) -> None:
        src = self.src
        start = self.i + 1
        j = start
        buf = []
        while True:
            if j >= len(src):
                raise ParseError("unterminated backtick")
            if src[j] == "\\" and j + 1 < len(src):
                buf.append(src[j + 1] if src[j + 1] in "`$\\" else src[j : j + 2])
                j += 2
                continue
            if src[j] == "`":
                break
            buf.append(src[j])
            j += 1
        cmd.subs.append(Parser("".join(buf)).parse())
        word.add("sub", src[start:j])
        self.i = j + 1

    def _dollar(self, word: Word, cmd: Command, in_quotes: bool) -> None:
        src = self.src
        nxt = src[self.i + 1] if self.i + 1 < len(src) else ""
        if nxt == "(":
            if src[self.i + 2 : self.i + 3] == "(":
                end = src.find("))", self.i + 3)
                if end >= 0:
                    word.add("sub", src[self.i : end + 2])
                    self.i = end + 2
                    return
            self.i += 2
            start = self.i
            cmd.subs.append(self._nested())
            word.add("sub", src[start : self.i - 1])
        elif nxt == "{":
            end = src.find("}", self.i + 2)
            if end < 0:
                raise ParseError("unterminated ${")
            inner = src[self.i + 2 : end]
            if _NAME.fullmatch(inner):
                word.add("var", inner)
            else:
                word.add("sub", "${" + inner + "}")
            self.i = end + 1
        elif nxt == "'" and not in_quotes:
            self.i += 2
            word.add("lit", self._ansi_c())
            word.quoted = True
        elif nxt == '"' and not in_quotes:
            self.i += 1
        else:
            m = _NAME.match(src, self.i + 1)
            if m:
                word.add("var", m.group())
                self.i = m.end()
            elif nxt and (nxt.isdigit() or nxt in "@*#?$!-"):
                word.add("sub", "$" + nxt)
                self.i += 2
            else:
                word.add("lit", "$")
                self.i += 1

    def _ansi_c(self) -> str:
        src = self.src
        simple = {"n": "\n", "t": "\t", "r": "\r", "a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "v": "\v"}
        out = []
        while True:
            if self.i >= len(src):
                raise ParseError("unterminated $'")
            c = src[self.i]
            if c == "'":
                self.i += 1
                return "".join(out)
            if c != "\\":
                out.append(c)
                self.i += 1
                continue
            e = src[self.i + 1] if self.i + 1 < len(src) else ""
            self.i += 2
            if e in simple:
                out.append(simple[e])
            elif e == "x" or e == "u" or e == "U":
                size = {"x": 2, "u": 4, "U": 8}[e]
                m = re.compile(r"[0-9a-fA-F]{1,%d}" % size).match(src, self.i)
                if not m:
                    raise ParseError("bad escape in $'")
                out.append(chr(int(m.group(), 16)))
                self.i = m.end()
            elif e in "01234567":
                m = re.compile(r"[0-7]{0,2}").match(src, self.i)
                out.append(chr(int(e + m.group(), 8) & 0xFF))
                self.i = m.end()
            else:
                out.append(e)


# ---------------------------------------------------------------------------
# Command analysis
# ---------------------------------------------------------------------------

KEYWORDS = {"if", "then", "else", "elif", "fi", "while", "until", "do", "done", "!", "{", "}", "[[", "]]", "esac", "time"}
SHELLS = {"sh", "bash", "dash", "zsh", "ksh", "ash", "fish", "csh", "tcsh"}
INTERPRETER = re.compile(r"^(python[0-9.]*|pypy[0-9.]*|node|nodejs|deno|bun|perl[0-9.]*|ruby[0-9.]*|php[0-9.]*|lua[0-9.]*|tclsh|osascript|Rscript)$")
INLINE_CODE_FLAGS = {"-c", "-e", "-E", "-r", "--eval", "--print", "-p"}
FORBIDDEN_COMMANDS = {"sudo", "doas", "su", "pkexec", "nsenter", "unshare", "chroot", "ssh", "scp", "sftp", "gdb", "strace", "ltrace"}
ENV_DUMP = {"env", "printenv"}
INERT = {"cat", "echo", "printf", "true", "false", ":", "grep", "egrep", "fgrep", "rg", "ag", "ack", "wc", "sort", "uniq", "head", "tail", "ls", "test", "["}
SEARCHERS = {"grep", "egrep", "fgrep", "rg", "ag", "ack"}
RECURSIVE = {"find", "fd", "fdfind", "rg", "ag", "ack", "tar", "zip", "rsync", "du", "tree"}
RECURSIVE_WITH_FLAG = {"grep", "egrep", "fgrep", "cp", "ls", "chmod", "chown", "rm", "diff", "scp"}
WRITERS = {"cp", "mv", "ln", "rm", "tee", "sed", "install", "touch", "truncate", "dd", "chmod", "chown", "rsync", "patch"}
DANGEROUS_ASSIGN = re.compile(r"^(GIT_(DIR|WORK_TREE|COMMON_DIR|SSH|SSH_COMMAND|SSH_VARIANT|ASKPASS|CONFIG|CONFIG_COUNT|CONFIG_GLOBAL|CONFIG_SYSTEM|CONFIG_PARAMETERS|CONFIG_KEY_\d+|CONFIG_VALUE_\d+|EXEC_PATH|PROXY_COMMAND|ALTERNATE_OBJECT_DIRECTORIES|OBJECT_DIRECTORY|NAMESPACE)|SSH_AUTH_SOCK|LD_PRELOAD|LD_LIBRARY_PATH|CLAUDE_CONFIG_DIR|CODEX_HOME|HOME)$")
DANGEROUS_GIT_CONFIG = re.compile(r"^(alias\.|core\.(sshcommand|hookspath|fsmonitor|pager|editor|gitproxy|askpass|attributesfile|excludesfile)|remote\.|url\.|push\.|credential\.|branch\.[^.]+\.(remote|pushremote|merge)|sequence\.editor|diff\.external|pager\.|include\.|includeif\.|filter\.|gpg\.)", re.I)

TEXT_GIT_PUSH = re.compile(r"(?<![\w.-])git(?![\w-])[^\n]{0,120}?(?<![\w.-])(?<!stash\s)push(?![\w-])|git-(?:receive|send)-pack|\bsend-pack\b|\bhttp-push\b")
TEXT_FORGE_MERGE = re.compile(r"\b(?:gh|tea)\b[^\n]{0,60}\bmerge\b|/pulls/[^/\s'\"]+/merge\b")
TEXT_TAG_REF = re.compile(r"refs/tags/")
TEXT_SECRET = re.compile(
    r"/run/secrets|/proc/[^\s'\"]*/(?:environ|mem)\b"
    r"|(?:^|[\s'\"=:(,~/$}\[])\.(?:claude|codex|ssh|gnupg|netrc|git-credentials|aws)(?:$|[\s'\"/.,)\]-])"
    r"|(?:^|[\s'\"=:(,~/$}\[])\.config(?:$|[\s'\"/,)\]])"
    r"|CLAUDE_CODE_OAUTH_TOKEN|ANTHROPIC_API_KEY|ANTHROPIC_AUTH_TOKEN|OPENAI_API_KEY|FACTORY_CLAUDE_TOKEN|agentd\.sock"
)
TEXT_ENV_READ = re.compile(r"\bos\.environ\b|\bos\.getenv\b|\bprocess\.env\b|\bENV\[|%ENV\b|\$ENV\{|\bgetenv\(")
GIT_KNOWN = set(
    """add am annotate apply archive bisect blame branch bundle cat-file check-attr check-ignore check-ref-format checkout
    cherry cherry-pick citool clean clone commit commit-tree config count-objects describe diff diff-files diff-index
    diff-tree fetch for-each-ref format-patch fsck gc grep hash-object help init log ls-files ls-remote ls-tree maintenance
    merge merge-base merge-file merge-tree mv name-rev notes pull push range-diff read-tree rebase reflog remote repack
    replace rerere reset restore rev-list rev-parse revert rm shortlog show show-branch show-ref sparse-checkout stash
    status stripspace submodule switch symbolic-ref tag unpack-file update-index update-ref var verify-commit verify-tag
    version whatchanged worktree write-tree blame lfs subtree""".split()
)
GIT_EMBEDS_COMMANDS = {"rebase", "bisect", "submodule", "lfs", "subtree", "filter-branch", "filter-repo", "difftool", "mergetool"}
PUSH_FLAGS_OK = {"--set-upstream", "--no-verify", "--verify", "--porcelain", "--quiet", "--verbose", "--dry-run", "--progress", "--no-progress"}
PUSH_SHORT_OK = set("uqvn")


@dataclass
class Ctx:
    policy: Policy
    cwd: str | None
    env: dict[str, str | None] = field(default_factory=dict)
    branch_changed: bool = False
    depth: int = 0

    def copy(self) -> "Ctx":
        return Ctx(self.policy, self.cwd, dict(self.env), self.branch_changed, self.depth)


@dataclass
class W:
    text: str
    unknown: bool
    has_glob: bool


def _expand_word(word: Word, ctx: Ctx) -> W:
    out = []
    unknown = False
    for kind, value in word.parts:
        if kind == "lit":
            out.append(value)
        elif kind == "var":
            if value in ctx.env and ctx.env[value] is not None:
                out.append(ctx.env[value])
            elif value == "HOME":
                out.append(ctx.policy.home)
            elif value == "PWD" and ctx.cwd:
                out.append(ctx.cwd)
            else:
                out.append("$" + value)
                unknown = True
        else:
            out.append("$(" + value + ")")
            unknown = True
    return W("".join(out), unknown, word.has_glob)


def check_source(source: str, ctx: Ctx) -> None:
    if ctx.depth > 8:
        raise Deny("command nesting is too deep to check")
    try:
        nodes = Parser(source).parse()
    except ParseError as exc:
        raise Deny(f"can't parse this command safely ({exc}); simplify it") from exc
    inner = ctx.copy()
    inner.depth += 1
    _check_nodes(nodes, inner)
    # A nested shell's cd doesn't leak; a plain `eval` is close enough to treat alike.


def _check_nodes(nodes: list, ctx: Ctx) -> None:
    stack: list[tuple[str | None, dict]] = []
    for node in nodes:
        if node == GROUP_OPEN:
            stack.append((ctx.cwd, dict(ctx.env)))
        elif node == GROUP_CLOSE:
            if stack:
                ctx.cwd, ctx.env = stack.pop()
        else:
            _check_command(node, ctx)


def _check_command(cmd: Command, ctx: Ctx) -> None:
    for sub in cmd.subs:
        inner = ctx.copy()
        inner.depth += 1
        if inner.depth > 8:
            raise Deny("command nesting is too deep to check")
        _check_nodes(sub, inner)

    words = [_expand_word(w, ctx) for w in cmd.words]
    redirects = [(op, _expand_word(w, ctx)) for op, w in cmd.redirects]

    for op, target in redirects:
        _check_redirect(op, target, ctx)

    # Leading assignments.
    idx = 0
    assigns: list[tuple[str, str | None]] = []
    while idx < len(words):
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", words[idx].text, re.S)
        if not m:
            break
        assigns.append((m.group(1), None if words[idx].unknown else m.group(2)))
        idx += 1
    for name, value in assigns:
        _check_assignment(name, value, ctx)
    rest = words[idx:]
    if not rest:
        for name, value in assigns:
            ctx.env[name] = value
        _check_heredocs_as_data(cmd, ctx)
        return

    _analyze(rest, cmd, ctx)


def _check_assignment(name: str, value: str | None, ctx: Ctx) -> None:
    if DANGEROUS_ASSIGN.match(name):
        raise Deny(f"setting {name} is not allowed")
    if value is not None:
        _text_checks(value, ctx, name="assign")


def _check_redirect(op: str, target: W, ctx: Ctx) -> None:
    if target.text.isdigit() or target.text == "-":
        return
    _check_path_word(target, ctx, recursive=False)
    if ">" in op:
        if ctx.policy.read_only:
            pass  # scratch output during a review is tolerated; the push is what's refused
        for path in _candidates(target.text, ctx.policy, ctx.cwd):
            reason = _control_file_reason(path, ctx.policy)
            if reason:
                raise Deny(reason)


def _check_heredocs_as_data(cmd: Command, ctx: Ctx) -> None:
    for body in cmd.heredocs:
        _text_checks(body, ctx, name="heredoc")


def _text_checks(text: str, ctx: Ctx, name: str, inline_code: bool = False) -> None:
    if not text:
        return
    collapsed = text.replace("\\\n", "")
    if TEXT_GIT_PUSH.search(_soften(collapsed)):
        raise Deny("this text runs or writes `git push`; push directly with `git push origin HEAD:<your branch>`")
    if TEXT_FORGE_MERGE.search(collapsed):
        raise Deny("workers never merge; report `ready` and a person merges")
    if TEXT_TAG_REF.search(collapsed):
        raise Deny("workers never touch tags")
    if TEXT_SECRET.search(collapsed):
        raise Deny("this mentions a secret location or credential variable")
    if inline_code and TEXT_ENV_READ.search(collapsed):
        raise Deny("inline code may not read the process environment")


def _soften(text: str) -> str:
    # 'git', 'push' and ["git","push"] read as `git push`.
    return re.sub(r"['\",\[\]()]+", " ", text)


def _glob_aims_at_secret(pattern: str, policy: Policy, cwd: str | None) -> bool:
    """Compare by components, because glob can't list a directory it can't read."""
    pattern = _expand_tilde(pattern, policy)
    if not pattern.startswith("/"):
        if cwd is None:
            return False
        pattern = os.path.join(cwd, pattern)
    parts = os.path.normpath(pattern).split("/")
    roots = list(ABS_SECRET_ROOTS) + list(policy.deny_roots)
    for home in policy.homes:
        roots += [os.path.join(home, g.rstrip("*.-")) for g in HOME_SECRET_GLOBS if "/" not in g]
    for root in roots:
        root_parts = root.split("/")
        if len(parts) >= len(root_parts) and all(
            fnmatch.fnmatchcase(r, p) for r, p in zip(root_parts, parts)
        ):
            return True
    return False


def _check_path_word(w: W, ctx: Ctx, recursive: bool) -> None:
    policy = ctx.policy
    text = w.text
    if text.startswith("-") and "=" in text:
        text = text.split("=", 1)[1]
    elif text.startswith("-"):
        return
    if not text:
        return
    if w.unknown:
        if TEXT_SECRET.search(text):
            raise Deny("this mentions a secret location or credential variable")
        literal = re.sub(r"\$\([^)]*\)|\$\w+", "", text)
        if ".." in literal.split("/"):
            if ctx.cwd is None or text.startswith("$"):
                raise Deny("a path with `..` and an unknown variable can't be checked")
        return
    if ctx.cwd is None and not text.startswith(("/", "~")) and ".." in text.split("/"):
        raise Deny("a relative `..` path after a `cd` to an unknown place can't be checked")
    forms = [text]
    if w.has_glob:
        if _glob_aims_at_secret(text, policy, ctx.cwd):
            raise Deny("this glob could match a secret location")
        base = _expand_tilde(text, policy)
        if not base.startswith("/") and ctx.cwd:
            base = os.path.join(ctx.cwd, base)
        try:
            forms += glob.glob(base, include_hidden=True)[:2000]
        except (OSError, re.error, ValueError):
            pass
        # A glob that matched nothing here may still be aimed somewhere forbidden.
        prefix = re.split(r"[*?\[]", text, maxsplit=1)[0]
        forms.append(prefix)
    for form in forms:
        if not form:
            continue
        reason = _path_reason(form, policy, ctx.cwd)
        if reason:
            raise Deny(reason)
        if recursive:
            for path in _candidates(form, policy, ctx.cwd):
                if _is_ancestor_of_forbidden(path, policy):
                    raise Deny(f"a recursive command from {path} would reach forbidden files; stay inside your worktree")


def _inside_worktree(w: W, ctx: Ctx) -> bool:
    # `if=/run/secrets/x` and `file:///x` read as relative paths, so they don't count.
    if w.unknown or re.search(r"[=:\s]", w.text):
        return False
    forms = _candidates(w.text, ctx.policy, ctx.cwd)
    return bool(forms) and all(_under(f, ctx.policy.worktree) for f in forms)


def _analyze(words: list[W], cmd: Command, ctx: Ctx) -> None:
    while words and words[0].text in KEYWORDS and not words[0].unknown:
        words = words[1:]
    if not words:
        _check_heredocs_as_data(cmd, ctx)
        return
    head = words[0]

    if head.unknown:
        # `$g push`, `$(which git) push`: we can't know the command.
        for w in words[1:]:
            if w.text in {"push", "merge", "tag", "send-pack"}:
                raise Deny("a computed command name running a git action can't be checked")
        _generic(words, cmd, ctx, name="")
        return
    if head.has_glob and head.text != "[":
        raise Deny("a glob as the command name can't be checked")

    name = os.path.basename(head.text)

    if name in FORBIDDEN_COMMANDS:
        raise Deny(f"`{name}` is not allowed in the factory")
    if name.startswith("git-"):
        raise Deny(f"call git directly, not `{name}`")

    if name in ("command", "builtin", "exec", "nohup", "setsid", "stdbuf", "unbuffer", "chronic", "nice", "ionice", "timeout", "busybox", "toybox", "flock", "time"):
        if name == "command" and len(words) > 1 and words[1].text in ("-v", "-V"):
            return
        return _analyze(_strip_wrapper(name, words), cmd, ctx)
    if name == "env":
        return _analyze_env(words, cmd, ctx)
    if name == "xargs":
        inner = _strip_xargs(words)
        if not inner:
            return
        joined = " ".join(w.text for w in inner)
        if os.path.basename(inner[0].text) == "git" and "push" in joined:
            raise Deny("`xargs git push` can't be checked: the refspec comes from stdin")
        return _analyze(inner, cmd, ctx)
    if name == "find":
        _check_find_exec(words, cmd, ctx)
        return _generic(words, cmd, ctx, name)
    if name in ("watch", "parallel", "script"):
        _text_checks(" ".join(w.text for w in words[1:]), ctx, name)
        return _generic(words, cmd, ctx, name)
    if name in ("export", "declare", "typeset", "readonly", "local"):
        return _analyze_declare(name, words, ctx)
    if name == "set" and len(words) == 1:
        raise Deny("dumping the environment is not allowed")
    if name == "compgen":
        raise Deny("dumping the environment is not allowed")
    if name == "printenv":
        raise Deny("dumping the environment is not allowed")
    if name == "eval":
        if any(w.unknown for w in words[1:]):
            raise Deny("`eval` of a computed string can't be checked")
        return check_source(" ".join(w.text for w in words[1:]), ctx)
    if name in ("source", "."):
        for w in words[1:2]:
            _scan_script(w, ctx)
        return _generic(words, cmd, ctx, name)
    if name in SHELLS:
        return _analyze_shell(words, cmd, ctx)
    if INTERPRETER.match(name):
        return _analyze_interpreter(name, words, cmd, ctx)
    if name == "cd" or name == "pushd" or name == "popd":
        return _analyze_cd(name, words, ctx)
    if name == "git":
        return _analyze_git(words, cmd, ctx)
    if name in ("gh", "tea"):
        return _analyze_forge(name, words, cmd, ctx)
    if "/" in head.text:
        _check_path_word(head, ctx, recursive=False)
        _scan_script(head, ctx)
    _generic(words, cmd, ctx, name)


def _strip_wrapper(name: str, words: list[W]) -> list[W]:
    rest = words[1:]
    if name in ("nice",):
        while rest and rest[0].text.startswith("-"):
            flag = rest.pop(0).text
            if flag == "-n" and rest:
                rest.pop(0)
    elif name == "ionice":
        while rest and rest[0].text.startswith("-"):
            flag = rest.pop(0).text
            if flag in ("-c", "-n", "-p") and rest:
                rest.pop(0)
    elif name == "timeout":
        while rest and rest[0].text.startswith("-"):
            flag = rest.pop(0).text
            if flag in ("-k", "-s", "--kill-after", "--signal") and rest:
                rest.pop(0)
        if rest:
            rest.pop(0)
    elif name == "flock":
        while rest and rest[0].text.startswith("-"):
            flag = rest.pop(0).text
            if flag in ("-w", "--timeout", "-E") and rest:
                rest.pop(0)
            if flag in ("-c", "--command"):
                return [W("sh", False, False), W("-c", False, False), *rest]
        if rest:
            rest.pop(0)
    elif name == "exec":
        while rest and rest[0].text.startswith("-"):
            flag = rest.pop(0).text
            if flag == "-a" and rest:
                rest.pop(0)
    else:
        while rest and rest[0].text.startswith("-"):
            rest.pop(0)
    return rest


def _analyze_env(words: list[W], cmd: Command, ctx: Ctx) -> None:
    rest = words[1:]
    while rest:
        t = rest[0].text
        if t in ("-i", "--ignore-environment", "-", "-0", "--null", "-v", "--debug"):
            rest.pop(0)
        elif t in ("-u", "--unset", "-C", "--chdir"):
            rest.pop(0)
            if rest:
                if t in ("-C", "--chdir"):
                    _check_path_word(rest[0], ctx, recursive=False)
                rest.pop(0)
        elif t.startswith("--unset=") or t.startswith("--chdir="):
            rest.pop(0)
        elif t in ("-S", "--split-string") or t.startswith("--split-string=") or (t.startswith("-S") and len(t) > 2):
            raise Deny("`env -S` can't be checked; run the command directly")
        elif t.startswith("-"):
            rest.pop(0)
        else:
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", t, re.S)
            if not m:
                break
            _check_assignment(m.group(1), None if rest[0].unknown else m.group(2), ctx)
            rest.pop(0)
    if not rest:
        raise Deny("dumping the environment is not allowed")
    _analyze(rest, cmd, ctx)


def _strip_xargs(words: list[W]) -> list[W]:
    rest = words[1:]
    takes_value = {"-I", "-n", "-P", "-L", "-d", "-a", "-s", "-E", "--max-args", "--max-procs", "--replace", "--delimiter", "--arg-file", "--max-chars", "--max-lines"}
    while rest and rest[0].text.startswith("-"):
        flag = rest.pop(0).text
        if flag in takes_value and rest:
            rest.pop(0)
    return rest


def _check_find_exec(words: list[W], cmd: Command, ctx: Ctx) -> None:
    i = 1
    while i < len(words):
        if words[i].text in ("-exec", "-execdir", "-ok", "-okdir"):
            j = i + 1
            while j < len(words) and words[j].text not in (";", "+"):
                j += 1
            inner = words[i + 1 : j]
            if inner:
                _analyze(inner, Command(), ctx.copy())
            i = j
        i += 1


def _analyze_declare(name: str, words: list[W], ctx: Ctx) -> None:
    args = words[1:]
    if not args or any(a.text in ("-p", "-x", "-px", "-xp") and len(args) == 1 for a in args):
        raise Deny("dumping the environment is not allowed")
    for a in args:
        if a.text.startswith("-"):
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)(?:=(.*))?$", a.text, re.S)
        if not m:
            continue
        value = None if a.unknown else m.group(2)
        if m.group(2) is None:
            continue
        _check_assignment(m.group(1), value, ctx)
        ctx.env[m.group(1)] = value


def _analyze_cd(name: str, words: list[W], ctx: Ctx) -> None:
    args = [w for w in words[1:] if not w.text.startswith("-") or w.text == "-"]
    if name == "popd" or not args:
        ctx.cwd = ctx.policy.home if (name == "cd" and not args) else None
        return
    target = args[0]
    if target.text == "-" or target.unknown:
        ctx.cwd = None
        if target.unknown:
            _check_path_word(target, ctx, recursive=False)
        return
    _check_path_word(target, ctx, recursive=False)
    forms = _candidates(target.text, ctx.policy, ctx.cwd)
    ctx.cwd = forms[-1] if forms else None


def _analyze_shell(words: list[W], cmd: Command, ctx: Ctx) -> None:
    args = words[1:]
    i = 0
    code: W | None = None
    script: W | None = None
    while i < len(args):
        t = args[i].text
        if t == "--":
            i += 1
            break
        if t.startswith("-") and len(t) > 1 and not t.startswith("--"):
            if "c" in t[1:]:
                i += 1
                if i < len(args):
                    code = args[i]
                    i += 1
                break
            if t[-1] == "O" or t[-1] == "o":
                i += 1
            i += 1
            continue
        if t.startswith("--") or t.startswith("+"):
            i += 1
            continue
        break
    if code is None and i < len(args):
        script = args[i]
    if code is not None:
        if code.unknown:
            raise Deny("`sh -c` with a computed string can't be checked")
        check_source(code.text, ctx)
        return
    if script is not None:
        _check_path_word(script, ctx, recursive=False)
        _scan_script(script, ctx)
        return
    if cmd.piped:
        raise Deny("piping text into a shell can't be checked")
    if any(op == "<" for op, _ in cmd.redirects):
        raise Deny("feeding a file to a shell on stdin can't be checked")
    for body in cmd.heredocs:
        check_source(body, ctx)


def _same_as_base(path: str, policy: Policy) -> bool:
    """Is this file byte for byte what the base branch has? Then the repository wrote it, not this worker, and scanning it finds only
    what the scan can't follow: the app's own scripts/dev.sh starts `repo=$(cd "$(dirname "$0")/.." && pwd)`. A worker that changes
    the script gets it scanned again. Comparing git's blob ids, computed here, so a file in the worktree can't answer for itself."""
    if not policy.common_dir or not policy.base:
        return False
    rel = os.path.relpath(os.path.realpath(path), os.path.realpath(policy.worktree))
    if rel.startswith(".."):
        return False
    try:
        with open(path, "rb") as fh:
            data = fh.read()
        want = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
        out = subprocess.run(
            ["git", "--git-dir", policy.common_dir, "rev-parse", "--verify", "-q", f"refs/remotes/origin/{policy.base}:{rel}"],
            capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if out.returncode == 0 and out.stdout.strip() == want:
        return True
    # The worktree may be older than the base's tip (main can change a script after the builder began). Any
    # version that was on the base is still the base's: its last 50, read from the common dir, never by git in the worktree.
    try:
        hist = subprocess.run(
            ["git", "--git-dir", policy.common_dir, "log", "-n", "50", "--format=%H", f"refs/remotes/origin/{policy.base}", "--", rel],
            capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL,
        )
        if hist.returncode != 0:
            return False
        for commit in hist.stdout.split():
            for spec in (f"{commit}:{rel}", f"{commit}^:{rel}"):
                got = subprocess.run(
                    ["git", "--git-dir", policy.common_dir, "rev-parse", "--verify", "-q", spec],
                    capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL,
                )
                if got.returncode == 0 and got.stdout.strip() == want:
                    return True
    except (OSError, subprocess.SubprocessError):
        return False
    return False


_SELF_FORMS = (
    ('$(cd "$(dirname "$0")/.." && pwd)', "parent"),
    ('$(cd "$(dirname "$0")" && pwd)', "dir"),
    ('$(dirname "$0")', "dir"),
)
_PLAIN_PATH = re.compile(r"^/[A-Za-z0-9._/@+-]*$")


def _known_self_paths(text: str, path: str, policy: Policy) -> str:
    """A run script's `$0` is the path it was run by, which the scan knows. So the usual first line,
    `repo=$(cd "$(dirname "$0")/.." && pwd)`, means one known directory, and is checked as that directory.
    Only these exact forms, only for a script inside the worktree, and only for a plain path."""
    here = os.path.normpath(os.path.dirname(path))
    parent = os.path.dirname(here)
    if not _PLAIN_PATH.match(here) or not _under(os.path.realpath(here), os.path.realpath(policy.worktree)):
        return text
    for form, which in _SELF_FORMS:
        text = text.replace(form, here if which == "dir" else parent)
    return text


def _scan_script(w: W, ctx: Ctx) -> None:
    """Refuse a local shell script that itself does something forbidden."""
    if w.unknown:
        return
    forms = _candidates(w.text, ctx.policy, ctx.cwd)
    if not forms:
        return
    path = forms[-1]
    if _same_as_base(path, ctx.policy):
        return
    try:
        with open(path, "rb") as fh:
            head = fh.read(256 * 1024)
    except OSError:
        return
    first = head.split(b"\n", 1)[0]
    is_shell = first.startswith(b"#!") and re.search(rb"\b(sh|bash|dash|zsh|ksh)\b", first)
    if not is_shell and not w.text.endswith((".sh", ".bash")):
        return
    try:
        text = head.decode("utf-8", "replace")
    except ValueError:
        return
    try:
        check_source(_known_self_paths(text, path, ctx.policy), ctx)
    except Deny as exc:
        raise Deny(f"{w.text}: {exc}") from exc


def _analyze_interpreter(name: str, words: list[W], cmd: Command, ctx: Ctx) -> None:
    args = words[1:]
    inline = False
    for i, a in enumerate(args):
        t = a.text
        if t in INLINE_CODE_FLAGS or re.fullmatch(r"-[a-zA-Z]*[ce]", t):
            if i + 1 < len(args):
                inline = True
                _text_checks(args[i + 1].text, ctx, name, inline_code=True)
    positional = [a for a in args if not a.text.startswith("-")]
    if not inline and not positional:
        if cmd.piped:
            raise Deny("piping text into an interpreter can't be checked")
    if not inline and args and args[0].text == "-" and cmd.piped:
        raise Deny("piping text into an interpreter can't be checked")
    for body in cmd.heredocs:
        _text_checks(body, ctx, name, inline_code=True)
    # The code argument isn't a path; check the rest of the words generically.
    for w in args:
        if not (inline and w.text and ("\n" in w.text or " " in w.text)):
            _check_path_word(w, ctx, recursive=False)


def _analyze_forge(name: str, words: list[W], cmd: Command, ctx: Ctx) -> None:
    lowered = [w.text.lower() for w in words[1:]]
    joined = " ".join(lowered)
    if any(w.unknown for w in words[1:3]):
        raise Deny(f"`{name}` with a computed subcommand can't be checked")
    if "merge" in lowered or "/merge" in joined or "--merge" in lowered:
        raise Deny("workers never merge; report `ready` and a person merges")
    if "release" in lowered[:1] or "releases" in lowered[:2] or "tag" in lowered[:1]:
        raise Deny("workers never tag or release")
    if lowered[:2] in (["auth", "token"], ["auth", "status"]) or (name == "tea" and lowered[:1] == ["login"]):
        raise Deny(f"`{name}` credentials are off limits")
    _generic(words, cmd, ctx, name)


def _generic(words: list[W], cmd: Command, ctx: Ctx, name: str) -> None:
    inert = name in INERT and not any(">" in op for op, _ in cmd.redirects)
    flags = {w.text for w in words[1:] if w.text.startswith("-")}
    recursive = name in RECURSIVE or (
        name in RECURSIVE_WITH_FLAG
        and any(f in ("--recursive", "--dereference-recursive") or (not f.startswith("--") and re.search("[rR]", f)) for f in flags)
    )
    positional = [w for w in words[1:] if not w.text.startswith("-")]

    if name in RECURSIVE or recursive:
        # With no path argument these commands walk the current directory.
        if ctx.cwd and not positional[(0 if name in ("find", "fd", "fdfind", "tree", "du") else 1) :]:
            if _is_ancestor_of_forbidden(ctx.cwd, ctx.policy):
                raise Deny(f"a recursive command from {ctx.cwd} would reach forbidden files; stay inside your worktree")

    # A search pattern or printed text isn't a path. Later words still are.
    not_paths: list[W] = []
    if name in SEARCHERS and positional:
        not_paths = [positional[0]]
    elif name in ("echo", "printf"):
        not_paths = positional
    for w in words[1:]:
        if not any(w is item for item in not_paths):
            _check_path_word(w, ctx, recursive=recursive)
        if not inert and not _inside_worktree(w, ctx):
            _text_checks(w.text, ctx, name)

    if name in WRITERS or any(">" in op for op, _ in cmd.redirects):
        for w in words[1:]:
            if w.unknown or w.text.startswith("-"):
                continue
            for path in _candidates(w.text, ctx.policy, ctx.cwd):
                reason = _control_file_reason(path, ctx.policy)
                if reason:
                    raise Deny(reason)
    if not inert:
        _check_heredocs_as_data(cmd, ctx)
    if name == "curl" or name == "wget":
        _text_checks(" ".join(w.text for w in words[1:]), ctx, name)


# -------- git --------


def _analyze_git(words: list[W], cmd: Command, ctx: Ctx) -> None:
    args = words[1:]
    git_cwd = ctx
    i = 0
    config_overrides: list[str] = []
    while i < len(args):
        a = args[i]
        t = a.text
        if a.unknown:
            raise Deny("git with a computed argument before the subcommand can't be checked")
        if t == "-C":
            i += 1
            if i >= len(args) or args[i].unknown:
                raise Deny("`git -C` with a computed path can't be checked")
            _check_path_word(args[i], ctx, recursive=False)
            forms = _candidates(args[i].text, ctx.policy, ctx.cwd)
            if not forms or not _under(forms[-1], ctx.policy.worktree):
                raise Deny("`git -C` must stay inside your worktree")
            git_cwd = ctx.copy()
            git_cwd.cwd = forms[-1]
            i += 1
        elif t == "-c":
            i += 1
            if i >= len(args) or args[i].unknown:
                raise Deny("`git -c` with a computed value can't be checked")
            config_overrides.append(args[i].text)
            i += 1
        elif t.startswith("--config-env"):
            raise Deny("`git --config-env` is not allowed")
        elif t in ("--git-dir", "--work-tree", "--namespace", "--exec-path", "--super-prefix") or t.startswith(
            ("--git-dir=", "--work-tree=", "--namespace=", "--exec-path=", "--super-prefix=")
        ):
            if t == "--exec-path" and i + 1 >= len(args):
                return
            raise Deny(f"`git {t.split('=')[0]}` is not allowed")
        elif t.startswith("-"):
            i += 1
        else:
            break
    if i >= len(args):
        return
    sub = args[i]
    sub_args = args[i + 1 :]
    if sub.unknown:
        raise Deny("git with a computed subcommand can't be checked")
    for item in config_overrides:
        key = item.split("=", 1)[0]
        if DANGEROUS_GIT_CONFIG.match(key):
            raise Deny(f"`git -c {key}` is not allowed")
    name = sub.text

    if name in ("send-pack", "http-push", "receive-pack", "http-backend", "upload-pack", "shell"):
        raise Deny(f"`git {name}` is not allowed")
    if name.startswith("credential"):
        raise Deny("git credentials are off limits")
    if name not in GIT_KNOWN:
        raise Deny(f"`git {name}` is not a command this policy knows (aliases are not allowed)")

    if name == "push":
        if config_overrides:
            raise Deny("`git -c ... push` is not allowed")
        if ctx.policy.read_only:
            raise Deny("a reviewer never pushes")
        _check_push(sub_args, git_cwd)
        return
    if name == "tag":
        _check_tag(sub_args)
    elif name == "config":
        _check_git_config(sub_args)
    elif name == "remote":
        verbs = [w.text for w in sub_args if not w.text.startswith("-")]
        if verbs and verbs[0] not in ("show", "get-url"):
            raise Deny("changing git remotes is not allowed")
    elif name == "worktree":
        verbs = [w.text for w in sub_args if not w.text.startswith("-")]
        if verbs and verbs[0] != "list":
            raise Deny("agentd manages worktrees; `git worktree` may only list")
    elif name in ("checkout", "switch", "symbolic-ref", "update-ref", "bisect"):
        ctx.branch_changed = True
    elif name == "branch":
        if any(w.text in ("-m", "-M", "--move", "-f", "--force", "-c", "-C") for w in sub_args):
            ctx.branch_changed = True
    if ctx.policy.read_only and name in ("commit", "merge", "rebase", "cherry-pick", "revert", "am"):
        raise Deny("a reviewer's worktree is read-only")

    joined = " ".join(w.text for w in sub_args)
    if name in GIT_EMBEDS_COMMANDS:
        if re.search(r"(?<![\w-])push(?![\w-])", joined) or "git-receive-pack" in joined:
            raise Deny(f"`git {name}` running a push is not allowed")
        _text_checks(joined, ctx, name)

    # Paths are still checked for every subcommand; messages aren't paths.
    skip_next = False
    for w in sub_args:
        if skip_next:
            skip_next = False
            continue
        if name == "commit" and w.text in ("-m", "--message"):
            skip_next = True
            continue
        if name == "commit" and w.text.startswith("--message="):
            continue
        _check_path_word(w, git_cwd, recursive=False)
    if name not in ("commit", "log", "show", "grep", "diff", "status"):
        _check_heredocs_as_data(cmd, ctx)
        for w in sub_args:
            if not _inside_worktree(w, ctx) and TEXT_SECRET.search(w.text):
                raise Deny("this mentions a secret location or credential variable")


def _check_tag(args: list[W]) -> None:
    listing = {"-l", "--list", "-n", "--contains", "--no-contains", "--merged", "--no-merged", "--points-at", "--sort", "--format", "--column", "--no-column", "-i", "--ignore-case", "-v", "--verify"}
    if not args:
        return
    flags = {w.text.split("=", 1)[0] for w in args if w.text.startswith("-")}
    if flags & listing and not flags & {"-d", "--delete", "-a", "--annotate", "-s", "--sign", "-f", "--force", "-m", "--message", "-F", "--file", "-u"}:
        return
    raise Deny("workers never create or delete tags")


def _check_git_config(args: list[W]) -> None:
    flags = [w.text for w in args if w.text.startswith("-")]
    positional = [w.text for w in args if not w.text.startswith("-")]
    if any(w.unknown for w in args):
        raise Deny("`git config` with a computed argument can't be checked")
    if any(f in ("--global", "--system", "--file", "-f", "--edit", "-e", "--blob", "--worktree") for f in flags):
        raise Deny("only reading this repo's git config is allowed")
    read_flags = {"--get", "--get-all", "--get-regexp", "--list", "-l", "--get-urlmatch", "--name-only", "--show-origin", "--show-scope"}
    writing = not any(f in read_flags for f in flags) and (
        len(positional) >= 2 or (positional[:1] and positional[0] in ("set", "unset")) or any(f.startswith(("--add", "--unset", "--replace", "--rename", "--remove")) for f in flags)
    )
    if not writing:
        return
    key = positional[1] if positional[:1] and positional[0] in ("set", "unset") and len(positional) > 1 else (positional[0] if positional else "")
    if DANGEROUS_GIT_CONFIG.match(key) or not key:
        raise Deny(f"`git config {key}` could redirect or rewrite a push and is not allowed")


def _allowed_branch(branch: str, policy: Policy) -> bool:
    if branch.startswith("refs/"):
        if not branch.startswith("refs/heads/"):
            return False
        branch = branch[len("refs/heads/") :]
    if ".." in branch or branch.endswith((".lock", "/")) or "//" in branch or "@{" in branch:
        return False
    if policy.push_branches:
        return branch in policy.push_branches
    return any(branch.startswith(prefix) and len(branch) > len(prefix) for prefix in policy.push_prefixes)


def _current_branch(ctx: Ctx) -> str | None:
    if not ctx.cwd:
        return None
    try:
        out = subprocess.run(
            ["git", "-C", ctx.cwd, "symbolic-ref", "-q", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def _check_push(args: list[W], ctx: Ctx) -> None:
    policy = ctx.policy
    positional: list[str] = []
    end_of_options = False
    for w in args:
        if w.unknown or w.has_glob:
            raise Deny("`git push` with a computed or glob argument can't be checked")
        t = w.text
        if end_of_options or not t.startswith("-"):
            positional.append(t)
        elif t == "--":
            end_of_options = True
        elif t.startswith("--"):
            if t not in PUSH_FLAGS_OK:
                raise Deny(f"`git push {t}` is not allowed")
        else:
            if not set(t[1:]) <= PUSH_SHORT_OK:
                raise Deny(f"`git push {t}` is not allowed")
    if positional and positional[0] != "origin":
        raise Deny("push only to `origin`")
    refspecs = positional[1:]
    if not refspecs:
        _check_implicit_push(ctx)
        return
    for spec in refspecs:
        if spec.startswith("+"):
            raise Deny("force pushes are not allowed")
        if ":" in spec:
            src, dst = spec.split(":", 1)
            if not src:
                raise Deny("deleting a remote branch is not allowed")
            if not _allowed_branch(dst, policy):
                raise Deny(f"pushing to `{dst}` is not allowed; push only your ticket's branch")
        elif spec == "HEAD":
            _check_implicit_push(ctx)
        else:
            if not _allowed_branch(spec, policy):
                raise Deny(f"pushing to `{spec}` is not allowed; push only your ticket's branch")


def _check_implicit_push(ctx: Ctx) -> None:
    if ctx.branch_changed:
        raise Deny("this command changes branch before pushing; name it: `git push origin HEAD:<your branch>`")
    branch = _current_branch(ctx)
    if not branch or not _allowed_branch(branch, ctx.policy):
        raise Deny(f"the current branch ({branch or 'unknown'}) is not one this ticket may push")


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

READ_TOOLS = {"Read", "NotebookRead"}
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
OWN_MCP_PREFIX = "mcp__factory-tools__"
# A turn is one `claude -p` run. When the model ends its turn to wait for a background task or a wakeup, the run
# exits, the factory sees a turn with no report, and the background work is killed with the turn (seen on a real PR).
LATER_TOOLS = {"ScheduleWakeup", "Monitor", "CronCreate", "RemoteTrigger"}
FOREGROUND = (
    "nothing runs after your turn ends: a background task or wakeup is killed with it, and the factory reads the "
    "turn as ending without a report. This isn't a block. Run it again in the foreground, with a `timeout` long "
    "enough (Bash allows up to 30 minutes), or run the suites one at a time, and report when it's done"
)


def _session_files(policy: Policy) -> list[str]:
    # Large tool outputs are saved under the session's project directory and the
    # model is told to Read them, so that one directory stays readable.
    slug = re.sub(r"[^A-Za-z0-9]", "-", policy.worktree)
    return [os.path.join(home, ".claude", "projects", slug) for home in policy.homes]


def decide(payload: dict, policy: Policy) -> str | None:
    """Return a refusal reason, or None to allow."""
    tool = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return "malformed tool input"
    cwd = payload.get("cwd") or policy.worktree
    if tool in LATER_TOOLS or tool_input.get("run_in_background") not in (None, False):
        return FOREGROUND
    try:
        if tool == "Bash":
            command = tool_input.get("command")
            if not isinstance(command, str):
                return "malformed Bash input"
            check_source(command, Ctx(policy, cwd))
            return None
        if tool in READ_TOOLS or tool in WRITE_TOOLS:
            return _file_tool(tool, tool_input, policy, cwd)
        if tool == "Grep":
            return _search_tool(tool_input.get("path"), None, policy, cwd)
        if tool == "Glob":
            return _search_tool(tool_input.get("path"), tool_input.get("pattern"), policy, cwd)
        if tool.startswith("mcp__"):
            if tool.startswith(OWN_MCP_PREFIX):
                return None
            return "only the factory-tools MCP server is available"
        return _unknown_tool(tool_input, policy, cwd)
    except Deny as exc:
        return str(exc)


def _file_tool(tool: str, tool_input: dict, policy: Policy, cwd: str) -> str | None:
    path = tool_input.get("file_path") or tool_input.get("notebook_path") or tool_input.get("path")
    if not isinstance(path, str) or not path:
        return f"{tool} needs a file path"
    if tool in READ_TOOLS:
        real = os.path.realpath(_expand_tilde(path, policy))
        if any(_under(real, root) for root in _session_files(policy)):
            return None
    reason = _path_reason(path, policy, cwd)
    if reason:
        return reason
    if tool in WRITE_TOOLS:
        if policy.read_only:
            return "a reviewer's worktree is read-only"
        for candidate in _candidates(path, policy, cwd):
            reason = _control_file_reason(candidate, policy)
            if reason:
                return reason
    return None


def _search_tool(path, pattern, policy: Policy, cwd: str) -> str | None:
    base = path if isinstance(path, str) and path else cwd
    reason = _path_reason(base, policy, cwd)
    if reason:
        return reason
    resolved = _candidates(base, policy, cwd)
    resolved_base = resolved[-1] if resolved else cwd
    if isinstance(pattern, str) and pattern:
        literal = re.split(r"[*?\[{]", pattern, maxsplit=1)[0]
        joined = os.path.join(resolved_base, literal)
        reason = _path_reason(joined, policy, cwd)
        if reason:
            return reason
        if not _under(os.path.normpath(joined), policy.worktree):
            return "search inside your worktree"
    if not _under(resolved_base, policy.worktree) and _is_ancestor_of_forbidden(resolved_base, policy):
        return f"a search from {resolved_base} would reach forbidden files; stay inside your worktree"
    return None


def _unknown_tool(tool_input: dict, policy: Policy, cwd: str) -> str | None:
    def walk(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for item in value.values():
                yield from walk(item)
        elif isinstance(value, list):
            for item in value:
                yield from walk(item)

    for text in walk(tool_input):
        if len(text) < 4096 and text.startswith(("/", "~")) and "\n" not in text:
            reason = _path_reason(text, policy, cwd)
            if reason:
                return reason
    return None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _audit(path: str | None, payload: dict, reason: str) -> None:
    if not path:
        return
    try:
        record = {
            "ts": time.time(),
            "tool": payload.get("tool_name"),
            "input": str(payload.get("tool_input"))[:400],
            "reason": reason,
        }
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError:
        pass


def main(argv: list[str]) -> int:
    try:
        policy_path = audit = None
        i = 0
        while i < len(argv):
            if argv[i] == "--policy":
                policy_path = argv[i + 1]
                i += 2
            elif argv[i] == "--audit":
                audit = argv[i + 1]
                i += 2
            else:
                i += 1
        if not policy_path:
            raise ValueError("no --policy")
        policy = Policy.load(policy_path)
        payload = json.loads(sys.stdin.read())
        reason = decide(payload, policy)
    except BaseException as exc:  # noqa: BLE001 - fail closed on anything at all
        print(f"factory policy hook failed, refusing the call: {type(exc).__name__}", file=sys.stderr)
        return 2
    if reason:
        _audit(audit, payload, reason)
        print(f"factory policy refused this: {reason}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
