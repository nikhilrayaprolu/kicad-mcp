"""Extract per-page text from PDFs into <pdf>.txt with page markers."""
import sys, pathlib, pymupdf
for p in sys.argv[1:]:
    p = pathlib.Path(p)
    doc = pymupdf.open(p)
    out = []
    for i, page in enumerate(doc, 1):
        out.append(f"\n===== PAGE {i} =====\n")
        out.append(page.get_text("text", sort=True))
    p.with_suffix(".txt").write_text("".join(out))
    print(p.name, doc.page_count, sum(len(x) for x in out))
