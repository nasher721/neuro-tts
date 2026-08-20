"""Pure text normalization that turns written clinical prose into speakable text.

``tts_core.source_text`` produces the *stable* text used for content digests: it
strips HTML and add-on metadata but is deliberately conservative, because any
change to it invalidates every existing marker in the collection.  This module
runs later, at synthesis time only, and is free to rewrite aggressively:
symbols, dose shorthand, and neuro-critical-care abbreviations that a TTS engine
would otherwise spell out one letter at a time.

Because the output only affects the audio and never the marker, the active
normalization settings are folded into the synthesis profile digest by
``main._synthesis_profile_digest`` so that changing them re-queues generation.

Public API
----------
normalize_for_speech(text, *, expand_abbreviations=True, extra_replacements=None)
split_for_synthesis(text, max_chars)
"""

from __future__ import annotations

import re

# ── Anki markup that survives HTML stripping ────────────────────────────────
# ``{{c1::answer}}`` / ``{{c1::answer::hint}}`` — keep the answer, drop the hint.
CLOZE_RE = re.compile(r"\{\{c\d+::(.*?)(?:::.*?)?\}\}", re.S)
# Any other Anki field replacement, e.g. ``{{type:Front}}`` or ``{{Extra}}``.
FIELD_RE = re.compile(r"\{\{[^{}]*\}\}")

# ── symbols ─────────────────────────────────────────────────────────────────
# Order matters: multi-character sequences are replaced before their prefixes.
SYMBOL_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    ("<->", " to "),
    ("-->", " leads to "),
    ("->", " leads to "),
    ("<-", " comes from "),
    ("=>", " therefore "),
    ("↔", " to "),
    ("→", " leads to "),
    ("←", " comes from "),
    ("⇒", " therefore "),
    ("↑↑", " markedly increased "),
    ("↓↓", " markedly decreased "),
    ("↑", " increased "),
    ("↓", " decreased "),
    ("≈", " approximately "),
    ("~", " approximately "),
    ("≥", " greater than or equal to "),
    ("≤", " less than or equal to "),
    ("≠", " not equal to "),
    ("±", " plus or minus "),
    ("°C", " degrees Celsius "),
    ("°F", " degrees Fahrenheit "),
    ("º", " degrees "),
    ("°", " degrees "),
    ("µ", "micro"),
    ("×", " times "),
    ("&", " and "),
    ("+/-", " plus or minus "),
    ("+", " plus "),
    ("=", " equals "),
)

