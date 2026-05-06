import re

f = r'c:\Users\octav\AppData\Local\Programs\Python\Python312\stdytime\modules\database.py'
with open(f, encoding='utf-8') as fh:
    content = fh.read()

# Fix trailing commas before ) in CREATE TABLE - pattern: comma then optional whitespace/newline then )
content = re.sub(r',(\s*\n\s*\))', r'\1', content)

with open(f, 'w', encoding='utf-8') as fh:
    fh.write(content)
print('Fixed trailing commas in database.py')
