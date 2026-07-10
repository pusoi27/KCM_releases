"""Capture raw scanner input from HID devices or serial ports."""
import sys
import time
import re

print("=" * 70)
print("RAW SCANNER CAPTURE - HID/SERIAL DIAGNOSTIC")
print("=" * 70)
print()

# Try to detect scanner hardware
scanner_found = False

# Try HID first
try:
    import hid
    devices = hid.enumerate()
    scanner_devices = [d for d in devices if 'scanner' in d.get('product_string', '').lower() 
                       or 'barcode' in d.get('product_string', '').lower()
                       or d.get('usage_page') == 0x01 and d.get('usage') == 0x06]  # Keyboard
    
    if scanner_devices:
        print(f"Found {len(scanner_devices)} potential scanner device(s):")
        for i, dev in enumerate(scanner_devices):
            print(f"  {i}: {dev.get('product_string', 'Unknown')} ({dev['vendor_id']:04x}:{dev['product_id']:04x})")
        
        # Try first one
        if scanner_devices:
            dev = scanner_devices[0]
            h = hid.device()
            h.open(dev['vendor_id'], dev['product_id'])
            print(f"\nOpened HID device: {dev.get('product_string', 'Unknown')}")
            print("Scanning... (press Ctrl+C to stop)\n")
            
            scans = []
            scan_data = ""
            try:
                while True:
                    report = h.read(64, timeout_ms=100)
                    if report:
                        # HID keyboard reports have key codes
                        # Try to decode if it's keyboard
                        # This is complex, so just show raw data
                        print(f"HID Report: {report}")
            except KeyboardInterrupt:
                h.close()
                scanner_found = True
except ImportError:
    print("(hidapi not available - install with: pip install hidapi)")
except Exception as e:
    print(f"HID detection failed: {e}")

# Fallback to stdin capture
if not scanner_found:
    print("\n" + "=" * 70)
    print("FALLBACK: Capturing from stdin")
    print("=" * 70)
    print()
    print("Make sure scanner is in HID (keyboard) mode.")
    print("Click in this window and scan QR codes.")
    print("Press Ctrl+C to exit.\n")
    
    scans = []
    try:
        while True:
            try:
                data = input(">>> ")
                if data.strip():
                    timestamp = time.strftime("%H:%M:%S")
                    length = len(data)
                    print(f"    [{timestamp}] {length} chars: {repr(data[:80])}")
                    print()
                    scans.append({"time": timestamp, "data": data, "len": length})
            except EOFError:
                break
    except KeyboardInterrupt:
        pass
    
    if scans:
        print()
        print("=" * 70)
        print(f"CAPTURED {len(scans)} SCAN(S)")
        print("=" * 70)
        for i, scan in enumerate(scans, 1):
            print(f"\nScan #{i}:")
            print(f"  Time: {scan['time']}")
            print(f"  Length: {scan['len']} chars")
            print(f"  Raw: {repr(scan['data'][:100])}")
            
            # Parse
            lines = scan['data'].split('\n')
            if len(lines) > 1:
                print(f"  Lines ({len(lines)}):")
                for j, line in enumerate(lines, 1):
                    print(f"    {j}: {repr(line)}")
            
            # Extract ID
            match = re.search(r'ID[:\.\s]*(\d+)', scan['data'], re.IGNORECASE)
            if match:
                id_extracted = match.group(1)
                print(f"  Extracted ID: {id_extracted}")
            
            # Extract UID
            uid_match = re.search(r'UID[:\.\s]*([A-Z0-9\-]+)', scan['data'], re.IGNORECASE)
            if uid_match:
                uid_extracted = uid_match.group(1)
                print(f"  Extracted UID: {uid_extracted}")

print()
print("Done.")
