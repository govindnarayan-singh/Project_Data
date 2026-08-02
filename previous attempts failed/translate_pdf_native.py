"""
Translate Marathi Government Resolution PDFs to English using Gemini's native PDF understanding.

Why this version is different:
- It does NOT extract Marathi text with PyMuPDF.
- It sends the original PDF pages to Gemini, so legacy Marathi font encodings do not corrupt the input.
- Gemini returns structured HTML.
- Chromium prints the HTML to a clean A4 PDF.
- It processes the first 10 PDFs and skips completed outputs.
"""

from __future__ import annotations

import csv
import html
import logging
import os
import random
import re
import time
from pathlib import Path
from typing import Optional

import fitz
from dotenv import load_dotenv
from google import genai
from google.genai import errors, types
from playwright.sync_api import sync_playwright


# ============================================================
# CONFIGURATION
# ============================================================

load_dotenv()

API_KEY = os.getenv("GEMINI_API_KEY")
if not API_KEY:
    raise RuntimeError(
        "GEMINI_API_KEY was not found. Put it in the .env file:\n"
        "GEMINI_API_KEY=your_key_here"
    )

client = genai.Client(api_key=API_KEY)

INPUT_FOLDER = Path(
    "GRs/Persons with Disabilities Welfare Department"
)
OUTPUT_FOLDER = Path(
    "translated/Persons with Disabilities Welfare Department"
)
HTML_FOLDER = OUTPUT_FOLDER / "html"
LOG_FOLDER = Path("logs")

OUTPUT_FOLDER.mkdir(parents=True, exist_ok=True)
HTML_FOLDER.mkdir(parents=True, exist_ok=True)
LOG_FOLDER.mkdir(parents=True, exist_ok=True)

MAX_FILES = 10

# The script tries the next model when one is unavailable or overloaded.
MODEL_CANDIDATES = [
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-flash-latest",
]

MAX_RETRIES_PER_MODEL = 4
MAX_INLINE_PDF_MB = 45
MAX_OUTPUT_TOKENS = 32768

CSV_LOG = LOG_FOLDER / "translation_results.csv"
TEXT_LOG = LOG_FOLDER / "translation_runtime.log"

logging.basicConfig(
    filename=TEXT_LOG,
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    encoding="utf-8",
)


# ============================================================
# TRANSLATION PROMPT
# ============================================================

TRANSLATION_PROMPT = """
You are translating an official Government of Maharashtra document from Marathi
to English.

Read the attached PDF visually and structurally. Do not depend only on its
embedded text layer because the PDF may use a legacy Marathi font encoding.

Translate every visible Marathi item faithfully into formal administrative
English.

Mandatory requirements:
1. Do not summarize, shorten, omit, explain, or add information.
2. Preserve the original order of all content.
3. Preserve document title, department name, Government Resolution number,
   dates, references, headings, numbered clauses, signatures, designations,
   distribution list, URLs, account heads, codes, and all numerical values.
4. Recreate every table using proper HTML table rows and columns.
5. Translate पदनाम as "Designation", पदसंख्या as "Number of Posts",
   अ.क्र. as "Sr. No.", शासन निर्णय as "Government Resolution",
   प्रस्तावना as "Preamble", वाचा as "Read", and प्रत as "Copy to",
   when these meanings apply.
6. Keep personal names and official reference numbers accurate. Transliterate
   a personal name only when an established English spelling is not visible.
7. Do not leave Marathi text in the result except where it is genuinely
   impossible to translate, such as a proper name with no reliable spelling.
8. Do not include translation notes, confidence comments, markdown, code
   fences, or introductory sentences.

Output requirements:
- Return only an HTML fragment, not a complete HTML document.
- Use semantic tags such as <h1>, <h2>, <h3>, <p>, <ol>, <ul>, <table>,
  <thead>, <tbody>, <tr>, <th>, <td>, <strong>, and <div>.
- Use <div class="source-page-break"></div> at the closest possible boundary
  between source PDF pages.
- Do not include CSS, JavaScript, <html>, <head>, or <body> tags.
"""


# ============================================================
# HELPERS
# ============================================================

