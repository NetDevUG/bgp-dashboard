"""Async DNS lookups with caching.

ASN names (Team Cymru) are cached in memory and persisted in MongoDB so a
restart does not trigger hundreds of DNS queries. Reverse lookups are cached
in memory. Failed lookups are cached briefly so a broken resolver cannot slow
down every request.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
from collections import OrderedDict
from collections.abc import Iterable
from datetime import datetime, timezone

import dns.asyncresolver
import dns.exception
import dns.resolver
import dns.reversename

log = logging.getLogger(__name__)

DNS_ERROR = "(DNS Error)"
_OK_TTL = 24 * 3600
_ERROR_TTL = 300
_ASN_DB_TTL = 7 * 24 * 3600


class TTLCache:
    """Tiny LRU cache with per-entry expiry."""

    def __init__(self, maxsize: int = 50_000):
        self.maxsize = maxsize
        self._data: OrderedDict = OrderedDict()

    def get(self, key):
        item = self._data.get(key)
        if item is None:
            return None
        value, expires = item
        if expires < time.monotonic():
            self._data.pop(key, None)
            return None
        self._data.move_to_end(key)
        return value

    def set(self, key, value, ttl: float) -> None:
        self._data[key] = (value, time.monotonic() + ttl)
        self._data.move_to_end(key)
        while len(self._data) > self.maxsize:
            self._data.popitem(last=False)


def special_asn_name(asn: int) -> str | None:
    if 64496 <= asn <= 64511:
        return "RFC5398 - Documentation ASN"
    if 64512 <= asn <= 65534 or 4200000000 <= asn <= 4294967294:
        return "RFC6996 - Private Use ASN"
    if asn in (0, 23456, 65535, 4294967295):
        return "Reserved ASN"
    return None


class DnsCache:
    def __init__(self, db=None, timeout: float = 2.0, concurrency: int = 32):
        self.resolver = dns.asyncresolver.Resolver()
        self.resolver.lifetime = timeout
        self.resolver.timeout = timeout
        self._names = TTLCache()
        self._ptr = TTLCache()
        self._db = db
        self._sem = asyncio.Semaphore(concurrency)
        self._inflight: dict[tuple, asyncio.Future] = {}

    # -- generic --------------------------------------------------------- #
    async def resolve(self, name: str, rdtype: str = "A") -> list[str]:
        """Return the answers for *name* as strings (empty list on error)."""
        try:
            async with self._sem:
                answer = await self.resolver.resolve(name, rdtype)
        except (dns.exception.DNSException, ValueError) as err:
            log.debug("DNS %s %s failed: %s", rdtype, name, err)
            return []
        if rdtype == "NS":
            return [str(r.target).rstrip(".") for r in answer]
        if rdtype == "SOA":
            return [str(r.mname).rstrip(".") for r in answer]
        return [str(r).rstrip(".") for r in answer]

    async def _dedup(self, key: tuple, factory):
        """Run *factory* once for concurrent callers asking for the same key."""
        fut = self._inflight.get(key)
        if fut is not None:
            return await fut
        fut = asyncio.get_running_loop().create_future()
        self._inflight[key] = fut
        try:
            result = await factory()
            fut.set_result(result)
            return result
        except BaseException as err:
            fut.set_exception(err)
            fut.exception()  # mark retrieved
            raise
        finally:
            self._inflight.pop(key, None)

    # -- reverse DNS ----------------------------------------------------- #
    async def ptr(self, ip: str | None) -> str:
        if not ip:
            return DNS_ERROR
        cached = self._ptr.get(ip)
        if cached is not None:
            return cached

        async def lookup() -> str:
            try:
                rev = dns.reversename.from_address(str(ip))
            except (dns.exception.DNSException, ValueError):
                return DNS_ERROR
            answers = await self.resolve(rev.to_text(), "PTR")
            name = answers[0] if answers else DNS_ERROR
            self._ptr.set(ip, name, _OK_TTL if answers else _ERROR_TTL)
            return name

        return await self._dedup(("ptr", ip), lookup)

    async def ptrs(self, ips: Iterable[str | None]) -> dict[str | None, str]:
        unique = list(dict.fromkeys(ips))
        names = await asyncio.gather(*(self.ptr(ip) for ip in unique))
        return dict(zip(unique, names))

    # -- ASN names ------------------------------------------------------- #
    async def asn_name(self, asn: int | None) -> str:
        return (await self.asn_names([asn]))[asn]

    async def asn_names(self, asns: Iterable[int | None], default_asn: int | None = None) -> dict:
        """Return ``{asn: name}``; ``None`` is resolved as *default_asn*."""
        result: dict = {}
        missing: list[int] = []
        for asn in dict.fromkeys(asns):
            real = default_asn if asn is None else asn
            if real is None:
                result[asn] = DNS_ERROR
                continue
            special = special_asn_name(real)
            cached = special or self._names.get(real)
            if cached is not None:
                result[asn] = cached
            else:
                missing.append(real)

        if missing:
            found = await self._load_from_db(missing)
            still_missing = [a for a in missing if a not in found]
            if still_missing:
                looked_up = await asyncio.gather(*(self._lookup_asn(a) for a in still_missing))
                fresh = {a: n for a, n in zip(still_missing, looked_up) if n != DNS_ERROR}
                found.update(dict(zip(still_missing, looked_up)))
                await self._store_in_db(fresh)

        for asn in dict.fromkeys(asns):
            if asn not in result:
                real = default_asn if asn is None else asn
                result[asn] = found.get(real) or self._names.get(real) or DNS_ERROR
        return result

    async def _lookup_asn(self, asn: int) -> str:
        async def lookup() -> str:
            answers = await self.resolve(f"AS{asn}.asn.cymru.com", "TXT")
            if not answers:
                self._names.set(asn, DNS_ERROR, _ERROR_TTL)
                return DNS_ERROR
            # "3701 | US | arin | 1994-10-25 | NERONET - Network for Education..., US"
            txt = answers[0].strip('"').replace('" "', "")
            name = txt.split("|")[-1].strip()
            name = name.rsplit(",", 1)[0].strip() if "," in name else name
            self._names.set(asn, name, _OK_TTL)
            return name

        return await self._dedup(("asn", asn), lookup)

    async def _load_from_db(self, asns: list[int]) -> dict[int, str]:
        if self._db is None:
            return {}
        try:
            found = {}
            async for doc in self._db["asn_names"].find({"_id": {"$in": asns}}):
                found[doc["_id"]] = doc["name"]
                self._names.set(doc["_id"], doc["name"], _OK_TTL)
            return found
        except Exception as err:  # noqa: BLE001 - cache only
            log.warning("Could not read ASN name cache: %s", err)
            return {}

    async def _store_in_db(self, names: dict[int, str]) -> None:
        if self._db is None or not names:
            return
        from pymongo import UpdateOne

        now = datetime.now(timezone.utc)
        ops = [UpdateOne({"_id": a}, {"$set": {"name": n, "updated": now}}, upsert=True)
               for a, n in names.items()]
        try:
            await self._db["asn_names"].bulk_write(ops, ordered=False)
        except Exception as err:  # noqa: BLE001 - cache only
            log.warning("Could not store ASN name cache: %s", err)

    async def ensure_indexes(self) -> None:
        if self._db is not None:
            await self._db["asn_names"].create_index("updated", expireAfterSeconds=_ASN_DB_TTL)


def is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False
