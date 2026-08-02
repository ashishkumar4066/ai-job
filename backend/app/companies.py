"""Loader for `companies.yaml`."""

from __future__ import annotations

import logging
from pathlib import Path

import yaml
from pydantic import ValidationError

from app.adapters import is_supported_ats, supported_ats
from app.config import get_settings
from app.schemas import CompanyConfig

log = logging.getLogger(__name__)


class CompanyConfigError(RuntimeError):
    pass


def load_companies(path: Path | None = None, *, include_disabled: bool = False) -> list[CompanyConfig]:
    """Parse and validate the company list.

    Unknown ATS values and malformed entries are skipped with a warning rather
    than aborting the run — one bad config line must not stop ingestion.
    """
    path = path or get_settings().companies_file
    if not path.exists():
        raise CompanyConfigError(f"companies file not found: {path}")

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if isinstance(data, list):  # tolerate a bare list at the top level
        entries = data
    else:
        entries = data.get("companies") or []

    if not isinstance(entries, list):
        raise CompanyConfigError(f"'companies' must be a list in {path}")

    configs: list[CompanyConfig] = []
    seen: set[str] = set()

    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            log.warning("companies.skip_malformed", extra={"index": index})
            continue
        try:
            config = CompanyConfig(**entry)
        except ValidationError as exc:
            log.warning(
                "companies.skip_invalid",
                extra={"index": index, "entry": entry, "error": exc.errors(include_url=False)},
            )
            continue

        if not is_supported_ats(config.ats):
            log.warning(
                "companies.skip_unknown_ats",
                extra={"company": config.company, "ats": config.ats, "supported": supported_ats()},
            )
            continue

        if config.source_id in seen:
            log.warning(
                "companies.skip_duplicate",
                extra={"company": config.company, "source_id": config.source_id},
            )
            continue
        seen.add(config.source_id)

        if config.enabled or include_disabled:
            configs.append(config)

    return configs
