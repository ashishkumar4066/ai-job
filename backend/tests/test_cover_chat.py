"""Stage 3 — the cover letter chat's guards, driven with canned answers.

`test_resume_chat.py`'s sibling. The letter's chat has one weaker structural
guarantee than the résumé's — it *can* add a paragraph, where the résumé has no
slot to add a bullet to — so the fact check is the only thing standing between a
request for an invented claim and a letter carrying one. These tests drive
`build_proposal` / `apply_to_paragraphs` with no model and no network.

`test_a_paragraph_with_one_bad_sentence_is_blocked_whole` is the load-bearing
one: generation removes an offending sentence because it has no fallback, but a
chat does, and silently returning a shortened paragraph would hide the refusal.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app import cover_letter
from app.cover_chat import (
    ChatAnswer,
    ChatChange,
    _body_text,
    apply_to_paragraphs,
    build_proposal,
)
from app.documents import DocumentError, tex_hash
from app.factcheck import FactCorpus
from app.profile import Profile, load_profile

PROFILE = Path(__file__).resolve().parent.parent / "profile.yaml"

pytestmark = pytest.mark.skipif(
    not PROFILE.is_file(),
    reason="profile.yaml holds personal data and is gitignored on a fresh checkout",
)

PARAGRAPHS = [
    "I am applying for the AI Engineer role.",
    "At AsInt I built a Text-to-SQL engine with schema-grounded RAG.",
    "I would welcome the chance to talk.",
]


@pytest.fixture
def profile() -> Profile:
    return load_profile(PROFILE)


@pytest.fixture
def corpus(profile: Profile) -> FactCorpus:
    return cover_letter.build_corpus(
        profile,
        company="Pulsora",
        title="AI Engineer",
        description_text="We build carbon accounting software. Requires 5+ years of Kubernetes.",
    )


def _proposal(answer: ChatAnswer, corpus: FactCorpus, paragraphs: list[str] | None = None) -> dict:
    return build_proposal(answer, list(paragraphs or PARAGRAPHS), corpus)


# --------------------------------------------------------------------------
# The fact check
# --------------------------------------------------------------------------


def test_asking_for_a_gap_stack_is_blocked(corpus: FactCorpus) -> None:
    """"Say I know Kubernetes" must come back blocked, visible, unapplicable."""
    answer = ChatAnswer(
        changes=[ChatChange(id="para.1", rewritten="I have run Kubernetes clusters in production.")],
        reply="Added Kubernetes.",
    )
    proposal = _proposal(answer, corpus)
    [change] = proposal["changes"]
    assert change["status"] == "blocked"
    assert any("kubernetes" in reason.lower() for reason in change["blocked"])
    with pytest.raises(DocumentError):
        apply_to_paragraphs(list(PARAGRAPHS), proposal, None)


def test_the_jd_asking_for_a_stack_does_not_vouch_for_it(corpus: FactCorpus) -> None:
    """The posting names Kubernetes; that is a requirement, not evidence.

    The corpus takes the JD's words so the letter may describe the employer, and
    this is the line that must not be crossed by doing so.
    """
    answer = ChatAnswer(changes=[ChatChange(id="para.0", rewritten="I bring 5+ years of Kubernetes.")])
    [change] = _proposal(answer, corpus)["changes"]
    assert change["status"] == "blocked"


def test_a_paragraph_with_one_bad_sentence_is_blocked_whole(corpus: FactCorpus) -> None:
    """Not silently shortened: the user asked for something and did not get it."""
    answer = ChatAnswer(
        changes=[
            ChatChange(
                id="para.1",
                rewritten=(
                    "At AsInt I built a Text-to-SQL engine with schema-grounded RAG. "
                    "I also ran Kubernetes in production."
                ),
            )
        ]
    )
    [change] = _proposal(answer, corpus)["changes"]
    assert change["status"] == "blocked"
    # The offending sentence is named, so the reason is actionable.
    assert any("Kubernetes" in reason for reason in change["blocked"])
    # And the text offered is still the whole paragraph, not a trimmed one.
    assert "Text-to-SQL" in change["after"] and "Kubernetes" in change["after"]


def test_describing_the_employer_is_not_flagged(corpus: FactCorpus) -> None:
    """The letter's whole job. The JD's vocabulary has to be allowed."""
    answer = ChatAnswer(
        changes=[ChatChange(id="para.0", rewritten="Your carbon accounting software is why I applied.")]
    )
    [change] = _proposal(answer, corpus)["changes"]
    assert change["status"] == "proposed"


def test_a_true_claim_from_the_profile_passes(corpus: FactCorpus) -> None:
    answer = ChatAnswer(
        changes=[
            ChatChange(id="para.1", rewritten="I built a Text-to-SQL engine with guardrails at AsInt.")
        ]
    )
    [change] = _proposal(answer, corpus)["changes"]
    assert change["status"] == "proposed", change["blocked"]


# --------------------------------------------------------------------------
# Addressing paragraphs
# --------------------------------------------------------------------------


def test_a_rewrite_replaces_only_that_paragraph(corpus: FactCorpus) -> None:
    answer = ChatAnswer(changes=[ChatChange(id="para.2", rewritten="Thank you for your time.")])
    out = apply_to_paragraphs(list(PARAGRAPHS), _proposal(answer, corpus), None)
    assert out[:2] == PARAGRAPHS[:2]
    assert out[2] == "Thank you for your time."


def test_a_paragraph_can_be_deleted(corpus: FactCorpus) -> None:
    answer = ChatAnswer(changes=[ChatChange(id="para.2", include=False, rewritten="")])
    proposal = _proposal(answer, corpus)
    assert proposal["changes"][0]["action"] == "drop"
    assert apply_to_paragraphs(list(PARAGRAPHS), proposal, None) == PARAGRAPHS[:2]


def test_one_past_the_end_appends(corpus: FactCorpus) -> None:
    """A letter has no fixed slots, unlike the résumé, so adding is allowed."""
    answer = ChatAnswer(changes=[ChatChange(id="para.3", rewritten="I am available immediately.")])
    proposal = _proposal(answer, corpus)
    assert proposal["changes"][0]["action"] == "add"
    out = apply_to_paragraphs(list(PARAGRAPHS), proposal, None)
    assert len(out) == 4 and out[3] == "I am available immediately."


def test_a_wild_index_is_ignored_rather_than_leaving_a_gap(corpus: FactCorpus) -> None:
    answer = ChatAnswer(changes=[ChatChange(id="para.9", rewritten="Out of range.")])
    assert _proposal(answer, corpus)["changes"] == []


def test_an_unknown_region_id_is_ignored(corpus: FactCorpus) -> None:
    answer = ChatAnswer(changes=[ChatChange(id="summary", rewritten="Not a paragraph id.")])
    assert _proposal(answer, corpus)["changes"] == []


def test_a_no_op_rewrite_is_not_a_change(corpus: FactCorpus) -> None:
    answer = ChatAnswer(changes=[ChatChange(id="para.0", rewritten=PARAGRAPHS[0])])
    proposal = _proposal(answer, corpus)
    assert proposal["changes"] == []
    assert proposal["status"] == "none"


def test_the_letter_cannot_be_emptied(corpus: FactCorpus) -> None:
    answer = ChatAnswer(
        changes=[ChatChange(id=f"para.{i}", include=False, rewritten="") for i in range(3)]
    )
    with pytest.raises(DocumentError, match="whole letter"):
        apply_to_paragraphs(list(PARAGRAPHS), _proposal(answer, corpus), None)


def test_only_ticked_changes_are_applied(corpus: FactCorpus) -> None:
    answer = ChatAnswer(
        changes=[
            ChatChange(id="para.0", rewritten="First, rewritten."),
            ChatChange(id="para.2", rewritten="Third, rewritten."),
        ]
    )
    out = apply_to_paragraphs(list(PARAGRAPHS), _proposal(answer, corpus), ["para.2"])
    assert out[0] == PARAGRAPHS[0]
    assert out[2] == "Third, rewritten."


# --------------------------------------------------------------------------
# Reordering
# --------------------------------------------------------------------------


def test_reordering_moves_the_paragraphs(corpus: FactCorpus) -> None:
    answer = ChatAnswer(order=["para.1", "para.0", "para.2"])
    proposal = _proposal(answer, corpus)
    assert proposal["order"] == ["para.1", "para.0", "para.2"]
    out = apply_to_paragraphs(list(PARAGRAPHS), proposal, None)
    assert out == [PARAGRAPHS[1], PARAGRAPHS[0], PARAGRAPHS[2]]


def test_a_partial_order_is_refused_rather_than_dropping_the_rest(corpus: FactCorpus) -> None:
    """Applying it literally would delete the paragraphs it forgot to mention."""
    answer = ChatAnswer(order=["para.2", "para.0"])
    assert _proposal(answer, corpus)["order"] == []


def test_an_order_identical_to_the_current_one_is_not_a_proposal(corpus: FactCorpus) -> None:
    answer = ChatAnswer(order=["para.0", "para.1", "para.2"])
    assert _proposal(answer, corpus)["status"] == "none"


def test_a_drop_and_a_reorder_together_keep_the_right_text(corpus: FactCorpus) -> None:
    """Paragraphs are keyed by id through the rewrite, not by position."""
    answer = ChatAnswer(
        changes=[ChatChange(id="para.0", include=False, rewritten="")],
        order=["para.2", "para.1", "para.0"],
    )
    out = apply_to_paragraphs(list(PARAGRAPHS), _proposal(answer, corpus), None)
    assert out == [PARAGRAPHS[2], PARAGRAPHS[1]]


# --------------------------------------------------------------------------
# Staleness
# --------------------------------------------------------------------------


def test_a_stale_proposal_refuses_when_its_paragraph_moved(corpus: FactCorpus) -> None:
    answer = ChatAnswer(changes=[ChatChange(id="para.1", rewritten="A fresh second paragraph.")])
    proposal = _proposal(answer, corpus)
    edited = [PARAGRAPHS[0], "I rewrote this myself in the meantime.", PARAGRAPHS[2]]
    with pytest.raises(DocumentError, match="changed since"):
        apply_to_paragraphs(edited, proposal, None)


def test_a_stale_proposal_still_applies_when_its_paragraph_is_untouched(
    corpus: FactCorpus,
) -> None:
    """A hand edit elsewhere must not invalidate an unrelated suggestion."""
    answer = ChatAnswer(changes=[ChatChange(id="para.1", rewritten="A fresh second paragraph.")])
    proposal = _proposal(answer, corpus)
    edited = [PARAGRAPHS[0], PARAGRAPHS[1], "I rewrote the closing myself."]
    out = apply_to_paragraphs(edited, proposal, None)
    assert out[1] == "A fresh second paragraph."
    assert out[2] == "I rewrote the closing myself."


def test_the_hash_covers_the_body_only(corpus: FactCorpus) -> None:
    """Chat only writes the body, so only the body's movement invalidates it."""
    proposal = _proposal(ChatAnswer(changes=[ChatChange(id="para.0", rewritten="New.")]), corpus)
    assert proposal["base_hash"] == tex_hash(_body_text(list(PARAGRAPHS)))


