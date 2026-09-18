#!/usr/bin/env bash
# Prepare the data volume, render patroni.yml from the environment, start Patroni.
set -euo pipefail

PGROOT=/var/lib/postgresql/data
CONFIG=/etc/patroni/patroni.yml

mkdir -p "$PGROOT"
chown -R postgres:postgres "$PGROOT"
chmod 0700 "$PGROOT"

# Rendered as root; the file holds passwords, so only postgres may read it.
render-patroni-config /etc/patroni/patroni.yml.j2 "$CONFIG"
chown postgres:postgres "$CONFIG"
chmod 0600 "$CONFIG"

exec gosu postgres patroni "$CONFIG"
