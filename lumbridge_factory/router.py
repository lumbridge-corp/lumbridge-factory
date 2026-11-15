"""§4.2, as far as this release needs: not at all for a Ticket's life.

The explicit path (label, /factory, a person's follow-up) never reaches the Router, so the real
session is left for a later release, with `main-red`. This one makes no model call and accepts nothing: `triage`
and `main-red` candidates, which start in observe mode, are recorded, and a builder's hand-back gets
"it didn't become new work" instead of waiting forever.
"""

from __future__ import annotations

from .core.model import Decision, RouterDecision
from .ports import Assessment


class NoModelRouter:
    def assess(self, request: Assessment) -> RouterDecision:
        return RouterDecision(Decision.IGNORE, note="no Router session yet")
