import pytest

import statsig_client as sc


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    sc.reset_for_tests()
    monkeypatch.delenv("STATSIG_SERVER_KEY", raising=False)
    yield
    sc.reset_for_tests()


def test_gate_false_when_unconfigured():
    assert sc.byok_gate_passes("user_1") is False


def test_gate_false_on_sdk_error(monkeypatch):
    class Boom:
        def check_gate(self, *a, **k):
            raise RuntimeError("sdk down")

    monkeypatch.setattr(sc, "_get_statsig", lambda: Boom())
    assert sc.byok_gate_passes("user_1") is False


def test_gate_true_passthrough(monkeypatch):
    class Passing:
        def check_gate(self, user, name):
            assert name == sc.BYOK_GATE
            return True

    monkeypatch.setattr(sc, "_get_statsig", lambda: Passing())
    assert sc.byok_gate_passes("user_1", org_id="o", experiment_name="e") is True
