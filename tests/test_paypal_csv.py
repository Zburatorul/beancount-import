import csv
import os
import tempfile
from pathlib import Path

from beancount.core.number import D
from beancount.ingest.cache import get_file

from my_sources.paypal_csv import PAYPAL_HEADER, make_paypal_csv_importer


def _write_csv(rows):
    fd, path = tempfile.mkstemp(suffix=".CSV")
    os.close(fd)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=PAYPAL_HEADER)
        writer.writeheader()
        writer.writerows(rows)
    return path


def _extract(rows):
    path = Path(_write_csv(rows)).resolve()
    try:
        file = get_file(str(path))
        importer = make_paypal_csv_importer()
        return importer.extract(file)
    finally:
        os.unlink(path)


def _make_row(**overrides):
    row = {key: "" for key in PAYPAL_HEADER}
    row.update(overrides)
    return row


def test_hebergement_pending_rows_are_filtered():
    rows = [
        _make_row(
            **{
                "Date": "02/02/2025",
                "Time": "11:05:37",
                "TimeZone": "PST",
                "Name": "HEBERGEMENT OVH INC",
                "Type": "General Authorization",
                "Status": "Pending",
                "Currency": "USD",
                "Amount": "-4.20",
                "Fees": "0.00",
                "Total": "-4.20",
                "Balance": "-4.20",
                "Transaction ID": "0CP74536F54363018",
            }
        ),
        _make_row(
            **{
                "Date": "02/02/2025",
                "Time": "11:07:15",
                "TimeZone": "PST",
                "Name": "HEBERGEMENT OVH INC",
                "Type": "General Authorization",
                "Status": "Completed",
                "Currency": "USD",
                "Amount": "-4.20",
                "Fees": "0.00",
                "Total": "-4.20",
                "Balance": "-4.20",
                "Transaction ID": "0CP74536F54363018",
            }
        ),
        _make_row(
            **{
                "Date": "02/02/2025",
                "Time": "11:07:15",
                "TimeZone": "PST",
                "Name": "HEBERGEMENT OVH INC",
                "Type": "PreApproved Payment Bill User Payment",
                "Status": "Completed",
                "Currency": "USD",
                "Amount": "-4.20",
                "Fees": "0.00",
                "Total": "-4.20",
                "Balance": "-4.20",
                "Transaction ID": "57336330JN384850D",
            }
        ),
    ]

    entries = _extract(rows)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.narration == "PreApproved Payment Bill User Payment"
    asset = next(p for p in entry.postings if p.account == "Assets:Current:PayPal")
    assert asset.units.number == D("-4.20")
    assert asset.units.currency == "USD"
    assert entry.payee == "HEBERGEMENT OVH INC"


def test_eur_subscription_merges_with_us_conversion():
    rows = [
        _make_row(
            **{
                "Date": "01/19/2025",
                "Time": "02:32:54",
                "TimeZone": "PST",
                "Name": "Asociatia Reporteri de Investigatie si Securitate Editoriala din Moldova",
                "Type": "Subscription Payment",
                "Status": "Completed",
                "Currency": "EUR",
                "Amount": "-10.00",
                "Fees": "0.00",
                "Total": "-10.00",
                "Balance": "-10.00",
                "Transaction ID": "52A00030SR616783M",
            }
        ),
        _make_row(
            **{
                "Date": "01/19/2025",
                "Time": "02:32:54",
                "TimeZone": "PST",
                "Name": "General Currency Conversion",
                "Type": "General Currency Conversion",
                "Status": "Completed",
                "Currency": "USD",
                "Amount": "-10.76",
                "Fees": "0.00",
                "Total": "-10.76",
                "Exchange Rate": "0.92916174864",
                "Balance": "0.00",
                "Transaction ID": "65T38034Y8608674T",
            }
        ),
    ]

    entries = _extract(rows)
    assert len(entries) == 1
    entry = entries[0]
    assert "Subscription Payment" in entry.narration
    assert "EUR" in entry.narration
    fixme = next(p for p in entry.postings if p.account == "Expenses:FIXME")
    asset = next(p for p in entry.postings if p.account == "Assets:Current:PayPal")
    assert asset.units.number == D("-10.76")
    assert fixme.meta["paypal_currency"] == "USD"
    assert fixme.meta["paypal_original_currency"] == "EUR"
    assert fixme.meta["paypal_original_amount"] == "10.00"
