"""Poll the BGP session state from gobgpd and store it in MongoDB.

Runs as a thread inside the collector process. Every ``interval`` seconds
``gobgp neighbor -j`` is executed and

* ``neighbors`` gets one document per configured neighbor (state, uptime,
  flaps, received/accepted prefixes per address family). ``session_drops``
  counts how often an established session was lost (GoBGP's own ``flops``
  counter does not count every drop),
* ``neighbor_events`` gets an entry for every state change (also for a
  session that went down and came back between two polls),
* ``collector_status`` gets a heartbeat, so the dashboard can tell a dead
  collector from a quiet network.
"""

from __future__ import annotations

import json
import logging
import shlex
import subprocess
import threading
from datetime import datetime, timezone

from pymongo import ASCENDING, ReplaceOne
from pymongo.database import Database

log = logging.getLogger("neighbor_poller")

# Enum values of the GoBGP v4 API (api.PeerState)
SESSION_STATES = {0: "unknown", 1: "idle", 2: "connect", 3: "active",
                  4: "opensent", 5: "openconfirm", 6: "established"}
ADMIN_STATES = {0: "unknown", 1: "up", 2: "down", 3: "pfx_ct"}
DISCONNECT_REASONS = {
    0: None, 1: "admin down", 2: "hold timer expired", 3: "notification sent",
    4: "notification received", 5: "read failed", 6: "write failed", 7: "idle timer expired",
    8: "restart timer expired", 9: "graceful restart", 10: "invalid message", 11: "hard reset",
    12: "deconfigured", 13: "bad peer AS",
}
FAMILIES = {(1, 1): "ipv4", (2, 1): "ipv6"}


def _enum(value, mapping: dict, prefix: str):
    if isinstance(value, str):  # tolerate enum names instead of numbers
        name = value.lower()
        return name[len(prefix):] if name.startswith(prefix) else name
    return mapping.get(value or 0, str(value))


def _timestamp(value) -> datetime | None:
    seconds = (value or {}).get("seconds") if isinstance(value, dict) else None
    return datetime.fromtimestamp(int(seconds), tz=timezone.utc) if seconds else None


def parse_neighbors(data) -> list[dict]:
    """Turn the output of ``gobgp neighbor -j`` into neighbor documents."""
    result = []
    for peer in data or []:
        conf = peer.get("conf") or {}
        state = peer.get("state") or {}
        timers = (peer.get("timers") or {}).get("state") or {}
        address = conf.get("neighbor_address") or state.get("neighbor_address")
        if not address:
            continue
        session = _enum(state.get("session_state"), SESSION_STATES, "session_state_")
        families = {}
        for afi_safi in peer.get("afi_safis") or []:
            af_state = afi_safi.get("state") or {}
            family = af_state.get("family") or (afi_safi.get("config") or {}).get("family") or {}
            key = (family.get("afi", 0), family.get("safi", 0))
            families[FAMILIES.get(key, f"afi{key[0]}-safi{key[1]}")] = {
                "received": af_state.get("received", 0),
                "accepted": af_state.get("accepted", 0),
            }
        messages = state.get("messages") or {}
        received = messages.get("received") or {}
        reason = state.get("disconnect_reason")
        result.append({
            "_id": address,
            "address": address,
            "peer_asn": conf.get("peer_asn") or state.get("peer_asn"),
            "local_asn": conf.get("local_asn") or state.get("local_asn"),
            "description": conf.get("description") or state.get("description") or "",
            "router_id": state.get("router_id"),
            "state": session,
            "established": session == "established",
            "admin_state": _enum(state.get("admin_state"), ADMIN_STATES, "admin_state_"),
            "flaps": state.get("flops", 0),
            "uptime_since": _timestamp(timers.get("uptime")),
            "downtime_since": _timestamp(timers.get("downtime")),
            "disconnect_reason": (_enum(reason, DISCONNECT_REASONS, "disconnect_reason_")
                                  if reason else None),
            "disconnect_message": state.get("disconnect_message") or None,
            "families": families,
            "messages_received": received.get("total", 0),
            "updates_received": received.get("update", 0),
            "messages_sent": (messages.get("sent") or {}).get("total", 0),
        })
    return result


def ensure_indexes(db: Database, event_days: int) -> None:
    db["neighbor_events"].create_index("ts", expireAfterSeconds=event_days * 86400)
    db["neighbor_events"].create_index([("neighbor", ASCENDING), ("ts", ASCENDING)])


