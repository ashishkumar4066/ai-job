"""Stage 3 — the résumé template, the fact check, and the tailoring pass.

The test this file exists for is `test_invented_metric_is_rejected`. CLAUDE.md:
*a generated résumé contains no claim absent from `profile.yaml` (fixture
test)*. It drives `apply_tailoring` with a canned completion — no network, no
model — so the guarantee is asserted about the code, not about a prompt.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.factcheck import FactCorpus, screen_edits
from app.profile import load_profile
from app.resume_tex import (
    BulletEdit,
    ResumeDocument,
    ResumeEdits,
    from_latex,
    latex_escape,
    to_latex,
)
from app.tailor import BulletDecision, ResumeTailoring, apply_tailoring

TEMPLATE = Path(__file__).resolve().parent.parent / "data" / "Ashish_AI_FullStack_v2.tex"
PROFILE = Path(__file__).resolve().parent.parent / "profile.yaml"

pytestmark = pytest.mark.skipif(
    not TEMPLATE.is_file(),
    reason="data/ holds personal documents and is gitignored; no template on this checkout",
)


@pytest.fixture
def document() -> ResumeDocument:
    return ResumeDocument.load(TEMPLATE)


@pytest.fixture
def corpus(document: ResumeDocument) -> FactCorpus:
    return FactCorpus.build(load_profile(PROFILE), document.tex)


# --------------------------------------------------------------------------
# Template
# --------------------------------------------------------------------------


def test_parses_every_section(document: ResumeDocument) -> None:
    kinds = {region.kind for region in document.regions}
    assert kinds == {"summary", "bullet", "skills_row"}
    groups = document.groups()
    assert "projects" in groups
    assert any(group.startswith("exp.") for group in groups)


def test_empty_edits_are_byte_identical(document: ResumeDocument) -> None:
    """The safety property: an untouched section cannot change."""
    assert document.render(ResumeEdits()) == document.tex


def test_reword_replaces_only_that_bullet(document: ResumeDocument) -> None:
    target = next(r for r in document.regions if r.kind == "bullet")
    out = document.render(
        ResumeEdits(bullets=[BulletEdit(id=target.id, text="Did a **thing** with Python.")])
    )
    assert r"\item Did a \textbf{thing} with Python." in out
    assert target.tex not in out
    # Every other bullet survives untouched.
    for other in document.regions:
        if other.id != target.id and other.kind == "bullet":
            assert other.tex in out


def test_dropping_a_bullet_removes_its_item_marker(document: ResumeDocument) -> None:
    """An orphaned `\\item` renders as an empty bullet in the PDF."""
    target = next(r for r in document.regions if r.kind == "bullet")
    out = document.render(ResumeEdits(bullets=[BulletEdit(id=target.id, include=False)]))
    assert target.tex not in out
    assert out.count(r"\item") == document.tex.count(r"\item") - 1


def test_skills_rows_are_never_dropped(document: ResumeDocument) -> None:
    """Removing a row would leave a dangling `\\\\` and break the tabular."""
    row = next(r for r in document.regions if r.kind == "skills_row")
    out = document.render(ResumeEdits(bullets=[BulletEdit(id=row.id, include=False)]))
    assert row.tex in out


def test_unknown_region_id_is_ignored(document: ResumeDocument) -> None:
    out = document.render(ResumeEdits(bullets=[BulletEdit(id="exp.nope.9", text="hi")]))
    assert out == document.tex


def test_reorder_stays_within_its_job(document: ResumeDocument) -> None:
    groups = document.groups()
    experience = next(
        ids for name, ids in groups.items() if name.startswith("exp.") and len(ids) > 2
    )
    first, third = experience[0].id, experience[2].id
    out = document.render(ResumeEdits(order=[third, first]))
    assert out.index(experience[2].tex) < out.index(experience[0].tex)
    # A different job's bullets did not move into this one.
    for other_name, others in groups.items():
        if other_name == experience[0].group or other_name == "skills":
            continue
        for region in others:
            assert region.tex in out


# --------------------------------------------------------------------------
# Escaping — the model must not be able to write LaTeX
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("100% done", r"100\% done"),
        ("R&D", r"R\&D"),
        ("a_b", r"a\_b"),
        ("cost $5", r"cost \$5"),
        ("~90% cheaper", r"$\sim$90\% cheaper"),
        ("**bold** text", r"\textbf{bold} text"),
    ],
)
def test_escaping(raw: str, expected: str) -> None:
    assert to_latex(raw) == expected


def test_macros_from_the_model_become_literal_text() -> None:
    """A completion carrying LaTeX must not execute as LaTeX."""
    out = to_latex(r"\input{/etc/passwd} \textbf{x} }{")
    assert r"\input" not in out
    assert out.count(r"\textbackslash{}") == 2
    # Every command sequence left is one this module emitted, never the model's.
    assert set(re.findall(r"\\([a-zA-Z]+)", out)) <= {"textbackslash"}


def test_escape_does_not_re_escape_its_own_output() -> None:
    assert latex_escape("{}") == r"\{\}"
    assert latex_escape("\\") == r"\textbackslash{}"


def test_from_latex_round_trips_bold() -> None:
    assert from_latex(r"Built \textbf{RAG} at \textbf{75\%}") == "Built **RAG** at **75%**"


# --------------------------------------------------------------------------
# The fact check
# --------------------------------------------------------------------------


def test_corpus_accepts_a_reworded_fact(corpus: FactCorpus) -> None:
    # Same numbers, different sentence — this is what tailoring should produce.
    assert corpus.check("x", "Schema-grounded RAG at **75%** accuracy across **200+** schemas") == []


def test_corpus_flags_an_invented_number(corpus: FactCorpus) -> None:
    issues = corpus.check("x", "Achieved **99.7%** accuracy across 40000 schemas")
    numbers = {issue.token for issue in issues if issue.kind == "number"}
    assert "99.7" in numbers
    assert "40000" in numbers
    assert all(issue.severity == "blocking" for issue in issues if issue.kind == "number")


def test_claiming_a_gap_stack_is_blocking(corpus: FactCorpus) -> None:
    """`gaps:` names what the résumé must never assert.

    The corpus contains "kubernetes" twice over — as a key in `gaps:` and
    inside the evidence line "No AWS, Azure or Kubernetes in production" — so a
    plain allowlist reads a denial as permission.
    """
    issues = corpus.check("x", "Shipped it on Kubernetes across AWS")
    blocking = {i.token for i in issues if i.severity == "blocking"}
    assert {"kubernetes", "aws"} <= blocking


def test_unknown_term_only_warns(corpus: FactCorpus) -> None:
    """A term that is neither proven nor a declared gap is the human's call."""
    issues = corpus.check("x", "Instrumented it with Datadog")
    assert [i.severity for i in issues if i.token == "datadog"] == ["warning"]


