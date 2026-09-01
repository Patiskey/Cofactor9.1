"""Conservative, auditable triage rules for Cofactor9.1."""

from __future__ import annotations

import re
from typing import Any


AA20 = frozenset("ACDEFGHIKLMNPQRSTVWY")


def classify_alphabet(sequence: str) -> dict[str, Any]:
    """Classify sequence symbols without treating selenocysteine as illegal."""

    symbols = sorted(set(sequence.upper()) - AA20)
    invalid = [symbol for symbol in symbols if symbol not in {"U", "X"}]
    reasons: list[str] = []
    if invalid:
        status = "INVALID"
    elif "X" in symbols:
        status = "HAS_X"
        reasons.append("UNKNOWN_RESIDUE_X")
        if "U" in symbols:
            reasons.append("SELENOCYSTEINE_U")
    elif "U" in symbols:
        status = "HAS_U"
        reasons.append("SELENOCYSTEINE_U")
    else:
        status = "AA20_ONLY"
    return {
        "status": status,
        "nonstandard_symbols": symbols,
        "reason_codes": sorted(reasons),
    }


FAMILY_PATTERNS = {
    "Mg": r"(?:Mg\s*\(?2\+\)?|magnesium)",
    "Mn": r"(?:Mn\s*\(?2\+\)?|manganese)",
    "Zn": r"(?:Zn\s*\(?2\+\)?|zinc)",
    "Fe2": r"(?:Fe\s*\(?2\+\)?|ferrous)",
    "Fe3": r"(?:Fe\s*\(?3\+\)?|ferric)",
    "Co": r"(?:Co\s*\(?(?:2|3)\+\)?|cobalt)",
    "Ca": r"(?:Ca\s*\(?2\+\)?|calcium)",
    "Cu": r"(?:Cu\s*\(?(?:1|2)?\+\)?|copper)",
    "Ni": r"(?:\bNi\s*\(?(?:1|2|3)\+\)?|\bnickel\b)",
    "K": r"(?:\bK\s*\(?\+\)?|potassium)",
    "Na": r"(?:Na\s*\(?\+\)?|sodium)",
    "NH4": r"(?:NH4\s*\(?\+\)?|ammonium)",
    "Cd": r"(?:Cd\s*\(?2\+\)?|cadmium)",
    "Sr": r"(?:Sr\s*\(?2\+\)?|strontium)",
    "Ba": r"(?:Ba\s*\(?2\+\)?|barium)",
    "Pb": r"(?:Pb\s*\(?2\+\)?|lead\(2\+\))",
    "lanthanide": r"(?:(?:La|Ce|Pr|Nd|Sm|Eu)\s*\(?3\+\)?|lanthanide)",
    "FAD": r"\b(?:FAD|flavin adenine dinucleotide|mFAD)\b",
    "FMN": r"\b(?:FMN|FMNH2|FMNH\(2\)|flavin mononucleotide)\b",
    "riboflavin": r"\briboflavin\b",
    "PLP": r"\b(?:PLP|pyridoxal(?: 5.?-)?phosphate)\b",
    "NAD": r"\b(?:NAD\(\+\)|NAD\+|NADH)\b",
    "NADP": r"\b(?:NADP\(\+\)|NADP\+|NADPH)\b",
    "FeS2": r"\[2Fe-2S\]",
    "FeS3": r"\[3Fe-4S\]",
    "FeS4": r"\[4Fe-4S\]",
    "NiFeS": r"\[Ni-Fe-S\]",
}
COMPILED_FAMILIES = {
    family: re.compile(pattern, re.IGNORECASE)
    for family, pattern in FAMILY_PATTERNS.items()
}

GOLD_FAMILY = {
    "CHEBI:18420": "Mg",
    "CHEBI:29035": "Mn",
    "CHEBI:29105": "Zn",
    "CHEBI:29033": "Fe2",
    "CHEBI:29034": "Fe3",
    "CHEBI:48828": "Co",
    "CHEBI:49415": "Co",
    "CHEBI:29108": "Ca",
    "CHEBI:29036": "Cu",
    "CHEBI:49552": "Cu",
    "CHEBI:49786": "Ni",
    "CHEBI:29103": "K",
    "CHEBI:29101": "Na",
    "CHEBI:57692": "FAD",
    "CHEBI:60470": "FAD",
    "CHEBI:58210": "FMN",
    "CHEBI:57618": "FMN",
    "CHEBI:87746": "FMN",
    "CHEBI:57986": "riboflavin",
    "CHEBI:597326": "PLP",
    "CHEBI:57540": "NAD",
    "CHEBI:57945": "NAD",
    "CHEBI:58349": "NADP",
    "CHEBI:57783": "NADP",
    "CHEBI:190135": "FeS2",
    "CHEBI:21137": "FeS3",
    "CHEBI:49883": "FeS4",
    "CHEBI:60400": "NiFeS",
}

