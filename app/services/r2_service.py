import logging
import urllib.parse
import anyio
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from app.core.config import settings
from app.core.exceptions import ServiceUnavailableException

logger = logging.getLogger(__name__)


class R2Service:
    def __init__(self) -> None:
        endpoint_url = f"https://{settings.R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
        self.bucket_name = settings.R2_BUCKET_NAME

        try:
            self._client = boto3.client(
                "s3",
                endpoint_url=endpoint_url,
                aws_access_key_id=settings.R2_ACCESS_KEY_ID,
                aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
                config=Config(signature_version="s3v4", region_name="auto"),
            )
        except Exception as e:
            logger.error(f"Failed to initialize R2 client: {e}")
            raise ServiceUnavailableException("R2 storage client initialization failed")

    def _get_object_key(self, transfer_id: str, file_id: str) -> str:
        return f"transfers/{transfer_id}/{file_id}"

    def _sanitize_filename(self, filename: str) -> str:
        # RFC 5987 attr-char excludes the path separator, so it is encoded
        # too: a name that carries one would otherwise travel into the
        # Content-Disposition header as a path.
        return urllib.parse.quote(filename, safe="")

    async def generate_upload_url(
        self, transfer_id: str, file_id: str, content_type: str, content_length: int
    ) -> str:
        key = self._get_object_key(transfer_id, file_id)

        def _generate() -> str:
            return self._client.generate_presigned_url(
                ClientMethod="put_object",
                Params={
                    "Bucket": self.bucket_name,
                    "Key": key,
                    "ContentType": content_type,
                    "ContentLength": content_length,
                },
                ExpiresIn=settings.UPLOAD_URL_LIFETIME_SECONDS,
            )

        try:
            return await anyio.to_thread.run_sync(_generate)
        except ClientError as e:
            logger.error(f"Failed to generate upload URL: {e}")
            raise ServiceUnavailableException("Failed to generate upload URL")

    async def generate_download_url(self, transfer_id: str, file_id: str, filename: str) -> str:
        key = self._get_object_key(transfer_id, file_id)
        encoded_filename = self._sanitize_filename(filename)

        def _generate() -> str:
            disposition = f"attachment; filename*=UTF-8''{encoded_filename}"
            return self._client.generate_presigned_url(
                ClientMethod="get_object",
                Params={
                    "Bucket": self.bucket_name,
                    "Key": key,
                    "ResponseContentDisposition": disposition,
                },
                ExpiresIn=settings.DOWNLOAD_URL_LIFETIME_SECONDS,
            )

        try:
            return await anyio.to_thread.run_sync(_generate)
        except ClientError as e:
            logger.error(f"Failed to generate download URL: {e}")
            raise ServiceUnavailableException("Failed to generate download URL")

    async def verify_file_exists(self, transfer_id: str, file_id: str) -> int:
        key = self._get_object_key(transfer_id, file_id)

        def _check() -> int:
            response = self._client.head_object(Bucket=self.bucket_name, Key=key)
            return int(response.get("ContentLength", 0))

        try:
            return await anyio.to_thread.run_sync(_check)
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            if error_code == "404":
                logger.warning(f"R2 Object not found: {key}")
                raise FileNotFoundError("Object not found in R2 storage")
            logger.error(f"R2 head_object failed for {key}: {e}")
            raise ServiceUnavailableException("R2 storage check failed")

    async def delete_objects(self, transfer_id: str, file_ids: list[str]) -> None:
        if not file_ids:
            return

        objects = [{"Key": self._get_object_key(transfer_id, fid)} for fid in file_ids]

        def _delete() -> None:
            self._client.delete_objects(
                Bucket=self.bucket_name, Delete={"Objects": objects, "Quiet": True}
            )

        try:
            await anyio.to_thread.run_sync(_delete)
        except ClientError as e:
            logger.warning(f"Failed to delete objects in R2 for transfer {transfer_id}: {e}")


r2_service = R2Service()
