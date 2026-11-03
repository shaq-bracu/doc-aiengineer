import pytest
from app.extraction.utils import amount_in_text, date_in_text, normalize_date, parse_amount


@pytest.mark.parametrize("raw,exp", [("1,234.50", 1234.5), ("$ 99", 99.0), ("1.234,56", 1234.56), ("12,50", 12.5), ("(120.00)", -120.0),
                                     ("-5", -5.0), (42, 42.0), ("abc", None), ("", None), (None, None), ("1,234", 1234.0)])
def test_parse_amount(raw, exp):
    assert parse_amount(raw) == exp


def test_normalize_date_variants():
    assert normalize_date("2025-03-14") == ("2025-03-14", 1.0)
    assert normalize_date("25/03/2025")[0] == "2025-03-25"
    assert normalize_date("14 March 2025")[0] == "2025-03-14"
    assert normalize_date("March 14, 2025")[0] == "2025-03-14"
    assert normalize_date("nonsense") == (None, 0.0)
    assert normalize_date("31/02/2025") == (None, 0.0)          # impossible date -> null, not "fixed"


def test_ambiguous_date_gets_low_confidence():
    iso, conf = normalize_date("03/04/2025", dayfirst=True)
    assert iso == "2025-04-03" and conf < 0.7


def test_grounding_helpers():
    assert amount_in_text("Total: $1,234.50", 1234.5) and not amount_in_text("Total: $1,234.50", 999.99)
    assert date_in_text("Issued: 14 March 2025", "2025-03-14") and not date_in_text("Issued: 14 March 2025", "2025-03-15")
