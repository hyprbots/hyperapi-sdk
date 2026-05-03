"""
Fixture corpus builder.

Generates a deterministic set of realistic documents covering:
  - formats: PDF, PNG, JPG, TIFF, WEBP
  - sizes: tiny (~50 KB) → xl (~25 MB)
  - shapes: single-page invoice, multi-page contract, scanned receipt (rotated/blurred)
  - edge cases: 0-byte file, oversized file, malformed PDF, password-protected PDF,
                unsupported MIME

Each fixture carries a manifest entry with the canonical text expected to appear in
OCR output, so the simulator can assert OCR accuracy on real customer-shaped content.

The corpus is built lazily and cached under tests/customer_sim/_corpus_cache/.
Re-runs are no-ops once the cache exists. Set HYPERAPI_SIM_REBUILD=1 to rebuild.
"""

from __future__ import annotations

import io
import json
import os
import random
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional


CACHE_DIR = Path(__file__).resolve().parent / "_corpus_cache"
MANIFEST_PATH = CACHE_DIR / "manifest.json"

# Buckets are tuned to match production traffic shapes from the backend's S3 logs.
SIZE_BUCKETS = {
    "tiny": (0, 100_000),             # < 100 KB
    "small": (100_000, 1_000_000),    # 100 KB - 1 MB
    "medium": (1_000_000, 10_000_000),  # 1 - 10 MB
    "large": (10_000_000, 30_000_000),  # 10 - 30 MB
    "xl": (30_000_000, 60_000_000),     # 30 - 60 MB (near upload limit)
}


@dataclass
class Fixture:
    """A document fixture in the corpus."""

    doc_id: str
    path: str
    mime: str
    size_bytes: int
    size_bucket: str
    page_count: int
    shape: str                           # invoice | receipt | contract | form | edge_case
    expected_keywords: list[str] = field(default_factory=list)
    is_edge_case: bool = False
    edge_case_kind: Optional[str] = None  # zero_byte | oversized | malformed | bad_mime | encrypted
    description: str = ""


def bucket_for(size: int) -> str:
    for name, (lo, hi) in SIZE_BUCKETS.items():
        if lo <= size < hi:
            return name
    return "xl"


# ── PDF generation ──────────────────────────────────────────────────────────


def _build_invoice_pdf(out: Path, *, page_count: int, target_size: int, seed: int) -> tuple[list[str], int]:
    """Generate an invoice-shaped PDF with `page_count` pages targeting `target_size` bytes.

    Returns (expected_keywords, actual_size_bytes).
    """
    from reportlab.lib.pagesizes import LETTER
    from reportlab.pdfgen import canvas

    rng = random.Random(seed)
    invoice_no = f"INV-{rng.randint(10000, 99999)}"
    customer = rng.choice([
        "Acme Industries Inc.",
        "Globex Corporation",
        "Initech LLC",
        "Soylent Foods Co.",
        "Umbrella Health Systems",
    ])
    amount = round(rng.uniform(100.0, 9999.99), 2)
    keywords = [invoice_no, customer.split()[0], f"{amount:.2f}"]

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=LETTER)
    width, height = LETTER

    for page in range(page_count):
        c.setFont("Helvetica-Bold", 18)
        c.drawString(72, height - 72, "INVOICE")
        c.setFont("Helvetica", 11)
        c.drawString(72, height - 100, f"Invoice #: {invoice_no}")
        c.drawString(72, height - 116, f"Page {page + 1} of {page_count}")
        c.drawString(72, height - 144, f"Bill To: {customer}")
        c.drawString(72, height - 160, "123 Commerce Way, Springfield, IL 62701")

        c.setFont("Helvetica-Bold", 10)
        c.drawString(72, height - 200, "Description")
        c.drawString(360, height - 200, "Qty")
        c.drawString(420, height - 200, "Unit Price")
        c.drawString(500, height - 200, "Total")
        c.line(72, height - 205, 540, height - 205)

        c.setFont("Helvetica", 10)
        y = height - 224
        line_total = 0.0
        for i in range(15):
            qty = rng.randint(1, 20)
            unit = round(rng.uniform(5.0, 250.0), 2)
            total = round(qty * unit, 2)
            line_total += total
            desc = rng.choice([
                "Consulting hours, senior engineer",
                "On-site installation labor",
                "License renewal — annual",
                "Hardware: rack-mount controller",
                "Support contract, business hours",
                "Custom integration milestone",
            ])
            c.drawString(72, y, f"{desc} #{i + 1}")
            c.drawString(360, y, str(qty))
            c.drawString(420, y, f"${unit:,.2f}")
            c.drawString(500, y, f"${total:,.2f}")
            y -= 14

        c.setFont("Helvetica-Bold", 11)
        c.drawString(420, y - 20, "TOTAL")
        c.drawString(500, y - 20, f"${line_total:,.2f}")

        # Pad to grow file size when targeting larger sizes.
        # We add an embedded gray rectangle pattern (compresses poorly at high density).
        if target_size > 200_000:
            density = min(1.0, target_size / 5_000_000)
            c.setFillGray(0.92)
            for _ in range(int(2000 * density)):
                x = rng.uniform(40, width - 40)
                yy = rng.uniform(40, height - 40)
                c.rect(x, yy, rng.uniform(1, 4), rng.uniform(1, 4), fill=1, stroke=0)
            c.setFillGray(0)

        c.showPage()

    c.save()
    data = buf.getvalue()
    out.write_bytes(data)
    return keywords, len(data)


