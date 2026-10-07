"""Renderers for a ResumeDocument: LaTeX (Jinja2), Markdown and fpdf2 (pure-Python PDF)."""

from __future__ import annotations

import unicodedata
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from .document import ResumeDocument, RichText

# --- LaTeX -------------------------------------------------------------------------

_TEX_SPECIAL = {
    "\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_",
    "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
    "<": r"\textless{}", ">": r"\textgreater{}", "|": r"\textbar{}",
}
_UNICODE_TO_TEX = {
    "–": "--", "—": "---", "‘": "`", "’": "'", "“": "``", "”": "''", "…": r"\ldots{}",
    "•": r"\textbullet{}", "→": r"$\rightarrow$", "←": r"$\leftarrow$", "×": r"$\times$",
    "≈": r"$\approx$", "≤": r"$\leq$", "≥": r"$\geq$", "₹": "Rs.~", "€": "EUR~", "\xa0": "~",
}


def tex_escape(value: object) -> str:
    out: list[str] = []
    for ch in str(value):
        if ch in _TEX_SPECIAL:
            out.append(_TEX_SPECIAL[ch])
        elif ch in _UNICODE_TO_TEX:
            out.append(_UNICODE_TO_TEX[ch])
        elif ord(ch) < 256:
            out.append(ch)  # ASCII + Latin-1 work with inputenc/T1 under pdflatex
        else:
            folded = unicodedata.normalize("NFKD", ch).encode("ascii", "ignore").decode("ascii")
            out.append("".join(_TEX_SPECIAL.get(c, c) for c in folded))
    return "".join(out)


def tex_url(url: object) -> str:
    return (
        str(url).replace("\\", "/").replace(" ", "%20").replace("%", r"\%")
        .replace("#", r"\#").replace("{", "%7B").replace("}", "%7D")
    )


def tex_rich(spans: RichText) -> str:
    return "".join(rf"\textbf{{{tex_escape(s.text)}}}" if s.bold else tex_escape(s.text) for s in spans)


def tex_items(items: list[tuple[str, bool]]) -> str:
    return ", ".join(rf"\textbf{{{tex_escape(t)}}}" if bold else tex_escape(t) for t, bold in items)


def latex_environment(templates_dir: Path) -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        block_start_string=r"\BLOCK{",
        block_end_string="}",
        variable_start_string=r"\VAR{",
        variable_end_string="}",
        comment_start_string=r"\#{",
        comment_end_string="}",
        line_statement_prefix="%%",
        line_comment_prefix="%#",
        trim_blocks=True,
        lstrip_blocks=True,
        autoescape=False,
        keep_trailing_newline=True,
        undefined=StrictUndefined,
    )
    env.filters.update(tex=tex_escape, url=tex_url, rich=tex_rich, items=tex_items)
    return env


def render_latex(doc: ResumeDocument, templates_dir: Path, template: str = "base_resume.tex") -> str:
    rendered = latex_environment(templates_dir).get_template(template).render(doc=doc, paper="a4paper")
    return rendered.lstrip()  # template-only comment lines leave blank lines behind


# --- Markdown ----------------------------------------------------------------------

def _md(text: str) -> str:
    return text.replace("*", r"\*").replace("_", r"\_")


def md_rich(spans: RichText) -> str:
    return "".join(f"**{_md(s.text)}**" if s.bold else _md(s.text) for s in spans)


def md_items(items: list[tuple[str, bool]]) -> str:
    return ", ".join(f"**{_md(t)}**" if bold else _md(t) for t, bold in items)


def render_markdown(doc: ResumeDocument) -> str:
    lines = [f"# {doc.name}", "", " | ".join(text for text, _ in doc.contact), ""]
    for section in doc.sections:
        if section == "summary":
            lines += ["## Summary", "", md_rich(doc.summary), ""]
        elif section == "education":
            lines += ["## Education", ""]
            for edu in doc.education:
                details = ", ".join(x for x in (edu["degree"], edu["score"], edu["location"], edu["dates"]) if x)
                lines.append(f"**{_md(edu['institution'])}** - {_md(details)}")
                if edu["coursework"]:
                    lines.append(f"Relevant coursework: {md_items(edu['coursework'])}")
                lines.append("")
        elif section == "skills":
            lines += ["## Technical Skills", ""]
            lines += [f"- **{_md(category)}:** {md_items(items)}" for category, items in doc.skills]
            lines.append("")
        elif section == "experience":
            lines += ["## Experience", ""]
            for job in doc.experience:
                meta = ", ".join(x for x in (job["location"], job["dates"]) if x)
                lines.append(f"**{_md(job['role'])}**, {_md(job['company'])} ({_md(meta)})")
                lines += [f"- {md_rich(b)}" for b in job["bullets"]]
                lines.append("")
        elif section == "projects":
            lines += ["## Projects", ""]
            for project in doc.projects:
                link = f" - {project['link']}" if project["link"] else ""
                lines.append(f"**{_md(project['name'])}** | {md_items(project['tech'])}{link}")
                lines += [f"- {md_rich(b)}" for b in project["bullets"]]
                lines.append("")
        elif section == "achievements":
            lines += ["## Achievements", ""]
            lines += [f"- {md_rich(a)}" for a in doc.achievements]
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# --- fpdf2 (pure-Python fallback) ------------------------------------------------

