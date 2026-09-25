"""Stage 0: dump raw PDFs to plain text with page markers.

Every later parser reads these .txt files instead of re-opening PDFs, and the
page markers let us keep a source page reference for every extracted fact.

Usage: python -m recommender.ingest.pdf_text
"""
from pathlib import Path

import pymupdf

from recommender.config import PAGE_MARKER, RAW, RAW_HANDOUTS, TEXT, TEXT_HANDOUTS


def pdf_to_text(pdf_path: Path, out_path: Path) -> int:
    doc = pymupdf.open(pdf_path)
    parts = []
    for i, page in enumerate(doc, start=1):
        # sort=True orders text blocks top-to-bottom, left-to-right
        parts.append(f"\n{PAGE_MARKER.format(n=i)}\n{page.get_text(sort=True)}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("".join(parts), encoding="utf-8")
    return len(doc)


def run():
    for pdf in sorted(RAW.glob("*.pdf")):
        n = pdf_to_text(pdf, TEXT / f"{pdf.stem}.txt")
        print(f"{pdf.name}: {n} pages")
    handouts = sorted(RAW_HANDOUTS.glob("*.pdf"))
    for pdf in handouts:
        pdf_to_text(pdf, TEXT_HANDOUTS / f"{pdf.stem}.txt")
    print(f"handouts: {len(handouts)} files")


if __name__ == "__main__":
    run()