def get_page_count(pdf_path: Path) -> int:
    with fitz.open(pdf_path) as document:
        return document.page_count


def clean_model_html(raw_text: str) -> str:
    """Remove common model wrappers while keeping the HTML fragment."""
    text = (raw_text or "").strip()

    text = re.sub(r"^```(?:html)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)

    body_match = re.search(
        r"<body[^>]*>(.*)</body>",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if body_match:
        text = body_match.group(1).strip()

    text = re.sub(r"</?(?:html|head)[^>]*>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text,
                  flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<script[^>]*>.*?</script>", "", text,
                  flags=re.IGNORECASE | re.DOTALL)

    # If the model unexpectedly returned plain text, preserve it safely.
    if not re.search(r"<[a-zA-Z][^>]*>", text):
        paragraphs = [
            f"<p>{html.escape(part.strip())}</p>"
            for part in re.split(r"\n\s*\n", text)
            if part.strip()
        ]
        text = "\n".join(paragraphs)

    return text.strip()


def devanagari_ratio(text: str) -> float:
    """Estimate whether too much untranslated Marathi remains."""
    visible = re.sub(r"<[^>]+>", "", text)
    letters = [ch for ch in visible if ch.isalpha()]
    if not letters:
        return 0.0
    devanagari = sum("\u0900" <= ch <= "\u097f" for ch in letters)
    return devanagari / len(letters)


def response_is_acceptable(translated_html: str) -> tuple[bool, str]:
    plain = re.sub(r"<[^>]+>", " ", translated_html)
    plain = re.sub(r"\s+", " ", plain).strip()

    if len(plain) < 300:
        return False, "The returned translation is unexpectedly short."

    ratio = devanagari_ratio(translated_html)
    if ratio > 0.03:
        return False, f"Too much untranslated Devanagari remains ({ratio:.1%})."

    if "<table" not in translated_html.lower():
        # Government Resolutions may not all contain tables, so this is only logged.
        logging.warning("No HTML table was found in the model response.")

    return True, ""


def should_retry_client_error(exc: errors.ClientError) -> bool:
    message = str(exc)
    return any(code in message for code in ("429", "RESOURCE_EXHAUSTED", "408"))


def model_not_available(exc: errors.ClientError) -> bool:
    message = str(exc)
    return any(code in message for code in ("404", "NOT_FOUND"))


# ============================================================
# GEMINI PDF TRANSLATION
# ============================================================

def call_gemini_with_pdf(
    pdf_path: Path,
    prompt: str,
) -> tuple[str, str]:
    """
    Return (translated_html, model_used).

    The original PDF bytes are sent directly to Gemini. This avoids corrupted
    text caused by legacy Marathi font character maps.
    """
    size_mb = pdf_path.stat().st_size / (1024 * 1024)
    if size_mb > MAX_INLINE_PDF_MB:
        raise ValueError(
            f"{pdf_path.name} is {size_mb:.1f} MB. "
            f"This script's inline limit is {MAX_INLINE_PDF_MB} MB."
        )

    pdf_bytes = pdf_path.read_bytes()
    pdf_part = types.Part.from_bytes(
        data=pdf_bytes,
        mime_type="application/pdf",
    )

    last_error: Optional[Exception] = None

    for model in MODEL_CANDIDATES:
        for attempt in range(1, MAX_RETRIES_PER_MODEL + 1):
            try:
                print(
                    f"  Model: {model} | attempt "
                    f"{attempt}/{MAX_RETRIES_PER_MODEL}"
                )

                response = client.models.generate_content(
                    model=model,
                    contents=[pdf_part, prompt],
                    config=types.GenerateContentConfig(
                        temperature=0.0,
                        max_output_tokens=MAX_OUTPUT_TOKENS,
                    ),
                )

                translated_html = clean_model_html(response.text or "")
                ok, reason = response_is_acceptable(translated_html)

                if not ok:
                    raise RuntimeError(
                        f"Translation quality check failed: {reason}"
                    )

                return translated_html, model

            except errors.ServerError as exc:
                last_error = exc
                wait = min(45, (2 ** (attempt - 1)) * 4) + random.uniform(0, 2)
                print(f"  Gemini server busy. Retrying in {wait:.1f} seconds.")
                logging.warning(
                    "%s | %s | server error | attempt %s | %s",
                    pdf_path.name, model, attempt, exc
                )
                time.sleep(wait)

            except errors.ClientError as exc:
                last_error = exc

                if model_not_available(exc):
                    print(f"  Model {model} is unavailable; trying next model.")
                    logging.warning(
                        "%s | model unavailable | %s | %s",
                        pdf_path.name, model, exc
                    )
                    break

                if should_retry_client_error(exc):
                    wait = min(60, (2 ** (attempt - 1)) * 6) + random.uniform(0, 3)
                    print(f"  API rate limit. Retrying in {wait:.1f} seconds.")
                    logging.warning(
                        "%s | %s | client retry | attempt %s | %s",
                        pdf_path.name, model, attempt, exc
                    )
                    time.sleep(wait)
                    continue

                raise

            except RuntimeError as exc:
                # A weak or incomplete translation is retried using a stricter prompt.
                last_error = exc
                if attempt < MAX_RETRIES_PER_MODEL:
                    print(f"  {exc}")
                    prompt = (
                        TRANSLATION_PROMPT
                        + "\nYour previous output failed validation. Produce a complete "
                          "English translation of every visible item. Return only HTML."
                    )
                    time.sleep(2)
                    continue
                break

    raise RuntimeError(
        f"All Gemini models failed for {pdf_path.name}. Last error: {last_error}"
    )


# ============================================================
# HTML AND PDF OUTPUT
# ============================================================

def build_complete_html(
    translated_fragment: str,
    source_filename: str,
) -> str:
    safe_source = html.escape(source_filename)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{safe_source} - English Translation</title>
<style>
  @page {{
    size: A4;
    margin: 17mm 16mm 18mm 16mm;
  }}

  * {{
    box-sizing: border-box;
  }}

  body {{
    margin: 0;
    font-family: Arial, "Noto Sans", sans-serif;
    font-size: 10.5pt;
    line-height: 1.48;
    color: #111;
  }}

  h1 {{
    font-size: 16pt;
    line-height: 1.25;
    text-align: center;
    margin: 0 0 10pt 0;
  }}

  h2 {{
    font-size: 13pt;
    line-height: 1.3;
    margin: 13pt 0 6pt 0;
  }}

  h3 {{
    font-size: 11.5pt;
    line-height: 1.3;
    margin: 10pt 0 5pt 0;
  }}

  p {{
    margin: 0 0 7pt 0;
    text-align: justify;
    orphans: 3;
    widows: 3;
  }}

  ol, ul {{
    margin: 4pt 0 8pt 20pt;
    padding-left: 12pt;
  }}

  li {{
    margin-bottom: 4pt;
  }}

  table {{
    width: 100%;
    border-collapse: collapse;
    margin: 9pt 0 12pt 0;
    page-break-inside: auto;
    font-size: 9.5pt;
  }}

  thead {{
    display: table-header-group;
  }}

  tr {{
    page-break-inside: avoid;
  }}

  th, td {{
    border: 0.7pt solid #222;
    padding: 4pt 5pt;
    vertical-align: top;
  }}

  th {{
    font-weight: bold;
    text-align: center;
  }}

  .source-page-break {{
    break-before: page;
    page-break-before: always;
  }}

  .translation-note {{
    margin-top: 16pt;
    padding-top: 6pt;
    border-top: 0.5pt solid #999;
    font-size: 8pt;
    color: #555;
  }}
</style>
</head>
<body>
{translated_fragment}
<div class="translation-note">
English translation generated from source file: {safe_source}
</div>
</body>
</html>
"""


def write_html_file(
    translated_html: str,
    source_pdf: Path,
) -> Path:
    output_html = HTML_FOLDER / f"{source_pdf.stem}_English.html"
    complete_html = build_complete_html(
        translated_html,
        source_pdf.name,
    )
    output_html.write_text(complete_html, encoding="utf-8")
    return output_html


def convert_html_to_pdf(
    html_path: Path,
    output_pdf: Path,
) -> None:
    html_content = html_path.read_text(encoding="utf-8")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_content(html_content, wait_until="load")
        page.emulate_media(media="print")
        page.pdf(
            path=str(output_pdf),
            format="A4",
            print_background=True,
            prefer_css_page_size=True,
            display_header_footer=True,
            header_template="<div></div>",
            footer_template=(
                '<div style="width:100%; font-size:8px; color:#666; '
                'text-align:center;"><span class="pageNumber"></span> / '
                '<span class="totalPages"></span></div>'
            ),
            margin={
                "top": "17mm",
                "right": "16mm",
                "bottom": "18mm",
                "left": "16mm",
            },
        )
        browser.close()


# ============================================================
# LOGGING AND MAIN LOOP
# ============================================================

def append_csv_log(
    filename: str,
    status: str,
    pages: int,
    model: str,
    seconds: float,
    message: str = "",
) -> None:
    file_exists = CSV_LOG.exists()

    with CSV_LOG.open("a", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        if not file_exists:
            writer.writerow(
                ["filename", "status", "pages", "model", "seconds", "message"]
            )
        writer.writerow(
            [
                filename,
                status,
                pages,
                model,
                f"{seconds:.2f}",
                message,
            ]
        )


def process_one_pdf(pdf_path: Path) -> None:
    output_pdf = OUTPUT_FOLDER / f"{pdf_path.stem}_English.pdf"

    if output_pdf.exists():
        print(f"Skipping {pdf_path.name}: output already exists.")
        return

    started = time.perf_counter()
    pages = get_page_count(pdf_path)
    model_used = ""

    print("=" * 72)
    print(f"Processing: {pdf_path.name}")
    print(f"Pages: {pages}")

    try:
        translated_fragment, model_used = call_gemini_with_pdf(
            pdf_path=pdf_path,
            prompt=TRANSLATION_PROMPT,
        )

        output_html = write_html_file(
            translated_html=translated_fragment,
            source_pdf=pdf_path,
        )

        convert_html_to_pdf(
            html_path=output_html,
            output_pdf=output_pdf,
        )

        elapsed = time.perf_counter() - started
        append_csv_log(
            filename=pdf_path.name,
            status="SUCCESS",
            pages=pages,
            model=model_used,
            seconds=elapsed,
        )
        logging.info(
            "%s | SUCCESS | pages=%s | model=%s | %.2fs",
            pdf_path.name, pages, model_used, elapsed
        )

        print(f"Saved HTML: {output_html}")
        print(f"Saved PDF : {output_pdf}")
        print(f"Time      : {elapsed:.1f} seconds")

    except Exception as exc:
        elapsed = time.perf_counter() - started
        append_csv_log(
            filename=pdf_path.name,
            status="FAILED",
            pages=pages,
            model=model_used,
            seconds=elapsed,
            message=str(exc),
        )
        logging.exception("%s | FAILED", pdf_path.name)
        print(f"FAILED: {pdf_path.name}")
        print(f"Reason: {exc}")


def main() -> None:
    pdfs = sorted(INPUT_FOLDER.glob("*.pdf"))

    print("=" * 72)
    print("Native PDF Marathi-to-English Translator")
    print(f"Input : {INPUT_FOLDER}")
    print(f"Output: {OUTPUT_FOLDER}")
    print(f"Found : {len(pdfs)} PDFs")
    print(f"Limit : {MAX_FILES} new PDFs")
    print("=" * 72)

    if not pdfs:
        raise FileNotFoundError(f"No PDF files found in {INPUT_FOLDER}")

    attempted = 0

    for pdf_path in pdfs:
        output_pdf = OUTPUT_FOLDER / f"{pdf_path.stem}_English.pdf"
        if output_pdf.exists():
            continue

        if attempted >= MAX_FILES:
            break

        process_one_pdf(pdf_path)
        attempted += 1

    print("=" * 72)
    print(f"Finished. Attempted {attempted} new PDF(s).")
    print(f"Results log: {CSV_LOG}")
    print("=" * 72)


if __name__ == "__main__":
    main()
