import os

for name in ["SYNTHETIC_API_KEY", "OPENCODE_API_KEY", "OPENAI_API_KEY"]:
    print(f"{name}: {'SET' if os.environ.get(name) else 'MISSING/EMPTY'}")
