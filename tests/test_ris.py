"""External view: RIS Live (fake WebSocket server) and RIPEstat routing-status (fake HTTP)."""
import asyncio
import http.server
import json
import threading

import httpx
import pytest
import pytest_asyncio
import websockets

from conftest import MONGO_URI

DB_NAME = "bgp_test_ris"
OWN = ("193.0.0.0/21", "2001:67c:2e8::/48")
SUBSCRIPTIONS = []


def ris_message(peer, path, announce=(), withdraw=(), ts=1795000000.0):
    return json.dumps({"type": "ris_message", "data": {
        "timestamp": ts, "peer": peer, "peer_asn": "64999", "id": "x", "host": "rrc00", "type": "UPDATE",
        "path": path, "origin": "IGP",
        "announcements": [{"next_hop": "192.0.2.1", "prefixes": list(announce)}] if announce else [],
        "withdrawals": list(withdraw)}})


async def ris_handler(ws):
    for _ in OWN:
        SUBSCRIPTIONS.append(json.loads(await ws.recv()))
    await ws.send(json.dumps({"type": "ris_subscribe_ok", "data": {}}))
    await ws.send(ris_message("10.0.0.1", [64999, 1299, 216333], announce=["193.0.0.0/21"]))
    await ws.send(ris_message("10.0.0.2", [64998, 3356, [216333, 64666]], announce=["193.0.0.0/21"]))  # AS_SET
    await ws.send(ris_message("10.0.0.3", [64997, 174, 64666], announce=["193.0.4.0/24"]))            # hijack
    await ws.send(ris_message("10.0.0.4", [64996, 174, 64666], announce=["193.0.4.0/24"]))
    await ws.send(ris_message("10.0.0.5", [64995, 6939, 15169], announce=["8.8.8.0/24"]))             # not ours
    await ws.send(json.dumps({"type": "ris_error", "data": {"message": "test error"}}))
    await asyncio.sleep(3600)


@pytest.fixture(scope="module")
def ripestat():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"status": "ok", "data": {
                "visibility": {"v4": {"ris_peers_seeing": 300, "total_ris_peers": 320},
                               "v6": {"ris_peers_seeing": 200, "total_ris_peers": 330}},
                "origins": [{"origin": 216333, "route_objects": ["RIPE"]}, {"origin": 64777}]}}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/data"
    server.shutdown()


@pytest_asyncio.fixture
async def ris_env(ripestat):
    pymongo = pytest.importorskip("pymongo")
    sync = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=1500)
    try:
        sync.admin.command("ping")
    except Exception:
        pytest.skip(f"MongoDB not reachable at {MONGO_URI}")
    sync.drop_database(DB_NAME)
    server = await websockets.serve(ris_handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    from app.config import Settings
    settings = Settings(mongo_uri=MONGO_URI, mongo_db=DB_NAME, background_jobs=False, asn=216333,
                        own_prefixes=OWN, ris_live_enabled=True, ris_live_url=f"ws://127.0.0.1:{port}/v1/ws/",
                        ripestat_url=ripestat)
    yield settings
    server.close()
    sync.drop_database(DB_NAME)


async def test_ris_live_flow(ris_env):
    from pymongo import AsyncMongoClient
    from app.ris import RisLiveService
    svc = RisLiveService(AsyncMongoClient(MONGO_URI)[DB_NAME], ris_env)
    task = asyncio.create_task(svc.run())
    for _ in range(100):
        if svc.error == "test error" and svc.visibility.get("2001:67c:2e8::/48"):
            break
        await asyncio.sleep(0.05)
    assert [s["data"]["prefix"] for s in SUBSCRIPTIONS] == list(OWN)
    assert all(s["type"] == "ris_subscribe" and s["data"]["moreSpecific"] for s in SUBSCRIPTIONS)
    summary = svc.summary()
    assert summary["connected"]
    v4 = summary["prefixes"][0]
    assert v4["ris_peers_announcing"] == 2 and v4["origins_seen"] == [64666, 216333]
    foreign = {(f["prefix"], f["origin_asn"]): f for f in v4["foreign"]}
    assert set(foreign) == {("193.0.0.0/21", 64666), ("193.0.4.0/24", 64666)}
    assert foreign[("193.0.4.0/24", 64666)]["peers"] == 2 and foreign[("193.0.4.0/24", 64666)]["more_specific"]
    assert summary["prefixes"][0]["visibility"]["ratio"] == pytest.approx(300 / 320, abs=0.001)
    assert summary["prefixes"][1]["visibility"]["ratio"] == pytest.approx(200 / 330, abs=0.001)
    assert summary["prefixes"][0]["visibility"]["foreign_origins"] == [64777]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task



async def test_withdrawals_and_events(ris_env):
    from pymongo import AsyncMongoClient
    from app.ris import RisLiveService
    svc = RisLiveService(AsyncMongoClient(MONGO_URI)[DB_NAME], ris_env)
    hijack = json.loads(ris_message("10.0.0.3", [64997, 174, 64666], announce=["193.0.4.0/24"]))
    await svc.handle(hijack)
    await svc.handle(json.loads(ris_message("10.0.0.4", [64996, 174, 64666], announce=["193.0.4.0/24"])))
    await svc.handle(json.loads(ris_message("10.0.0.3", [], withdraw=["193.0.4.0/24"])))
    assert svc.foreign_announcements()[0]["peers"] == 1
    await svc.handle(json.loads(ris_message("10.0.0.4", [], withdraw=["193.0.4.0/24"])))
    assert svc.foreign_announcements() == []
    events = await svc.events()
    assert [(e["type"], e["prefix"], e["origin_asn"], e["more_specific"]) for e in reversed(events)] == [
        ("foreign_announcement", "193.0.4.0/24", 64666, True),
        ("foreign_announcement_gone", "193.0.4.0/24", 64666, True)]
    # an allowed origin is not reported
    await svc.handle(json.loads(ris_message("10.0.0.9", [1, 2, 216333], announce=["193.0.6.0/24"])))
    assert svc.foreign_announcements() == []


async def test_api_and_metrics(ris_env):
    from app.main import create_app
    app = create_app(ris_env)
    async with app.router.lifespan_context(app):
        await app.state.ris.handle(json.loads(ris_message("10.0.0.3", [64997, 174, 64666], announce=["193.0.4.0/24"])))
        await app.state.ris.refresh_visibility()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            ext = (await c.get("/bgp/api/v1.0/external")).json()
            assert ext["enabled"] and ext["foreign_total"] == 1
            text = (await c.get("/metrics")).text
            assert 'bgp_external_foreign_origins{prefix="193.0.0.0/21"} 2' in text   # stream + RIPEstat
            assert 'bgp_external_visibility_ratio{prefix="193.0.0.0/21"} 0.938' in text
            assert "bgp_ris_live_connected 0" in text
            assert len((await c.get("/bgp/api/v1.0/external/events")).json()) == 1


def test_disabled_without_own_prefixes():
    from app.config import Settings
    from app.ris import RisLiveService
    assert RisLiveService(None, Settings(ris_live_enabled=True)).enabled is False
