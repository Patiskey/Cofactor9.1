"""Leakage-safe construction of sequence-only model prompts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
import re
import secrets


CATALOG_SIZE = 104
PROMPT_VERSION = "cofactor9.1.sequence-only.named-catalog.v2"

_CASE_FIELDS = frozenset({"sample_id", "sequence", "label_catalog"})
_CATALOG_FIELDS = frozenset({"version", "terms"})
_TERM_FIELDS = frozenset({"chebi_id", "name"})
_SAMPLE_ID = re.compile(r"sample_[0-9a-f]{32}\Z")
_SEQUENCE = re.compile(r"[A-Z]+\Z")
_CHEBI_ID = re.compile(r"CHEBI:[1-9][0-9]*\Z")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


class PromptValidationError(ValueError):
    """Raised when a prompt case is not exactly sequence-only and valid."""


@dataclass(frozen=True, slots=True)
class CatalogTerm:
    """One frozen ChEBI class definition exposed identically to every case."""

    chebi_id: str
    name: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.chebi_id, str)
            or _CHEBI_ID.fullmatch(self.chebi_id) is None
        ):
            raise PromptValidationError("catalog term has an invalid CHEBI identifier")
        if (
            not isinstance(self.name, str)
            or not self.name
            or len(self.name) > 256
            or any(ord(character) < 32 for character in self.name)
        ):
            raise PromptValidationError("catalog term has an invalid frozen name")

    def to_dict(self) -> dict[str, str]:
        return {"chebi_id": self.chebi_id, "name": self.name}


@dataclass(frozen=True, slots=True)
class PromptCase:
    """The complete and closed per-sample payload exposed to the model."""

    sample_id: str
    sequence: str
    catalog_version: str
    catalog_terms: tuple[CatalogTerm, ...]

    def __post_init__(self) -> None:
        _validate_values(
            sample_id=self.sample_id,
            sequence=self.sequence,
            catalog_version=self.catalog_version,
            catalog_terms=self.catalog_terms,
        )

    def to_payload(self) -> dict[str, object]:
        """Return the canonical JSON-safe case payload."""

        return {
            "sample_id": self.sample_id,
            "sequence": self.sequence,
            "label_catalog": {
                "version": self.catalog_version,
                "terms": [term.to_dict() for term in self.catalog_terms],
            },
        }

    @property
    def allowed_labels(self) -> tuple[str, ...]:
        """Return the exact closed response vocabulary."""

        return tuple(term.chebi_id for term in self.catalog_terms)

    @classmethod
    def from_payload(cls, payload: object) -> PromptCase:
        """Validate a decoded closed payload without coercion or repair."""

        return validate_prompt_payload(payload)


def _field_names(value: Mapping[object, object]) -> frozenset[object]:
    return frozenset(value.keys())


def _require_exact_fields(
    value: Mapping[object, object],
    expected: frozenset[str],
    *,
    location: str,
) -> None:
    actual = _field_names(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(repr(field) for field in actual - expected)
        raise PromptValidationError(
            f"{location} fields do not match contract; "
            f"missing={missing}, extra={extra}"
        )


def _validate_values(
    *,
    sample_id: object,
    sequence: object,
    catalog_version: object,
    catalog_terms: object,
) -> None:
    if not isinstance(sample_id, str) or _SAMPLE_ID.fullmatch(sample_id) is None:
        raise PromptValidationError("sample_id must be a random opaque sample identifier")
    if not isinstance(sequence, str) or _SEQUENCE.fullmatch(sequence) is None:
        raise PromptValidationError("sequence must be a nonempty uppercase amino-acid string")
    if (
        not isinstance(catalog_version, str)
        or _VERSION.fullmatch(catalog_version) is None
    ):
        raise PromptValidationError("catalog version must be a safe nonempty identifier")
    if (
        isinstance(catalog_terms, (str, bytes))
        or not isinstance(catalog_terms, Sequence)
    ):
        raise PromptValidationError("catalog terms must be an ordered sequence")
    if len(catalog_terms) != CATALOG_SIZE:
        raise PromptValidationError(
            f"catalog must contain exactly {CATALOG_SIZE} terms"
        )

    if any(not isinstance(term, CatalogTerm) for term in catalog_terms):
        raise PromptValidationError("catalog terms must be validated CatalogTerm values")
    identifiers = tuple(term.chebi_id for term in catalog_terms)
    names = tuple(term.name for term in catalog_terms)
    if len(set(identifiers)) != len(identifiers):
        raise PromptValidationError("catalog contains duplicate CHEBI identifiers")
    if len(set(names)) != len(names):
        raise PromptValidationError("catalog contains duplicate frozen names")
    numeric_order = tuple(
        sorted(identifiers, key=lambda value: int(value.split(":", 1)[1]))
    )
    if identifiers != numeric_order:
        raise PromptValidationError("catalog terms must use canonical numeric CHEBI order")


def create_prompt_case(
    *,
    sequence: str,
    catalog_terms: Sequence[CatalogTerm],
    catalog_version: str,
) -> PromptCase:
    """Create a validated case with a cryptographically random opaque ID."""

    return PromptCase(
        sample_id=f"sample_{secrets.token_hex(16)}",
        sequence=sequence,
        catalog_version=catalog_version,
        catalog_terms=tuple(catalog_terms),
    )


def validate_prompt_payload(payload: object) -> PromptCase:
    """Accept only the closed sequence/catalog payload and reject metadata."""

    if not isinstance(payload, Mapping):
        raise PromptValidationError("prompt case must be a JSON object")
    _require_exact_fields(payload, _CASE_FIELDS, location="prompt case")

    catalog = payload["label_catalog"]
    if not isinstance(catalog, Mapping):
        raise PromptValidationError("label_catalog must be a JSON object")
    _require_exact_fields(catalog, _CATALOG_FIELDS, location="label_catalog")

    raw_terms = catalog["terms"]
    if not isinstance(raw_terms, list):
        raise PromptValidationError("label_catalog.terms must be a JSON list")
    terms: list[CatalogTerm] = []
    for index, raw_term in enumerate(raw_terms):
        if not isinstance(raw_term, Mapping):
            raise PromptValidationError(f"catalog term {index} must be an object")
        _require_exact_fields(raw_term, _TERM_FIELDS, location=f"catalog term {index}")
        terms.append(CatalogTerm(raw_term["chebi_id"], raw_term["name"]))
    return PromptCase(
        sample_id=payload["sample_id"],
        sequence=payload["sequence"],
        catalog_version=catalog["version"],
        catalog_terms=tuple(terms),
    )


def render_prompt(case: PromptCase) -> str:
    """Render one deterministic prompt whose only case data is ``case``."""

    payload = json.dumps(
        case.to_payload(),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return (
        f"Protocol: {PROMPT_VERSION}\n"
        "Predict the UniProt-style COFACTOR annotations applicable to this protein "
        "using only the supplied amino-acid sequence and frozen ChEBI class "
        "definitions. Do not call tools or use outside data. Do not emit ordinary "
        "substrates, products, or non-cofactor ligands. Catalog order is arbitrary "
        "and conveys no frequency or likelihood.\n"
        "Return exactly one JSON object with these fields and no others:\n"
        '- "schema_version": exactly "cofactor9.1.response.v2"\n'
        '- "sample_id": copy the supplied sample_id exactly\n'
        '- "status": "predict" when confidence_complete is at least 0.5, otherwise "abstain"\n'
        '- "predicted_cofactors": a nonempty, unordered set-like JSON list of unique catalog CHEBI IDs you simultaneously assert; this is not a ranked top-k list\n'
        '- "primary_guess": one catalog CHEBI ID that is a member of predicted_cofactors and is your single best class for conventional top-1 evaluation\n'
        '- "confidence_complete": your probability from 0 through 1 that the entire predicted_cofactors set is record-exact, with no missing or extra cofactor\n'
        "For distinct jointly required cofactors (AND), include one prediction for "
        "each. For interchangeable alternatives serving one role (OR), include only "
        "the single most likely alternative. Even when abstaining, provide the best "
        "available joint set and primary guess. Emit no Markdown or prose.\n"
        "BEGIN_CASE_JSON\n"
        f"{payload}\n"
        "END_CASE_JSON\n"
    )


__all__ = [
    "CATALOG_SIZE",
    "CatalogTerm",
    "PROMPT_VERSION",
    "PromptCase",
    "PromptValidationError",
    "create_prompt_case",
    "render_prompt",
    "validate_prompt_payload",
]
