"""Presidio behind the `Recogniser` protocol, for the entity types the built-in patterns
cannot find: people, places, organisations.

Optional. `presidio-analyzer` and a spaCy model are installed with the `redact` extra and
a `python -m spacy download` step (docs/redact.md); nothing in `boundary.redact` imports
them unless this class is constructed without an engine. The adapter accepts any object
with Presidio's `analyze(text=, language=, entities=)` shape so that its mapping is tested
without the download.

What the adapter does not have to do is cut a span at a line break. The analyzer does that
to every span from every recogniser (analyzer.py), which is where the guarantee belongs.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from boundary.errors import ConfigError
from boundary.redact.types import EntityType, Span

# Presidio's names for the types this library reports. Anything else Presidio finds
# (DATE_TIME, NRP, CREDIT_CARD, ...) is dropped here: the vocabulary is closed, and a type
# a consumer cannot switch on is worse than a miss it can measure.
ENTITY_MAP: dict[str, EntityType] = {
    "PERSON": EntityType.PERSON,
    "EMAIL_ADDRESS": EntityType.EMAIL,
    "PHONE_NUMBER": EntityType.PHONE,
    "LOCATION": EntityType.LOCATION,
    "ORGANIZATION": EntityType.ORGANISATION,
    "URL": EntityType.URL,
}


class PresidioLike(Protocol):
    """The one method of `presidio_analyzer.AnalyzerEngine` this adapter calls."""

    def analyze(self, *, text: str, language: str, entities: list[str]) -> Sequence[Any]: ...


class PresidioRecogniser:
    """Presidio's analyzer as one recogniser. Each span names the Presidio recogniser that
    fired (`presidio:SpacyRecognizer`), from the result's recognition metadata, so a miss
    is attributable to a component and not to "Presidio"."""

    id = "presidio"

    def __init__(
        self,
        engine: PresidioLike | None = None,
        *,
        language: str = "en",
        entities: Sequence[str] = tuple(ENTITY_MAP),
        nlp_configuration: dict[str, Any] | None = None,
    ) -> None:
        """`engine` is any Presidio-shaped analyzer; without one, the default is built from
        `nlp_configuration` (default `DEFAULT_NLP_CONFIGURATION`), which names the model and
        maps its labels explicitly so that organisations are actually requested."""
        self.language = language
        self.entities = [e for e in entities if e in ENTITY_MAP]
        self._engine: PresidioLike = (
            engine if engine is not None else _default_engine(nlp_configuration)
        )

    def analyze(self, text: str, page: int) -> Sequence[Span]:
        out: list[Span] = []
        for r in self._engine.analyze(text=text, language=self.language, entities=self.entities):
            kind = ENTITY_MAP.get(str(r.entity_type))
            if kind is None:
                continue
            start, end = int(r.start), int(r.end)
            if end <= start:
                continue
            metadata = getattr(r, "recognition_metadata", None) or {}
            name = metadata.get("recognizer_name") if isinstance(metadata, dict) else None
            out.append(
                Span(
                    page=page,
                    start=start,
                    end=end,
                    text=text[start:end],
                    entity_type=kind,
                    score=max(0.0, min(1.0, float(r.score))),
                    recogniser=f"presidio:{name}" if name else "presidio",
                )
            )
        return out


# How the default engine is built. Explicit, because a bare `AnalyzerEngine()` does not
# declare ORGANIZATION at all: asking it for organisations returns nothing, silently, and
# on 07's corpus that read as 19.5% recall against 86.7% for a detector using the same
# spaCy model. The label mapping is the one 07 found gave the most correct output:
# "Newfoundland" as a location and the real departments as organisations.
DEFAULT_NLP_CONFIGURATION: dict[str, Any] = {
    "nlp_engine_name": "spacy",
    "models": [{"lang_code": "en", "model_name": "en_core_web_lg"}],
    "ner_model_configuration": {
        "model_to_presidio_entity_mapping": {
            "PERSON": "PERSON",
            "PER": "PERSON",
            "ORG": "ORGANIZATION",
            "GPE": "LOCATION",
            "LOC": "LOCATION",
            "FAC": "LOCATION",
            "NORP": "NRP",
            "DATE": "DATE_TIME",
            "TIME": "DATE_TIME",
        },
        "low_confidence_score_multiplier": 0.4,
        "low_score_entity_names": [],
        "labels_to_ignore": [
            "CARDINAL",
            "ORDINAL",
            "MONEY",
            "PERCENT",
            "QUANTITY",
            "PRODUCT",
            "EVENT",
            "WORK_OF_ART",
            "LAW",
            "LANGUAGE",
        ],
    },
}


def _default_engine(nlp_configuration: dict[str, Any] | None = None) -> PresidioLike:
    try:
        from presidio_analyzer import AnalyzerEngine
        from presidio_analyzer.nlp_engine import NlpEngineProvider
    except ImportError as e:
        raise ConfigError(
            "Presidio is not installed. Install the `redact` extra "
            "(uv sync --extra redact) and a spaCy model (docs/redact.md), or pass an engine"
        ) from e
    provider = NlpEngineProvider(nlp_configuration=nlp_configuration or DEFAULT_NLP_CONFIGURATION)
    engine: PresidioLike = AnalyzerEngine(
        nlp_engine=provider.create_engine(), supported_languages=["en"]
    )
    return engine


__all__ = ["DEFAULT_NLP_CONFIGURATION", "ENTITY_MAP", "PresidioLike", "PresidioRecogniser"]
