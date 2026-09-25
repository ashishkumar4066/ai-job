"""Stage 3 — the cover letter's fact check and its LaTeX.

The test this file exists for is `test_invented_metric_sentence_is_removed`:
the résumé's guarantee (*no claim absent from `profile.yaml`*) applied to
prose. It drives `apply_draft` with a canned completion — no network, no
model — so the guarantee is asserted about the code, not about a prompt.

Needs only `profile.yaml`, not the gitignored `data/` template.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.cover_letter import (
    BODY_END,
    BODY_START,
    CoverLetterDraft,
    apply_draft,
    body_paragraphs,
    build_corpus,
    check_letter,
    render_letter,
    split_sentences,
)
from app.factcheck import FactCorpus
from app.profile import Profile, load_profile

PROFILE = Path(__file__).resolve().parent.parent / "profile.yaml"

JD = (
    "We are hiring a Senior AI Engineer to build retrieval systems on Postgres. "
    "You have 5+ years of Python experience and ship evaluation pipelines."
)


@pytest.fixture
def profile() -> Profile:
    return load_profile(PROFILE)


@pytest.fixture
def corpus(profile: Profile) -> FactCorpus:
    return build_corpus(profile, company="Acme Retrieval", title="Senior AI Engineer", description_text=JD)


def _apply(draft: CoverLetterDraft, corpus: FactCorpus, profile: Profile):
    return apply_draft(
        draft, corpus, profile=profile, company="Acme Retrieval", title="Senior AI Engineer",
        dated="22 September 2026",
    )


def test_invented_metric_sentence_is_removed(profile: Profile, corpus: FactCorpus) -> None:
    """A letter cannot keep an original line, so the offending SENTENCE goes."""
    draft = CoverLetterDraft(
        paragraphs=[
            "I built a **Text-to-SQL** engine with **75%** accuracy across 200+ schemas. "
            "It later reached **98%** accuracy for 900 customers.",
        ]
    )
    tex, paragraphs, issues = _apply(draft, corpus, profile)

    assert "75" in tex
    assert "98" not in tex and "900" not in tex
    assert paragraphs == [
        r"I built a **Text-to-SQL** engine with **75%** accuracy across 200+ schemas."
    ]
    assert {"98", "900"} <= {i.token for i in issues if i.severity == "blocking"}


def test_jd_numbers_do_not_vouch_for_the_candidate(profile: Profile, corpus: FactCorpus) -> None:
    """The JD says 5+ years; that is a requirement, not a fact about the candidate.

    The profile states 6 years — so a letter claiming the JD's figure is
    stopped only if the figure is absent from the profile. Use one that is.
    """
    draft = CoverLetterDraft(paragraphs=["I have 15 years of Python in production."])
    _, paragraphs, issues = _apply(draft, corpus, profile)
    assert paragraphs == []
    assert "15" in {i.token for i in issues if i.severity == "blocking"}


def test_jd_vocabulary_does_not_warn(corpus: FactCorpus) -> None:
    """Naming what the employer builds is the point of a letter."""
    assert not [i for i in corpus.check("x", "Acme Retrieval builds evaluation pipelines") if i.severity == "warning"]


def test_gap_stack_is_blocked(profile: Profile, corpus: FactCorpus) -> None:
    draft = CoverLetterDraft(
        paragraphs=["I deployed agents on GCP. I also ran them on Kubernetes for years."]
    )
    _, paragraphs, issues = _apply(draft, corpus, profile)
    assert paragraphs == ["I deployed agents on GCP."]
    assert "kubernetes" in {i.token for i in issues if i.severity == "blocking"}


def test_model_text_cannot_inject_latex(profile: Profile, corpus: FactCorpus) -> None:
    draft = CoverLetterDraft(paragraphs=[r"I built RAG \input{/etc/passwd} systems & tools."])
    tex, _, _ = _apply(draft, corpus, profile)
    assert r"\input{" not in tex
    assert r"\textbackslash{}input\{/etc/passwd\}" in tex
    assert r"\&" in tex


def test_header_comes_from_the_profile(profile: Profile) -> None:
    tex = render_letter(
        ["Body."], profile=profile, company="R&D Co", title="AI Engineer", dated="1 January 2026"
    )
    assert profile.identity.full_name in tex
    assert f"mailto:{profile.identity.email}" in tex
    assert r"Dear R\&D Co hiring team," in tex
    assert tex.index(BODY_START) < tex.index("Body.") < tex.index(BODY_END)


def test_body_round_trips_for_the_hand_edit_check(profile: Profile, corpus: FactCorpus) -> None:
    paragraphs = ["I built **LLM observability** from scratch.", "Prompt caching cut cost ~90%."]
    tex = render_letter(
        paragraphs, profile=profile, company="Acme", title="AI Engineer", dated="1 January 2026"
    )
    assert body_paragraphs(tex) == paragraphs
    assert check_letter(tex, corpus) == []

    edited = tex.replace("~90", "~99").replace(r"$\sim$90", r"$\sim$99")
    assert any("99" in message for message in check_letter(edited, corpus))

    unmarked = tex.replace(BODY_START, "")
    assert check_letter(unmarked, corpus) == [
        "the body markers were removed, so the letter was not fact-checked"
    ]


def test_sentence_split_keeps_dotted_names() -> None:
    assert split_sentences("Built it in Node.js and React. Shipped e.g. SSO. Done!") == [
        "Built it in Node.js and React.",
        "Shipped e.g. SSO.",
        "Done!",
    ]
