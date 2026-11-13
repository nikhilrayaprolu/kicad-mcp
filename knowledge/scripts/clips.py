"""Render named clips: clips.py pdf page zoom outdir name:x0,y0,x1,y1 ..."""
import sys, pathlib, pymupdf
pdf, pg, zoom, out = sys.argv[1], int(sys.argv[2]), float(sys.argv[3]), pathlib.Path(sys.argv[4])
out.mkdir(parents=True, exist_ok=True)
page = pymupdf.open(pdf)[pg-1]
for spec in sys.argv[5:]:
    name, r = spec.split(":"); clip = pymupdf.Rect(*map(float, r.split(",")))
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip); pix.save(out/f"{name}.png"); print(name, pix.width, pix.height)
