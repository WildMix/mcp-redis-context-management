"""Download pinned local semantic comparison weights and warm both models before offline startup."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sentence_transformers import CrossEncoder
import memory_relations
from memory_store import load_model, encode

if __name__ == "__main__":
    encode(load_model(), ["Project memory startup"])
    if memory_relations.enabled():
        CrossEncoder(memory_relations.MODEL, revision=memory_relations.REVISION, device="cpu")
        memory_relations.warm()
    print("Local memory models cached and ready for offline startup.")
