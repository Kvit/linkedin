"""An in-memory stand-in for `google.cloud.storage.Client`: only the calls
`linkedinmcp.documents` makes."""

from datetime import UTC, datetime
from types import SimpleNamespace

from google.api_core.exceptions import NotFound


class FakeStorage:
    def __init__(self) -> None:
        self.files: dict[str, SimpleNamespace] = {}

    def add(
        self, name: str, data: bytes = b"%PDF-1.7", content_type: str = "application/pdf",
        updated: datetime = datetime(2026, 9, 8, tzinfo=UTC),
    ) -> None:
        """Store one object under its full name, e.g. `marketing/a.pdf`."""
        self.files[name] = SimpleNamespace(
            name=name, size=len(data), updated=updated, content_type=content_type, download_as_bytes=lambda: data,
        )

    def list_blobs(self, bucket_name: str, prefix: str = "", delimiter: str | None = None) -> list:
        """The objects under `prefix`; with a delimiter, its direct children only."""
        return [
            blob for name, blob in sorted(self.files.items())
            if name.startswith(prefix) and (delimiter is None or delimiter not in name[len(prefix):])
        ]

    def bucket(self, name: str) -> "FakeStorage":
        return self

    def get_blob(self, name: str):
        return self.files.get(name)

    def blob(self, name: str) -> SimpleNamespace:
        """A handle that reads on download, as the real one does: it then knows
        its content type, and a missing object raises `NotFound`."""
        handle = SimpleNamespace(name=name, content_type=None)

        def download() -> bytes:
            if name not in self.files:
                raise NotFound(name)
            handle.content_type = self.files[name].content_type
            return self.files[name].download_as_bytes()

        handle.download_as_bytes = download
        return handle
