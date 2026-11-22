#!/usr/bin/env python3
"""Read ``gobgp monitor global rib -j`` output from stdin and store it in MongoDB.

Every line from GoBGP is a JSON list of one or more paths. Lines are collected
into small batches (up to ``BATCH_SIZE`` paths or ``BATCH_WAIT`` seconds) and
written with a single ``bulk_write`` per batch, which keeps up with a full
table dump after a session reset.

Each prefix is one document (``_id`` = prefix). When the attributes of a prefix
change, the previous version is pushed onto the embedded ``history`` list
(newest first, capped at ``MAX_PREFIX_HISTORY`` entries).
"""

from __future__ import annotations

import json
import logging
import os
import queue
import sys
import threading
import time
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone

import ipaddress
from pymongo import ASCENDING, MongoClient, UpdateOne
from pymongo.database import Database
from pymongo.errors import PyMongoError

import bgp_attributes as BGP

log = logging.getLogger("gobgp_to_mongo")

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://mongodb:27017")
MONGO_DB = os.environ.get("MONGO_DB", "bgp")
# 0 = unlimited (BGP flapping will likely blow up the DB if unlimited)
MAX_PREFIX_HISTORY = int(os.environ.get("MAX_PREFIX_HISTORY", "100"))
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "2000"))
BATCH_WAIT = float(os.environ.get("BATCH_WAIT", "0.5"))

# Keys that are bookkeeping only and are ignored when comparing two versions of a route.
_IGNORED_ON_COMPARE = {"_id", "history", "age", "active"}


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
def db_connect(uri: str = MONGO_URI, name: str = MONGO_DB, retries: int = 30) -> Database:
    """Return a handle to the Mongo database, waiting until MongoDB is reachable."""
    client: MongoClient = MongoClient(uri, serverSelectionTimeoutMS=5000, tz_aware=True)
    for attempt in range(1, retries + 1):
        try:
            client.admin.command("ping")
            return client[name]
        except PyMongoError as err:
            log.warning("MongoDB not reachable (attempt %d/%d): %s", attempt, retries, err)
            time.sleep(2)
    raise SystemExit("Giving up: MongoDB is not reachable")


def initialize_database(db: Database) -> None:
    """Create indexes and mark all known prefixes inactive.

    GoBGP starts with an empty RIB, so every route that is still valid is
    announced again (``--current`` dumps the existing RIB first).
    """
    coll = db["bgp"]
    coll.create_index("nexthop")
    coll.create_index("nexthop_asn")
    coll.create_index([("nexthop", ASCENDING), ("active", ASCENDING)])
    coll.create_index([("nexthop_asn", ASCENDING), ("active", ASCENDING)])
    coll.create_index([("ip_version", ASCENDING), ("active", ASCENDING)])
    coll.create_index([("origin_asn", ASCENDING), ("ip_version", ASCENDING), ("active", ASCENDING)])
    coll.create_index([("communities", ASCENDING), ("active", ASCENDING)])
    coll.create_index([("as_path.1", ASCENDING), ("nexthop_asn", ASCENDING), ("active", ASCENDING)])
    result = coll.update_many({"active": True}, {"$set": {"active": False}})
    log.info("Database initialised, %d prefixes marked inactive", result.modified_count)


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def parse_line(line: str) -> list[dict]:
    """Return all paths contained in one line of GoBGP JSON output."""
    line = line.strip()
    if not line:
        return []
    try:
        data = json.loads(line)
    except json.JSONDecodeError as err:
        log.warning("Skipping invalid JSON line (%s): %.200s", err, line)
        return []
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []
    return [entry for entry in data
            if isinstance(entry, dict) and "error" not in entry and "nlri" in entry]


def community_32bit_to_string(number: int) -> str:
    """Convert a 32bit standard community to ``ASN:VALUE``."""
    return f"{number >> 16}:{number & 0xFFFF}"


def community_large_to_string(community: dict) -> str:
    """Convert a large community dict to ``ASN:DATA1:DATA2``."""
    return f"{community['ASN']}:{community['LocalData1']}:{community['LocalData2']}"


