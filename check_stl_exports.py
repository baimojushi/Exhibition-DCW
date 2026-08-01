import re

data = open(r'C:\Users\Administrator\hp60c_audit\exports.txt', 'rb').read()
text = data.decode('utf-8', errors='replace')
lines = [l for l in text.splitlines() if l.startswith('ord=')]

pats = {
    'list<void*> ctor': r'\?\?0\?\$list@PEAX',
    'list<void*> dtor': r'\?\?1\?\$list@PEAX',
    'list<void*> push_back': r'\?\$push_back@\?\$list@PEAX',
    'list<void*> clear': r'\?\$clear@\?\$list@PEAX',
    'vector<STREAM_PARAM> ctor': r'\?\?0\?\$vector@USTREAM_PARAM_S@@',
    'vector<STREAM_PARAM> dtor': r'\?\?1\?\$vector@USTREAM_PARAM_S@@',
    'vector<STREAM_PARAM> push_back': r'\?\$push_back@\?\$vector@USTREAM_PARAM_S@@',
    'vector<STREAM_PARAM> reserve': r'\?\$reserve@\?\$vector@USTREAM_PARAM_S@@',
    'vector<PCL> dtor': r'\?\?1\?\$vector@UAS_POINTCLOUD@@',
    'list<CAMERA_HANDLE> dtor': r'\?\?1\?\$list@PEAUCAMERA_HANDLE@@',
}
for name, pat in pats.items():
    hits = [l for l in lines if re.search(pat, l)]
    print(f"{name}: {len(hits)}")
    for h in hits[:3]:
        print("   ", h[:150])
