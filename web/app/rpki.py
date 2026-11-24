"""RPKI route origin validation (RFC 6811) with VRPs from Routinator / rpki-client.

The VRPs are loaded from a JSON export (Routinator ``/json``, rpki-client
``rpki.json``: ``{"roas": [{"prefix", "maxLength", "asn", "ta"}]}``) every
``rpki_refresh_minutes``. All active routes are validated in a worker thread,
the index stays in memory for looking glass queries and the own prefix check.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
from datetime import datetime, timezone

import httpx
from pymongo import MongoClient

from .config import Settings
from .dns_cache import DnsCache

log = logging.getLogger(__name__)

VALID, INVALID, NOT_FOUND = "valid", "invalid", "not-found"
STATES = (VALID, INVALID, NOT_FOUND)
MAX_INVALID_LISTED = 500
_WIDTH = {4: 32, 6: 128}


def _parse_asn(value) -> int:
    if isinstance(value, str):
        return int(value.strip().lstrip("aAsS"))
    return int(value)


class VrpIndex:
    """Validated ROA payloads indexed by (network, prefix length) per IP version."""

    def __init__(self):
        # key: network_int << 8 | prefixlen -> packed (asn << 8 | maxlen) or tuple of those
        self._tables: dict[int, dict[int, int | tuple]] = {4: {}, 6: {}}
        self._lengths: dict[int, list[int]] = {4: [], 6: []}
        self.count = 0

    @classmethod
    def from_json(cls, data: dict) -> "VrpIndex":
        index = cls()
        lengths = {4: set(), 6: set()}
        for roa in data.get("roas") or data.get("vrps") or []:
            try:
                net = ipaddress.ip_network(roa["prefix"], strict=False)
                asn = _parse_asn(roa["asn"])
                maxlen = int(roa.get("maxLength") or roa.get("max_length") or net.prefixlen)
            except (KeyError, ValueError, TypeError):
                continue
            key = (int(net.network_address) << 8) | net.prefixlen
            packed = (asn << 8) | maxlen
            table = index._tables[net.version]
            existing = table.get(key)
            if existing is None:
                table[key] = packed
            elif isinstance(existing, tuple):
                if packed not in existing:
                    table[key] = existing + (packed,)
            elif existing != packed:
                table[key] = (existing, packed)
            lengths[net.version].add(net.prefixlen)
            index.count += 1
        index._lengths = {v: sorted(lengths[v], reverse=True) for v in (4, 6)}
        return index

    def _covering(self, version: int, net_int: int, plen: int):
        table, width = self._tables[version], _WIDTH[version]
        for length in self._lengths[version]:
            if length > plen:
                continue
            host_bits = width - length
            key = (((net_int >> host_bits) << host_bits) << 8) | length
            entry = table.get(key)
            if entry is not None:
                for packed in (entry if isinstance(entry, tuple) else (entry,)):
                    yield key, packed

    def validate_int(self, version: int, net_int: int, plen: int, origin: int | None) -> tuple[str, str | None]:
        """Return (state, reason). reason is 'as' or 'length' for invalid routes."""
        covered = False
        wrong_length = False
        for _key, packed in self._covering(version, net_int, plen):
            covered = True
            asn, maxlen = packed >> 8, packed & 0xFF
            if origin is not None and asn == origin and asn != 0:
                if plen <= maxlen:
                    return VALID, None
                wrong_length = True
        if not covered:
            return NOT_FOUND, None
        return INVALID, "length" if wrong_length else "as"

    def validate(self, prefix: str, origin: int | None) -> tuple[str, str | None]:
        net = ipaddress.ip_network(prefix, strict=False)
        return self.validate_int(net.version, int(net.network_address), net.prefixlen, origin)

    def covering(self, prefix: str) -> list[dict]:
        net = ipaddress.ip_network(prefix, strict=False)
        result = []
        for key, packed in self._covering(net.version, int(net.network_address), net.prefixlen):
            network_cls = ipaddress.IPv4Network if net.version == 4 else ipaddress.IPv6Network
            vrp_net = network_cls((key >> 8, key & 0xFF))
            result.append({"prefix": str(vrp_net), "max_length": packed & 0xFF, "asn": packed >> 8})
        return result


def validate_table(coll, index: VrpIndex, own_asn: int) -> dict:
    """Validate all active routes (runs in a worker thread with a sync collection)."""
    counts = {s: {"4": 0, "6": 0} for s in STATES}
    invalid: list[dict] = []
    invalid_total = 0
    cursor = coll.find({"active": True},
                       {"origin_asn": 1, "ip_version": 1, "prefix_len": 1, "net_start": 1, "nexthop_asn": 1},
                       batch_size=10000)
    for doc in cursor:
        try:
            if "net_start" in doc and "prefix_len" in doc:
                version, net_int, plen = doc["ip_version"], int(doc["net_start"], 16), doc["prefix_len"]
            else:
                net = ipaddress.ip_network(doc["_id"], strict=False)
                version, net_int, plen = net.version, int(net.network_address), net.prefixlen
        except (ValueError, KeyError, TypeError):
            continue
        origin = doc.get("origin_asn")
        if origin is None:  # empty AS path: originated by the own AS
            origin = own_asn
        state, reason = index.validate_int(version, net_int, plen, origin)
        counts[state][str(version)] += 1
        if state == INVALID:
            invalid_total += 1
            if len(invalid) < MAX_INVALID_LISTED:
                invalid.append({"prefix": doc["_id"], "origin_asn": origin, "reason": reason,
                                "nexthop_asn": doc.get("nexthop_asn")})
    for item in invalid:
        item["vrps"] = index.covering(item["prefix"])
    return {"counts": counts, "invalid": invalid, "invalid_total": invalid_total}


class RpkiService:
    def __init__(self, settings: Settings, dns: DnsCache):
        self.settings = settings
        self.dns = dns
        self.index: VrpIndex | None = None
        self.data: dict = {"enabled": bool(settings.rpki_vrp_url), "ok": False, "error": None,
                           "updated": None, "vrps": 0, "counts": None, "invalid": [], "invalid_total": 0}

    @property
    def enabled(self) -> bool:
        return bool(self.settings.rpki_vrp_url)

    async def fetch(self) -> VrpIndex:
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            response = await client.get(self.settings.rpki_vrp_url, headers={"Accept": "application/json"})
            response.raise_for_status()
            data = response.json()
        index = await asyncio.to_thread(VrpIndex.from_json, data)
        if index.count == 0:
            raise ValueError("the VRP export contains no ROAs")
        return index

    async def refresh(self) -> None:
        started = time.monotonic()
        try:
            index = await self.fetch()
            self.index = index

            def run():
                client = MongoClient(self.settings.mongo_uri, serverSelectionTimeoutMS=5000)
                try:
                    return validate_table(client[self.settings.mongo_db]["bgp"], index, self.settings.asn)
                finally:
                    client.close()

            result = await asyncio.to_thread(run)
            names = await self.dns.asn_names({i["origin_asn"] for i in result["invalid"]})
            for item in result["invalid"]:
                item["origin_name"] = names.get(item["origin_asn"])
            self.data.update(result, ok=True, error=None, vrps=index.count,
                             updated=datetime.now(timezone.utc).isoformat())
            log.info("RPKI: %d VRPs, %d invalid routes, validated in %.1fs",
                     index.count, result["invalid_total"], time.monotonic() - started)
        except Exception as err:  # noqa: BLE001 - keep the last good result
            self.data.update(ok=False, error=f"{type(err).__name__}: {err}")
            log.warning("RPKI refresh failed: %s", err)

    async def run(self) -> None:
        while True:
            await self.refresh()
            await asyncio.sleep(max(60, self.settings.rpki_refresh_minutes * 60))

    def route_state(self, prefix: str, origin: int | None) -> dict | None:
        if self.index is None:
            return None
        state, reason = self.index.validate(prefix, origin if origin is not None else self.settings.asn)
        return {"state": state, "reason": reason, "vrps": self.index.covering(prefix)}
