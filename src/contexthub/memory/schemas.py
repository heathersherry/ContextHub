from dataclasses import dataclass
from typing import Literal
import math
import unicodedata

from pydantic import BaseModel, ConfigDict, Field


class Trace(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    run_id: str = Field(min_length=1)
    episode_id: str = Field(min_length=1)
    strategy_id: Literal["local_base", "full_strong", "adaptive"]

    @property
    def args(self):
        return self.run_id, self.episode_id, self.strategy_id


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


@dataclass(frozen=True)
class ExtractedItem:
    text: str
    type: str
    entities: list[str]
    message_refs: list[int]


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


def vector_literal(vector, dimensions):
    if (
        len(vector) != dimensions
        or any(type(v) not in (float, int) or not math.isfinite(v) for v in vector)
        or not any(vector)
    ):
        raise ValueError("nonzero finite vector of configured dimension required")
    return "[" + ",".join(str(float(v)) for v in vector) + "]"
