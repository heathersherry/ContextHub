"""Local pinned spaCy NER, used only to propose source-backed mentions."""
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
import re
from pydantic import Field

from contexthub.models.knowledge import Contract, ContractError, Name


class SpacyMentionConfig(Contract):
    spacy_version: Name
    english_model: Name
    english_version: Name
    chinese_model: Name
    chinese_version: Name
    allowed_entity_labels: tuple[Name, ...] | None = Field(default=None, exclude_if=lambda v: v is None)


@lru_cache(maxsize=4)
def _pipeline(package, expected_version, spacy_version):
    try:
        if version('spacy') != spacy_version or version(package) != expected_version:
            raise ContractError('mention_ner_version_mismatch')
        import spacy
        nlp = spacy.load(package)
    except (PackageNotFoundError, ImportError, OSError) as exc:
        raise ContractError('mention_ner_model_unavailable') from exc
    if 'ner' not in nlp.pipe_names:
        raise ContractError('mention_ner_component_missing')
    return nlp


def check_ner(config):
    """Explicit preflight: both configured languages must be installed."""
    for package, expected in ((config.english_model, config.english_version),
                              (config.chinese_model, config.chinese_version)):
        _pipeline(package, expected, config.spacy_version)


@lru_cache(maxsize=128)
def _recognize(package, expected_version, spacy_version, text):
    nlp = _pipeline(package, expected_version, spacy_version)
    # Never trim inputs to fit the local recognizer's supported length.
    if len(text) > nlp.max_length:
        raise ContractError('mention_ner_input_too_long')
    try:
        doc = nlp(text)
    except Exception as exc:
        raise ContractError('mention_ner_inference_failed') from exc
    result = []
    for ent in doc.ents:
        if not (0 <= ent.start_char < ent.end_char <= len(text)) or text[ent.start_char:ent.end_char] != ent.text:
            raise ContractError('mention_ner_span_mismatch')
        result.append((ent.start_char, ent.end_char, ent.label_))
    return tuple(result)


def entity_spans(config, text):
    """Mixed text runs both models; no label-based identity or regex fallback."""
    models = []
    if re.search(r'[A-Za-z]', text):
        models.append((config.english_model, config.english_version))
    if re.search(r'[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U000323af]', text):
        models.append((config.chinese_model, config.chinese_version))
    # Numbers/other scripts still receive the explicit default English pipeline.
    if not models:
        models.append((config.english_model, config.english_version))
    return tuple(sorted({(a, b) for package, expected in models
                         for a, b, label in _recognize(package, expected, config.spacy_version, text)
                         if config.allowed_entity_labels is None or label in config.allowed_entity_labels}))
