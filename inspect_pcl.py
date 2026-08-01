import struct

path = r"D:\Express Program Files\Angstrong Viewer V2.1.10_240109\Angstrong Viewer V2.1.10_240109\RelWithDebInfo\Nuwa-HP60C\ASC60CG03002135\Snap\pointcloud\pointcloud_20260512_111531922.txt"
with open(path, 'rb') as f:
    data = f.read()
print('size:', len(data))
print('head bytes:', data[:200])
# try ascii text?
try:
    txt = data[:500].decode('ascii')
    print('head text:', repr(txt[:300]))
except Exception as e:
    print('not ascii:', e)
# try float32 stream
n = len(data) // 4
floats = struct.unpack('<%df' % n, data)
print('n floats:', n)
print('first floats:', floats[:12])
