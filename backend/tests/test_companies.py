"""companies.yaml loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.companies import CompanyConfigError, load_companies
from app.schemas import CompanyConfig
from tests.conftest import FIXTURE_DIR

REPO_COMPANIES = Path(__file__).resolve().parent.parent / "companies.yaml"


class TestLoading:
    def test_skips_unknown_ats_and_disabled_entries(self) -> None:
        configs = load_companies(FIXTURE_DIR / "companies_test.yaml")
        assert [c.company for c in configs] == ["Stripe", "Palantir", "Linear"]

    def test_include_disabled(self) -> None:
        configs = load_companies(FIXTURE_DIR / "companies_test.yaml", include_disabled=True)
        assert "Disabled Co" in {c.company for c in configs}

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(CompanyConfigError):
            load_companies(tmp_path / "nope.yaml")

    def test_malformed_entries_are_skipped_not_fatal(self, tmp_path: Path) -> None:
        path = tmp_path / "companies.yaml"
        path.write_text(
            "companies:\n"
            "  - just-a-string\n"
            "  - company: Missing Fields\n"
            "  - company: Good Co\n"
            "    ats: greenhouse\n"
            "    token_or_slug: good\n",
            encoding="utf-8",
        )
        configs = load_companies(path)
        assert [c.company for c in configs] == ["Good Co"]

    def test_duplicate_sources_are_dropped(self, tmp_path: Path) -> None:
        path = tmp_path / "companies.yaml"
        path.write_text(
            "companies:\n"
            "  - {company: Dup, ats: greenhouse, token_or_slug: a}\n"
            "  - {company: Dup, ats: greenhouse, token_or_slug: b}\n",
            encoding="utf-8",
        )
        assert len(load_companies(path)) == 1

    def test_empty_file(self, tmp_path: Path) -> None:
        path = tmp_path / "companies.yaml"
        path.write_text("", encoding="utf-8")
        assert load_companies(path) == []

    def test_bare_list_is_tolerated(self, tmp_path: Path) -> None:
        path = tmp_path / "companies.yaml"
        path.write_text(
            "- {company: Bare, ats: lever, token_or_slug: bare}\n", encoding="utf-8"
        )
        assert [c.company for c in load_companies(path)] == ["Bare"]


class TestShippedConfig:
    def test_repo_companies_file_is_valid(self) -> None:
        configs = load_companies(REPO_COMPANIES, include_disabled=True)
        assert len(configs) >= 10
        assert {c.ats for c in configs} <= {"greenhouse", "lever", "ashby"}

    def test_source_ids_are_unique(self) -> None:
        configs = load_companies(REPO_COMPANIES, include_disabled=True)
        ids = [c.source_id for c in configs]
        assert len(ids) == len(set(ids))


class TestCompanyConfig:
    def test_key_is_slugified(self) -> None:
        config = CompanyConfig(company="Grow Therapy!", ats="lever", token_or_slug="gt")
        assert config.key == "grow-therapy"
        assert config.source_id == "lever:grow-therapy"

    def test_ats_is_lowercased(self) -> None:
        assert CompanyConfig(company="X", ats="GreenHouse", token_or_slug="x").ats == "greenhouse"
