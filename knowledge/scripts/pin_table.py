"""Extract RV1106G2/G3 QFN128 pin table (datasheet Table 2-1) via pymupdf find_tables."""
import sys, json, re, pymupdf
pdf, pages = sys.argv[1], [int(x) for x in sys.argv[2].split(",")]
doc = pymupdf.open(pdf)
rows = []
for pn in pages:
    page = doc[pn-1]
    for t in page.find_tables().tables:
        for r in t.extract():
            rows.append((pn, r))
for pn, r in rows:
    print(pn, json.dumps(r))
