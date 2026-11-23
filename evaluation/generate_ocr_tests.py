"""
evaluation/generate_ocr_tests.py — makes SYNTHETIC scanned pages for OCR testing (Phase 6.15).

WHY
We only have one real sample report (2 pages). To test OCR on hundreds of
pages, this script DRAWS inspection report pages with random values (so the
true text is known exactly), then damages each page the way real scans and
phone photos are damaged:
    rotation   page placed slightly crooked on the scanner (up to 3 degrees)
    blur       out-of-focus photo
    noise      grainy scan
    contrast   faded print / poor lighting
    low_res    low-resolution photo, scaled back up
    jpeg       heavy compression
    smudge     a grey blot over one value (like the smudged row in the sample)
Each page gets 0-3 of these, at a random strength. The page image and its
exact text (and the true value of every field) are saved together.

HOW TO RUN (from the project folder, with .venv active)
    python evaluation/generate_ocr_tests.py            (300 pages, the default)
    python evaluation/generate_ocr_tests.py 50         (fewer pages, quicker)
Deterministic: the same page number always gives the same page.
Writes: evaluation/ocr_synthetic/page_0001.jpg ... and truth.json
"""

import json
import random
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = Path(__file__).parent
OUT = HERE / "ocr_synthetic"

# A real TrueType font: Windows fonts first, then common Linux ones.
FONT_CANDIDATES = [r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\calibri.ttf", r"C:\Windows\Fonts\times.ttf",
                   r"C:\Windows\Fonts\cour.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                   "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"]
FONTS = [f for f in FONT_CANDIDATES if Path(f).exists()]

REMARKS = ["Coupling guard in place. No oil leak observed.", "Slight noise from the NDE bearing.",
           "Seal flush line found warm.", "Base plate bolts checked and tight.",
           "Oil level at the middle of the sight glass.", "Minor paint damage on the casing.",
           "Suction strainer cleaned during the last shift.", "No abnormal sound at the time of reading."]


def page_fields(rnd: random.Random) -> list:
    """The fields of one report: (label, value) in reading order."""
    v = lambda lo, hi: f"{rnd.uniform(lo, hi):.1f}"
    cost = rnd.randrange(50, 2500) * 1000
    lakh = f"{cost // 100000},{(cost // 1000) % 100:02d},000" if cost >= 100000 else f"{cost // 1000},000"
    return [
        ("Report No", f"IR-2026-{rnd.randrange(100, 999)}"),
        ("Date", f"{rnd.randrange(1, 29):02d}-{rnd.randrange(1, 13):02d}-2026"),
        ("Equipment", f"P-{rnd.randrange(1000, 9999)}{rnd.choice('ABC')}"),
        ("Vibration DE horizontal", f"{v(0.8, 9.5)} mm/s"),
        ("Vibration DE vertical", f"{v(0.8, 9.5)} mm/s"),
        ("Vibration DE axial", f"{v(0.5, 6.0)} mm/s"),
        ("Vibration NDE horizontal", f"{v(0.8, 9.5)} mm/s"),
        ("Vibration NDE vertical", f"{v(0.8, 9.5)} mm/s"),
        ("Vibration NDE axial", f"{v(0.5, 6.0)} mm/s"),
        ("Bearing temperature DE", f"{rnd.randrange(45, 98)} °C"),
        ("Bearing temperature NDE", f"{rnd.randrange(45, 98)} °C"),
        ("Ambient temperature", f"{rnd.randrange(22, 44)} °C"),
        ("Estimated cost", f"Rs {lakh}"),
        ("Remarks", rnd.choice(REMARKS)),
    ]


def draw_page(fields: list, rnd: random.Random):
    """A clean A4-like page at about 150 dpi, black text on white."""
    width, height = 1240, 1754
    page = Image.new("L", (width, height), 255)
    pen = ImageDraw.Draw(page)
    font_path = rnd.choice(FONTS) if FONTS else None
    size = rnd.randrange(26, 34)
    font = ImageFont.truetype(font_path, size) if font_path else ImageFont.load_default()
    title = ImageFont.truetype(font_path, size + 10) if font_path else font
    y = 120
    lines = ["SAMPLE REFINERY LTD. - INSPECTION REPORT"]
    pen.text((100, y), lines[0], fill=0, font=title)
    y += int(size * 3)
    boxes = {}
    for label, value in fields:
        line = f"{label}: {value}"
        pen.text((100, y), line, fill=0, font=font)
        box = pen.textbbox((100, y), line, font=font)
        boxes[label] = box
        lines.append(line)
        y += int(size * 1.9)
    return page, lines, boxes, Path(font_path).name if font_path else "default"


DAMAGE = ["rotation", "blur", "noise", "contrast", "low_res", "jpeg", "smudge"]


def damage(page, boxes: dict, rnd: random.Random):
    """Apply 0-3 kinds of damage, each with a strength from 1 (light) to 3 (heavy)."""
    applied = {}
    for kind in rnd.sample(DAMAGE, rnd.choice([0, 1, 1, 2, 2, 3])):
        strength = rnd.choice([1, 2, 3])
        applied[kind] = strength
        if kind == "rotation":
            page = page.rotate(rnd.choice([-1, 1]) * strength, expand=False, fillcolor=255, resample=Image.BICUBIC)
        elif kind == "blur":
            page = page.filter(ImageFilter.GaussianBlur(0.6 * strength))
        elif kind == "noise":
            noise = Image.effect_noise(page.size, 18 * strength)
            page = Image.blend(page, noise, 0.12 * strength)
        elif kind == "contrast":
            low = 70 * strength                     # text no longer pure black
            page = page.point(lambda p: low + p * (255 - low) // 255)
        elif kind == "low_res":
            factor = [0.6, 0.45, 0.35][strength - 1]
            small = page.resize((int(page.width * factor), int(page.height * factor)), Image.BILINEAR)
            page = small.resize(page.size, Image.BILINEAR)
        elif kind == "smudge":
            label = rnd.choice([k for k in boxes if k.startswith(("Vibration", "Bearing"))])
            x0, y0, x1, y1 = boxes[label]
            blot = ImageDraw.Draw(page)
            grey = [170, 140, 110][strength - 1]
            blot.ellipse((x1 - 120, y0 - 6, x1 + 10, y1 + 6), fill=grey)
            applied["smudged_field"] = label
    return page, applied


def main(pages: int = 300):
    if not FONTS:
        print("Warning: no TrueType font found; using a small built-in font.")
    OUT.mkdir(parents=True, exist_ok=True)
    truth = []
    for n in range(1, pages + 1):
        rnd = random.Random(f"ocr-page-{n}")
        fields = page_fields(rnd)
        page, lines, boxes, font = draw_page(fields, rnd)
        page, applied = damage(page, boxes, rnd)
        name = f"page_{n:04d}.jpg"
        quality = [70, 35, 15][applied["jpeg"] - 1] if "jpeg" in applied else 92
        page.convert("L").save(OUT / name, quality=quality)
        truth.append({"file": name, "text": "\n".join(lines), "fields": dict(fields),
                      "damage": applied, "font": font})
    (OUT / "truth.json").write_text(json.dumps(truth, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"{pages} pages written to {OUT}")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 300)
