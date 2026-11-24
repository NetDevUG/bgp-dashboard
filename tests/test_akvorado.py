"""Akvorado traffic integration (needs ClickHouse: TEST_CLICKHOUSE_URL, default http://127.0.0.1:8123/)."""
import asyncio
import os

import httpx
import pytest

from app.akvorado import AkvoradoService, build_query
from app.config import Settings

CH_URL = os.environ.get("TEST_CLICKHOUSE_URL", "http://127.0.0.1:8123/")
CH_AUTH = (os.environ.get("TEST_CLICKHOUSE_USER", "akvorado"), os.environ.get("TEST_CLICKHOUSE_PASSWORD", "secret"))
TABLE = "bgp_dashboard_test_flows"


def ch(query: str) -> str:
    r = httpx.post(CH_URL, content=query, auth=CH_AUTH, timeout=10)
    r.raise_for_status()
    return r.text


@pytest.fixture(scope="module")
def clickhouse():
    try:
        ch("SELECT 1")
    except Exception:
        if os.environ.get("REQUIRE_CLICKHOUSE") == "1":
            raise
        pytest.skip(f"ClickHouse not reachable at {CH_URL}")
    ch(f"DROP TABLE IF EXISTS {TABLE}")
    # the columns of Akvorado's consolidated flows tables that the dashboard uses
    ch(f"""CREATE TABLE {TABLE} (TimeReceived DateTime, SamplingRate UInt64, DstAS UInt32,
           OutIfBoundary Enum8('undefined' = 0, 'external' = 1, 'internal' = 2), Bytes UInt64, Packets UInt64)
           ENGINE = MergeTree ORDER BY (TimeReceived, DstAS)""")
    ch(f"""INSERT INTO {TABLE} VALUES
           (now() - 3600, 1000, 13335, 'external', 900000, 900), (now() - 7200, 1000, 13335, 'external', 900000, 900),
           (now() - 600, 100, 6939, 'external', 500000, 500),
           (now() - 600, 1000, 15169, 'internal', 999999999, 9),      -- internal: ignored
           (now() - 200000, 1000, 24940, 'external', 99999999, 9),    -- outside the window
           (now() - 60, 1, 0, 'external', 5, 1)                       -- unknown AS""")
    yield
    ch(f"DROP TABLE IF EXISTS {TABLE}")


def _settings(**kw):
    return Settings(akvorado_clickhouse_url=CH_URL, akvorado_user=CH_AUTH[0], akvorado_password=CH_AUTH[1],
                    akvorado_table=TABLE, **kw)


def test_query_traffic_per_asn(clickhouse):
    svc = AkvoradoService(_settings())
    asyncio.run(svc.refresh())
    assert svc.data["ok"] and svc.data["error"] is None
    assert svc.by_asn.keys() == {13335, 6939}
    assert svc.by_asn[13335] == pytest.approx(2 * 900000 * 1000 * 8 / 86400)
    assert svc.data["top"][0]["asn"] == 13335
    assert svc.traffic() == svc.by_asn


def test_wrong_password_keeps_running(clickhouse):
    svc = AkvoradoService(Settings(akvorado_clickhouse_url=CH_URL, akvorado_user=CH_AUTH[0],
                                   akvorado_password="wrong", akvorado_table=TABLE))
    asyncio.run(svc.refresh())
    assert svc.data["ok"] is False and "403" in svc.data["error"] and svc.traffic() is None


def test_table_name_is_validated():
    with pytest.raises(ValueError):
        build_query(Settings(akvorado_table="flows; DROP TABLE x"))


def test_disabled():
    assert AkvoradoService(Settings()).enabled is False and AkvoradoService(Settings()).traffic() is None
