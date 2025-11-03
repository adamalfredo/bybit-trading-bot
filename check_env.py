import os
from dotenv import load_dotenv
load_dotenv()

def show(name):
    v = os.environ.get(name)
    print(f"{name}: {repr(v)}")
    if v is not None:
        print(f"  masked: {v[:4]}...{v[-4:]} (len={len(v)})")
    print()

show("BYBIT_API_KEY")
show("BYBIT_API_SECRET")
show("BYBIT_KEY")
show("BYBIT_SECRET")
show("BYBIT_BASE_URL")