from pydantic import BaseModel, Field
from typing import Optional, Dict, Any


class TeamsPayload(BaseModel):
    text: Optional[str] = None
    from_: Optional[Dict[str, Any]] = Field(None, alias="from")
    conversation: Optional[Dict[str, Any]] = None
    serviceUrl: Optional[str] = None
    channelId: Optional[str] = None
    recipient: Optional[Dict[str, Any]] = None

    class Config:
        allow_population_by_field_name = True
        extra = "allow"


class TeamsResponse(BaseModel):
    type: str = "message"
    text: str
