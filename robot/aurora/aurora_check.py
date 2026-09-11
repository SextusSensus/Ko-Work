import sys
try:
    import slamtec_aurora_sdk as sdk
except Exception as e:
    print("IMPORT-FAIL:", type(e).__name__, e); sys.exit(1)
print("IMPORT-OK  version:", getattr(sdk, "__version__", "?"))
# what's the public surface for pose + map load?
names = [n for n in dir(sdk) if not n.startswith("_")]
print("top-level:", names[:20])
# find the main client class
cls = None
for n in names:
    obj = getattr(sdk, n)
    if isinstance(obj, type) and ("Sdk" in n or "Aurora" in n):
        cls = obj; break
print("client class:", cls.__name__ if cls else "NOT FOUND")
if cls:
    meth = [m for m in dir(cls) if not m.startswith("_")]
    for key in ("pose","map","reloc","connect","session"):
        hits = [m for m in meth if key in m.lower()]
        if hits: print("  %-8s -> %s" % (key, hits))
