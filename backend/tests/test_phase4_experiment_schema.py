import pytest

from app.services.experiment_schema import Experiment001, HumanReview


def test_human_review_requires_explicit_one_to_five_scores():
    review = HumanReview(*(3 for _ in range(8)))
    assert review.readability == 3
    with pytest.raises(ValueError):
        HumanReview(0, 3, 3, 3, 3, 3, 3, 3)


def test_experiment_001_is_prepared_but_cannot_run_without_cost_and_approval():
    experiment = Experiment001()
    assert experiment.status == "prepared_do_not_run"
    assert experiment.may_run() is False
