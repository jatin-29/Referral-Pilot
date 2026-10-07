"""Resume compilation chain.

auto = pdflatex -> xelatex -> lualatex -> tectonic -> typst -> fpdf

The .tex (and a plain .md copy) are always written next to the PDF, so the
LaTeX source is available even when the PDF comes from a fallback engine.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .document import ResumeDocument
from .render import render_fpdf, render_latex, render_markdown

LATEX_ENGINES = ("pdflatex", "xelatex", "lualatex", "tectonic")
ENGINE_CHAIN = LATEX_ENGINES + ("typst", "fpdf")


class CompileError(RuntimeError):
    pass


@dataclass
class CompileResult:
    engine: str | None
    pdf_path: Path | None
    tex_path: Path
    md_path: Path
    duration: float = 0.0
    attempts: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.pdf_path is not None

    def describe_failures(self) -> str:
        return "; ".join(f"{engine}: {error}" for engine, error in self.attempts)


def _typst_python_available() -> bool:
    return importlib.util.find_spec("typst") is not None


def engine_available(engine: str) -> bool:
    if engine in LATEX_ENGINES:
        return shutil.which(engine) is not None
    if engine == "typst":
        return _typst_python_available() or shutil.which("typst") is not None
    return engine == "fpdf"


def available_engines() -> dict[str, bool]:
    return {engine: engine_available(engine) for engine in ENGINE_CHAIN}


def _latex_error_summary(log_text: str) -> str:
    errors = [line.strip() for line in log_text.splitlines() if line.startswith("!")]
    if errors:
        return " | ".join(errors[:3])
    tail = [line for line in log_text.splitlines() if line.strip()][-5:]
    return " | ".join(tail)[-400:]


def _run_latex(engine: str, tex_source: str, pdf_out: Path, timeout: int) -> None:
    with tempfile.TemporaryDirectory(prefix="rp-tex-") as tmp:
        workdir = Path(tmp)
        (workdir / "resume.tex").write_text(tex_source, encoding="utf-8")
        if engine == "tectonic":
            cmd = ["tectonic", "--outdir", str(workdir), "resume.tex"]
        else:
            cmd = [engine, "-interaction=nonstopmode", "-halt-on-error", "-no-shell-escape",
                   f"-output-directory={workdir}", "resume.tex"]
        env = {**os.environ, "openout_any": "p", "shell_escape": "f"}
        try:
            proc = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True, errors="replace",
                                  timeout=timeout, env=env)
        except subprocess.TimeoutExpired as exc:
            raise CompileError(f"timed out after {timeout}s") from exc
        except OSError as exc:
            raise CompileError(str(exc)) from exc
        produced = workdir / "resume.pdf"
        if proc.returncode != 0 or not produced.exists():
            log_file = workdir / "resume.log"
            log_text = log_file.read_text(errors="replace") if log_file.exists() else proc.stdout + proc.stderr
            raise CompileError(_latex_error_summary(log_text) or f"exit code {proc.returncode}")
        shutil.copyfile(produced, pdf_out)


def _run_typst(doc: ResumeDocument, templates_dir: Path, pdf_out: Path, timeout: int) -> None:
    template = templates_dir / "base_resume.typ"
    if not template.exists():
        raise CompileError(f"missing template {template}")
    with tempfile.TemporaryDirectory(prefix="rp-typst-") as tmp:
        workdir = Path(tmp)
        shutil.copyfile(template, workdir / "resume.typ")
        (workdir / "resume.json").write_text(json.dumps(doc.to_json(), ensure_ascii=False), encoding="utf-8")
        produced = workdir / "resume.pdf"
        if _typst_python_available():
            import typst  # type: ignore[import-not-found]

            try:
                typst.compile(str(workdir / "resume.typ"), output=str(produced), root=str(workdir))
            except Exception as exc:  # typst raises its own error types
                raise CompileError(str(exc).strip().splitlines()[0] if str(exc).strip() else repr(exc)) from exc
        else:
            cmd = ["typst", "compile", "--root", str(workdir), "resume.typ", str(produced)]
            try:
                proc = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True, timeout=timeout)
            except (subprocess.TimeoutExpired, OSError) as exc:
                raise CompileError(str(exc)) from exc
            if proc.returncode != 0:
                raise CompileError((proc.stderr or proc.stdout).strip()[:400])
        if not produced.exists():
            raise CompileError("typst produced no PDF")
        shutil.copyfile(produced, pdf_out)


def compile_resume(
    doc: ResumeDocument,
    out_dir: Path,
    basename: str,
    *,
    templates_dir: Path,
    engine: str = "auto",
    timeout: int = 90,
) -> CompileResult:
    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    tex_path = out_dir / f"{basename}.tex"
    md_path = out_dir / f"{basename}.md"
    pdf_path = out_dir / f"{basename}.pdf"
    tex_source = render_latex(doc, templates_dir)
    tex_path.write_text(tex_source, encoding="utf-8")
    md_path.write_text(render_markdown(doc), encoding="utf-8")

    engine = (engine or "auto").lower()
    chain = ENGINE_CHAIN if engine == "auto" else (engine,)
    attempts: list[tuple[str, str]] = []
    for name in chain:
        if name not in ENGINE_CHAIN:
            attempts.append((name, "unknown engine"))
            continue
        if not engine_available(name):
            attempts.append((name, "not installed"))
            continue
        tmp_pdf = pdf_path.with_suffix(".pdf.part")
        try:
            if name in LATEX_ENGINES:
                _run_latex(name, tex_source, tmp_pdf, timeout)
            elif name == "typst":
                _run_typst(doc, templates_dir, tmp_pdf, timeout)
            else:
                render_fpdf(doc, tmp_pdf)
        except CompileError as exc:
            attempts.append((name, str(exc)))
            tmp_pdf.unlink(missing_ok=True)
            continue
        except Exception as exc:  # fpdf / unexpected engine errors should not stop the chain
            attempts.append((name, f"{type(exc).__name__}: {exc}"))
            tmp_pdf.unlink(missing_ok=True)
            continue
        tmp_pdf.replace(pdf_path)
        return CompileResult(name, pdf_path, tex_path, md_path, time.monotonic() - started, attempts)
    return CompileResult(None, None, tex_path, md_path, time.monotonic() - started, attempts)


def resume_basename(company: str, role: str) -> str:
    from ..textutil import slugify

    return f"{slugify(company, 40)}_{slugify(role, 60)}_resume"


_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


def safe_filename(name: str) -> str:
    return _SAFE.sub("_", name)
