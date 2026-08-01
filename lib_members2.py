import struct, os

LIB = r"D:\Express Program Files\Angstrong Viewer V2.1.10_240109\Angstrong Viewer V2.1.10_240109\RelWithDebInfo\AngstrongCameraSdk.lib"

with open(LIB, 'rb') as f:
    data = f.read()

pos = 8
members = []
while pos < len(data):
    hdr = data[pos:pos+60]
    if len(hdr) < 60:
        break
    name = hdr[0:16].decode('ascii', errors='replace').rstrip(' ')
    size_str = hdr[48:58].decode('ascii', errors='replace').strip()
    try:
        size = int(size_str)
    except ValueError:
        break
    members.append((name, pos, size))
    pos += 60 + size
    if size % 2 == 1:
        pos += 1

from collections import Counter
names = Counter(m[0] for m in members)
print("unique member name patterns (top 20):")
for n, c in names.most_common(20):
    print(f"  {n!r}: {c}")

longnames = b''
for name, pos, size in members:
    if name == '//':
        longnames = data[pos:pos+size]
        break
print("longnames table size:", len(longnames))

def realname(m):
    name, pos, size = m
    if name.startswith('/') and name != '/':
        try:
            idx = int(name[1:])
            end = longnames.find(b'\x00', idx)
            return longnames[idx:end].decode('ascii', errors='replace')
        except Exception:
            return name
    return name

sample = [(realname(m), m[2]) for m in members[:40]]
for n, s in sample:
    print(f"  {n!r} size={s}")
