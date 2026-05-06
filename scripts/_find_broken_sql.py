import re
import os

BASE = r'c:\Users\octav\AppData\Local\Programs\Python\Python312\stdytime'
skip_dirs = {'.venv', '__pycache__', '.git', 'data', 'uploads', 'exports', 'assets', 'scripts'}

for root, dirs, files in os.walk(BASE):
    dirs[:] = [d for d in dirs if d not in skip_dirs]
    for fname in files:
        if not fname.endswith('.py'):
            continue
        path = os.path.join(root, fname)
        with open(path, encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
        for i, line in enumerate(lines):
            # Look for lines ending in WHERE with nothing after, or WHERE followed by something on same line but no proper continuation
            stripped = line.rstrip()
            if re.search(r'WHERE\s*$', stripped) or re.search(r'WHERE\s+\w+=\d+\s*$', stripped):
                # Check if this is inside a string (heuristic: line contains a quote but doesn't close it)
                if '"' in stripped or "'" in stripped:
                    rel = path.replace(BASE, '')
                    print(f'{rel}:{i+1}: {stripped}')
