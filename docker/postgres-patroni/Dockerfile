# PostgreSQL 17 managed by Patroni, talking to etcd over the v3 API.
# iproute2 is included so the harness can run `tc netem` inside the container.
FROM postgres:17.11-trixie

ARG PATRONI_VERSION=4.1.5
ARG JINJA2_VERSION=3.1.6

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        python3 \
        python3-venv \
        iproute2 \
        procps \
        curl; \
    rm -rf /var/lib/apt/lists/*; \
    python3 -m venv /opt/patroni; \
    /opt/patroni/bin/pip install --no-cache-dir \
        "patroni[etcd3,psycopg3]==${PATRONI_VERSION}" \
        "jinja2==${JINJA2_VERSION}"

ENV PATH="/opt/patroni/bin:${PATH}"

COPY patroni.yml.j2 /etc/patroni/patroni.yml.j2
COPY render.py /usr/local/bin/render-patroni-config
COPY entrypoint.sh /usr/local/bin/leaderfall-entrypoint

RUN chmod +x /usr/local/bin/render-patroni-config /usr/local/bin/leaderfall-entrypoint

EXPOSE 5432 8008

ENTRYPOINT ["leaderfall-entrypoint"]