def test_lowercase_profile_words_are_not_invented(corpus: FactCorpus) -> None:
    """A rewrite may capitalise a word the résumé writes lower-case."""
    assert not corpus.check("x", "Guardrails against destructive SQL")


def test_screen_edits_drops_only_the_offending_rewrite(corpus: FactCorpus) -> None:
    accepted, issues = screen_edits(
        corpus,
        {
            "good": "Built a **Text-to-SQL engine** with **75%** accuracy",
            "bad": "Built a **Text-to-SQL engine** with **98%** accuracy",
        },
    )
    assert set(accepted) == {"good"}
    assert {i.region_id for i in issues if i.severity == "blocking"} == {"bad"}


# --------------------------------------------------------------------------
# The guarantee
# --------------------------------------------------------------------------


def test_invented_metric_is_rejected(document: ResumeDocument, corpus: FactCorpus) -> None:
    """CLAUDE.md: a generated résumé contains no claim absent from the profile.

    The canned completion below is what a hallucinating model looks like: it
    keeps the shape of the bullet and inflates the number. The rewrite must be
    discarded and the ORIGINAL bullet must survive into the LaTeX.
    """
    target = next(r for r in document.regions if "75" in r.text)

    tailoring = ResumeTailoring(
        bullets=[
            BulletDecision(
                id=target.id,
                include=True,
                rewritten=(
                    "Built a **Text-to-SQL engine from scratch** achieving **98%** accuracy "
                    "across **900+** schemas at **Google**"
                ),
            )
        ],
        summary="",
    )
    tex, edits, issues = apply_tailoring(tailoring, document, corpus)

    assert target.tex in tex, "the original bullet must be kept when a rewrite is rejected"
    assert "98" not in tex.replace(target.tex, "")
    blocking = {issue.token for issue in issues if issue.severity == "blocking"}
    assert {"98", "900"} <= blocking
    assert edits.by_id()[target.id].text is None


def test_accepted_rewrite_reaches_the_latex(document: ResumeDocument, corpus: FactCorpus) -> None:
    """The other half: a truthful rewrite is not blocked by the guard."""
    target = next(r for r in document.regions if "75" in r.text)
    tailoring = ResumeTailoring(
        bullets=[
            BulletDecision(
                id=target.id,
                rewritten="**Text-to-SQL** from scratch: **75%** accuracy over **200+** schemas",
            )
        ]
    )
    tex, _, issues = apply_tailoring(tailoring, document, corpus)
    assert r"\textbf{Text-to-SQL} from scratch" in tex
    assert not [i for i in issues if i.severity == "blocking"]


def test_tailoring_cannot_add_a_bullet(document: ResumeDocument, corpus: FactCorpus) -> None:
    """There is no slot to add one to — the template only has fillable holes."""
    tailoring = ResumeTailoring(
        bullets=[BulletDecision(id="exp.invented.0", rewritten="Ran **Kubernetes** at Meta")]
    )
    tex, _, _ = apply_tailoring(tailoring, document, corpus)
    assert tex == document.tex
    assert "Meta" not in tex
