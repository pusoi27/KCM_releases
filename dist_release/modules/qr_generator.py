#*****************************
#qr_gererator.py   ver 04--
#*****************************

import qrcode, os
from io import BytesIO


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QR_CODES_DIR = os.path.join(PROJECT_ROOT, "assets", "qr_codes")

def generate_qr_bytes(data):
    """Generate QR code and return as bytes (PNG format) for database storage."""
    img = qrcode.make(data)
    bytes_io = BytesIO()
    img.save(bytes_io, format='PNG')
    return bytes_io.getvalue()

def generate_qr(data, name):
    """Legacy function: Generate QR code and save to file (keeps backward compatibility)."""
    os.makedirs(QR_CODES_DIR, exist_ok=True)
    img = qrcode.make(data)
    path = os.path.join(QR_CODES_DIR, f"{name}.png")
    img.save(path)
    return path