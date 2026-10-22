from pydantic import BaseModel


class SenderSessionModel(BaseModel):
    """A sender-owned pairing session the receiver joins with a six-digit code."""

    session_id: str
    code: str
    # waiting -> joined (the receiver published its key) -> attached (key wrapped)
    status: str = "waiting"
    # Receiver's ephemeral public key, so the sender can seal the AES key to it.
    receiver_pk: str | None = None
    transfer_id: str | None = None
    # Opaque client-side ECDH-sealed ciphertext. Never a plaintext AES key.
    wrapped_key: str | None = None
    created_at: str
    expires_at: str
