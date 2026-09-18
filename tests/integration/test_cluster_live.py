"""Checks against a running cluster. Run with ``pytest -m integration`` after ``leaderfall up``."""

import psycopg
import pytest

from leaderfall.cluster import (
    HAPROXY_READ_PORT,
    HAPROXY_WRITE_PORT,
    NODES,
    PatroniClient,
    conninfo,
)

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def patroni() -> PatroniClient:
    return PatroniClient()


def test_topology_is_healthy(patroni: PatroniClient) -> None:
    state = patroni.cluster()
    assert state.health_problems() == []


def test_every_node_answers_rest(patroni: PatroniClient) -> None:
    for node in NODES:
        assert patroni.node_status(node) is not None, node.name


def test_write_port_is_primary() -> None:
    with psycopg.connect(conninfo(HAPROXY_WRITE_PORT), autocommit=True) as conn:
        assert conn.execute("select pg_is_in_recovery()").fetchone() == (False,)
        conn.execute("create table if not exists leaderfall_smoke(id int)")
        conn.execute("insert into leaderfall_smoke values (1)")


def test_read_port_is_replica() -> None:
    with psycopg.connect(conninfo(HAPROXY_READ_PORT)) as conn:
        assert conn.execute("select pg_is_in_recovery()").fetchone() == (True,)


def test_direct_ports_agree_with_patroni(patroni: PatroniClient) -> None:
    state = patroni.cluster()
    for node in NODES:
        member = state.member(node.name)
        assert member is not None
        with psycopg.connect(conninfo(node.pg_port)) as conn:
            row = conn.execute("select pg_is_in_recovery()").fetchone()
        assert row is not None
        assert row[0] is (not member.is_leader), node.name
