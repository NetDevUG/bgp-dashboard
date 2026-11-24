"""RPKI route origin validation (RFC 6811) with a Routinator style VRP export."""
import http.server
import json
import threading

import httpx
import pytest
import pytest_asyncio

import gobgp_to_mongo as collector
from conftest import MONGO_URI
from app.rpki import INVALID, NOT_FOUND, VALID, VrpIndex, validate_table

VRPS = {"metadata": {"generated": 1795000000}, "roas": [
    {"asn": "AS64500", "prefix": "192.0.2.0/24", "maxLength": 24, "ta": "ripe"},
    {"asn": "AS64501", "prefix": "198.51.100.0/22", "maxLength": 23, "ta": "ripe"},
    {"asn": 64502, "prefix": "198.51.100.0/22", "maxLength": 24, "ta": "arin"},   # rpki-client style
    {"asn": "AS0", "prefix": "203.0.113.0/24", "maxLength": 32, "ta": "apnic"},     # AS0: never valid
    {"asn": "AS216333", "prefix": "2001:db8::/32", "maxLength": 48, "ta": "ripe"},
    {"asn": "AS64500", "prefix": "not-a-prefix", "maxLength": 24},                  # ignored
]}


@pytest.fixture(scope="module")
def index():
    return VrpIndex.from_json(VRPS)


@pytest.mark.parametrize("prefix,origin,expected", [
    ("192.0.2.0/24", 64500, (VALID, None)),
    ("192.0.2.0/24", 64999, (INVALID, "as")),
    ("192.0.2.128/25", 64500, (INVALID, "length")),          # too specific
    ("198.51.100.0/23", 64501, (VALID, None)),
    ("198.51.101.0/24", 64501, (INVALID, "length")),         # maxLength 23 for AS64501 ...
    ("198.51.101.0/24", 64502, (VALID, None)),               # ... but 24 for AS64502
    ("203.0.113.0/24", 0, (INVALID, "as")),
    ("203.0.113.0/25", 64500, (INVALID, "as")),
    ("10.0.0.0/8", 64500, (NOT_FOUND, None)),
    ("2001:db8:1::/48", 216333, (VALID, None)),
    ("2001:db8:1::/49", 216333, (INVALID, "length")),
    ("2001:db9::/32", 216333, (NOT_FOUND, None)),
    ("192.0.2.0/24", None, (INVALID, "as")),                 # unknown origin can not be valid
])
def test_validation_rules(index, prefix, origin, expected):
    assert index.validate(prefix, origin) == expected


def test_index_parsing_and_covering(index):
    assert index.count == 5
    assert sorted((v["prefix"], v["asn"], v["max_length"]) for v in index.covering("198.51.101.0/24")) == [
        ("198.51.100.0/22", 64501, 23), ("198.51.100.0/22", 64502, 24)]
    assert index.covering("2001:db8:1::/48") == [{"prefix": "2001:db8::/32", "max_length": 48, "asn": 216333}]


def _line(prefix, path):
    return json.dumps([{"nlri": {"prefix": prefix}, "attrs": [
        {"type": 2, "as_paths": [{"segment_type": 2, "asns": path}] if path else []},
        {"type": 3, "nexthop": "192.0.2.254"}]}])


ROUTES = [_line("192.0.2.0/24", [3356, 64500]), _line("192.0.2.128/25", [3356, 64500]),
          _line("198.51.101.0/24", [174, 64502]), _line("10.0.0.0/8", [174, 64500]),
          _line("2001:db8:1::/48", [])]   # empty AS path: originated by AS216333


@pytest.fixture(scope="module")
def rpki_db():
    pymongo = pytest.importorskip("pymongo")
    client = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=1500)
    try:
        client.admin.command("ping")
    except Exception:
        pytest.skip(f"MongoDB not reachable at {MONGO_URI}")
    client.drop_database("bgp_test_rpki")
    db = client["bgp_test_rpki"]
    collector.initialize_database(db)
    collector.process_batch(db, ROUTES)
    yield db
    client.drop_database("bgp_test_rpki")


def test_validate_table(rpki_db, index):
    result = validate_table(rpki_db.bgp, index, own_asn=216333)
    assert result["counts"] == {"valid": {"4": 2, "6": 1}, "invalid": {"4": 1, "6": 0},
                                "not-found": {"4": 1, "6": 0}}
    [inv] = result["invalid"]
    assert (inv["prefix"], inv["origin_asn"], inv["reason"], inv["nexthop_asn"]) == ("192.0.2.128/25", 64500, "length", 3356)
    assert inv["vrps"] == [{"prefix": "192.0.2.0/24", "max_length": 24, "asn": 64500}]


@pytest.fixture(scope="module")
def routinator():
    """Local HTTP server that serves the VRPs like Routinator's /json."""
    body = json.dumps(VRPS).encode()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/json":
                self.send_response(404); self.end_headers(); return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest_asyncio.fixture
async def client(rpki_db, routinator):
    from app.config import Settings
    from app.main import create_app
    settings = Settings(mongo_uri=MONGO_URI, mongo_db="bgp_test_rpki", background_jobs=False, asn=216333,
                        rpki_vrp_url=f"{routinator}/json", own_prefixes=("2001:db8:1::/48", "192.0.2.128/25"))
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async def no_dns(name, rdtype="A"):
            return []
        app.state.dns.resolve = no_dns
        await app.state.rpki.refresh()
        await app.state.stats.update_stats()
        await app.state.stats.update_advanced_stats()
        app.state.stats.ready = True
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http_client:
            yield http_client


async def test_rpki_endpoint(client):
    d = (await client.get("/bgp/api/v1.0/rpki")).json()
    assert d["ok"] and d["error"] is None and d["vrps"] == 5 and d["invalid_total"] == 1
    assert d["counts"]["valid"] == {"4": 2, "6": 1}


async def test_rpki_in_lookup_and_own_prefixes(client):
    r = (await client.get("/bgp/api/v1.0/ip/192.0.2.200")).json()
    assert r["prefix"] == "192.0.2.128/25" and r["rpki"]["state"] == "invalid" and r["rpki"]["reason"] == "length"
    own = {p["prefix"]: p for p in (await client.get("/bgp/api/v1.0/own-prefixes")).json()}
    assert own["2001:db8:1::/48"]["ok"] and own["2001:db8:1::/48"]["rpki"]["state"] == "valid"
    assert "rpki_invalid" in own["192.0.2.128/25"]["problems"]


async def test_rpki_metrics(client):
    text = (await client.get("/metrics")).text
    assert 'bgp_rpki_routes{state="invalid",ip_version="4"} 1' in text
    assert "bgp_rpki_vrps 5" in text and "bgp_rpki_ok 1" in text
    assert 'bgp_own_prefix_rpki_valid{prefix="2001:db8:1::/48"} 1' in text


async def test_rpki_unreachable_keeps_running(rpki_db):
    from app.config import Settings
    from app.dns_cache import DnsCache
    from app.rpki import RpkiService
    service = RpkiService(Settings(rpki_vrp_url="http://127.0.0.1:9/json", mongo_uri=MONGO_URI), DnsCache(None))
    await service.refresh()
    assert service.data["ok"] is False and "ConnectError" in service.data["error"] and service.index is None
