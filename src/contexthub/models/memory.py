from pydantic import BaseModel, Field


class PromoteRequest(BaseModel):
    uri: str
    target_team: str


class AddMemoryRequest(BaseModel):
    content: str
    tags: list[str] = Field(default_factory=list)
