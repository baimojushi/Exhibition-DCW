import struct, os

LIB = r"D:\Express Program Files\Angstrong Viewer V2.1.10_240109\Angstrong Viewer V2.1.10_240109\RelWithDebInfo\AngstrongCameraSdk.lib"

with open(LIB, 'rb') as f:
    data = f.read()

assert data[:8] == b'!<arch>\n'
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
    # long names via // member
    members.append((name, pos, size))
    pos += 60 + size
    if size % 2 == 1:
        pos += 1

print(f"total members: {len(members)}")

# find the longnames table
longnames = b''
for name, pos, size in members:
    if name == '//':
        longnames = data[pos:pos+size]
        break

def realname(m):
    name, pos, size = m
    if name.startswith('/'):
        try:
            idx = int(name[1:])
            end = longnames.find(b'\x00', idx)
            return longnames[idx:end].decode('ascii', errors='replace')
        except Exception:
            return name
    return name

obj_names = []
for m in members:
    rn = realname(m)
    if rn.endswith('.obj') or rn.endswith('.o'):
        obj_names.append(rn)

print(f"object files: {len(obj_names)}")
for o in obj_names:
    print(" ", o)
