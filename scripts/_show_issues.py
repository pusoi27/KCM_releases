import re
files = ['routes/assistants.py', 'routes/books.py', 'routes/dashboard.py', 'routes/students.py']
for rel in files:
    with open(rel, encoding='utf-8') as f:
        lines = f.readlines()
    for i, line in enumerate(lines):
        if re.search(r'\(\s*"""', line.rstrip()) and not re.search(r'execute|executemany', line):
            for j in range(max(0,i-1), min(len(lines), i+3)):
                print(f'{rel}:{j+1}: {repr(lines[j].rstrip())}')
            print('---')
