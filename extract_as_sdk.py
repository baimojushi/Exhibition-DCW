import re

data = open(r'C:\Users\Administrator\hp60c_audit\exports.txt', 'rb').read()
text = data.decode('utf-8', errors='replace')
out = []
for l in text.splitlines():
    if l.startswith('ord=') and ('AS_SDK' in l or 'AS_HP60C' in l or 'AS_Nuwa' in l):
        out.append(l)
with open(r'C:\Users\Administrator\hp60c_audit\as_sdk_exports.txt', 'w', encoding='utf-8') as f:
    f.write('\n'.join(out))
print(len(out), 'AS_SDK lines written')
