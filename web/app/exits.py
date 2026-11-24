"""Exit analysis: where (location) and how (transit / peering / own) prefixes leave the network.

Uses communities your routers set on import:

* ``location_community_prefix`` (e.g. ``216333:105:``) – location where the route was learned
* ``transit_community`` / ``peer_learned_community`` / ``originated_community`` – type of route
* ``peer_community_prefix`` (e.g. ``216333:101:``) – which peer or exchange sent it

Only the best path per prefix is known (that is what the routers send), so this
is the exit the network actually uses.
"""

from __future__ import annotations

from .config import Settings
from .dns_cache import DnsCache

TYPES = ("transit", "peering", "originated", "other")
SAMPLE_PREFIXES = 5


def configured(settings: Settings) -> bool:
    return bool(settings.location_community_prefix or settings.peer_learned_community)


def _first_with_prefix(prefix: str) -> dict:
    """Aggregation expression: first community starting with *prefix* (or null)."""
    if not prefix:
        return {"$literal": None}
    return {"$first": {"$filter": {
        "input": {"$ifNull": ["$communities", []]},
        "cond": {"$eq": [{"$indexOfCP": ["$$this", prefix]}, 0]},
    }}}


def _type_expression(settings: Settings) -> dict:
    branches = []
    for community, name in ((settings.transit_community, "transit"),
                            (settings.peer_learned_community, "peering"),
                            (settings.originated_community, "originated")):
        if community:
            branches.append({"case": {"$in": [community, {"$ifNull": ["$communities", []]}]},
                             "then": name})
    if not branches:
        return {"$literal": "other"}
    return {"$switch": {"branches": branches, "default": "other"}}


async def exit_analysis(coll, settings: Settings, dns: DnsCache) -> dict:
    if not configured(settings):
        return {"configured": False}

    names = settings.community_names
    rows = await (await coll.aggregate([
        {"$match": {"active": True}},
        {"$project": {"ip_version": 1,
                      "loc": _first_with_prefix(settings.location_community_prefix),
                      "type": _type_expression(settings),
                      "via": _first_with_prefix(settings.peer_community_prefix)}},
        {"$facet": {
            "matrix": [{"$group": {"_id": {"loc": "$loc", "type": "$type", "v": "$ip_version"},
                                   "count": {"$sum": 1}}}],
            "via": [{"$match": {"via": {"$ne": None}}},
                    {"$group": {"_id": {"via": "$via", "loc": "$loc"}, "count": {"$sum": 1}}}],
        }},
    ], allowDiskUse=True)).to_list()
    facets = rows[0] if rows else {"matrix": [], "via": []}

    # --- location x type -------------------------------------------------- #
    locations: dict = {}
    for r in facets["matrix"]:
        loc = r["_id"].get("loc")
        entry = locations.setdefault(loc, {
            "location": loc, "name": names.get(loc, loc) if loc else "Unknown location",
            "total": 0, **{t: {"4": 0, "6": 0} for t in TYPES}})
        entry[r["_id"]["type"]][str(r["_id"]["v"])] += r["count"]
        entry["total"] += r["count"]
    location_list = sorted(locations.values(), key=lambda e: (e["location"] is None, -e["total"]))

    # --- peers / exchanges per location ------------------------------------ #
    via: dict = {}
    for r in facets["via"]:
        key = r["_id"]["via"]
        entry = via.setdefault(key, {"community": key, "name": names.get(key), "total": 0, "locations": {}})
        entry["locations"][r["_id"].get("loc") or "unknown"] = r["count"]
        entry["total"] += r["count"]
    via_list = sorted(via.values(), key=lambda e: -e["total"])

    # --- via transit although the origin AS is a direct peer --------------- #
    transit_instead = {"available": False, "total_prefixes": 0, "asns": []}
    if settings.transit_community and settings.peer_learned_community:
        peer_rows = await (await coll.aggregate([
            {"$match": {"active": True, "communities": settings.peer_learned_community,
                        "nexthop_asn": {"$ne": None}}},
            {"$project": {"nexthop_asn": 1,
                          "loc": _first_with_prefix(settings.location_community_prefix)}},
            {"$group": {"_id": "$nexthop_asn", "locations": {"$addToSet": "$loc"},
                        "prefixes": {"$sum": 1}}},
        ], allowDiskUse=True)).to_list()
        peers = {r["_id"]: r for r in peer_rows}
        transit_rows = await (await coll.aggregate([
            {"$match": {"active": True, "communities": settings.transit_community,
                        "origin_asn": {"$in": list(peers)}}},
            {"$sort": {"_id": 1}},
            {"$group": {"_id": "$origin_asn", "count": {"$sum": 1},
                        "samples": {"$firstN": {"input": "$_id", "n": SAMPLE_PREFIXES}},
                        "transit_locations": {"$addToSet": _first_with_prefix(
                            settings.location_community_prefix)}}},
            {"$sort": {"count": -1}},
        ], allowDiskUse=True)).to_list()
        asn_names = await dns.asn_names([r["_id"] for r in transit_rows])
        loc_name = lambda c: names.get(c, c) if c else None  # noqa: E731
        transit_instead = {
            "available": True,
            "total_prefixes": sum(r["count"] for r in transit_rows),
            "asns": [{
                "asn": r["_id"],
                "name": asn_names.get(r["_id"]),
                "prefixes_via_transit": r["count"],
                "prefixes_via_peering": peers[r["_id"]]["prefixes"],
                "peering_locations": sorted(filter(None, map(loc_name, peers[r["_id"]]["locations"]))),
                "transit_locations": sorted(filter(None, map(loc_name, r["transit_locations"]))),
                "samples": r["samples"],
            } for r in transit_rows],
        }

    return {"configured": True, "types": list(TYPES), "locations": location_list,
            "via": via_list, "transit_instead_of_peering": transit_instead}
