f = r'c:\Users\octav\AppData\Local\Programs\Python\Python312\stdytime\routes\qr.py'
with open(f, encoding='utf-8') as fh:
    content = fh.read()

broken = 'c.execute("SELECT id, title FROM books WHERE            books = c.fetchall()'
fixed = 'c.execute("SELECT id, title FROM books")\n            books = c.fetchall()'
new = content.replace(broken, fixed)
print('Replacements:', content.count(broken))
with open(f, 'w', encoding='utf-8') as fh:
    fh.write(new)
print('Done')
