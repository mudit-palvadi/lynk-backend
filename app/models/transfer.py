from pydantic import BaseModel, Field


class FileItemModel(BaseModel):
    file_id: str
    file_name: str
    file_size: int
    content_type: str
    status: str = "pending"


class TransferModel(BaseModel):
    transfer_id: str
    status: str = "uploading"
    created_at: str
    expires_at: str
    total_files: int
    total_size: int
    # Present only on records created after the capability split. Empty means
    # the record cannot authorize metadata, download, cancel, extend, or complete.
    sender_secret: str = ""
    access_token: str = ""
    # The address that owns this transfer's unfinished slot. Empty on records
    # written before the slot was kept for a cancelled upload URL. The API
    # responses build their own dicts, so this stays off the wire.
    owner_address: str = ""
    files: list[FileItemModel] = Field(default_factory=list)
