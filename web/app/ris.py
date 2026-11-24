"""External view of the own prefixes via RIPE RIS.

* **RIS Live** (WebSocket): every BGP update the RIS route collectors see for
  the own prefixes and their more specifics. The dashboard keeps, per RIS peer,
  the origin it currently announces; a foreign origin (not the own AS and not
  in ``allowed_origins``) for the prefix or a more specific is a possible hijack.
* **RIPEstat routing-status** (every 15 minutes): how many RIS peers see the
  prefix and which origins it has, also right after a restart.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import time
from datetime import datetime, timedelta, timezone

import httpx
import websockets

from .config import Settings

log = logging.getLogger(__name__)
CLIENT_NAME = "bgp-dashboard"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def origin_of(path: list) -> list[int]:
    """Origin AS(es) of a RIS path; the last element can be an AS_SET (list)."""
    if not path:
        return []
    last = path[-1]
    return [int(a) for a in last] if isinstance(last, list) else [int(last)]


class RisLiveService:
    def __init__(self, db, settings: Settings):
        self.db = db
        self.settings = settings
        self.allowed = {settings.asn, *settings.allowed_origins}
        self.own = []
        for raw in settings.own_prefixes:
            try:
                self.own.append(ipaddress.ip_network(str(raw).strip(), strict=False))
            except ValueError:
                continue
        # announced prefix -> RIS peer -> {"origins": [...], "path": [...], "ts": datetime}
        self.state: dict[str, dict[str, dict]] = {}
        self.connected = False
        self.last_message: datetime | None = None
        self.error: str | None = None
        self.visibility: dict[str, dict] = {}

    @property
    def enabled(self) -> bool:
        return self.settings.ris_live_enabled and bool(self.own)

    def covering_own(self, prefix: str):
        try:
            net = ipaddress.ip_network(prefix, strict=False)
        except ValueError:
            return None
        for own in self.own:
            if net.version == own.version and net.subnet_of(own):
                return own
        return None

    # ------------------------------------------------------------------ #
    # Message handling (also used by the tests)
    # ------------------------------------------------------------------ #
    async def handle(self, message: dict) -> None:
        if message.get("type") == "ris_error":
            self.error = str((message.get("data") or {}).get("message"))
            log.warning("RIS Live error: %s", self.error)
            return
        if message.get("type") != "ris_message":
            return
        data = message.get("data") or {}
        if data.get("type") != "UPDATE":
            return
        self.last_message = _now()
        peer = f"{data.get('host', '?')}/{data.get('peer', '?')}"
        ts = datetime.fromtimestamp(float(data.get("timestamp") or time.time()), tz=timezone.utc)
        path = data.get("path") or []
        origins = origin_of(path)
        for announcement in data.get("announcements") or []:
            for prefix in announcement.get("prefixes") or []:
                if self.covering_own(prefix) is not None:
                    await self._set(prefix, peer, {"origins": origins, "path": path, "ts": ts,
                                                   "peer_asn": data.get("peer_asn")})
        for prefix in data.get("withdrawals") or []:
            if prefix in self.state and peer in self.state[prefix]:
                await self._set(prefix, peer, None)

    def _foreign(self, prefix: str) -> set[int]:
        return {o for entry in self.state.get(prefix, {}).values()
                for o in entry["origins"] if o not in self.allowed}

    async def _set(self, prefix: str, peer: str, entry: dict | None) -> None:
        before = self._foreign(prefix)
        peers = self.state.setdefault(prefix, {})
        if entry is None:
            peers.pop(peer, None)
        else:
            peers[peer] = entry
        if not peers:
            self.state.pop(prefix, None)
        after = self._foreign(prefix)
        own = self.covering_own(prefix)
        for origin in after - before:
            await self._event("foreign_announcement", prefix, own, origin, entry)
        for origin in before - after:
            await self._event("foreign_announcement_gone", prefix, own, origin, None)

    async def _event(self, kind: str, prefix: str, own, origin: int, entry: dict | None) -> None:
        event = {"ts": _now(), "type": kind, "prefix": prefix, "own_prefix": str(own),
                 "more_specific": own is not None and prefix != str(own), "origin_asn": origin,
                 "path": entry["path"] if entry else None}
        log.warning("RIS: %s %s origin AS%s (own prefix %s)", kind, prefix, origin, own)
        try:
            await self.db["ris_events"].insert_one(event)
        except Exception as err:  # noqa: BLE001
            log.warning("Could not store RIS event: %s", err)

    # ------------------------------------------------------------------ #
    # Current view
    # ------------------------------------------------------------------ #
    def foreign_announcements(self) -> list[dict]:
        result = []
        for prefix, peers in self.state.items():
            by_origin: dict[int, dict] = {}
            for peer, entry in peers.items():
                for origin in entry["origins"]:
                    if origin in self.allowed:
                        continue
                    item = by_origin.setdefault(origin, {"prefix": prefix, "origin_asn": origin, "peers": 0,
                                                         "own_prefix": str(self.covering_own(prefix)),
                                                         "path": entry["path"], "last_seen": entry["ts"]})
                    item["peers"] += 1
                    item["last_seen"] = max(item["last_seen"], entry["ts"])
            result += by_origin.values()
        for item in result:
            item["more_specific"] = item["prefix"] != item["own_prefix"]
            item["last_seen"] = item["last_seen"].isoformat()
        return sorted(result, key=lambda i: (-i["peers"], i["prefix"]))

    def summary(self) -> dict:
        foreign = self.foreign_announcements()
        prefixes = []
        for own in self.own:
            key = str(own)
            exact = self.state.get(key, {})
            prefixes.append({
                "prefix": key,
                "ris_peers_announcing": len(exact),
                "origins_seen": sorted({o for e in exact.values() for o in e["origins"]}),
                "foreign": [f for f in foreign if f["own_prefix"] == key],
                "visibility": self.visibility.get(key),
            })
        return {"enabled": self.enabled, "connected": self.connected, "error": self.error,
                "last_message": self.last_message.isoformat() if self.last_message else None,
                "prefixes": prefixes, "foreign_total": len(foreign)}

    async def events(self, hours: float = 168, limit: int = 200) -> list[dict]:
        since = _now() - timedelta(hours=hours)
        docs = await self.db["ris_events"].find({"ts": {"$gte": since}}, {"_id": 0}) \
            .sort("ts", -1).limit(limit).to_list()
        for d in docs:
            ts = d["ts"]
            d["ts"] = (ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)).isoformat()
        return docs

    # ------------------------------------------------------------------ #
    # RIPEstat visibility
    # ------------------------------------------------------------------ #
    async def refresh_visibility(self) -> None:
        async with httpx.AsyncClient(timeout=30) as client:
            for own in self.own:
                try:
                    r = await client.get(f"{self.settings.ripestat_url.rstrip('/')}/routing-status/data.json",
                                         params={"resource": str(own), "sourceapp": CLIENT_NAME})
                    r.raise_for_status()
                    data = r.json().get("data") or {}
                    vis = (data.get("visibility") or {}).get(f"v{own.version}") or {}
                    seeing, total = vis.get("ris_peers_seeing"), vis.get("total_ris_peers")
                    origins = sorted({int(o["origin"]) for o in data.get("origins") or []
                                      if str(o.get("origin", "")).isdigit()})
                    self.visibility[str(own)] = {
                        "ris_peers_seeing": seeing, "total_ris_peers": total,
                        "ratio": round(seeing / total, 3) if seeing is not None and total else None,
                        "origins": origins,
                        "foreign_origins": [o for o in origins if o not in self.allowed],
                        "updated": _now().isoformat()}
                except Exception as err:  # noqa: BLE001
                    log.warning("RIPEstat routing-status for %s failed: %s", own, err)

    async def visibility_loop(self) -> None:
        while True:
            await self.refresh_visibility()
            await asyncio.sleep(15 * 60)

    # ------------------------------------------------------------------ #
    # RIS Live connection
    # ------------------------------------------------------------------ #
    async def run(self) -> None:
        try:
            await self.db["ris_events"].create_index("ts", expireAfterSeconds=self.settings.ris_live_event_days * 86400)
        except Exception as err:  # noqa: BLE001
            log.warning("Could not create RIS event index: %s", err)
        visibility = asyncio.create_task(self.visibility_loop())
        delay = 5
        url = f"{self.settings.ris_live_url}?client={CLIENT_NAME}"
        try:
            while True:
                try:
                    async with websockets.connect(url, ping_interval=30, open_timeout=30) as ws:
                        for own in self.own:
                            await ws.send(json.dumps({"type": "ris_subscribe", "data": {
                                "prefix": str(own), "moreSpecific": True, "type": "UPDATE",
                                "socketOptions": {"includeRaw": False}}}))
                        # missed withdrawals while disconnected: start from scratch; RIPEstat
                        # (refresh_visibility) still reports origins seen right now
                        self.state.clear()
                        self.connected, self.error, delay = True, None, 5
                        log.info("RIS Live connected, watching %d prefixes", len(self.own))
                        async for raw in ws:
                            try:
                                await self.handle(json.loads(raw))
                            except (ValueError, TypeError, KeyError) as err:
                                log.debug("Bad RIS message: %s", err)
                except asyncio.CancelledError:
                    raise
                except Exception as err:  # noqa: BLE001 - reconnect
                    self.error = f"{type(err).__name__}: {err}"
                    log.warning("RIS Live connection lost: %s (retry in %ss)", err, delay)
                self.connected = False
                await asyncio.sleep(delay)
                delay = min(delay * 2, 300)
        finally:
            visibility.cancel()
