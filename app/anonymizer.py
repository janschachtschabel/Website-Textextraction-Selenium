"""
PII anonymization using Microsoft Presidio.

Supported languages: German (de), English (en).
NLP models are loaded once at startup in a background thread.

Required spacy models (download once):
    python -m spacy download de_core_news_lg
    python -m spacy download en_core_web_lg

If models are not yet loaded when anonymize() is called, the original text is
returned unchanged with a warning in the result.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field

from loguru import logger

_analyzer = None
_anonymizer = None
_ready = False
_init_lock = threading.Lock()


@dataclass
class AnonymizationResult:
    """Result of a Presidio anonymization run."""
    entities_found: list[str] = field(default_factory=list)
    entity_count: int = 0
    warning: str | None = None


def init_anonymizer(
    de_model: str = "de_core_news_lg",
    en_model: str = "en_core_web_lg",
) -> None:
    """Load spacy NLP models and initialise Presidio engines.

    Safe to call from a background thread.  Sets the module-level
    _analyzer and _anonymizer on success.
    """
    global _analyzer, _anonymizer, _ready
    with _init_lock:
        try:
            from presidio_analyzer import AnalyzerEngine
            from presidio_analyzer.nlp_engine import NlpEngineProvider
            from presidio_anonymizer import AnonymizerEngine

            provider = NlpEngineProvider(
                nlp_configuration={
                    "nlp_engine_name": "spacy",
                    "models": [
                        {"lang_code": "de", "model_name": de_model},
                        {"lang_code": "en", "model_name": en_model},
                    ],
                }
            )
            nlp_engine = provider.create_engine()
            _analyzer = AnalyzerEngine(
                nlp_engine=nlp_engine,
                supported_languages=["de", "en"],
            )
            _anonymizer = AnonymizerEngine()
            _ready = True
            logger.info(f"Presidio anonymizer ready (de={de_model}, en={en_model})")
        except Exception as exc:
            logger.error(f"Presidio initialisation failed: {exc}")
            _analyzer = None
            _anonymizer = None
            _ready = False


def anonymize(text: str, language: str = "de") -> tuple[str, AnonymizationResult]:
    """Anonymize PII in *text*.

    Returns (anonymized_text, AnonymizationResult).
    Falls back to original text if models are not ready or an error occurs.
    """
    if not _ready or _analyzer is None or _anonymizer is None:
        return text, AnonymizationResult(
            warning="Presidio models not loaded yet — text returned unchanged"
        )
    try:
        results = _analyzer.analyze(text=text, language=language)
        if not results:
            return text, AnonymizationResult()
        anonymized = _anonymizer.anonymize(text=text, analyzer_results=results)
        entity_types = sorted({r.entity_type for r in results})
        return anonymized.text, AnonymizationResult(
            entities_found=entity_types,
            entity_count=len(results),
        )
    except Exception as exc:
        logger.warning(f"Anonymization error: {exc}")
        return text, AnonymizationResult(warning=f"Anonymization failed: {exc}")
