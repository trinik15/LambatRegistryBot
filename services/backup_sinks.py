"""Off-site backup sinks.

The local disk on a free-tier host (Render/Railway/Fly.io) is ephemeral — if
the container is recreated, every backup (including the emergency pre-restore
safety net) is gone. This module uploads each backup to a second location in
addition to the local copy.

Three sinks are supported, selected by ``BACKUP_SINK``:

* ``local`` (default) — no upload; keeps the legacy disk-only behaviour.
* ``s3``   — uploads to an S3-compatible bucket via boto3.
  Requires ``pip install boto3``. Credentials come from boto3's default
  credential chain (env vars ``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY``,
  an AWS profile, or an IAM role). Set ``BACKUP_S3_BUCKET`` (and optionally
  ``BACKUP_S3_PREFIX``).
* ``gcs``  — uploads to a Google Cloud Storage bucket.
  Requires ``pip install google-cloud-storage``. Credentials come from
  ``GOOGLE_APPLICATION_CREDENTIALS``. Set ``BACKUP_GCS_BUCKET`` (and
  optionally ``BACKUP_GCS_PREFIX``).

Cloud SDK imports are **lazy** (inside the methods), so a deployment that only
uses the ``local`` sink never needs boto3 / google-cloud-storage installed.

All sinks are best-effort from the caller's perspective: if an upload fails,
the local backup is still preserved and the bot keeps running — we just log the
failure loudly. Losing the off-site copy is bad; losing the whole backup
because the upload raised would be worse.
"""

import asyncio
import logging
import os
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class BackupSink(ABC):
    """A destination for off-site backup uploads."""

    name: str = "abstract"

    @abstractmethod
    async def upload(self, local_path: str) -> str:
        """Upload a local file to the sink.

        Returns a remote URI/identifier (e.g. ``s3://bucket/key``) on success.
        Raises on failure. Implementations MUST run blocking I/O in a thread
        (e.g. ``asyncio.to_thread``) so they don't block the event loop.
        """


class LocalSink(BackupSink):
    """No-op sink: the file already lives on local disk."""

    name = "local"

    async def upload(self, local_path: str) -> str:
        return local_path


class S3Sink(BackupSink):
    """Upload to an S3-compatible bucket via boto3."""

    name = "s3"

    def __init__(self, bucket: str, prefix: str = ""):
        self.bucket = bucket
        # Strip leading/trailing slashes so the key is clean.
        self.prefix = prefix.strip("/")

    async def upload(self, local_path: str) -> str:
        # Lazy import so the dep is only required when actually used.
        try:
            import boto3
            from botocore.exceptions import BotoCoreError, ClientError
        except ImportError as e:
            raise RuntimeError(
                "BACKUP_SINK=s3 requires the 'boto3' package. Install it with: pip install boto3"
            ) from e

        def _sync_upload():
            client = boto3.client("s3")  # uses the default credential chain
            key = os.path.basename(local_path)
            if self.prefix:
                key = f"{self.prefix}/{key}"
            client.upload_file(local_path, self.bucket, key)
            return f"s3://{self.bucket}/{key}"

        try:
            return await asyncio.to_thread(_sync_upload)
        except (BotoCoreError, ClientError) as e:
            raise RuntimeError(f"S3 upload failed: {e}") from e


class GCSSink(BackupSink):
    """Upload to a Google Cloud Storage bucket."""

    name = "gcs"

    def __init__(self, bucket: str, prefix: str = ""):
        self.bucket = bucket
        self.prefix = prefix.strip("/")

    async def upload(self, local_path: str) -> str:
        try:
            from google.cloud import storage
        except ImportError as e:
            raise RuntimeError(
                "BACKUP_SINK=gcs requires the 'google-cloud-storage' package. "
                "Install it with: pip install google-cloud-storage"
            ) from e

        def _sync_upload():
            client = storage.Client()  # uses GOOGLE_APPLICATION_CREDENTIALS
            bucket = client.bucket(self.bucket)
            key = os.path.basename(local_path)
            if self.prefix:
                key = f"{self.prefix}/{key}"
            blob = bucket.blob(key)
            blob.upload_from_filename(local_path)
            return f"gs://{self.bucket}/{key}"

        try:
            return await asyncio.to_thread(_sync_upload)
        except Exception as e:
            raise RuntimeError(f"GCS upload failed: {e}") from e


def get_sink() -> BackupSink:
    """Build the configured backup sink from Config.

    Called per-backup (cheap). Raises ValueError if the config is invalid —
    but Config.validate_config() already guards this on import, so in practice
    we only reach here with a valid sink kind.
    """
    # Imported here to avoid a circular import at module load time.
    from core.config import Config

    kind = (Config.BACKUP_SINK or "local").lower()
    if kind == "local":
        return LocalSink()
    if kind == "s3":
        return S3Sink(Config.BACKUP_S3_BUCKET, Config.BACKUP_S3_PREFIX)
    if kind == "gcs":
        return GCSSink(Config.BACKUP_GCS_BUCKET, Config.BACKUP_GCS_PREFIX)
    raise ValueError(f"Unknown BACKUP_SINK: {kind!r}")


async def upload_to_sink(local_path: str) -> str | None:
    """Upload a file to the configured sink, logging failures but not raising.

    Returns the remote URI on success, or None if the sink is 'local' or the
    upload failed (the caller's local copy is still intact and authoritative).
    """
    try:
        sink = get_sink()
    except ValueError as e:
        logger.error(f"Backup sink misconfigured, skipping upload: {e}")
        return None

    try:
        remote = await sink.upload(local_path)
        logger.info(f"Backup uploaded to {sink.name} sink: {remote}")
        return remote
    except Exception as e:
        # The local copy is still the safety net — do NOT let a sink failure
        # fail the whole backup. Log loudly so an operator notices.
        logger.error(
            f"Backup sink upload FAILED ({sink.name}); local copy preserved at "
            f"{local_path}. Error: {e}",
            exc_info=True,
        )
        return None
