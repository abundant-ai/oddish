"""--environment-kwarg values must carry JSON-literal types, not raw strings.

A boolean kwarg like flex_start=false previously reached the harbor
environment constructor as the truthy string "false", making boolean and
numeric kwargs impossible to set per-submission.
"""

from oddish.cli.api import _coerce_kwarg_values


def test_booleans_and_null_become_typed():
    coerced = _coerce_kwarg_values(
        {"flex_start": "false", "other": "true", "maybe": "null"}
    )
    assert coerced == {"flex_start": False, "other": True, "maybe": None}


def test_numbers_become_typed():
    coerced = _coerce_kwarg_values(
        {"pod_ready_timeout_sec": "900", "multiplier": "1.5"}
    )
    assert coerced == {"pod_ready_timeout_sec": 900, "multiplier": 1.5}


def test_plain_strings_pass_through():
    coerced = _coerce_kwarg_values(
        {
            "cluster_name": "oddish-tpu-usw4",
            "region": "us-west4",
            "topology": "2x2",
            "agent_tools_image": "ghcr.io/org/tools:tag",
        }
    )
    assert coerced == {
        "cluster_name": "oddish-tpu-usw4",
        "region": "us-west4",
        "topology": "2x2",
        "agent_tools_image": "ghcr.io/org/tools:tag",
    }


def test_quoting_forces_string():
    # JSON-quoted values are the escape hatch for literal strings that would
    # otherwise coerce ("false", "123").
    coerced = _coerce_kwarg_values({"tag": '"123"', "name": '"false"'})
    assert coerced == {"tag": "123", "name": "false"}


def test_json_objects_pass_through_typed():
    coerced = _coerce_kwarg_values({"mounts": '{"host": "/x", "pod": "/y"}'})
    assert coerced == {"mounts": {"host": "/x", "pod": "/y"}}


def test_nonstandard_json_constants_stay_strings():
    # json.loads would accept these as floats, which break strict JSON
    # serialization downstream (API payload, JSONB); they must stay strings.
    coerced = _coerce_kwarg_values(
        {"tag": "NaN", "x": "Infinity", "y": "-Infinity"}
    )
    assert coerced == {"tag": "NaN", "x": "Infinity", "y": "-Infinity"}
