import json
import os
import types

from . import SourceResults
from . import paypal_json


def _txn(txn_id, cust_id):
    return {
        'id': txn_id,
        'status': 'COMPLETED',
        'type': 'PAYMENT_SENT',
        'filterSet': ['PAYMENT_SENT'],
        'date': {'rawDate': {'date': '2026-03-02T18:59:31.000Z'}},
        'counterpartyInfo': {'counterpartyName': 'Example Store'},
        'ftsSearchInfo': {'displayAmount': '&minus;$4.20'},
        'fptiInfo': {'clicks': {'rescenter': {'custId': cust_id}}},
        '_details': {
            'fundingSource': {'fundingSourceList': [{
                'currencyCode': 'USD', 'amount': '$4.20', 'last4': '1234',
                'institution': 'EXAMPLE BANK', 'nickname': 'Checking'}]},
            'amount': {'rawAmounts': {'gross': {'value': '-4.20',
                                                'currencyCode': 'USD'}}},
            'unused': 'x' * 1000,
        },
    }


def _write_export(directory, name, txns):
    with open(os.path.join(directory, name), 'w') as f:
        json.dump({'transactions': txns}, f)


def _pending(directory, cust_id, cache_filename=None):
    source = paypal_json.PaypalJsonSource(
        directory=directory, assets_account='Assets:PayPal',
        fee_account='Expenses:Fees', prefix='paypal', cust_id=cust_id,
        known_cust_ids=['A', 'B'], cache_filename=cache_filename,
        log_status=lambda m: None)
    results = SourceResults()
    source.prepare(types.SimpleNamespace(all_entries=[]), results)
    return sorted(repr(p) for p in results.pending)


def test_exports_parsed_once_and_cached(tmpdir, monkeypatch):
    directory = str(tmpdir.mkdir('paypal_json'))
    _write_export(directory, 'paypal_transactions_2026-03-01.json',
                  [_txn('T1', 'A'), _txn('T2', 'B')])
    _write_export(directory, 'paypal_transactions_2026-03-02.json',
                  [_txn('T1', 'A'), _txn('T3', 'A')])
    cache = os.path.join(str(tmpdir), 'cache.pickle')

    paypal_json._parsed_exports.clear()
    expected_a = _pending(directory, 'A')
    expected_b = _pending(directory, 'B')
    assert len(expected_a) == 2 and len(expected_b) == 1

    loads = []
    real_load = json.load
    monkeypatch.setattr(paypal_json.json, 'load',
                        lambda f: loads.append(f.name) or real_load(f))

    # Two accounts reading the same exports parse each file once.
    paypal_json._parsed_exports.clear()
    assert _pending(directory, 'A', cache) == expected_a
    assert _pending(directory, 'B', cache) == expected_b
    assert len(loads) == 2

    # A new process reads the disk cache instead of the exports.
    paypal_json._parsed_exports.clear()
    assert _pending(directory, 'A', cache) == expected_a
    assert len(loads) == 2

    # A changed export (detected by mtime and size) is parsed again.
    _write_export(directory, 'paypal_transactions_2026-03-02.json',
                  [_txn('T1', 'A'), _txn('T3', 'A'), _txn('T4', 'A')])
    paypal_json._parsed_exports.clear()
    assert len(_pending(directory, 'A', cache)) == 3
    assert len(loads) == 3