# --------------------------------------------------------------------------
# Writing back into the LaTeX
# --------------------------------------------------------------------------


def test_replacing_the_body_leaves_the_header_and_date_alone(profile: Profile) -> None:
    """Chat must not restamp a letter's date, which re-rendering would do."""
    tex = cover_letter.render_letter(
        list(PARAGRAPHS), profile=profile, company="Pulsora", title="AI Engineer", dated="12 March 2026"
    )
    out = cover_letter.replace_body(tex, ["Only one paragraph now."])

    assert "12 March 2026" in out
    assert "Pulsora" in out
    assert out.split(cover_letter.BODY_START)[0] == tex.split(cover_letter.BODY_START)[0]
    assert out.split(cover_letter.BODY_END)[1] == tex.split(cover_letter.BODY_END)[1]
    assert cover_letter.body_paragraphs(out) == ["Only one paragraph now."]


def test_the_model_cannot_smuggle_latex_through_a_paragraph(profile: Profile) -> None:
    tex = cover_letter.render_letter(
        list(PARAGRAPHS), profile=profile, company="Pulsora", title="AI Engineer", dated="12 March 2026"
    )
    out = cover_letter.replace_body(tex, [r"\input{/etc/passwd} \textbf{bold}"])
    body = out.split(cover_letter.BODY_START)[1].split(cover_letter.BODY_END)[0]
    assert r"\input" not in body
    assert r"\textbf{bold}" not in body
    assert "input" in body  # kept, but as literal characters


