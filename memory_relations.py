"""Local, bounded semantic comparison. Similarity alone never overwrites knowledge."""
from __future__ import annotations

import os
import re
import threading

MODEL = "cross-encoder/nli-deberta-v3-base"
REVISION = "6c749ce3425cd33b46d187e45b92bbf96ee12ec7"
_model = None
_lock = threading.Lock()


def enabled():
    return os.environ.get("MEMORY_RELATIONS", "local") != "off"


def load_model():
    global _model
    if _model is None:
        from sentence_transformers import CrossEncoder
        _model = CrossEncoder(MODEL, revision=REVISION, device="cpu", local_files_only=True)
    return _model


def warm():
    if enabled():
        with _lock:
            load_model().predict([("Memory is local.", "Memory is local.")], apply_softmax=True)


def explicit_update(text: str) -> bool:
    # Require an asserted transition; suggestions/questions must not retire accepted facts.
    if re.search(r"\?|\b(might|maybe|perhaps|propos\w*|suggest\w*|should|could|would|hypothetical|not yet|not decided)\b", text, re.I):
        return False
    return bool(re.search(r"\b(now|no longer|replaces?|supersedes?|changed from|changed to|as of)\b", text, re.I))


def same_scope(old: str, new: str) -> bool:
    # NLI alone can confuse "production uses X" / "staging uses Y" with a conflict.
    environments = r"\b(production|prod|staging|stage|development|dev|testing|test|local|windows|macos|linux)\b"
    normalize = {"prod": "production", "stage": "staging", "dev": "development", "testing": "test"}
    def scopes(text):
        return {normalize.get(x.lower(), x.lower()) for x in re.findall(environments, text, re.I)}
    if scopes(old) != scopes(new):
        return False
    acronyms = r"\b[A-Z][A-Z0-9_-]{1,}\b"
    if set(re.findall(acronyms, old)) != set(re.findall(acronyms, new)):
        return False
    old_dates, new_dates = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", old), re.findall(r"\b\d{4}-\d{2}-\d{2}\b", new)
    return not (old_dates and new_dates and max(new_dates) < max(old_dates))


def compare(old: str, new: str) -> str:
    """Return equivalent, extends, contradicts, updates, or unrelated/uncertain.

    NLI labels are contradiction, entailment, neutral (model card). Check both
    directions. Never truncate a long note and then retire its unexamined content.
    The classifier is trained for English; no multilingual accuracy is claimed.
    """
    if not enabled() or not same_scope(old, new):
        return "uncertain"
    with _lock:
        model = load_model()
        limit = min(model.max_seq_length or 512, 512)
        if len(model.tokenizer(old, new, truncation=False)["input_ids"]) > limit:
            return "uncertain"
        forward, backward = model.predict([(old, new), (new, old)], apply_softmax=True,
                                          show_progress_bar=False)
    if forward[1] >= .95 and backward[1] >= .95:
        return "equivalent"
    if backward[1] >= .95 and forward[2] >= .90:
        return "extends"
    if max(forward[0], backward[0]) >= .90:
        # A contradictory replacement must not retire unrelated facts in a long note.
        single_claim = len(re.split(r"[.!?]\s+|\n", old.strip().rstrip(".!?"))) == 1
        return "updates" if explicit_update(new) and single_claim else "contradicts"
    return "uncertain"
