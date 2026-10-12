"""Preview selected curated Markdown using the real, locally cached embedding model.

No Redis connection, embeddings or model download. The output is review evidence;
the current directory importer does not consume the selection manifest itself.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from curated_corpus import prepare
from memory_store import MODEL_NAME


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(MODEL_NAME, local_files_only=True, device="cpu")
    report = prepare(args.manifest, model)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"domain": report["domain"], "documents": len(report["documents"]),
                      "chunks": len(report["records"]),
                      "max_tokens": max(r["tokens"] for r in report["records"]),
                      "unselected": report["unselected_files"]}, indent=2))


if __name__ == "__main__":
    main()
