"""Amount parsing and formatting, text folding, dates."""

from datetime import date
from decimal import Decimal

import pytest

from financeproject.core.dates import (
    add_months,
    business_days,
    month_end,
    month_label,
    months_between,
    parse_date,
    parse_month,
    sample_dates,
)
from financeproject.core.money import (
    check_decimals,
    format_amount,
    infer_decimal_separator,
    is_ambiguous_amount,
    parse_decimal,
    set_number_format,
)
from financeproject.core.text import fold
from financeproject.errors import AmountError, DateError


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1234.56", "1234.56"),
        ("1.234,56", "1234.56"),
        ("1,234.56", "1234.56"),
        ("1234,56", "1234.56"),
        ("-450", "-450"),
        ("(12,5)", "-12.5"),
        ("1.234.567", "1234567"),
        ("1,234,567", "1234567"),
        ("₺ 1.234,50", "1234.50"),
        ("1 234,50 TL", "1234.50"),
        ("450-", "-450"),
        ("+7", "7"),
        (".5", "0.5"),
        ("1.234", "1.234"),
        ("USD", None),
    ],
)
def test_parse_decimal_formats(text, expected):
    if expected is None:
        with pytest.raises(AmountError):
            parse_decimal(text)
    else:
        assert parse_decimal(text) == Decimal(expected)


def test_parse_decimal_with_explicit_separator():
    assert parse_decimal("1.234", decimal_sep=",") == Decimal(1234)
    assert parse_decimal("1.234,5", decimal_sep=",") == Decimal("1234.5")
    assert parse_decimal("1,234.5", decimal_sep=".") == Decimal("1234.5")


@pytest.mark.parametrize("bad", ["", "abc", "1,2,3.4.5", "1e5", "--", "TL"])
def test_parse_decimal_rejects(bad):
    with pytest.raises(AmountError):
        parse_decimal(bad)


def test_parse_decimal_refuses_floats_and_bools():
    with pytest.raises(AmountError):
        parse_decimal(1.5)
    with pytest.raises(AmountError):
        parse_decimal(True)
    assert parse_decimal(7) == Decimal(7)


def test_infer_decimal_separator_from_a_column():
    assert infer_decimal_separator(["-450,00", "65.000,00", "12"]) == ","
    assert infer_decimal_separator(["-450.00", "65,000.00"]) == "."
    assert infer_decimal_separator(["1.234.567"]) == ","  # dots group thousands
    assert infer_decimal_separator(["12,5"]) == ","
    assert infer_decimal_separator(["1.234", "-450"]) is None  # undecided
    assert is_ambiguous_amount("1.234") and is_ambiguous_amount("-1,500")
    assert not is_ambiguous_amount("1.23") and not is_ambiguous_amount("1.234,5")
    with pytest.raises(AmountError):
        infer_decimal_separator(["1.234,56", "1,234.56"])


def test_format_amount_both_styles():
    assert format_amount(Decimal("1234567.891")) == "1,234,567.89"
    assert format_amount(Decimal("-1234.5")) == "-1,234.50"
    assert format_amount(Decimal("0.005")) == "0.01"  # half up
    assert format_amount(Decimal("1234.5"), 0) == "1,235"
    assert format_amount(Decimal("1"), sign=True) == "+1.00"
    assert format_amount(None) == "—"
    assert format_amount(float("nan")) == "—"  # missing values in report tables
    assert format_amount(2.5, 1) == "2.5"
    set_number_format("1.234,56")
    assert format_amount(Decimal("1234567.891")) == "1.234.567,89"
    assert format_amount(Decimal("-1234.5")) == "-1.234,50"
    with pytest.raises(AmountError):
        set_number_format("1 234,56")


def test_check_decimals():
    check_decimals(Decimal("1.20"), 1)
    check_decimals(Decimal("5"), 0)
    with pytest.raises(AmountError):
        check_decimals(Decimal("1.234"), 2)


def test_fold_ignores_case_and_turkish_letters():
    assert fold("GIDA:Market") == fold("Gıda:market") == "gida:market"
    assert fold("İSTANBUL") == fold("istanbul") == "istanbul"
    assert fold("  Ödeme   Şubesi ") == "odeme subesi"
    assert fold("Çağrı") == "cagri"
    assert fold("Café") == "cafe"


def test_parse_date_variants():
    today = date(2026, 10, 9)
    assert parse_date("2026-10-09") == today
    assert parse_date("09.10.2026") == today
    assert parse_date("9/10/2026") == today  # day first
    assert parse_date("today", today) == today
    assert parse_date("Yesterday", today) == date(2026, 10, 8)
    with pytest.raises(DateError):
        parse_date("2026-02-30")
    with pytest.raises(DateError):
        parse_date("tomorrow morning")


def test_month_helpers():
    assert add_months(date(2026, 11, 1), 3) == date(2027, 2, 1)
    assert add_months(date(2026, 1, 31), -1) == date(2025, 12, 1)
    assert month_end(date(2028, 2, 10)) == date(2028, 2, 29)
    assert months_between(date(2025, 11, 5), date(2026, 2, 1)) == 3
    assert parse_month("2026-9") == parse_month("09-2026") == date(2026, 9, 1)
    assert month_label(date(2026, 9, 1)) == "September 2026"
    with pytest.raises(DateError):
        parse_month("2026-13")


def test_business_days_skip_weekends():
    days = list(business_days(date(2026, 10, 1), date(2026, 10, 9)))
    assert days == [date(2026, 10, d) for d in (1, 2, 5, 6, 7, 8, 9)]


def test_sample_dates():
    assert sample_dates(date(2026, 1, 15), date(2026, 3, 10)) == [
        date(2026, 1, 31),
        date(2026, 2, 28),
        date(2026, 3, 10),
    ]
    assert sample_dates(date(2026, 1, 1), date(2026, 3, 31)) == [
        date(2026, 1, 31),
        date(2026, 2, 28),
        date(2026, 3, 31),
    ]
    weekly = sample_dates(date(2026, 10, 1), date(2026, 10, 20), "week")
    assert weekly == [date(2026, 10, 4), date(2026, 10, 11), date(2026, 10, 18), date(2026, 10, 20)]
    assert len(sample_dates(date(2026, 10, 1), date(2026, 10, 3), "day")) == 3
    assert sample_dates(date(2026, 10, 3), date(2026, 10, 1)) == []
    with pytest.raises(DateError):
        sample_dates(date(2026, 1, 1), date(2026, 2, 1), "year")
