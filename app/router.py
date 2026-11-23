"""
app/router.py — the MODEL ROUTER (Phase 4).

The problem statement asks the backend to "support multiple open weight
models at once and automatically pick the right one for a given task based
on what that task needs, a coding request handled differently from a
document summary request".

The router answers ONE question per message: which model ("role") should
handle this task? It decides in this order, and always records WHY:

  1. MANUAL CHOICE   the user picked a model in the dropdown -> use it.
  2. ATTACHMENT TYPE a photo or image is attached -> "vision".
                     A PDF plus a question about a drawing/diagram/photo
                     -> "vision" too (Phase 5).
  3. MEANING         the task is turned into a meaning vector by the
                     embedding model, and compared with EXAMPLE TASKS for
                     each role (below). The role whose examples are most
                     similar gets the highest score.
  4. KEYWORDS        clear words such as "python" or "debug" add a boost
                     (config.ROUTER_KEYWORD_BOOST) to that role's score.
  5. FALLBACK        if the winning model isn't installed, use the
                     general model instead, and say so.

Why examples + meaning instead of only keywords? Keywords miss tasks worded
differently ("make this script stop crashing" has no word "code"), while
meaning-matching catches them. Keywords are kept as a transparent nudge.

Why not ask the language model itself to classify the task? That would add
a whole extra model call (tens of seconds on a CPU) to every message. One
embedding call takes well under a second.

Every decision is saved to data/router_log.jsonl, which is the raw data for
evaluating the router later (accuracy, confusion matrix).
"""

import json
import re
from datetime import datetime

import numpy as np

import config
from app import documents, knowledge, llm, trained

# ---------------------------------------------------------------------------
# Example tasks for each role. The router compares every new task with
# these. To teach it a new kind of task, add an example here.
# ---------------------------------------------------------------------------
EXAMPLES = {
    "general": [
        "Summarise this inspection report",
        "Draft an approval note for replacing the pump bearings",
        "What does the SOP say about bearing temperature limits?",
        "Calculate the corrosion rate and remaining life of the vessel",
        "Which vibration zone is this reading in and what action is required?",
        "Make a spreadsheet of the readings in this report",
        "Write an email to the vendor about the delayed delivery",
        "Who must approve expenditure of 3 lakh rupees?",
        "Compare the findings of these two reports",
        "Explain what NPSH means for a centrifugal pump",
    ],
    "code": [
        "Write a Python function that classifies vibration readings",
        "Write a script that reads a CSV file and plots the values",
        "Fix the error in this Python code",
        "Debug this program, it crashes with a KeyError",
        "Write unit tests for this function",
        "Convert this Excel formula into Python code",
        "Write an SQL query to find pumps with high vibration",
        "Explain what this code does line by line",
        "Refactor this function to make it faster",
        "Write a program to convert units from psi to bar",
    ],
    "vision": [
        "Describe what is shown in this photo",
        "What equipment can you see in this image?",
        "Is there visible corrosion or damage in the picture?",
        "Read the labels on this engineering drawing",
        "Identify the defects in this photograph of the weld",
    ],
}

# Clear words that point at a role. Each match adds ROUTER_KEYWORD_BOOST once.
KEYWORDS = {
    "code": r"\b(python|code|script|function|program|debug|bug|traceback|exception|"
            r"sql|regex|javascript|unit tests?|refactor|compile)\b",
    "vision": r"\b(photo|photograph|picture|image|drawing|diagram|p&id)\b",
}

# The meaning vectors of the examples are computed once, then reused.
def _scores(question: str, query, examples: dict, candidates: list):
    """
    Steps 3 and 4 of route(), in ONE place, so the normal router and the
    evaluation's batch mode (route_batch) score a task in exactly the same way.
        meaning:  similarity to the role's CLOSEST example (0..1)
        keywords: + ROUTER_KEYWORD_BOOST if the role's keywords appear
    Returns (scores, keywords found per role).
    """
    if query is not None and examples:
        query = np.array(query, dtype=np.float32)
        query /= np.linalg.norm(query)
        scores = {role: float(np.max(vectors @ query)) for role, vectors in examples.items()}
    else:
        scores = {role: 0.0 for role in candidates}
    found = {}
    for role, pattern in KEYWORDS.items():
        if role in scores:
            words = sorted({m.lower() for m in re.findall(pattern, question, re.IGNORECASE)})
            if words:
                found[role] = words
                scores[role] += config.ROUTER_KEYWORD_BOOST
    return scores, found


async def route_batch(questions: list, batch_size: int = 64, use_trained: bool = None) -> list:
    """
    Phase 6.15: route MANY questions quickly (for the large evaluation sets).
    The questions are sent to the embedding model in batches of 64 instead
    of one at a time, which is much faster on a CPU. The scoring is the same
    _scores() as route(), for tasks with no attachment and no manual choice.
    Returns one {"intended_role", "scores", "vector"} per question, in order.
    use_trained: None = follow config.USE_TRAINED_ROUTER; False = rules only
    (the evaluation uses False to compare the old rules with the trained model).
    """
    candidates = [r for r in config.MODELS if r != "vision" and EXAMPLES.get(r)]
    examples = await _examples_for(candidates)
    results = []
    for start in range(0, len(questions), batch_size):
        chunk = questions[start:start + batch_size]
        vectors = await llm.embed([config.EMBED_QUERY_PREFIX + q for q in chunk])
        for question, vector in zip(chunk, vectors):
            p_code = trained.router_code_probability(vector, use_trained)
            if p_code is not None:      # Phase 6.16: the trained classifier, as in route()
                results.append({"intended_role": "code" if p_code >= 0.5 else "general",
                                "scores": {"general": round(1 - p_code, 3), "code": round(p_code, 3)},
                                "vector": vector})
                continue
            scores, _ = _scores(question, vector, examples, candidates)
            best = max(scores, key=scores.get) if any(scores.values()) else config.DEFAULT_ROLE
            results.append({"intended_role": best, "scores": {r: round(v, 3) for r, v in scores.items()},
                            "vector": vector})
    return results


