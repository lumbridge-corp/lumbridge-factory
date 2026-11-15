"""Names the factory writes where other programs, or its own earlier runs, read them back.

`TAG` marks the factory's own comments on the forge (`<!-- <TAG>:key=... -->`): that is how it finds what it already posted
and tells its words from a person's. `NEEDS_KEY` is the snapshot's list of tickets waiting on the owner. An install that began
under other names keeps them by setting these variables before the factory starts.
"""

import os
import re

TAG = os.environ.get("LUMBRIDGE_FACTORY_TAG") or "lumbridge-factory"
if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", TAG):
    raise SystemExit(f"LUMBRIDGE_FACTORY_TAG must be lower-case letters, digits and dashes: {TAG!r}")
NEEDS_KEY = os.environ.get("LUMBRIDGE_FACTORY_NEEDS_KEY") or "needs_owner"
