"""
app/vision.py — answering questions about images (Phase 5).

The problem statement asks the workbench to handle "scanned PDFs, handwritten
notes, engineering drawings, photographs, read through on-device OCR and
vision models", and the demo must include "a multimodal task involving image
or scanned document understanding".

Phase 2 already READS TEXT out of images with OCR. A vision model adds
UNDERSTANDING: what equipment is in a photo, how a drawing is laid out,
whether something looks damaged — things OCR can't do.

How a vision question is answered:

    attached image / PDF ──► prepare: shrink to max 1024 px, convert to JPEG,
                             encode as text (base64) — the format Ollama wants
                         ──► send to the vision model TOGETHER WITH the OCR text
                             of the same file
                         ──► stream the answer back, with the OCR text listed
                             as a source

Why send OCR text too? Small vision models are good at layout and objects
but weak at reading small print. OCR is the opposite. Together they cover
each other, and the instructions tell the model to say when they disagree.

Why no agent loop here? Small vision models can't use tools reliably, so
vision questions are answered in one step. (Future work: give the agent a
"look at image" tool backed by a larger vision model.)

Everything runs locally: images never leave the computer.
"""

import base64
import io
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image

import config
from app import documents, knowledge, llm


# ---------------------------------------------------------------------------
# 1. Preparing images for the model
# ---------------------------------------------------------------------------
def _to_base64_jpeg(image: Image.Image) -> str:
    """
    Shrink an image (keeping its proportions), save it as JPEG in memory,
    and return it as base64 text.

    base64 = a way of writing binary data (like a photo) using only normal
    letters and digits, so it can travel inside a JSON request to Ollama.
    """
    image = image.convert("RGB")             # JPEG has no transparency
    image.thumbnail((config.VISION_MAX_IMAGE_SIDE, config.VISION_MAX_IMAGE_SIDE))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=85)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def images_for_attachment(doc_id: str) -> list:
    """
    The images of one attached document, ready for the model:
        [{"label": "photo.jpg", "base64": "..."}, ...]
    Image files give one image; PDFs give their first pages, rendered.
    """
    saved = knowledge.attachment_document(doc_id)
    name = saved["summary"]["filename"]
    folder = config.UPLOADS_DIR / doc_id
    extension = Path(name).suffix.lower()
    original = folder / f"original{extension}"
    if not original.exists():
        return []

    if extension in documents.IMAGE_EXTENSIONS:
        with Image.open(original) as image:
            return [{"label": name, "base64": _to_base64_jpeg(image)}]

    if extension == ".pdf":
        pages = []
        pdf = pdfium.PdfDocument(str(original))
        try:
            for index in range(min(len(pdf), config.VISION_MAX_PDF_PAGES)):
                # 150 DPI is plenty: the image is shrunk to 1024 px anyway.
                bitmap = pdf[index].render(scale=150 / 72)
                pages.append({"label": f"{name}, page {index + 1}",
                              "base64": _to_base64_jpeg(bitmap.to_pil())})
        finally:
            pdf.close()
        return pages

    return []        # Word or text files have no images to look at


# ---------------------------------------------------------------------------
# 2. Answering
# ---------------------------------------------------------------------------
async def answer(messages: list, attachment_ids: list, role: str = "vision"):
    """
    Answer the last question using the attached images. Yields the same
    kind of events as the agent: status, sources, token, done.
    """
    question = messages[-1]["content"]

    yield {"type": "status", "text": "Preparing images…"}
    images, ocr_sources = [], []
    for doc_id in attachment_ids:
        try:
            images += images_for_attachment(doc_id)
            saved = knowledge.attachment_document(doc_id)
        except Exception:
            continue
        name = saved["summary"]["filename"]
        for page in saved["pages"]:
            if page["text"].strip():
                ocr_sources.append({"text": page["text"], "source": name, "page": page["page"],
                                    "section": "(OCR text)" if page["method"] == "ocr" else "",
                                    "method": page["method"], "score": None})

    if not images:
        yield {"type": "token", "text": "There is no image or PDF attached for me to look at. "
                                        "Attach one with 📎 and ask again."}
        yield {"type": "done", "files": []}
        return

    if ocr_sources:
        yield {"type": "sources", "sources": [
            {"n": i + 1, "source": s["source"], "page": s["page"], "section": s["section"],
             "score": None, "method": s["method"], "text": s["text"]}
            for i, s in enumerate(ocr_sources)]}

    # The question, the list of images, and the OCR text, in one message.
    labels = "\n".join(f"- Image {i + 1}: {img['label']}" for i, img in enumerate(images))
    content = f"{question}\n\nImages attached:\n{labels}"
    if ocr_sources:
        content += ("\n\nText that OCR read from these files (may contain errors; words marked "
                    "[unclear: ...] were hard to read):\n=== OCR TEXT ===\n"
                    + knowledge.format_sources(ocr_sources)
                    + "\n=== END OF OCR TEXT ===\nCite the OCR text as [1], [2] when you use it.")

    conversation = messages[:-1] + [{"role": "user", "content": content,
                                     "images": [img["base64"] for img in images]}]

    yield {"type": "status", "text": f"Looking at {len(images)} image(s)… "
                                     "(the first time, loading the vision model takes a while)"}
    async for piece in llm.chat_stream(conversation, role=role,
                                       system_prompt=config.VISION_SYSTEM_PROMPT):
        yield {"type": "token", "text": piece}
    yield {"type": "done", "files": []}
