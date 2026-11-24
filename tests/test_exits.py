"""Exit analysis (location x type, peers by location, transit although peered)."""
import json

import httpx
import pytest
import pytest_asyncio

import gobgp_to_mongo as collector
from conftest import MONGO_URI

DB_NAME = "bgp_test_exits"
TRANSIT, PEER, OWN = (216333, 100, 100), (216333, 100, 120), (216333, 100, 130)
MEP, AMS = (216333, 105, 1), (216333, 105, 2)


def _line(prefix, path, *communities):
    lcs = [{"ASN": a, "LocalData1": b, "LocalData2": c} for a, b, c in communities]
    return json.dumps([{"nlri": {"prefix": prefix}, "attrs": [
        {"type": 2, "as_paths": [{"segment_type": 2, "asns": path}] if path else []},
        {"type": 3, "nexthop": "192.0.2.1"}, {"type": 32, "value": lcs}]}])


ROUTES = [
    _line("198.51.100.0/24", [174, 64501], TRANSIT, MEP),
    _line("198.51.101.0/24", [1299, 13335], TRANSIT, AMS),       # Cloudflare via transit
    _line("198.51.102.0/24", [1299, 13335], TRANSIT, AMS),       # Cloudflare via transit
    _line("198.51.103.0/24", [1299, 64502], TRANSIT, AMS),
    _line("1.1.1.0/24", [13335], PEER, (216333, 101, 13335), MEP),   # Cloudflare via peering in Meppel
    _line("203.0.113.0/24", [6939, 64503], PEER, (216333, 101, 6777), AMS),
    _line("2001:db8:1::/48", [6939], PEER, (216333, 101, 6777), AMS),
    _line("192.0.2.0/24", [], OWN, MEP),
    _line("100.64.0.0/24", [64999]),                                    # no communities at all
]


@pytest.fixture(scope="module")
def exits_db():
    pymongo = pytest.importorskip("pymongo")
    client = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=1500)
    try:
        client.admin.command("ping")
    except Exception:
        pytest.skip(f"MongoDB not reachable at {MONGO_URI}")
    client.drop_database(DB_NAME)
    db = client[DB_NAME]
    collector.initialize_database(db)
    collector.process_batch(db, ROUTES)
    yield db
    client.drop_database(DB_NAME)


async def _client(settings):
    from app.main import create_app
    app = create_app(settings)
    ctx = app.router.lifespan_context(app)
    await ctx.__aenter__()

    async def no_dns(name, rdtype="A"):
        return []
    app.state.dns.resolve = no_dns
    await app.state.stats.update_stats()
    await app.state.stats.update_advanced_stats()
    app.state.stats.ready = True
    return ctx, httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest_asyncio.fixture
async def client(exits_db):
    from app.config import Settings
    settings = Settings(
        mongo_uri=MONGO_URI, mongo_db=DB_NAME, background_jobs=False, asn=216333,
        transit_community="216333:100:100", peer_community_prefix="216333:101:",
        location_community_prefix="216333:105:", peer_learned_community="216333:100:120",
        originated_community="216333:100:130",
        community_names={"216333:105:1": "Location Meppel, NL", "216333:105:2": "Location Amsterdam, NL",
                         "216333:101:6777": "AMS-IX", "216333:101:13335": "Cloudflare"})
    ctx, http = await _client(settings)
    async with http:
        yield http
    await ctx.__aexit__(None, None, None)


async def test_exit_matrix(client):
    ex = (await client.get("/bgp/api/v1.0/exits")).json()
    assert ex["configured"] is True
    locs = {l["name"]: l for l in ex["locations"]}
    ams, mep, unknown = locs["Location Amsterdam, NL"], locs["Location Meppel, NL"], locs["Unknown location"]
    assert (ams["transit"], ams["peering"], ams["total"]) == ({"4": 3, "6": 0}, {"4": 1, "6": 1}, 5)
    assert (mep["transit"]["4"], mep["peering"]["4"], mep["originated"]["4"], mep["total"]) == (1, 1, 1, 3)
    assert unknown["other"]["4"] == 1
    assert ex["locations"][-1]["location"] is None            # unknown location sorted last


async def test_peers_by_location(client):
    via = {v["community"]: v for v in (await client.get("/bgp/api/v1.0/exits")).json()["via"]}
    assert via["216333:101:6777"]["name"] == "AMS-IX"
    assert via["216333:101:6777"]["locations"] == {"216333:105:2": 2} and via["216333:101:6777"]["total"] == 2
    assert via["216333:101:13335"]["locations"] == {"216333:105:1": 1}


async def test_transit_instead_of_peering(client):
    tip = (await client.get("/bgp/api/v1.0/exits")).json()["transit_instead_of_peering"]
    assert tip["available"] and tip["total_prefixes"] == 2
    [cf] = tip["asns"]
    assert cf["asn"] == 13335 and cf["prefixes_via_transit"] == 2 and cf["prefixes_via_peering"] == 1
    assert cf["peering_locations"] == ["Location Meppel, NL"]
    assert cf["transit_locations"] == ["Location Amsterdam, NL"]
    assert cf["samples"] == ["198.51.101.0/24", "198.51.102.0/24"]


async def test_exit_metrics(client):
    text = (await client.get("/metrics")).text
    assert 'bgp_exit_prefixes{location="Location Amsterdam, NL",type="transit",ip_version="4"} 3' in text
    assert "bgp_transit_instead_of_peering_prefixes 2" in text
    assert 'bgp_peer_community_prefixes{community="216333:101:6777",name="AMS-IX"} 2' in text


async def test_not_configured(exits_db):
    from app.config import Settings
    ctx, http = await _client(Settings(mongo_uri=MONGO_URI, mongo_db=DB_NAME, background_jobs=False))
    async with http:
        assert (await http.get("/bgp/api/v1.0/exits")).json() == {"configured": False}
        assert "bgp_exit_prefixes" not in (await http.get("/metrics")).text
    await ctx.__aexit__(None, None, None)


async def test_own_prefix_tiles_without_customers(client):
    s = (await client.get("/bgp/api/v1.0/stats")).json()
    assert s["customers_enabled"] is False and s["customer_count"] == 0
    assert (s["own_ipv4_prefixes"], s["own_ipv6_prefixes"]) == (1, 0)   # tagged 216333:100:130
    metrics = (await client.get("/metrics")).text
    assert 'bgp_own_prefixes{ip_version="4"} 1' in metrics and "bgp_customers" not in metrics
    page = (await client.get("/")).text
    assert '"customersEnabled": false' in page


async def test_customers_enabled(exits_db):
    from app.config import Settings
    ctx, http = await _client(Settings(mongo_uri=MONGO_URI, mongo_db=DB_NAME, background_jobs=False,
                                       customer_community="216333:100:120"))
    async with http:
        s = (await http.get("/bgp/api/v1.0/stats")).json()
        assert s["customers_enabled"] is True
        assert sorted(c["asn"] for c in s["customers"]) == [6939, 13335]
        assert s["own_ipv4_prefixes"] == 1      # no originated community configured: empty AS path
        assert "bgp_customers 2" in (await http.get("/metrics")).text
    await ctx.__aexit__(None, None, None)
