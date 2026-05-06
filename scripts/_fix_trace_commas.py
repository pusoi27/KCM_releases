import re

f = r'c:\Users\octav\AppData\Local\Programs\Python\Python312\stdytime\routes\api.py'
with open(f, encoding='utf-8') as fh:
    content = fh.read()

# Fix string-only lines inside _trace_column3 calls (add trailing comma)
# Pattern: indented "some_event_name"\n followed by whitespace + keyword=
content = re.sub(r'("([a-z_]+)")\n(\s+\w+=)', r'\1,\n\3', content)

with open(f, 'w', encoding='utf-8') as fh:
    fh.write(content)
print('Fixed api.py')
