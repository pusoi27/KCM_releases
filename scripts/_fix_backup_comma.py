import re

for fpath in [
    r'c:\Users\octav\AppData\Local\Programs\Python\Python312\stdytime\routes\assistants.py',
    r'c:\Users\octav\AppData\Local\Programs\Python\Python312\stdytime\routes\books.py',
    r'c:\Users\octav\AppData\Local\Programs\Python\Python312\stdytime\routes\api.py',
    r'c:\Users\octav\AppData\Local\Programs\Python\Python312\stdytime\routes\schedule.py',
    r'c:\Users\octav\AppData\Local\Programs\Python\Python312\stdytime\routes\reports.py',
]:
    with open(fpath, encoding='utf-8') as f:
        content = f.read()
    # Fix backup_path=backup_path without trailing comma followed by table_names or other kwarg
    fixed = re.sub(r'(backup_path=backup_path)\n(\s+table_names)', r'\1,\n\2', content)
    if fixed != content:
        with open(fpath, 'w', encoding='utf-8') as f:
            f.write(fixed)
        print(f'Fixed: {fpath.split(chr(92))[-1]}')
    else:
        print(f'No changes: {fpath.split(chr(92))[-1]}')
