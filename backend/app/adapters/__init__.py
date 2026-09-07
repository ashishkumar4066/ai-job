"""Adapter registry.

Two families, one interface:

  * **Curated ATS** — Greenhouse, Lever, Ashby. One board per company, driven
    by a `companies.yaml` entry with a board token.
  * **Aggregator boards** — Himalayas, Remotive. Whole-board feeds carrying
    candidate-eligibility metadata, driven by query params rather than a token.

Adding a company on an already-supported ATS is a `companies.yaml` edit.
Adding a *new* source means one module here plus one line in `_REGISTRY`.
"""

from __future__ import annotations

import httpx

from app.adapters.ashby import AshbyAdapter
from app.adapters.base import AdapterError, BaseAdapter
from app.adapters.greenhouse import GreenhouseAdapter
from app.adapters.himalayas import HimalayasAdapter
from app.adapters.lever import LeverAdapter
from app.adapters.playwright_adapter import PlaywrightAdapter
from app.adapters.remotive import RemotiveAdapter
from app.adapters.wellfound import WellfoundAdapter

_REGISTRY: dict[str, type[BaseAdapter]] = {
    GreenhouseAdapter.ats: GreenhouseAdapter,
    LeverAdapter.ats: LeverAdapter,
    AshbyAdapter.ats: AshbyAdapter,
    HimalayasAdapter.ats: HimalayasAdapter,
    RemotiveAdapter.ats: RemotiveAdapter,
    WellfoundAdapter.ats: WellfoundAdapter,
    PlaywrightAdapter.ats: PlaywrightAdapter,
}

# Sources that aggregate many employers onto one board. Their `company` column
# holds the employer from the feed, not the board name.
AGGREGATOR_SOURCES = frozenset(
    {HimalayasAdapter.ats, RemotiveAdapter.ats, WellfoundAdapter.ats}
)

# Aliases people naturally write in config.
_ALIASES = {
    "gh": "greenhouse",
    "greenhouse.io": "greenhouse",
    "lever.co": "lever",
    "ashbyhq": "ashby",
    "ashby_hq": "ashby",
    "himalayas.app": "himalayas",
    "remotive.com": "remotive",
    "remotive.io": "remotive",
    "wellfound.com": "wellfound",
    "angellist": "wellfound",
    "angel.co": "wellfound",
}


def is_aggregator(ats: str) -> bool:
    return canonical_ats(ats) in AGGREGATOR_SOURCES


def canonical_ats(ats: str) -> str:
    key = ats.strip().lower()
    return _ALIASES.get(key, key)


def supported_ats() -> list[str]:
    return sorted(_REGISTRY)


def is_supported_ats(ats: str) -> bool:
    return canonical_ats(ats) in _REGISTRY


def get_adapter(ats: str, client: httpx.AsyncClient | None = None) -> BaseAdapter:
    key = canonical_ats(ats)
    try:
        adapter_cls = _REGISTRY[key]
    except KeyError as exc:
        raise AdapterError(
            f"no adapter for ATS '{ats}' (supported: {', '.join(supported_ats())})"
        ) from exc
    return adapter_cls(client=client)


def register_adapter(adapter_cls: type[BaseAdapter]) -> None:
    """Register an adapter at runtime (used by tests to inject failures)."""
    _REGISTRY[adapter_cls.ats] = adapter_cls


__all__ = [
    "AGGREGATOR_SOURCES",
    "AdapterError",
    "AshbyAdapter",
    "BaseAdapter",
    "GreenhouseAdapter",
    "HimalayasAdapter",
    "LeverAdapter",
    "PlaywrightAdapter",
    "RemotiveAdapter",
    "WellfoundAdapter",
    "canonical_ats",
    "get_adapter",
    "is_aggregator",
    "is_supported_ats",
    "register_adapter",
    "supported_ats",
]
