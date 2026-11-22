"""Web side of the monitoring features: own prefixes, unstable prefixes, sessions, /metrics."""
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio

import gobgp_to_mongo as collector
import neighbor_poller as npoll
from conftest import MONGO_URI, SAMPLE

DB_NAME = "bgp_test_monitoring_web"
OWN = ("140.211.167.122/32",  # locally originated -> ok
       "69.89.123.0/24",      # origin AS14103 -> wrong origin
       "69.89.0.0/16",        # not announced, but 69.89.123.0/24 by AS14103 -> missing + foreign more-specific
       "198.51.100.0/24",     # missing
       "not-a-prefix")


@pytest.fixture(scope="module")
def sample_db():
    pymongo = pytest.importorskip("pymongo")
    client = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=1500, tz_aware=True)
    try:
        client.admin.command("ping")
    except Exception:
        pytest.skip(f"MongoDB not reachable at {MONGO_URI}")
    client.drop_database(DB_NAME)
    db = client[DB_NAME]
    collector.initialize_database(db)
    lines = SAMPLE.read_text().splitlines()
    for i in range(0, len(lines), 1000):
        collector.process_batch(db, lines[i:i + 1000])
    now = datetime.now(timezone.utc)
    up = {"_id": "192.0.2.1", "address": "192.0.2.1", "peer_asn": 3356, "description": 'Transit "A"',
          "state": "established", "established": True, "admin_state": "up", "flaps": 0,
          "uptime_since": now - timedelta(hours=5), "downtime_since": None, "disconnect_reason": None,
          "disconnect_message": None, "families": {"ipv4": {"received": 900, "accepted": 899}}}
    down = {**up, "_id": "192.0.2.5", "address": "192.0.2.5", "peer_asn": 6939, "description": "Peering",
            "state": "active", "established": False, "downtime_since": now - timedelta(minutes=3),
            "disconnect_reason": "hold timer expired"}
    disabled = {**down, "_id": "192.0.2.9", "address": "192.0.2.9", "admin_state": "down"}
    npoll.store_neighbors(db, [up, {**down, "state": "established", "established": True}, disabled],
                          now - timedelta(minutes=10))
    npoll.store_neighbors(db, [up, down, disabled], now)
    db.collector_status.update_one({"_id": "collector"}, {"$set": {"neighbors_polled": now, "heartbeat": now}})
    yield db
    client.drop_database(DB_NAME)


@pytest_asyncio.fixture
async def client(sample_db):
    from app.config import Settings
    from app.main import create_app

    settings = Settings(mongo_uri=MONGO_URI, mongo_db=DB_NAME, background_jobs=False, own_prefixes=OWN,
                        community_names={"3701:380": "Transit"})
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async def no_dns(name, rdtype="A"):
            return []
        app.state.dns.resolve = no_dns
        await app.state.stats.update_stats()
        await app.state.stats.update_advanced_stats()
        app.state.stats.ready = True
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
            yield http


async def test_own_prefixes(client):
    result = {p["prefix"]: p for p in (await client.get("/bgp/api/v1.0/own-prefixes")).json()}
    assert result["140.211.167.122/32"]["ok"] and result["140.211.167.122/32"]["route"]["origin_asn"] is None
    assert result["69.89.123.0/24"]["problems"] == ["wrong_origin"]
    wide = result["69.89.0.0/16"]
    assert wide["problems"] == ["missing", "foreign_more_specific"]
    assert [(m["prefix"], m["origin_asn"], m["allowed"]) for m in wide["more_specifics"]] == [
        ("69.89.123.0/24", 14103, False)]
    assert result["198.51.100.0/24"]["problems"] == ["missing"]
    assert result["not-a-prefix"]["problems"] == ["invalid_config"]
    stats = (await client.get("/bgp/api/v1.0/stats")).json()
    assert stats["own_prefix_problems"] == 4


async def test_allowed_origins(sample_db):
    from app.config import Settings
    from app.monitoring import check_own_prefixes
    from pymongo import AsyncMongoClient

    db = AsyncMongoClient(MONGO_URI)[DB_NAME]
    settings = Settings(own_prefixes=("69.89.0.0/16", "69.89.123.0/24"), allowed_origins=(14103,))
    result = await check_own_prefixes(db["bgp"], settings)
    assert result[0]["problems"] == ["missing"] and result[0]["more_specifics"][0]["allowed"]
    assert result[1]["ok"]


async def test_unstable(client, sample_db):
    u = (await client.get("/bgp/api/v1.0/unstable")).json()
    assert u["changed_prefixes"] == sample_db.bgp.count_documents({"last_change": {"$exists": True}})
    assert u["route_changes"] == u["route_changes_1h"] > 0
    top = u["top"]
    assert top and top[0]["changes"] >= top[-1]["changes"]
    assert top[0]["changes"] == len(sample_db.bgp.find_one({"_id": top[0]["prefix"]})["change_times"])


async def test_neighbors_and_events(client):
    n = (await client.get("/bgp/api/v1.0/neighbors")).json()
    assert n["summary"] == {"total": 3, "established": 1, "admin_down": 1, "down": 1}
    assert n["stale"] is False
    by_addr = {x["address"]: x for x in n["neighbors"]}
    assert by_addr["192.0.2.5"]["session_drops"] == 1
    assert 170 <= by_addr["192.0.2.5"]["duration_seconds"] <= 200
    assert by_addr["192.0.2.5"]["never_established"] is False
    events = (await client.get("/bgp/api/v1.0/neighbors/events")).json()
    assert events[0]["neighbor"] == "192.0.2.5" and events[0]["to_state"] == "active"


async def test_metrics(client):
    from prometheus_client.parser import text_string_to_metric_families

    res = await client.get("/metrics")
    assert res.status_code == 200 and res.headers["content-type"].startswith("text/plain; version=0.0.4")
    samples = {}
    for family in text_string_to_metric_families(res.text):
        for s in family.samples:
            samples[(s.name, tuple(sorted(s.labels.items())))] = s.value
    get = lambda metric, **labels: samples[(metric, tuple(sorted({k: str(v) for k, v in labels.items()}.items())))]
    assert get("bgp_stats_ready") == 1
    assert get("bgp_prefixes", ip_version=4) > 1000
    session = dict(neighbor="192.0.2.1", peer_asn=3356, description='Transit "A"')
    assert get("bgp_session_up", **session) == 1
    assert get("bgp_session_prefixes_accepted", **session, family="ipv4") == 899
    down = dict(neighbor="192.0.2.5", peer_asn=6939, description="Peering")
    assert get("bgp_session_up", **down) == 0 and get("bgp_session_drops_total", **down) == 1
    assert get("bgp_own_prefix_ok", prefix="140.211.167.122/32") == 1
    assert get("bgp_own_prefix_origin_ok", prefix="69.89.123.0/24") == 0
    assert get("bgp_own_prefix_foreign_more_specifics", prefix="69.89.0.0/16") == 1
    assert get("bgp_own_prefix_visible", prefix="198.51.100.0/24") == 0
    assert get("bgp_route_changes", window="1h") > 0
    assert get("bgp_collector_paths_total") == 8231
    assert get("bgp_community_prefixes", community="3701:380", name="Transit") > 0


async def test_page_has_monitoring_cards(client):
    page = (await client.get("/")).text
    assert 'id="sessionTable"' in page and 'id="ownPrefixes"' in page and 'id="unstableTable"' in page
