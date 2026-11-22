"""Collector side of the monitoring features: change tracking, range fields, BGP sessions."""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import gobgp_to_mongo as c
import neighbor_poller as npoll
from conftest import MONGO_URI

FIXTURES = Path(__file__).parent / "fixtures"


def _line(prefix, path, withdrawal=False, age=1700000000):
    entry = {"nlri": {"prefix": prefix}, "age": age, "attrs": [
        {"type": 2, "as_paths": [{"segment_type": 2, "asns": path}]}, {"type": 3, "nexthop": "192.0.2.1"}]}
    if withdrawal:
        entry["withdrawal"] = True
    return json.dumps([entry])


@pytest.fixture
def db():
    pymongo = pytest.importorskip("pymongo")
    client = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=1500, tz_aware=True)
    try:
        client.admin.command("ping")
    except Exception:
        pytest.skip(f"MongoDB not reachable at {MONGO_URI}")
    database = client["bgp_test_monitoring"]
    client.drop_database(database.name)
    c.initialize_database(database)
    yield database
    client.drop_database(database.name)


# --------------------------------------------------------------------------- range fields
@pytest.mark.parametrize("prefix,start,length", [
    ("10.0.0.0/8", "0a000000", 8),
    ("192.0.2.128/25", "c0000280", 25),
    ("2001:db8::/32", "20010db8000000000000000000000000", 32),
])
def test_net_fields(prefix, start, length):
    assert c.net_fields(prefix) == {"net_start": start, "prefix_len": length}


def test_backfill_range_fields(db):
    db.bgp.insert_many([{"_id": "198.51.100.0/24", "active": True}, {"_id": "2001:db8::/48"}])
    assert c.backfill_range_fields(db) == 2
    assert db.bgp.find_one({"_id": "198.51.100.0/24"})["net_start"] == "c6336400"
    assert c.backfill_range_fields(db) == 0


# --------------------------------------------------------------------------- change tracking
def test_change_tracking(db):
    p = "198.51.100.0/24"
    c.process_batch(db, [_line(p, [1, 2])])
    doc = db.bgp.find_one({"_id": p})
    assert "last_change" not in doc and doc["first_seen"]            # first appearance is no change
    c.process_batch(db, [_line(p, [1, 2], age=1700000005)])           # identical re-announcement
    assert "change_times" not in db.bgp.find_one({"_id": p})
    c.process_batch(db, [_line(p, [3, 2])])                             # path change
    c.process_batch(db, [_line(p, [3, 2], withdrawal=True)])            # withdraw
    c.process_batch(db, [_line(p, [3, 2])])                             # re-announce
    doc = db.bgp.find_one({"_id": p})
    assert len(doc["change_times"]) == 3 and doc["last_change"] == doc["change_times"][-1]
    for entry in doc["history"]:                                        # no bookkeeping in history
        assert not {"change_times", "last_change", "first_seen", "net_start", "active"} & entry.keys()
    status = db.collector_status.find_one({"_id": "collector"})
    assert status["paths_total"] == 5 and status["route_changes_total"] == 3
    assert status["withdrawals_total"] == 1 and status["last_update"]


def test_change_times_are_capped(db, monkeypatch):
    monkeypatch.setattr(c, "MAX_CHANGE_TIMES", 5)
    c.process_batch(db, [_line("192.0.2.0/24", [i]) for i in range(1, 12)])
    assert len(db.bgp.find_one({"_id": "192.0.2.0/24"})["change_times"]) == 5


def test_upgrade_does_not_create_fake_history(db):
    """Documents of the previous version (no range/bookkeeping fields) must compare equal."""
    doc = c.build_json(json.loads(_line("203.0.113.0/24", [7, 8]))[0])
    old = {k: v for k, v in doc.items() if k not in ("net_start", "prefix_len")}
    db.bgp.insert_one({**old, "history": [], "active": False})
    c.process_batch(db, [_line("203.0.113.0/24", [7, 8])])
    stored = db.bgp.find_one({"_id": "203.0.113.0/24"})
    assert stored["history"] == [] and stored["active"] and stored["net_start"] == "cb007100"


