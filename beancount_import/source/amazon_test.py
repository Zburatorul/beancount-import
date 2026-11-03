import os
from typing import Dict, Any

import pytest

from .amazon_invoice_test import testdata_dir
from .source_test import check_source_example
from ..source import SourceSpec

source_spec_without_posttax_accounts = {
    'module': 'beancount_import.source.amazon',
    'directory': testdata_dir,
    'amazon_account': 'name@domain.com',
}

source_spec_with_posttax_accounts = {
    **source_spec_without_posttax_accounts,
    'posttax_adjustment_accounts': {
        'Gift Card Amount': 'Assets:Gift-Cards:Amazon',
        'Rewards Points': 'Income:Amazon:Cashback',
    },
}  # type: Dict[str, Any]

source_spec_with_credit_card_account = {
    **source_spec_with_posttax_accounts,
    'credit_card_account': 'Liabilities:Accounts-Payable:Amazon',
}  # type: Dict[str, Any]

examples = [
    ('test_basic', source_spec_with_posttax_accounts),
    ('test_credit_card_transactions', source_spec_with_posttax_accounts),
    ('test_cleared_and_invalid', source_spec_with_posttax_accounts),
    ('test_prediction', source_spec_without_posttax_accounts),
    # Overrides the credit_card_last_4_digits account in the journal.
    ('test_credit_card_account', source_spec_with_credit_card_account),
]


@pytest.mark.parametrize('name,source_spec', examples)
def test_source(name: str, source_spec: SourceSpec):
    check_source_example(
        example_dir=os.path.join(testdata_dir, name),
        source_spec=source_spec,
        replacements=[(testdata_dir, '<testdata>')])


refunds_testdata_dir = os.path.realpath(
    os.path.join(testdata_dir, '..', 'amazon_refunds'))


def test_refunds():
    # Refunds from refunds.csv become pending gift card transactions, except
    # those whose refund_id is already in the journal.
    check_source_example(
        example_dir=os.path.join(refunds_testdata_dir, 'test_refunds'),
        source_spec={
            **source_spec_without_posttax_accounts,
            'directory': refunds_testdata_dir,
            'posttax_adjustment_accounts': {
                'gift_card': 'Assets:Gift-Cards:Amazon',
            },
        },
        replacements=[(refunds_testdata_dir, '<testdata>')])


@pytest.mark.parametrize('credit_card_account,expected', [
    ('Liabilities:Accounts-Payable:Amazon', 'Liabilities:Accounts-Payable:Amazon'),
    (None, 'Expenses:FIXME'),
])
def test_unlisted_card_payment_goes_to_credit_card_account(credit_card_account, expected):
    # Newer Amazon invoices only link to their card transactions, so the
    # invoice lists none and the amount paid by card is left as a remainder.
    from . import amazon, amazon_invoice
    order = amazon_invoice.parse_invoice(
        os.path.join(testdata_dir, '166-7926740-5141621.html'))
    assert order.credit_card_transactions
    order = order._replace(credit_card_transactions=[])
    txn = amazon.make_amazon_transaction(
        order, posttax_adjustment_accounts={}, credit_card_accounts={},
        amazon_account='name@domain.com', credit_card_account=credit_card_account)
    remainder = txn.postings[-1]
    assert remainder.account == expected
    assert -remainder.units.number == sum(
        p.units.number for p in txn.postings[:-1])


def test_failed_parse_is_pickled(tmpdir, monkeypatch):
    # An invoice that does not parse is not re-parsed while its pickled
    # failure is newer than the invoice, and reports the same warning.
    import types
    from . import SourceResults
    from . import amazon
    directory = str(tmpdir.mkdir('invoices'))
    pickle_dir = str(tmpdir.mkdir('pickles'))
    with open(os.path.join(directory, '111-1111111-1111111.html'), 'w') as f:
        f.write('<html><body>not an invoice</body></html>')

    real_parse = amazon.parse_invoice
    parses = []
    monkeypatch.setattr(amazon, 'parse_invoice',
                        lambda *a, **k: parses.append(a) or real_parse(*a, **k))
    journal = types.SimpleNamespace(all_entries=[], accounts={})

    def messages():
        source = amazon.AmazonSource(directory=directory,
                                     amazon_account='a@example.com',
                                     pickle_dir=pickle_dir,
                                     log_status=lambda m: None)
        results = SourceResults()
        source.prepare(journal, results)
        return [(m[0], m[1].split(':')[0]) for m in results.messages]

    first = messages()
    assert first and len(parses) == 1
    assert messages() == first
    assert len(parses) == 1
