import re

data = open(r'C:\Users\Administrator\hp60c_audit\exports.txt', 'rb').read()
text = data.decode('utf-8', errors='replace')
lines = [l for l in text.splitlines() if l.startswith('ord=')]
print('total exports:', len(lines))

def readable(name):
    m = re.search(r'\?([A-Za-z_]\w*)@', name)
    if m:
        return m.group(1)
    return name

groups = {}
for l in lines:
    m = re.match(r'ord=\d+\s+rva=0x[0-9a-f]+\s+(.+)$', l)
    if not m:
        continue
    name = m.group(1).strip()
    key = readable(name)
    groups.setdefault(key, []).append(name)

print('unique identifiers:', len(groups))
for k in sorted(groups):
    print(f"{k}: {len(groups[k])}")