def _format_age(epoch: int | float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def build_json(update_entry: dict) -> dict:
    """Turn one GoBGP path into the document stored in MongoDB."""
    prefix = update_entry["nlri"]["prefix"]
    doc = {
        "_id": prefix,
        "ip_version": ipaddress.ip_network(prefix, strict=False).version,
        "origin_asn": None,
        "nexthop": None,
        "nexthop_asn": None,
        "as_path": [],
        "med": 0,
        "local_pref": 0,
        "communities": [],
        "route_origin": None,
        "atomic_aggregate": None,
        "aggregator_as": None,
        "aggregator_address": None,
        "originator_id": None,
        "cluster_list": [],
        "withdrawal": False,
        "age": 0,
        "active": True,
    }
    for attribute in update_entry.get("attrs") or []:
        attr_type = attribute.get("type")
        try:
            if attr_type == BGP.ORIGIN:
                doc["route_origin"] = BGP.ORIGIN_CODE.get(attribute["value"])
            elif attr_type == BGP.AS_PATH:
                as_path: list[int] = []
                for segment in attribute.get("as_paths") or []:
                    # Only AS_SET / AS_SEQUENCE, confederation segments are local.
                    if segment.get("segment_type") in (BGP.AS_SET, BGP.AS_SEQUENCE):
                        as_path.extend(segment.get("asns") or [])
                doc["as_path"] = as_path
                if as_path:
                    doc["nexthop_asn"] = as_path[0]
                    doc["origin_asn"] = as_path[-1]
            elif attr_type == BGP.NEXT_HOP:
                doc["nexthop"] = attribute["nexthop"]
            elif attr_type == BGP.MULTI_EXIT_DISC:
                doc["med"] = attribute.get("metric", 0)
            elif attr_type == BGP.LOCAL_PREF:
                doc["local_pref"] = attribute.get("value", 0)
            elif attr_type == BGP.ATOMIC_AGGREGATE:
                doc["atomic_aggregate"] = True
            elif attr_type == BGP.AGGREGATOR:
                doc["aggregator_as"] = attribute.get("as")
                doc["aggregator_address"] = attribute.get("address")
            elif attr_type == BGP.COMMUNITY:
                doc["communities"].extend(  # 0 is reserved / no community
                    community_32bit_to_string(n) for n in attribute.get("communities") or [] if n)
            elif attr_type == BGP.ORIGINATOR_ID:
                doc["originator_id"] = attribute.get("value")
            elif attr_type == BGP.CLUSTER_LIST:
                doc["cluster_list"] = attribute.get("value") or []
            elif attr_type == BGP.MP_REACH_NLRI:
                doc["nexthop"] = attribute.get("nexthop")
            elif attr_type == BGP.LARGE_COMMUNITIES:
                doc["communities"].extend(
                    community_large_to_string(c) for c in attribute.get("value") or [])
        except (KeyError, TypeError, ValueError) as err:
            log.debug("Error processing attribute %s of %s: %s", attribute, prefix, err)

    if update_entry.get("withdrawal"):
        doc["withdrawal"] = True
        doc["active"] = False
    if "age" in update_entry:
        doc["age"] = _format_age(update_entry["age"])
    return doc


def same_route(new: dict, old: dict) -> bool:
    """Compare two versions of a prefix, ignoring bookkeeping fields."""
    keys = (new.keys() | old.keys()) - _IGNORED_ON_COMPARE
    return all(new.get(k) == old.get(k) for k in keys)


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #
def _history_push(old: dict) -> dict:
    entry = {k: v for k, v in old.items() if k not in ("history", "active")}
    spec: dict = {"$each": [entry], "$position": 0}
    if MAX_PREFIX_HISTORY > 0:
        spec["$slice"] = MAX_PREFIX_HISTORY
    return {"history": spec}


def plan_update(new: dict, old: dict | None) -> tuple[UpdateOne, dict]:
    """Return the write operation for *new* and the resulting stored state."""
    if new["withdrawal"] and old is not None:
        # Withdrawals only flip the state, the last known attributes are kept.
        new = {**old, "withdrawal": True, "active": False, "age": new["age"] or old.get("age")}

    if old is None:
        update = {"$set": new, "$setOnInsert": {"history": []}}
    elif same_route(new, old):
        update = {"$set": {"active": new["active"], "age": new["age"]}}
    else:
        update = {"$set": new, "$push": _history_push(old)}
    return UpdateOne({"_id": new["_id"]}, update, upsert=True), new


def process_batch(db: Database, lines: Iterable[str]) -> int:
    """Parse *lines* and write all contained paths. Returns the number of paths."""
    routes = []
    for line in lines:
        for entry in parse_line(line):
            try:
                routes.append(build_json(entry))
            except (KeyError, TypeError, ValueError) as err:
                log.warning("Skipping unparsable path (%s): %.200s", err, entry)
    if not routes:
        return 0

    coll = db["bgp"]
    prefixes = list({r["_id"] for r in routes})
    current = {doc["_id"]: doc for doc in coll.find({"_id": {"$in": prefixes}}, {"history": 0})}

    operations = []
    for route in routes:  # keep order: the same prefix can occur several times per batch
        op, state = plan_update(route, current.get(route["_id"]))
        operations.append(op)
        current[route["_id"]] = state
    coll.bulk_write(operations, ordered=True)
    return len(routes)


def batched_lines(stream: Iterable[str], max_size: int = BATCH_SIZE,
                  max_wait: float = BATCH_WAIT) -> Iterator[list[str]]:
    """Yield lists of lines; a batch is closed when it is full or *max_wait* passed."""
    q: queue.Queue = queue.Queue(maxsize=max_size * 20)
    end = object()

    def reader() -> None:
        try:
            for line in stream:
                q.put(line)
        finally:
            q.put(end)

    threading.Thread(target=reader, name="stdin-reader", daemon=True).start()
    while True:
        item = q.get()
        if item is end:
            return
        batch = [item]
        deadline = time.monotonic() + max_wait
        while len(batch) < max_size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                item = q.get(timeout=remaining)
            except queue.Empty:
                break
            if item is end:
                yield batch
                return
            batch.append(item)
        yield batch


def main() -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    db = db_connect()
    initialize_database(db)
    total = 0
    last_report = time.monotonic()
    for batch in batched_lines(sys.stdin):
        total += process_batch(db, batch)
        if time.monotonic() - last_report > 60:
            log.info("%d paths processed so far", total)
            last_report = time.monotonic()
    log.warning("Input stream closed after %d paths", total)
    return 1  # GoBGP monitor ended: let the container restart


if __name__ == "__main__":
    sys.exit(main())
