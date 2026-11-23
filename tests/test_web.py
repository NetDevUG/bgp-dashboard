"""API tests against a real MongoDB filled with the sample data."""
import asyncio

import httpx
import pytest
import pytest_asyncio

import gobgp_to_mongo as collector
from conftest import MONGO_URI, SAMPLE

DB_NAME = "bgp_test_web"


@pytest.fixture(scope="module")
def sample_db():
    pymongo = pytest.importorskip("pymongo")
    client = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=1500)
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
    yield db
    client.drop_database(DB_NAME)


@pytest_asyncio.fixture
async def client(sample_db):
    from app.config import Settings
    from app.dns_cache import DnsCache
    from app.main import create_app

    settings = Settings(mongo_uri=MONGO_URI, mongo_db=DB_NAME, background_jobs=False,
                        community_names={"3701:380": "Transit"})
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        # no DNS in tests: answer everything from a stub
        async def no_dns(name, rdtype="A"):
            return []
        app.state.dns.resolve = no_dns
        await app.state.stats.update_stats()
        await app.state.stats.update_advanced_stats()
        await app.state.stats.record_history()
        app.state.stats.ready = True
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield http


async def test_stats(client, sample_db):
    s = (await client.get("/bgp/api/v1.0/stats")).json()
    assert s["ready"] is True
    assert s["ipv4_table_size"] == sample_db.bgp.count_documents({"ip_version": 4, "active": True})
    assert s["ipv6_table_size"] == sample_db.bgp.count_documents({"ip_version": 6, "active": True})
    assert s["peer_count"] == len(sample_db.bgp.distinct("nexthop_asn", {"active": True}))
    assert sum(c["count"] for c in s["cidr_breakdown"]) == s["ipv4_table_size"] + s["ipv6_table_size"]
    assert 1 < s["avg_as_path_length"] < 10
    assert len(s["top_n_peers"]) == 5
    assert s["top_n_peers"][0]["count"] >= s["top_n_peers"][-1]["count"]


async def test_peers_match_direct_queries(client, sample_db):
    peers = (await client.get("/bgp/api/v1.0/peers")).json()
    p = next(p for p in peers if p["asn"] == 3356)
    q = {"nexthop_asn": 3356, "active": True}
    assert p["ipv4_nexthop_count"] == sample_db.bgp.count_documents({**q, "ip_version": 4})
    assert p["ipv6_nexthop_count"] == sample_db.bgp.count_documents({**q, "ip_version": 6})
    assert p["asn_count"] == len(sample_db.bgp.distinct("as_path.1", q))


async def test_communities(client, sample_db):
    comms = (await client.get("/bgp/api/v1.0/communities")).json()
    transit = next(c for c in comms if c["community"] == "3701:380")
    assert transit["name"] == "Transit"
    assert transit["count"] == sample_db.bgp.count_documents({"communities": "3701:380", "active": True})


async def test_ip_longest_prefix_match(client, sample_db):
    r = (await client.get("/bgp/api/v1.0/ip/69.89.123.77")).json()
    assert r["prefix"] == "69.89.123.0/24"
    assert r["history"].endswith("/bgp/api/v1.0/ip/69.89.123.77/history")
    r6 = (await client.get("/bgp/api/v1.0/ip/2620:101:80fb::1")).json()
    assert r6["prefix"] == "2620:101:80fb::/48" and r6["ip_version"] == 6


async def test_ip_history_and_errors(client):
    r = (await client.get("/bgp/api/v1.0/ip/69.89.123.77/history")).json()
    assert isinstance(r["history"], list)
    assert (await client.get("/bgp/api/v1.0/ip/10.255.255.1")).json() == {}
    bad = await client.get("/bgp/api/v1.0/ip/not-a-host.invalid")
    assert bad.status_code == 400 and "error" in bad.json()


async def test_asn_endpoints(client, sample_db):
    r = (await client.get("/bgp/api/v1.0/asn/14103")).json()
    assert r["origin_prefix_count"] == 2 and r["is_peer"] is False
    down = (await client.get("/bgp/api/v1.0/asn/3356/downstream")).json()
    assert down["downstream_asns_count"] == len(down["downstream_asns"]) > 0
    nh = (await client.get("/bgp/api/v1.0/asn/3356/nexthop/ipv6")).json()
    assert nh["nexthop_prefix_count"] == sample_db.bgp.count_documents(
        {"nexthop_asn": 3356, "ip_version": 6, "active": True})
    tr = (await client.get("/bgp/api/v1.0/asn/6762/transit")).json()
    assert tr["transit_prefix_count"] == sample_db.bgp.count_documents({"as_path": 6762, "active": True})
    orig = (await client.get("/bgp/api/v1.0/asn/14103/originated")).json()
    assert orig["originated_prefix_count"] == 2


async def test_stats_history_and_pages(client):
    hist = (await client.get("/bgp/api/v1.0/stats/history?hours=1")).json()
    assert hist and "ipv4_table_size" in hist[-1] and "ts" in hist[-1]
    page = await client.get("/")
    assert page.status_code == 200 and "AS3701" in page.text
    assert (await client.get("/static/dashboard.js")).status_code == 200
    health = (await client.get("/healthz")).json()
    assert health["mongo"] == "ok"
