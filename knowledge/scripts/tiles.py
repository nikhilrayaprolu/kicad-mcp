"""Render a PDF page into NxM tiles at a zoom: tiles.py pdf page cols rows zoom outdir"""
import sys, pathlib, pymupdf
pdf, pg, cols, rows, zoom, out = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]), float(sys.argv[5]), pathlib.Path(sys.argv[6])
out.mkdir(parents=True, exist_ok=True)
page = pymupdf.open(pdf)[pg-1]; R = page.rect
w, h = R.width/cols, R.height/rows
for r in range(rows):
    for c in range(cols):
        clip = pymupdf.Rect(c*w, r*h, (c+1)*w, (r+1)*h)
        page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip).save(out/f"p{pg}_r{r}c{c}.png")
        print(out/f"p{pg}_r{r}c{c}.png", clip)
