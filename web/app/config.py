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
    customer_community: str = "3701:370"
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

    sections = {**data.get("network", {}), **data.get("dashboard", {}), **data.get("monitoring", {})}
    known = Settings.__dataclass_fields__
    kwargs = {k: (tuple(v) if isinstance(v, list) else v) for k, v in sections.items() if k in known}
    kwargs["community_names"] = {str(k): str(v) for k, v in data.get("communities", {}).items()}
    kwargs["mongo_uri"] = os.environ.get("MONGO_URI", Settings.mongo_uri)
    kwargs["mongo_db"] = os.environ.get("MONGO_DB", Settings.mongo_db)
    if os.environ.get("BGP_ASN"):
        kwargs["asn"] = int(os.environ["BGP_ASN"])
    return Settings(**kwargs)
