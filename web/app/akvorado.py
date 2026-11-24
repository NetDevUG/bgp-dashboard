"""Optional traffic data from Akvorado (ClickHouse HTTP interface).

Outgoing traffic per destination AS over the last ``akvorado_window_hours``:
``SUM(Bytes*SamplingRate*8)`` of flows leaving through an external interface
(``OutIfBoundary = 'external'``), read from a consolidated Akvorado table
(default ``flows_5m0s``). The dashboard uses it to rank "via transit although
peered" and the peering suggestions by traffic instead of prefix counts.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone

import httpx

from .config import Settings

log = logging.getLogger(__name__)
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_ASNS = 20000


def build_query(settings: Settings) -> str:
    table = settings.akvorado_table
    if not _IDENTIFIER.match(table):
        raise ValueError(f"invalid Akvorado table name: {table!r}")
    seconds = int(settings.akvorado_window_hours * 3600)
    return (
        "SELECT DstAS AS asn, SUM(Bytes * SamplingRate * 8) / {seconds} AS bps "
        "FROM {table} "
        "WHERE TimeReceived > now() - INTERVAL {seconds} SECOND "
        "AND OutIfBoundary = 'external' AND DstAS != 0 "
        "GROUP BY DstAS ORDER BY bps DESC LIMIT {limit} FORMAT JSON"
    ).format(seconds=seconds, table=table, limit=MAX_ASNS)


class AkvoradoService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.by_asn: dict[int, float] = {}
        self.data: dict = {"enabled": self.enabled, "ok": False, "error": None, "updated": None,
                           "window_hours": settings.akvorado_window_hours, "total_bps": 0, "top": []}

    @property
    def enabled(self) -> bool:
        return bool(self.settings.akvorado_clickhouse_url)

    async def query(self) -> dict[int, float]:
        headers = {}
        if self.settings.akvorado_user:
            headers["X-ClickHouse-User"] = self.settings.akvorado_user
        if self.settings.akvorado_password:
            headers["X-ClickHouse-Key"] = self.settings.akvorado_password
        # no settings in the URL: a read-only ClickHouse user (readonly=1) may not change any
        params = {"database": self.settings.akvorado_database}
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(self.settings.akvorado_clickhouse_url, params=params,
                                         headers=headers, content=build_query(self.settings))
            if response.status_code != 200:
                raise RuntimeError(f"ClickHouse HTTP {response.status_code}: {response.text[:300].strip()}")
            rows = response.json().get("data", [])
        return {int(r["asn"]): float(r["bps"]) for r in rows}

    async def refresh(self) -> None:
        started = time.monotonic()
        try:
            self.by_asn = await self.query()
            top = sorted(self.by_asn.items(), key=lambda kv: kv[1], reverse=True)[:20]
            self.data.update(ok=True, error=None, updated=datetime.now(timezone.utc).isoformat(),
                             total_bps=sum(self.by_asn.values()),
                             top=[{"asn": a, "bps": b} for a, b in top])
            log.info("Akvorado: traffic for %d ASNs in %.1fs", len(self.by_asn), time.monotonic() - started)
        except Exception as err:  # noqa: BLE001 - keep the last good data
            self.data.update(ok=False, error=f"{type(err).__name__}: {err}")
            log.warning("Akvorado query failed: %s", err)

    async def run(self) -> None:
        while True:
            await self.refresh()
            await asyncio.sleep(15 * 60)

    def traffic(self) -> dict[int, float] | None:
        """Traffic per destination AS, or None when Akvorado is not configured / not loaded yet."""
        return self.by_asn if self.enabled and self.by_asn else None
