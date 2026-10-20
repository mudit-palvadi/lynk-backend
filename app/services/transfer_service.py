import secrets
import time
from datetime import datetime, timedelta, timezone
from app.core.config import settings
from app.core.exceptions import (
    LimitExceededException,
    TransferNotFoundException,
    TransferNotReadyException,
    InvalidInputException,
)
from app.models.transfer import TransferModel, FileItemModel
from app.services.redis_service import RedisService
from app.services.r2_service import R2Service


class TransferService:
    def __init__(self, redis_service: RedisService, r2_service: R2Service) -> None:
        self.redis = redis_service
        self.r2 = r2_service

    def _generate_id(self, length: int) -> str:
        return secrets.token_urlsafe(length)[:length]

    async def create_transfer(self, files_input: list[dict]) -> dict:
        if len(files_input) > settings.MAX_FILES_PER_TRANSFER:
            raise LimitExceededException("Maximum file count limit exceeded")

        total_size = 0
        file_models = []
        transfer_id = self._generate_id(12)

        for f in files_input:
            size = f["file_size"]
            if size <= 0:
                raise InvalidInputException(
                    f"File '{f['file_name']}' size must be a positive number of bytes"
                )
            if size > settings.MAX_INDIVIDUAL_FILE_SIZE:
                raise LimitExceededException(
                    f"File '{f['file_name']}' exceeds individual size limit"
                )
            total_size += size

            file_id = self._generate_id(8)
            file_models.append(
                FileItemModel(
                    file_id=file_id,
                    file_name=f["file_name"],
                    file_size=size,
                    content_type=f["content_type"],
                    status="pending",
                )
            )

        if total_size > settings.MAX_TOTAL_TRANSFER_SIZE:
            raise LimitExceededException("Total transfer size limit exceeded")

        client = await self.redis.get_client()
        current_time = int(time.time())
        await client.zremrangebyscore("lynk:active_transfers", "-inf", current_time)
        active_items = await client.zrange("lynk:active_transfers", 0, -1)
        current_active_size = 0
        for item in active_items:
            try:
                _, size_str = item.split(":")
                current_active_size += int(size_str)
            except ValueError:
                continue

        if current_active_size + total_size > settings.MAX_TOTAL_R2_CAP_BYTES:
            raise LimitExceededException(
                "High server traffic. Active storage capacity limit "
                "reached. Please try again later."
            )

        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=settings.TRANSFER_LIFETIME_SECONDS)

        transfer = TransferModel(
            transfer_id=transfer_id,
            status="uploading",
            created_at=now.isoformat(),
            expires_at=expires_at.isoformat(),
            total_files=len(file_models),
            total_size=total_size,
            files=file_models,
        )

        await self.redis.set_transfer(
            transfer_id, transfer.model_dump(), settings.TRANSFER_LIFETIME_SECONDS
        )

        expires_timestamp = int(expires_at.timestamp())
        await client.zadd(
            "lynk:active_transfers", {f"{transfer_id}:{total_size}": expires_timestamp}
        )

        upload_files = []
        for f in file_models:
            url = await self.r2.generate_upload_url(
                transfer_id, f.file_id, f.content_type, f.file_size
            )
            upload_files.append(
                {
                    "file_id": f.file_id,
                    "file_name": f.file_name,
                    "file_size": f.file_size,
                    "upload_url": url,
                }
            )

        return {
            "transfer_id": transfer_id,
            "status": "uploading",
            "total_files": len(file_models),
            "total_size": total_size,
            "expires_in": settings.TRANSFER_LIFETIME_SECONDS,
            "files": upload_files,
        }

    async def complete_file(self, transfer_id: str, file_id: str) -> dict:
        data = await self.redis.get_transfer(transfer_id)
        if not data:
            raise TransferNotFoundException()

        transfer = TransferModel.model_validate(data)
        target_file = next((f for f in transfer.files if f.file_id == file_id), None)
        if not target_file:
            raise InvalidInputException("File not found in transfer")

        try:
            actual_size = await self.r2.verify_file_exists(transfer_id, file_id)
        except FileNotFoundError:
            target_file.status = "failed"
            raise InvalidInputException("File has not been uploaded to storage")

        if actual_size != target_file.file_size:
            await self.r2.delete_objects(transfer_id, [file_id])
            raise InvalidInputException(
                "Uploaded object size does not match the declared file size"
            )

        target_file.status = "uploaded"

        if all(f.status == "uploaded" for f in transfer.files):
            transfer.status = "ready"

        await self.redis.update_transfer(transfer_id, transfer.model_dump())

        return {
            "file_id": file_id,
            "status": target_file.status,
            "transfer_status": transfer.status,
        }

    async def get_transfer_metadata(self, transfer_id: str) -> dict:
        data = await self.redis.get_transfer(transfer_id)
        if not data:
            raise TransferNotFoundException()

        transfer = TransferModel.model_validate(data)

        client = await self.redis.get_client()
        key = self.redis._get_key(transfer_id)
        ttl = await client.ttl(key)
        expires_in = max(0, ttl) if ttl != -2 else 0

        return {
            "transfer_id": transfer.transfer_id,
            "status": transfer.status,
            "total_files": transfer.total_files,
            "total_size": transfer.total_size,
            "expires_in": expires_in,
            "files": [
                {
                    "file_id": f.file_id,
                    "file_name": f.file_name,
                    "file_size": f.file_size,
                    "content_type": f.content_type,
                }
                for f in transfer.files
            ],
        }

    async def extend_transfer(self, transfer_id: str) -> dict:
        data = await self.redis.get_transfer(transfer_id)
        if not data:
            raise TransferNotFoundException()

        transfer = TransferModel.model_validate(data)

        now = datetime.now(timezone.utc)
        new_expires_at = now + timedelta(seconds=settings.EXTENDED_TRANSFER_LIFETIME_SECONDS)
        transfer.expires_at = new_expires_at.isoformat()

        await self.redis.extend_transfer(
            transfer_id, transfer.model_dump(), settings.EXTENDED_TRANSFER_LIFETIME_SECONDS
        )

        client = await self.redis.get_client()
        await client.zadd(
            "lynk:active_transfers",
            {f"{transfer_id}:{transfer.total_size}": int(new_expires_at.timestamp())},
        )

        return {
            "transfer_id": transfer.transfer_id,
            "status": transfer.status,
            "expires_in": settings.EXTENDED_TRANSFER_LIFETIME_SECONDS,
        }

    async def get_download_urls(self, transfer_id: str, file_ids: list[str] | None = None) -> dict:
        data = await self.redis.get_transfer(transfer_id)
        if not data:
            raise TransferNotFoundException()

        transfer = TransferModel.model_validate(data)
        if transfer.status != "ready":
            raise TransferNotReadyException()

        targets = file_ids if file_ids is not None else [f.file_id for f in transfer.files]
        download_files = []

        for f in transfer.files:
            if f.file_id in targets:
                if f.status != "uploaded":
                    continue
                url = await self.r2.generate_download_url(transfer_id, f.file_id, f.file_name)
                download_files.append(
                    {
                        "file_id": f.file_id,
                        "file_name": f.file_name,
                        "download_url": url,
                        "expires_in": settings.DOWNLOAD_URL_LIFETIME_SECONDS,
                    }
                )

        return {"files": download_files}

    async def cancel_transfer(self, transfer_id: str) -> None:
        data = await self.redis.get_transfer(transfer_id)
        if not data:
            return

        transfer = TransferModel.model_validate(data)
        await self.redis.delete_transfer(transfer_id)

        client = await self.redis.get_client()
        await client.zrem("lynk:active_transfers", f"{transfer_id}:{transfer.total_size}")

        file_ids = [f.file_id for f in transfer.files]
        await self.r2.delete_objects(transfer_id, file_ids)
