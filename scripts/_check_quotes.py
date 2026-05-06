with open(r'c:\Users\octav\AppData\Local\Programs\Python\Python312\stdytime\modules\database.py', encoding='utf-8') as f:
    lines = f.readlines()
print('Total lines:', len(lines))
for i, line in enumerate(lines):
    if '"""' in line:
        print(f'{i+1}: {repr(line.rstrip())}')
