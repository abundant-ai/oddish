"""QA feedback validation at the API schema boundary."""

import pytest
from pydantic import ValidationError

from oddish.schemas import FeedbackCreate


@pytest.mark.parametrize(
    "data",
    [
        {},
        {
            "target": "experiment",
            "target_key": "x",
            "vote": "agree",
            "trial_id": "trial-1",
        },
        {
            "target": "qa_verdict",
            "target_key": " ",
            "vote": "agree",
            "trial_id": "trial-1",
        },
        {
            "target": "qa_verdict",
            "target_key": "BAD_FAILURE",
            "vote": "maybe",
            "trial_id": "trial-1",
        },
        {
            "target": "qa_verdict",
            "target_key": "BAD_FAILURE",
            "vote": "agree",
            "trial_id": " ",
        },
    ],
)
def test_feedback_create_rejects_invalid_votes(data: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        FeedbackCreate.model_validate(data)


def test_feedback_create_normalizes_valid_vote() -> None:
    request = FeedbackCreate(
        body="  needs context  ",
        target="qa_action_item",
        target_key="  action-1  ",
        vote="disagree",
        trial_id="trial-1",
    )

    assert request.body == "needs context"
    assert request.target_key == "action-1"