# ── clinical shorthand ──────────────────────────────────────────────────────
# Matched case-sensitively as whole words unless listed in _CASE_INSENSITIVE.
ABBREVIATIONS: dict[str, str] = {
    # general clinical shorthand
    "w/": "with",
    "w/o": "without",
    "s/p": "status post",
    "h/o": "history of",
    "r/o": "rule out",
    "c/w": "consistent with",
    "b/l": "bilateral",
    "f/u": "follow up",
    "d/c": "discontinue",
    "n/v": "nausea and vomiting",
    "y/o": "year old",
    "pt": "patient",
    "pts": "patients",
    "hx": "history",
    "dx": "diagnosis",
    "ddx": "differential diagnosis",
    "tx": "treatment",
    "rx": "prescription",
    "sx": "symptoms",
    "fx": "fracture",
    "px": "prognosis",
    "abx": "antibiotics",
    "yrs": "years",
    "wks": "weeks",
    "hrs": "hours",
    # routes and frequencies
    "PO": "by mouth",
    "IV": "intravenous",
    "IM": "intramuscular",
    "SQ": "subcutaneous",
    "SC": "subcutaneous",
    "NPO": "nothing by mouth",
    "PRN": "as needed",
    "BID": "twice daily",
    "TID": "three times daily",
    "QID": "four times daily",
    "QD": "once daily",
    "QHS": "at bedtime",
    "gtt": "drip",
    # neurology and neurocritical care
    "ICP": "intracranial pressure",
    "CPP": "cerebral perfusion pressure",
    "MAP": "mean arterial pressure",
    "CBF": "cerebral blood flow",
    "GCS": "Glasgow Coma Scale",
    "NIHSS": "N I H Stroke Scale",
    "mRS": "modified Rankin Scale",
    "SAH": "subarachnoid hemorrhage",
    "ICH": "intracerebral hemorrhage",
    "IVH": "intraventricular hemorrhage",
    "SDH": "subdural hematoma",
    "EDH": "epidural hematoma",
    "TBI": "traumatic brain injury",
    "DAI": "diffuse axonal injury",
    "EVD": "external ventricular drain",
    "CSF": "cerebrospinal fluid",
    "AVM": "arteriovenous malformation",
    "MCA": "middle cerebral artery",
    "ACA": "anterior cerebral artery",
    "PCA": "posterior cerebral artery",
    "ICA": "internal carotid artery",
    "PCOM": "posterior communicating artery",
    "ACOM": "anterior communicating artery",
    "DCI": "delayed cerebral ischemia",
    "TCD": "transcranial Doppler",
    "EEG": "E E G",
    "cEEG": "continuous E E G",
    "NCSE": "nonconvulsive status epilepticus",
    "SE": "status epilepticus",
    "AED": "antiseizure medication",
    "ASM": "antiseizure medication",
    "LP": "lumbar puncture",
    "tPA": "tissue plasminogen activator",
    "EVT": "endovascular thrombectomy",
    "TIA": "transient ischemic attack",
    "CVT": "cerebral venous thrombosis",
    "PRES": "posterior reversible encephalopathy syndrome",
    "RCVS": "reversible cerebral vasoconstriction syndrome",
    "GBS": "Guillain Barre syndrome",
    "MG": "myasthenia gravis",
    "ALS": "amyotrophic lateral sclerosis",
    "MS": "multiple sclerosis",
    "NMO": "neuromyelitis optica",
    # imaging
    "CTA": "C T angiogram",
    "CTP": "C T perfusion",
    "MRA": "M R angiogram",
    "MRV": "M R venogram",
    "DWI": "diffusion weighted imaging",
    "ADC": "apparent diffusion coefficient",
    "FLAIR": "flair",
    "GRE": "gradient echo",
    "SWI": "susceptibility weighted imaging",
    "DSA": "digital subtraction angiography",
    # systemic critical care
    "ICU": "I C U",
    "ETT": "endotracheal tube",
    "PEEP": "peep",
    "ARDS": "A R D S",
    "AKI": "acute kidney injury",
    "DVT": "deep vein thrombosis",
    "PE": "pulmonary embolism",
    "VTE": "venous thromboembolism",
    "DIC": "disseminated intravascular coagulation",
    "SIADH": "syndrome of inappropriate antidiuretic hormone",
    "CSW": "cerebral salt wasting",
    "DI": "diabetes insipidus",
    "HTN": "hypertension",
    "DM": "diabetes mellitus",
    "CAD": "coronary artery disease",
    "CHF": "congestive heart failure",
    "COPD": "C O P D",
    "AF": "atrial fibrillation",
    "SBP": "systolic blood pressure",
    "DBP": "diastolic blood pressure",
    "HR": "heart rate",
    "RR": "respiratory rate",
    "CBC": "complete blood count",
    "BMP": "basic metabolic panel",
    "INR": "I N R",
    "PTT": "P T T",
}

# Abbreviations whose meaning does not depend on capitalization.  Everything
# else is matched exactly so that "MS" (multiple sclerosis) does not swallow the
# unit "ms" (milliseconds), and "SE" does not rewrite ordinary prose.
_CASE_INSENSITIVE = frozenset(
    {
        "w/", "w/o", "s/p", "h/o", "r/o", "c/w", "b/l", "f/u", "d/c", "n/v",
        "y/o", "pt", "pts", "hx", "dx", "ddx", "tx", "rx", "sx", "fx", "px",
        "abx", "yrs", "wks", "hrs", "prn", "bid", "tid", "qid", "qd", "qhs",
    }
)