_FONT_CANDIDATES = [
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/dejavu/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/TTF/DejaVuSans.ttf", "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"),
    ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
    ("/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
    ("/Library/Fonts/Arial.ttf", "/Library/Fonts/Arial Bold.ttf"),
]
_LATIN1_FALLBACK = {
    "–": "-", "—": "-", "‘": "'", "’": "'", "“": '"', "”": '"', "…": "...", "•": "-",
    "→": "->", "←": "<-", "×": "x", "≈": "~", "≤": "<=", "≥": ">=", "₹": "Rs.", "€": "EUR",
}


def _latin1(text: str) -> str:
    text = "".join(_LATIN1_FALLBACK.get(ch, ch) for ch in text)
    return text.encode("latin-1", "replace").decode("latin-1")


def render_fpdf(doc: ResumeDocument, pdf_path: Path) -> None:
    from fpdf import FPDF

    pdf = FPDF(format="A4", unit="mm")
    pdf.set_margins(14, 12, 14)
    pdf.set_auto_page_break(True, margin=12)
    pdf.set_title(f"{doc.name} - Resume")
    pdf.set_author(doc.name)
    if doc.keywords:
        pdf.set_keywords(", ".join(doc.keywords))

    family, clean = "Helvetica", _latin1
    for regular, bold in _FONT_CANDIDATES:
        if Path(regular).exists() and Path(bold).exists():
            pdf.add_font("Body", "", regular)
            pdf.add_font("Body", "B", bold)
            family, clean = "Body", (lambda s: s)
            break

    def esc(text: str) -> str:
        # fpdf2's markdown mode treats ** / __ / -- as markers.
        return clean(text).replace("**", "* *").replace("__", "_ _").replace("--", "-")

    def rich(spans: RichText) -> str:
        return "".join(f"**{esc(s.text)}**" if s.bold else esc(s.text) for s in spans)

    def items(values: list[tuple[str, bool]]) -> str:
        return ", ".join(f"**{esc(t)}**" if b else esc(t) for t, b in values)

    def para(text: str, size: float = 9.5, indent: float = 0.0) -> None:
        pdf.set_font(family, "", size)
        pdf.set_x(pdf.l_margin + indent)
        pdf.multi_cell(pdf.epw - indent, 4.6, text, markdown=True, align="L", new_x="LMARGIN", new_y="NEXT")

    def bullet(text: str) -> None:
        pdf.set_font(family, "", 9.5)
        pdf.set_x(pdf.l_margin + 2)
        pdf.cell(4, 4.6, "-")
        pdf.multi_cell(pdf.epw - 6, 4.6, text, markdown=True, align="L", new_x="LMARGIN", new_y="NEXT")

    def heading(title: str) -> None:
        pdf.ln(2.5)
        pdf.set_font(family, "B", 11)
        pdf.cell(0, 6, clean(title.upper()), new_x="LMARGIN", new_y="NEXT")
        y = pdf.get_y()
        pdf.line(pdf.l_margin, y, pdf.l_margin + pdf.epw, y)
        pdf.ln(1.2)

    def row(left: str, right: str, bold_left: bool = True) -> None:
        pdf.set_font(family, "B" if bold_left else "", 10)
        width = pdf.get_string_width(clean(right)) + 2 if right else 0
        pdf.cell(pdf.epw - width, 5, clean(left))
        pdf.set_font(family, "", 9.5)
        pdf.cell(width, 5, clean(right), align="R", new_x="LMARGIN", new_y="NEXT")

    pdf.add_page()
    pdf.set_font(family, "B", 18)
    pdf.cell(0, 9, clean(doc.name), align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font(family, "", 9)
    pdf.multi_cell(0, 4.5, clean("  |  ".join(t for t, _ in doc.contact)), align="C", new_x="LMARGIN", new_y="NEXT")

    for section in doc.sections:
        if section == "summary":
            heading("Summary")
            para(rich(doc.summary))
        elif section == "education":
            heading("Education")
            for edu in doc.education:
                row(edu["institution"], edu["dates"])
                para(esc(", ".join(x for x in (edu["degree"], edu["score"], edu["location"]) if x)))
                if edu["coursework"]:
                    para(f"Coursework: {items(edu['coursework'])}")
        elif section == "skills":
            heading("Technical Skills")
            for category, values in doc.skills:
                para(f"**{esc(category)}:** {items(values)}")
        elif section == "experience":
            heading("Experience")
            for job in doc.experience:
                row(f"{job['role']}, {job['company']}", job["dates"])
                for b in job["bullets"]:
                    bullet(rich(b))
        elif section == "projects":
            heading("Projects")
            for project in doc.projects:
                row(project["name"], project["link_label"])
                para(f"Tech: {items(project['tech'])}", size=9)
                for b in project["bullets"]:
                    bullet(rich(b))
        elif section == "achievements":
            heading("Achievements")
            for a in doc.achievements:
                bullet(rich(a))
    pdf.output(str(pdf_path))
