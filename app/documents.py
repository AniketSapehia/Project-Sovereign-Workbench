"""
app/documents.py — reading documents and turning them into searchable pieces.

The problem statement asks the workbench to handle "scanned PDFs, handwritten
notes, engineering drawings, photographs, read through on-device OCR". This
file is where every kind of file becomes plain text:

    .txt / .md      -> read directly
    .docx (Word)    -> read paragraphs and tables with python-docx
    .pdf            -> pages WITH a text layer: read the text directly (fast)
                       pages WITHOUT one (scanned): render to an image + OCR
    images          -> OCR with Tesseract

Then `chunk_pages()` cuts the text into small pieces ("chunks") that the
knowledge base can search (see app/knowledge.py).

Everything here runs on this computer. Tesseract is a local program; no
file is ever uploaded anywhere.
"""

import re
from pathlib import Path

import numpy as np           # fast pixel maths (used to erase table lines)
import pypdf                 # reads the text layer of PDFs
import pypdfium2 as pdfium   # renders PDF pages to images (for scanned pages)
import pytesseract           # Python wrapper around the Tesseract OCR program
from docx import Document    # python-docx: reads Word .docx files
from docx.table import Table
from PIL import Image, ImageOps   # Pillow: opens and cleans up images

import config

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}

# An A4 page scanned at 300 DPI is about 2480 pixels wide. Narrower images
# are enlarged to this width before OCR.
OCR_TARGET_WIDTH = 2480

# Words Tesseract reads with less than this confidence (0-100) are counted
# as "uncertain" and reported to the user.
LOW_CONFIDENCE = 60


# ===========================================================================
# 1. OCR (reading text out of images)
# ===========================================================================

def _setup_tesseract() -> None:
    """Tell pytesseract where tesseract.exe is, if it is at the configured path."""
    if Path(config.TESSERACT_CMD).exists():
        pytesseract.pytesseract.tesseract_cmd = config.TESSERACT_CMD
    # Otherwise pytesseract looks for "tesseract" on the PATH by itself.


def ocr_status() -> dict:
    """Check whether Tesseract is installed. Used by the health check."""
    _setup_tesseract()
    try:
        version = str(pytesseract.get_tesseract_version())
        return {"available": True, "version": version}
    except Exception as error:
        return {"available": False, "error": str(error)}


def _straight_runs(dark: np.ndarray, length: int, axis: int) -> np.ndarray:
    """
    Find pixels that belong to a straight dark line at least `length` pixels
    long, along `axis` (1 = horizontal, 0 = vertical).

    How: a "cumulative sum" lets us count dark pixels in every window of
    `length` pixels at once. A window that is 100% dark is part of a line.
    Letters are never that long, so text is left untouched.
    """
    a = np.moveaxis(dark, axis, -1).astype(np.int32)
    pad = np.zeros(a.shape[:-1] + (1,), np.int32)

    total = np.concatenate([pad, np.cumsum(a, axis=-1)], axis=-1)
    starts = (total[..., length:] - total[..., :-length]) == length   # fully dark windows

    # Mark every pixel covered by at least one fully dark window.
    covered = np.concatenate([pad, np.cumsum(starts, axis=-1)], axis=-1)
    idx = np.arange(a.shape[-1])
    hi = np.minimum(idx + 1, starts.shape[-1])
    lo = np.clip(idx - length + 1, 0, starts.shape[-1])
    mask = (covered[..., hi] - covered[..., lo]) > 0
    return np.moveaxis(mask, -1, axis)


