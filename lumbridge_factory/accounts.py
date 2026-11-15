"""§7: which account a turn runs on.

StaticAccounts is the built-in provider: a fixed list of the credentials the operator is entitled to use, tried in
order. An account whose turn ends on a provider's rate limit rests for a while; the turn waits rather than fails when
every account is resting.

An add-on can supply a different provider without changing this package. Name it in factoryd's config:

    "accounts_provider": "some_package.some_module:make_accounts",
    "accounts_options": {"any": "settings the provider takes"}

The target is called as `target(names=<the accounts list>, clock=<a time function>, **accounts_options)` and must
return an object with `pick(role, avoid="")` and `mark_limited(account)` (ports.AccountRouter). It may also offer
`used_pct(account)`: how much of the account's allowance is used, 0 to 100, or None when it can't say. The policy's
`tight_account` setting reads it. Whatever a provider does, follow your model provider's terms for the accounts it uses.
"""

from __future__ import annotations

import importlib
import time
from typing import Any, Callable, Mapping, Sequence

from .core.executor import Role

LIMIT_REST_SECONDS = 30 * 60.0


class ProviderError(Exception):
    """The configured accounts provider can't be loaded or doesn't build a router."""


class StaticAccounts:
    def __init__(self, names: Sequence[str], clock: Callable[[], float] = time.time):
        self.names = list(names)
        self.clock = clock
        self.limited_until: dict[str, float] = {}

    def _usable(self) -> list[str]:
        return [n for n in self.names if self.clock() >= self.limited_until.get(n, 0.0)]

    def pick(self, role: Role, avoid: str = "") -> str | None:
        usable = self._usable()
        for name in usable:
            if name != avoid:
                return name
        return usable[0] if usable else None

    def mark_limited(self, account: str) -> None:
        self.limited_until[account] = self.clock() + LIMIT_REST_SECONDS


def load_provider(
    spec: str, names: Sequence[str], options: Mapping[str, Any] | None = None, clock: Callable[[], float] = time.time
) -> Any:
    """The router `spec` ("package.module:attribute") builds; StaticAccounts when `spec` is empty."""
    options = dict(options or {})
    if not spec:
        if options:
            raise ProviderError("accounts_options without accounts_provider")
        return StaticAccounts(names, clock)
    module_name, sep, attr = spec.partition(":")
    if not sep or not module_name or not attr:
        raise ProviderError(f"accounts_provider must look like package.module:attribute, not {spec!r}")
    try:
        target = importlib.import_module(module_name)
        for part in attr.split("."):
            target = getattr(target, part)
    except (ImportError, AttributeError) as exc:
        raise ProviderError(f"can't load accounts_provider {spec}: {exc}") from exc
    try:
        router = target(names=list(names), clock=clock, **options)
    except TypeError as exc:
        raise ProviderError(f"accounts_provider {spec} refused its settings: {exc}") from exc
    missing = [m for m in ("pick", "mark_limited") if not callable(getattr(router, m, None))]
    if missing:
        raise ProviderError(f"accounts_provider {spec} built something without {', '.join(missing)}")
    return router
