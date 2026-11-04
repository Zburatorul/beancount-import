"""PayPal JSON export source for beancount-import.

Data format
===========

This source reads PayPal JSON exports in the new format (single file with all
transactions in a `transactions` array). This is different from the per-transaction
JSON files expected by the standard paypal source.

Example file structure:

    financial/
      data/
        paypal_json/
          paypal_transactions_2026-03-07.json

Each JSON file contains:
    {
      "exportDate": "2026-03-07T03:08:49.250Z",
      "transactions": [
        {
          "id": "70374659CE2727048",
          "status": "COMPLETED",
          "type": "PRE_APPROVED_PAYMENT",
          "date": {"rawDate": {"date": "2026-03-02T18:59:31.000Z"}},
          "counterpartyInfo": {"counterpartyName": "MERCHANT NAME"},
          "ftsSearchInfo": {"displayAmount": "$4.20", "email": "..."},
          "amounts": {"currencyCode": "USD"},
          "fptiInfo": {"clicks": {"rescenter": {"custId": "..."}}},
          "_details": {
            "fundingSource": {
              "fundingSourceList": [{
                "type": "BANK_ACCOUNT",
                "last4": "0580",
                "institution": "JPMORGAN CHASE",
                "nickname": "Chase Savings",
                "amount": "$12.36",
                "currencyCode": "USD"
              }]
            },
            "amount": {
              "currencyCode": "EUR",
              "rawAmounts": {"gross": {"value": "10", "currencyCode": "EUR"}}
            }
          }
        },
        ...
      ]
    }

When _details is present (detail fetch succeeded), the funding source includes
last4/nickname/institution for bank account identification, and the USD amount
charged when the merchant currency is non-USD.

Specifying the source to beancount_import
=========================================

    dict(
        module='beancount_import.source.paypal_json',
        directory='/path/to/paypal_json',
        assets_account='Assets:Current:PayPal',
        fee_account='Expenses:Finance:PayPal',
        prefix='paypal',
        cust_id='9TBVWL7DHNN3C',  # Filter to specific account
    )

Multiple sources can be configured for different accounts by using different
`cust_id` values.

All sources in a process share the parsed exports, so configuring several
accounts on one directory reads each file once.  To skip parsing in later
runs too, pass the same `cache_filename` to each source; it keeps only the
fields this source uses (a few KB per transaction instead of the full
`_details`) and is refreshed when exports are added, changed or removed.

Imported transaction format
===========================

USD payment:

    2026-03-02 * "HEBERGEMENT OVH INC" "Payment" ^paypal.70374659CE2727048
      Expenses:FIXME    4.20 USD
        paypal_counterparty: "HEBERGEMENT OVH INC"
        paypal_counterparty_email: "paypal-ca-admin@ovh.ca"
        paypal_type: "PRE_APPROVED_PAYMENT"
      Assets:Current:PayPal  -4.20 USD
        date: 2026-03-02
        paypal_transaction_id: "70374659CE2727048"
        paypal_funding_source_last4: "0580"
        paypal_funding_source_institution: "JPMORGAN CHASE"
        paypal_funding_source_nickname: "Chase Savings"

EUR payment (booked in USD, merchant amount in narration):

    2025-03-28 * "Comedy Box SRL" "Payment (6.99 EUR)" ^paypal.69E43320AL462133K
      Expenses:FIXME    7.85 USD
        paypal_counterparty: "Comedy Box SRL"
        paypal_type: "PRE_APPROVED_PAYMENT"
      Assets:Current:PayPal  -7.85 USD
        date: 2025-03-28
        paypal_transaction_id: "69E43320AL462133K"
        paypal_funding_source_last4: "0580"
"""

import collections
import datetime
import html
import json
import os
import pickle
import re
import threading
from decimal import Decimal
from typing import Any, Dict, List, Optional

from beancount.core.amount import Amount
from beancount.core.data import EMPTY_SET, Posting, Transaction
from beancount.core.number import D, ZERO