def _remove_table_lines(image: Image.Image) -> Image.Image:
    """
    Erase the ruled lines of tables before OCR.

    Inspection reports are full of tables, and Tesseract often skips whole
    table rows because the borders confuse its layout analysis. In our tests
    on a scanned report, erasing the lines first recovered the missing rows.
    Line lengths are relative to the page size, so this works at any resolution.
    """
    pixels = np.array(image)
    dark = pixels < 160
    width, height = image.size
    mask = (_straight_runs(dark, max(60, width // 16), axis=1) |      # horizontal lines
            _straight_runs(dark, max(40, height // 34), axis=0))      # vertical lines

    # Widen the mask by 2 pixels: scanned lines have blurry edges.
    grown = mask.copy()
    for shift in (1, 2):
        for ax in (0, 1):
            grown |= np.roll(mask, shift, ax) | np.roll(mask, -shift, ax)

    pixels = pixels.copy()
    pixels[grown] = 255            # paint the lines white
    return Image.fromarray(pixels)


def ocr_image(image: Image.Image) -> dict:
    """
    Read the text in one image with Tesseract.

    Returns {"text": ..., "confidence": 0-100}.
    The confidence is Tesseract's own average certainty for the words it
    found. We show it on the page so users know when to double-check —
    being honest about OCR quality is part of being trustworthy.
    """
    _setup_tesseract()

    # Light clean-up helps OCR a lot on scans and phone photos:
    #   convert("L")   -> greyscale (colour only confuses OCR)
    #   autocontrast   -> stretch faint grey text towards black-on-white
    #   upscale        -> Tesseract works best at ~300 DPI; small photos and
    #                     low-resolution scans are enlarged to about that size
    #   table lines    -> erased, so table rows are not skipped
    # (We tested a "median" de-speckle filter too; on our sample scans it
    #  made digits WORSE, e.g. "86" read as "386", so it is not used.)
    image = ImageOps.autocontrast(image.convert("L"))
    if image.width < OCR_TARGET_WIDTH:
        factor = OCR_TARGET_WIDTH / image.width
        image = image.resize((OCR_TARGET_WIDTH, int(image.height * factor)), Image.LANCZOS)
    image = _remove_table_lines(image)

    # image_to_data returns every word Tesseract found, with its position
    # (block / paragraph / line number) and a confidence score.
    data = pytesseract.image_to_data(
        image,
        lang=config.OCR_LANGUAGE,
        output_type=pytesseract.Output.DICT,
    )

    # Rebuild readable text from the words, line by line.
    # The dictionary keeps insertion order, which is Tesseract's reading order.
    lines = {}           # (block, paragraph, line) -> [words]
    confidences = []
    for i, word in enumerate(data["text"]):
        if not word.strip():
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        lines.setdefault(key, []).append(word)
        conf = float(data["conf"][i])
        if conf >= 0:                 # -1 means "no confidence available"
            confidences.append(conf)

    # Join words into lines; put a blank line between paragraphs.
    output = []
    previous_paragraph = None
    for (block, paragraph, _line), words in lines.items():
        if previous_paragraph is not None and (block, paragraph) != previous_paragraph:
            output.append("")         # paragraph break
        output.append(" ".join(words))
        previous_paragraph = (block, paragraph)

    confidence = round(sum(confidences) / len(confidences), 1) if confidences else 0.0

    # A good average can hide a few badly read words (e.g. one smudged table
    # row). We count words Tesseract was unsure about so the page can warn.
    uncertain = sum(1 for c in confidences if c < LOW_CONFIDENCE)
    return {"text": "\n".join(output), "confidence": confidence, "uncertain_words": uncertain}


# ===========================================================================
# 2. Extracting text from each file type
# ===========================================================================
# Every extractor returns a list of "pages":
#     [{"page": 1, "text": "...", "method": "text" or "ocr",
#       "ocr_confidence": None or 0-100, "uncertain_words": 0}, ...]
# Keeping page numbers lets the assistant cite "page 2 of the report".

def _extract_pdf(path: Path) -> list:
    pages = []
    reader = pypdf.PdfReader(str(path))
    pdf_images = None    # opened only if we actually need OCR

    for index, page in enumerate(reader.pages):
        text = (page.extract_text() or "").strip()

        if len(text) >= config.MIN_TEXT_CHARS_PER_PAGE:
            # A normal "digital" page: its text can be read directly.
            pages.append({"page": index + 1, "text": text, "method": "text",
                          "ocr_confidence": None, "uncertain_words": 0})
            continue

        # Almost no text = probably a scanned page (just a picture of paper).
        # Render it to an image at OCR_DPI and read it with Tesseract.
        if pdf_images is None:
            pdf_images = pdfium.PdfDocument(str(path))
        scale = config.OCR_DPI / 72          # PDF units are 1/72 inch
        bitmap = pdf_images[index].render(scale=scale)
        result = ocr_image(bitmap.to_pil())
        pages.append({"page": index + 1, "text": result["text"], "method": "ocr",
                      "ocr_confidence": result["confidence"],
                      "uncertain_words": result["uncertain_words"]})

    if pdf_images is not None:
        pdf_images.close()
    return pages


def _extract_docx(path: Path) -> list:
    """
    Word files have no fixed pages, so the whole file is "page 1".
    Headings are marked with '#' (like Markdown) so the chunker can tell
    which section each piece of text came from.
    """
    document = Document(str(path))
    lines = []

    # iter_inner_content() gives paragraphs AND tables in the order they
    # appear in the document, so a table stays inside its own section.
    for block in document.iter_inner_content():
        if isinstance(block, Table):
            # Tables (common in SOPs and reports): one row per line,
            # cells separated by " | ".
            lines.append("")
            for row in block.rows:
                lines.append(" | ".join(cell.text.strip() for cell in row.cells))
            lines.append("")
            continue

        text = block.text.strip()
        if not text:
            continue
        style = (block.style.name or "").lower() if block.style else ""
        if style.startswith("heading") or style == "title":
            # Blank line + "#" marks a heading, like Markdown.
            lines.append("")
            lines.append(f"# {text}")
            lines.append("")
        else:
            lines.append(text)

    return [{"page": 1, "text": "\n".join(lines).strip(), "method": "text",
             "ocr_confidence": None, "uncertain_words": 0}]


def _extract_plain(path: Path) -> list:
    # errors="replace" means a strange character never crashes the reader.
    text = path.read_text(encoding="utf-8", errors="replace")
    return [{"page": 1, "text": text.strip(), "method": "text",
             "ocr_confidence": None, "uncertain_words": 0}]


def _extract_image(path: Path) -> list:
    with Image.open(path) as image:
        result = ocr_image(image)
    return [{"page": 1, "text": result["text"], "method": "ocr",
             "ocr_confidence": result["confidence"],
             "uncertain_words": result["uncertain_words"]}]


def extract(path: Path) -> list:
    """Pick the right extractor for the file type and return its pages."""
    extension = path.suffix.lower()
    if extension == ".pdf":
        return _extract_pdf(path)
    if extension == ".docx":
        return _extract_docx(path)
    if extension in {".txt", ".md"}:
        return _extract_plain(path)
    if extension in IMAGE_EXTENSIONS:
        return _extract_image(path)
    raise ValueError(f"Unsupported file type: {extension}")


# ===========================================================================
# 3. Chunking — cutting text into searchable pieces
# ===========================================================================
# Why chunk at all?
#   (a) The model can only read a limited amount of text at once.
#   (b) Search works better on focused pieces: one paragraph about "pump
#       vibration limits" matches a vibration question far better than a
#       whole 40-page manual would.

_HEADING = re.compile(r"^#{1,6}\s+(.*)")   # a Markdown heading like "## 5. Limits"


def _split_long(text: str) -> list:
    """Split one very long paragraph into overlapping windows."""
    size, overlap = config.CHUNK_SIZE_CHARS, config.CHUNK_OVERLAP_CHARS
    pieces = []
    start = 0
    while start < len(text):
        pieces.append(text[start:start + size])
        start += size - overlap
    return pieces


def chunk_pages(pages: list, source: str) -> list:
    """
    Turn a document's pages into chunks:
        [{"text", "source", "page", "section", "method"}, ...]

    Method: walk through the paragraphs, adding them to the current chunk
    until it would get too big, then start a new chunk that begins with the
    last CHUNK_OVERLAP_CHARS of the previous one (so context isn't lost at
    the boundary). We remember the latest heading as the chunk's "section",
    which makes citations more useful ("SOP-04, section 5").
    """
    chunks = []
    section = ""

    for page in pages:
        current = ""

        def flush():
            # Save the current chunk if it has real content.
            if current.strip():
                chunks.append({
                    "text": current.strip(),
                    "source": source,
                    "page": page["page"],
                    "section": section,
                    "method": page["method"],
                })

        # Paragraphs are separated by blank lines.
        for paragraph in re.split(r"\n\s*\n", page["text"]):
            paragraph = paragraph.strip()
            if not paragraph:
                continue

            heading = _HEADING.match(paragraph.splitlines()[0])
            if heading:
                # A new section starts: close the current chunk so a chunk
                # never mixes two sections, then remember the heading.
                flush()
                current = ""
                section = heading.group(1).strip()

            for piece in (_split_long(paragraph)
                          if len(paragraph) > config.CHUNK_SIZE_CHARS else [paragraph]):
                if len(current) + len(piece) + 2 > config.CHUNK_SIZE_CHARS and current:
                    flush()
                    current = current[-config.CHUNK_OVERLAP_CHARS:]   # the overlap
                current = f"{current}\n\n{piece}" if current else piece

        flush()

    return chunks
