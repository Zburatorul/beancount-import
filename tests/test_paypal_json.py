from decimal import Decimal as D

from beancount_import.source.paypal_json import PaypalJsonSource


def _source():
    return PaypalJsonSource(
        directory='.', assets_account='Assets:Current:PayPal',
        fee_account='Expenses:Finance:PayPal', prefix='paypal',
        log_status=lambda msg: None)


def _txn(txn_type, amount_text, is_credit, filter_set=()):
    return {
        'id': 'T1',
        'status': 'COMPLETED',
        'type': txn_type,
        'filterSet': list(filter_set),
        'date': {'rawDate': {'date': '2026-09-22T17:18:08.000Z'}},
        'counterpartyInfo': {'counterpartyName': 'Refund from PayPal'},
        'ftsSearchInfo': {'displayType': 'Transfer'},
        'amountInfo': {
            'contents': {'amountText': amount_text},
            'classNames': {
                'transactionAmountClassName':
                    'isCredit txn_amt_font' if is_credit else 'txn_amt_font'},
        },
    }


def _paypal_units(result):
    (entry,) = result.entries
    (posting,) = [p for p in entry.postings
                  if p.account == 'Assets:Current:PayPal']
    return posting.units.number


def test_unknown_type_with_credit_amount_is_credit():
    # MONEY_TRANSFER ("Refund from PayPal") is in neither credit list, but the
    # rendered amount is flagged isCredit.
    result = _source()._make_import_result(
        'T1', _txn('MONEY_TRANSFER', '+$23.11', True), 'x.json')
    assert _paypal_units(result) == D('23.11')


def test_unknown_type_without_credit_flag_is_debit():
    result = _source()._make_import_result(
        'T1', _txn('PAYMENT', '&minus;$23.11', False), 'x.json')
    assert _paypal_units(result) == D('-23.11')
