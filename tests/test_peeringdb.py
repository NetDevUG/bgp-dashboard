"""Peering suggestions with a local fake PeeringDB (same JSON format as the real API)."""
import http.server
import json
import threading
import urllib.parse

import httpx
import pytest
import pytest_asyncio

import gobgp_to_mongo as collector
from conftest import MONGO_URI

DB_NAME = "bgp_test_peeringdb"
TRANSIT, PEER = (216333, 100, 100), (216333, 100, 120)
NETS = {
    3320: {"asn": 3320, "name": "Deutsche Telekom", "policy_general": "Restrictive", "info_type": "NSP"},
    6453: {"asn": 6453, "name": "TATA", "policy_general": "Selective", "info_type": "NSP"},
    64700: {"asn": 64700, "name": "private"},          # private ASN: never a candidate
    65000: {"asn": 65000, "name": "x"},
    8075: {"asn": 8075, "name": "Microsoft", "policy_general": "Selective", "info_type": "Content"},
}
NETIXLAN = [
    {"asn": 216333, "ix_id": 26, "name": "AMS-IX", "speed": 10000, "is_rs_peer": True, "operational": True},
    {"asn": 216333, "ix_id": 359, "name": "FrysIX", "speed": 10000, "is_rs_peer": True, "operational": True},
    {"asn": 3320, "ix_id": 26, "name": "AMS-IX", "speed": 400000, "is_rs_peer": False, "operational": True},
    {"asn": 6453, "ix_id": 999, "name": "Other-IX", "speed": 100000, "is_rs_peer": True, "operational": True},
    {"asn": 8075, "ix_id": 359, "name": "FrysIX", "speed": 100000, "is_rs_peer": True, "operational": True},
    {"asn": 8075, "ix_id": 26, "name": "AMS-IX", "speed": 100000, "is_rs_peer": False, "operational": False},
]
REQUESTS = []


def _line(prefix, path, *communities):
    lcs = [{"ASN": a, "LocalData1": b, "LocalData2": c} for a, b, c in communities]
    return json.dumps([{"nlri": {"prefix": prefix}, "attrs": [
        {"type": 2, "as_paths": [{"segment_type": 2, "asns": path}]}, {"type": 3, "nexthop": "192.0.2.1"},
        {"type": 32, "value": lcs}]}])


ROUTES = (
    [_line(f"10.0.{i}.0/24", [174, 3320, 64600 + i], TRANSIT) for i in range(5)]          # 5 via Telekom
    + [_line(f"10.1.{i}.0/24", [174, 6453, 4134], TRANSIT) for i in range(3)]             # 3 via TATA
    + [_line("10.2.0.0/24", [174, 8075], TRANSIT)]                                         # 1 Microsoft
    + [_line("10.3.0.0/24", [174, 13335], TRANSIT), _line("1.1.1.0/24", [13335], PEER)]   # 13335 is a peer
    + [_line("10.4.0.0/24", [174, 64700, 1], TRANSIT)]                                     # private ASN
)


@pytest.fixture(scope="module")
def fake_peeringdb():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            q = urllib.parse.parse_qs(url.query)
            REQUESTS.append((url.path, q, self.headers.get("Authorization")))
            asns = {int(a) for a in (q.get("asn__in", [""])[0].split(",") + q.get("asn", [])) if a}
            if url.path == "/api/net":
                data = [n for a, n in NETS.items() if a in asns]
            elif url.path == "/api/netixlan":
                data = [n for n in NETIXLAN if n["asn"] in asns]
            else:
                self.send_response(404); self.end_headers(); return
            body = json.dumps({"meta": {}, "data": data}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/api"
    server.shutdown()


@pytest.fixture(scope="module")
def pdb_db():
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


def _settings(url, **kw):
    from app.config import Settings
    return Settings(mongo_uri=MONGO_URI, mongo_db=DB_NAME, background_jobs=False, asn=216333,
                    transit_community="216333:100:100", peer_learned_community="216333:100:120",
                    peeringdb_enabled=True, peeringdb_url=url, **kw)


@pytest_asyncio.fixture
async def app_client(pdb_db, fake_peeringdb):
    from app.main import create_app
    app = create_app(_settings(fake_peeringdb, peeringdb_api_key="k123"))
    async with app.router.lifespan_context(app):
        async def no_dns(name, rdtype="A"):
            return []
        app.state.dns.resolve = no_dns
        await app.state.peeringdb.refresh()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http_client:
            yield app, http_client


async def test_candidates(app_client):
    app, _ = app_client
    cands = await app.state.peeringdb.candidates()
    assert [(c["asn"], c["prefixes_via_transit"]) for c in cands] == [(3320, 5), (6453, 3), (8075, 1)]


async def test_suggestions(app_client):
    _, http_client = app_client
    d = (await http_client.get("/bgp/api/v1.0/peering-suggestions")).json()
    assert d["ok"] and d["ranked_by"] == "prefixes"
    assert [ix["name"] for ix in d["own_ixes"]] == ["AMS-IX", "FrysIX"]
    sug = {s["asn"]: s for s in d["suggestions"]}
    assert set(sug) == {3320, 8075}                                  # TATA only at another IX
    assert sug[3320]["policy"] == "Restrictive" and sug[3320]["common_ixes"][0]["name"] == "AMS-IX"
    assert [ix["name"] for ix in sug[8075]["common_ixes"]] == ["FrysIX"]   # AMS-IX not operational
    assert sug[8075]["common_ixes"][0]["route_server"] is True
    assert d["no_common_ix"] == 1 and d["not_in_peeringdb"] == 0
    assert all(auth == "Api-Key k123" for _, _, auth in REQUESTS)


async def test_ranked_by_traffic_and_cache(pdb_db, fake_peeringdb):
    from pymongo import AsyncMongoClient
    from app.dns_cache import DnsCache
    from app.peeringdb import PeeringDBService

    class FakeAkvorado:
        enabled = True
        def traffic(self):
            return {4134: 9e9, 64601: 1e6}                   # China Telecom behind TATA: lots of traffic

    db = AsyncMongoClient(MONGO_URI)[DB_NAME]
    svc = PeeringDBService(db, _settings(fake_peeringdb), DnsCache(None), akvorado=FakeAkvorado())
    cands = await svc.candidates()
    assert cands[0]["asn"] == 6453 and cands[0]["traffic_bps"] == 9e9
    await svc.refresh()
    assert svc.data["ranked_by"] == "traffic"
    fresh = PeeringDBService(db, _settings(fake_peeringdb), DnsCache(None))
    age = await fresh.load_cache()
    assert age is not None and age < 60 and fresh.data["suggestions"] == svc.data["suggestions"]


async def test_unreachable(pdb_db):
    from pymongo import AsyncMongoClient
    from app.dns_cache import DnsCache
    from app.peeringdb import PeeringDBService
    svc = PeeringDBService(AsyncMongoClient(MONGO_URI)[DB_NAME], _settings("http://127.0.0.1:9/api"), DnsCache(None))
    await svc.refresh()
    assert svc.data["ok"] is False and "ConnectError" in svc.data["error"]


async def test_metrics(app_client):
    _, http_client = app_client
    text = (await http_client.get("/metrics")).text
    assert "bgp_peeringdb_ok 1" in text and "bgp_peering_suggestions 2" in text
