import re

with open(r'c:\Users\octav\AppData\Local\Programs\Python\Python312\stdytime\routes\qr.py', encoding='utf-8') as f:
    lines = f.readlines()

for i, line in enumerate(lines):
    stripped = line.rstrip()
    if re.search(r'WHERE\s*$', stripped) and ('"' in stripped or "'" in stripped):
        for j in range(max(0,i-1), min(len(lines), i+3)):
            print(f'{j+1}: {repr(lines[j].rstrip())}')
        print('---')
