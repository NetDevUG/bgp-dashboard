"""Settings loaded from ``dashboard.toml`` plus environment variables."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

_DEFAULT_PATHS = (
    Path("/config/dashboard.toml"),
    Path(__file__).resolve().parents[2] / "config" / "dashboard.toml",
)


@dataclass(frozen=True)
class Settings:
    asn: int = 3701
    # Empty = no customers: the dashboard shows the own prefixes instead
    customer_community: str = ""
    transit_community: str = "3701:380"
    peer_community_prefix: str = "3701:39"
    community_names: dict[str, str] = field(default_factory=dict)

    stats_interval: int = 60
    advanced_stats_interval: int = 300
    stats_history_days: int = 14
    top_peers: int = 5
    large_query_threshold: int = 200
    dns_timeout: float = 2.0

    # Monitoring of own prefixes
    own_prefixes: tuple[str, ...] = ()
    allowed_origins: tuple[int, ...] = ()
    check_more_specifics: bool = True
    # Unstable prefixes
    unstable_window_hours: int = 24
    unstable_top: int = 20
    unstable_min_changes: int = 3
    # Exit analysis (empty = disabled)
    location_community_prefix: str = ""
    peer_learned_community: str = ""
    originated_community: str = ""
    # RPKI ([rpki]): JSON export of Routinator / rpki-client, empty = disabled
    rpki_vrp_url: str = ""
    rpki_refresh_minutes: float = 15
    # Akvorado ([akvorado]): ClickHouse HTTP interface, empty = disabled (prefix counts are used)
    akvorado_clickhouse_url: str = ""
    akvorado_database: str = "default"
    akvorado_table: str = "flows_5m0s"
    akvorado_window_hours: float = 24
    akvorado_user: str = ""
    akvorado_password: str = ""
    # PeeringDB ([peeringdb])
    peeringdb_enabled: bool = False
    peeringdb_refresh_hours: float = 12
    peeringdb_candidates: int = 50
    peeringdb_api_key: str = ""
    peeringdb_url: str = "https://www.peeringdb.com/api"
    # RIPE RIS Live ([ris_live]): watch the own prefixes as seen by the RIS route collectors
    ris_live_enabled: bool = False
    ris_live_url: str = "wss://ris-live.ripe.net/v1/ws/"
    ris_live_event_days: int = 30
    ripestat_url: str = "https://stat.ripe.net/data"
    # BGP sessions: data older than this is shown as stale (collector polls every 10s)
    neighbor_stale_seconds: int = 60

    mongo_uri: str = "mongodb://mongodb:27017"
    mongo_db: str = "bgp"
    # Disable the background statistics job (used by tests)
    background_jobs: bool = True


def load_settings(path: str | os.PathLike | None = None) -> Settings:
    path = path or os.environ.get("BGP_DASHBOARD_CONFIG")
    candidates = [Path(path)] if path else list(_DEFAULT_PATHS)
    data: dict = {}
    for candidate in candidates:
        if candidate.is_file():
            with candidate.open("rb") as fh:
                data = tomllib.load(fh)
            break

    sections = {**data.get("network", {}), **data.get("dashboard", {}),
                **data.get("monitoring", {}), **data.get("exits", {})}
    # Sections whose keys are prefixed with the section name, e.g. [rpki] vrp_url -> rpki_vrp_url
    for section in ("rpki", "akvorado", "peeringdb", "ris_live"):
        sections.update({f"{section}_{k}": v for k, v in data.get(section, {}).items()})
    # Secrets only from the environment
    for env, key in (("AKVORADO_CLICKHOUSE_USER", "akvorado_user"),
                     ("AKVORADO_CLICKHOUSE_PASSWORD", "akvorado_password"),
                     ("PEERINGDB_API_KEY", "peeringdb_api_key")):
        if os.environ.get(env):
            sections[key] = os.environ[env]
    known = Settings.__dataclass_fields__
    kwargs = {k: (tuple(v) if isinstance(v, list) else v) for k, v in sections.items() if k in known}
    kwargs["community_names"] = {str(k): str(v) for k, v in data.get("communities", {}).items()}
    kwargs["mongo_uri"] = os.environ.get("MONGO_URI", Settings.mongo_uri)
    kwargs["mongo_db"] = os.environ.get("MONGO_DB", Settings.mongo_db)
    if os.environ.get("BGP_ASN"):
        kwargs["asn"] = int(os.environ["BGP_ASN"])
    return Settings(**kwargs)
