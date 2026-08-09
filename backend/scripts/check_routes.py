import urllib.request

paths = [
    "/providers/zai/quota",
    "/providers/synthetic/quota",
    "/health",
    "/settings/clones",
    "/openapi.json",
]
for p in paths:
    try:
        r = urllib.request.urlopen("http://localhost:8000" + p)
        body = r.read().decode()[:200]
        print(p, "->", r.status, body)
    except Exception as e:
        print(p, "->", e)