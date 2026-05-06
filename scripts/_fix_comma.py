f = r'c:\Users\octav\AppData\Local\Programs\Python\Python312\stdytime\routes\students.py'
with open(f, encoding='utf-8') as fh:
    content = fh.read()
# Fix both occurrences
content = content.replace(
    '                day2_time=_dt2\n                subjects=subjects,',
    '                day2_time=_dt2,\n                subjects=subjects,'
)
with open(f, 'w', encoding='utf-8') as fh:
    fh.write(content)
print('Fixed', content.count('day2_time=_dt2,'), 'occurrences')
