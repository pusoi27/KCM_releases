import re

f = r'c:\Users\octav\AppData\Local\Programs\Python\Python312\stdytime\routes\books.py'
with open(f, encoding='utf-8') as fh:
    lines = fh.readlines()

new_lines = []
i = 0
while i < len(lines):
    line = lines[i]
    # Pattern: _invalidate_books_cache(    followed by return/code on same line
    m = re.match(r'^(\s*)_invalidate_books_cache\(\s+(.*)', line)
    if m:
        indent = m.group(1)
        rest = m.group(2)
        new_lines.append(f'{indent}_invalidate_books_cache()\n')
        new_lines.append(f'{indent}{rest}\n')
        i += 1
        continue
    # Pattern: _invalidate_book_sync_caches(    followed by return/code on same line
    m2 = re.match(r'^(\s*)_invalidate_book_sync_caches\(\s+(.*)', line)
    if m2:
        indent = m2.group(1)
        rest = m2.group(2)
        new_lines.append(f'{indent}_invalidate_book_sync_caches()\n')
        new_lines.append(f'{indent}{rest}\n')
        i += 1
        continue
    new_lines.append(line)
    i += 1

with open(f, 'w', encoding='utf-8') as fh:
    fh.writelines(new_lines)
print('Done')