# ── dose and unit shorthand ─────────────────────────────────────────────────
UNITS: dict[str, str] = {
    "g": "grams",
    "mg": "milligrams",
    "mcg": "micrograms",
    "ug": "micrograms",
    "kg": "kilograms",
    "mL": "milliliters",
    "ml": "milliliters",
    "L": "liters",
    "mmHg": "millimeters of mercury",
    "cmH2O": "centimeters of water",
    "mEq": "milliequivalents",
    "mmol": "millimoles",
    "mOsm": "milliosmoles",
    "ms": "milliseconds",
    "sec": "seconds",
    "min": "minutes",
    "hr": "hours",
}

# ``5mg`` / ``0.5 mg/kg`` — a number, then one or two slash-joined units.
_UNIT_ALTERNATION = "|".join(sorted((re.escape(u) for u in UNITS), key=len, reverse=True))
DOSE_RE = re.compile(
    rf"(?<![A-Za-z0-9])(\d+(?:\.\d+)?)\s*({_UNIT_ALTERNATION})(?:/({_UNIT_ALTERNATION}))?(?![A-Za-z0-9])"
)

# ``q6h`` / ``q4-6h`` / ``q12hr`` — dosing interval shorthand.
INTERVAL_RE = re.compile(r"(?<![A-Za-z0-9])[qQ](\d+)(?:\s*-\s*(\d+))?\s*(?:h|hr|hrs)(?![A-Za-z0-9])")

# ``x 21 days`` / ``x3 weeks`` — duration shorthand, not the letter x.
MULTIPLIER_RE = re.compile(r"(?<![A-Za-z0-9])[xX]\s*(\d+\s*(?:day|days|week|weeks|hour|hours|month|months|dose|doses))(?![A-Za-z0-9])")

# ``5-10`` between digits is a range, not a subtraction or a hyphenated word.
RANGE_RE = re.compile(r"(?<=\d)\s*-\s*(?=\d)")

# ``1/2`` style fractions read badly as "one slash two".
FRACTION_RE = re.compile(r"(?<![A-Za-z0-9/])(\d+)\s*/\s*(\d+)(?![A-Za-z0-9/])")

SPACE_RE = re.compile(r"\s+")
# Collapse punctuation runs left behind by symbol substitution (". ," -> ".").
PUNCT_SPACE_RE = re.compile(r"\s+([,.;:!?])")
REPEAT_PUNCT_RE = re.compile(r"([,.;:])(?:\s*\1)+")

# Sentence break used by :func:`split_for_synthesis`.
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


def _abbreviation_pattern(term: str) -> re.Pattern[str]:
    """Whole-word matcher tolerant of the ``/`` inside shorthand like ``s/p``."""
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])")


# Longest-first so that "w/o" is expanded before "w/", and "ddx" before "dx".
_ABBREVIATION_MATCHERS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (
        re.compile(
            _abbreviation_pattern(term).pattern,
            re.I if term.lower() in _CASE_INSENSITIVE else 0,
        ),
        expansion,
    )
    for term, expansion in sorted(ABBREVIATIONS.items(), key=lambda item: len(item[0]), reverse=True)
)


def _expand_units(match: re.Match[str]) -> str:
    amount, unit, per_unit = match.group(1), match.group(2), match.group(3)
    spoken = f"{amount} {UNITS[unit]}"
    if per_unit:
        spoken += f" per {UNITS[per_unit].rstrip('s')}"
    return spoken


def _expand_interval(match: re.Match[str]) -> str:
    low, high = match.group(1), match.group(2)
    if high:
        return f" every {low} to {high} hours "
    return f" every {low} hour{'s' if low != '1' else ''} "


def _tidy(text: str) -> str:
    text = SPACE_RE.sub(" ", text)
    text = PUNCT_SPACE_RE.sub(r"\1", text)
    text = REPEAT_PUNCT_RE.sub(r"\1", text)
    return text.strip()


