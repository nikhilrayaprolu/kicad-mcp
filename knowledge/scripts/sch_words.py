"""Dump schematic page words with coordinates (x0,y0,word) to <pdf>.words.txt; also compact text dump."""
import sys, pathlib, pymupdf
for p in sys.argv[1:]:
    p = pathlib.Path(p); doc = pymupdf.open(p)
    lines = []
    for i, page in enumerate(doc, 1):
        lines.append(f"===== PAGE {i} size={page.rect} rot={page.rotation}")
        for w in page.get_text("words"):
            lines.append(f"{i}\t{w[0]:.1f}\t{w[1]:.1f}\t{w[4]}")
        # compact text: blocks
    p.with_suffix(".words.txt").write_text("\n".join(lines))
    blocks = []
    for i, page in enumerate(doc, 1):
        blocks.append(f"\n===== PAGE {i} =====\n")
        for b in page.get_text("blocks"):
            blocks.append(f"[{b[0]:.0f},{b[1]:.0f}] {b[4].strip()}\n")
    p.with_suffix(".txt").write_text("".join(blocks))
    print(p.name, len(lines))
