import re

with open(r'c:\Users\octav\AppData\Local\Programs\Python\Python312\stdytime\routes\api.py', encoding='utf-8') as f:
    lines = f.readlines()

for i, line in enumerate(lines):
    # Find string-only lines that should have trailing commas (inside function calls)
    if re.match(r'\s+"[a-z_]+"$', line.rstrip()):
        print(f'{i+1}: {repr(line.rstrip())}')
