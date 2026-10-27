from typing import Literal

from pydantic import BaseModel, Field


class SenderSessionCreateResponse(BaseModel):
    session_id: str
    code: str
    status: str
    expires_in: int
    sender_secret: str


class SenderSessionJoinRequest(BaseModel):
    code: str = Field(min_length=1, max_length=12)
    receiver_pk: str = Field(min_length=16, max_length=200)


class SenderSessionJoinResponse(BaseModel):
    session_id: str
    status: str
    expires_in: int
    # The receiver keeps this and presents it when collecting the wrapped key.
    # The sender's poll never receives it.
    receiver_secret: str


class SenderSessionResponse(BaseModel):
    session_id: str
    status: str
    receiver_pk: str | None = None
    transfer_id: str | None = None
    expires_in: int


class SenderSessionAttachRequest(BaseModel):
    transfer_id: str = Field(min_length=1, max_length=64)
    wrapped_key: str = Field(min_length=16, max_length=1000)
    # Tells the receiver whether a direct connection is worth waiting for.
    mode: Literal["cloud", "p2p"] = "cloud"


class SenderSessionAttachResponse(BaseModel):
    session_id: str
    status: str
    expires_in: int


class SenderSessionKeyResponse(BaseModel):
    transfer_id: str
    wrapped_key: str
    mode: str
    expires_in: int
