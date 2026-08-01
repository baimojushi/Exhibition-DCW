import re

data = open(r'C:\Users\Administrator\hp60c_audit\exports.txt', 'rb').read()
text = data.decode('utf-8', errors='replace')
lines = [l for l in text.splitlines() if l.startswith('ord=')]

# operator delete / new exports
for pat in [r'\?\?3@YAXPEAX@Z', r'\?\?3@YAXPEAX_K@Z', r'\?\?2@YAPEAX_K@Z', r'\?\$destroy@', r'\?\$assign@\?\$vector@USTREAM_PARAM_S@@']:
    hits = [l for l in lines if re.search(pat, l)]
    print(f"{pat}: {len(hits)}")
    for h in hits[:2]:
        print("   ", h[:160])
