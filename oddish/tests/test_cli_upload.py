from __future__ import annotations

from pathlib import Path
import sys

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.cli import api as cli_api


class _RetryingUploadClient:
    def __init__(self):
        self.payloads: list[bytes] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def put(
        self,
        _url: str,
        *,
        headers: dict[str, str],
        content: object,
    ) -> httpx.Response:
        assert headers["Content-Length"] == "7"
        assert hasattr(content, "read")
        self.payloads.append(content.read())
        if len(self.payloads) == 1:
            raise httpx.ReadError("transient tls failure")
        return httpx.Response(204)


def test_upload_to_presigned_url_retries_transport_error(
    monkeypatch, tmp_path: Path
) -> None:
    tarball_path = tmp_path / "task.tar.gz"
    tarball_path.write_bytes(b"payload")
    upload_client = _RetryingUploadClient()

    def fake_client(*, timeout: float, follow_redirects: bool):
        assert timeout == 600.0
        assert follow_redirects is True
        return upload_client

    monkeypatch.setattr(cli_api.httpx, "Client", fake_client)
    monkeypatch.setattr(cli_api.time, "sleep", lambda _seconds: None)

    cli_api._upload_to_presigned_url(
        "https://storage.example/upload",
        tarball_path,
        {"Content-Type": "application/gzip"},
    )

    assert upload_client.payloads == [b"payload", b"payload"]
