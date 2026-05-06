import re
import os

BASE = r'c:\Users\octav\AppData\Local\Programs\Python\Python312\stdytime'

# Fix email_manager.py
f = os.path.join(BASE, 'modules', 'email_manager.py')
with open(f, encoding='utf-8') as fh:
    c = fh.read()
c = re.sub(r',?\s*owner_user_id\s*:\s*Optional\[int\]\s*=\s*None', '', c)
c = re.sub(r'[ \t]*(?:if\s+)?owner_user_id[^\n]*\n', '', c)
with open(f, 'w', encoding='utf-8') as fh:
    fh.write(c)
print('email_manager.py remaining:', c.count('owner_user_id'))

# Fix server_cache.py comment
f2 = os.path.join(BASE, 'modules', 'server_cache.py')
with open(f2, encoding='utf-8') as fh:
    c2 = fh.read()
old = '# Shared cache key base strings (all runtime keys append :u:{owner_user_id})'
c2 = c2.replace(old, '# Shared cache key base strings')
with open(f2, 'w', encoding='utf-8') as fh:
    fh.write(c2)
print('server_cache.py remaining:', c2.count('owner_user_id'))
