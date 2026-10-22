from pydantic import BaseModel


class SenderSessionCreateResponse(BaseModel):
    session_id: str
    code: str
    status: str
    expires_in: int
