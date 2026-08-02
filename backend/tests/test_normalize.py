"""Unit tests for the pure normalization helpers."""

from __future__ import annotations

from datetime import UTC, datetime

from app.normalize import (
    build_source_key,
    clean_locations,
    content_hash,
    detect_remote,
    html_to_text,
    maybe_unescape_html,
    parse_epoch_millis,
    parse_iso_datetime,
)


class TestUnescape:
    def test_unescapes_greenhouse_style_double_encoded_html(self) -> None:
        # Greenhouse returns `content` entity-escaped; verified live.
        assert maybe_unescape_html("&lt;p&gt;Hello&lt;/p&gt;") == "<p>Hello</p>"

    def test_leaves_real_html_alone(self) -> None:
        html = '<p>Tom &amp; Jerry</p>'
        assert maybe_unescape_html(html) == html

    def test_handles_none_and_empty(self) -> None:
        assert maybe_unescape_html(None) is None
        assert maybe_unescape_html("") == ""


class TestHtmlToText:
    def test_strips_tags_and_keeps_structure(self) -> None:
        text = html_to_text("<h2>Role</h2><p>Build things.</p><ul><li>One</li><li>Two</li></ul>")
        assert "Role" in text
        assert "Build things." in text
        assert "• One" in text and "• Two" in text
        assert "<" not in text

    def test_decodes_entities_and_nbsp(self) -> None:
        assert html_to_text("<p>Tom &amp; Jerry&nbsp;here</p>") == "Tom & Jerry here"

    def test_drops_script_and_style(self) -> None:
        text = html_to_text("<p>Keep</p><script>var x=1;</script><style>a{}</style>")
        assert text == "Keep"

    def test_collapses_excess_blank_lines(self) -> None:
        assert "\n\n\n" not in (html_to_text("<p>a</p><div></div><div></div><p>b</p>") or "")

    def test_none_for_empty_input(self) -> None:
        assert html_to_text(None) is None
        assert html_to_text("<div>  </div>") is None


class TestCleanLocations:
    def test_dedupes_case_insensitively_preserving_order(self) -> None:
        assert clean_locations(["New York", "new york", "Berlin"]) == ["New York", "Berlin"]

    def test_drops_empties_and_trims_punctuation(self) -> None:
        assert clean_locations(["  Paris , ", None, "", "  "]) == ["Paris"]

    def test_handles_none(self) -> None:
        assert clean_locations(None) == []


class TestDetectRemote:
    def test_workplace_type_outranks_explicit_flag(self) -> None:
        # Live Ashby data has isRemote=True with workplaceType="Hybrid";
        # hybrid is the more specific claim and must win.
        assert detect_remote(["San Francisco"], workplace_type="Hybrid", explicit=True) is False

    def test_explicit_used_when_workplace_type_missing(self) -> None:
        assert detect_remote(["San Francisco"], workplace_type=None, explicit=True) is True

    def test_workplace_type_remote(self) -> None:
        assert detect_remote(["North America"], workplace_type="remote") is True

    def test_keyword_scan_fallback(self) -> None:
        assert detect_remote(["US-Remote, US-Chicago"]) is True
        assert detect_remote(["San Francisco, CA"]) is False

    def test_onsite_is_not_remote(self) -> None:
        assert detect_remote(["Remote-ish office"], workplace_type="onsite") is False


class TestDateParsing:
    def test_iso_with_offset_converts_to_utc(self) -> None:
        parsed = parse_iso_datetime("2026-06-02T08:58:57-04:00")
        assert parsed == datetime(2026, 6, 2, 12, 58, 57, tzinfo=UTC)

    def test_iso_with_z(self) -> None:
        assert parse_iso_datetime("2025-03-27T16:44:10.676Z").tzinfo is UTC

    def test_naive_assumed_utc(self) -> None:
        assert parse_iso_datetime("2026-01-01T00:00:00") == datetime(2026, 1, 1, tzinfo=UTC)

    def test_garbage_returns_none(self) -> None:
        assert parse_iso_datetime("not a date") is None
        assert parse_iso_datetime(None) is None
        assert parse_iso_datetime(12345) is None

    def test_epoch_millis(self) -> None:
        # Lever's createdAt, verified live.
        assert parse_epoch_millis(1711403416463) == datetime(
            2024, 3, 25, 21, 50, 16, 463000, tzinfo=UTC
        )

    def test_epoch_seconds_tolerated(self) -> None:
        assert parse_epoch_millis(1711403416).year == 2024

    def test_epoch_garbage(self) -> None:
        assert parse_epoch_millis(None) is None
        assert parse_epoch_millis("abc") is None
        assert parse_epoch_millis(0) is None


class TestSourceKey:
    def test_format_matches_spec(self) -> None:
        assert build_source_key("greenhouse", "stripe", "7954688") == "greenhouse:stripe:7954688"


class TestContentHash:
    def _hash(self, **overrides: object) -> str:
        base: dict[str, object] = {
            "title": "Engineer",
            "locations": ["NYC", "SF"],
            "department": "Eng",
            "remote": False,
            "apply_url": "https://example.com/1",
            "description_text": "Do the thing",
            "posted_at": None,
            "updated_at": None,
        }
        base.update(overrides)
        return content_hash(**base)  # type: ignore[arg-type]

    def test_stable_across_calls(self) -> None:
        assert self._hash() == self._hash()

    def test_location_order_does_not_matter(self) -> None:
        assert self._hash(locations=["SF", "NYC"]) == self._hash(locations=["NYC", "SF"])

    def test_content_change_changes_hash(self) -> None:
        assert self._hash(title="Senior Engineer") != self._hash()
        assert self._hash(description_text="Different") != self._hash()
