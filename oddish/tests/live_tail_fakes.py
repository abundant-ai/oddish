import base64
import contextlib

from sqlalchemy.dialects.postgresql import Insert as PGInsert

from oddish.workers.harbor import live_tail


class FakeResult:
    def __init__(self, stdout="", return_code=0):
        self.stdout = stdout
        self.return_code = return_code


class FakeEnv:
    def __init__(self, results):
        self.results = list(results)
        self.commands = []

    async def exec(self, command, timeout_sec=None):
        self.commands.append(command)
        result = self.results.pop(0) if self.results else FakeResult()
        if isinstance(result, Exception):
            raise result
        return result


def b64(raw: bytes) -> FakeResult:
    return FakeResult(stdout=base64.b64encode(raw).decode())


class FakeExecuteResult:
    def __init__(self, rowcount=1):
        self.rowcount = rowcount


class FakeSession:
    def __init__(self, rowcount=1, fail_inserts=False):
        self.stmts = []
        self.params = []
        self.rowcount = rowcount
        self.fail_inserts = fail_inserts

    async def execute(self, stmt, params=None):
        if self.fail_inserts and isinstance(stmt, PGInsert):
            raise RuntimeError("insert failed")
        self.stmts.append(stmt)
        self.params.append(params)
        return FakeExecuteResult(self.rowcount)


def patch_db(monkeypatch, module=live_tail, price=None, **kwargs):
    session = FakeSession(**kwargs)

    @contextlib.asynccontextmanager
    async def fake_get_session():
        yield session

    monkeypatch.setattr(module, "get_session", fake_get_session)
    if module is live_tail:
        monkeypatch.setattr(live_tail, "estimate_cost_usd", lambda *_a, **_k: price)
    return session


def update_params(session):
    return [
        dict(s.compile().params)
        for s in session.stmts
        if not isinstance(s, PGInsert)
    ]