def normalize_for_speech(
    text: str,
    *,
    expand_abbreviations: bool = True,
    extra_replacements: dict[str, str] | None = None,
) -> str:
    """Return *text* rewritten so a TTS engine reads it the way a clinician would.

    *extra_replacements* is applied first and as a plain whole-word substitution,
    so users can override or extend the built-in table from ``config.json``
    without editing the add-on.
    """
    if not text:
        return ""

    value = CLOZE_RE.sub(r"\1", text)
    if "{{" in value:
        value = FIELD_RE.sub(" ", value)

    if extra_replacements:
        for term, expansion in sorted(extra_replacements.items(), key=lambda i: len(i[0]), reverse=True):
            if not term:
                continue
            value = _abbreviation_pattern(term).sub(str(expansion), value)

    # Ranges and fractions run before symbol replacement so that the hyphen and
    # slash are consumed as numeric punctuation rather than left as stray marks.
    value = INTERVAL_RE.sub(_expand_interval, value)
    value = MULTIPLIER_RE.sub(r" for \1 ", value)
    value = RANGE_RE.sub(" to ", value)

    if expand_abbreviations:
        for matcher, expansion in _ABBREVIATION_MATCHERS:
            value = matcher.sub(expansion, value)

    value = DOSE_RE.sub(_expand_units, value)
    value = FRACTION_RE.sub(r"\1 out of \2", value)

    # Comparison operators only read as words when they qualify a number.
    value = re.sub(r"<\s*=\s*(?=[\d.])", " less than or equal to ", value)
    value = re.sub(r">\s*=\s*(?=[\d.])", " greater than or equal to ", value)
    value = re.sub(r"<\s*(?=[\d.])", " less than ", value)
    value = re.sub(r">\s*(?=[\d.])", " greater than ", value)

    for symbol, spoken in SYMBOL_REPLACEMENTS:
        if symbol in value:
            value = value.replace(symbol, spoken)

    # A lone "%" reads as "percent" only after a number; elsewhere it is noise.
    value = re.sub(r"(?<=\d)\s*%", " percent", value)
    value = value.replace("%", " percent ")

    return _tidy(value)


def split_for_synthesis(text: str, max_chars: int = 800) -> list[str]:
    """Split *text* into synthesis chunks of at most *max_chars* characters.

    F5-TTS degrades and can run out of memory on very long single utterances, so
    long explanations are synthesized in pieces and concatenated.  Splits prefer
    sentence boundaries, fall back to clause boundaries, and only ever break
    mid-word when a single "sentence" exceeds the limit on its own.
    """
    text = (text or "").strip()
    if not text:
        return []
    if max_chars <= 0 or len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    current = ""
    for sentence in SENTENCE_RE.split(text):
        sentence = sentence.strip()
        if not sentence:
            continue
        for piece in _fit(sentence, max_chars):
            if not current:
                current = piece
            elif len(current) + 1 + len(piece) <= max_chars:
                current = f"{current} {piece}"
            else:
                chunks.append(current)
                current = piece
    if current:
        chunks.append(current)
    return chunks


def _fit(sentence: str, max_chars: int) -> list[str]:
    """Break one over-long sentence at clause boundaries, then at whitespace."""
    if len(sentence) <= max_chars:
        return [sentence]

    pieces: list[str] = []
    current = ""
    for clause in re.split(r"(?<=[,;:])\s+", sentence):
        if len(current) + len(clause) + 1 <= max_chars:
            current = f"{current} {clause}".strip()
            continue
        if current:
            pieces.append(current)
        current = clause if len(clause) <= max_chars else ""
        if not current:
            pieces.extend(_split_words(clause, max_chars))
    if current:
        pieces.append(current)
    return [piece for piece in pieces if piece]


def _split_words(clause: str, max_chars: int) -> list[str]:
    pieces: list[str] = []
    current = ""
    for word in clause.split():
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= max_chars:
            current = f"{current} {word}"
        else:
            pieces.append(current)
            current = word
        while len(current) > max_chars:  # a single unbroken token
            pieces.append(current[:max_chars])
            current = current[max_chars:]
    if current:
        pieces.append(current)
    return pieces
