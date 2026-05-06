import re
import os

BASE = r'c:\Users\octav\AppData\Local\Programs\Python\Python312\stdytime'
target_dirs = ['routes', 'modules']

issues = []
for tdir in target_dirs:
    full = os.path.join(BASE, tdir)
    for fname in os.listdir(full):
        if not fname.endswith('.py'):
            continue
        path = os.path.join(full, fname)
        with open(path, encoding='utf-8') as f:
            lines = f.readlines()
        for i, line in enumerate(lines):
            stripped = line.rstrip()
            # Pattern: line ends with a ( but has content before it that looks broken
            # Or: def func(  followed immediately by """ (docstring on same line after orphaned param)
            # Or: WHERE\s*$ suggesting truncated SQL string
            if re.search(r'\(\s*"""', stripped):
                issues.append((path.replace(BASE,''), i+1, 'broken-def-docstring', stripped))
            elif re.search(r'(?:WHERE|SET|FROM)\s*$', stripped) and ('"' in stripped or "'" in stripped):
                issues.append((path.replace(BASE,''), i+1, 'truncated-sql', stripped))
            elif re.search(r',\s*$', stripped) and re.search(r'\[.*,\s*$', stripped):
                pass  # list continuation is ok
            elif re.search(r'values\s*=.*,\s*$', stripped) and not stripped.strip().endswith(']'):
                # possibly truncated values list
                if re.search(r',\s*$', stripped) and not re.search(r'#', stripped):
                    issues.append((path.replace(BASE,''), i+1, 'maybe-truncated-list', stripped))

for iss in issues:
    print(f'{iss[0]}:{iss[1]} [{iss[2]}] {iss[3]}')
print(f'Total potential issues: {len(issues)}')
