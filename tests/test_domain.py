from decimal import Decimal

from student_agent.domain import collect, first, money, unique_strings


def test_money_uses_decimal_and_brl_rounding() -> None:
    assert money("10.005") == Decimal("10.01")
    assert money(-1) is None
    assert money(True) is None


def test_nested_extractors_preserve_repeated_payment_rows() -> None:
    data = {"payments": [{"payment_value": 10}, {"payment_value": 10}]}
    assert collect(data, "payment_value") == [10, 10]
    assert first(data, "payment_value") == 10
    assert unique_strings(["a", "a", None, "b"]) == ["a", "b"]
