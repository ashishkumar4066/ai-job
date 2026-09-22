"""Stage 3 — the résumé chat's guards, driven with canned answers.

The chat is where a user can simply *ask* for a stack they do not have, so the
fact check has to hold here at least as firmly as in the one-shot tailoring.
These tests drive `build_proposal` / `apply_to_tex` with no model and no
network, like `test_tailor.py` does for `apply_tailoring`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.documents import DocumentError
from app.factcheck import FactCorpus
from app.profile import load_profile
from app.resume_chat import ChatAnswer, ChatChange, apply_to_tex, build_proposal
from app.resume_tex import ResumeDocument

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


def _bullet(document: ResumeDocument, index: int = 0):
    return [r for r in document.regions if r.kind == "bullet"][index]


def test_asking_for_a_gap_stack_is_blocked(document: ResumeDocument, corpus: FactCorpus) -> None:
    """"Add Kubernetes" must come back blocked, visible, and unapplicable."""
    target = _bullet(document)
    answer = ChatAnswer(
        changes=[ChatChange(id=target.id, rewritten=f"{target.text} Deployed on Kubernetes.")],
        reply="Added Kubernetes.",
    )
    proposal = build_proposal(answer, document, corpus)

    [change] = proposal["changes"]
    assert change["status"] == "blocked"
    assert any("kubernetes" in reason for reason in change["blocked"])
    with pytest.raises(DocumentError):
        apply_to_tex(document.tex, proposal, None)


def test_invented_metric_is_blocked(document: ResumeDocument, corpus: FactCorpus) -> None:
    target = _bullet(document)
    answer = ChatAnswer(changes=[ChatChange(id=target.id, rewritten="Cut latency by 97.3%.")])
    [change] = build_proposal(answer, document, corpus)["changes"]
    assert change["status"] == "blocked"


def test_applying_touches_only_the_accepted_region(
    document: ResumeDocument, corpus: FactCorpus
) -> None:
    first, second = _bullet(document, 0), _bullet(document, 1)
    answer = ChatAnswer(
        changes=[
            ChatChange(id=first.id, rewritten="Built **agentic AI** workflows with Python."),
            ChatChange(id=second.id, include=False),
        ]
    )
    proposal = build_proposal(answer, document, corpus)
    assert proposal["status"] == "pending"

    out = apply_to_tex(document.tex, proposal, [first.id])
    assert r"\item Built \textbf{agentic AI} workflows with Python." in out
    assert second.tex in out  # the dropped bullet was not accepted


def test_unknown_region_and_no_op_changes_are_ignored(
    document: ResumeDocument, corpus: FactCorpus
) -> None:
    target = _bullet(document)
    answer = ChatAnswer(
        changes=[
            ChatChange(id="exp.nowhere.9", rewritten="Invented bullet."),
            ChatChange(id=target.id, rewritten=target.text),
        ],
        reply="Nothing to change.",
    )
    proposal = build_proposal(answer, document, corpus)
    assert proposal["changes"] == []
    assert proposal["status"] == "none"


def test_stale_proposal_refuses_when_its_region_moved(
    document: ResumeDocument, corpus: FactCorpus
) -> None:
    """A suggestion written before a hand edit must not overwrite that edit."""
    target = _bullet(document)
    proposal = build_proposal(
        ChatAnswer(changes=[ChatChange(id=target.id, rewritten="Built workflows with Python.")]),
        document,
        corpus,
    )
    hand_edited = document.tex.replace(target.tex, r"\item My own words about Python.")
    with pytest.raises(DocumentError, match="changed since"):
        apply_to_tex(hand_edited, proposal, None)


def test_stale_proposal_still_applies_when_its_region_is_untouched(
    document: ResumeDocument, corpus: FactCorpus
) -> None:
    first, second = _bullet(document, 0), _bullet(document, 1)
    proposal = build_proposal(
        ChatAnswer(changes=[ChatChange(id=first.id, rewritten="Built workflows with Python.")]),
        document,
        corpus,
    )
    elsewhere = document.tex.replace(second.tex, r"\item My own words about Python.")
    out = apply_to_tex(elsewhere, proposal, None)
    assert r"\item Built workflows with Python." in out
    assert r"\item My own words about Python." in out


def test_model_cannot_smuggle_latex(document: ResumeDocument, corpus: FactCorpus) -> None:
    target = _bullet(document)
    proposal = build_proposal(
        ChatAnswer(changes=[ChatChange(id=target.id, rewritten=r"Python \input{/etc/passwd}")]),
        document,
        corpus,
    )
    out = apply_to_tex(document.tex, proposal, None)
    assert r"\input{/etc/passwd}" not in out
