import pefile

DLL = r"D:\Express Program Files\Angstrong Viewer V2.1.10_240109\Angstrong Viewer V2.1.10_240109\RelWithDebInfo\AngstrongCameraSdk.dll"

pe = pefile.PE(DLL, fast_load=True)
pe.parse_data_directories(directories=[
    pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_EXPORT'],
    pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_IMPORT'],
])

print("=== EXPORTS ===")
if hasattr(pe, 'DIRECTORY_ENTRY_EXPORT'):
    for exp in pe.DIRECTORY_ENTRY_EXPORT.symbols:
        name = exp.name.decode() if exp.name else f"ordinal_{exp.ordinal}"
        print(f"ord={exp.ordinal:<5} rva=0x{exp.address:08x} {name}")
else:
    print("no exports")

print("\n=== IMPORTS (dll -> functions) ===")
if hasattr(pe, 'DIRECTORY_ENTRY_IMPORT'):
    for entry in pe.DIRECTORY_ENTRY_IMPORT:
        dll = entry.dll.decode()
        print(f"\n[{dll}]")
        for imp in entry.imports:
            if imp.name:
                print(f"    {imp.name.decode()}")
            else:
                print(f"    ord#{imp.ordinal}")
