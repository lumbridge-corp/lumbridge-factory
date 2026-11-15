"""factoryd (§3): the main loop the engine lacks. One writer, enforced by the store's lock.

Each tick drains the command spool, polls Sources, evaluates Threads, takes in what running turns
produced, reads the gates, schedules turns and runs Outbox effects, all through engine.Engine. Turns
run in agentd, so killing this process doesn't stop one: on restart the engine asks agentd what's
alive and attaches, collects, or sends a recovery note (§8).

    python3 -m lumbridge_factory.factoryd --config <file>
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .accounts import ProviderError, load_provider
from .agentd.client import AgentdClient
from .channels.feed import RoutedChannel
from .channels.gitea import GiteaChannel
from .core.executor import ExecutorError
from .core.policy import Policy, PolicyError
from .engine import Engine
from .gates import GiteaGates
from .gitea_api import GiteaClient, GiteaError
from .router import NoModelRouter
from .snapshot import build as build_snapshot
from .sources.gitea import GiteaSource
from .store import Store, WriterBusy
from .turns import ThreadRunner

log = logging.getLogger("lumbridge_factory.factoryd")
MAX_FAILED_TICKS = 30
IDENTITY_RETRY_SECONDS = 15.0


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    db: str
    socket: str
    gitea_url: str
    token_file: str  # the factory's account: reads, comments, opening PRs, updating branches
    review_token_file: str  # the reviewer's account; only its comments count at gate 2
    accounts: tuple[str, ...]
    policy: dict[str, Any] = field(default_factory=dict)  # installed once, on a new store
    allow_sandbox: bool = False
    tick_seconds: float = 1.0
    poll_seconds: float = 15.0
    gate_read_seconds: float = 20.0
    retain_ended_seconds: float = 14 * 86400.0
    gitea_timeout: float = 30.0
    accounts_provider: str = ""  # "package.module:attribute" of an add-on's router; empty means the fixed list (§7)
    accounts_options: dict[str, Any] = field(default_factory=dict)  # passed to that provider as keyword arguments
    snapshot_file: str = ""  # where a status screen reads the picture; empty means nowhere
    snapshot_seconds: float = 15.0


def load_config(path: str) -> Config:
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError) as exc:
        raise ConfigError(f"can't read {path}: {exc}") from exc
    missing = [k for k in ("db", "socket", "gitea_url", "token_file", "review_token_file", "accounts", "policy") if not raw.get(k)]
    if missing:
        raise ConfigError(f"missing: {', '.join(missing)}")
    # A key starting with "_" is a note for people, such as an instance's later-phase repositories.
    raw = {k: v for k, v in raw.items() if not k.startswith("_")}
    for name in ("db", "socket", "token_file", "review_token_file"):
        raw[name] = os.path.abspath(os.path.expanduser(raw[name]))
    raw["accounts"] = tuple(raw["accounts"])
    if not isinstance(raw.get("accounts_options", {}), dict):
        raise ConfigError("accounts_options must be an object")
    known = set(Config.__dataclass_fields__)
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(f"unknown settings: {', '.join(sorted(unknown))}")
    return Config(**raw)


def boot_time() -> float | None:
    """When this machine came up (§8). Jobs that started before it may be the outage's casualties."""
    try:
        with open("/proc/stat", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("btime "):
                    return float(line.split()[1])
    except OSError:
        pass
    return None


def check_identities(policy: Policy, factory_login: str, reviewer_login: str) -> None:
    """A reviewer token that isn't a reviewer bot means no verdict ever counts and every Ticket
    waits forever. Better not to start."""
    if factory_login not in policy.bots:
        raise ConfigError(f"the factory token is {factory_login}, which isn't in policy.bots {list(policy.bots)}")
    if reviewer_login not in policy.reviewer_bots:
        raise ConfigError(f"the reviewer token is {reviewer_login}, which isn't in policy.reviewer_bots {list(policy.reviewer_bots)}")


def build_engine(config: Config, store: Store, executor: Any, *, clock: Any = time.time) -> Engine:
    client = GiteaClient(config.gitea_url, config.token_file, config.gitea_timeout)
    review_client = GiteaClient(config.gitea_url, config.review_token_file, config.gitea_timeout)

    def policy() -> Policy:
        current = store.policy()
        assert current is not None
        return current

    gates = GiteaGates(client, policy)
    try:
        accounts = load_provider(config.accounts_provider, config.accounts, config.accounts_options, clock)
    except ProviderError as exc:
        raise ConfigError(str(exc)) from exc
    return Engine(
        store,
        executor=executor,
        gates=gates,
        channel=RoutedChannel(GiteaChannel(client, review_client), store, clock),
        accounts=accounts,
        assessor=NoModelRouter(),
        runner=ThreadRunner(),
        sources=(GiteaSource(client, policy, gates.ci),),
        clock=clock,
        spool_dir=config.db + ".commands",
        source_poll_seconds=config.poll_seconds,
        gate_read_seconds=config.gate_read_seconds,
        retain_ended_seconds=config.retain_ended_seconds,
        boot_time=boot_time(),
    )


class Factoryd:
    def __init__(self, config: Config, *, executor: Any = None):
        self.config = config
        self.executor = executor if executor is not None else AgentdClient(config.socket)
        self.done = threading.Event()
        self.next_snapshot = 0.0
        self.store = Store.writer(config.db)
        self.engine = build_engine(config, self.store, self.executor)
        self.engine.bootstrap(Policy.from_dict(config.policy), actor="factoryd")
        policy = self.engine.policy
        policy.validate()
        if policy.sandbox_shared_account and not config.allow_sandbox:
            raise ConfigError("this policy uses a shared sandbox account; start with allow_sandbox only on the sandbox")

    def _wait_for_identities(self) -> None:
        """A forge that's down at boot isn't a reason to exit. A wrong token is."""
        client = GiteaClient(self.config.gitea_url, self.config.token_file, self.config.gitea_timeout)
        review = GiteaClient(self.config.gitea_url, self.config.review_token_file, self.config.gitea_timeout)
        while not self.done.is_set():
            try:
                check_identities(self.engine.policy, client.login(), review.login())
                return
            except GiteaError as exc:
                log.warning("can't check who the tokens are yet: %s", exc)
                self.done.wait(IDENTITY_RETRY_SECONDS)

    def _publish_snapshot(self) -> None:
        """Whole file or nothing, world-readable: it says what the factory is doing and holds no secret."""
        now = time.time()
        if not self.config.snapshot_file or now < self.next_snapshot:
            return
        self.next_snapshot = now + self.config.snapshot_seconds
        tmp = f"{self.config.snapshot_file}.tmp.{os.getpid()}"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(build_snapshot(self.store, now), fh, indent=1, default=str)
            os.chmod(tmp, 0o644)
            os.replace(tmp, self.config.snapshot_file)
        except OSError as exc:
            log.warning("can't write the snapshot: %s", exc)

    def run(self) -> int:
        self._wait_for_identities()
        try:
            log.info("agentd: %s", self.executor.hello())
        except (ExecutorError, AttributeError) as exc:
            log.warning("agentd isn't answering yet: %s", exc)  # recovery keeps trying
        if self.engine.policy.sandbox_shared_account:
            log.warning("SANDBOX: one account is the owner, the factory and the reviewer. Not a production setup.")
        log.info("factoryd running on %s", self.config.db)
        failed = 0
        while not self.done.is_set():
            try:
                moved = self.engine.tick()
                failed = 0
                self._publish_snapshot()
            except Exception:
                failed += 1
                log.exception("tick failed (%d in a row)", failed)
                if failed >= MAX_FAILED_TICKS:
                    return 1  # the unit restarts us, which runs recovery again
                moved = 0
            if not moved:
                self.done.wait(self.config.tick_seconds)
        log.info("factoryd stopping; running turns carry on in agentd")
        return 0

    def stop(self, *_: Any) -> None:
        self.done.set()

    def close(self) -> None:
        self.store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="factoryd", description="Lumbridge Factory's domain engine, one per instance")
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s", stream=sys.stderr)
    try:
        daemon = Factoryd(load_config(args.config))
    except WriterBusy as exc:
        print(f"factoryd: {exc}", file=sys.stderr)
        return 3
    except (ConfigError, PolicyError) as exc:
        print(f"factoryd: {exc}", file=sys.stderr)
        return 2
    signal.signal(signal.SIGTERM, daemon.stop)
    signal.signal(signal.SIGINT, daemon.stop)
    try:
        return daemon.run()
    except ConfigError as exc:
        print(f"factoryd: {exc}", file=sys.stderr)
        return 2
    finally:
        daemon.close()


if __name__ == "__main__":
    raise SystemExit(main())
