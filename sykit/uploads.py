from __future__ import annotations

from typing import BinaryIO


class Upload:
    """A temporary, disk-backed file received by an exposed endpoint.

    The file is closed automatically when the endpoint finishes. Copy any
    content that must outlive the request while the endpoint is running.
    Client-provided names and content types are untrusted metadata.
    """

    def __init__(
        self,
        file: BinaryIO,
        *,
        size: int,
        client_filename: str,
        client_content_type: str | None,
    ) -> None:
        self.file = file
        self.size = size
        self.client_filename = client_filename
        self.client_content_type = client_content_type

    @property
    def closed(self) -> bool:
        return self.file.closed

    def read(self, size: int = -1) -> bytes:
        return self.file.read(size)

    def seek(self, offset: int, whence: int = 0) -> int:
        return self.file.seek(offset, whence)

    def tell(self) -> int:
        return self.file.tell()


__all__ = ["Upload"]
