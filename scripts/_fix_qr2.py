import re

f = r'c:\Users\octav\AppData\Local\Programs\Python\Python312\stdytime\routes\qr.py'
with open(f, encoding='utf-8') as fh:
    content = fh.read()

# Fix all truncated SQL strings: "...WHERE\s*" followed by next variable assignment
# Pattern: c.execute("SELECT ... WHERE            varname = c.fetchall()
# Fix: close the string and put variable on new line
def fix_truncated_where(content):
    import re
    # Pattern: any execute call where string ends with WHERE + spaces + variable assignment
    pattern = r'(c\.execute\("SELECT[^"]*?)WHERE\s+(\w+\s*=\s*c\.\w+\(\))'
    replacement = r'\1")\n            \2'
    return re.sub(pattern, replacement, content)

new = fix_truncated_where(content)
changed = content.count('WHERE') - new.count('WHERE')
print(f'Changed {changed} occurrences')
with open(f, 'w', encoding='utf-8') as fh:
    fh.write(new)
print('Done')
