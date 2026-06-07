"""CSV-based importer for PayPal exports."""

from __future__ import annotations

from typing import Dict, Iterable

from beancount.core import data
from beancount.core.amount import Amount
from beangulp.importers.csv import Importer as CSVImporter, Col
from beancount_import.matching import FIXME_ACCOUNT

PAYPAL_HEADER = [
    "Date",
    "Time",
    "TimeZone",
    "Name",
    "Type",
    "Status",
    "Currency",
    "Amount",
    "Fees",
    "Total",
    "Exchange Rate",
    "Receipt ID",
    "Balance",
    "Transaction ID",
    "Item Title",
]

HEADER_INDEX = {name: idx for idx, name in enumerate(PAYPAL_HEADER)}

PAYPAL_HEADER_REGEX = (
    r'"Date","Time","TimeZone","Name","Type","Status","Currency","Amount","Fees","Total","Exchange '
    r'Rate","Receipt ID","Balance","Transaction ID","Item Title"'
)


def _normalize_key(key: str) -> str:
    return key.lower().replace(" ", "_")


class PaypalCsvImporter(CSVImporter):
    """Wraps the standard CSV importer to emit a FIXME posting for PayPal rows."""

    def __init__(self, account: str, currency: str = "USD"):
        config = {
            Col.DATE: "Date",
            Col.TXN_TIME: "Time",
            Col.PAYEE: "Name",
            Col.NARRATION1: "Type",
            Col.AMOUNT: "Amount",
            Col.REFERENCE_ID: "Transaction ID",
        }
        super().__init__(
            config,
            account,
            currency,
            regexps=PAYPAL_HEADER_REGEX,
            encoding="utf-8-sig",
            narration_sep=" - ",
            categorizer=self._categorize,
        )
        self.account = account

    def _categorize(self, txn: data.Transaction, row: Iterable[str]) -> data.Transaction:
        asset_posting = txn.postings[0]
        row_map = self._row_map(row)
        txn_id = row_map.get("Transaction ID")
        source_desc = f"paypal_csv.{txn_id}" if txn_id else None
        metadata = {
            f"paypal_{_normalize_key(key)}": value
            for key, value in row_map.items()
            if value
        }
        if source_desc:
            metadata["source_desc"] = source_desc
        fixme_posting = data.Posting(
            account=FIXME_ACCOUNT,
            units=Amount(
                -asset_posting.units.number, asset_posting.units.currency),
            cost=None,
            price=None,
            flag=None,
            meta=metadata,
        )
        txn.postings.append(fixme_posting)

        existing_meta = dict(asset_posting.meta) if asset_posting.meta else {}
        metadata_for_asset = {
            key: value
            for key, value in metadata.items()
            if key.startswith("paypal_") and key != "paypal_balance"
        }
        existing_meta.update(metadata_for_asset)
        existing_meta.update({
            "paypal_transaction_id": row_map.get("Transaction ID"),
            "paypal_status": row_map.get("Status"),
            **({"source_desc": source_desc} if source_desc else {}),
        })
        txn.postings[0] = asset_posting._replace(meta=existing_meta)
        return txn

    def extract(self, file, existing_entries=None):
        entries = super().extract(file, existing_entries=existing_entries)
        conversion_map: dict[tuple[str | None, str | None, str | None], data.Transaction] = {}
        filtered: list[data.Transaction] = []
        for entry in entries:
            if self._is_conversion(entry):
                key = self._time_key(entry)
                if key and key not in conversion_map:
                    conversion_map[key] = entry
                continue
            filtered.append(entry)

        result: list[data.Transaction] = []
        for entry in filtered:
            if self._is_generic_type(entry):
                continue
            conversion = None
            if not self._is_us_currency(entry):
                key = self._time_key(entry)
                conversion = conversion_map.pop(key, None) if key else None
                if conversion:
                    entry = self._merge_entries(entry, conversion)
            if self._is_completed(entry) and self._is_us_currency(entry):
                result.append(entry)
        return result

    def _is_completed(self, entry: data.Transaction) -> bool:
        asset_posting = next(
            (p for p in entry.postings if p.account == self.account), None)
        status = asset_posting.meta.get("paypal_status") if asset_posting and asset_posting.meta else None
        return status is None or status.lower() == "completed"

    def _is_conversion(self, entry: data.Transaction) -> bool:
        asset = next((p for p in entry.postings if p.account == self.account), None)
        if not asset or asset.units is None:
            return False
        if asset.units.number >= 0:
            return False
        return (entry.narration or "").strip().lower() == "general currency conversion"

    def _merge_entries(self, merchant: data.Transaction,
                       conversion: data.Transaction) -> data.Transaction:
        asset = next((p for p in merchant.postings if p.account == self.account), None)
        fixme = next((p for p in merchant.postings if p.account == FIXME_ACCOUNT), None)
        conv_asset = next((p for p in conversion.postings if p.account == self.account), None)
        conv_fixme = next((p for p in conversion.postings if p.account == FIXME_ACCOUNT), None)
        if not asset or not fixme or not conv_asset:
            return merchant

        original_currency = fixme.meta.get("paypal_currency") if fixme.meta else None
        original_amount = fixme.units if fixme.units else None
        converted_units = conv_asset.units

        new_asset = asset._replace(units=converted_units)
        new_fixme_units = Amount(-converted_units.number, converted_units.currency)

        fixme_meta = dict(fixme.meta or {})
        if original_amount:
            fixme_meta["paypal_original_amount"] = str(original_amount.number)
        if original_currency:
            fixme_meta["paypal_original_currency"] = original_currency
        fixme_meta["paypal_currency"] = converted_units.currency
        if conv_fixme and conv_fixme.meta:
            for field in ("paypal_exchange_rate", "paypal_total", "paypal_fees"):
                if field in conv_fixme.meta:
                    fixme_meta[field] = conv_fixme.meta[field]

        new_fixme = fixme._replace(units=new_fixme_units, meta=fixme_meta)

        new_postings = []
        for posting in merchant.postings:
            if posting is asset:
                new_postings.append(new_asset)
            elif posting is fixme:
                new_postings.append(new_fixme)
            else:
                new_postings.append(posting)

        narration = merchant.narration or ""
        if original_currency and original_currency.lower() != "usd" and original_amount:
            original_amount_str = f"{original_amount} {original_currency}"
            narration = f"{narration} ({original_amount_str})".strip()

        return merchant._replace(postings=new_postings, narration=narration)

    def _attach_conversion_metadata(self, entry: data.Transaction,
                                    conversion: data.Transaction) -> data.Transaction:
        fixme = next((p for p in entry.postings if p.account == FIXME_ACCOUNT), None)
        conv_fixme = next(
            (p for p in conversion.postings if p.account == FIXME_ACCOUNT), None)
        if not fixme:
            return entry

        fixme_meta = dict(fixme.meta or {})
        fixme_meta["paypal_currency"] = "USD"
        if conv_fixme and conv_fixme.meta:
            for field in ("paypal_exchange_rate", "paypal_total", "paypal_fees"):
                if field in conv_fixme.meta:
                    fixme_meta[field] = conv_fixme.meta[field]
        if "paypal_status" in fixme_meta:
            fixme_meta["paypal_status"] = "Completed"
        new_fixme = fixme._replace(meta=fixme_meta)

        new_postings = []
        for posting in entry.postings:
            if posting is fixme:
                new_postings.append(new_fixme)
            else:
                new_postings.append(posting)
        return entry._replace(postings=new_postings)

    def _mark_completed(self, entry: data.Transaction) -> data.Transaction:
        asset = next((p for p in entry.postings if p.account == self.account), None)
        if not asset:
            return entry
        asset_meta = dict(asset.meta or {})
        asset_meta["paypal_status"] = "Completed"
        new_asset = asset._replace(meta=asset_meta)
        new_postings = []
        for posting in entry.postings:
            if posting is asset:
                new_postings.append(new_asset)
            else:
                new_postings.append(posting)
        return entry._replace(postings=new_postings)

    def _is_us_currency(self, entry: data.Transaction) -> bool:
        fixme = next((p for p in entry.postings if p.account == FIXME_ACCOUNT), None)
        if not fixme or not fixme.meta:
            return True
        currency = fixme.meta.get("paypal_currency")
        return currency is None or currency.lower() == "usd"

    def _time_key(self, entry: data.Transaction) -> tuple[str | None, str | None, str | None] | None:
        fixme_posting = next(
            (p for p in entry.postings if p.account == FIXME_ACCOUNT), None)
        if not fixme_posting or not fixme_posting.meta:
            return None
        return (
            fixme_posting.meta.get("paypal_date"),
            fixme_posting.meta.get("paypal_time"),
            fixme_posting.meta.get("paypal_timezone"),
        )

    def _generic_types(self) -> set[str]:
        return {
            "general authorization",
            "account hold for open authorization",
            "reversal of general account hold",
            "general currency conversion",
        }

    def _is_generic_type(self, entry: data.Transaction) -> bool:
        return (entry.narration or "").strip().lower() in self._generic_types()

    def _transaction_id(self, entry: data.Transaction) -> str | None:
        asset_posting = next(
            (p for p in entry.postings if p.account == self.account), None)
        if asset_posting and asset_posting.meta:
            return asset_posting.meta.get("paypal_transaction_id")
        return None

    def _row_map(self, row: Iterable[str]) -> Dict[str, str]:
        return {
            key: (row[idx].strip() if idx < len(row) else "")
            for key, idx in HEADER_INDEX.items()
        }


def make_paypal_csv_importer(account: str = "Assets:Current:PayPal",
                            currency: str = "USD") -> PaypalCsvImporter:
    """Creates an importer that feeds PayPal CSV rows into the importer source."""

    return PaypalCsvImporter(account, currency)
