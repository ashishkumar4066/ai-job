"""Lever application forms (`jobs.lever.co/{company}/{id}/apply`).

Verified against a live form (tryjeeves, 2026-09-19):

- Standard fields are plain inputs keyed by `name`: `name`, `email`, `phone`,
  `location`, `org`, `urls[LinkedIn]`, `urls[GitHub]`, ...
- The résumé is `#resume-upload-input`. Lever PARSES it and overwrites the
  name/email/phone boxes with what it read, so the upload goes first and the
  profile values are written after it.
- `location` is an autocomplete. Typed text alone leaves the hidden
  `selectedLocation` empty, so a suggestion from `.dropdown-location` must be
  clicked.
- Custom questions are `li.application-question.custom-question`, each with a
  `.application-label .text` and radio / checkbox / textarea inputs whose
  `value` is the option text.
- Submit is guarded by an invisible hCaptcha. We never reach it: the human
  clicks submit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from playwright.async_api import Locator, Page

from app.profile import Profile

_DASHES = re.compile(r"[‐-―−]")


def norm(text: str) -> str:
    """Compare question and option text loosely: dashes, case, whitespace, ✱."""
    text = _DASHES.sub("-", text.replace("✱", ""))
    return re.sub(r"\s+", " ", text).strip().lower()


@dataclass
class FillReport:
    filled: dict[str, Any] = field(default_factory=dict)
    needs_you: list[str] = field(default_factory=list)  # required, left empty
    drafts: list[str] = field(default_factory=list)  # filled, but read them
    unmatched: list[str] = field(default_factory=list)  # answer given, option not found


def _lookup(answers: dict[str, Any], question: str) -> tuple[bool, Any]:
    """Answers are keyed by a prefix of the question text."""
    q = norm(question)
    for key, value in answers.items():
        if q.startswith(norm(key)):
            return True, value
    return False, None


async def _highlight(loc: Locator, colour: str) -> None:
    await loc.evaluate(
        "(el, c) => { el.style.outline = `3px solid ${c}`; el.style.outlineOffset = '6px'; }",
        colour,
    )


class LeverFiller:
    ats = "lever"

    def __init__(self, page: Page, profile: Profile, resume: Path | None) -> None:
        self.page = page
        self.profile = profile
        self.resume = resume
        self.report = FillReport()

    async def fill(self, answers: dict[str, Any], drafts: set[str]) -> FillReport:
        await self.page.wait_for_selector("input[name=name]", timeout=30_000)
        await self._resume()
        await self._standard()
        await self._location()
        await self._custom(answers, {norm(d) for d in drafts})
        return self.report

    async def _resume(self) -> None:
        if not self.resume:
            self.report.needs_you.append("Resume/CV (no file in profile.resume_files)")
            return
        await self.page.set_input_files("#resume-upload-input", str(self.resume))
        try:
            await self.page.wait_for_selector(".resume-upload-success", state="visible", timeout=30_000)
            self.report.filled["resume"] = self.resume.name
        except Exception:  # noqa: BLE001 - surfaced to the human, not fatal
            self.report.needs_you.append("Resume/CV (upload not confirmed — check it)")

    async def _standard(self) -> None:
        ident = self.profile.identity
        current = next((e for e in self.profile.experience if not e.get("end")), {})
        values = {
            "name": ident.full_name,
            "email": ident.email,
            "phone": ident.phone,
            "org": current.get("company", ""),
            "urls[LinkedIn]": ident.links.get("linkedin", ""),
            "urls[GitHub]": ident.links.get("github", ""),
            "urls[Portfolio]": ident.links.get("portfolio", ""),
        }
        for name, value in values.items():
            box = self.page.locator(f'input[name="{name}"]')
            if not value or await box.count() == 0:
                continue
            await box.first.fill(value)
            self.report.filled[name] = value

    async def _location(self) -> None:
        text = self.profile.identity.location
        box = self.page.locator("#location-input")
        if not text or await box.count() == 0:
            return
        await box.fill("")
        await box.press_sequentially(text, delay=60)
        option = self.page.locator(".dropdown-results .dropdown-location").first
        try:
            await option.wait_for(state="visible", timeout=6_000)
            picked = (await option.inner_text()).strip()
            await option.click()
            # The top suggestion is a guess: "Bihar, India" picks the town of
            # Bihār in Nālanda district, not the state.
            self.report.filled["location"] = picked
            self.report.drafts.append(f"Current location (picked '{picked}')")
            await _highlight(box, "#f59e0b")
        except Exception:  # noqa: BLE001
            self.report.needs_you.append(f"Current location (typed '{text}' — pick a suggestion)")
            await _highlight(box, "#e11d48")

    async def _custom(self, answers: dict[str, Any], drafts: set[str]) -> None:
        questions = self.page.locator("li.application-question.custom-question")
        for i in range(await questions.count()):
            q = questions.nth(i)
            label = norm(await q.locator(".application-label .text").first.inner_text())
            required = await q.locator(".required").count() > 0
            found, value = _lookup(answers, label)
            short = label[:90]

            if not found or value in (None, "", []):
                if required:
                    self.report.needs_you.append(short)
                    await _highlight(q, "#e11d48")
                continue

            ok = await self._answer(q, value)
            if not ok:
                self.report.unmatched.append(f"{short} -> {value!r}")
                await _highlight(q, "#e11d48")
                continue
            self.report.filled[short] = value
            if any(label.startswith(d) for d in drafts):
                self.report.drafts.append(short)
                await _highlight(q, "#f59e0b")

    async def _answer(self, q: Locator, value: Any) -> bool:
        textarea = q.locator("textarea, input[type=text]")
        if await textarea.count():
            await textarea.first.fill(str(value).strip())
            return True

        wanted = [norm(v) for v in (value if isinstance(value, list) else [value])]
        options = q.locator("input[type=radio], input[type=checkbox]")
        hit = 0
        for j in range(await options.count()):
            opt = options.nth(j)
            text = norm(await opt.get_attribute("value") or "")
            if any(text.startswith(w) for w in wanted):
                await opt.check()
                hit += 1
        return hit == len(wanted)
