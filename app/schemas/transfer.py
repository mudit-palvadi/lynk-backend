from pydantic import BaseModel, Field


class FileCreate(BaseModel):
    file_name: str
    file_size: int = Field(gt=0)
    content_type: str


class TransferCreate(BaseModel):
    files: list[FileCreate]


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
