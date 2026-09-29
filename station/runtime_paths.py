"""Resolve writable GrainSilo data separately from packaged application assets."""

import os
from pathlib import Path


def resolve_runtime_paths(script_path, *, frozen, local_app_data=None,
                          user_home=None, db_override=None):
    """Return bundle, web, database, and log paths for source or frozen runs.

    Source checkouts deliberately keep the existing ``station/station.db``
    location. Frozen Windows builds keep mutable state under LocalAppData so
    upgrades do not write into a read-only or replaceable application folder.
    """
    bundle_root = Path(script_path).resolve().parent
    if frozen:
        if local_app_data:
            data_root = Path(local_app_data).expanduser() / "GrainSilo"
        else:
            home = Path(user_home or Path.home()).expanduser()
            data_root = home / "AppData" / "Local" / "GrainSilo"
    else:
        data_root = bundle_root

    db_path = Path(db_override).expanduser() if db_override else data_root / "station.db"
    return {
        "root": str(bundle_root),
        "web": str(bundle_root / "web"),
        "data": str(data_root),
        "db": str(db_path),
        "logs": str(data_root / "logs"),
    }
