"""Compile a LaTeX résumé to PDF with Tectonic.

Why Tectonic and not pdflatex
-----------------------------
There is no TeX distribution on this machine, and asking for one is a ~500MB
install that then has to be repeated in the Docker image. Tectonic is a single
binary that downloads only the support files a document actually needs and
caches them. Measured here on 2026-09-20 against the real résumé:

    cold (fetching the bundle)   ~5 min, once, ever
    warm                         ~3.5 s

3.5s is why the editor has a *Recompile* button and does not rebuild on every
keystroke. It is fast enough to feel like a preview, not fast enough to be one.

Fidelity was verified, not assumed: a Tectonic render of
`data/Ashish_AI_FullStack_v2.tex` was compared page-by-page against
`data/Ashish_AI_FullStack_v2.pdf` (built on Overleaf) until they matched — same
single page, same line breaks, same link colours. The two class tweaks that
took to get there are documented in `data/resume.cls`.

On `--untrusted`
----------------
The .tex compiled here is hand-editable in the browser, so it is input, not
code we wrote. `--untrusted` turns off shell-escape and the other known-unsafe
features. It is a personal tool and the only author is the user, but a résumé
editor that can read arbitrary files off the disk into a PDF is a bad default
to leave lying around.

On running it in a thread
-------------------------
`subprocess.run` inside `asyncio.to_thread`, rather than
`asyncio.create_subprocess_exec`. Subprocess support on Windows needs the
Proactor event loop, and a résumé preview is not worth a dependency on which
loop policy the server happens to be started with. A 3.5s blocking call on a
worker thread costs nothing here.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import asyncio

log = logging.getLogger(__name__)

# `backend/` — this file is `backend/app/latex.py`.
BACKEND_DIR = Path(__file__).resolve().parent.parent
DEFAULT_TOOLS_DIR = BACKEND_DIR / ".tools"
DEFAULT_RESOURCES_DIR = BACKEND_DIR / "data"

# The résumé is one page and compiles in seconds; anything past this is a
# runaway macro, not a slow document.
COMPILE_TIMEOUT_SECONDS = 120.0


class LatexUnavailable(RuntimeError):
    """No Tectonic binary could be found. The message says how to get one."""


@dataclass(slots=True)
class CompileResult:
    """What one compile produced. `ok` and `pdf` always agree."""

    ok: bool
    pdf: bytes | None
    log: str
    errors: list[str] = field(default_factory=list)
    duration_s: float = 0.0


def resolve_tectonic(explicit: str | None = None) -> Path:
    """Find the Tectonic binary, most specific source first.

    Order: an explicit setting, `TECTONIC_PATH`, the vendored copy under
    `backend/.tools/`, then whatever is on PATH. The vendored copy is what the
    setup script installs and is gitignored — it is a 50MB binary, not source.
    """
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    if env := os.environ.get("TECTONIC_PATH"):
        candidates.append(Path(env))
    name = "tectonic.exe" if os.name == "nt" else "tectonic"
    candidates.append(DEFAULT_TOOLS_DIR / name)

    for candidate in candidates:
        if candidate.is_file():
            return candidate
    if found := shutil.which("tectonic"):
        return Path(found)

    raise LatexUnavailable(
        "tectonic was not found. Install it with "
        "`python -m scripts.install_tectonic`, or set TECTONIC_PATH to an "
        "existing binary."
    )


# Tectonic reports engine errors as `error: <file>:<line>: <message>` and as
# TeX's own `! <message>`. Both are worth showing above the editor; the rest of
# the log is noise until someone asks for it.
_ERROR_LINE = re.compile(r"^(?:error:|! ).*", re.MULTILINE)
# The "something bad happened inside XeTeX" wrapper repeats what follows it.
_NOISE = ("error: something bad happened inside", "caused by:", "error: the XeTeX engine")


def extract_errors(output: str, limit: int = 12) -> list[str]:
    """Pull the human-readable failure lines out of a compile log."""
    seen: list[str] = []
    for match in _ERROR_LINE.findall(output):
        line = match.strip()
        if any(line.startswith(prefix) for prefix in _NOISE):
            continue
        if line not in seen:
            seen.append(line)
        if len(seen) >= limit:
            break
    return seen


def _run(
    binary: Path, tex: str, resources: Path, timeout: float
) -> tuple[bytes | None, str, float]:
    """Compile in a scratch directory. Blocking — call it on a thread."""
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="resume-tex-") as tmp:
        workdir = Path(tmp)
        source = workdir / "resume.tex"
        source.write_text(tex, encoding="utf-8")

        # `resume.cls` and anything else the document \input-s has to sit
        # beside it: Tectonic resolves local files relative to the input, and
        # the bundle has no `resume.cls` in it.
        if resources.is_dir():
            for extra in (*resources.glob("*.cls"), *resources.glob("*.sty")):
                shutil.copy2(extra, workdir / extra.name)

        try:
            proc = subprocess.run(
                [
                    str(binary),
                    "--untrusted",
                    "--chatter",
                    "minimal",
                    "--color",
                    "never",
                    "--keep-logs",
                    "--outdir",
                    str(workdir),
                    str(source),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                cwd=workdir,
            )
            output = f"{proc.stdout}\n{proc.stderr}".strip()
        except subprocess.TimeoutExpired:
            return None, f"timed out after {timeout:.0f}s", time.monotonic() - started

        # TeX's own log names the offending line far better than the CLI does.
        texlog = workdir / "resume.log"
        if texlog.is_file():
            output = f"{output}\n\n--- resume.log ---\n{texlog.read_text(encoding='utf-8', errors='replace')}"

        pdf_path = workdir / "resume.pdf"
        pdf = pdf_path.read_bytes() if pdf_path.is_file() else None
        return pdf, output, time.monotonic() - started


async def compile_tex(
    tex: str,
    *,
    binary: Path | None = None,
    resources: Path | None = None,
    timeout: float = COMPILE_TIMEOUT_SECONDS,
) -> CompileResult:
    """Compile `tex` and return the PDF bytes, or the errors that stopped it.

    A failed compile is a normal outcome, not an exception: the editor is
    expected to be mid-edit and broken half the time. `LatexUnavailable` *is*
    raised, because that one is a setup problem the user has to act on.
    """
    binary = binary or resolve_tectonic()
    resources = resources or DEFAULT_RESOURCES_DIR

    pdf, output, duration = await asyncio.to_thread(_run, binary, tex, resources, timeout)
    errors = extract_errors(output)
    if pdf is None and not errors:
        errors = ["compile produced no PDF and no error — see the full log"]

    log.info(
        "latex.compile",
        extra={"ok": pdf is not None, "duration_s": round(duration, 2), "errors": len(errors)},
    )
    return CompileResult(
        ok=pdf is not None,
        pdf=pdf,
        log=output,
        errors=[] if pdf is not None else errors,
        duration_s=duration,
    )
