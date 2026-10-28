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
        """A wrong credential looks the same as a missing transfer.

        Answering 401 only when the record exists would tell a guesser which
        ids are live.
        """
        if not secrets_match(presented, expected):
            raise TransferNotFoundException()

    @staticmethod
    def _landed_bytes(transfer: TransferModel) -> int:
        return sum(f.file_size for f in transfer.files if f.status == "uploaded")

    async def _hold_landed_bytes(
        self, transfer: TransferModel, after: int, until: int
    ) -> bool | None:
        """Hold capacity for the bytes that are really in storage (#50).

        Only those bytes may outlive the short create-time reservation. The
        reservation itself stays until it expires or every file has landed, so
        a 1-byte upload cannot free the rest of a large create. One lock
        replaces every landed member for this transfer, and a complete that
        no longer has a reservation is refused when it would pass the cap.
        None means the transfer record is already gone, so nothing is written.
        """
        client = await self.redis.get_client()
        lock_key = f"{ACTIVE_TRANSFERS_KEY}:lock"
        token = await self.redis.acquire_record_lock(client, lock_key)
        try:
            if not await self.redis.get_transfer(transfer.transfer_id):
                return None
            now = int(time.time())
            rows = await client.zrange(ACTIVE_TRANSFERS_KEY, 0, -1, withscores=True)
            prefix = f"{transfer.transfer_id}:"
            reservation = f"{transfer.transfer_id}:{transfer.total_size}"
            reservation_alive = False
            own: list[str] = []
            others = 0
            for member, score in rows:
                name = str(member)
                if name.startswith(prefix):
                    own.append(name)
                    # A score Redis is about to sweep (score <= now) is
                    # already expired. Keeping it would skip the cap check.
                    score_value = float(score)
                    if (
                        name == reservation
                        and now
                        < score_value
                        <= now + settings.UNFILLED_RESERVATION_SECONDS + 2
                    ):
                        reservation_alive = True
                    continue
                try:
                    others += int(name.rsplit(":", 1)[1])
                except ValueError:
                    continue

            keep_reservation = reservation_alive and after != transfer.total_size
            if (
                not keep_reservation
                and others + after > settings.MAX_TOTAL_R2_CAP_BYTES
            ):
                return False
            for name in own:
                if keep_reservation and name == reservation:
                    continue
                await client.zrem(ACTIVE_TRANSFERS_KEY, name)

            if after:
                await client.zadd(
                    ACTIVE_TRANSFERS_KEY,
                    {f"{transfer.transfer_id}:{after}": until},
                )
            return True
        finally:
            await self.redis.release_record_lock(client, lock_key, token)

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

        client = await self.redis.get_client()
        # The cap and the per-address slot are decided and recorded together.
        # Two creates that each read the zset before either one wrote it would
        # both pass and the cap would be exceeded.
        lock_key = f"{ACTIVE_TRANSFERS_KEY}:lock"
        token = await self.redis.acquire_record_lock(client, lock_key)
        try:
            current_time = int(time.time())
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

            await self.redis.set_transfer(
                transfer_id, transfer.model_dump(), settings.TRANSFER_LIFETIME_SECONDS
            )

            # Reserve the declared size, but only for a couple of minutes: a
            # create that never uploads must not hold capacity for the whole
            # transfer lifetime (#34). After that only landed bytes are held (#50).
            reservation_until = current_time + settings.UNFILLED_RESERVATION_SECONDS
            await client.zadd(
                ACTIVE_TRANSFERS_KEY,
                {f"{transfer_id}:{total_size}": reservation_until},
            )
            if open_key:
                await client.zadd(open_key, {transfer_id: reservation_until})
                await client.expire(
                    open_key, settings.UNFILLED_RESERVATION_SECONDS * 2
                )
        finally:
            await self.redis.release_record_lock(client, lock_key, token)

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
            secrets_match(sender_secret, None)
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

        # Re-read under the record lock. An extend that landed while R2 was
        # being checked must not be replaced by this older copy.
        saved: dict = {}

        def mutate(data: dict) -> dict:
            fresh = TransferModel.model_validate(data)
            self._require_secret(sender_secret, fresh.sender_secret)
            target = next((f for f in fresh.files if f.file_id == file_id), None)
            if target is None:
                raise InvalidInputException("File not found in transfer")
            saved["previous_status"] = target.status
            target.status = "uploaded"
            if all(f.status == "uploaded" for f in fresh.files):
                fresh.status = "ready"
            saved["transfer"] = fresh
            return fresh.model_dump()

        updated = await self.redis.mutate_json(self.redis._get_key(transfer_id), mutate)
        if updated is None:
            raise TransferNotFoundException()
        transfer = saved["transfer"]
        landed = self._landed_bytes(transfer)
        accepted = await self._hold_landed_bytes(
            transfer,
            landed,
            int(datetime.fromisoformat(transfer.expires_at).timestamp()),
        )
        if accepted is None:
            raise TransferNotFoundException()
        if not accepted:
            previous_status = saved["previous_status"]

            def revert(data: dict) -> dict:
                fresh = TransferModel.model_validate(data)
                target = next((f for f in fresh.files if f.file_id == file_id), None)
                if target is not None and target.status == "uploaded":
                    target.status = previous_status
                if fresh.status == "ready":
                    fresh.status = "uploading"
                return fresh.model_dump()

            await self.redis.mutate_json(self.redis._get_key(transfer_id), revert)
            await self.r2.delete_objects(transfer_id, [file_id])
            raise LimitExceededException(
                "High server traffic. Active storage capacity limit "
                "reached. Please try again later."
            )
        # A partly uploaded transfer keeps its open slot until the reservation
        # lapses, so completing one small file cannot free the slot for the
        # next create (#50).
        if transfer.status == "ready":
            await self._release_open_slot(client_ip, transfer_id)

        fresh_file = next(f for f in transfer.files if f.file_id == file_id)
        return {
            "file_id": file_id,
            "status": fresh_file.status,
            "transfer_status": transfer.status,
        }

    async def require_access(self, transfer_id: str, *, access_token: str | None) -> None:
        """Refuse TURN credentials for an unknown id or a wrong token (#47).

        The id alone used to be enough here, because the client collects
        credentials before the signaling room opens. That let anyone who
        guessed an id spend the relay's bandwidth. A wrong token is the same
        404 as an unknown id, so the id is not an existence oracle.
        """
        data = await self.redis.get_transfer(transfer_id)
        if not data:
            secrets_match(access_token, None)
            raise TransferNotFoundException()
        self._require_secret(access_token, data.get("access_token", ""))

    async def get_transfer_metadata(self, transfer_id: str, *, access_token: str | None) -> dict:
        data = await self.redis.get_transfer(transfer_id)
        if not data:
            secrets_match(access_token, None)
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
            secrets_match(sender_secret, None)
            raise TransferNotFoundException()

        transfer = TransferModel.model_validate(data)
        self._require_secret(sender_secret, transfer.sender_secret)

        now = datetime.now(timezone.utc)
        new_expires_at = now + timedelta(seconds=settings.EXTENDED_TRANSFER_LIFETIME_SECONDS)
        key = self.redis._get_key(transfer_id)
        client = await self.redis.get_client()
        previous_ttl = int(await client.ttl(key))
        previous_expires_at = transfer.expires_at
        if previous_ttl <= 0:
            raise TransferNotFoundException()
        saved: dict = {}

        def mutate(data: dict) -> dict:
            fresh = TransferModel.model_validate(data)
            self._require_secret(sender_secret, fresh.sender_secret)
            fresh.expires_at = new_expires_at.isoformat()
            saved["transfer"] = fresh
            return fresh.model_dump()

        updated = await self.redis.mutate_json(
            key,
            mutate,
            ttl_seconds=settings.EXTENDED_TRANSFER_LIFETIME_SECONDS,
        )
        if updated is None:
            raise TransferNotFoundException()
        transfer = saved["transfer"]

        # The record lives longer, but capacity is held only for what is in
        # storage: extending an empty transfer must not hold its declared size.
        landed = self._landed_bytes(transfer)
        held = await self._hold_landed_bytes(
            transfer, landed, int(new_expires_at.timestamp())
        )
        if held is None:
            raise TransferNotFoundException()
        if not held:
            # The longer record was written before the cap answered. Put the
            # previous lifetime back, or the objects outlive their cap entry.

            def restore(data: dict) -> dict:
                fresh = TransferModel.model_validate(data)
                fresh.expires_at = previous_expires_at
                return fresh.model_dump()

            await self.redis.mutate_json(key, restore, ttl_seconds=previous_ttl)
            raise LimitExceededException(
                "High server traffic. Active storage capacity limit "
                "reached. Please try again later."
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
            secrets_match(access_token, None)
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
        transfer = TransferModel.model_validate(data) if data else None
        # A wrong secret answers the same 204 as an unknown id, and deletes
        # nothing. The comparison still runs when the record is missing.
        matched = secrets_match(
            sender_secret, transfer.sender_secret if transfer is not None else None
        )
        if transfer is None or not matched:
            return
        await self.redis.delete_transfer(transfer_id)

        client = await self.redis.get_client()
        lock_key = f"{ACTIVE_TRANSFERS_KEY}:lock"
        token = await self.redis.acquire_record_lock(client, lock_key)
        try:
            # A completion can add a landed member after this snapshot was
            # read. Every member for this transfer goes, not only the two
            # names known before the delete.
            prefix = f"{transfer_id}:"
            rows = await client.zrange(ACTIVE_TRANSFERS_KEY, 0, -1)
            for member in rows:
                name = str(member)
                if name.startswith(prefix):
                    await client.zrem(ACTIVE_TRANSFERS_KEY, name)
        finally:
            await self.redis.release_record_lock(client, lock_key, token)
        await self._release_open_slot(client_ip, transfer_id)

        file_ids = [f.file_id for f in transfer.files]
        await self.r2.delete_objects(transfer_id, file_ids)
