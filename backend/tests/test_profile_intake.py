"""Stage 1 — reading a résumé into `profile.yaml`, and writing it back.

Two guarantees are worth more than the rest here.

`test_a_drafted_gap_that_is_also_a_skill_is_dropped` protects the fact checker:
`gaps:` is its denylist and `skills:` its allowlist, so a term in both would
block a claim the résumé actually supports.

`test_a_rejected_save_leaves_the_file_untouched` protects the app's ability to
boot: `load_profile` raises rather than degrading, so a save that writes a file
the loader then refuses would break scoring with no visible cause.

The de-TeX tests use inline LaTeX rather than `data/`, which is gitignored as
PII — they have to run on a fresh checkout, since that is exactly the state a
first-time upload happens in.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from app import profile_intake
from app.profile import ProfileError, load_profile
from app.profile_intake import (
    DraftEvidence,
    DraftGap,
    DraftLink,
    DraftRole,
    DraftSkill,
    ProfileDraft,
    draft_to_profile_data,
    read_profile_data,
    save_profile,
    store_resume_files,
    tex_to_text,
)
from app.resume_tex import ResumeDocument, resolve_template

# Mirrors the real template's structure — rSection, an \hfill header line and an
# itemize list — because `resume_tex` parses bullets out of itemize. An earlier
# version of this fixture used rSubsection, parsed to zero bullets, and so
# asserted nothing about the path tailoring actually takes.
MINIMAL_TEX = r"""
\documentclass{resume}
\usepackage{geometry}
\def\sectionskip{\vspace{3pt}}
\begin{document}
\name{Priya Raman}
\address{\href{https://github.com/priya}{github.com/priya}}

\begin{rSection}{SUMMARY}
\vspace{3pt}
\textbf{Backend engineer} who ships payments infrastructure. Cut latency
$\sim$40\% on a service handling 2,000+ requests/second. Costs under \$50K.
\end{rSection}

\begin{rSection}{EXPERIENCE}
\textbf{Senior Engineer} \hfill Mar 2021 -- Present\\
Fintech Co \hfill \textit{Chennai, India}

\begin{itemize}
    \itemsep -3pt {}
    \item Built a \textbf{ledger service} in \textbf{Go} \& Postgres, settling 1M+ rows daily.
    \item Led 3 engineers through a migration off a monolith.
\end{itemize}
\end{rSection}

\begin{rSection}{SKILLS}
\begin{tabular}{ @{} >{\bfseries}l @{\hspace{6ex}} p{4.5in} }
Back-End & Go, Postgres, gRPC \\
Cloud & GCP, Docker \\
\end{tabular}
\end{rSection}
\end{document}
"""

# The other header form the class supports, for the de-TeX pass only.
SUBSECTION_TEX = r"""
\begin{document}
\begin{rSection}{EXPERIENCE}
\begin{rSubsection}{Staff Engineer}{Jan 2019 - Feb 2021}{Acme}{Pune, India}
    \item Ran the platform team.
\end{rSubsection}
\end{rSection}
\end{document}
"""


# --------------------------------------------------------------------------
# LaTeX -> readable text
# --------------------------------------------------------------------------


def test_the_prose_survives_and_the_markup_does_not() -> None:
    text = tex_to_text(MINIMAL_TEX)
    assert "Backend engineer who ships payments infrastructure." in " ".join(text.split())
    assert "Built a ledger service in Go & Postgres, settling 1M+ rows daily." in text
    for markup in ("\\", "{", "}", "rSection", "itemsep", "textbf", "vspace"):
        assert markup not in text, markup


def test_section_headings_are_kept_as_structure() -> None:
    text = tex_to_text(MINIMAL_TEX)
    assert "## SUMMARY" in text
    assert "## EXPERIENCE" in text
    assert "## SKILLS" in text


def test_the_preamble_is_dropped() -> None:
    # `\def\sectionskip{\vspace{3pt}}` lives before \begin{document}.
    assert "sectionskip" not in tex_to_text(MINIMAL_TEX)


def test_math_shorthand_becomes_readable_and_a_real_dollar_survives() -> None:
    text = tex_to_text(MINIMAL_TEX)
    # `$\sim$40\%` degraded to "$$40%" before `_MATH` and the `$` sentinel.
    assert "~40%" in text
    assert "$$" not in text
    assert "$50K" in text


def test_a_bare_length_does_not_leak_into_the_prose() -> None:
    # `\itemsep -3pt {}` has no braces, so stripping the macro alone left
    # "-3pt" sitting mid-résumé.
    assert "3pt" not in tex_to_text(MINIMAL_TEX)


def test_a_nested_tabular_column_spec_is_not_read_as_text() -> None:
    # `{ @{} >{\bfseries}l @{\hspace{6ex}} p{4.5in} }` is three braces deep, so
    # a non-greedy match stopped inside it and spilled ">p4.5in @" into the text.
    text = tex_to_text(MINIMAL_TEX)
    for fragment in ("4.5in", "bfseries", ">", "@"):
        assert fragment not in text, fragment


def test_a_tabular_row_reads_as_a_labelled_line() -> None:
    assert "Back-End: Go, Postgres, gRPC" in tex_to_text(MINIMAL_TEX)


def test_an_escaped_ampersand_is_not_confused_with_a_column_separator() -> None:
    assert "Go & Postgres" in tex_to_text(MINIMAL_TEX)


def test_href_keeps_both_the_label_and_the_url() -> None:
    text = tex_to_text(MINIMAL_TEX)
    assert "github.com/priya" in text
    assert "https://github.com/priya" in text


def test_the_other_header_form_keeps_its_role_and_dates() -> None:
    """`rSubsection{title}{dates}{company}{place}` is the class's other form.

    Its four arguments are the only place the employer and dates appear, so
    dropping the environment whole would lose them.
    """
    text = tex_to_text(SUBSECTION_TEX)
    for fact in ("Staff Engineer", "Jan 2019 - Feb 2021", "Acme", "Pune, India"):
        assert fact in text, fact
    assert "rSubsection" not in text


def test_a_file_with_no_document_body_still_yields_its_text() -> None:
    assert "Hello there" in tex_to_text(r"\textbf{Hello there} and more words")


def test_an_empty_file_does_not_raise() -> None:
    assert tex_to_text("") == ""


# --------------------------------------------------------------------------
# Draft -> the mapping `profile.yaml` holds
# --------------------------------------------------------------------------


def _draft(**overrides: object) -> ProfileDraft:
    base: dict[str, object] = {
        "full_name": "Priya Raman",
        "email": "priya@example.com",
        "location": "Chennai, India",
        "links": [DraftLink(name="GitHub", url="https://github.com/priya")],
        "summary": "Backend engineer who ships payments infrastructure.",
        "total_years": 5,
        "ai_years": 1,
        "current_title": "Senior Engineer",
        "target_titles": ["Backend Engineer"],
        "skills": [
            DraftSkill(category="Backend", name="Go", weight=3),
            DraftSkill(category="cloud", name="gcp", weight=2),
        ],
        "gaps": [DraftGap(name="Kubernetes", weight=2)],
        "evidence": [DraftEvidence(area="Ledgers", depth="production", proof="Settled 1M+ rows.")],
        "unproven": ["no people management"],
        "domains": ["fintech"],
        "experience": [
            DraftRole(company="Fintech Co", title="Senior Engineer", bullets=["Built a ledger."])
        ],
    }
    base.update(overrides)
    return ProfileDraft(**base)  # type: ignore[arg-type]


def test_a_draft_becomes_a_loadable_profile(tmp_path: Path) -> None:
    data = draft_to_profile_data(_draft())
    path = tmp_path / "profile.yaml"
    save_profile(data, path)
    profile = load_profile(path)
    assert profile.identity.full_name == "Priya Raman"
    assert profile.flat_skills["go"] == 3
    assert profile.gaps["kubernetes"] == 2


def test_skills_and_gaps_are_lowercased_and_nested() -> None:
    data = draft_to_profile_data(_draft())
    assert data["skills"] == {"backend": {"go": 3}, "cloud": {"gcp": 2}}
    assert data["gaps"] == {"kubernetes": 2}


def test_a_drafted_gap_that_is_also_a_skill_is_dropped() -> None:
    """`gaps:` blocks claims; `skills:` permits them. A term cannot be both.

    Left in, the fact checker would refuse a rewrite mentioning Go on a résumé
    whose own skills list says Go — and `gaps:` is blocking, so the rewrite is
    silently discarded rather than warned about.
    """
    data = draft_to_profile_data(_draft(gaps=[DraftGap(name="go", weight=2), DraftGap(name="aws")]))
    assert "go" not in data["gaps"]
    assert "aws" in data["gaps"]


def test_ai_years_cannot_exceed_total_years() -> None:
    data = draft_to_profile_data(_draft(total_years=3, ai_years=9))
    assert data["seniority"] == {
        "total_years": 3,
        "ai_years": 3,
        "current_title": "Senior Engineer",
        "target_titles": ["Backend Engineer"],
    }


def test_weights_are_clamped_to_the_scale_the_ranker_expects() -> None:
    data = draft_to_profile_data(
        _draft(skills=[DraftSkill(category="a", name="x", weight=9), DraftSkill(category="a", name="y", weight=-2)])
    )
    assert data["skills"]["a"] == {"x": 3, "y": 1}


def test_an_unknown_evidence_depth_falls_back_to_the_cautious_one() -> None:
    data = draft_to_profile_data(_draft(evidence=[DraftEvidence(area="X", depth="expert", proof="p")]))
    assert data["evidence"][0]["depth"] == "project"


def test_blocks_intake_cannot_read_are_preserved(tmp_path: Path) -> None:
    """A re-parse must not wipe the pay floor or the Phase 3 answers.

    Neither is on the résumé, so the model has nothing to say about them, and
    an overwrite would silently reset the Matches gate's comp filter.
    """
    existing = {
        "compensation": {"min_annual_inr": 3_000_000.0, "preferred_currency": "USD"},
        "work_authorization": {"needs_sponsorship": True, "remote_only": True},
        "answers": {"notice_period": "60 days"},
        "resume_files": {"base": "data/old.pdf"},
    }
    data = draft_to_profile_data(_draft(), existing=existing)
    assert data["compensation"]["min_annual_inr"] == 3_000_000.0
    assert data["answers"]["notice_period"] == "60 days"
    assert data["resume_files"]["base"] == "data/old.pdf"


def test_identity_keeps_a_hand_set_country_and_timezone() -> None:
    data = draft_to_profile_data(
        _draft(), existing={"identity": {"country": "SG", "timezone": "Asia/Singapore"}}
    )
    assert data["identity"]["country"] == "SG"
    assert data["identity"]["timezone"] == "Asia/Singapore"
    assert data["identity"]["full_name"] == "Priya Raman"


# --------------------------------------------------------------------------
# Saving
# --------------------------------------------------------------------------


def test_a_profile_with_no_skills_is_refused(tmp_path: Path) -> None:
    data = draft_to_profile_data(_draft(skills=[]))
    with pytest.raises(ProfileError, match="skills"):
        save_profile(data, tmp_path / "profile.yaml")


def test_a_profile_with_no_name_is_refused(tmp_path: Path) -> None:
    """Generated documents are signed with it, so an empty name ships blank."""
    data = draft_to_profile_data(_draft(full_name="  "))
    with pytest.raises(ProfileError, match="name"):
        save_profile(data, tmp_path / "profile.yaml")


def test_a_rejected_save_leaves_the_file_untouched(tmp_path: Path) -> None:
    path = tmp_path / "profile.yaml"
    save_profile(draft_to_profile_data(_draft()), path)
    before = path.read_text(encoding="utf-8")

    with pytest.raises(ProfileError):
        save_profile({"identity": {"full_name": "Nobody"}}, path)
    assert path.read_text(encoding="utf-8") == before
    assert load_profile(path).identity.full_name == "Priya Raman"


def test_the_written_file_carries_the_warning_about_gaps(tmp_path: Path) -> None:
    """The denylist's role has to be discoverable by whoever hand-edits next."""
    path = tmp_path / "profile.yaml"
    save_profile(draft_to_profile_data(_draft()), path)
    assert "DENYLIST" in path.read_text(encoding="utf-8")


def test_unknown_keys_survive_a_round_trip(tmp_path: Path) -> None:
    """The editor round-trips the raw mapping, so a hand-added key must live.

    `Profile` ignores keys it does not declare; saving the *model* rather than
    the mapping would delete them without a word.
    """
    path = tmp_path / "profile.yaml"
    data = draft_to_profile_data(_draft())
    data["my_own_notes"] = ["keep me"]
    save_profile(data, path)
    assert read_profile_data(path)["my_own_notes"] == ["keep me"]


def test_keys_are_written_in_reading_order(tmp_path: Path) -> None:
    path = tmp_path / "profile.yaml"
    save_profile(draft_to_profile_data(_draft()), path)
    body = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert list(body)[:3] == ["identity", "summary", "seniority"]


def test_reading_a_missing_profile_is_empty_not_an_error(tmp_path: Path) -> None:
    assert read_profile_data(tmp_path / "nothing.yaml") == {}


def test_a_malformed_profile_names_the_file(tmp_path: Path) -> None:
    path = tmp_path / "profile.yaml"
    path.write_text("identity: [unclosed\n", encoding="utf-8")
    with pytest.raises(ProfileError, match="profile.yaml"):
        read_profile_data(path)


# --------------------------------------------------------------------------
# Storing the uploaded files
# --------------------------------------------------------------------------


def test_the_tex_lands_where_the_template_loader_looks(tmp_path: Path) -> None:
    stored = store_resume_files(MINIMAL_TEX, data_dir=tmp_path)
    assert (tmp_path / "resume.tex").read_text(encoding="utf-8") == MINIMAL_TEX
    assert stored["tex"].endswith("resume.tex")


def test_an_uploaded_template_parses_into_regions(tmp_path: Path) -> None:
    """The point of storing it: tailoring has to be able to address it."""
    store_resume_files(MINIMAL_TEX, data_dir=tmp_path)
    document = ResumeDocument.load(tmp_path / "resume.tex")
    assert [r.id for r in document.regions][:1] == ["summary"]
    assert any(r.kind == "bullet" for r in document.regions)


def test_a_hostile_pdf_filename_cannot_escape_the_data_directory(tmp_path: Path) -> None:
    stored = store_resume_files(
        None, resume_bytes=b"%PDF-1.4", resume_name="../../../etc/passwd", data_dir=tmp_path
    )
    written = [p.name for p in tmp_path.iterdir() if p.is_file()]
    assert written == ["passwd.pdf"]
    assert ".." not in stored["base"]


def test_an_extensionless_upload_is_named_as_a_pdf(tmp_path: Path) -> None:
    store_resume_files(None, resume_bytes=b"%PDF-1.4", resume_name="resume", data_dir=tmp_path)
    assert (tmp_path / "resume.pdf").is_file()


def test_a_docx_keeps_its_own_extension(tmp_path: Path) -> None:
    store_resume_files(None, resume_bytes=b"PK", resume_name="CV final.docx", data_dir=tmp_path)
    assert (tmp_path / "CV_final.docx").is_file()


def test_a_crlf_tex_is_stored_byte_for_byte(tmp_path: Path) -> None:
    """Found live: a real Windows .tex came back 153 bytes larger.

    `write_text` applies the platform newline translation, so the `\\n` of an
    already-CRLF upload was rewritten and every line ending became `\\r\\r\\n`.
    """
    crlf = MINIMAL_TEX.replace("\n", "\r\n")
    store_resume_files(crlf, data_dir=tmp_path)
    assert (tmp_path / "resume.tex").read_bytes() == crlf.encode("utf-8")


def test_an_lf_tex_is_stored_byte_for_byte(tmp_path: Path) -> None:
    store_resume_files(MINIMAL_TEX, data_dir=tmp_path)
    assert (tmp_path / "resume.tex").read_bytes() == MINIMAL_TEX.encode("utf-8")


def test_no_temporary_file_is_left_behind(tmp_path: Path) -> None:
    store_resume_files(MINIMAL_TEX, resume_bytes=b"%PDF-1.4", resume_name="r.pdf", data_dir=tmp_path)
    assert [p.name for p in tmp_path.glob("*.tmp")] == []


# --------------------------------------------------------------------------
# Which .tex is the base résumé
# --------------------------------------------------------------------------


def test_an_upload_takes_priority_over_the_legacy_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An upload has to take effect, or the button does nothing visible."""
    monkeypatch.setattr("app.resume_tex.DATA_DIR", tmp_path)
    monkeypatch.setattr("app.resume_tex.UPLOADED_TEMPLATE", tmp_path / "resume.tex")
    monkeypatch.setattr("app.resume_tex.LEGACY_TEMPLATE", tmp_path / "legacy.tex")
    (tmp_path / "legacy.tex").write_text("legacy", encoding="utf-8")
    assert resolve_template().name == "legacy.tex"

    (tmp_path / "resume.tex").write_text(MINIMAL_TEX, encoding="utf-8")
    assert resolve_template().name == "resume.tex"


def test_a_lone_tex_under_any_name_is_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`data/` is gitignored, so a checkout has whatever the user dropped in."""
    monkeypatch.setattr("app.resume_tex.DATA_DIR", tmp_path)
    monkeypatch.setattr("app.resume_tex.UPLOADED_TEMPLATE", tmp_path / "resume.tex")
    monkeypatch.setattr("app.resume_tex.LEGACY_TEMPLATE", tmp_path / "legacy.tex")
    (tmp_path / "My CV.tex").write_text(MINIMAL_TEX, encoding="utf-8")
    assert resolve_template().name == "My CV.tex"


def test_an_explicit_path_always_wins(tmp_path: Path) -> None:
    target = tmp_path / "given.tex"
    assert resolve_template(target) == target


def test_a_missing_template_names_where_to_put_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.resume_tex.DATA_DIR", tmp_path)
    monkeypatch.setattr("app.resume_tex.UPLOADED_TEMPLATE", tmp_path / "resume.tex")
    monkeypatch.setattr("app.resume_tex.LEGACY_TEMPLATE", tmp_path / "legacy.tex")
    assert resolve_template().name == "resume.tex"


# --------------------------------------------------------------------------
# The prompt's own shape
# --------------------------------------------------------------------------


def test_every_schema_field_carries_a_description() -> None:
    """Measured in 2B: without descriptions the models answer off-scale."""

    def walk(node: dict[str, object], path: str) -> None:
        properties = node.get("properties")
        if isinstance(properties, dict):
            for name, child in properties.items():
                assert isinstance(child, dict)
                assert child.get("description"), f"{path}.{name} has no description"
                walk(child, f"{path}.{name}")
        items = node.get("items")
        if isinstance(items, dict):
            walk(items, f"{path}[]")

    walk(profile_intake.INTAKE_SCHEMA, "intake")


def test_every_object_in_the_schema_is_closed() -> None:
    """Strict `json_schema` rejects an object that omits this."""

    def walk(node: dict[str, object]) -> None:
        if node.get("type") == "object":
            assert node.get("additionalProperties") is False
            assert node.get("required"), "a closed object needs its required list"
            assert sorted(node["required"]) == sorted(node.get("properties", {}))  # type: ignore[arg-type]
        for child in (node.get("properties") or {}).values():  # type: ignore[union-attr]
            walk(child)
        if isinstance(node.get("items"), dict):
            walk(node["items"])  # type: ignore[arg-type]

    walk(profile_intake.INTAKE_SCHEMA)


def test_the_schema_and_the_model_describe_the_same_object() -> None:
    assert sorted(profile_intake.INTAKE_SCHEMA["required"]) == sorted(
        ProfileDraft.model_fields
    )


def test_list_limits_are_written_into_the_descriptions() -> None:
    """Cerebras strips `maxItems`, so a bound only in the keyword is not sent."""
    properties = profile_intake.INTAKE_SCHEMA["properties"]
    for field in ("skills", "gaps", "evidence", "unproven", "domains", "experience"):
        assert re.search(r"up to \d+", properties[field]["description"], re.I), field
