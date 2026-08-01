import pytest

from oddish.evals.prompt_eval.grading import grade


class TestPostTrial:
    def test_exact_classification_passes(self):
        result = grade(
            "QA_POST_TRIAL",
            {"classification": "GOOD_SUCCESS"},
            {"classification": "GOOD_SUCCESS", "subtype": "Correct Solution"},
        )
        assert result.passed

    def test_wrong_classification_fails_with_detail(self):
        result = grade(
            "QA_POST_TRIAL",
            {"classification": "GOOD_SUCCESS"},
            {"classification": "BAD_SUCCESS", "subtype": "Hardcoding"},
        )
        assert not result.passed
        assert "BAD_SUCCESS" in result.detail

    def test_classification_in_accepts_any_listed(self):
        expected = {"classification_in": ["GOOD_FAILURE", "BAD_FAILURE"]}
        assert grade(
            "QA_POST_TRIAL", expected, {"classification": "BAD_FAILURE"}
        ).passed
        assert not grade(
            "QA_POST_TRIAL", expected, {"classification": "GOOD_SUCCESS"}
        ).passed

    def test_subtype_constraint(self):
        expected = {"classification": "GOOD_FAILURE", "subtype_in": ["Timeout"]}
        output = {"classification": "GOOD_FAILURE", "subtype": "Wrong Approach"}
        result = grade("QA_POST_TRIAL", expected, output)
        assert not result.passed
        assert "Wrong Approach" in result.detail

    def test_missing_expected_classification_raises(self):
        with pytest.raises(ValueError):
            grade("QA_POST_TRIAL", {}, {"classification": "GOOD_SUCCESS"})


class TestPreTrial:
    ITEMS = {
        "items": [
            {
                "dimension": "info_leakage",
                "tier": "must_fix",
                "file": "environment/Dockerfile",
                "title": "The expected output ships into the workspace",
            },
            {
                "dimension": "verifier",
                "tier": "should_fix",
                "file": "tests/test.sh",
                "title": "The verifier ignores stderr",
            },
        ]
    }

    def test_matcher_hits(self):
        expected = {
            "items": [
                {"dimension": "info_leakage", "tier_in": ["must_fix", "should_fix"]}
            ]
        }
        assert grade("QA_PRE_TRIAL", expected, self.ITEMS).passed

    def test_file_contains_is_case_insensitive_substring(self):
        expected = {
            "items": [{"dimension": "info_leakage", "file_contains": "dockerfile"}]
        }
        assert grade("QA_PRE_TRIAL", expected, self.ITEMS).passed

    def test_unmatched_matcher_fails(self):
        expected = {"items": [{"dimension": "oracle"}]}
        result = grade("QA_PRE_TRIAL", expected, self.ITEMS)
        assert not result.passed
        assert "oracle" in result.detail

    def test_forbidden_matcher_fails_on_hit(self):
        expected = {"items": [], "forbidden": [{"dimension": "verifier"}]}
        result = grade("QA_PRE_TRIAL", expected, self.ITEMS)
        assert not result.passed
        assert "forbidden" in result.detail

    def test_empty_expectations_pass_on_empty_output(self):
        assert grade("QA_PRE_TRIAL", {"items": []}, {"items": []}).passed


class TestVerdict:
    def test_is_good_match(self):
        assert grade("VERDICT", {"is_good": True}, {"is_good": True}).passed
        assert not grade("VERDICT", {"is_good": True}, {"is_good": False}).passed

    def test_confidence_constraint(self):
        expected = {"is_good": False, "confidence_in": ["high"]}
        assert grade(
            "VERDICT", expected, {"is_good": False, "confidence": "high"}
        ).passed
        result = grade("VERDICT", expected, {"is_good": False, "confidence": "low"})
        assert not result.passed

    def test_missing_is_good_raises(self):
        with pytest.raises(ValueError):
            grade("VERDICT", {}, {"is_good": True})


def test_unknown_kind_raises():
    with pytest.raises(ValueError):
        grade("TRAJECTORY_SUMMARY", {}, {})
