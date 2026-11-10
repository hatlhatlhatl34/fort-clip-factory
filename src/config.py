"""Configuration loader — reads channel.yaml once, exposes typed access.

Ported unchanged from forever-factory (proven for 60+ episodes)."""
from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("PyYAML missing — pip install -r requirements.txt") from exc

ROOT = Path(__file__).resolve().parent.parent


@functools.lru_cache(maxsize=1)
def cfg() -> dict[str, Any]:
    """Load channel.yaml from the repo root."""
    path = ROOT / "channel.yaml"
    if not path.exists():
        raise SystemExit(f"channel.yaml not found at {path}")
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def path(*parts: str) -> Path:
    """Absolute path inside the repo."""
    return ROOT.joinpath(*parts)


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]
