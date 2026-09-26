"""The profile routes through HTTP — setup, upload, edit and save.

The behaviour worth asserting at this level is the *absence* of a 500 when there
is no profile yet. `load_profile` raises by design, and the dashboard has to tell
"not set up" apart from "the server is broken" in order to show the setup dialog
instead of an error screen. A 500 here would make a first run look like a crash.

The upload route is exercised with `parse=false`, which stores the files without
an LLM call. The drafting call itself is covered in `test_profile_intake.py`,
where it needs no HTTP.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import yaml

from app.config import get_settings
from app.main import app
from app.profile import get_profile
from tests.test_profile_intake import MINIMAL_TEX


@pytest.fixture
def isolated_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the app at a throwaway profile and résumé directory."""
    monkeypatch.setenv("PROFILE_FILE", str(tmp_path / "profile.yaml"))
    monkeypatch.setattr("app.resume_tex.DATA_DIR", tmp_path / "data")
    monkeypatch.setattr("app.resume_tex.UPLOADED_TEMPLATE", tmp_path / "data" / "resume.tex")
    monkeypatch.setattr("app.resume_tex.LEGACY_TEMPLATE", tmp_path / "data" / "legacy.tex")
    monkeypatch.setattr("app.resume_tex.BACKEND_DIR", tmp_path)
    get_settings.cache_clear()
    get_profile.cache_clear()
    yield tmp_path
    get_settings.cache_clear()
    get_profile.cache_clear()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


VALID = {
    "identity": {"full_name": "Priya Raman", "email": "priya@example.com", "country": "IN"},
    "seniority": {"total_years": 5, "ai_years": 2, "current_title": "Senior Engineer"},
    "skills": {"backend": {"python": 3, "go": 2}},
    "gaps": {"kubernetes": 2},
    "compensation": {"min_annual_inr": 3_000_000.0},
}


# --------------------------------------------------------------------------
# A first run, with no profile
# --------------------------------------------------------------------------


async def test_a_missing_profile_is_a_state_not_a_server_error(isolated_profile) -> None:
    async with _client() as client:
        response = await client.get("/profile")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["configured"] is False
    assert body["error"]
    assert body["version"] == ""


async def test_the_editor_can_read_an_absent_profile(isolated_profile) -> None:
    async with _client() as client:
        response = await client.get("/profile/document")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["configured"] is False
    assert body["data"] == {}
    # Names where the file will be written, so the dialog can say so.
    assert body["path"].endswith("profile.yaml")


async def test_a_missing_template_is_reported_separately_from_the_profile(
    isolated_profile,
) -> None:
    """A profile can be complete while the .tex is absent; they are fixed apart."""
    async with _client() as client:
        saved = await client.put("/profile", json={"data": VALID})
        assert saved.status_code == 200, saved.text
        assert saved.json()["configured"] is True
        assert saved.json()["has_template"] is False


# --------------------------------------------------------------------------
# Saving
# --------------------------------------------------------------------------


async def test_saving_writes_the_file_and_returns_a_version(isolated_profile) -> None:
    async with _client() as client:
        saved = await client.put("/profile", json={"data": VALID})
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["configured"] is True
    assert len(body["version"]) == 16
    assert body["full_name"] == "Priya Raman"

    on_disk = yaml.safe_load((isolated_profile / "profile.yaml").read_text(encoding="utf-8"))
    assert on_disk["identity"]["full_name"] == "Priya Raman"


async def test_editing_the_profile_changes_its_version(isolated_profile) -> None:
    """The key every score and document is stored under, so it has to move."""
    async with _client() as client:
        first = (await client.put("/profile", json={"data": VALID})).json()["version"]
        changed = dict(VALID, skills={"backend": {"python": 3, "go": 2, "rust": 3}})
        second = (await client.put("/profile", json={"data": changed})).json()["version"]
    assert first != second


async def test_editing_a_field_no_score_depends_on_keeps_the_version(
    isolated_profile,
) -> None:
    """Re-scoring 900 rows because a phone number changed is pure waste."""
    async with _client() as client:
        first = (await client.put("/profile", json={"data": VALID})).json()["version"]
        same = dict(VALID, identity=dict(VALID["identity"], phone="+91-99999-99999"))
        second = (await client.put("/profile", json={"data": same})).json()["version"]
    assert first == second


async def test_a_profile_with_no_skills_is_refused_with_a_reason(isolated_profile) -> None:
    async with _client() as client:
        response = await client.put("/profile", json={"data": dict(VALID, skills={})})
    assert response.status_code == 422
    assert "skills" in response.json()["detail"]


async def test_a_refused_save_does_not_create_the_file(isolated_profile) -> None:
    async with _client() as client:
        await client.put("/profile", json={"data": dict(VALID, skills={})})
    assert not (isolated_profile / "profile.yaml").exists()


