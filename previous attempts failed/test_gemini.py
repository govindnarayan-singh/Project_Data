import os
import time
import logging
from pathlib import Path

import fitz  # PyMuPDF
import pytesseract
from pdf2image import convert_from_path

from dotenv import load_dotenv
from google import genai

# ==========================================================
# CONFIGURATION
# ==========================================================

load_dotenv()

API_KEY = os.getenv("GEMINI_API_KEY")

if not API_KEY:
    raise ValueError("GEMINI_API_KEY not found in .env file")

client = genai.Client(api_key=API_KEY)

MODEL = "gemini-3.5-flash"

INPUT_FOLDER = Path(
    "GRs/Persons with Disabilities Welfare Department"
)

OUTPUT_FOLDER = Path(
    "translated/Persons with Disabilities Welfare Department"
)

OUTPUT_FOLDER.mkdir(parents=True, exist_ok=True)

MAX_FILES = 10

# OCR executable (Windows)

pytesseract.pytesseract.tesseract_cmd = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe"
)

# Marathi language data

OCR_LANGUAGE = "mar"

# Chunk size for Gemini

CHUNK_SIZE = 2000

# ==========================================================
# LOGGING
# ==========================================================

logging.basicConfig(
    filename="translation.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

# ==========================================================
# TEXT EXTRACTION
# ==========================================================

def extract_text_from_pdf(pdf_path):

    doc = fitz.open(pdf_path)

    text = ""

    for page in doc:
        text += page.get_text()

    doc.close()

    return text

# ==========================================================
# DETECT SCANNED PDF
# ==========================================================

def is_scanned_pdf(pdf_path):

    text = extract_text_from_pdf(pdf_path)

    return len(text.strip()) < 50

# ==========================================================
# OCR FUNCTION
# ==========================================================

def ocr_pdf(pdf_path):

    print("Running OCR...")

    images = convert_from_path(pdf_path)

    text = ""

    for image in images:

        page_text = pytesseract.image_to_string(
            image,
            lang=OCR_LANGUAGE
        )

        text += page_text
        text += "\n"

    return text

# ==========================================================
# GET TEXT
# ==========================================================

def get_pdf_text(pdf_path):

    if is_scanned_pdf(pdf_path):

        logging.info(f"OCR used: {pdf_path.name}")

        return ocr_pdf(pdf_path)

    else:

        logging.info(f"Text PDF: {pdf_path.name}")

        return extract_text_from_pdf(pdf_path)

# ==========================================================
# CHUNKING
# ==========================================================

def split_text(text, chunk_size=CHUNK_SIZE):

    chunks = []

    start = 0

    while start < len(text):

        end = start + chunk_size

        chunks.append(text[start:end])

        start = end

    return chunks

# ==========================================================
# PRINT SUMMARY
# ==========================================================

def print_summary():

    print("=" * 60)
    print("Department Translator")
    print("=" * 60)
    print("Input Folder :", INPUT_FOLDER)
    print("Output Folder:", OUTPUT_FOLDER)
    print("Model        :", MODEL)
    print("Chunk Size   :", CHUNK_SIZE)
    print("Max PDFs     :", MAX_FILES)
    print("=" * 60)

# ==========================================================
# TEST
# ==========================================================

if __name__ == "__main__":

    print_summary()

    pdfs = sorted(INPUT_FOLDER.glob("*.pdf"))

    print(f"\nFound {len(pdfs)} PDFs\n")

    if len(pdfs) == 0:
        raise Exception("No PDFs found!")

    first_pdf = pdfs[0]

    print("Testing first PDF:")
    print(first_pdf.name)

    text = get_pdf_text(first_pdf)

    print("\nCharacters extracted:", len(text))

    chunks = split_text(text)

    print("Chunks:", len(chunks))

    print("\nFirst 500 characters:\n")


    print(text[:500])


    from google.genai import errors, types
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

# ==========================================================
# PDF FONT
# ==========================================================

FONT_PATH = r"C:\Windows\Fonts\arial.ttf"

pdfmetrics.registerFont(
    TTFont("Arial", FONT_PATH)
)

# ==========================================================
# TRANSLATE ONE CHUNK
# ==========================================================

def translate_chunk(chunk):

    prompt = f"""
You are a professional Marathi to English Government translator.

Translate the following Marathi Government Resolution into English.

Rules:

1. Translate every sentence.
2. Do not summarize.
3. Preserve numbering.
4. Preserve headings.
5. Preserve official meaning.
6. Preserve dates.
7. Preserve names of schemes.
8. Keep formatting wherever possible.

Marathi Text:

{chunk}
"""

    retries = 5

    for attempt in range(retries):

        try:

            response = client.models.generate_content(
                model=MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.1,
                    max_output_tokens=4000
                )
            )

            if response.text:
                return response.text

            return ""

        except errors.ServerError:

            wait = (attempt + 1) * 5

            print(f"Server busy. Waiting {wait} seconds...")

            time.sleep(wait)

        except Exception as e:

            print("Gemini Error:", e)

            wait = (attempt + 1) * 5

            time.sleep(wait)

    raise Exception("Translation failed after retries.")

