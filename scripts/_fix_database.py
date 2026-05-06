import re
import os

BASE = r'c:\Users\octav\AppData\Local\Programs\Python\Python312\stdytime'
f = os.path.join(BASE, 'modules', 'database.py')
with open(f, encoding='utf-8') as fh:
    lines = fh.readlines()

out = []
skip_until = None
i = 0
while i < len(lines):
    line = lines[i]

    # Skip lines with owner_user_id that are just column definitions or ALTER TABLE
    if re.search(r'owner_user_id', line):
        # UNIQUE constraints referencing owner_user_id
        if re.search(r'UNIQUE\(.*owner_user_id.*\)', line):
            # Remove owner_user_id from the UNIQUE constraint
            new_line = re.sub(r',\s*owner_user_id', '', line)
            out.append(new_line)
            i += 1
            continue
        # Otherwise skip the whole line
        i += 1
        continue

    # Skip the entire users CREATE TABLE block (line 199 in original = index 198)
    if re.search(r'CREATE TABLE IF NOT EXISTS users', line):
        # Skip until we see the closing );
        while i < len(lines):
            if lines[i].strip() == ')':
                i += 1  # skip the )
                break
            i += 1
        # skip the closing """)\n line
        if i < len(lines) and lines[i].strip() == '""")'  :
            i += 1
        continue

    # Skip users migration block (pragma + alter table)
    if re.search(r'c\.execute\("PRAGMA table_info\(users\)"\)', line):
        # skip 4 lines
        i += 4
        continue

    # Skip bootstrap admin block
    if re.search(r'# Optional bootstrap admin', line):
        # skip until conn.commit(); conn.close()
        while i < len(lines):
            if 'conn.commit(); conn.close()' in lines[i]:
                out.append(lines[i])
                i += 1
                break
            i += 1
        continue

    # Skip "from werkzeug.security import generate_password_hash" inside init_db
    if re.search(r'from werkzeug\.security import generate_password_hash', line):
        i += 1
        continue

    # Skip bootstrap_admin_email and bootstrap_admin_password variable lines
    if re.search(r'bootstrap_admin', line):
        i += 1
        continue

    # Skip 'now = datetime.now().isoformat()' if it's inside the bootstrap section
    # (we'll leave it if it's used elsewhere; this line only appears once and is for bootstrap)
    if re.search(r'now = datetime\.now\(\)\.isoformat\(\)', line):
        i += 1
        continue

    # Skip migration blocks for owner_user_id: if "owner_user_id" not in cols: ...
    if re.search(r'if "owner_user_id" not in cols', line):
        # skip this if block (2 lines: the if + the cur.execute)
        i += 2
        continue

    # Skip "# Ensure owner_user_id exists..." comment lines
    if re.search(r'# Ensure owner_user_id', line):
        i += 1
        continue

    out.append(line)
    i += 1

result = ''.join(out)
with open(f, 'w', encoding='utf-8') as fh:
    fh.write(result)

remaining = result.count('owner_user_id')
print('database.py remaining owner_user_id:', remaining)
users_table = 'CREATE TABLE IF NOT EXISTS users' in result
print('users table still present:', users_table)
bootstrap = 'bootstrap_admin' in result
print('bootstrap code still present:', bootstrap)
