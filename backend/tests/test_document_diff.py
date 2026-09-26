"""Stage 3 — the diff between a tailored résumé and the untailored template.

CLAUDE.md asks for "a diff against the base résumé so I can see exactly what was
changed". The classification is what these tests pin down, because three of the
four verdicts are easy to get silently wrong:

* a bullet that only *moved* has identical text, so a text comparison calls it
  unchanged and the reorder — the tailoring pass's other power — goes unreported;
* a *dropped* bullet is absent rather than empty, so it has to be looked for in
  the base, not in the current document;
* an *added* region cannot come from tailoring at all, and hiding it would make
  the diff a partial account of the difference.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.documents import diff_against_base
from app.resume_tex import BulletEdit, ResumeDocument, ResumeEdits

TEMPLATE = Path(__file__).resolve().parent.parent / "data" / "Ashish_AI_FullStack_v2.tex"

pytestmark = pytest.mark.skipif(
    not TEMPLATE.is_file(),
    reason="data/ holds personal documents and is gitignored; no template on this checkout",
)


@pytest.fixture
def document() -> ResumeDocument:
    return ResumeDocument.load(TEMPLATE)


def _bullets(document: ResumeDocument) -> list:
    return [r for r in document.regions if r.kind == "bullet"]


def _row(rows: list[dict], region_id: str) -> dict:
    return next(r for r in rows if r["region_id"] == region_id)


def test_an_untailored_resume_reports_no_changes(document: ResumeDocument) -> None:
    """The safety property `resume_tex` guarantees, restated as a diff."""
    rows, counts = diff_against_base(document.render(ResumeEdits()), document)
    assert counts["reworded"] == 0
    assert counts["dropped"] == 0
    assert counts["moved"] == 0
    assert counts["added"] == 0
    assert counts["unchanged"] == len(rows) == len(document.regions)


def test_a_reworded_bullet_is_reported_with_both_texts(document: ResumeDocument) -> None:
    """A real tailored rewrite keeps the employer, stack and metrics.

    That is what makes it recognisable as a rewrite of *this* bullet rather
    than a different one, so the fixture has to look like one.
    """
    target = _bullets(document)[0]
    rewritten = " ".join(target.text.split()[:12]) + " for this posting."
    tailored = document.render(ResumeEdits(bullets=[BulletEdit(id=target.id, text=rewritten)]))
    rows, counts = diff_against_base(tailored, document)

    row = _row(rows, target.id)
    assert row["status"] == "reworded"
    assert row["base"] == target.text
    assert row["current"] == rewritten
    assert counts["reworded"] == 1


def test_a_wholesale_replacement_reads_as_a_drop_plus_an_add(document: ResumeDocument) -> None:
    """Text sharing nothing with the original is not a rewrite of it.

    Calling it a rewrite would show a before/after pair with no relationship,
    which is a worse account of what happened than "one gone, one new".
    """
    target = _bullets(document)[0]
    tailored = document.render(
        ResumeEdits(bullets=[BulletEdit(id=target.id, text="Entirely unrelated sentence here.")])
    )
    _, counts = diff_against_base(tailored, document)
    assert counts["reworded"] == 0
    assert counts["dropped"] == 1
    assert counts["added"] == 1


def test_a_dropped_bullet_is_reported_with_an_empty_current(document: ResumeDocument) -> None:
    target = _bullets(document)[1]
    tailored = document.render(ResumeEdits(bullets=[BulletEdit(id=target.id, include=False)]))
    rows, counts = diff_against_base(tailored, document)

    row = _row(rows, target.id)
    assert row["status"] == "dropped"
    assert row["base"] == target.text
    assert row["current"] == ""
    assert row["current_index"] is None
    assert counts["dropped"] == 1


def test_a_moved_bullet_is_not_called_unchanged(document: ResumeDocument) -> None:
    """Its text is identical, so only the position can give the reorder away."""
    group = _bullets(document)[0].group
    in_group = [r.id for r in _bullets(document) if r.group == group]
    assert len(in_group) >= 2

    reordered = [in_group[1], in_group[0], *in_group[2:]]
    rows, counts = diff_against_base(document.render(ResumeEdits(order=reordered)), document)

    assert counts["moved"] >= 2
    assert counts["reworded"] == 0
    first = _row(rows, in_group[0])
    assert first["status"] == "moved"
    assert first["base_index"] == 0
    assert first["current_index"] == 1


def test_a_reorder_outside_the_moved_pair_stays_unchanged(document: ResumeDocument) -> None:
    group = _bullets(document)[0].group
    in_group = [r.id for r in _bullets(document) if r.group == group]
    other_group = next(r for r in _bullets(document) if r.group != group)

    reordered = [in_group[1], in_group[0], *in_group[2:]]
    rows, _ = diff_against_base(document.render(ResumeEdits(order=reordered)), document)
    assert _row(rows, other_group.id)["status"] == "unchanged"


def test_the_summary_is_diffed_too(document: ResumeDocument) -> None:
    tailored = document.render(ResumeEdits(summary="A tighter summary for this posting."))
    rows, counts = diff_against_base(tailored, document)
    assert _row(rows, "summary")["status"] == "reworded"
    assert counts["reworded"] == 1


def test_rows_come_back_in_the_resumes_own_order(document: ResumeDocument) -> None:
    """The pane reads top to bottom like the document, not grouped by verdict."""
    rows, _ = diff_against_base(document.render(ResumeEdits()), document)
    assert [r["region_id"] for r in rows] == [r.id for r in document.regions]


def test_every_row_carries_its_label_and_kind(document: ResumeDocument) -> None:
    rows, _ = diff_against_base(document.render(ResumeEdits()), document)
    assert all(r["label"] and r["kind"] and r["group"] for r in rows)


def test_a_hand_added_region_is_surfaced_not_hidden(document: ResumeDocument) -> None:
    """Tailoring has no slot to add a bullet, so this means a structural edit."""
    target = _bullets(document)[0]
    tex = document.tex.replace(target.tex, target.tex + "\n    \\item A bullet I typed myself.", 1)
    rows, counts = diff_against_base(tex, document)

    assert counts["added"] == 1
    added = next(r for r in rows if r["status"] == "added")
    assert "typed myself" in added["current"]
    assert added["base"] == ""
    assert added["base_index"] is None


def test_counts_agree_with_the_rows(document: ResumeDocument) -> None:
    target = _bullets(document)[0]
    tailored = document.render(ResumeEdits(bullets=[BulletEdit(id=target.id, text="Rewritten.")]))
    rows, counts = diff_against_base(tailored, document)
    for status, total in counts.items():
        assert sum(1 for r in rows if r["status"] == status) == total, status