from ..journal_editor import JournalEditor
from ..matching import FIXME_ACCOUNT
from ..posting_date import POSTING_DATE_KEY
from . import AssociatedData, ImportResult, Source, SourceResults
from .link_based_source import LinkBasedSource


# Only these parts of a transaction's `_details` are used.  The full
# `_details` is most of an export's size (hundreds of KB per transaction).
_DETAILS_KEYS_USED = ('fundingSource', 'amount')

_CACHE_VERSION = 1

# Parsed exports shared by all PaypalJsonSource instances in this process
# (one per configured account), keyed by (path, mtime_ns, size).
_parsed_exports: Dict[tuple, List[Dict[str, Any]]] = {}
_parsed_exports_lock = threading.Lock()


def _slim_transaction(txn: Dict[str, Any]) -> Dict[str, Any]:
    details = txn.get('_details')
    if not isinstance(details, dict):
        return txn
    txn = dict(txn)
    txn['_details'] = {k: details[k] for k in _DETAILS_KEYS_USED if k in details}
    return txn


def _file_key(json_path: str) -> tuple:
    st = os.stat(json_path)
    return (os.path.realpath(json_path), st.st_mtime_ns, st.st_size)


def load_export_transactions(json_path: str,
                             disk_cache: Optional[Dict[tuple, Any]] = None
                             ) -> List[Dict[str, Any]]:
    """Returns the `transactions` of an export, without unused `_details`.

    Each file is parsed at most once per process.  `disk_cache`, if given,
    is consulted and updated as well.  Files are identified by path, mtime
    and size, so an export rewritten in place with the same size within the
    filesystem's timestamp resolution would not be re-read; exports are
    expected to be written once, under a new name.
    """
    key = _file_key(json_path)
    with _parsed_exports_lock:
        txns = _parsed_exports.get(key)
        if txns is None and disk_cache is not None:
            txns = disk_cache.get(key)
        if txns is None:
            with open(json_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            txns = [_slim_transaction(t) for t in data.get('transactions', [])]
        _parsed_exports[key] = txns
        if disk_cache is not None:
            disk_cache[key] = txns
        return txns


def _read_disk_cache(cache_filename: str) -> Dict[tuple, Any]:
    try:
        with open(cache_filename, 'rb') as f:
            data = pickle.load(f)
        if data.get('version') == _CACHE_VERSION:
            return data['files']
    except FileNotFoundError:
        pass
    except Exception as e:
        print('paypal_json: ignoring unreadable cache %s: %s' % (cache_filename, e))
    return {}


def _write_disk_cache(cache_filename: str, files: Dict[tuple, Any]) -> None:
    tmp = cache_filename + '.tmp'
    with open(tmp, 'wb') as f:
        pickle.dump({'version': _CACHE_VERSION, 'files': files}, f)
    os.replace(tmp, cache_filename)


def parse_amount(amount_text: str) -> tuple[Decimal | None, str | None]:
    """Parse HTML-encoded amount like '&minus;$4.20', '€10.00 EUR', '+$100'.

    Returns:
        Tuple of (absolute amount as Decimal, currency code) or (None, None).
    """
    if not amount_text:
        return None, None

    decoded = html.unescape(amount_text)
    # Normalize minus signs (Unicode minus, en-dash → ASCII minus)
    decoded = decoded.replace('\u2212', '-').replace('\u2013', '-')

    # Pattern: optional sign, currency symbol, digits, optional 3-letter currency code
    match = re.match(
        r'[+-]?\s*[$\u20ac\xa3\xa5]?([\d,]+\.?\d*)\s*([A-Z]{3})?',
        decoded.strip()
    )

    if match:
        value, currency = match.groups()
        if not value:
            return None, None
        return D(value.replace(',', '')), currency or 'USD'

    return None, None


def parse_iso_date(raw_date: str) -> datetime.date:
    """Convert ISO date string to date object.

    Input: "2026-03-02T18:59:31.000Z"
    """
    try:
        dt = datetime.datetime.fromisoformat(raw_date.replace('Z', '+00:00'))
        return dt.date()
    except (ValueError, AttributeError):
        return datetime.date.today()


class PaypalJsonSource(LinkBasedSource, Source):
    """Source for PayPal JSON export files (new list format).

    Export files live in one directory, named paypal_transactions_<date>[_<owner>].json.
    The owner is only a label: which source a transaction belongs to comes from the
    custId inside it (the cust_id option).
    """

    # custIds of every configured source, to flag exports that belong to none.
    known_cust_ids: set = set()

    def __init__(
        self,
        directory: str,
        assets_account: str,
        fee_account: str,
        prefix: str,
        cust_id: Optional[str] = None,
        earliest_date: Optional[datetime.date] = None,
        cache_filename: Optional[str] = None,
        **kwargs
    ) -> None:
        super().__init__(link_prefix=prefix + '.', **kwargs)
        self.directory = directory
        self.prefix = prefix
        self.assets_account = assets_account
        self.fee_account = fee_account
        self.cust_id = cust_id
        if cust_id:
            self.known_cust_ids.add(cust_id)
        self.earliest_date = earliest_date
        self.cache_filename = cache_filename
        self.transaction_meta_key = prefix + '_transaction_id'

        # Register metadata keys for ML predictor training
        self.example_transaction_key_extractors[prefix + '_counterparty'] = None
        self.example_posting_key_extractors[prefix + '_counterparty'] = None
        self.example_posting_key_extractors[prefix + '_counterparty_email'] = None
        self.example_posting_key_extractors[prefix + '_memo'] = None
        self.example_posting_key_extractors[prefix + '_type'] = None
        self.example_posting_key_extractors[prefix + '_funding_source_last4'] = None
        self.example_posting_key_extractors[prefix + '_funding_source_institution'] = None
        self.example_posting_key_extractors[prefix + '_funding_source_nickname'] = None

    def _get_cust_id(self, txn: Dict) -> Optional[str]:
        """Extract custId from transaction to identify account owner."""
        try:
            return txn['fptiInfo']['clicks']['rescenter']['custId']
        except (KeyError, TypeError):
            return None

    def _get_funding_source(self, txn: Dict) -> Optional[Dict]:
        """Extract primary funding source from _details if available."""
        try:
            sources = txn['_details']['fundingSource']['fundingSourceList']
            return sources[0] if sources else None
        except (KeyError, TypeError):
            return None

    def _get_merchant_amount(self, txn: Dict) -> tuple[Optional[Decimal], str]:
        """Extract merchant-currency amount from _details.amount if available."""
        try:
            raw = txn['_details']['amount']['rawAmounts']['gross']
            value = D(str(raw['value']))
            currency = raw.get('currencyCode', 'USD')
            return value, currency
        except (KeyError, TypeError, Exception):
            return None, 'USD'

    def _make_import_result(
        self,
        txn_id: str,
        txn: Dict[str, Any],
        json_path: str
    ) -> Optional[ImportResult]:
        """Convert a transaction dict to an ImportResult."""

        if txn.get('status') != 'COMPLETED':
            return None

        # Parse date
        raw_date = txn.get('date', {}).get('rawDate', {}).get('date', '')
        date = parse_iso_date(raw_date)

        if self.earliest_date and date < self.earliest_date:
            return None

        # Counterparty info
        counterparty_name = (txn.get('counterpartyInfo', {}).get('counterpartyName', '') or '').strip()
        fts = txn.get('ftsSearchInfo', {})
        counterparty_email = (fts.get('email') or '').strip()
        memo = (fts.get('memo') or '').strip()
        display_type = (fts.get('displayType', '') or '').strip()

        # Transaction type / direction
        txn_type = (txn.get('type', '') or '').strip()
        filter_set = txn.get('filterSet', [])

        is_credit = False  # money flowing into PayPal balance
        if 'PAYMENT_RECEIVED' in filter_set or txn_type == 'PAYMENT_RECEIVED':
            is_credit = True
        elif 'REFUND' in filter_set or txn_type == 'REFUND':
            is_credit = True
        elif 'TRANSFER' in filter_set:
            is_credit = (txn_type == 'TRANSFER_FROM_BANK')
        # PAYMENT_SENT / PRE_APPROVED_PAYMENT → is_credit stays False
        # The rendered amount carries the true direction; types the lists above
        # don't know (e.g. MONEY_TRANSFER "Refund from PayPal") would otherwise
        # default to a debit.
        amount_classes = txn.get('amountInfo', {}).get('classNames', {})
        if 'isCredit' in (amount_classes.get('transactionAmountClassName') or ''):
            is_credit = True

        # ── Amount resolution ──────────────────────────────────────────────
        # Priority:
        #   1. _details.fundingSource USD amount  (most accurate for booking)
        #   2. _details.amount.rawAmounts.gross   (merchant currency)
        #   3. ftsSearchInfo.displayAmount        (fallback)
        #   4. amountInfo.contents.amountText     (last resort)

        funding_source = self._get_funding_source(txn)
        merchant_amount, merchant_currency = self._get_merchant_amount(txn)

        # Booking amount defaults to merchant amount
        booking_amount = merchant_amount
        booking_currency: Optional[str] = merchant_currency

        # If we have a USD funding source and merchant is non-USD, book USD
        foreign_note = None
        if (funding_source
                and funding_source.get('currencyCode') == 'USD'
                and merchant_amount is not None
                and merchant_currency not in ['USD']):
            usd_amount, _ = parse_amount(funding_source.get('amount', ''))
            if usd_amount is not None:
                booking_amount = usd_amount
                booking_currency = 'USD'
                foreign_note = f'{merchant_amount:.2f} {merchant_currency}'

        # Fall back to display fields if _details not present
        if booking_amount is None:
            booking_amount, booking_currency = parse_amount(fts.get('displayAmount', ''))
        if booking_amount is None:
            booking_amount, booking_currency = parse_amount(
                txn.get('amountInfo', {}).get('contents', {}).get('amountText', '')
            )
        if booking_amount is None:
            self.log_status(f'paypal_json: skipping {txn_id} - could not parse amount')
            return None

        booking_currency = booking_currency or txn.get('amounts', {}).get('currencyCode', 'USD')

        if booking_currency != 'USD':
            self.log_status(f'paypal_json: skipping {txn_id} - non-USD amount ({booking_amount} {booking_currency}), no USD equivalent available')
            return None

        # ── Narration ──────────────────────────────────────────────────────
        narration = display_type or 'Payment'
        if memo:
            narration += ' - ' + memo
        if foreign_note:
            narration += f' ({foreign_note})'

        # ── Build transaction ──────────────────────────────────────────────
        txn_meta: Dict[str, Any] = collections.OrderedDict()
        transaction = Transaction(
            meta=txn_meta,
            date=date,
            flag='*',
            payee=counterparty_name,
            narration=narration,
            links=frozenset([self.link_prefix + txn_id]),
            tags=EMPTY_SET,
            postings=[],
        )

        units = Amount(booking_amount, booking_currency)

        # All metadata on the assets/PayPal posting so posting_meta rules can
        # match on counterparty, type, and funding-source fields.
        assets_meta = collections.OrderedDict([
            (self.transaction_meta_key, txn_id),
            (POSTING_DATE_KEY, date),
            (self.prefix + '_counterparty', counterparty_name),
        ])
        if counterparty_email:
            assets_meta[self.prefix + '_counterparty_email'] = counterparty_email
        if memo:
            assets_meta[self.prefix + '_memo'] = memo
        assets_meta[self.prefix + '_type'] = txn_type
        if funding_source:
            last4 = (funding_source.get('last4') or '').strip()
            institution = (funding_source.get('institution') or '').strip()
            nickname = (funding_source.get('nickname') or '').strip()
            if last4:
                assets_meta[self.prefix + '_funding_source_last4'] = last4
            if institution:
                assets_meta[self.prefix + '_funding_source_institution'] = institution
            if nickname:
                assets_meta[self.prefix + '_funding_source_nickname'] = nickname

        if is_credit:
            transaction.postings.append(Posting(
                account=self.assets_account,
                units=units, cost=None, price=None, flag=None,
                meta=assets_meta,
            ))
            transaction.postings.append(Posting(
                account=FIXME_ACCOUNT,
                units=-units, cost=None, price=None, flag=None,
                meta=None,
            ))
        else:
            transaction.postings.append(Posting(
                account=FIXME_ACCOUNT,
                units=units, cost=None, price=None, flag=None,
                meta=None,
            ))
            transaction.postings.append(Posting(
                account=self.assets_account,
                units=-units, cost=None, price=None, flag=None,
                meta=assets_meta,
            ))

        return ImportResult(
            date=date,
            info=dict(type='application/json', filename=json_path),
            entries=[transaction],
        )

    def prepare(self, journal: JournalEditor, results: SourceResults):
        """Process all paypal_transactions_*.json files in the directory."""

        json_files = sorted(
            f for f in os.listdir(self.directory)
            if f.endswith('.json') and f.startswith('paypal_transactions_')
        )

        # Collect all transactions, keeping the most-detailed version per ID.
        # Later files (newer exports) override earlier ones so richer _details win.
        txn_data: Dict[str, tuple] = {}  # txn_id -> (txn, json_path)
        warned_cust_ids: set = set()

        disk_cache = None
        if self.cache_filename is not None:
            disk_cache = _read_disk_cache(self.cache_filename)
            cached_keys = set(disk_cache)

        for json_file in json_files:
            json_path = os.path.join(self.directory, json_file)
            self.log_status(f'paypal_json: loading {json_file}')

            for txn in load_export_transactions(json_path, disk_cache):
                txn_id = txn.get('id')
                if not txn_id:
                    continue

                if self.cust_id and self._get_cust_id(txn) != self.cust_id:
                    other = self._get_cust_id(txn)
                    if other not in self.known_cust_ids and other not in warned_cust_ids:
                        warned_cust_ids.add(other)
                        self.log_status(
                            f'paypal_json: WARNING {json_file} has custId {other}, '
                            'which matches no configured PayPal source; ignoring')
                    continue

                # Prefer entries with _details (complete fetch) over those without
                existing = txn_data.get(txn_id)
                if existing is None:
                    txn_data[txn_id] = (txn, json_path)
                elif '_detailError' in existing[0] and '_detailError' not in txn:
                    txn_data[txn_id] = (txn, json_path)

        if disk_cache is not None:
            # Drop entries for exports that were removed or changed.
            live = {_file_key(os.path.join(self.directory, f)) for f in json_files}
            for key in set(disk_cache) - live:
                del disk_cache[key]
            if set(disk_cache) != cached_keys:
                assert self.cache_filename is not None
                _write_disk_cache(self.cache_filename, disk_cache)

        all_txn_ids = set(txn_data.keys())

        seen_in_journal = self.get_entries_with_link(
            all_entries=journal.all_entries,
            results=results,
            valid_links=all_txn_ids,
        )

        # Also catch transactions imported by legacy sources (old paypal CSV
        # importer) that used a bare link like ^8G6043334G7479638 (no prefix)
        # but still wrote paypal_transaction_id into posting metadata.
        seen_via_meta = set()
        for entry in journal.all_entries:
            if not isinstance(entry, Transaction):
                continue
            for posting in entry.postings:
                if posting.meta and self.transaction_meta_key in posting.meta:
                    seen_via_meta.add(posting.meta[self.transaction_meta_key])

        for txn_id in sorted(all_txn_ids):
            if txn_id in seen_in_journal or txn_id in seen_via_meta:
                continue

            txn, json_path = txn_data[txn_id]
            import_result = self._make_import_result(txn_id, txn, json_path)

            if import_result is not None:
                results.add_pending_entry(import_result)

        results.add_account(self.assets_account)

    def is_posting_cleared(self, posting: Posting) -> bool:
        if posting.meta is None:
            return False
        return self.transaction_meta_key in posting.meta

    @property
    def name(self) -> str:
        return 'paypal_json'

    def get_associated_data_for_link(
        self, entry_id: str
    ) -> Optional[List[AssociatedData]]:
        return None


def load(spec, log_status):
    return PaypalJsonSource(log_status=log_status, **spec)
