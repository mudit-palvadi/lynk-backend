from pydantic import BaseModel, Field


class ReceiverSessionCreateResponse(BaseModel):
    session_id: str
    status: str
    expires_in: int


class ReceiverSessionResponse(BaseModel):
    session_id: str
    status: str
    transfer_id: str | None = None
    wrapped_key: str | None = None
    expires_in: int


class AttachTransferRequest(BaseModel):
    # Both values are stored in Redis as they arrive, so they are bounded like
    # the sender-session attach (#51).
    transfer_id: str = Field(min_length=1, max_length=64)
    # Opaque ECDH-sealed ciphertext of the AES key, wrapped client-side to the
    # receiver's ephemeral public key. The backend never sees the plaintext.
    wrapped_key: str | None = Field(default=None, max_length=1000)
