"""Core data models shared across the AI layer."""
from pydantic import BaseModel


class Model(BaseModel):
    id: str
    provider: str