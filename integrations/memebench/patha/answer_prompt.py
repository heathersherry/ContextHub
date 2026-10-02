"""Official answer template with deterministic serialization of the sealed view."""

import ast
import json
from pathlib import Path
from typing import Literal
from pydantic import model_validator
from contexthub.models.knowledge import ContractError
from contexthub.propagation.candidate import ModelConfig
from .run_gates import FileIdentity


def load_answer_template(source):
    source.verify()
    tree = ast.parse(Path(source.path).read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "UNIFIED_ANSWER_PROMPT"
            for t in node.targets
        ):
            value = ast.literal_eval(node.value)
            if isinstance(value, str):
                return value
    raise ContractError("official_answer_template_missing")


class OfficialAnswerConfig(ModelConfig):
    adapter: Literal["meme-unified-answer-v1"]
    official_source: FileIdentity
    context_serialization: Literal["sealed-contexts-canonical-json-v1"]

    @model_validator(mode="after")
    def template(self):
        if self.prompt_template != load_answer_template(self.official_source):
            raise ContractError("official_answer_template_mismatch")
        if self.temperature != 0 or self.max_tokens != 500:
            raise ContractError("official_answer_decode_mismatch")
        if self.request_parameters is not None:
            self.request_parameters.check_temperature(self.temperature)
        return self


def render_answer(config, question, contexts):
    if isinstance(config, OfficialAnswerConfig):
        # Preserve every field and list order of the existing sealed answer view;
        # sort object keys only. No retrieval, filtering, truncation or labels.
        context = json.dumps(
            contexts,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return config.prompt_template.format(context=context, question=question)
    return config.prompt_template.format(
        input_json=json.dumps(
            dict(question=question, contexts=contexts), ensure_ascii=False
        )
    )
