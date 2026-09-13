"""Hard blockers read from JD prose — every positive case is a real board sentence.

A false blocker silently hides a job from the LLM shortlist, while a missed one
costs a single call, so the negatives here matter as much as the positives.
"""

from __future__ import annotations

import pytest

from app.blockers import find_blockers


def keys(text: str) -> list[str]:
    return [b.split(":")[0] for b in find_blockers(text)]


@pytest.mark.parametrize(
    ("sentence", "key"),
    [
        # KoBold Metals
        ("Candidates must be authorized to work in the United States.", "us_work_authorization"),
        # Orquesta.Ai
        ("***No visa sponsorship ***", "no_visa_sponsorship"),
        # ACV Auctions
        ("No immigration or work visa sponsorship provided for this position.", "no_visa_sponsorship"),
        # Halborn
        ("We are unable to sponsor or take over sponsorship of employment Visas at this time.",
         "no_visa_sponsorship"),
        # Clera
        ("Visa sponsorship is not available for this role.", "no_visa_sponsorship"),
        # Vannevar Labs — split at "U.S." hid this in the first cut
        ("• U.S. Person status is required as this position will require the ability to access "
         "U.S. only data systems.", "us_citizenship"),
        # Sphinx Defense
        ("Applicants must hold an active security clearance.", "security_clearance"),
        # Kodify Media Group
        ("From wherever you want, the position is fully remote in the EU.", "remote_region_only"),
        # Canonical
        ("Location: This is a remote role based in the EMEA region.", "remote_region_only"),
        # Strike
        ("This position is available to candidates located in European and U.S. time zones.",
         "candidate_region_only"),
        # Wikimedia Foundation
        ("We are currently only accepting applicants based within the United States.",
         "candidate_region_only"),
        # EWOR GmbH
        ("You are based in Europe or the Americas (or open to relocate/remote arrangements).",
         "candidate_region_only"),
    ],
)
def test_real_blocker_sentences_are_caught(sentence: str, key: str) -> None:
    assert key in keys(sentence)


@pytest.mark.parametrize(
    "sentence",
    [
        # A region sentence that also names India is open, not exclusive.
        "Open to candidates based in Europe or India.",
        "This is a remote role based in the EMEA or APAC region.",
        "Fully remote in the US, Europe and anywhere else with 4h overlap.",
        # Pay notes are not location rules (MariaDB).
        "Salaries for candidates outside the U.S. will vary based on local compensation.",
        # Offering sponsorship is the opposite of a blocker (Clera).
        "Salary range: $185,000 – $240,000 USD annually. Visa sponsorship is available.",
        # A US office mentioned in passing.
        "We have offices in San Francisco and Bangalore.",
        "",
    ],
)
def test_open_or_irrelevant_sentences_are_not_blockers(sentence: str) -> None:
    assert find_blockers(sentence) == []


def test_each_blocker_names_its_evidence_once() -> None:
    text = (
        "No visa sponsorship.\nWe do not offer visa sponsorship.\n"
        "Must be authorized to work in the US."
    )
    found = find_blockers(text)
    assert [f.split(":")[0] for f in found] == ["us_work_authorization", "no_visa_sponsorship"]
    assert all(":" in f and f.split(":", 1)[1] for f in found)
