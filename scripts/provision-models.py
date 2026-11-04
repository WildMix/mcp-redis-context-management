"""Cache and warm the local embedding model before offline startup."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memory_store import load_model, encode

if __name__ == "__main__":
    encode(load_model(), ["Project memory startup"])
    print("Local embedding model cached and ready for offline startup.")
