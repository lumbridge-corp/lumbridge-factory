"""python3 -m lumbridge_factory.agentd --config /etc/<name>-factory/agentd.json"""

from __future__ import annotations

import argparse
import logging
import sys

from . import config as config_module
from .daemon import serve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lumbridge_factory.agentd", description=__doc__)
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = config_module.load(args.config)
    except config_module.ConfigError as exc:
        print(f"agentd: {exc}", file=sys.stderr)
        return 2
    return serve(config)


if __name__ == "__main__":
    sys.exit(main())