async def test_a_save_takes_effect_immediately(isolated_profile) -> None:
    """`get_profile` is lru_cached, so a save that forgets to clear it is invisible."""
    async with _client() as client:
        await client.put("/profile", json={"data": VALID})
        read_back = await client.get("/profile")
    assert read_back.json()["full_name"] == "Priya Raman"

    async with _client() as client:
        await client.put("/profile", json={"data": dict(VALID, identity={"full_name": "A N Other", "country": "IN"})})
        again = await client.get("/profile")
    assert again.json()["full_name"] == "A N Other"


async def test_the_editor_round_trips_what_it_reads(isolated_profile) -> None:
    payload = dict(VALID, my_own_notes=["keep me"])
    async with _client() as client:
        await client.put("/profile", json={"data": payload})
        document = (await client.get("/profile/document")).json()
    assert document["data"]["my_own_notes"] == ["keep me"]
    assert document["configured"] is True


# --------------------------------------------------------------------------
# Upload
# --------------------------------------------------------------------------


async def test_uploading_a_tex_stores_it_where_tailoring_looks(isolated_profile) -> None:
    async with _client() as client:
        response = await client.post(
            "/profile/upload",
            params={"parse": "false"},
            files={"tex": ("resume.tex", MINIMAL_TEX.encode("utf-8"), "text/x-tex")},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["stored"]["tex"].endswith("resume.tex")
    assert (isolated_profile / "data" / "resume.tex").is_file()
    # The de-TeXed text comes back so the dialog can show what was read.
    assert "payments infrastructure" in body["resume_text"]


async def test_an_upload_wires_up_the_resume_paths_for_saving(isolated_profile) -> None:
    async with _client() as client:
        body = (
            await client.post(
                "/profile/upload",
                params={"parse": "false"},
                files={
                    "tex": ("r.tex", MINIMAL_TEX.encode("utf-8"), "text/x-tex"),
                    "resume": ("Priya CV.pdf", b"%PDF-1.4 ...", "application/pdf"),
                },
            )
        ).json()
    files = body["data"]["resume_files"]
    assert files["tex"].endswith("resume.tex")
    assert files["base"].endswith("Priya_CV.pdf")
    assert (isolated_profile / "data" / "Priya_CV.pdf").is_file()


async def test_the_template_becomes_available_after_an_upload(isolated_profile) -> None:
    async with _client() as client:
        await client.post(
            "/profile/upload",
            params={"parse": "false"},
            files={"tex": ("r.tex", MINIMAL_TEX.encode("utf-8"), "text/x-tex")},
        )
        await client.put("/profile", json={"data": VALID})
        profile = (await client.get("/profile")).json()
    assert profile["has_template"] is True


async def test_an_empty_tex_is_refused(isolated_profile) -> None:
    async with _client() as client:
        response = await client.post(
            "/profile/upload", params={"parse": "false"}, files={"tex": ("r.tex", b"", "text/x-tex")}
        )
    assert response.status_code == 422


async def test_a_pdf_in_the_tex_slot_says_so(isolated_profile) -> None:
    """The likeliest mistake at this dialog, and a decode traceback explains nothing."""
    async with _client() as client:
        response = await client.post(
            "/profile/upload",
            params={"parse": "false"},
            files={"tex": ("r.tex", b"%PDF-1.4\x00\xff\xfe binary", "application/pdf")},
        )
    assert response.status_code == 422
    assert ".tex" in response.json()["detail"]


async def test_a_tex_that_will_not_compile_warns_but_is_still_stored(
    isolated_profile,
) -> None:
    """Storing it is what lets the user fix it in the editor afterwards."""
    async with _client() as client:
        response = await client.post(
            "/profile/upload",
            params={"parse": "false"},
            files={"tex": ("r.tex", b"just some prose, no latex at all", "text/x-tex")},
        )
    assert response.status_code == 200, response.text
    assert any("documentclass" in w for w in response.json()["warnings"])
    assert (isolated_profile / "data" / "resume.tex").is_file()


async def test_an_oversized_upload_is_refused_before_it_is_parsed(isolated_profile) -> None:
    from app.api import MAX_UPLOAD_BYTES

    async with _client() as client:
        response = await client.post(
            "/profile/upload",
            params={"parse": "false"},
            files={"tex": ("r.tex", b"x" * (MAX_UPLOAD_BYTES + 1), "text/x-tex")},
        )
    assert response.status_code == 413


async def test_uploading_without_a_tex_is_a_validation_error(isolated_profile) -> None:
    async with _client() as client:
        response = await client.post("/profile/upload", params={"parse": "false"})
    assert response.status_code == 422


async def test_an_upload_preserves_a_previously_saved_pay_floor(isolated_profile) -> None:
    """Nothing on a résumé states it, so a re-upload must not reset the gate."""
    async with _client() as client:
        await client.put("/profile", json={"data": VALID})
        body = (
            await client.post(
                "/profile/upload",
                params={"parse": "false"},
                files={"tex": ("r.tex", MINIMAL_TEX.encode("utf-8"), "text/x-tex")},
            )
        ).json()
    assert body["data"]["compensation"]["min_annual_inr"] == 3_000_000.0
