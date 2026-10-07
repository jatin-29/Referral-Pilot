"""Module B: JD parsing, ATS match scoring and LaTeX resume tailoring."""

from .compiler import CompileResult, available_engines, compile_resume
from .document import ResumeDocument, build_resume
from .jd_parser import ParsedJD, parse_job_description
from .matcher import MatchResult, score_match
from .service import TailorError, TailorOutcome, analyze_job, tailor_job

__all__ = [
    "CompileResult",
    "MatchResult",
    "ParsedJD",
    "ResumeDocument",
    "TailorError",
    "TailorOutcome",
    "analyze_job",
    "available_engines",
    "build_resume",
    "compile_resume",
    "parse_job_description",
    "score_match",
    "tailor_job",
]
