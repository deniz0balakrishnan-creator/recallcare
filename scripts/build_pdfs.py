"""Build the submission PDFs: Markdown → (pandoc) Typst → (typst) PDF.

    .venv/bin/python scripts/build_pdfs.py            # both documents
    .venv/bin/python scripts/build_pdfs.py proposal   # one

Placeholders like <LIVE_URL> are filled from docs/submission_values.yaml (missing keys stay visible on purpose).
Dev-only dependencies: pandoc (system) and `pip install typst pypdf`.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
OUT = DOCS / "pdf"
PANDOC = shutil.which("pandoc") or "/opt/anaconda3/bin/pandoc"

# The business proposal is limited to 3 pages. The build fails if it isn't.
PAGE_LIMITS = {"proposal": 3}

DOCS_TO_BUILD = {
    "proposal": ("business_proposal.md", "business_proposal.pdf", {"size": "9.6pt", "margin": "1.45cm"}),
    "technical": ("technical_document.md", "technical_document.pdf", {"size": "10pt", "margin": "1.8cm"}),
}

PREAMBLE = r"""
#set document(title: "{title}", author: "Team Binary Beasts (K2EZYJRZ)")
#set page(paper: "a4", margin: {margin}, numbering: "1 / 1",
  footer: context [#set text(size: 7.5pt, fill: rgb("#6f6d68")); RecallCare · Team Binary Beasts (K2EZYJRZ) · synthetic data only #h(1fr) #counter(page).display("1 / 1", both: true)])
#set text(font: ("Helvetica Neue", "Helvetica", "PingFang SC", "Tamil Sangam MN", "Arial Unicode MS"), size: {size}, lang: "en", fill: rgb("#0b0b0b"))
#set par(justify: false, leading: 0.55em, spacing: 0.75em)
#show heading.where(level: 1): it => block(above: 0.9em, below: 0.45em, text(size: 1.32em, weight: "bold", fill: rgb("#184f95"), it.body))
#show heading.where(level: 2): it => block(above: 0.85em, below: 0.4em, text(size: 1.12em, weight: "bold", fill: rgb("#184f95"), it.body))
#show heading.where(level: 3): it => block(above: 0.7em, below: 0.35em, text(size: 1.0em, weight: "bold", it.body))
#show link: it => text(fill: rgb("#184f95"), it)
#show raw: set text(font: ("Menlo", "Courier New"), size: 0.88em)
#show raw.where(block: true): it => block(fill: rgb("#f3f2ee"), inset: 6pt, radius: 4pt, width: 100%, it)
#set table(stroke: 0.5pt + rgb("#c3c2b7"), inset: 4pt)
#show figure: set block(breakable: true)
#show figure.where(kind: table): set block(breakable: true)
#show table: set text(size: 0.9em)
#show quote: it => block(fill: rgb("#e8f1fc"), inset: 7pt, radius: 4pt, width: 100%, it.body)
#set list(spacing: 0.45em)
#align(left)[#text(size: 1.75em, weight: "bold")[{title}] \ #text(size: 1.0em, fill: rgb("#52514e"))[{subtitle}]]
#v(0.3em)
"""


def expand_includes(md: str) -> str:
    """`{{include generated/x.md}}` on its own line → that file's contents (paths relative to docs/)."""
    def repl(m: re.Match) -> str:
        f = DOCS / m.group(1).strip()
        return f.read_text(encoding="utf-8") if f.exists() else f"_(missing {m.group(1)})_"
    return re.sub(r"^\{\{include (.+?)\}\}\s*$", repl, md, flags=re.M)


def fill_placeholders(md: str) -> str:
    vals_file = DOCS / "submission_values.yaml"
    vals = yaml.safe_load(vals_file.read_text(encoding="utf-8")) if vals_file.exists() else {}
    for k, v in (vals or {}).items():
        if v not in (None, ""):
            md = md.replace(f"<{k}>", str(v))
    return md


def split_front_matter(md: str) -> tuple[dict, str]:
    m = re.match(r"^---\n(.*?)\n---\n", md, re.S)
    if not m:
        return {}, md
    return yaml.safe_load(m.group(1)) or {}, md[m.end():]


def build(key: str) -> Path:
    import typst
    src, out_name, style = DOCS_TO_BUILD[key]
    md = fill_placeholders(expand_includes((DOCS / src).read_text(encoding="utf-8")))
    meta, body = split_front_matter(md)
    body_typ = subprocess.run([PANDOC, "-f", "markdown+pipe_tables+link_attributes", "-t", "typst", "--wrap=none"],
                              input=body, capture_output=True, text=True, check=True).stdout
    body_typ = body_typ.replace("align(center)[#table(", "align(left)[#table(")
    esc = lambda s: str(s).replace('"', '\\"').replace("#", "\\#")
    pre = PREAMBLE.replace("{title}", esc(meta.get("title", ""))).replace("{subtitle}", esc(meta.get("subtitle", "")))
    pre = pre.replace("{size}", style["size"]).replace("{margin}", style["margin"])
    typ_path = DOCS / f".{key}.typ"
    typ_path.write_text(pre + "\n" + body_typ, encoding="utf-8")
    OUT.mkdir(exist_ok=True)
    pdf = OUT / out_name
    typst.compile(str(typ_path), output=str(pdf), root=str(DOCS),
                  font_paths=["/System/Library/Fonts", "/System/Library/Fonts/Supplemental", "/Library/Fonts"])
    try:
        from pypdf import PdfReader
        pages = len(PdfReader(str(pdf)).pages)
    except Exception:
        pages = "?"
    print(f"built {pdf.relative_to(ROOT)} ({pages} pages)")
    limit = PAGE_LIMITS.get(key)
    if limit and isinstance(pages, int) and pages > limit:
        raise SystemExit(f"ERROR: {out_name} has {pages} pages; the limit is {limit}. Shorten it.")
    return pdf


if __name__ == "__main__":
    for k in sys.argv[1:] or list(DOCS_TO_BUILD):
        build(k)
