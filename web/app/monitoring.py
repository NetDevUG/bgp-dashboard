"""Monitoring checks: own prefixes, unstable prefixes, BGP sessions, collector."""

from __future__ import annotations

import ipaddress
from datetime import datetime, timedelta, timezone

from .config import Settings
from .dns_cache import DnsCache

ROUTE_PROJECTION = {"history": 0, "change_times": 0}
MAX_MORE_SPECIFICS = 500


def _iso(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    return value


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def prefix_range(net: ipaddress.IPv4Network | ipaddress.IPv6Network) -> tuple[str, str]:
    """First and last address as fixed width hex (same encoding as the collector's ``net_start``)."""
    width = 8 if net.version == 4 else 32
    return (format(int(net.network_address), f"0{width}x"),
            format(int(net.broadcast_address), f"0{width}x"))


# --------------------------------------------------------------------------- #
# Own prefixes
# --------------------------------------------------------------------------- #
async def check_own_prefixes(coll, settings: Settings) -> list[dict]:
    """Check every configured own prefix.

    Problems:
    * ``missing``: the prefix is not in the table (or withdrawn)
    * ``wrong_origin``: the prefix is originated by an AS that is not allowed
    * ``foreign_more_specific``: a more specific prefix with a foreign origin exists
    """
    allowed = {None, settings.asn, *settings.allowed_origins}
    results = []
    for raw in settings.own_prefixes:
        try:
            net = ipaddress.ip_network(str(raw).strip(), strict=False)
        except ValueError:
            results.append({"prefix": str(raw), "ok": False, "problems": ["invalid_config"],
                            "route": None, "more_specifics": []})
            continue
        prefix = str(net)
        doc = await coll.find_one({"_id": prefix}, ROUTE_PROJECTION)
        problems = []
        route = None
        if not doc or not doc.get("active"):
            problems.append("missing")
        else:
            route = {
                "origin_asn": doc.get("origin_asn"),
                "as_path": doc.get("as_path"),
                "nexthop": doc.get("nexthop"),
                "nexthop_asn": doc.get("nexthop_asn"),
                "age": doc.get("age"),
                "origin_ok": doc.get("origin_asn") in allowed,
            }
            if not route["origin_ok"]:
                problems.append("wrong_origin")

        more_specifics = []
        if settings.check_more_specifics and net.prefixlen < net.max_prefixlen:
            start, end = prefix_range(net)
            cursor = coll.find({
                "ip_version": net.version,
                "net_start": {"$gte": start, "$lte": end},
                "prefix_len": {"$gt": net.prefixlen},
                "active": True,
            }, ROUTE_PROJECTION).sort("net_start", 1).limit(MAX_MORE_SPECIFICS)
            async for d in cursor:
                more_specifics.append({
                    "prefix": d["_id"],
                    "origin_asn": d.get("origin_asn"),
                    "as_path": d.get("as_path"),
                    "allowed": d.get("origin_asn") in allowed,
                    "age": d.get("age"),
                })
            if any(not m["allowed"] for m in more_specifics):
                problems.append("foreign_more_specific")

        results.append({"prefix": prefix, "ok": not problems, "problems": problems,
                        "route": route, "more_specifics": more_specifics})
    return results


# --------------------------------------------------------------------------- #
# Unstable prefixes
# --------------------------------------------------------------------------- #
async def unstable_prefixes(coll, settings: Settings, dns: DnsCache) -> dict:
    now = datetime.now(timezone.utc)
    window = now - timedelta(hours=settings.unstable_window_hours)
    hour = now - timedelta(hours=1)

    def count_since(since):
        return {"$size": {"$filter": {"input": {"$ifNull": ["$change_times", []]},
                                      "as": "t", "cond": {"$gte": ["$$t", since]}}}}

    cursor = await coll.aggregate([
        {"$match": {"last_change": {"$gte": window}}},
        {"$project": {"origin_asn": 1, "nexthop_asn": 1, "nexthop": 1, "active": 1, "as_path": 1,
                      "last_change": 1, "changes": count_since(window), "changes_1h": count_since(hour)}},
        {"$facet": {
            "top": [{"$match": {"changes": {"$gt": 0}}},
                    {"$sort": {"changes": -1, "last_change": -1}},
                    {"$limit": settings.unstable_top}],
            "totals": [{"$group": {
                "_id": None,
                "changes": {"$sum": "$changes"},
                "changes_1h": {"$sum": "$changes_1h"},
                "prefixes": {"$sum": {"$cond": [{"$gt": ["$changes", 0]}, 1, 0]}},
                "unstable": {"$sum": {"$cond": [
                    {"$gte": ["$changes", settings.unstable_min_changes]}, 1, 0]}},
            }}],
        }},
    ], allowDiskUse=True)
    result = (await cursor.to_list()) or [{"top": [], "totals": []}]
    top, totals = result[0]["top"], (result[0]["totals"] or [{}])[0]
    names = await dns.asn_names([t.get("origin_asn") for t in top], default_asn=settings.asn)
    return {
        "window_hours": settings.unstable_window_hours,
        "route_changes": totals.get("changes", 0),
        "route_changes_1h": totals.get("changes_1h", 0),
        "changed_prefixes": totals.get("prefixes", 0),
        "unstable_prefixes": totals.get("unstable", 0),
        "min_changes": settings.unstable_min_changes,
        "top": [{
            "prefix": t["_id"],
            "changes": t["changes"],
            "changes_1h": t["changes_1h"],
            "origin_asn": t.get("origin_asn"),
            "origin_name": names.get(t.get("origin_asn")),
            "nexthop_asn": t.get("nexthop_asn"),
            "as_path": t.get("as_path"),
            "active": t.get("active", False),
            "last_change": _iso(t.get("last_change")),
        } for t in top],
    }


# --------------------------------------------------------------------------- #
# BGP sessions and collector
# --------------------------------------------------------------------------- #
async def collector_status(db) -> dict:
    status = await db["collector_status"].find_one({"_id": "collector"}) or {}
    status.pop("_id", None)
    return status


async def neighbors(db, settings: Settings, dns: DnsCache) -> dict:
    now = datetime.now(timezone.utc)
    docs = await db["neighbors"].find({}).sort("_id", 1).to_list()
    status = await collector_status(db)
    polled = _aware(status.get("neighbors_polled"))
    stale = polled is None or (now - polled).total_seconds() > settings.neighbor_stale_seconds
    names = await dns.asn_names([d.get("peer_asn") for d in docs if d.get("peer_asn")])

    result = []
    for d in docs:
        up = _aware(d.get("uptime_since"))
        down = _aware(d.get("downtime_since"))
        established = bool(d.get("established"))
        admin_down = d.get("admin_state") == "down"
        never_up = not established and up is None and down is None
        since = up if established else down
        result.append({
            "address": d["_id"],
            "peer_asn": d.get("peer_asn"),
            "peer_name": names.get(d.get("peer_asn")),
            "description": d.get("description") or "",
            "router_id": d.get("router_id"),
            "state": d.get("state"),
            "established": established,
            "admin_state": d.get("admin_state"),
            "admin_down": admin_down,
            "never_established": never_up,
            "since": _iso(since),
            "duration_seconds": int((now - since).total_seconds()) if since else None,
            "session_drops": d.get("session_drops", 0),
            "flaps": d.get("flaps", 0),
            "disconnect_reason": d.get("disconnect_reason"),
            "disconnect_message": d.get("disconnect_message"),
            "families": d.get("families", {}),
            "messages_received": d.get("messages_received", 0),
            "messages_sent": d.get("messages_sent", 0),
            "updated": _iso(d.get("updated")),
        })

    total = len(result)
    up_count = sum(1 for n in result if n["established"])
    admin_down_count = sum(1 for n in result if n["admin_down"] and not n["established"])
    return {
        "summary": {"total": total, "established": up_count, "admin_down": admin_down_count,
                    "down": total - up_count - admin_down_count},
        "stale": stale,
        "polled": _iso(polled),
        "error": status.get("neighbor_error"),
        "neighbors": result,
    }


async def neighbor_events(db, hours: float = 168, limit: int = 200) -> list[dict]:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    docs = await db["neighbor_events"].find({"ts": {"$gte": since}}, {"_id": 0}) \
        .sort("ts", -1).limit(limit).to_list()
    for d in docs:
        d["ts"] = _iso(d["ts"])
    return docs
