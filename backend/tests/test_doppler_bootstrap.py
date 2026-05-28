import json
from urllib.parse import parse_qs, urlparse

import doppler_bootstrap


class _FakeResponse:
    def __init__(self, payload: dict[str, str]):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return json.dumps(self.payload).encode()


def _reset(monkeypatch):
    monkeypatch.setattr(doppler_bootstrap, "_LOADED", False)
    for key in [
        "DOPPLER_TOKEN",
        "DOPPLER_PROJECT",
        "DOPPLER_CONFIG",
        "DOPPLER_SECRETS_OVERRIDE",
        "EXISTING_SECRET",
        "NEW_SECRET",
    ]:
        monkeypatch.delenv(key, raising=False)


def test_load_doppler_secrets_noops_without_token(monkeypatch):
    _reset(monkeypatch)

    assert doppler_bootstrap.load_doppler_secrets() is False


def test_load_doppler_secrets_populates_environment(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setenv("DOPPLER_TOKEN", "dp.st.test")
    monkeypatch.setenv("DOPPLER_PROJECT", "oddish")
    monkeypatch.setenv("DOPPLER_CONFIG", "prd")
    seen = {}

    def fake_urlopen(request, timeout):
        seen["timeout"] = timeout
        seen["url"] = request.full_url
        seen["auth"] = request.headers["Authorization"]
        return _FakeResponse({"NEW_SECRET": "loaded"})

    monkeypatch.setattr(doppler_bootstrap.urllib.request, "urlopen", fake_urlopen)

    assert doppler_bootstrap.load_doppler_secrets() is True
    assert seen["timeout"] == 20
    assert seen["auth"] == "Bearer dp.st.test"
    query = parse_qs(urlparse(seen["url"]).query)
    assert query["project"] == ["oddish"]
    assert query["config"] == ["prd"]
    assert query["format"] == ["json"]
    assert doppler_bootstrap.os.environ["NEW_SECRET"] == "loaded"


def test_load_doppler_secrets_can_preserve_existing_env(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setenv("DOPPLER_TOKEN", "dp.st.test")
    monkeypatch.setenv("DOPPLER_SECRETS_OVERRIDE", "false")
    monkeypatch.setenv("EXISTING_SECRET", "platform-value")

    def fake_urlopen(_request, timeout):
        assert timeout == 20
        return _FakeResponse(
            {
                "EXISTING_SECRET": "doppler-value",
                "NEW_SECRET": "loaded",
            }
        )

    monkeypatch.setattr(doppler_bootstrap.urllib.request, "urlopen", fake_urlopen)

    assert doppler_bootstrap.load_doppler_secrets() is True
    assert doppler_bootstrap.os.environ["EXISTING_SECRET"] == "platform-value"
    assert doppler_bootstrap.os.environ["NEW_SECRET"] == "loaded"
