#!/opt/patroni/bin/python
"""Render the Patroni config template from environment variables.

Usage: render-patroni-config TEMPLATE OUTPUT

Every LEADERFALL_* and PATRONI_NAME variable is exposed to the template.
Missing variables are an error: the Compose file must set them all.
"""

import os
import sys
from pathlib import Path

from jinja2 import Environment, StrictUndefined


def main() -> int:
    if len(sys.argv) != 3:
        sys.stderr.write(str(__doc__))
        return 2
    template_path, output_path = Path(sys.argv[1]), Path(sys.argv[2])

    env = Environment(undefined=StrictUndefined, keep_trailing_newline=True, autoescape=False)
    template = env.from_string(template_path.read_text())

    context = {
        "name": os.environ["PATRONI_NAME"],
        "scope": os.environ.get("LEADERFALL_SCOPE", "leaderfall"),
        "etcd_hosts": os.environ["LEADERFALL_ETCD_HOSTS"],
        "ttl": int(os.environ["LEADERFALL_TTL"]),
        "loop_wait": int(os.environ["LEADERFALL_LOOP_WAIT"]),
        "retry_timeout": int(os.environ["LEADERFALL_RETRY_TIMEOUT"]),
        "maximum_lag_on_failover": int(os.environ["LEADERFALL_MAX_LAG"]),
        "synchronous_mode": os.environ["LEADERFALL_SYNC_MODE"],
        "synchronous_mode_strict": os.environ["LEADERFALL_SYNC_STRICT"],
        "superuser_password": os.environ["LEADERFALL_SUPERUSER_PASSWORD"],
        "replication_password": os.environ["LEADERFALL_REPLICATION_PASSWORD"],
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(template.render(**context))
    return 0


if __name__ == "__main__":
    sys.exit(main())
