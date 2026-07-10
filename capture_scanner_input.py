"""Capture raw scanner input directly at the terminal."""
import sys
import time

print("=" * 60)
print("SCANNER INPUT CAPTURE TOOL")
print("=" * 60)
print()
print("This tool will capture raw scanner input directly.")
print("Place scanner in this window (click in terminal) and scan QR codes.")
print()
print("Press Ctrl+C to exit.")
print()
print("-" * 60)

scans = []
try:
    while True:
        try:
            data = input(">>> ")
            if data.strip():
                timestamp = time.strftime("%H:%M:%S")
                length = len(data)
                print(f"    [{timestamp}] Scanned {length} chars: {repr(data)}")
                print()
                scans.append({"time": timestamp, "data": data, "len": length})
        except EOFError:
            break
except KeyboardInterrupt:
    print()
    print()
    print("-" * 60)
    print(f"CAPTURE COMPLETE - {len(scans)} scans captured")
    print("-" * 60)
    for i, scan in enumerate(scans, 1):
        print(f"\nScan #{i} ({scan['time']}): {scan['len']} chars")
        print(f"  Raw: {repr(scan['data'])}")
        
        # Parse the data
        lines = scan['data'].split('\n')
        if len(lines) > 1:
            print(f"  Lines ({len(lines)}):")
            for j, line in enumerate(lines, 1):
                print(f"    {j}: {repr(line)}")
        
        # Try to extract ID
        import re
        match = re.search(r'ID[:\.\s]*(\d+)', scan['data'], re.IGNORECASE)
        if match:
            extracted_id = match.group(1)
            print(f"  Extracted ID: {extracted_id}")
