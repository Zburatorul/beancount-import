import collections
import datetime
import json
import os

import pytest

from . import amazon_invoice

testdata_dir = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__), '..', '..', 'testdata', 'source', 'amazon'))


@pytest.mark.parametrize('name', [
    '277-5312419-9119541',
    '781-8429198-6057878',
    '166-7926740-5141621',
    'D56-5204779-4181560',
    '111-0000001-0000001',
])
def test_parsing_en_US(name: str):
    source_path = os.path.join(testdata_dir, name + '.html')
    invoice = amazon_invoice.parse_invoice(source_path)
    json_path = os.path.join(testdata_dir, name + '.json')
    expected = json.load(
        open(json_path, 'r'), object_pairs_hook=collections.OrderedDict)
    expected_str = json.dumps(expected, indent=4)
    actual = amazon_invoice.to_json(invoice)
    actual_str = json.dumps(actual, indent=4)
    if expected_str != actual_str:
        print(actual_str)
    assert expected_str == actual_str


@pytest.mark.parametrize('name', [
    '256-0244967-2403944', # regular order
    '393-2608279-9292916', # Spar-Abo, payed with gift card 
    '898-5185906-0096901', # Spar-Abo
    '974-6135682-9358749', # several credit card transactions
    'D22-9220967-2566135', # digital order, audible subscription
    'D60-9825125-4795642', # digital order
    '399-5779972-5007935', # Direct Debit (Bankeinzug)
    '071-4816388-0694813', # gift card amazon
    '075-2225405-7594823', # gift card spotify
    '447-6209054-6766419', # charge up Amazon account
    '588-8509154-9761865', # preparing shipment 
    '142-4912939-2196263', # shipping soon
])
def test_parsing_de_DE(name: str):
    testdata_dir_locale = os.path.join(testdata_dir, 'de_DE')
    source_path = os.path.join(testdata_dir_locale, name + '.html')
    invoice = amazon_invoice.parse_invoice(source_path, locale=amazon_invoice.LOCALES['de_DE']())
    json_path = os.path.join(testdata_dir_locale, name + '.json')
    expected = json.load(
        open(json_path, 'r'), object_pairs_hook=collections.OrderedDict)
    expected_str = json.dumps(expected, indent=4)
    actual = amazon_invoice.to_json(invoice)
    actual_str = json.dumps(actual, indent=4)
    if expected_str != actual_str:
        print(actual_str)
    assert expected_str == actual_str


def _wfm_order(subtotal='20.00'):
    usd = lambda x: amazon_invoice.Amount(amazon_invoice.D(x), 'USD')
    shipment = amazon_invoice.Shipment(
        shipped_date=None, items=[], items_subtotal=usd(subtotal),
        pretax_adjustments=[], total_before_tax=usd(subtotal),
        posttax_adjustments=[], tax=[], total=usd(subtotal), errors=[])
    card = amazon_invoice.CreditCardTransaction(
        date=datetime.date(2026, 6, 1), card_description='Amazon Visa',
        card_ending_in='0000', amount=usd(subtotal))
    return amazon_invoice.Order(
        order_id='113-0000002-0000002', order_date=datetime.date(2026, 6, 1),
        shipments=[shipment], credit_card_transactions=[card],
        pretax_adjustments=[], tax=usd('0'), posttax_adjustments=[], errors=[])


def test_parse_wfm_receipt():
    receipt = amazon_invoice.parse_wfm_receipt(
        os.path.join(testdata_dir, 'wfm', '113-0000002-0000002.html'))
    assert receipt.order_id == '113-0000002-0000002'
    assert str(receipt.item_subtotal) == '20.00 USD'
    assert str(receipt.savings) == '-2.50 USD'
    assert str(receipt.tax) == '1.25 USD'
    assert str(receipt.grand_total) == '18.75 USD'
    assert [(t, str(a)) for t, a in receipt.payments] == [('0000', '18.75 USD')]


def test_apply_wfm_receipt():
    receipt = amazon_invoice.parse_wfm_receipt(
        os.path.join(testdata_dir, 'wfm', '113-0000002-0000002.html'))
    order = amazon_invoice.apply_wfm_receipt(_wfm_order(), receipt)
    assert order.errors == []
    shipment, = order.shipments
    assert [(a.description, str(a.amount)) for a in shipment.pretax_adjustments] == [
        ('Whole Foods promotions', '-2.50 USD')]
    assert [(a.description, str(a.amount)) for a in shipment.tax] == [
        ('Sales Tax', '1.25 USD')]
    card, = order.credit_card_transactions
    assert (card.card_description, str(card.amount)) == ('Amazon Visa', '18.75 USD')


def test_apply_wfm_receipt_reports_subtotal_mismatch():
    receipt = amazon_invoice.parse_wfm_receipt(
        os.path.join(testdata_dir, 'wfm', '113-0000002-0000002.html'))
    order = amazon_invoice.apply_wfm_receipt(_wfm_order('30.00'), receipt)
    assert any('item subtotal' in e for e in order.errors)
