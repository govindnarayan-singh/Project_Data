import fitz
import pytesseract

from PIL import Image

PDF_PATH = (
    r"GRs\Persons with Disabilities Welfare Department"
    r"\202301041906309635.pdf"
)

pytesseract.pytesseract.tesseract_cmd = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe"
)

document = fitz.open(PDF_PATH)
page = document[0]

matrix = fitz.Matrix(300 / 72, 300 / 72)
pixmap = page.get_pixmap(matrix=matrix, alpha=False)

image = Image.frombytes(
    "RGB",
    (pixmap.width, pixmap.height),
    pixmap.samples,
)

text = pytesseract.image_to_string(
    image,
    lang="mar+eng",
    config="--oem 1 --psm 3",
)

document.close()

print(text[:2000])