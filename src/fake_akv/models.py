from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field


class SecretCreate(BaseModel):
    value: str
    tags: Optional[Dict[str, str]] = None
    attributes: Optional[Dict[str, Any]] = None
    contentType: Optional[str] = None
    model_config = ConfigDict(extra="allow")


class SecretResult(BaseModel):
    value: Optional[str] = None
    id: str
    attributes: Dict[str, object] = Field(default_factory=dict)
    tags: Optional[Dict[str, str]] = None


class DeletedSecretResult(BaseModel):
    recoveryId: str
    deletedDate: int
    scheduledPurgeDate: int
    id: str
    attributes: Dict[str, object] = Field(default_factory=dict)
    tags: Optional[Dict[str, str]] = None
