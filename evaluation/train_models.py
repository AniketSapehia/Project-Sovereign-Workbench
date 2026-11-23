"""
evaluation/train_models.py — TRAINS the two classifiers (Phase 6.16).

WHAT IS TRAINED, AND WHY
The evaluation found two weak, hand-written parts:
  1. The model ROUTER (general vs code model) used "closest example + a
     keyword boost". General questions using words like "function" or
     "program" were routed correctly only 79 times out of 260.
  2. The MISSING-ATTACHMENT CHECK was one regular expression. It caught only
     ~19% of the questions that needed a warning.
Both are CLASSIFICATION problems (pick one of two labels), and the project
already has thousands of labelled examples (evaluation/*_bigset.csv). So we
train a classifier for each, here on the laptop, offline.

THE TWO MODELS
  Router:     logistic regression on the task's EMBEDDING (768 numbers from
              nomic-embed-text, the same vector the router already computes,
              so routing costs nothing extra).
  Attachment: logistic regression on TF-IDF features of the question text:
              single words and word pairs, plus 3-5 letter pieces of words
              ("char n-grams"), which still match when there is a typo or
              Hinglish ("attachmnt", "PDF bheji").
Logistic regression is a simple, well understood, fast model: it learns one
weight per feature and outputs a probability (0-1). It trains in seconds on
a CPU, and its decisions can be inspected.

HOW IT IS TESTED HONESTLY
  - Grouped 5-fold cross-validation: the data is split into 5 parts, the
    model is trained on 4 and tested on the 5th, five times. All rewordings
    of one SEED question stay in the same part (GroupKFold), otherwise the
    model would be tested on near-copies of its training questions.
  - The regularisation strength C (how strongly the weights are kept small)
    is chosen by that cross-validation, from 0.1, 1 and 10.
  - The FINAL test is on evaluation/fresh_*_testset.csv: new questions in new
    styles, written before training and never used for it. evaluate.py
    compares the old rule and the trained model on them.

HOW TO RUN (from the project folder, with .venv active; needs scikit-learn)
    pip install scikit-learn
    python evaluation/train_models.py
It takes a few minutes (most of it is computing 5,000 embeddings once; they
are cached in data/models/ for the next run). Writes:
    data/models/router.joblib, data/models/attachment.joblib
    results/training_<date>.md and .json
"""

import asyncio
import csv
import datetime as dt
import json
import sys
import time
from pathlib import Path

import numpy as np

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import config                                             # noqa: E402
from app import llm, router                               # noqa: E402
from app.main import _MISSING_ATTACHMENT                  # noqa: E402  (the old rule, for comparison)

try:
    import joblib
    import sklearn
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import GroupKFold
    from sklearn.pipeline import FeatureUnion, make_pipeline
except ImportError:
    sys.exit("scikit-learn is not installed. Run:  pip install scikit-learn")

EVAL = PROJECT / "evaluation"
C_VALUES = [0.1, 1.0, 10.0]
FOLDS = 5


# ---------------------------------------------------------------------------
# 1. Training data
# ---------------------------------------------------------------------------
def read(name: str, text_col: str, label_col: str, label_map=None):
    """(texts, labels, seed groups) from one of the generated CSV files."""
    with open(EVAL / name, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    labels = [label_map(r[label_col]) if label_map else r[label_col] for r in rows]
    return [r[text_col] for r in rows], labels, [r["seed_id"] for r in rows]


def dev_sets():
    """The 30 + 20 original development questions (each its own seed group)."""
    import evaluate
    router_dev = [(t, label, f"DR{i}") for i, (t, label) in enumerate(evaluate.ROUTER_TESTS)]
    attach_dev = [(q, "yes" if w else "no", f"DA{i}") for i, (q, w) in enumerate(evaluate.ATTACHMENT_TESTS)]
    return router_dev, attach_dev


# ---------------------------------------------------------------------------
# 2. Embeddings (cached, because computing 5,000 of them takes minutes on a CPU)
# ---------------------------------------------------------------------------
CACHE = config.TRAINED_MODELS_DIR / "embedding_cache.npz"


async def embed_all(texts: list) -> np.ndarray:
    cached = {}
    if CACHE.exists():
        data = np.load(CACHE, allow_pickle=False)
        cached = dict(zip(data["texts"].tolist(), data["vectors"]))
    missing = [t for t in dict.fromkeys(texts) if t not in cached]
    for start in range(0, len(missing), 64):
        chunk = missing[start:start + 64]
        vectors = await llm.embed([config.EMBED_QUERY_PREFIX + t for t in chunk])
        cached.update(zip(chunk, np.asarray(vectors, dtype=np.float32)))
        print(f"   embedded {min(start + 64, len(missing))} of {len(missing)} new texts")
    config.TRAINED_MODELS_DIR.mkdir(parents=True, exist_ok=True)
    keys = list(cached)
    np.savez(CACHE, texts=np.array(keys), vectors=np.stack([cached[k] for k in keys]))
    matrix = np.stack([cached[t] for t in texts]).astype(np.float32)
    return matrix / np.linalg.norm(matrix, axis=1, keepdims=True)      # unit length, as the router uses


# ---------------------------------------------------------------------------
# 3. Models
# ---------------------------------------------------------------------------
def router_model(c: float):
    return LogisticRegression(C=c, max_iter=3000, class_weight="balanced")


def attachment_model(c: float):
    features = FeatureUnion([
        ("words", TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True, min_df=2)),
        ("letters", TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), sublinear_tf=True, min_df=2)),
    ])
    return make_pipeline(features, LogisticRegression(C=c, max_iter=3000, class_weight="balanced"))


