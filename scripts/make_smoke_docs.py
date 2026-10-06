"""Builds the smoke-test documents. Runs inside the API container (`docker compose exec`), which
also proves PyMuPDF and Pillow work in the image. Writes /tmp/text.pdf and /tmp/scan.pdf."""

import pymupdf
from PIL import Image, ImageDraw, ImageFont

text = pymupdf.open()
text.new_page().insert_text((72, 72), "Revenue increased by twenty three percent this year.")
text.save("/tmp/text.pdf")

image = Image.new("RGB", (1600, 300), "white")
ImageDraw.Draw(image).text(
    (40, 40), "Scanned invoice total 4200", fill="black", font=ImageFont.load_default(size=56)
)
image.save("/tmp/scan.png")
scan = pymupdf.open()
scan.new_page().insert_image(pymupdf.Rect(36, 36, 576, 140), filename="/tmp/scan.png")
scan.save("/tmp/scan.pdf")
