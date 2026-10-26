from dataclasses import dataclass
from typing import Literal
import math
import unicodedata

from pydantic import BaseModel, ConfigDict, Field


class MemoryScope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    tenant_id: str = Field(min_length=1)
    namespace: str = Field(min_length=1)
    owner_id: str = Field(min_length=1)

    @property
    def args(self):
        return self.tenant_id, self.namespace, self.owner_id


class IndexConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    dimensions: int = Field(strict=True, gt=0)
    embedding_model: str = Field(min_length=1)
    document_prefix: str
    query_prefix: str
    text_config: str = Field(min_length=1)


class RetrievalConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    text_k: int = Field(strict=True, gt=0)
    vector_k: int = Field(strict=True, gt=0)
    rrf_c: float = Field(gt=0, allow_inf_nan=False)
    hybrid_limit: int = Field(strict=True, gt=0)
    entity_k: int = Field(strict=True, ge=0)
    topic_similarity_threshold: float = Field(default=0.8, ge=-1, le=1, allow_inf_nan=False)


@dataclass(frozen=True)
class ExtractedItem:
    text: str
    type: str
    entities: list[str]
    temporal_scope: Literal["current", "historical"] | None
    topics: list[str]


@dataclass(frozen=True)
class Extraction:
    items: list[ExtractedItem]


def normalize_entities(names):
    """NFKC, casefold and whitespace only; never infer aliases or identity."""
    return sorted(
        {
            value
            for name in names
            if (
                value := " ".join(
                    unicodedata.normalize("NFKC", name).casefold().split()
                )
            )
        }
    )


def normalize_topics(topics):
    if not isinstance(topics, list) or any(
        not isinstance(topic, str) or not topic.strip() for topic in topics
    ):
        raise ValueError("topics must be a list of nonempty strings")
    return normalize_entities(topics)


def vector_literal(vector, dimensions):
    if (
        len(vector) != dimensions
        or any(type(v) not in (float, int) or not math.isfinite(v) for v in vector)
        or not any(vector)
    ):
        raise ValueError("nonzero finite vector of configured dimension required")
    return "[" + ",".join(str(float(v)) for v in vector) + "]"
