"""File downloader for Slack uploaded files."""

import os
from pathlib import Path
from typing import Any

import aiofiles
import aiohttp
from loguru import logger
from slack_sdk.web.async_client import AsyncWebClient


class FileTooLargeError(Exception):
    """Raised when file exceeds size limit."""

    def __init__(self, filename: str, size_bytes: int, max_size_bytes: int):
        self.filename = filename
        self.size_mb = size_bytes / (1024 * 1024)
        self.max_mb = max_size_bytes / (1024 * 1024)
        super().__init__(
            f"File {filename} is too large ({self.size_mb:.1f}MB, max: {self.max_mb}MB)"
        )


class FileDownloadError(Exception):
    """Raised when file download fails."""

    pass


async def download_slack_file(
    client: AsyncWebClient,
    file_id: str,
    slack_bot_token: str,
    destination_dir: str,
    max_size_bytes: int = 10_485_760,
) -> tuple[str, dict[str, Any]]:
    """Download file from Slack to local directory.

    Args:
        client: Slack async web client
        file_id: Slack file ID
        slack_bot_token: Bot token for authentication
        destination_dir: Local directory to save file
        max_size_bytes: Maximum file size in bytes (default 10MB)

    Returns:
        Tuple of (local_path, metadata_dict)

    Raises:
        FileTooLargeError: File exceeds max_size_bytes
        FileDownloadError: Download failed
    """
    try:
        # Get file info from Slack
        file_info = await client.files_info(file=file_id)

        if not file_info["ok"]:
            raise FileDownloadError(f"Failed to get file info: {file_info.get('error')}")

        file_data = file_info["file"]
        filename = file_data["name"]
        file_size = file_data.get("size", 0)
        file_url = file_data.get("url_private")

        if not file_url:
            raise FileDownloadError(f"No private URL for file {filename}")

        # Check file size
        if file_size > max_size_bytes:
            raise FileTooLargeError(filename, file_size, max_size_bytes)

        # Create destination directory
        os.makedirs(destination_dir, exist_ok=True)

        # Sanitize filename to prevent path traversal
        safe_filename = os.path.basename(filename.replace("..", "_"))
        if not safe_filename or safe_filename.startswith("."):
            safe_filename = f"upload_{file_id}"

        # Determine local path (handle duplicate filenames)
        base_path = Path(destination_dir) / safe_filename
        local_path = base_path
        counter = 1
        while local_path.exists():
            stem = base_path.stem
            suffix = base_path.suffix
            local_path = Path(destination_dir) / f"{stem}_{counter}{suffix}"
            counter += 1

        # Download file using aiohttp with Slack authorization
        timeout = aiohttp.ClientTimeout(total=300, connect=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            headers = {"Authorization": f"Bearer {slack_bot_token}"}
            async with session.get(file_url, headers=headers) as response:
                if response.status != 200:
                    raise FileDownloadError(f"Failed to download file: HTTP {response.status}")

                # Verify content length if available
                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > max_size_bytes:
                    raise FileTooLargeError(safe_filename, int(content_length), max_size_bytes)

                # Write file asynchronously with size verification
                bytes_downloaded = 0
                async with aiofiles.open(local_path, "wb") as f:
                    while True:
                        chunk = await response.content.read(8192)
                        if not chunk:
                            break
                        bytes_downloaded += len(chunk)
                        if bytes_downloaded > max_size_bytes:
                            # Clean up partial file
                            await f.close()
                            os.remove(local_path)
                            raise FileTooLargeError(safe_filename, bytes_downloaded, max_size_bytes)
                        await f.write(chunk)

        logger.info(f"Downloaded file {filename} ({file_size} bytes) to {local_path}")

        # Prepare metadata
        metadata = {
            "slack_file_id": file_id,
            "filename": filename,
            "mimetype": file_data.get("mimetype", ""),
            "size": file_size,
            "local_path": str(local_path),
        }

        return str(local_path), metadata

    except FileTooLargeError:
        raise
    except FileDownloadError:
        raise
    except Exception as e:
        logger.error(f"Error downloading file {file_id}: {e}")
        raise FileDownloadError(f"Failed to download file: {e}")
