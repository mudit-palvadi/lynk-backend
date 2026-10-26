import secrets
import time
from datetime import datetime, timedelta, timezone
from app.core.config import settings
from app.core.exceptions import (
    LimitExceededException,
    RateLimitExceededException,
    TransferNotFoundException,
    TransferNotReadyException,
    InvalidInputException,
    UnauthorizedException,
)
from app.core.security import secrets_match
from app.core.file_names import validate_file_name
from app.models.transfer import TransferModel, FileItemModel
from app.services.redis_service import RedisService
from app.services.r2_service import R2Service

ACTIVE_TRANSFERS_KEY = "lynk:active_transfers"


class TransferService:
    def __init__(self, redis_service: RedisService, r2_service: R2Service) -> None:
        self.redis = redis_service
        self.r2 = r2_service

    def _generate_id(self, length: int) -> str:
        return secrets.token_urlsafe(length)[:length]

    def _require_secret(self, presented: str | None, expected: str) -> None:
        if not secrets_match(presented, expected):
            raise UnauthorizedException()

    @staticmethod
    def _landed_bytes(transfer: TransferModel) -> int:
        return sum(f.file_size for f in transfer.files if f.status == "uploaded")

    async def _hold_landed_bytes(
        self, transfer_id: str, before: int, after: int, until: int
    ) -> None:
        """Hold capacity for the bytes that are really in storage (#50).

        Only those bytes may outlive the short create-time reservation. Holding
        the declared size instead let one address lock the whole cap with a
        single 1-byte file, or with an extend and no upload at all. While the
        reservation is still running both entries count, which only overstates
        use. Once every file has landed the two entries are the same member.
        """
        client = await self.redis.get_client()
        if before and before != after:
            await client.zrem(ACTIVE_TRANSFERS_KEY, f"{transfer_id}:{before}")
        if after:
            await client.zadd(ACTIVE_TRANSFERS_KEY, {f"{transfer_id}:{after}": until})

    async def _release_open_slot(self, client_ip: str | None, transfer_id: str) -> None:
        """Drop a transfer from its address's open-reservation set (#34)."""
        if not client_ip:
            return
        client = await self.redis.get_client()
        await client.zrem(f"lynk:open:{client_ip}", transfer_id)

    async def create_transfer(
        self, files_input: list[dict], *, client_ip: str | None = None
    ) -> dict:
        if len(files_input) > settings.MAX_FILES_PER_TRANSFER:
            raise LimitExceededException("Maximum file count limit exceeded")

        total_size = 0
        file_models = []
        transfer_id = self._generate_id(12)

        for f in files_input:
            size = f["file_size"]
            # The sender chooses this name and every receiver writes it to
            # disk, so a name that is not a single path segment is refused
            # before it can be stored or served to anyone.
            try:
                file_name = validate_file_name(f["file_name"])
            except ValueError as e:
                raise InvalidInputException(str(e)) from e
            if size <= 0:
                raise InvalidInputException(
                    f"File '{file_name}' size must be a positive number of bytes"
                )
            if size > settings.MAX_INDIVIDUAL_FILE_SIZE:
                raise LimitExceededException(f"File '{file_name}' exceeds individual size limit")
            total_size += size

            file_id = self._generate_id(8)
            file_models.append(
                FileItemModel(
                    file_id=file_id,
                    file_name=file_name,
                    file_size=size,
                    content_type=f["content_type"],
                    status="pending",
                )
            )

        if total_size > settings.MAX_TOTAL_TRANSFER_SIZE:
            raise LimitExceededException("Total transfer size limit exceeded")

        client = await self.redis.get_client()
        current_time = int(time.time())

        # Two minutes of reservation and a five-a-minute limiter still let one
        # address hold ten creates in flight, which is enough to fill the whole
        # capacity cap and lock everyone else out (#34).
        open_key = f"lynk:open:{client_ip}" if client_ip else None
        if open_key:
            await client.zremrangebyscore(open_key, "-inf", current_time)
            open_count = int(await client.zcard(open_key))
            if open_count >= settings.MAX_OPEN_RESERVATIONS_PER_CLIENT:
                raise RateLimitExceededException(
                    "Too many unfinished transfers from this address. "
                    "Finish or cancel one and try again."
                )

        await client.zremrangebyscore(ACTIVE_TRANSFERS_KEY, "-inf", current_time)
        active_items = await client.zrange(ACTIVE_TRANSFERS_KEY, 0, -1)
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
        sender_secret = secrets.token_urlsafe(32)
        access_token = secrets.token_urlsafe(32)

        transfer = TransferModel(
            transfer_id=transfer_id,
            status="uploading",
            created_at=now.isoformat(),
            expires_at=expires_at.isoformat(),
            total_files=len(file_models),
            total_size=total_size,
            sender_secret=sender_secret,
            access_token=access_token,
            files=file_models,
        )

        await self.redis.set_transfer(
            transfer_id, transfer.model_dump(), settings.TRANSFER_LIFETIME_SECONDS
        )

        # Reserve the declared size, but only for a couple of minutes: a
        # create that never uploads must not hold capacity for the whole
        # transfer lifetime (#34). After that only landed bytes are held (#50).
        reservation_until = current_time + settings.UNFILLED_RESERVATION_SECONDS
        await client.zadd(
            ACTIVE_TRANSFERS_KEY, {f"{transfer_id}:{total_size}": reservation_until}
        )
        if open_key:
            await client.zadd(open_key, {transfer_id: reservation_until})
            await client.expire(
                open_key, settings.UNFILLED_RESERVATION_SECONDS * 2
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
            "sender_secret": sender_secret,
            "access_token": access_token,
        }

    async def complete_file(
        self,
        transfer_id: str,
        file_id: str,
        *,
        sender_secret: str | None,
        client_ip: str | None = None,
    ) -> dict:
        data = await self.redis.get_transfer(transfer_id)
        if not data:
            raise TransferNotFoundException()

        transfer = TransferModel.model_validate(data)
        self._require_secret(sender_secret, transfer.sender_secret)
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

        landed_before = self._landed_bytes(transfer)
        target_file.status = "uploaded"

        if all(f.status == "uploaded" for f in transfer.files):
            transfer.status = "ready"

        await self.redis.update_transfer(transfer_id, transfer.model_dump())

        await self._hold_landed_bytes(
            transfer_id,
            landed_before,
            self._landed_bytes(transfer),
            int(datetime.fromisoformat(transfer.expires_at).timestamp()),
        )
        # A partly uploaded transfer keeps its open slot until the reservation
        # lapses, so completing one small file cannot free the slot for the
        # next create (#50).
        if transfer.status == "ready":
            await self._release_open_slot(client_ip, transfer_id)

        return {
            "file_id": file_id,
            "status": target_file.status,
            "transfer_status": transfer.status,
        }

    async def require_access(self, transfer_id: str, *, access_token: str | None) -> None:
        """Refuse TURN credentials for an unknown id or a wrong token (#47).

        The id alone used to be enough here, because the client collects
        credentials before the signaling room opens. That let anyone who
        guessed an id spend the relay's bandwidth, so the check now matches
        the other transfer routes: unknown id -> 404, token -> 401.
        """
        data = await self.redis.get_transfer(transfer_id)
        if not data:
            raise TransferNotFoundException()
        self._require_secret(access_token, data.get("access_token", ""))

    async def get_transfer_metadata(self, transfer_id: str, *, access_token: str | None) -> dict:
        data = await self.redis.get_transfer(transfer_id)
        if not data:
            raise TransferNotFoundException()

        transfer = TransferModel.model_validate(data)
        self._require_secret(access_token, transfer.access_token)

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

    async def extend_transfer(self, transfer_id: str, *, sender_secret: str | None) -> dict:
        data = await self.redis.get_transfer(transfer_id)
        if not data:
            raise TransferNotFoundException()

        transfer = TransferModel.model_validate(data)
        self._require_secret(sender_secret, transfer.sender_secret)

        now = datetime.now(timezone.utc)
        new_expires_at = now + timedelta(seconds=settings.EXTENDED_TRANSFER_LIFETIME_SECONDS)
        transfer.expires_at = new_expires_at.isoformat()

        await self.redis.extend_transfer(
            transfer_id, transfer.model_dump(), settings.EXTENDED_TRANSFER_LIFETIME_SECONDS
        )

        # The record lives longer, but capacity is held only for what is in
        # storage: extending an empty transfer must not hold its declared size.
        landed = self._landed_bytes(transfer)
        await self._hold_landed_bytes(
            transfer_id, landed, landed, int(new_expires_at.timestamp())
        )

        return {
            "transfer_id": transfer.transfer_id,
            "status": transfer.status,
            "expires_in": settings.EXTENDED_TRANSFER_LIFETIME_SECONDS,
        }

    async def get_download_urls(
        self,
        transfer_id: str,
        file_ids: list[str] | None = None,
        *,
        access_token: str | None,
    ) -> dict:
        data = await self.redis.get_transfer(transfer_id)
        if not data:
            raise TransferNotFoundException()

        transfer = TransferModel.model_validate(data)
        self._require_secret(access_token, transfer.access_token)
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

    async def cancel_transfer(
        self,
        transfer_id: str,
        *,
        sender_secret: str | None,
        client_ip: str | None = None,
    ) -> None:
        data = await self.redis.get_transfer(transfer_id)
        if not data:
            return

        transfer = TransferModel.model_validate(data)
        self._require_secret(sender_secret, transfer.sender_secret)
        await self.redis.delete_transfer(transfer_id)

        client = await self.redis.get_client()
        await client.zrem(
            ACTIVE_TRANSFERS_KEY,
            f"{transfer_id}:{transfer.total_size}",
            f"{transfer_id}:{self._landed_bytes(transfer)}",
        )
        await self._release_open_slot(client_ip, transfer_id)

        file_ids = [f.file_id for f in transfer.files]
        await self.r2.delete_objects(transfer_id, file_ids)
