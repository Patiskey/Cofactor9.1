"""Leakage-safe construction of sequence-only model prompts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
import re
import secrets


CATALOG_SIZE = 104
PROMPT_VERSION = "cofactor9.1.sequence-only.v1"

_CASE_FIELDS = frozenset({"sample_id", "sequence", "label_catalog"})
_CATALOG_FIELDS = frozenset({"version", "labels"})
_SAMPLE_ID = re.compile(r"sample_[0-9a-f]{32}\Z")
_SEQUENCE = re.compile(r"[A-Z]+\Z")
_CHEBI_ID = re.compile(r"CHEBI:[1-9][0-9]*\Z")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


class PromptValidationError(ValueError):
    """Raised when a prompt case is not exactly sequence-only and valid."""


@dataclass(frozen=True, slots=True)
class PromptCase:
    """The complete and closed per-sample payload exposed to the model."""

    sample_id: str
    sequence: str
    catalog_version: str
    allowed_labels: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_values(
            sample_id=self.sample_id,
            sequence=self.sequence,
            catalog_version=self.catalog_version,
            allowed_labels=self.allowed_labels,
        )

    def to_payload(self) -> dict[str, object]:
        """Return the canonical JSON-safe case payload."""

        return {
            "sample_id": self.sample_id,
            "sequence": self.sequence,
            "label_catalog": {
                "version": self.catalog_version,
                "labels": list(self.allowed_labels),
            },
        }

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
    allowed_labels: object,
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
        isinstance(allowed_labels, (str, bytes))
        or not isinstance(allowed_labels, Sequence)
    ):
        raise PromptValidationError("catalog labels must be an ordered sequence")
    if len(allowed_labels) != CATALOG_SIZE:
        raise PromptValidationError(
            f"catalog must contain exactly {CATALOG_SIZE} labels"
        )

    seen: set[str] = set()
    for index, label in enumerate(allowed_labels):
        if not isinstance(label, str) or _CHEBI_ID.fullmatch(label) is None:
            raise PromptValidationError(
                f"catalog label {index} must be a canonical CHEBI identifier"
            )
        if label in seen:
            raise PromptValidationError(f"catalog contains duplicate label {label!r}")
        seen.add(label)


def create_prompt_case(
    *,
    sequence: str,
    allowed_labels: Sequence[str],
    catalog_version: str,
) -> PromptCase:
    """Create a validated case with a cryptographically random opaque ID."""

    return PromptCase(
        sample_id=f"sample_{secrets.token_hex(16)}",
        sequence=sequence,
        catalog_version=catalog_version,
        allowed_labels=tuple(allowed_labels),
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

    labels = catalog["labels"]
    if not isinstance(labels, list):
        raise PromptValidationError("label_catalog.labels must be a JSON list")
    return PromptCase(
        sample_id=payload["sample_id"],
        sequence=payload["sequence"],
        catalog_version=catalog["version"],
        allowed_labels=tuple(labels),
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
        "Annotate the protein using only the supplied amino-acid sequence and "
        "frozen allowed-label catalog. Do not call tools or use outside data.\n"
        "Return exactly one JSON object with these fields and no others:\n"
        '- "schema_version": exactly "cofactor9.1.response.v1"\n'
        '- "sample_id": copy the supplied sample_id exactly\n'
        '- "status": either "predict" or "abstain"\n'
        '- "best_guess": a nonempty list of unique catalog CHEBI IDs, most likely first\n'
        '- "confidence_complete": a number from 0 through 1\n'
        "Use status abstain when confidence is insufficient, but still provide the "
        "best available nonempty best_guess. Emit no Markdown or prose.\n"
        "BEGIN_CASE_JSON\n"
        f"{payload}\n"
        "END_CASE_JSON\n"
    )


__all__ = [
    "CATALOG_SIZE",
    "PROMPT_VERSION",
    "PromptCase",
    "PromptValidationError",
    "create_prompt_case",
    "render_prompt",
    "validate_prompt_payload",
]
