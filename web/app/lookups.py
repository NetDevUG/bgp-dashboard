"""Looking glass queries (IP, ASN, domain)."""

from __future__ import annotations

import asyncio
import ipaddress

from .config import Settings
from .dns_cache import DnsCache

NO_HISTORY = {"history": 0}
ID_ONLY = {"_id": 1}


class InvalidQuery(ValueError):
    pass


def candidate_networks(ip: str) -> list[str]:
    """All networks containing *ip*, most specific first."""
    addr = ipaddress.ip_address(ip)
    return [str(ipaddress.ip_network(f"{addr}/{length}", strict=False))
            for length in range(addr.max_prefixlen, -1, -1)]


def is_transit(route: dict, settings: Settings) -> bool:
    return settings.transit_community in (route.get("communities") or [])


class Lookups:
    def __init__(self, db, settings: Settings, dns: DnsCache, rpki=None):
        self.rpki = rpki
        self.coll = db["bgp"]
        self.settings = settings
        self.dns = dns

    async def find_network(self, ip: str, with_history: bool = False) -> dict | None:
        """Longest prefix match of *ip* among the active routes (one query)."""
        candidates = candidate_networks(ip)
        projection = None if with_history else NO_HISTORY
        found = await self.coll.find(
            {"_id": {"$in": candidates}, "active": True}, projection).to_list()
        if not found:
            return None
        return max(found, key=lambda d: ipaddress.ip_network(d["_id"], strict=False).prefixlen)

    async def resolve_target(self, query: str) -> str:
        """Turn an IP, prefix or hostname into an IP address string."""
        query = query.strip().split("/")[0]
        try:
            return str(ipaddress.ip_address(query))
        except ValueError:
            pass
        for rdtype in ("A", "AAAA"):
            answers = await self.dns.resolve(query, rdtype)
            if answers:
                return answers[0]
        raise InvalidQuery(f"'{query}' is neither an IP address nor a resolvable hostname")

    async def ip_info(self, query: str, history_url: str | None = None) -> dict:
        ip = await self.resolve_target(query)
        network = await self.find_network(ip, with_history=history_url is None)
        if not network:
            return {}
        name, nexthop_dns, originator_dns = await asyncio.gather(
            self.dns.asn_name(network.get("origin_asn") or self.settings.asn),
            self.dns.ptr(network.get("nexthop")),
            self.dns.ptr(network.get("originator_id")),
        )
        return {
            "prefix": network["_id"],
            "ip_version": network.get("ip_version"),
            "is_transit": is_transit(network, self.settings),
            "origin_asn": network.get("origin_asn"),
            "name": name,
            "nexthop": network.get("nexthop"),
            "nexthop_ip_dns": nexthop_dns,
            "nexthop_asn": network.get("nexthop_asn"),
            "as_path": network.get("as_path"),
            "med": network.get("med"),
            "local_pref": network.get("local_pref"),
            "communities": network.get("communities"),
            "community_names": {c: self.settings.community_names[c]
                                for c in network.get("communities") or []
                                if c in self.settings.community_names},
            "route_origin": network.get("route_origin"),
            "atomic_aggregate": network.get("atomic_aggregate"),
            "aggregator_as": network.get("aggregator_as"),
            "aggregator_address": network.get("aggregator_address"),
            "originator_id": network.get("originator_id"),
            "originator_id_dns": originator_dns,
            "cluster_list": network.get("cluster_list"),
            "age": network.get("age"),
            "rpki": self.rpki.route_state(network["_id"], network.get("origin_asn")) if self.rpki else None,
            "history": history_url if history_url is not None else network.get("history", []),
        }

    def _origin_filter(self, asn: int) -> dict:
        # Locally originated routes have an empty AS path (origin_asn = None)
        if asn == self.settings.asn:
            return {"origin_asn": {"$in": [None, asn]}}
        return {"origin_asn": asn}

    async def asn_info(self, asn: int, is_peer: bool) -> dict:
        routes = await self.coll.find(
            {**self._origin_filter(asn), "active": True}, NO_HISTORY).sort("_id", 1).to_list()
        name = await self.dns.asn_name(asn)
        nexthop_names = await self.dns.ptrs(r.get("nexthop") for r in routes)
        return {
            "asn": asn,
            "name": name,
            "origin_prefix_count": len(routes),
            "is_peer": is_peer,
            "origin_prefix_list": [{
                "prefix": r["_id"],
                "is_transit": is_transit(r, self.settings),
                "origin_asn": r.get("origin_asn"),
                "name": name,
                "nexthop_ip": r.get("nexthop"),
                "nexthop_ip_dns": nexthop_names.get(r.get("nexthop")),
                "nexthop_asn": r.get("nexthop_asn"),
                "as_path": r.get("as_path"),
                "communities": r.get("communities"),
                "updated": r.get("age"),
            } for r in routes],
        }

    async def prefix_ids(self, query: dict) -> list[str]:
        docs = await self.coll.find({**query, "active": True}, ID_ONLY).sort("_id", 1).to_list()
        return [d["_id"] for d in docs]

    async def originated(self, asn: int, version: int | None = None) -> list[str]:
        query = self._origin_filter(asn)
        if version:
            query["ip_version"] = version
        return await self.prefix_ids(query)

    async def nexthop(self, asn: int, version: int | None = None) -> list[str]:
        query: dict = {"nexthop_asn": asn}
        if version:
            query["ip_version"] = version
        return await self.prefix_ids(query)

    async def transit(self, asn: int) -> list[str]:
        return await self.prefix_ids({"as_path": asn})

    async def downstream(self, asn: int) -> dict:
        asns = await self.coll.distinct("as_path.1", {"nexthop_asn": asn, "active": True})
        asns = sorted(a for a in asns if a is not None)
        if len(asns) > self.settings.large_query_threshold:
            names = {a: "(LARGE QUERY - DNS LOOKUP DISABLED)" for a in asns}
        else:
            names = await self.dns.asn_names(asns)
        return {
            "asn": asn,
            "name": await self.dns.asn_name(asn),
            "downstream_asns_count": len(asns),
            "downstream_asns": [{"asn": a, "name": names[a]} for a in asns],
        }

    async def domain(self, domain: str) -> dict:
        domain = domain.strip().lower().rstrip(".")
        labels = domain.split(".")
        if len(labels) < 2:
            raise InvalidQuery(f"'{domain}' is not a domain name")
        org = labels[-2]
        name_servers = await self.dns.resolve(domain, "NS")
        soa = await self.dns.resolve(domain, "SOA")
        # Prefer a name server that belongs to the organisation itself
        local_ns = next((ns.lower() for ns in name_servers if org in ns.lower()), "")
        if not local_ns and soa and org in soa[0].lower():
            local_ns = soa[0].lower()
        if not local_ns:
            return {}
        ns_ip = await self.resolve_target(local_ns)
        network = await self.find_network(ns_ip)
        asn = network.get("origin_asn") if network else None
        originated = await self.originated(asn) if asn is not None else []
        a_records = await self.dns.resolve(domain, "A")
        return {
            "domain": domain,
            "A Record": a_records[0] if a_records else None,
            "SOA/NS Record": local_ns,
            "SOA/NS IP": ns_ip,
            "asn": asn,
            "name": await self.dns.asn_name(asn) if asn is not None else None,
            "originated_prefix_count": len(originated),
            "originated_prefix_list": originated,
        }