def grouped_cv(make, X, y, groups, rule_predictions):
    """
    Grouped 5-fold cross-validation for each C. Returns the per-fold accuracy
    of the trained model for every C, and of the old rule on the same folds.
    """
    y = np.asarray(y)
    folds = list(GroupKFold(n_splits=FOLDS).split(np.zeros(len(y)), y, groups))
    out = {"folds": FOLDS, "by_C": {}, "rule": []}
    # NEW (Phase 6.17): out-of-fold predictions.
    # In 5-fold cross-validation every example sits in the TEST part exactly once.
    # We keep the prediction made at that moment, by a model that never saw the
    # example or any rewording of its seed question. Put together, that gives one
    # honest prediction for ALL ~5,000 examples -> a confusion matrix on all of them.
    oof = {}                                       # C -> array of out-of-fold predictions
    for c in C_VALUES:
        accs = []
        oof[c] = np.empty(len(y), dtype=object)
        for train, test in folds:
            Xtr = X[train] if isinstance(X, np.ndarray) else [X[i] for i in train]
            Xte = X[test] if isinstance(X, np.ndarray) else [X[i] for i in test]
            model = make(c).fit(Xtr, y[train])
            predicted = model.predict(Xte)
            oof[c][test] = predicted               # remember the prediction for these held-out examples
            accs.append(float((predicted == y[test]).mean()))
        out["by_C"][str(c)] = accs
        print(f"   C={c:<5} accuracy per fold: {', '.join(f'{a:.3f}' for a in accs)}  mean {np.mean(accs):.3f}")
    rule = np.asarray(rule_predictions)
    out["rule"] = [float((rule[test] == y[test]).mean()) for _, test in folds]
    print(f"   old rule, same folds:            {', '.join(f'{a:.3f}' for a in out['rule'])}  mean {np.mean(out['rule']):.3f}")
    out["best_C"] = float(max(C_VALUES, key=lambda c: np.mean(out["by_C"][str(c)])))
    # Confusion matrices over ALL examples: trained model (best C) and the old rule.
    out["confusion_trained"] = confusion(y, oof[out["best_C"]])
    out["confusion_rule"] = confusion(y, rule)
    return out


def confusion(actual, predicted) -> dict:
    """{actual label: {predicted label: count}} plus accuracy and the total, for the report and charts."""
    actual = np.asarray([str(a) for a in actual])          # plain text labels, so the JSON is clean
    predicted = np.asarray([str(p) for p in predicted])
    labels = sorted({str(a) for a in actual})
    matrix = {a: {p: int(np.sum((actual == a) & (predicted == p))) for p in labels} for a in labels}
    total = int(len(actual))
    correct = sum(matrix[l][l] for l in labels)
    return {"labels": labels, "matrix": matrix, "total": total, "accuracy": correct / total}


