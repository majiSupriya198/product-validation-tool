import pytest

from comparators.base import CompareContext
from comparators.exact import compare_unit
from comparators.numeric import compare_numeric

CTX = CompareContext("x")


@pytest.mark.parametrize("up,prod,score", [
    ("4.5", 4.469387755102041, 1.0),   # display-rounded rating
    ("4.5", 4.4, 0.0),                 # genuinely different
    ("4", 4.469, 0.0),                 # no decimals uploaded: no rounding leniency
    ("3656", 5286, 0.0),
])
def test_round_to_uploaded_precision(up, prod, score):
    assert compare_numeric(up, prod, CTX).score == score


def test_rounding_can_be_disabled():
    ctx = CompareContext("x", round_to_uploaded=False)
    assert compare_numeric("4.5", 4.469387755102041, ctx).score == 0.0


@pytest.mark.parametrize("a,b,score", [("EA", "UNIT", 1.0), ("pcs", "each", 1.0), ("KG", "kilogram", 1.0),
                                       ("g", "KG", 0.0), ("LITER", "UNIT", 0.0)])
def test_unit_labels(a, b, score):
    assert compare_unit(a, b, CTX).score == score