def store_neighbors(db: Database, neighbors: list[dict], now: datetime,
                    missing_polls: dict[str, int] | None = None,
                    remove_after: int = 1) -> list[dict]:
    """Replace the neighbor documents and record state changes. Returns the events.

    A neighbor that disappears is only removed after *remove_after* consecutive
    polls without it (gobgpd briefly reports no neighbors while it starts).
    """
    missing_polls = {} if missing_polls is None else missing_polls
    previous = {doc["_id"]: doc for doc in db["neighbors"].find({})}
    events, ops = [], []
    for current in neighbors:
        old = previous.get(current["_id"])
        changed = old is None or old.get("state") != current["state"]
        # Session went down and up again between two polls
        reset = (not changed and current["established"] and old.get("established")
                 and current["uptime_since"] and old.get("uptime_since")
                 and int(current["uptime_since"].timestamp()) != int(old["uptime_since"].timestamp()))
        went_down = bool(old and old.get("established") and not current["established"])
        current["session_drops"] = (old or {}).get("session_drops", 0) + int(went_down or bool(reset))
        if changed or reset:
            events.append({
                "ts": now,
                "neighbor": current["_id"],
                "peer_asn": current["peer_asn"],
                "description": current["description"],
                "from_state": old.get("state") if old else None,
                "to_state": current["state"],
                "reason": current["disconnect_reason"] if not current["established"] else
                          ("re-established between polls" if reset else None),
                "message": current["disconnect_message"] if not current["established"] else None,
            })
        ops.append(ReplaceOne({"_id": current["_id"]}, {**current, "updated": now}, upsert=True))
    present = {n["_id"] for n in neighbors}
    for address in present:
        missing_polls.pop(address, None)
    removed = set()
    for address in set(previous) - present:
        missing_polls[address] = missing_polls.get(address, 0) + 1
        if missing_polls[address] >= remove_after:
            removed.add(address)
            missing_polls.pop(address)
    for address in removed:
        old = previous[address]
        events.append({"ts": now, "neighbor": address, "peer_asn": old.get("peer_asn"),
                       "description": old.get("description", ""), "from_state": old.get("state"),
                       "to_state": "removed", "reason": "removed from configuration", "message": None})
    if ops:
        db["neighbors"].bulk_write(ops, ordered=False)
    if removed:
        db["neighbors"].delete_many({"_id": {"$in": list(removed)}})
    if events:
        db["neighbor_events"].insert_many(events)
        for e in events:
            log.info("Neighbor %s (AS%s): %s -> %s %s", e["neighbor"], e["peer_asn"],
                     e["from_state"], e["to_state"], e["reason"] or "")
    return events


class NeighborPoller(threading.Thread):
    def __init__(self, db: Database, interval: float = 10, command: str = "gobgp",
                 event_days: int = 90):
        super().__init__(name="neighbor-poller", daemon=True)
        self.db = db
        self.interval = interval
        self.command = shlex.split(command)
        self.event_days = event_days
        self.stop_event = threading.Event()
        self._last_error: str | None = None
        self._missing: dict[str, int] = {}

    def poll(self) -> list[dict]:
        proc = subprocess.run([*self.command, "neighbor", "-j"], capture_output=True,
                              text=True, timeout=15, check=False)
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout).strip()[:300] or f"exit {proc.returncode}")
        data = json.loads(proc.stdout or "null")
        return parse_neighbors([data] if isinstance(data, dict) else data)

    def run_once(self) -> None:
        now = datetime.now(timezone.utc)
        status: dict = {"heartbeat": now}
        try:
            store_neighbors(self.db, self.poll(), now, self._missing, remove_after=3)
            status.update(neighbors_polled=now, neighbor_error=None)
            if self._last_error:
                log.info("Neighbor polling works again")
            self._last_error = None
        except Exception as err:  # noqa: BLE001 - keep polling
            message = f"{type(err).__name__}: {err}"
            if message != self._last_error:
                log.warning("Polling neighbors failed: %s", message)
            self._last_error = message
            status["neighbor_error"] = message
        self.db["collector_status"].update_one({"_id": "collector"}, {"$set": status}, upsert=True)

    def run(self) -> None:
        try:
            ensure_indexes(self.db, self.event_days)
        except Exception as err:  # noqa: BLE001
            log.warning("Could not create neighbor indexes: %s", err)
        while not self.stop_event.is_set():
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 - e.g. MongoDB temporarily away
                log.exception("Neighbor poller iteration failed")
            self.stop_event.wait(self.interval)
