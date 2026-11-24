"""Dashboard statistics.

All numbers are computed inside MongoDB with aggregation pipelines (no
documents with their history are pulled into Python) by one background task
per process. Requests only read the cached result, so a slow query can never
block the web server.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from .config import Settings
from .dns_cache import DnsCache
from .exits import exit_analysis
from .monitoring import check_own_prefixes, unstable_prefixes

log = logging.getLogger(__name__)

ACTIVE = {"active": True}
# Counters stored for the trend graphs
TREND_KEYS = (
    "peer_count", "ipv4_table_size", "ipv6_table_size", "nexthop_ip_count",
    "avg_as_path_length", "customer_count", "customer_ipv4_prefixes", "customer_ipv6_prefixes",
    "own_ipv4_prefixes", "own_ipv6_prefixes", "route_changes_1h",
)


def _timestamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())


class StatsService:
    def __init__(self, db, settings: Settings, dns: DnsCache):
        self.db = db
        self.coll = db["bgp"]
        self.settings = settings
        self.dns = dns
        self.ready = False
        self.peer_asns: set[int | None] = set()
        self.last_error: str | None = None
        self.data: dict = {
            "peer_count": 0,
            "ipv6_table_size": 0,
            "ipv4_table_size": 0,
            "nexthop_ip_count": 0,
            "avg_as_path_length": 0,
            "top_n_peers": [],
            "cidr_breakdown": [],
            "communities": [],
            "peers": [],
            "customers": [],
            "customer_count": 0,
            "customer_ipv4_prefixes": 0,
            "customer_ipv6_prefixes": 0,
            "customers_enabled": bool(settings.customer_community),
            "own_ipv4_prefixes": 0,
            "own_ipv6_prefixes": 0,
            "route_changes_1h": 0,
            "transit_percentage": None,
            "own_prefixes": [],
            "exits": {"configured": False},
            "own_prefix_problems": 0,
            "unstable": {"window_hours": settings.unstable_window_hours, "route_changes": 0,
                         "route_changes_1h": 0, "changed_prefixes": 0, "unstable_prefixes": 0,
                         "min_changes": settings.unstable_min_changes, "top": []},
            "timestamp": _timestamp(),
            "basic_updated": None,
            "advanced_updated": None,
        }

    # ------------------------------------------------------------------ #
    # Background loop
    # ------------------------------------------------------------------ #
    async def ensure_indexes(self) -> None:
        days = self.settings.stats_history_days
        await self.db["stats_history"].create_index("ts", expireAfterSeconds=days * 86400)

    async def run(self) -> None:
        """Refresh the statistics forever. Errors are logged, never fatal."""
        next_advanced = 0.0
        while True:
            started = time.monotonic()
            try:
                await self.update_stats()
                if started >= next_advanced:
                    await self.update_advanced_stats()
                    next_advanced = started + self.settings.advanced_stats_interval
                await self.record_history()
                self.ready = True
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                self.last_error = f"{type(err).__name__}: {err}"
                log.exception("Updating statistics failed")
            elapsed = time.monotonic() - started
            log.debug("Statistics refreshed in %.2fs", elapsed)
            await asyncio.sleep(max(1.0, self.settings.stats_interval - elapsed))

    # ------------------------------------------------------------------ #
    # Cheap counters
    # ------------------------------------------------------------------ #
    async def update_stats(self) -> None:
        peers, v4, v6, nexthops = await asyncio.gather(
            self.coll.distinct("nexthop_asn", ACTIVE),
            self.coll.count_documents({"ip_version": 4, "active": True}),
            self.coll.count_documents({"ip_version": 6, "active": True}),
            self.coll.distinct("nexthop", ACTIVE),
        )
        self.peer_asns = set(peers)
        own = await check_own_prefixes(self.coll, self.settings)
        own_v4, own_v6 = await asyncio.gather(*(
            self.coll.count_documents({**self.own_filter(), "ip_version": v}) for v in (4, 6)))
        self.data.update(
            own_prefixes=own,
            own_prefix_problems=sum(1 for p in own if not p["ok"]),
            own_ipv4_prefixes=own_v4,
            own_ipv6_prefixes=own_v6,
            peer_count=len(peers),
            ipv4_table_size=v4,
            ipv6_table_size=v6,
            nexthop_ip_count=len(nexthops),
            timestamp=_timestamp(),
            basic_updated=datetime.now(timezone.utc).isoformat(),
        )

    # ------------------------------------------------------------------ #
    # Expensive statistics
    # ------------------------------------------------------------------ #
    async def _aggregate(self, pipeline: list[dict]) -> list[dict]:
        cursor = await self.coll.aggregate(pipeline, allowDiskUse=True)
        return await cursor.to_list()

    async def nexthop_breakdown(self) -> list[dict]:
        """Per next hop ASN: prefix count per IP version and downstream ASNs."""
        return await self._aggregate([
            {"$match": ACTIVE},
            {"$group": {
                "_id": "$nexthop_asn",
                "v4": {"$sum": {"$cond": [{"$eq": ["$ip_version", 4]}, 1, 0]}},
                "v6": {"$sum": {"$cond": [{"$eq": ["$ip_version", 6]}, 1, 0]}},
                "downstream": {"$addToSet": {"$arrayElemAt": [{"$ifNull": ["$as_path", []]}, 1]}},
            }},
            {"$project": {
                "v4": 1, "v6": 1,
                "asn_count": {"$size": {"$filter": {
                    "input": "$downstream", "cond": {"$ne": ["$$this", None]}}}},
            }},
        ])

    async def origin_counts(self, asns: list[int | None]) -> dict:
        rows = await self._aggregate([
            {"$match": {"active": True, "origin_asn": {"$in": asns}}},
            {"$group": {"_id": {"asn": "$origin_asn", "v": "$ip_version"}, "count": {"$sum": 1}}},
        ])
        counts: dict = defaultdict(lambda: {4: 0, 6: 0})
        for row in rows:
            counts[row["_id"].get("asn")][row["_id"]["v"]] = row["count"]
        return counts

    async def cidrs(self) -> list[dict]:
        rows = await self._aggregate([
            {"$match": ACTIVE},
            {"$group": {
                "_id": {
                    "v": "$ip_version",
                    "mask": {"$toInt": {"$arrayElemAt": [{"$split": ["$_id", "/"]}, 1]}},
                },
                "count": {"$sum": 1},
            }},
        ])
        result = [{"mask": r["_id"]["mask"], "count": r["count"], "ip_version": r["_id"]["v"]}
                  for r in rows]
        return sorted(result, key=lambda x: (x["mask"], x["ip_version"]))

    async def avg_as_path_len(self, digits: int = 2) -> float:
        """Average AS path length, AS prepending is ignored."""
        rows = await self._aggregate([
            {"$match": ACTIVE},
            {"$group": {"_id": None, "avg": {
                "$avg": {"$size": {"$setUnion": [{"$ifNull": ["$as_path", []]}, []]}}}}},
        ])
        return round(rows[0]["avg"], digits) if rows and rows[0]["avg"] is not None else 0

    async def communities_count(self) -> list[dict]:
        rows = await self._aggregate([
            {"$match": ACTIVE},
            {"$project": {"communities": 1}},
            {"$unwind": "$communities"},
            {"$group": {"_id": "$communities", "count": {"$sum": 1}}},
        ])
        names = self.settings.community_names
        return sorted(
            ({"community": r["_id"], "count": r["count"], "name": names.get(r["_id"])}
             for r in rows if r["_id"] is not None),
            key=lambda c: c["community"])

    def own_filter(self) -> dict:
        """Routes originated in the own AS: tagged with the originated community or empty AS path."""
        if self.settings.originated_community:
            return {"communities": self.settings.originated_community, "active": True}
        return {"origin_asn": None, "active": True}

    async def customer_asns(self) -> list[int | None]:
        if not self.settings.customer_community:
            return []
        return await self.coll.distinct(
            "nexthop_asn", {"communities": self.settings.customer_community, "active": True})

    async def update_advanced_stats(self) -> None:
        started = time.monotonic()
        nexthop_rows, cidrs, avg_len, communities, customer_asns, unstable, exits = await asyncio.gather(
            self.nexthop_breakdown(), self.cidrs(), self.avg_as_path_len(),
            self.communities_count(), self.customer_asns(),
            unstable_prefixes(self.coll, self.settings, self.dns),
            exit_analysis(self.coll, self.settings, self.dns))

        by_nexthop = {row["_id"]: row for row in nexthop_rows}
        relevant = list(set(by_nexthop) | set(customer_asns))
        origins = await self.origin_counts(relevant)
        names = await self.dns.asn_names(relevant, default_asn=self.settings.asn)

        def entry(asn):
            nh = by_nexthop.get(asn, {})
            return {
                "asn": asn if asn is not None else self.settings.asn,
                "name": names.get(asn),
                "ipv4_origin_count": origins[asn][4],
                "ipv6_origin_count": origins[asn][6],
                "ipv4_nexthop_count": nh.get("v4", 0),
                "ipv6_nexthop_count": nh.get("v6", 0),
                "asn_count": nh.get("asn_count", 0),
            }

        peers = sorted((entry(a) for a in by_nexthop), key=lambda p: p["asn"])
        customers = sorted((entry(a) for a in customer_asns), key=lambda p: p["asn"])
        top = sorted(peers, key=lambda p: p["ipv4_nexthop_count"] + p["ipv6_nexthop_count"],
                     reverse=True)[: self.settings.top_peers]

        total = sum(c["count"] for c in cidrs)
        transit = next((c["count"] for c in communities
                        if c["community"] == self.settings.transit_community), 0)

        self.peer_asns = set(by_nexthop)
        self.data.update(
            avg_as_path_length=avg_len,
            top_n_peers=[{"asn": p["asn"], "count": p["ipv4_nexthop_count"] + p["ipv6_nexthop_count"],
                          "name": p["name"]} for p in top],
            cidr_breakdown=cidrs,
            communities=communities,
            peers=peers,
            customers=customers,
            customer_count=len(customers),
            customer_ipv4_prefixes=sum(c["ipv4_origin_count"] for c in customers),
            customer_ipv6_prefixes=sum(c["ipv6_origin_count"] for c in customers),
            transit_percentage=round(transit / total * 100, 1) if total else None,
            unstable=unstable,
            route_changes_1h=unstable["route_changes_1h"],
            exits=exits,
            timestamp=_timestamp(),
            advanced_updated=datetime.now(timezone.utc).isoformat(),
        )
        log.info("Advanced statistics refreshed in %.1fs (%d peers, %d prefixes)",
                 time.monotonic() - started, len(peers), total)

    # ------------------------------------------------------------------ #
    # Trend history
    # ------------------------------------------------------------------ #
    async def record_history(self) -> None:
        doc = {k: self.data[k] for k in TREND_KEYS}
        doc["ts"] = datetime.now(timezone.utc)
        await self.db["stats_history"].insert_one(doc)

    async def history(self, hours: float = 24, points: int = 300) -> list[dict]:
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        cursor = self.db["stats_history"].find({"ts": {"$gte": since}}, {"_id": 0}).sort("ts", 1)
        rows = await cursor.to_list()
        if len(rows) > points > 0:
            step = len(rows) / points
            rows = [rows[int(i * step)] for i in range(points - 1)] + [rows[-1]]
        for row in rows:
            ts = row["ts"]
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            row["ts"] = ts.isoformat()
        return rows

    def get_data(self) -> dict:
        return {**self.data, "ready": self.ready}

