"""Prometheus metrics in the text exposition format (no extra dependency)."""

from __future__ import annotations

import math
from datetime import datetime

from .config import Settings


def _escape(value) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _num(value) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        return repr(value)
    return str(int(value))


def _ts(value) -> float | None:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.timestamp() if isinstance(value, datetime) else None


class Exposition:
    """Collects metric families; samples of one family stay together."""

    def __init__(self):
        self._families: dict[str, tuple[str, str, list[str]]] = {}

    def add(self, name: str, value, labels: dict | None = None, *,
            help: str = "", kind: str = "gauge") -> None:
        if value is None:
            return
        family = self._families.setdefault(name, (kind, help, []))
        label_str = ""
        if labels:
            label_str = "{" + ",".join(f'{k}="{_escape(v)}"' for k, v in labels.items()) + "}"
        family[2].append(f"{name}{label_str} {_num(value)}")

    def render(self) -> str:
        lines = []
        for name, (kind, help_text, samples) in self._families.items():
            if help_text:
                lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {kind}")
            lines.extend(samples)
        return "\n".join(lines) + "\n"


def render_metrics(settings: Settings, stats: dict, ready: bool, last_error: str | None,
                   sessions: dict, collector: dict) -> str:
    m = Exposition()
    m.add("bgp_dashboard_info", 1, {"asn": settings.asn}, help="Dashboard information")
    m.add("bgp_stats_ready", ready, help="1 once the first statistics run finished")
    m.add("bgp_stats_error", last_error is not None, help="1 if the last statistics run failed")
    for kind in ("basic", "advanced"):
        m.add("bgp_stats_last_update_timestamp_seconds", _ts(stats.get(f"{kind}_updated")),
              {"kind": kind}, help="Time of the last statistics refresh")

    if ready:
        for v in (4, 6):
            m.add("bgp_prefixes", stats[f"ipv{v}_table_size"], {"ip_version": v},
                  help="Active prefixes in the BGP table")
        m.add("bgp_peer_asns", stats["peer_count"], help="Number of next-hop ASNs")
        m.add("bgp_nexthop_addresses", stats["nexthop_ip_count"], help="Unique next-hop addresses")
        m.add("bgp_as_path_length_avg", float(stats["avg_as_path_length"]),
              help="Average AS path length (prepends ignored)")
        if settings.customer_community:
            m.add("bgp_customers", stats["customer_count"], help="Number of customer ASNs")
            for v in (4, 6):
                m.add("bgp_customer_prefixes", stats[f"customer_ipv{v}_prefixes"], {"ip_version": v},
                      help="Prefixes originated by customers")
        for v in (4, 6):
            m.add("bgp_own_prefixes", stats[f"own_ipv{v}_prefixes"], {"ip_version": v},
                  help="Prefixes originated in the own AS")
        if stats.get("transit_percentage") is not None:
            m.add("bgp_transit_ratio", stats["transit_percentage"] / 100,
                  help="Share of prefixes tagged with the transit community")
        for peer in stats["peers"]:
            for v in (4, 6):
                m.add("bgp_peer_prefixes", peer[f"ipv{v}_nexthop_count"],
                      {"asn": peer["asn"], "ip_version": v},
                      help="Active prefixes per next-hop ASN")
        for c in stats["communities"]:
            if c["community"] in settings.community_names:
                m.add("bgp_community_prefixes", c["count"],
                      {"community": c["community"], "name": c["name"]},
                      help="Active prefixes per configured community")

        unstable = stats["unstable"]
        m.add("bgp_route_changes", unstable["route_changes_1h"], {"window": "1h"},
              help="Route changes (attribute change, withdraw, re-announce) in the window")
        m.add("bgp_route_changes", unstable["route_changes"], {"window": f"{unstable['window_hours']}h"})
        m.add("bgp_changed_prefixes", unstable["changed_prefixes"],
              {"window": f"{unstable['window_hours']}h"}, help="Prefixes with at least one change")
        m.add("bgp_unstable_prefixes", unstable["unstable_prefixes"],
              {"window": f"{unstable['window_hours']}h", "min_changes": unstable["min_changes"]},
              help="Prefixes with at least min_changes changes in the window")

        exits = stats.get("exits") or {}
        if exits.get("configured"):
            for loc in exits["locations"]:
                for kind in exits["types"]:
                    for v in ("4", "6"):
                        m.add("bgp_exit_prefixes", loc[kind][v],
                              {"location": loc["name"], "type": kind, "ip_version": v},
                              help="Active prefixes by exit location and route type")
            for peer in exits["via"]:
                m.add("bgp_peer_community_prefixes", peer["total"],
                      {"community": peer["community"], "name": peer["name"] or ""},
                      help="Active prefixes per peer/exchange community")
            tip = exits["transit_instead_of_peering"]
            if tip["available"]:
                m.add("bgp_transit_instead_of_peering_prefixes", tip["total_prefixes"],
                      help="Prefixes reached via transit although their origin AS is a direct peer")

        for p in stats["own_prefixes"]:
            labels = {"prefix": p["prefix"]}
            m.add("bgp_own_prefix_ok", p["ok"], labels, help="1 if the own prefix has no problem")
            m.add("bgp_own_prefix_visible", "missing" not in p["problems"]
                  and "invalid_config" not in p["problems"], labels,
                  help="1 if the own prefix is in the table")
            m.add("bgp_own_prefix_origin_ok", bool(p["route"] and p["route"]["origin_ok"]), labels,
                  help="1 if the own prefix is originated by an allowed AS")
            m.add("bgp_own_prefix_foreign_more_specifics",
                  sum(1 for x in p["more_specifics"] if not x["allowed"]), labels,
                  help="More specific prefixes announced by foreign origin ASNs")

    # Collector
    m.add("bgp_collector_heartbeat_timestamp_seconds", _ts(collector.get("heartbeat")),
          help="Last sign of life of the collector process")
    m.add("bgp_collector_started_timestamp_seconds", _ts(collector.get("started")),
          help="Start time of the collector")
    m.add("bgp_collector_last_update_timestamp_seconds", _ts(collector.get("last_update")),
          help="Time the last BGP update was written")
    m.add("bgp_collector_paths_total", collector.get("paths_total", 0), kind="counter",
          help="BGP paths processed")
    m.add("bgp_collector_route_changes_total", collector.get("route_changes_total", 0),
          kind="counter", help="Route changes written")
    m.add("bgp_collector_withdrawals_total", collector.get("withdrawals_total", 0),
          kind="counter", help="Withdrawals processed")
    m.add("bgp_neighbor_poll_timestamp_seconds", _ts(collector.get("neighbors_polled")),
          help="Last successful poll of the BGP session state")
    m.add("bgp_neighbor_poll_error", bool(collector.get("neighbor_error")),
          help="1 if the last poll of the BGP session state failed")

    # BGP sessions
    for n in sessions.get("neighbors", []):
        labels = {"neighbor": n["address"], "peer_asn": n["peer_asn"],
                  "description": n["description"]}
        m.add("bgp_session_up", n["established"], labels, help="1 if the BGP session is established")
        m.add("bgp_session_admin_down", n["admin_down"], labels,
              help="1 if the session is administratively disabled")
        if n["established"]:
            m.add("bgp_session_established_timestamp_seconds", _ts(n["since"]), labels,
                  help="Time the session was established")
        m.add("bgp_session_drops_total", n["session_drops"], labels, kind="counter",
              help="Times an established session was lost (observed by the poller)")
        for family, counts in sorted(n["families"].items()):
            fl = {**labels, "family": family}
            m.add("bgp_session_prefixes_received", counts.get("received", 0), fl,
                  help="Prefixes received on the session")
            m.add("bgp_session_prefixes_accepted", counts.get("accepted", 0), fl,
                  help="Prefixes accepted on the session")
    return m.render()