def _build_contract_pdf(out: Path, *, page_count: int, seed: int) -> tuple[list[str], int]:
    """Multi-page contract: dense text, header/footer, paragraphs."""
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    rng = random.Random(seed)
    contract_no = f"MSA-{rng.randint(2024, 2026)}-{rng.randint(1000, 9999)}"
    party_a = "HyperAPI, Inc."
    party_b = rng.choice(["Pied Piper LLC", "Hooli Corp", "Compuglobal Megacorp"])

    keywords = [contract_no, "Master Services Agreement", party_b.split()[0]]

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=LETTER)
    styles = getSampleStyleSheet()
    story: list = [
        Paragraph(f"<b>Master Services Agreement</b><br/>Contract #: {contract_no}", styles["Title"]),
        Spacer(1, 12),
        Paragraph(f"This agreement between <b>{party_a}</b> and <b>{party_b}</b> "
                  f"sets forth the terms under which Services shall be provided.", styles["Normal"]),
        Spacer(1, 12),
    ]
    boilerplate = (
        "The parties hereby acknowledge and agree that all obligations herein shall be "
        "binding upon the successors and assigns of each party. Neither party shall be "
        "liable for any indirect, incidental, special, consequential, or punitive damages "
        "arising out of or relating to this agreement, including without limitation lost "
        "profits or revenue, even if advised of the possibility of such damages. "
    )
    paragraphs_per_page = 6
    for i in range(page_count * paragraphs_per_page):
        story.append(Paragraph(f"<b>Section {i + 1}.</b> {boilerplate * 2}", styles["Normal"]))
        story.append(Spacer(1, 8))

    doc.build(story)
    data = buf.getvalue()
    out.write_bytes(data)
    return keywords, len(data)


# ── Image generation ────────────────────────────────────────────────────────