def test_a_letter_whose_markers_are_gone_is_refused_not_guessed(profile: Profile) -> None:
    with pytest.raises(ValueError, match="markers"):
        cover_letter.replace_body("no markers here", ["Something."])


def test_a_round_trip_through_the_latex_preserves_the_paragraphs(profile: Profile) -> None:
    tex = cover_letter.render_letter(
        list(PARAGRAPHS), profile=profile, company="Pulsora", title="AI Engineer", dated="12 March 2026"
    )
    assert cover_letter.body_paragraphs(tex) == PARAGRAPHS


# --------------------------------------------------------------------------
# The prompt's own shape
# --------------------------------------------------------------------------


def test_the_proposal_shape_matches_the_resume_chats() -> None:
    """One panel in the UI renders both kinds, so the keys cannot drift."""
    from app import cover_chat, resume_chat

    assert set(cover_chat.ChatAnswer.model_fields) == set(resume_chat.ChatAnswer.model_fields)
    assert sorted(cover_chat.CHAT_SCHEMA["required"]) == sorted(
        resume_chat.CHAT_SCHEMA["required"]
    )


def test_every_schema_field_carries_a_description() -> None:
    from app.cover_chat import CHAT_SCHEMA

    def walk(node: dict, path: str) -> None:
        for name, child in (node.get("properties") or {}).items():
            assert child.get("description"), f"{path}.{name}"
            walk(child, f"{path}.{name}")
        if isinstance(node.get("items"), dict):
            walk(node["items"], f"{path}[]")

    walk(CHAT_SCHEMA, "cover_chat")


def test_the_module_exposes_the_same_service_functions_as_the_resume_chat() -> None:
    """The API picks a module by kind and otherwise does not branch."""
    from app import cover_chat, resume_chat

    for name in ("send_message", "apply_proposal", "dismiss_proposal", "clear_chat", "suggestions"):
        assert callable(getattr(cover_chat, name)), name
        assert callable(getattr(resume_chat, name)), name
