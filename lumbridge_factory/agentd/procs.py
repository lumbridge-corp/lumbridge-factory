"""Finding and killing what a turn left behind."""

from __future__ import annotations

import os
import signal
import time

# The variable every turn's processes carry, so a turn's strays can be found after they leave its process group.
TURN_VAR = "LUMBRIDGE_FACTORY_TURN"
# Its name before 0.1.0: turns started by an older agentd still carry it, so the scan looks for both.
OLD_TURN_VAR = "FACTORY2_TURN"


def group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def pids_with_marker(marker: str) -> list[int]:
    """Processes whose environment carries TURN_VAR=<marker> (or OLD_TURN_VAR=<marker>).

    This catches what left the process group with `setsid`. A process that also
    cleared its environment gets away until the unit stops (docs/AGENTD.md).
    """
    needles = {f"{var}={marker}".encode() for var in (TURN_VAR, OLD_TURN_VAR)}
    me = os.getpid()
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == me:
            continue
        try:
            with open(f"/proc/{entry}/environ", "rb") as fh:
                env = fh.read()
        except OSError:
            continue
        if needles.intersection(env.split(b"\0")):
            found.append(int(entry))
    return found


def kill_marked(marker: str) -> None:
    for _ in range(5):  # a fork can race the scan
        pids = pids_with_marker(marker)
        if not pids:
            return
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        time.sleep(0.05)


def sweep(pgid: int | None, marker: str, grace: float) -> None:
    """Nothing of this turn survives: SIGTERM, then after `grace`, SIGKILL."""
    if pgid is not None and group_alive(pgid):
        signal_group(pgid, signal.SIGTERM)
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline and group_alive(pgid):
            time.sleep(0.02)
        signal_group(pgid, signal.SIGKILL)
    kill_marked(marker)


def harden_daemon() -> None:
    """Make /proc/<agentd>/environ and ptrace unavailable to the workers, its own uid."""
    try:
        import ctypes

        PR_SET_DUMPABLE = 4
        ctypes.CDLL(None, use_errno=True).prctl(PR_SET_DUMPABLE, 0, 0, 0, 0)
    except (OSError, AttributeError):
        pass