# ---------------------------------------------------------------------------
# 4. Train both
# ---------------------------------------------------------------------------
async def main():
    started = time.time()
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H-%M")
    router_dev, attach_dev = dev_sets()
    report = {"date": dt.datetime.now().strftime("%Y-%m-%d %H:%M"), "sklearn": sklearn.__version__}

    # ---- Router ----
    print("1/2 Router classifier")
    texts, labels, groups = read("router_bigset.csv", "task", "label")
    texts += [t for t, _, _ in router_dev]; labels += [l for _, l, _ in router_dev]; groups += [g for _, _, g in router_dev]
    print(f"   {len(texts)} examples from {len(set(groups))} seed questions; computing embeddings…")
    X = await embed_all(texts)
    examples = await router._examples_for(["general", "code"])
    rule = []
    for text, vector in zip(texts, X):          # the OLD router's answer, for comparison
        scores, _ = router._scores(text, vector, examples, ["general", "code"])
        rule.append(max(scores, key=scores.get))
    cv = grouped_cv(router_model, X, labels, groups, rule)
    final = router_model(cv["best_C"]).fit(X, np.asarray(labels))
    info = {"trained_at": report["date"], "examples": len(texts), "seeds": len(set(groups)),
            "features": "nomic-embed-text embedding (768 numbers, unit length)",
            "method": "logistic regression, class_weight=balanced", "C": cv["best_C"],
            "cv_accuracy": float(np.mean(cv["by_C"][str(cv["best_C"])])), "cv_rule_accuracy": float(np.mean(cv["rule"]))}
    joblib.dump({**info, "model": final}, config.TRAINED_MODELS_DIR / "router.joblib")
    report["router"] = {**info, "cv": cv}

    # ---- Attachment ----
    print("2/2 Attachment classifier")
    texts, labels, groups = read("attachment_bigset.csv", "question", "should_warn")
    texts += [t for t, _, _ in attach_dev]; labels += [l for _, l, _ in attach_dev]; groups += [g for _, _, g in attach_dev]
    print(f"   {len(texts)} examples from {len(set(groups))} seed questions")
    rule = ["yes" if _MISSING_ATTACHMENT.search(t) else "no" for t in texts]
    cv = grouped_cv(attachment_model, texts, labels, groups, rule)
    final = attachment_model(cv["best_C"]).fit(texts, np.asarray(labels))
    info = {"trained_at": report["date"], "examples": len(texts), "seeds": len(set(groups)),
            "features": "TF-IDF of words, word pairs and 3-5 letter pieces",
            "method": "logistic regression, class_weight=balanced", "C": cv["best_C"],
            "cv_accuracy": float(np.mean(cv["by_C"][str(cv["best_C"])])), "cv_rule_accuracy": float(np.mean(cv["rule"]))}
    joblib.dump({**info, "model": final}, config.TRAINED_MODELS_DIR / "attachment.joblib")
    report["attachment"] = {**info, "cv": cv}

    # ---- What the attachment model learned: its strongest words ----
    vocabulary = np.concatenate([v.get_feature_names_out() for _, v in final[0].transformer_list])
    weights = final[-1].coef_[0]
    order = np.argsort(weights)
    report["attachment"]["top_features"] = {"warn": vocabulary[order[-15:]][::-1].tolist(),
                                            "no warning": vocabulary[order[:15]].tolist()}

    # ---- Report ----
    report["seconds"] = round(time.time() - started, 1)
    results = PROJECT / "results"
    results.mkdir(exist_ok=True)
    (results / f"training_{stamp}.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    md = [f"# Training report — {report['date']}", "",
          "| Model | Examples (seeds) | Features | C | Cross-validated accuracy | Old rule, same folds |",
          "|---|---|---|---|---|---|"]
    for key, name in [("router", "Router"), ("attachment", "Attachment check")]:
        m = report[key]
        md.append(f"| {name} | {m['examples']} ({m['seeds']}) | {m['features']} | {m['C']} | "
                  f"{m['cv_accuracy']:.1%} | {m['cv_rule_accuracy']:.1%} |")
    # NEW (Phase 6.17): confusion matrices over ALL examples (out-of-fold predictions)
    for key, name in [("router", "Router"), ("attachment", "Attachment check")]:
        for kind, title in [("confusion_trained", "trained model"), ("confusion_rule", "old rule")]:
            cm = report[key]["cv"][kind]
            labs = cm["labels"]
            md += ["", f"### {name}: {title}, all {cm['total']} examples (accuracy {cm['accuracy']:.1%})", "",
                   "| Actual \\ Predicted | " + " | ".join(labs) + " |", "|---" * (len(labs) + 1) + "|"]
            md += [f"| {a} | " + " | ".join(str(cm["matrix"][a][p]) for p in labs) + " |" for a in labs]
    md += ["", "Grouped 5-fold cross-validation: all rewordings of one seed question stay in the same fold.",
           "Each example's prediction comes from the fold in which it was held out, so no model saw the question it was tested on.",
           "The final, independent test is on evaluation/fresh_*_testset.csv (run `python evaluate.py`).", "",
           "Strongest 'warn' features: " + ", ".join(report["attachment"]["top_features"]["warn"]),
           "", "Strongest 'no warning' features: " + ", ".join(report["attachment"]["top_features"]["no warning"])]
    (results / f"training_{stamp}.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"\nDone in {report['seconds']} s. Models saved in {config.TRAINED_MODELS_DIR}")
    print(f"Router:     {report['router']['cv_accuracy']:.1%} cross-validated (old rule {report['router']['cv_rule_accuracy']:.1%})")
    print(f"Attachment: {report['attachment']['cv_accuracy']:.1%} cross-validated (old rule {report['attachment']['cv_rule_accuracy']:.1%})")
    for key, name in [("router", "Router"), ("attachment", "Attachment")]:
        cm = report[key]["cv"]["confusion_trained"]
        print(f"{name} confusion matrix over all {cm['total']} examples: {cm['matrix']}")
    print(f"Report: results/training_{stamp}.md  (send me the .json for the charts)")


if __name__ == "__main__":
    asyncio.run(main())
