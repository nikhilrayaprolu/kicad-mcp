"""Render PDF page(s) to PNG: render.py pdf page[,page] outdir zoom [clip x0,y0,x1,y1 in pts]"""
import sys, pathlib, pymupdf
pdf, pages, outdir, zoom = sys.argv[1], sys.argv[2], pathlib.Path(sys.argv[3]), float(sys.argv[4])
clip = pymupdf.Rect(*map(float, sys.argv[5].split(","))) if len(sys.argv) > 5 else None
outdir.mkdir(parents=True, exist_ok=True)
doc = pymupdf.open(pdf)
for p in map(int, pages.split(",")):
    pg = doc[p-1]
    print("page size", pg.rect)
    pix = pg.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip)
    out = outdir / f"{pathlib.Path(pdf).stem}_p{p}{'_clip' if clip else ''}.png"
    pix.save(out); print(out, pix.width, pix.height)
