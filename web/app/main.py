"""BGP Dashboard web application (FastAPI)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pymongo import AsyncMongoClient

from .config import Settings, load_settings
from .dns_cache import DnsCache
from .lookups import InvalidQuery, Lookups
from . import monitoring
from .metrics import render_metrics
from .akvorado import AkvoradoService
from .peeringdb import PeeringDBService
from .rpki import RpkiService
from .stats import StatsService

log = logging.getLogger("bgp_dashboard")
BASE_DIR = Path(__file__).resolve().parent
API = "/bgp/api/v1.0"


class PrettyJSONResponse(JSONResponse):
    """Indented JSON, so the API is readable in the browser."""

    def render(self, content: Any) -> bytes:
        return json.dumps(content, ensure_ascii=False, indent=2, default=str).encode("utf-8")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        client = AsyncMongoClient(settings.mongo_uri, tz_aware=True,
                                  serverSelectionTimeoutMS=5000, appname="bgp-dashboard")
        db = client[settings.mongo_db]
        dns = DnsCache(db, timeout=settings.dns_timeout)
        rpki = RpkiService(settings, dns)
        akvorado = AkvoradoService(settings)
        stats = StatsService(db, settings, dns, rpki=rpki, akvorado=akvorado)
        app.state.client, app.state.db = client, db
        app.state.dns, app.state.stats, app.state.rpki = dns, stats, rpki
        app.state.akvorado = akvorado
        peeringdb = PeeringDBService(db, settings, dns, akvorado=akvorado)
        app.state.peeringdb = peeringdb
        app.state.lookups = Lookups(db, settings, dns, rpki=rpki)

        tasks = []
        if settings.background_jobs:
            with contextlib.suppress(Exception):
                await dns.ensure_indexes()
                await stats.ensure_indexes()
            if rpki.enabled:
                tasks.append(asyncio.create_task(rpki.run(), name="rpki-refresh"))
            if akvorado.enabled:
                tasks.append(asyncio.create_task(akvorado.run(), name="akvorado-refresh"))
            tasks.append(asyncio.create_task(stats.run(), name="stats-refresh"))
            if peeringdb.enabled:
                tasks.append(asyncio.create_task(peeringdb.run(stats), name="peeringdb-refresh"))
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await client.close()

    app = FastAPI(
        title="BGP Dashboard",
        description="Realtime view of the BGP table learned by GoBGP.",
        version="2.0",
        lifespan=lifespan,
        default_response_class=PrettyJSONResponse,
    )
    app.state.settings = settings
    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
    templates = Jinja2Templates(directory=BASE_DIR / "templates")

    @app.exception_handler(InvalidQuery)
    async def invalid_query(_: Request, exc: InvalidQuery):
        return PrettyJSONResponse({"error": str(exc)}, status_code=400)

    def lk(request: Request) -> Lookups:
        return request.app.state.lookups

    def st(request: Request) -> StatsService:
        return request.app.state.stats

    def version_number(version: str) -> int:
        return 6 if version.lower() == "ipv6" else 4

    async def is_peer(request: Request, asn: int) -> bool:
        stats = st(request)
        if stats.ready:
            return asn in stats.peer_asns
        return asn in await request.app.state.db["bgp"].distinct("nexthop_asn", {"active": True})

    # ------------------------------------------------------------------ #
    # Pages
    # ------------------------------------------------------------------ #
    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index(request: Request):
        name = await request.app.state.dns.asn_name(settings.asn)
        return templates.TemplateResponse(request, "index.html", {
            "source_asn": settings.asn,
            "source_asn_name": name,
            "config": {
                "asn": settings.asn,
                "customerCommunity": settings.customer_community,
                "customersEnabled": bool(settings.customer_community),
                "transitCommunity": settings.transit_community,
                "peerCommunityPrefix": settings.peer_community_prefix,
                "statsInterval": settings.stats_interval,
                "advancedInterval": settings.advanced_stats_interval,
                "historyDays": settings.stats_history_days,
                "ownPrefixesConfigured": len(settings.own_prefixes),
                "exitsConfigured": bool(settings.location_community_prefix or settings.peer_learned_community),
                "rpkiEnabled": bool(settings.rpki_vrp_url),
                "akvoradoEnabled": bool(settings.akvorado_clickhouse_url),
                "peeringdbEnabled": settings.peeringdb_enabled,
            },
        })

    @app.get("/healthz", include_in_schema=False)
    async def healthz(request: Request):
        try:
            await request.app.state.client.admin.command("ping")
            mongo = "ok"
        except Exception as err:  # noqa: BLE001
            mongo = f"error: {err}"
        stats = st(request)
        collector = {}
        if mongo == "ok":
            collector = await monitoring.collector_status(request.app.state.db)
        body = {"status": "ok" if mongo == "ok" else "degraded", "mongo": mongo,
                "stats_ready": stats.ready, "stats_error": stats.last_error,
                "collector_heartbeat": monitoring._iso(collector.get("heartbeat")),
                "collector_last_update": monitoring._iso(collector.get("last_update"))}
        return PrettyJSONResponse(body, status_code=200 if mongo == "ok" else 503)

    # ------------------------------------------------------------------ #
    # Statistics (served from the cache)
    # ------------------------------------------------------------------ #
    @app.get(f"{API}/stats", tags=["statistics"])
    async def get_stats(request: Request):
        return st(request).get_data()

    @app.get(f"{API}/stats/history", tags=["statistics"])
    async def get_stats_history(request: Request,
                                hours: float = Query(24, gt=0, le=24 * 366),
                                points: int = Query(300, ge=2, le=5000)):
        return await st(request).history(hours=hours, points=points)

    @app.get(f"{API}/peers", tags=["statistics"])
    async def get_peers(request: Request):
        return st(request).data["peers"]

    @app.get(f"{API}/customers", tags=["statistics"])
    async def get_customers(request: Request):
        return st(request).data["customers"]

    @app.get(f"{API}/communities", tags=["statistics"])
    async def get_communities(request: Request):
        return st(request).data["communities"]

    # ------------------------------------------------------------------ #
    # Monitoring
    # ------------------------------------------------------------------ #
    @app.get(f"{API}/neighbors", tags=["monitoring"])
    async def get_neighbors(request: Request):
        """BGP sessions of gobgpd (polled by the collector every 10s)."""
        return await monitoring.neighbors(request.app.state.db, settings, request.app.state.dns)

    @app.get(f"{API}/neighbors/events", tags=["monitoring"])
    async def get_neighbor_events(request: Request,
                                  hours: float = Query(168, gt=0, le=24 * 366),
                                  limit: int = Query(200, ge=1, le=5000)):
        """BGP session state changes, newest first."""
        return await monitoring.neighbor_events(request.app.state.db, hours, limit)

    @app.get(f"{API}/own-prefixes", tags=["monitoring"])
    async def get_own_prefixes(request: Request):
        """Check result for the own prefixes configured in dashboard.toml."""
        return st(request).data["own_prefixes"]

    @app.get(f"{API}/unstable", tags=["monitoring"])
    async def get_unstable(request: Request):
        """Prefixes with the most route changes in the configured window."""
        return st(request).data["unstable"]

    @app.get(f"{API}/exits", tags=["monitoring"])
    async def get_exits(request: Request):
        """Where (location) and how (transit / peering / own) prefixes leave the network."""
        return st(request).data["exits"]

    @app.get(f"{API}/rpki", tags=["monitoring"])
    async def get_rpki(request: Request):
        """RPKI route origin validation of all active routes (Routinator VRPs)."""
        return request.app.state.rpki.data

    @app.get(f"{API}/peering-suggestions", tags=["monitoring"])
    async def get_peering_suggestions(request: Request):
        """Networks behind your transits that share an internet exchange with you (PeeringDB)."""
        return request.app.state.peeringdb.data

    @app.get(f"{API}/akvorado", tags=["monitoring"])
    async def get_akvorado(request: Request):
        """Status of the Akvorado traffic data (top destination ASNs by outgoing traffic)."""
        return request.app.state.akvorado.data

    @app.get("/metrics", response_class=PlainTextResponse, tags=["monitoring"])
    async def metrics(request: Request):
        """Prometheus metrics."""
        db = request.app.state.db
        stats = st(request)
        try:
            sessions = await monitoring.neighbors(db, settings, request.app.state.dns)
            collector = await monitoring.collector_status(db)
        except Exception:  # noqa: BLE001 - still export the cached statistics
            sessions, collector = {}, {}
        akv = request.app.state.akvorado
        extra = []
        if akv.enabled:
            extra = [("bgp_akvorado_ok", akv.data["ok"], None, "1 if the last Akvorado query succeeded"),
                     ("bgp_akvorado_outgoing_bps", float(akv.data["total_bps"]), None,
                      "Average outgoing external traffic in the Akvorado window")]
        pdb = request.app.state.peeringdb
        if pdb.enabled:
            extra += [("bgp_peeringdb_ok", pdb.data["ok"], None, "1 if the last PeeringDB refresh succeeded"),
                      ("bgp_peering_suggestions", len(pdb.data["suggestions"]), None,
                       "Networks behind the transits that share an internet exchange with you")]
        text = render_metrics(settings, stats.data, stats.ready, stats.last_error, sessions, collector,
                              rpki=request.app.state.rpki.data, extra=extra)
        return PlainTextResponse(text, media_type="text/plain; version=0.0.4; charset=utf-8")

    # ------------------------------------------------------------------ #
    # Looking glass
    # ------------------------------------------------------------------ #
    @app.get(f"{API}/ip/{{ip}}", tags=["looking glass"])
    async def get_ip(request: Request, ip: str):
        history_url = str(request.url.replace(query="")) + "/history"
        return await lk(request).ip_info(ip, history_url=history_url)

    @app.get(f"{API}/ip/{{ip}}/history", tags=["looking glass"])
    async def get_ip_history(request: Request, ip: str):
        return await lk(request).ip_info(ip)

    @app.get(f"{API}/asn/{{asn}}", tags=["looking glass"])
    async def get_asn(request: Request, asn: int):
        return await lk(request).asn_info(asn, is_peer=await is_peer(request, asn))

    @app.get(f"{API}/asn/{{asn}}/downstream", tags=["looking glass"])
    async def get_downstream(request: Request, asn: int):
        return await lk(request).downstream(asn)

    async def prefix_list(request: Request, asn: int, kind: str, prefixes: list[str]) -> dict:
        return {"asn": asn,
                "name": await request.app.state.dns.asn_name(asn),
                f"{kind}_prefix_count": len(prefixes),
                f"{kind}_prefix_list": prefixes}

    @app.get(f"{API}/asn/{{asn}}/originated", tags=["looking glass"])
    async def get_originated(request: Request, asn: int):
        return await prefix_list(request, asn, "originated", await lk(request).originated(asn))

    @app.get(f"{API}/asn/{{asn}}/originated/{{version}}", tags=["looking glass"])
    async def get_originated_version(request: Request, asn: int, version: Literal["ipv4", "ipv6", "IPv4", "IPv6"]):
        prefixes = await lk(request).originated(asn, version_number(version))
        return await prefix_list(request, asn, "originated", prefixes)

    @app.get(f"{API}/asn/{{asn}}/nexthop", tags=["looking glass"])
    async def get_nexthop(request: Request, asn: int):
        return await prefix_list(request, asn, "nexthop", await lk(request).nexthop(asn))

    @app.get(f"{API}/asn/{{asn}}/nexthop/{{version}}", tags=["looking glass"])
    async def get_nexthop_version(request: Request, asn: int, version: Literal["ipv4", "ipv6", "IPv4", "IPv6"]):
        prefixes = await lk(request).nexthop(asn, version_number(version))
        return await prefix_list(request, asn, "nexthop", prefixes)

    @app.get(f"{API}/asn/{{asn}}/transit", tags=["looking glass"])
    async def get_transit(request: Request, asn: int):
        return await prefix_list(request, asn, "transit", await lk(request).transit(asn))

    @app.get(f"{API}/domain/{{domain}}", tags=["looking glass"])
    async def get_domain(request: Request, domain: str):
        return await lk(request).domain(domain)

    return app


logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
app = create_app() if os.environ.get("BGP_DASHBOARD_NO_APP") is None else None
