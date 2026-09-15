"""Lazy language-specific Presidio initialization; failures never disclose text."""

from importlib import import_module
from threading import Lock

from .config import settings
from .results import CrawlError
from .schemas import AnonymizationResult

_engines = {}
_lock = Lock()


def _engine(language: str):
    with _lock:
        if language not in _engines:
            try:
                analyzer_module = import_module("presidio_analyzer")
                provider_type = import_module("presidio_analyzer.nlp_engine").NlpEngineProvider
                anonymizer_type = import_module("presidio_anonymizer").AnonymizerEngine
                model = settings.presidio_de_model if language == "de" else settings.presidio_en_model
                # Do not allow a missing model to trigger runtime downloads.
                import_module(model)
                provider = provider_type(
                    nlp_configuration={
                        "nlp_engine_name": "spacy",
                        "models": [{"lang_code": language, "model_name": model}],
                    }
                )
                analyzer = analyzer_module.AnalyzerEngine(
                    nlp_engine=provider.create_engine(), supported_languages=[language]
                )
                _engines[language] = (analyzer, anonymizer_type())
            except Exception as exc:
                raise CrawlError(
                    "Anonymization unavailable: install the PII extra and requested language model", 503
                ) from exc
        return _engines[language]


def anonymize(text: str, language: str = "de") -> tuple[str, AnonymizationResult]:
    if language not in {"de", "en"}:
        raise CrawlError("Unsupported anonymization language", 422)
    analyzer, redactor = _engine(language)
    try:
        entities = analyzer.analyze(text=text, language=language)
        result = redactor.anonymize(text=text, analyzer_results=entities)
        return result.text, AnonymizationResult(
            entities_found=sorted({item.entity_type for item in entities}),
            entity_count=len(entities),
        )
    except Exception as exc:
        raise CrawlError("Anonymization failed; no content returned", 503) from exc
