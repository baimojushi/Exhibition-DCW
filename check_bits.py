import pefile, struct, sys

DLL = r"D:\Express Program Files\Angstrong Viewer V2.1.10_240109\Angstrong Viewer V2.1.10_240109\RelWithDebInfo\AngstrongCameraSdk.dll"
LIB = r"D:\Express Program Files\Angstrong Viewer V2.1.10_240109\Angstrong Viewer V2.1.10_240109\RelWithDebInfo\AngstrongCameraSdk.lib"

pe = pefile.PE(DLL, fast_load=True)
machine = pe.FILE_HEADER.Machine
print("DLL machine:", hex(machine), "(0x14c=x86/32bit, 0x8664=x64/64bit)")
print("Python:", sys.version, sys.maxsize > 2**32)

import os
print("LIB size:", os.path.getsize(LIB))
with open(LIB, 'rb') as f:
    head = f.read(8)
print("LIB header:", head.hex())
# archive header '!<arch>\n' -> static lib; 'Microsoft (R) Library Manager' -> import lib
print("Is static archive:", head.startswith(b'!<arch>'))