FAMILY_GROUPS = (
    frozenset(
        {
            "Mg", "Mn", "Zn", "Fe2", "Fe3", "Co", "Ca", "Cu", "Ni",
            "K", "Na", "NH4", "Cd", "Sr", "Ba", "Pb", "lanthanide",
        }
    ),
    frozenset({"FAD", "FMN", "riboflavin"}),
    frozenset({"NAD", "NADP"}),
    frozenset({"FeS2", "FeS3", "FeS4", "NiFeS"}),
)

RISK_PATTERNS = {
    "NOTE_ALTERNATIVE_OR_COMPARISON": re.compile(
        r"\b(?:or|either|and/or|alternative\w*|also|various|other|both|"
        r"followed by|instead of|rather than|whereas|however|but)\b",
        re.IGNORECASE,
    ),
    "NOTE_PREFERENCE_OR_PARTIAL_ACTIVITY": re.compile(
        r"\b(?:prefer\w*|optimum|optimal|best activity|highest activity|"
        r"more active|less active|lower efficiency|poor(?:er)? "
        r"(?:cofactor|substrate)|weak activity|low activity|support\w* "
        r"(?:the )?(?:activity|cleavage)|accept\w*|substitut\w*|replac\w*|"
        r"can use|use .* as (?:a )?cofactor)\b",
        re.IGNORECASE,
    ),
    "NOTE_UNCERTAINTY": re.compile(
        r"\b(?:may|might|probably|probable|possibly|potential\w*|appears?|"
        r"seems?|suggest\w*|likely|unclear|not clear|under debate|presum\w*|"
        r"putative|could be|unlikely)\b",
        re.IGNORECASE,
    ),
    "NOTE_CONDITIONAL_OR_ACTIVITY_SCOPE": re.compile(
        r"\b(?:in vitro|in vivo|physiolog\w*|growth conditions?|"
        r"storage conditions?|under (?:aerobic|anaerobic|acidic|neutral)|"
        r"depending on|depends on|at high concentrations?|"
        r"at low concentrations?|for .* activity|during .* activity|"
        r"different activit\w*)\b",
        re.IGNORECASE,
    ),
    "NOTE_NEGATION_OR_INHIBITION": re.compile(
        r"\b(?:no cofactor|cofactor[- ]independent|does not|do not|not required|"
        r"not essential|cannot|unable|absence of|inactive|inhibit\w*|fails? to)\b",
        re.IGNORECASE,
    ),
    "NOTE_STATE_CHANGE_OR_HISTORY": re.compile(
        r"\b(?:oxidiz\w* to|convert\w* to|reconstitut\w*|degrad\w* to|"
        r"lost upon|originally|previously|thought to|reported to)\b",
        re.IGNORECASE,
    ),
    "NOTE_GENERIC_LABEL_SPECIFIC_MENTIONS": re.compile(
        r"\b(?:(?:di|mono)valent metal (?:ion|cation)s?|"
        r"metal (?:ion|cation)s?)\b",
        re.IGNORECASE,
    ),
}


def triage_record(
    *,
    label_id: str,
    note: str,
    molecule: str | None = None,
    is_ancestor_target: bool = False,
) -> dict[str, Any]:
    """Return high-recall review signals; never make an exclusion decision."""

    reasons: set[str] = set()
    if is_ancestor_target:
        reasons.add("ONTOLOGY_ANCESTOR_TARGET")
    if molecule:
        reasons.add("MOLECULE_SCOPE")

    if note:
        for code, pattern in RISK_PATTERNS.items():
            if pattern.search(note):
                reasons.add(code)

        mentions = {
            family
            for family, pattern in COMPILED_FAMILIES.items()
            if pattern.search(note)
        }
        gold = GOLD_FAMILY.get(label_id)
        group = next((item for item in FAMILY_GROUPS if gold in item), frozenset())
        if gold and (mentions & group) - {gold}:
            reasons.add("NOTE_OTHER_COFACTOR_MENTION")

    ordered = sorted(reasons)
    return {
        "adjudication_status": "PENDING" if ordered else "NOT_REQUIRED",
        "reason_codes": ordered,
    }
