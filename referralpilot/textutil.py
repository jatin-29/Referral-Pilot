"""Text helpers shared by the harvester, tailor and outreach modules."""

from __future__ import annotations

import re
import unicodedata
from html import unescape

from bs4 import BeautifulSoup, NavigableString

_BLOCK_TAGS = (
    "p", "div", "section", "article", "header", "footer", "ul", "ol", "table", "tr",
    "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre",
)


def html_to_text(html: str | None) -> str:
    """Convert job-description HTML into plain text that keeps headings and bullets."""
    if not html:
        return ""
    if "<" not in html and "&lt;" in html:
        html = unescape(html)  # Greenhouse returns entity-escaped HTML
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "iframe"]):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with(NavigableString("\n"))
    for li in soup.find_all("li"):
        li.insert_before(NavigableString("\n- "))
        li.insert_after(NavigableString("\n"))
    for block in soup.find_all(_BLOCK_TAGS):
        block.insert_before(NavigableString("\n"))
        block.insert_after(NavigableString("\n"))
    return normalize_text(soup.get_text())


def normalize_text(text: str) -> str:
    text = text.replace("\xa0", " ").replace("\r", "")
    lines = [re.sub(r"[ \t\f\v]+", " ", line).strip() for line in text.split("\n")]
    out: list[str] = []
    for line in lines:
        if line in {"-", "•"}:
            continue
        if not line and (not out or not out[-1]):
            continue
        if line.startswith("- ") and len(out) >= 2 and not out[-1] and out[-2].startswith("- "):
            out.pop()  # keep consecutive bullets together
        out.append(line)
    return "\n".join(out).strip()


def slugify(value: str, max_length: int = 60) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return value[:max_length].rstrip("-") or "item"


def ascii_fold(value: str) -> str:
    return unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")


def truncate(value: str, length: int) -> str:
    return value if len(value) <= length else value[: max(0, length - 1)].rstrip() + "…"


_SENTENCE_BREAK = re.compile(r"(?<=[.!?])[\"')\]]*\s+(?=[A-Z0-9\"'(])")


def split_sentences(text: str) -> list[str]:
    """Sentence split that ignores dots inside tokens such as "B.Tech" or "Node.js"."""
    return [part.strip() for part in _SENTENCE_BREAK.split(text.strip()) if part.strip()]


def human_join(items: list[str], conjunction: str = "and") -> str:
    items = [item for item in items if item]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} {conjunction} {items[-1]}"


# --- years-of-experience extraction ------------------------------------------

_WORD_NUMBERS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}
_NUM = r"(\d{1,2}|zero|one|two|three|four|five|six|seven|eight|nine|ten)"
_YEARS_RE = re.compile(
    rf"\b{_NUM}\s*(?:\(\s*\d{{1,2}}\s*\)\s*)?(?:\+|plus)?\s*"
    rf"(?:(?:-|–|—|to)\s*{_NUM}\s*\+?\s*)?(?:or\s+more\s+)?(?:years?|yrs?)\b",
    re.IGNORECASE,
)
_EXPERIENCE_CONTEXT = re.compile(
    r"experien|\bexp\b|industry|professional|hands[- ]on|track record|background|"
    r"working (?:with|on|in)|work(?:ed)? (?:with|on|in)|in (?:a|an) (?:similar|relevant)",
    re.IGNORECASE,
)
_UPPER_BOUND_CUE = re.compile(r"(?:up\s*to|upto|maximum(?:\s+of)?|max\.?|less\s+than|under|<)\s*$", re.IGNORECASE)


def _to_int(token: str) -> int:
    token = token.lower()
    return _WORD_NUMBERS[token] if token in _WORD_NUMBERS else int(token)


def required_years(text: str) -> int | None:
    """Largest lower bound of any "N+ years of experience"-style requirement.

    Returns None when the text never states an experience requirement.
    """
    if not text:
        return None
    best: int | None = None
    for match in _YEARS_RE.finditer(text):
        before = text[max(0, match.start() - 40): match.start()]
        after = text[match.end(): match.end() + 70]
        if _UPPER_BOUND_CUE.search(before):
            continue
        if not (_EXPERIENCE_CONTEXT.search(after) or _EXPERIENCE_CONTEXT.search(before[-25:])):
            continue
        low = _to_int(match.group(1))
        if low > 25:  # "2024 years" style noise
            continue
        best = low if best is None else max(best, low)
    return best
