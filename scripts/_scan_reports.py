import re

f = r'c:\Users\octav\AppData\Local\Programs\Python\Python312\stdytime\routes\reports.py'
with open(f, encoding='utf-8') as fh:
    lines = fh.readlines()

# Find all instances of c.execute(q, (...  var = c.fetchall() patterns
for i, line in enumerate(lines):
    if re.search(r'c\.execute\([^)]*\([^)]*$', line.rstrip()):
        for j in range(i, min(len(lines), i+3)):
            print(f'{j+1}: {repr(lines[j].rstrip())}')
        print('---')
