import re

from pydantic import BaseModel, Field, field_validator


# A bare RFC 6838 media type: `type/subtype`, no parameters, no whitespace,
# and nothing that could carry a header injection. The value is echoed back in
# metadata and signed into the R2 upload URL, so it has to be exactly this.
_CONTENT_TYPE_PATTERN = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}"
)


class FileCreate(BaseModel):
    file_name: str
    file_size: int = Field(gt=0)
    content_type: str = Field(min_length=3, max_length=255)

    @field_validator("content_type")
    @classmethod
    def _validate_content_type(cls, value: str) -> str:
        if _CONTENT_TYPE_PATTERN.fullmatch(value) is None:
            raise ValueError(
                "content_type must be a bare MIME type such as 'image/jpeg'"
            )
        return value


class TransferCreate(BaseModel):
    # An empty transfer would sit in `uploading` and hold an open slot until
    # the create reservation lapses, so it is refused before it is stored.
    # The file-count ceiling stays in the service so it can answer 413.
    files: list[FileCreate] = Field(min_length=1)


class FileUploadResponse(BaseModel):
    file_id: str
    file_name: str
    file_size: int
    upload_url: str


class TransferCreateResponse(BaseModel):
    transfer_id: str
    status: str
    total_files: int
    total_size: int
    expires_in: int
    files: list[FileUploadResponse]
    # Returned once, at create. The sender secret never goes in the share link.
    sender_secret: str
    # Returned once, at create, and carried in the link fragment for receivers.
    access_token: str


class FileMetadata(BaseModel):
    file_id: str
    file_name: str
    file_size: int
    content_type: str


class TransferMetadataResponse(BaseModel):
    transfer_id: str
    status: str
    total_files: int
    total_size: int
    expires_in: int
    files: list[FileMetadata]


class FileDownloadRequest(BaseModel):
    file_ids: list[str] | None = None


class FileDownloadInfo(BaseModel):
    file_id: str
    file_name: str
    download_url: str
    expires_in: int


class TransferDownloadResponse(BaseModel):
    files: list[FileDownloadInfo]


class FileCompleteResponse(BaseModel):
    file_id: str
    status: str
    transfer_status: str


class TransferExtendResponse(BaseModel):
    transfer_id: str
    status: str
    expires_in: int
