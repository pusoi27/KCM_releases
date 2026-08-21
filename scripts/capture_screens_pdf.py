"""Capture screenshots of every Stdytime page and assemble them into a labeled PDF.

Requires the app to already be running (e.g. via the "Run app to verify
availability rules" task) and the `playwright` package with its Chromium
browser installed (`python -m playwright install chromium`).

Usage:
    python scripts/capture_screens_pdf.py --email you@example.com
    python scripts/capture_screens_pdf.py --base-url http://127.0.0.1:5000 --output exports/app_screens.pdf
"""
import argparse
import os
import sys
import tempfile

from playwright.sync_api import sync_playwright
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas

# (path, label) - GET-only pages that render full HTML views without requiring
# a specific record id or prior form submission. Dynamic-id pages, POST-only
# actions, binary/PDF endpoints and pure JSON APIs are intentionally excluded.
PAGES = [
    ("/", "Dashboard - Active Class"),
    ("/instructor/home", "Instructor Station Home"),
    ("/checkin/home", "Check-in Station Home"),
    ("/students", "Student Database"),
    ("/students/add", "Add Student"),
    ("/students/duplicates", "Student Duplicates"),
    ("/assistants", "Staff / Assistants"),
    ("/assistants/add", "Add Staff Member"),
    ("/schedule/assistants", "Staff Schedule"),
    ("/books", "Books Catalog"),
    ("/books/add", "Add Book"),
    ("/materials", "Materials / Devices"),
    ("/materials/add", "Add Material"),
    ("/qr/generate", "QR Code Generator"),
    ("/qr/generate_page", "QR Generate Page"),
    ("/qr/print_page", "QR Print Page"),
    ("/reports/assistants", "Staff Report"),
    ("/reports/class-attendance", "Class Attendance Report"),
    ("/reports/student-attendance", "Student Attendance Report"),
    ("/reports/loaned-books", "Loaned Books Report"),
    ("/reports/loans", "Loans Report"),
    ("/instructor/profile", "Instructor Profile"),
    ("/instructor/profile/edit", "Edit Instructor Profile"),
    ("/instructor/calendar", "Instructor Calendar"),
    ("/utilities/cancellation-notice", "Cancellation Notice"),
    ("/setup", "Setup"),
    ("/setup/storage", "Storage Setup"),
]

VIEWPORT = {"width": 1600, "height": 1000}


def _login_if_needed(page, base_url: str, email: str) -> None:
    """Fill the one-time email login form if the app redirects to it."""
    page.goto(base_url + "/", wait_until="networkidle")
    if "/login/email" not in page.url:
        return
    if not email:
        raise SystemExit(
            "App is showing the email login screen; pass --email you@example.com to sign in."
        )
    page.fill("#email", email)
    page.click("button[type=submit]")
    page.wait_for_load_state("networkidle")


def capture_pages(base_url: str, email: str, shots_dir: str) -> list[tuple[str, str]]:
    """Visit each page and save a full-page screenshot. Returns (label, image_path) pairs."""
    captured = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=VIEWPORT)
        _login_if_needed(page, base_url, email)

        for path, label in PAGES:
            url = base_url + path
            try:
                page.goto(url, wait_until="networkidle", timeout=15000)
            except Exception as exc:
                print(f"[capture] SKIP {path}: navigation failed ({exc})", file=sys.stderr)
                continue
            image_path = os.path.join(shots_dir, f"{path.strip('/').replace('/', '_') or 'home'}.png")
            page.screenshot(path=image_path, full_page=True)
            captured.append((label, image_path))
            print(f"[capture] OK {path} -> {image_path}")

        browser.close()
    return captured


def build_pdf(shots: list[tuple[str, str]], output_path: str) -> None:
    """Assemble screenshots into a single PDF, one page per screenshot with a label header."""
    page_width, page_height = letter
    margin = 0.4 * inch
    label_height = 0.35 * inch

    c = canvas.Canvas(output_path, pagesize=letter)
    for label, image_path in shots:
        c.setFont("Helvetica-Bold", 14)
        c.drawString(margin, page_height - margin - 10, label)

        max_w = page_width - 2 * margin
        max_h = page_height - 2 * margin - label_height
        from PIL import Image

        with Image.open(image_path) as img:
            img_w, img_h = img.size
        scale = min(max_w / img_w, max_h / img_h)
        draw_w, draw_h = img_w * scale, img_h * scale
        x = (page_width - draw_w) / 2
        y = page_height - margin - label_height - draw_h

        c.drawImage(image_path, x, y, width=draw_w, height=draw_h, preserveAspectRatio=True)
        c.showPage()
    c.save()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:5000")
    parser.add_argument("--email", default=os.getenv("STDYTIME_CAPTURE_EMAIL", ""))
    parser.add_argument("--output", default="exports/app_screens.pdf")
    args = parser.parse_args()

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)

    with tempfile.TemporaryDirectory() as shots_dir:
        shots = capture_pages(args.base_url, args.email, shots_dir)
        if not shots:
            raise SystemExit("No screens were captured; aborting PDF generation.")
        build_pdf(shots, args.output)

    print(f"\nSaved {len(shots)} labeled screens to {args.output}")


if __name__ == "__main__":
    main()