# ==========================================================
# TRANSLATE COMPLETE DOCUMENT
# ==========================================================

def translate_document(text):

    chunks = split_text(text)

    english_document = ""

    total = len(chunks)

    print(f"Total Chunks: {total}")

    for i, chunk in enumerate(chunks):

        print(f"Chunk {i+1}/{total}")

        translated = translate_chunk(chunk)

        english_document += translated

        english_document += "\n\n"

    return english_document

# ==========================================================
# SAVE ENGLISH PDF
# ==========================================================

def save_pdf(text, output_file):

    c = canvas.Canvas(
        str(output_file),
        pagesize=A4
    )

    width, height = A4

    margin = 40

    y = height - margin

    c.setFont("Arial", 10)

    lines = text.split("\n")

    for line in lines:

        while len(line) > 110:

            c.drawString(
                margin,
                y,
                line[:110]
            )

            line = line[110:]

            y -= 15

            if y < margin:

                c.showPage()

                c.setFont("Arial", 10)

                y = height - margin

        c.drawString(
            margin,
            y,
            line
        )

        y -= 15

        if y < margin:

            c.showPage()

            c.setFont("Arial", 10)

            y = height - margin

    c.save()

# ==========================================================
# TRANSLATE SINGLE PDF
# ==========================================================

def process_pdf(pdf_path):

    print("=" * 60)
    print("Processing:", pdf_path.name)

    logging.info(f"Started {pdf_path.name}")

    text = get_pdf_text(pdf_path)

    if len(text.strip()) == 0:

        print("No text extracted.")

        logging.warning(f"No text {pdf_path.name}")

        return

    print("Characters:", len(text))

    english = translate_document(text)

    output_pdf = OUTPUT_FOLDER / f"{pdf_path.stem}_English.pdf"

    save_pdf(
        english,
        output_pdf
    )

    logging.info(f"Completed {pdf_path.name}")

    print("Saved:", output_pdf)

    # ==========================================================
# MAIN PROGRAM
# ==========================================================

def main():

    print_summary()

    pdfs = sorted(INPUT_FOLDER.glob("*.pdf"))

    print(f"\nFound {len(pdfs)} PDFs\n")

    if len(pdfs) == 0:
        print("No PDF files found.")
        return

    processed = 0
    success = 0
    failed = 0

    success_log = open(
        "success.log",
        "a",
        encoding="utf-8"
    )

    failed_log = open(
        "failed.log",
        "a",
        encoding="utf-8"
    )

    for pdf in pdfs:

        if processed >= MAX_FILES:
            break

        output_pdf = OUTPUT_FOLDER / f"{pdf.stem}_English.pdf"

        # Skip already translated PDFs

        if output_pdf.exists():

            print(f"Skipping {pdf.name} (already translated)")

            continue

        try:

            process_pdf(pdf)

            success += 1

            success_log.write(pdf.name + "\n")

            success_log.flush()

        except Exception as e:

            failed += 1

            print("\nERROR:", pdf.name)

            print(e)

            logging.exception(e)

            failed_log.write(
                f"{pdf.name} : {str(e)}\n"
            )

            failed_log.flush()

        processed += 1

    success_log.close()
    failed_log.close()

    print("\n")
    print("=" * 60)
    print("Translation Finished")
    print("=" * 60)
    print(f"Processed : {processed}")
    print(f"Success   : {success}")
    print(f"Failed    : {failed}")
    print("=" * 60)


# ==========================================================
# ENTRY POINT
# ==========================================================

if __name__ == "__main__":

    start = time.time()

    main()

    end = time.time()

    print(f"\nExecution Time: {end-start:.2f} seconds")