_example_vectors = {}


async def _examples_for(roles: list) -> dict:
    """Meaning vectors for the example tasks of the given roles (cached)."""
    missing = [r for r in roles if r not in _example_vectors and EXAMPLES.get(r)]
    for role in missing:
        texts = [config.EMBED_QUERY_PREFIX + t for t in EXAMPLES[role]]
        vectors = np.array(await llm.embed(texts), dtype=np.float32)
        vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
        _example_vectors[role] = vectors
    return {r: _example_vectors[r] for r in roles if r in _example_vectors}


def _attached_images(attachment_ids: list) -> list:
    """File names of attached images (not PDFs or Word files)."""
    names = []
    for doc_id in attachment_ids:
        try:
            name = knowledge.attachment_document(doc_id)["summary"]["filename"]
        except Exception:
            continue
        if any(name.lower().endswith(ext) for ext in documents.IMAGE_EXTENSIONS):
            names.append(name)
    return names


def _attached_pdfs(attachment_ids: list) -> list:
    """File names of attached PDFs."""
    names = []
    for doc_id in attachment_ids:
        try:
            name = knowledge.attachment_document(doc_id)["summary"]["filename"]
        except Exception:
            continue
        if name.lower().endswith(".pdf"):
            names.append(name)
    return names


def _log(decision: dict, question: str) -> None:
    """Save the decision to data/router_log.jsonl. Never breaks routing."""
    try:
        config.ROUTER_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(config.ROUTER_LOG_FILE, "a", encoding="utf-8") as log:
            log.write(json.dumps({"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                  "question": question, **decision}, ensure_ascii=False) + "\n")
    except Exception:
        pass


async def route(question: str, attachment_ids: list, choice: str = "auto",
                log: bool = True, use_trained: bool = None) -> dict:
    """
    Decide which role handles this task. Returns:
        {"role": "code", "model": "qwen2.5-coder:3b", "method": "meaning",
         "reason": "...", "scores": {"general": 0.61, "code": 0.84}, "fallback": False}
    """
    roles = list(config.MODELS)
    installed = await llm.installed_models()
    decision = {"scores": {}, "fallback": False}

    def finish(role: str, method: str, reason: str) -> dict:
        # Remember the router's own choice BEFORE any fallback (Phase 6:
        # the evaluation measures the router itself, not which models are installed).
        decision["intended_role"] = role
        # ---- 5. FALLBACK: the chosen model must actually be installed ----
        name = config.MODELS[role]["name"]
        if role != config.DEFAULT_ROLE and not llm.is_installed(name, installed):
            decision["fallback"] = True
            reason += (f" — but {name} is not installed (run: ollama pull {name}), "
                       f"so the {config.DEFAULT_ROLE} model is used instead")
            role = config.DEFAULT_ROLE
        decision.update({"role": role, "model": config.MODELS[role]["name"],
                         "method": method, "reason": reason})
        if log:
            _log(decision, question)
        return decision

    # ---- 1. MANUAL CHOICE ----
    if choice and choice != "auto" and choice in config.MODELS:
        return finish(choice, "manual", "chosen manually in the model menu")

    # ---- 2. ATTACHMENT TYPE ----
    images = _attached_images(attachment_ids)
    if images and "vision" in config.MODELS:
        return finish("vision", "attachment", f"an image is attached ({images[0]})")
    # Phase 5: a PDF (e.g. a scanned drawing) plus a question about what it
    # LOOKS like ("drawing", "diagram", "photo", "P&ID") also goes to vision.
    pdfs = _attached_pdfs(attachment_ids)
    vision_words = sorted({m.lower() for m in re.findall(KEYWORDS["vision"], question, re.IGNORECASE)})
    if pdfs and vision_words and "vision" in config.MODELS:
        return finish("vision", "attachment+keywords",
                      f"asks about a {vision_words[0]} in {pdfs[0]}")

    # ---- 3. MEANING: compare the task with each role's examples ----
    candidates = [r for r in roles if r != "vision" and EXAMPLES.get(r)]
    try:
        examples = await _examples_for(candidates)
        query = (await llm.embed([config.EMBED_QUERY_PREFIX + question]))[0]
    except Exception as error:
        # No embedding model? Route by keywords only, and say so.
        examples, query = {}, None
        decision["note"] = f"meaning check unavailable ({error}); keywords only"

    # ---- 3b. TRAINED CLASSIFIER (Phase 6.16), if one has been trained ----
    p_code = (trained.router_code_probability(query, use_trained)
              if set(candidates) == {"general", "code"} else None)
    if p_code is not None:
        decision["scores"] = {"general": round(1 - p_code, 3), "code": round(p_code, 3)}
        role = "code" if p_code >= 0.5 else "general"
        return finish(role, "trained classifier",
                      f"trained router: {max(p_code, 1 - p_code):.0%} sure this is a {role} task")

    # ---- 4. KEYWORDS: a transparent nudge (inside _scores) ----
    scores, found = _scores(question, query, examples, candidates)

    decision["scores"] = {role: round(score, 3) for role, score in scores.items()}
    best = max(scores, key=scores.get) if scores else config.DEFAULT_ROLE
    if not any(scores.values()):
        return finish(config.DEFAULT_ROLE, "default", "no clear signal, so the general model")

    reason = f"closest to {best} tasks (score {scores[best]:.2f})"
    if best in found:
        reason += f"; keywords: {', '.join(found[best])}"
    return finish(best, "meaning+keywords" if best in found else "meaning", reason)
