"""
app/trained.py — loads the two TRAINED models (Phase 6.16).

WHAT WAS TRAINED (see evaluation/train_models.py)
1. Router classifier: logistic regression on the nomic-embed-text vector of
   the task (768 numbers). It answers: how likely is this a CODE task?
   It replaces "closest example + keyword boost", which sent general
   questions using words like "function" or "program" to the code model.
2. Attachment classifier: logistic regression on TF-IDF features (word
   pairs and 3-5 letter pieces of the question). It answers: how likely is
   this question about a file the user meant to attach?
   It replaces one hand-written regular expression that caught only ~20%.

Both were trained on THIS laptop, offline, on the labelled test data the
project already had. No data left the machine.

FALLBACK
If a model file is missing, scikit-learn is not installed, or the feature
is switched off in config.py, the functions return None and the old
hand-written rules are used instead. The workbench never breaks.
"""

import numpy as np

import config

_cache = {}


def _load(name: str):
    """The saved model bundle, or None. Loaded once, then kept in memory."""
    if name in _cache:
        return _cache[name]
    bundle = None
    path = config.TRAINED_MODELS_DIR / f"{name}.joblib"
    if path.exists():
        try:
            import joblib                       # comes with scikit-learn
            bundle = joblib.load(path)          # only ever our OWN file, written by train_models.py
        except Exception as error:              # scikit-learn missing or a damaged file
            print(f"[trained] could not load {path.name}: {error}; using the hand-written rule")
    _cache[name] = bundle
    return bundle


def info(name: str):
    """Training details shown in the evaluation (date, data size, accuracy)."""
    bundle = _load(name)
    return {k: v for k, v in bundle.items() if k != "model"} if bundle else None


def router_code_probability(vector, use: bool = None):
    """
    Chance (0-1) that the task is a CODE task, from its embedding vector.
    None means: no trained router, use the rules.
    """
    if not (config.USE_TRAINED_ROUTER if use is None else use):
        return None
    bundle = _load("router")
    if bundle is None or vector is None:
        return None
    x = np.asarray(vector, dtype=np.float32)
    x = x / np.linalg.norm(x)
    model = bundle["model"]
    code_column = list(model.classes_).index("code")
    return float(model.predict_proba(x.reshape(1, -1))[0][code_column])


def attachment_probability(question: str, use: bool = None):
    """
    Chance (0-1) that the question refers to a file that should be attached.
    None means: no trained detector, use the rule.
    """
    if not (config.USE_TRAINED_ATTACHMENT if use is None else use):
        return None
    bundle = _load("attachment")
    if bundle is None:
        return None
    model = bundle["model"]
    yes_column = list(model.classes_).index("yes")
    return float(model.predict_proba([question])[0][yes_column])