# --------------------------------------------------------------------------- BGP sessions
def test_parse_neighbor_up():
    [n] = npoll.parse_neighbors(json.loads((FIXTURES / "gobgp_neighbor_up.json").read_text()))
    assert n["_id"] == "127.0.0.1" and n["peer_asn"] == 3701
    assert n["state"] == "established" and n["established"] and n["admin_state"] == "up"
    assert n["families"] == {"ipv4": {"received": 1, "accepted": 1}, "ipv6": {"received": 1, "accepted": 1}}
    assert n["uptime_since"].tzinfo is not None and n["downtime_since"] is None


def test_parse_neighbor_down():
    [n] = npoll.parse_neighbors(json.loads((FIXTURES / "gobgp_neighbor_down.json").read_text()))
    assert n["state"] == "active" and not n["established"]
    assert n["downtime_since"] > n["uptime_since"]


def test_parse_enum_names_and_empty_output():
    assert npoll.parse_neighbors(None) == []
    [n] = npoll.parse_neighbors([{"conf": {"neighbor_address": "192.0.2.1"},
                                  "state": {"session_state": "SESSION_STATE_IDLE", "admin_state": "ADMIN_STATE_DOWN",
                                            "disconnect_reason": 2}}])
    assert (n["state"], n["admin_state"], n["disconnect_reason"]) == ("idle", "down", "hold timer expired")


def _neighbor(state="established", uptime=1000, address="192.0.2.1"):
    return {"_id": address, "address": address, "peer_asn": 64500, "description": "test", "state": state,
            "established": state == "established", "admin_state": "up", "flaps": 0,
            "uptime_since": datetime.fromtimestamp(uptime, tz=timezone.utc), "downtime_since": None,
            "disconnect_reason": None if state == "established" else "hold timer expired",
            "disconnect_message": None, "families": {}}


def test_store_neighbors_events_and_drops(db):
    t = datetime(2026, 1, 1, tzinfo=timezone.utc)
    npoll.store_neighbors(db, [_neighbor()], t)
    npoll.store_neighbors(db, [_neighbor()], t + timedelta(seconds=10))          # no change
    npoll.store_neighbors(db, [_neighbor("active")], t + timedelta(seconds=20))  # down
    npoll.store_neighbors(db, [_neighbor(uptime=2000)], t + timedelta(seconds=30))  # up again
    npoll.store_neighbors(db, [_neighbor(uptime=3000)], t + timedelta(seconds=40))  # reset between polls
    events = list(db.neighbor_events.find({}, {"_id": 0}).sort("ts", 1))
    assert [(e["from_state"], e["to_state"]) for e in events] == [
        (None, "established"), ("established", "active"), ("active", "established"),
        ("established", "established")]
    assert events[1]["reason"] == "hold timer expired"
    assert events[3]["reason"] == "re-established between polls"
    assert db.neighbors.find_one()["session_drops"] == 2


def test_store_neighbors_removal_needs_confirmation(db):
    t = datetime(2026, 1, 1, tzinfo=timezone.utc)
    missing: dict = {}
    npoll.store_neighbors(db, [_neighbor()], t, missing, remove_after=3)
    for i in range(2):  # gobgpd restarting: briefly no neighbors
        npoll.store_neighbors(db, [], t, missing, remove_after=3)
    assert db.neighbors.count_documents({}) == 1
    npoll.store_neighbors(db, [_neighbor()], t, missing, remove_after=3)
    assert missing == {}
    for i in range(3):
        npoll.store_neighbors(db, [], t, missing, remove_after=3)
    assert db.neighbors.count_documents({}) == 0
    assert db.neighbor_events.find_one({"to_state": "removed"})


def test_poller_run_once(db, tmp_path):
    fake = tmp_path / "gobgp"
    fake.write_text(f"#!{sys.executable}\nimport sys\nsys.stdout.write(open({str(FIXTURES / 'gobgp_neighbor_up.json')!r}).read())\n")
    fake.chmod(0o755)
    npoll.NeighborPoller(db, command=str(fake)).run_once()
    status = db.collector_status.find_one({"_id": "collector"})
    assert status["neighbor_error"] is None and status["neighbors_polled"] and status["heartbeat"]
    assert db.neighbors.find_one()["state"] == "established"


def test_poller_reports_errors_but_keeps_heartbeat(db):
    npoll.NeighborPoller(db, command="/nonexistent/gobgp").run_once()
    status = db.collector_status.find_one({"_id": "collector"})
    assert "FileNotFoundError" in status["neighbor_error"] and status["heartbeat"]
    assert "neighbors_polled" not in status