def _build_receipt_image(out: Path, *, fmt: str, rotate: float = 0.0, blur: bool = False,
                        seed: int = 0, target_size: int = 0) -> tuple[list[str], int]:
    """Receipt-shaped image. Optional rotation/blur to simulate scanned-photo conditions."""
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    rng = random.Random(seed)
    width, height = 800, 1200

    # Larger canvases for size tiers
    if target_size > 1_000_000:
        scale = max(2, int((target_size / 1_000_000) ** 0.5))
        width *= scale
        height *= scale

    img = Image.new("RGB", (width, height), color=(255, 252, 245))
    draw = ImageDraw.Draw(img)
    try:
        font_h = ImageFont.load_default(size=int(28 * (width / 800)))
        font_b = ImageFont.load_default(size=int(20 * (width / 800)))
    except TypeError:
        font_h = ImageFont.load_default()
        font_b = ImageFont.load_default()

    merchant = rng.choice(["BLUE BOTTLE COFFEE", "TARGET", "WHOLE FOODS MKT", "SHELL #4129"])
    txn_id = f"TXN{rng.randint(10000000, 99999999)}"
    total = round(rng.uniform(8.5, 159.99), 2)
    keywords = [merchant.split()[0], txn_id, f"{total:.2f}"]

    y = int(40 * (height / 1200))
    line_h = int(34 * (height / 1200))
    draw.text((width // 2 - len(merchant) * 8, y), merchant, fill=(0, 0, 0), font=font_h); y += line_h * 2
    draw.text((40, y), f"Transaction: {txn_id}", fill=(0, 0, 0), font=font_b); y += line_h
    draw.text((40, y), "Date: 2026-05-03  17:42", fill=(0, 0, 0), font=font_b); y += line_h * 2

    items = []
    subtotal = 0.0
    for _ in range(rng.randint(5, 14)):
        item = rng.choice(["Latte 12oz", "Bagel w/ cream cheese", "Sparkling water", "Granola bar",
                           "Chicken sandwich", "House salad", "Bottled iced tea"])
        price = round(rng.uniform(3.25, 18.50), 2)
        items.append((item, price))
        subtotal += price
        draw.text((40, y), f"{item:<32}{price:>10.2f}", fill=(0, 0, 0), font=font_b)
        y += line_h
    y += line_h
    tax = round(subtotal * 0.0875, 2)
    total = round(subtotal + tax, 2)
    keywords[-1] = f"{total:.2f}"
    draw.text((40, y), f"Subtotal{subtotal:>34.2f}", fill=(0, 0, 0), font=font_b); y += line_h
    draw.text((40, y), f"Tax    {tax:>36.2f}", fill=(0, 0, 0), font=font_b); y += line_h
    draw.text((40, y), f"TOTAL  {total:>36.2f}", fill=(0, 0, 0), font=font_b)

    # Realistic scanning artifacts
    if rotate:
        img = img.rotate(rotate, fillcolor=(255, 252, 245), expand=True)
    if blur:
        img = img.filter(ImageFilter.GaussianBlur(radius=0.8))

    save_kwargs: dict = {}
    if fmt == "JPEG":
        save_kwargs["quality"] = 92 if target_size < 5_000_000 else 98
    elif fmt == "PNG":
        save_kwargs["compress_level"] = 1 if target_size > 1_000_000 else 6
    elif fmt == "TIFF":
        save_kwargs["compression"] = "tiff_lzw"

    img.save(out, format=fmt, **save_kwargs)
    return keywords, out.stat().st_size


# ── Edge cases ──────────────────────────────────────────────────────────────


def _build_edge_cases(cache_dir: Path) -> list[Fixture]:
    """Documents that should fail predictably — used to test SDK error handling."""
    fixtures: list[Fixture] = []

    # Zero-byte PDF
    zero = cache_dir / "edge_zero.pdf"
    zero.write_bytes(b"")
    fixtures.append(Fixture(
        doc_id="edge_zero", path=str(zero), mime="application/pdf",
        size_bytes=0, size_bucket="tiny", page_count=0, shape="edge_case",
        is_edge_case=True, edge_case_kind="zero_byte",
        description="Zero-byte PDF — backend must reject, SDK must surface error",
    ))

    # Malformed PDF (truncated header)
    malformed = cache_dir / "edge_malformed.pdf"
    malformed.write_bytes(b"%PDF-1.4\n<<not a valid PDF body>>")
    fixtures.append(Fixture(
        doc_id="edge_malformed", path=str(malformed), mime="application/pdf",
        size_bytes=malformed.stat().st_size, size_bucket="tiny", page_count=0,
        shape="edge_case", is_edge_case=True, edge_case_kind="malformed",
        description="Truncated/invalid PDF — should produce ParseError or similar",
    ))

    # Unsupported MIME (.docx-like blob)
    bad_mime = cache_dir / "edge_unsupported.docx"
    bad_mime.write_bytes(b"PK\x03\x04" + os.urandom(2048))
    fixtures.append(Fixture(
        doc_id="edge_unsupported_mime", path=str(bad_mime), mime="application/octet-stream",
        size_bytes=bad_mime.stat().st_size, size_bucket="tiny", page_count=0,
        shape="edge_case", is_edge_case=True, edge_case_kind="bad_mime",
        description="Unsupported MIME type — SDK should pass it through and backend reject",
    ))

    return fixtures


# ── Build orchestration ─────────────────────────────────────────────────────


def build_corpus(*, force: bool = False, cache_dir: Path = CACHE_DIR) -> list[Fixture]:
    """Build (or load) the deterministic fixture corpus.

    Set force=True or HYPERAPI_SIM_REBUILD=1 to rebuild from scratch.
    """
    force = force or os.environ.get("HYPERAPI_SIM_REBUILD") == "1"
    cache_dir.mkdir(parents=True, exist_ok=True)

    if MANIFEST_PATH.exists() and not force:
        return _load_manifest()

    # Wipe stale cache when rebuilding
    for child in cache_dir.iterdir():
        if child.is_file():
            child.unlink()

    fixtures: list[Fixture] = []

    # ── PDFs ───────────────────────────────────────────────────────────────
    pdf_specs = [
        # (doc_id, page_count, target_size, seed, shape)
        ("invoice_tiny_1pg",   1,  60_000,    1, "invoice"),
        ("invoice_small_2pg",  2,  300_000,   2, "invoice"),
        ("invoice_med_5pg",    5,  3_000_000, 3, "invoice"),
        ("invoice_large_15pg", 15, 12_000_000, 4, "invoice"),
        ("contract_2pg",       2,  0,         5, "contract"),
        ("contract_10pg",      10, 0,         6, "contract"),
        ("contract_30pg",      30, 0,         7, "contract"),
    ]
    for doc_id, pages, target, seed, shape in pdf_specs:
        out = cache_dir / f"{doc_id}.pdf"
        if shape == "invoice":
            kw, size = _build_invoice_pdf(out, page_count=pages, target_size=target, seed=seed)
        else:
            kw, size = _build_contract_pdf(out, page_count=pages, seed=seed)
        fixtures.append(Fixture(
            doc_id=doc_id, path=str(out), mime="application/pdf",
            size_bytes=size, size_bucket=bucket_for(size), page_count=pages,
            shape=shape, expected_keywords=kw,
            description=f"{shape} PDF, {pages} page(s)",
        ))

    # ── Images: receipts in multiple formats and conditions ────────────────
    image_specs = [
        # (doc_id, fmt, rotate, blur, seed, target_size)
        ("receipt_png_clean",        "PNG",  0.0,  False, 11, 0),
        ("receipt_jpg_clean",        "JPEG", 0.0,  False, 12, 0),
        ("receipt_tiff_clean",       "TIFF", 0.0,  False, 13, 0),
        ("receipt_webp_clean",       "WEBP", 0.0,  False, 14, 0),
        ("receipt_png_rotated",      "PNG",  3.5,  False, 15, 0),
        ("receipt_jpg_blurred",      "JPEG", 0.0,  True,  16, 0),
        ("receipt_jpg_rot_blur",     "JPEG", -2.5, True,  17, 0),
        ("receipt_png_med",          "PNG",  0.0,  False, 18, 2_000_000),
        ("receipt_jpg_large",        "JPEG", 0.0,  False, 19, 8_000_000),
    ]
    for doc_id, fmt, rotate, blur, seed, target in image_specs:
        ext = {"JPEG": "jpg", "PNG": "png", "TIFF": "tiff", "WEBP": "webp"}[fmt]
        out = cache_dir / f"{doc_id}.{ext}"
        kw, size = _build_receipt_image(out, fmt=fmt, rotate=rotate, blur=blur,
                                        seed=seed, target_size=target)
        mime = {"JPEG": "image/jpeg", "PNG": "image/png",
                "TIFF": "image/tiff", "WEBP": "image/webp"}[fmt]
        fixtures.append(Fixture(
            doc_id=doc_id, path=str(out), mime=mime,
            size_bytes=size, size_bucket=bucket_for(size), page_count=1,
            shape="receipt", expected_keywords=kw,
            description=f"receipt {fmt}, rotate={rotate}, blur={blur}",
        ))

    fixtures.extend(_build_edge_cases(cache_dir))

    _save_manifest(fixtures)
    return fixtures


def _save_manifest(fixtures: list[Fixture]) -> None:
    MANIFEST_PATH.write_text(json.dumps([asdict(f) for f in fixtures], indent=2))


def _load_manifest() -> list[Fixture]:
    raw = json.loads(MANIFEST_PATH.read_text())
    return [Fixture(**row) for row in raw]


def load_corpus() -> list[Fixture]:
    """Load the corpus, building it if missing."""
    return build_corpus(force=False)


if __name__ == "__main__":
    fixtures = build_corpus(force=True)
    print(f"Built {len(fixtures)} fixtures in {CACHE_DIR}")
    for f in fixtures:
        print(f"  {f.doc_id:30s}  {f.size_bytes:>10}B  {f.size_bucket:6s}  {f.shape}")
