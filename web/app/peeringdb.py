"""Peering suggestions from PeeringDB.

Candidates are the networks directly behind the transit providers (second AS of
the AS path of routes tagged with the transit community, i.e. what you could
reach directly with a peering) that are not direct peers yet. For the top
candidates (by traffic when Akvorado is configured, otherwise by prefix count)
PeeringDB tells at which internet exchanges you are both present and their
peering policy. Results are cached in MongoDB and refreshed every
``peeringdb_refresh_hours``.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import httpx

from .config import Settings
from .dns_cache import special_asn_name

log = logging.getLogger(__name__)
CHUNK = 50
CACHE_ID = "suggestions"


class PeeringDBClient:
    def __init__(self, settings: Settings):
        self.base = settings.peeringdb_url.rstrip("/")
        self.headers = {"Accept": "application/json", "User-Agent": "bgp-dashboard"}
        if settings.peeringdb_api_key:
            self.headers["Authorization"] = f"Api-Key {settings.peeringdb_api_key}"

    async def get(self, client: httpx.AsyncClient, namespace: str, **params) -> list[dict]:
        response = await client.get(f"{self.base}/{namespace}", params=params, headers=self.headers)
        if response.status_code == 429:
            raise RuntimeError("PeeringDB rate limit reached (set PEERINGDB_API_KEY for a higher limit)")
        response.raise_for_status()
        return response.json().get("data", [])

    async def by_asns(self, client, namespace: str, asns: list[int]) -> list[dict]:
        result = []
        for i in range(0, len(asns), CHUNK):
            chunk = asns[i:i + CHUNK]
            result += await self.get(client, namespace, asn__in=",".join(map(str, chunk)))
        return result


class PeeringDBService:
    def __init__(self, db, settings: Settings, dns, akvorado=None):
        self.db = db
        self.coll = db["bgp"]
        self.settings = settings
        self.dns = dns
        self.akvorado = akvorado
        self.data: dict = {"enabled": settings.peeringdb_enabled, "ok": False, "error": None,
                           "updated": None, "ranked_by": None, "own_ixes": [], "suggestions": [],
                           "candidates_checked": 0, "not_in_peeringdb": 0, "no_common_ix": 0}

    @property
    def enabled(self) -> bool:
        return self.settings.peeringdb_enabled

    async def candidates(self) -> list[dict]:
        """Networks directly behind the transit providers, with prefix counts."""
        s = self.settings
        match: dict = {"active": True, "as_path.1": {"$exists": True}}
        if s.transit_community:
            match["communities"] = s.transit_community
        peers = set(await self.coll.distinct(
            "nexthop_asn", {"active": True, **({"communities": s.peer_learned_community}
                                               if s.peer_learned_community else {})}))
        if not s.peer_learned_community:  # without the community: every next-hop AS is a neighbor
            peers = set()
        rows = await (await self.coll.aggregate([
            {"$match": match},
            {"$group": {"_id": {"$arrayElemAt": ["$as_path", 1]}, "prefixes": {"$sum": 1},
                        "origins": {"$addToSet": "$origin_asn"}}},
        ], allowDiskUse=True)).to_list()
        traffic = self.akvorado.traffic() if self.akvorado else None
        result = []
        for r in rows:
            asn = r["_id"]
            if not isinstance(asn, int) or asn in peers or asn == s.asn or special_asn_name(asn):
                continue
            bps = sum(traffic.get(o, 0) for o in r["origins"] if o is not None) if traffic else None
            result.append({"asn": asn, "prefixes_via_transit": r["prefixes"],
                           "origin_asns": len(r["origins"]), "traffic_bps": bps})
        if traffic:
            result.sort(key=lambda c: (-(c["traffic_bps"] or 0), -c["prefixes_via_transit"]))
        else:
            result.sort(key=lambda c: -c["prefixes_via_transit"])
        return result[: s.peeringdb_candidates]

    async def refresh(self) -> None:
        try:
            candidates = await self.candidates()
            api = PeeringDBClient(self.settings)
            async with httpx.AsyncClient(timeout=60) as client:
                own = await api.get(client, "netixlan", asn=self.settings.asn)
                asns = [c["asn"] for c in candidates]
                nets = {n["asn"]: n for n in await api.by_asns(client, "net", asns)} if asns else {}
                netixlans = await api.by_asns(client, "netixlan", asns) if asns else []

            own_ixes = {}
            for lan in own:
                own_ixes.setdefault(lan["ix_id"], {"ix_id": lan["ix_id"], "name": lan.get("name"),
                                                   "speed": 0})["speed"] += lan.get("speed") or 0
            presence: dict[int, dict[int, dict]] = {}
            for lan in netixlans:
                if lan.get("operational") is False:
                    continue
                entry = presence.setdefault(lan["asn"], {}).setdefault(lan["ix_id"], {
                    "ix_id": lan["ix_id"], "name": lan.get("name"), "speed": 0, "route_server": False})
                entry["speed"] += lan.get("speed") or 0
                entry["route_server"] = entry["route_server"] or bool(lan.get("is_rs_peer"))

            suggestions, not_in_pdb, no_common = [], 0, 0
            for c in candidates:
                net = nets.get(c["asn"])
                if net is None:
                    not_in_pdb += 1
                    continue
                common = [ix for ix_id, ix in presence.get(c["asn"], {}).items() if ix_id in own_ixes]
                if not common:
                    no_common += 1
                    continue
                suggestions.append({
                    **c,
                    "name": net.get("name"),
                    "policy": net.get("policy_general"),
                    "type": net.get("info_type") or ", ".join(net.get("info_types") or []),
                    "irr_as_set": net.get("irr_as_set"),
                    "website": net.get("website"),
                    "policy_url": net.get("policy_url"),
                    "peeringdb_url": f"https://www.peeringdb.com/asn/{c['asn']}",
                    "common_ixes": sorted(common, key=lambda ix: ix["name"] or ""),
                })
            result = {"ok": True, "error": None, "updated": datetime.now(timezone.utc).isoformat(),
                      "ranked_by": "traffic" if any(c["traffic_bps"] for c in candidates) else "prefixes",
                      "own_ixes": sorted(own_ixes.values(), key=lambda ix: ix["name"] or ""),
                      "suggestions": suggestions, "candidates_checked": len(candidates),
                      "not_in_peeringdb": not_in_pdb, "no_common_ix": no_common}
            self.data.update(result)
            await self.db["peeringdb_cache"].replace_one({"_id": CACHE_ID}, {"_id": CACHE_ID, **result},
                                                         upsert=True)
            log.info("PeeringDB: %d suggestions from %d candidates", len(suggestions), len(candidates))
        except Exception as err:  # noqa: BLE001 - keep the last result
            self.data.update(ok=False, error=f"{type(err).__name__}: {err}")
            log.warning("PeeringDB refresh failed: %s", err)

    async def load_cache(self) -> float | None:
        """Load the last result; returns its age in seconds."""
        doc = await self.db["peeringdb_cache"].find_one({"_id": CACHE_ID})
        if not doc:
            return None
        doc.pop("_id")
        self.data.update(doc)
        updated = datetime.fromisoformat(doc["updated"])
        return (datetime.now(timezone.utc) - updated).total_seconds()

    async def run(self, stats=None) -> None:
        interval = max(3600, self.settings.peeringdb_refresh_hours * 3600)
        age = await self.load_cache()
        # wait until the table is learned (and Akvorado had a chance to load) before the first run
        for _ in range(60):
            if stats is None or stats.ready:
                break
            await asyncio.sleep(10)
        if age is not None and age < interval:
            await asyncio.sleep(interval - age)
        else:
            await asyncio.sleep(120 if self.akvorado and self.akvorado.enabled else 30)
        while True:
            await self.refresh()
            await asyncio.sleep(interval)
