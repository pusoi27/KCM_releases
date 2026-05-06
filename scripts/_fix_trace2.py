import re

f = r'c:\Users\octav\AppData\Local\Programs\Python\Python312\stdytime\routes\api.py'
with open(f, encoding='utf-8') as fh:
    content = fh.read()

# Fix _trace_column3 calls with missing comma after event name string
# Pattern: _trace_column3("some_event" sid= or keyword=  (single line)
content = re.sub(r'(_trace_column3\("[a-z_]+")\s+(\w+=)', r'\1, \2', content)

with open(f, 'w', encoding='utf-8') as fh:
    fh.write(content)
print('Fixed')
