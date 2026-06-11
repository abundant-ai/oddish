import pytest
from oddish.mcp import s3_server


@pytest.mark.asyncio
async def test_grep_matches_lines(monkeypatch):
    async def fake_list(exp, prefix):
        return ["tasks/a/trials/a-0/log.txt"]

    async def fake_read(exp, key, offset, length):
        return "alpha\nBETA match\ngamma\n"

    monkeypatch.setattr(s3_server, "list_scoped", fake_list)
    monkeypatch.setattr(s3_server, "read_scoped", fake_read)
    s3_server._current_experiment.set("exp1")

    hits = await s3_server._grep_impl("match", prefix="", max_matches=10)
    assert hits == [
        {"key": "tasks/a/trials/a-0/log.txt", "line_no": 2, "line": "BETA match"}
    ]
