"""Parser tests (no database needed) plus an end-to-end run against MongoDB."""
import json

import pytest

import gobgp_to_mongo as c
from conftest import MONGO_URI, SAMPLE

# Real output of `gobgp monitor global rib --current -j` (GoBGP v4.11)
V4_LINE = json.dumps([
    {"Family": 0, "nlri": {"prefix": "10.10.0.0/16"}, "age": 1795343611, "best": False,
     "attrs": [{"type": 1, "value": 2}, {"type": 2, "as_paths": []}, {"type": 3, "nexthop": "192.0.2.2"},
               {"type": 5, "value": 100}], "stale": False, "peer-id": "10.0.0.1", "peer-address": "127.0.0.1"},
    {"Family": 0, "nlri": {"prefix": "1.2.3.0/24"}, "age": 1795343611, "best": False,
     "attrs": [{"type": 1, "value": 0},
               {"type": 2, "as_paths": [{"segment_type": 2, "num": 3, "asns": [3356, 6762, 55330]}]},
               {"type": 3, "nexthop": "192.0.2.1"}, {"type": 4, "metric": 10}, {"type": 5, "value": 200},
               {"type": 8, "communities": [242549116, 242548847]},
               {"type": 32, "value": [{"ASN": 3701, "LocalData1": 1, "LocalData2": 2}]}],
     "stale": False, "peer-id": "10.0.0.1", "peer-address": "127.0.0.1"},
    {"Family": 0, "nlri": {"prefix": "2001:db8::/32"}, "age": 1795343611,
     "attrs": [{"type": 1, "value": 2},
               {"type": 2, "as_paths": [{"segment_type": 2, "num": 2, "asns": [6939, 13335]}]},
               {"type": 5, "value": 100}, {"type": 8, "communities": [242549126]},
               {"type": 14, "nexthop": "2001:db8::1", "afi": 2, "safi": 1,
                "value": [{"NLRI": {"prefix": "2001:db8::/32"}, "ID": 0}]}]},
])


def test_parse_line_returns_all_paths():
    assert [p["nlri"]["prefix"] for p in c.parse_line(V4_LINE)] == ["10.10.0.0/16", "1.2.3.0/24", "2001:db8::/32"]


def test_parse_line_ignores_garbage():
    assert c.parse_line("") == []
    assert c.parse_line("not json") == []
    assert c.parse_line('[{"error": "x"}]') == []


@pytest.mark.parametrize("number,expected", [
    (242549116, "3701:380"), (65281, "0:65281"), (4294967041, "65535:65281"), (1, "0:1")])
def test_community_conversion(number, expected):
    assert c.community_32bit_to_string(number) == expected


def test_build_json():
    local, route, v6 = (c.build_json(p) for p in c.parse_line(V4_LINE))
    assert local["origin_asn"] is None and local["as_path"] == [] and local["nexthop"] == "192.0.2.2"
    assert route["as_path"] == [3356, 6762, 55330]
    assert (route["nexthop_asn"], route["origin_asn"]) == (3356, 55330)
    assert route["communities"] == ["3701:380", "3701:111", "3701:1:2"]
    assert (route["med"], route["local_pref"], route["route_origin"]) == (10, 200, "IGP")
    assert route["age"].endswith("UTC") and route["active"] is True
    assert v6["ip_version"] == 6 and v6["nexthop"] == "2001:db8::1"


def test_as_set_and_confed_segments():
    entry = {"nlri": {"prefix": "192.0.2.0/24"}, "attrs": [{"type": 2, "as_paths": [
        {"segment_type": 3, "asns": [65001]}, {"segment_type": 2, "asns": [1, 2]}, {"segment_type": 1, "asns": [3, 4]}]}]}
    doc = c.build_json(entry)
    assert doc["as_path"] == [1, 2, 3, 4] and doc["nexthop_asn"] == 1


def test_withdrawal_marks_inactive():
    doc = c.build_json({"nlri": {"prefix": "192.0.2.0/24"}, "attrs": [], "withdrawal": True})
    assert doc["active"] is False and doc["withdrawal"] is True


# --------------------------------------------------------------------------- #
# MongoDB based tests
# --------------------------------------------------------------------------- #
@pytest.fixture
def db():
    pymongo = pytest.importorskip("pymongo")
    client = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=1500)
    try:
        client.admin.command("ping")
    except Exception:
        pytest.skip(f"MongoDB not reachable at {MONGO_URI}")
    database = client["bgp_test_collector"]
    client.drop_database(database.name)
    c.initialize_database(database)
    yield database
    client.drop_database(database.name)


def _line(prefix, path, communities=(), withdrawal=False, age=1700000000):
    entry = {"nlri": {"prefix": prefix}, "age": age, "attrs": [
        {"type": 2, "as_paths": [{"segment_type": 2, "asns": path}]},
        {"type": 3, "nexthop": "192.0.2.1"}, {"type": 8, "communities": list(communities)}]}
    if withdrawal:
        entry["withdrawal"] = True
    return json.dumps([entry])


def test_history_and_withdrawal_flow(db):
    p = "198.51.100.0/24"
    c.process_batch(db, [_line(p, [1, 2, 3])])
    c.process_batch(db, [_line(p, [1, 2, 3], age=1700000100)])           # unchanged
    doc = db.bgp.find_one({"_id": p})
    assert doc["active"] and doc["history"] == []                        # history not wiped / created

    c.process_batch(db, [_line(p, [4, 3], age=1700000200)])               # path change
    c.process_batch(db, [_line(p, [4, 3], withdrawal=True, age=1700000300)])
    doc = db.bgp.find_one({"_id": p})
    assert doc["active"] is False and doc["as_path"] == [4, 3]           # attributes kept on withdraw
    assert [h["as_path"] for h in doc["history"]] == [[4, 3], [1, 2, 3]]

    c.process_batch(db, [_line(p, [4, 3], withdrawal=True, age=1700000400)])  # repeated withdraw
    assert db.bgp.find_one({"_id": p})["active"] is False

    c.process_batch(db, [_line(p, [4, 3], age=1700000500)])               # re-announce
    doc = db.bgp.find_one({"_id": p})
    assert doc["active"] is True and len(doc["history"]) == 3


def test_same_prefix_twice_in_one_batch(db):
    p = "203.0.113.0/24"
    c.process_batch(db, [_line(p, [1]), _line(p, [2], age=1700000001)])
    doc = db.bgp.find_one({"_id": p})
    assert doc["as_path"] == [2] and doc["history"][0]["as_path"] == [1]


def test_history_is_capped(db, monkeypatch):
    monkeypatch.setattr(c, "MAX_PREFIX_HISTORY", 3)
    p = "192.0.2.0/24"
    c.process_batch(db, [_line(p, [i], age=1700000000 + i) for i in range(1, 8)])
    assert len(db.bgp.find_one({"_id": p})["history"]) == 3


def test_sample_data(db):
    lines = SAMPLE.read_text().splitlines()
    total = sum(c.process_batch(db, lines[i:i + 500]) for i in range(0, len(lines), 500))
    assert total == len(lines)
    assert db.bgp.count_documents({"active": True}) > 1000
    assert db.bgp.count_documents({"communities": None}) == 0
