"""Parses an Amazon.com/.de regular or digital order details HTML file.

Hierarchy of functions for parsing Amazon invoices:

main(...)
    |
    + parse_invoice(...)
    |    |
    |    + parse_digital_order_invoice(...)
    |    |   |
    |    |   + parse_credit_card_transactions_from_payments_table(...)
    |    |   +-> returns Order(..., shipments, ...)
    |    |
    |    + parse_regular_order_invoice(...)
    |        |
    |        + parse_shipments(...)
    |        |   + parse_shipment_payments(...)
    |        |   |   +-> returns Shipment
    |        |   +-> returns List[Shipment]
    |        |
    |        + parse_gift_cards(...)
    |        |   + parse_shipment_payments(...)
    |        |   |   +-> returns Shipment
    |        |   +-> returns List[Shipment]
    |        |
    |        + parse_credit_card_transactions(...)
    |        + parse_credit_card_transactions_from_payments_table(...)
    |        +-> returns Order(..., shipments, ...)
    |
    +-> returns Order
"""
from typing import (
    NamedTuple,
    Optional,
    List,
    Union,
    Iterable,
    Dict,
    Sequence,
    cast,
    Type,
    Tuple,
    Callable )
from abc import ABC, abstractmethod
import collections
import re
import os
import functools
import datetime
import logging

import bs4
from bs4 import BeautifulSoup, Tag, ResultSet, PageElement

from beancount_import.api_proxies.beautifulsoup import require_find, require_find_parent
import dateutil.parser
import beancount.core.amount
from beancount.core.amount import Amount
from beancount.core.number import D, ZERO
from decimal import Decimal

from ..amount_parsing import parse_amount, parse_number

logger = logging.getLogger('amazon_invoice')


class Locale_Data(ABC):
    LOCALE: str
    tax_included_in_price: bool
    payee: str
    currency: str  # only used for assumed prices

    # common fields regular and digital orders
    items_ordered: str
    price: str
    items_subtotal: str
    total_before_tax: str
    pretax_adjustment_fields_pattern: str
    posttax_adjustment_fields_pattern: str

    # Payment Table & Credit Card Transactions
    grand_total: str
    credit_card_transactions: str
    credit_card_last_digits: str
    payment_type: List[str]
    payment_information: str

    # regular orders only
    shipment_shipped_pattern: str
    shipment_nonshipped_headers: List[str]
    shipment_quantity: str
    shipment_of: str
    shipment_sales_tax: str
    shipment_total: str
    shipment_seller_profile: str
    shipment_sold_by: str
    shipment_condition: str
    regular_total_order: str
    regular_estimated_tax: str
    regular_order_placed: str
    regular_order_id: str
    gift_card: Optional[str]
    gift_card_to: Optional[str]
    gift_card_amazon_account: Optional[str]

    # digital orders only
    digital_order: str
    digital_order_cancelled: str
    digital_by: str
    digital_sold_by: str
    digital_tax_collected: str
    digital_total_order: str
    digital_order_id: str
    digital_payment_information: str

    @staticmethod
    @abstractmethod
    def parse_amount(amount, assumed_currency=None) -> Amount:
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def parse_date(date_str) -> datetime.date:
        raise NotImplementedError


class Locale_en_US(Locale_Data):
    """Language and region specific settings for parsing amazon.com invoices
    """
    LOCALE='en_US'
    tax_included_in_price=False
    payee='Amazon.com'
    currency='USD'  # only used for assumed prices
    
    # common fields regular and digital orders
    items_ordered='Items Ordered'
    price='Price'
    items_subtotal=r'Item\(s\) Subtotal:'
    total_before_tax='Total Before Tax:'
    pretax_adjustment_fields_pattern=('(?:' + '|'.join([
        'Shipping & Handling',
        'Free Shipping',
        'Free delivery',
        'Pantry delivery',
        'Promotion(?:s| Applied)',
        'Lightning Deal',
        'Your Coupon Savings', 
        '[0-9]+% off savings',
        'Subscribe & Save',
        '[0-9]+ Audible Credit Applied',
        '.*[0-9]+% Off.*',
        'Courtesy Credit',
        'Extra Savings',
        'Exclusive Promotion Applied',
        '(?:.*) Discount',
        'Gift[ -]Wrap',
    ]) + ') *:')
    posttax_adjustment_fields_pattern=r'Gift Card Amount:|Rewards Points:|Tip [(]optional[)]:|Recycle Fee \$X'

    # Payment Table & Credit Card Transactions
    grand_total=r'\n\s*Grand Total:\s+(.*)\n'
    credit_card_transactions='Credit Card transactions'
    credit_card_last_digits=r'^([^:]+) ending in ([0-9]+):\s+([^:]+):$'
    payment_type=[
        # only first matching regex is used!
        r'\n\s*([^\s|][^|\n]*[^|\s])\s+\|\s+Last (?:4 )?digits:\s+([0-9]{4})\n',
        r'\n\s*(.+)\s+ending in\s+([0-9]{4})\n'
        ]
    payment_information='^Payment information$'

    # regular orders only
    shipment_shipped_pattern='^Shipped on ([^\\n]+)$'
    shipment_nonshipped_headers=[
        'Service completed',
        'Preparing for Shipment',
        'Not Yet Shipped',
        'Shipping now'
        # unknown shipment statuses will be ignored
        # transaction total will not match
        ]
    shipment_quantity=r'^\s*(?:(?P<quantity>[0-9]+)|(?P<weight1>[0-9.]+\s+(?:lb|kg))|(?:(?P<quantityIgnore>[0-9.]+) [(](?P<weight2>[^)]+)[)]))\s+of:'
    shipment_of='of:'
    shipment_sales_tax='Sales Tax:'
    shipment_total='Total for This Shipment:'
    shipment_seller_profile=' (seller profile)'
    shipment_sold_by=r'(?P<description>.*)\n\s*(?:Sold|Provided) by:? (?P<sold_by>[^\n]+)'
    shipment_condition=r'\n.*\n\s*Condition: (?P<condition>[^\n]+)'
    regular_total_order='Grand Total:'
    regular_estimated_tax = 'Estimated tax to be collected:'
    regular_order_placed=r'(?:Subscribe and Save )?Order Placed:\s+([^\s]+ \d+, \d{4})'
    regular_order_id=r'.*Order ([0-9\-]+)'

    # digital orders only
    digital_order='Digital Order: (.*)'
    digital_order_cancelled='Order Canceled'
    digital_by='By'
    digital_sold_by=r'Sold\s+By'
    digital_tax_collected='Tax Collected:'
    digital_total_order='Total for this Order:'
    digital_order_id='^Amazon.com\\s+order number:\\s+(D[0-9-]+)$'
    digital_payment_information='Payment Information'

    @staticmethod
    def parse_amount(amount, assumed_currency=None) -> Amount:
        return parse_amount(amount, assumed_currency=assumed_currency)

    @staticmethod
    def parse_date(date_str) -> datetime.date:
        return dateutil.parser.parse(date_str).date()


class Locale_de_DE(Locale_Data):
    """Language and region specific settings for parsing amazon.de invoices
    """
    LOCALE='de_DE'
    tax_included_in_price=True  # no separate tax transactions
    payee='Amazon.de'
    currency='EUR'  # only used for assumed prices

    # common fields regular and digital orders
    items_ordered='Bestellte Artikel|Erhalten|Versendet|Amazon-Konto erfolgreich aufgeladen' # Erhalten|Versendet for gift cards
    price='Preis|Betrag'
    items_subtotal='Zwischensumme:'
    total_before_tax='Summe ohne MwSt.:'
    # most of translations still missing ...
    pretax_adjustment_fields_pattern=('(?:' + '|'.join([
        'Verpackung & Versand',
        # 'Free Shipping', 'Free delivery', 'Pantry delivery',
        # 'Promotion(?:s| Applied)', 'Lightning Deal',
        # 'Your Coupon Savings', '[0-9]+% off savings',
        # 'Subscribe & Save', '[0-9]+ Audible Credit Applied',
        # '.*[0-9]+% Off.*', 'Courtesy Credit',
        # 'Extra Savings', '(?:.*) Discount', 'Gift[ -]Wrap',
    ]) + ') *:')
    # most adjustments in DE are posttax:
    posttax_adjustment_fields_pattern=r'Gutschein eingelöst:|Geschenkgutschein\(e\):'
    
    # Payment Table & Credit Card Transactions
    grand_total=r'\n\s*(?:Gesamtsumme|Endsumme):\s+(.*)\n' # regular: Gesamtsumme, digital: Endsumme
    credit_card_transactions='Kreditkarten-Transaktionen'
    credit_card_last_digits=r'^([^:]+) mit den Endziffern ([0-9]+):\s+([^:]+):$'
    payment_type=[
        # only first matching regex is used!
        r'\n\s*([^\s|][^|\n]*[^|\s])\s+\|\s+Die letzten (?:4 )?Ziffern:\s*([0-9]{3,4})', # 3 digits for Bankeinzug
        r'\n\s*(.+)\s+mit den Endziffern\s+([0-9]{4})\n'
        ]
    payment_information='^Zahlungsdaten$'

    # regular orders only
    shipment_shipped_pattern='^versandt am ([^\\n]+)$'
    shipment_nonshipped_headers=[
        'Versand wird vorbereitet',
        'Versand in Kürze',
        # additional cases missing?
        # unknown shipment statuses will be ignored
        # transaction total will not match
    ]
    shipment_quantity=r'^\s*(?:(?P<quantity>[0-9]+)|(?P<weight1>[0-9.]+\s+(?:lb|kg))|(?:(?P<quantityIgnore>[0-9.]+) [(](?P<weight2>[^)]+)[)]))\s+Exemplar\(e\)\svon:'
    shipment_of='Exemplar(e) von:'
    shipment_sales_tax='Anzurechnende MwSt.:' # not sure (only old invoices)
    shipment_total='Gesamtsumme:'
    shipment_seller_profile=' (Mitgliedsprofil)'
    shipment_sold_by=r'(?P<description>.*)\n\s*(?:Verkauf) durch:? (?P<sold_by>[^\n]+)'
    shipment_condition=r'\n.*\n\s*Zustand: (?P<condition>[^\n]+)'
    regular_total_order='Gesamtsumme:'
    regular_estimated_tax='Anzurechnende MwSt.:'
    regular_order_placed=r'(?:Getätigte Spar-Abo-Bestellung|Bestellung aufgegeben am):\s+(\d+\. [^\s]+ \d{4})'
    regular_order_id=r'.*Bestellung ([0-9\-]+)'
    gift_card='Geschenkgutscheine'
    gift_card_to=r'^(?P<type>Geschenkgutschein)[\w\s-]*:\s*(?P<sent_to>[\w@._-]*)$'
    gift_card_amazon_account=r'^[\w\s-]*(?P<type>Amazon-Konto)[\w\s-]*(?P<sent_to>aufgeladen)[\w\s-]*$'

    # digital orders only
    digital_order_cancelled='Order Canceled'
    digital_order='Digitale Bestellung: (.*)'
    digital_by='Von'
    digital_sold_by=r'Verkauft von'
    digital_tax_collected='MwSt:'
    digital_total_order='Endsumme:'
    digital_order_id='^Amazon.de\\s+Bestellnummer:\\s+(D[0-9-]+)$'
    digital_payment_information='Zahlungsinformation'

    @staticmethod
    def _format_number_str(value: str) -> str:
        # 12.345,67 EUR -> 12345.67 EUR
        thousands_sep = '.'
        decimal_sep = ','
        return value.replace(thousands_sep, '').replace(decimal_sep, '.')

    @staticmethod
    def parse_amount(amount: str, assumed_currency=None) -> Amount:
        if amount is None:
            return None
        else:
            return parse_amount(
                Locale_de_DE._format_number_str(amount),
                assumed_currency=assumed_currency)

    class _parserinfo(dateutil.parser.parserinfo):
        MONTHS=[
            ('Jan', 'Januar'), ('Feb', 'Februar'), ('Mär', 'März'),
            ('Apr', 'April'), ('Mai', 'Mai'), ('Jun', 'Juni'),
            ('Jul', 'Juli'), ('Aug', 'August'), ('Sep', 'September'),
            ('Okt', 'Oktober'), ('Nov', 'November'), ('Dez', 'Dezember')
            ]
    
    @staticmethod
    def parse_date(date_str) -> datetime.date:
        return dateutil.parser.parse(date_str, parserinfo=Locale_de_DE._parserinfo(dayfirst=True)).date()


LOCALES: Dict[str, Type[Locale_Data]] = {x.LOCALE: x for x in [Locale_en_US, Locale_de_DE]}

Errors = List[str]
Adjustment = NamedTuple('Adjustment', [
    ('description', str),
    ('amount', Amount),
])
Item = NamedTuple('Item', [
    ('quantity', Decimal),
    ('description', str),
    ('sold_by', Optional[str]),
    ('condition', Optional[str]),
    ('price', Amount),
])

DigitalItem = NamedTuple('DigitalItem', [
    ('description', str),
    ('url', Optional[str]),
    ('sold_by', Optional[str]),
    ('by', Optional[str]),
    ('price', Amount),
])

Shipment = NamedTuple('Shipment', [
    ('shipped_date', Optional[datetime.date]),
    ('items', List[Union[Item, DigitalItem]]),
    ('items_subtotal', Amount),
    ('pretax_adjustments', List[Adjustment]),
    ('total_before_tax', Amount),
    ('posttax_adjustments', List[Adjustment]),
    ('tax', List[Adjustment]),
    ('total', Amount),
    ('errors', Errors),
])
CreditCardTransaction = NamedTuple('CreditCardTransaction', [
    ('date', datetime.date),
    ('card_description', str),
    ('card_ending_in', str),
    ('amount', Amount),
])
Order = NamedTuple('Order', [
    ('order_id', str),
    ('order_date', datetime.date),
    ('shipments', List[Shipment]),
    ('credit_card_transactions', List[CreditCardTransaction]),
    ('pretax_adjustments', List[Adjustment]),
    ('tax', Amount),
    ('posttax_adjustments', List[Adjustment]),
    ('errors', Errors),
])

def to_json(obj):
    if hasattr(obj, '_asdict'):
        return to_json(obj._asdict())
    if isinstance(obj, list):
        return [to_json(x) for x in obj]
    if isinstance(obj, dict):
        return collections.OrderedDict((k, to_json(v)) for k, v in obj.items())
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, datetime.date):
        return obj.strftime('%Y-%m-%d')
    return obj


def add_amount(a: Optional[Amount], b: Optional[Amount]) -> Optional[Amount]:
    """Add two amounts, amounts with value `None` are ignored.
    """
    if a is None:
        return b
    if b is None:
        return a
    return beancount.core.amount.add(a, b)

def reduce_amounts_may_return_none(amounts: Iterable[Amount]) -> Optional[Amount]:
    return functools.reduce(add_amount, amounts, None)


def reduce_amounts(amounts: Iterable[Amount]) -> Amount:
    """Reduce iterable of amounts to sum by applying `add_amount`.
    """

    reduced_amounts = functools.reduce(add_amount, amounts, None)
    if reduced_amounts is None:
        raise ValueError("amount iterable is empty, must be checked before reducing.")
    else:
        return reduced_amounts




def get_field_in_table(table, pattern, allow_multiple=False,
                       return_label=False):
    def predicate(node):
        return node.name == 'td' and re.fullmatch(pattern, node.text.strip(),
                                                  re.I) is not None

    tds = table.find_all(predicate)
    results = [(td.text.strip().strip(':'),
                td.find_next_sibling('td').text.strip()) for td in tds]
    if not return_label:
        results = [r[1] for r in results]
    if not allow_multiple:
        if not results:
            return None
        return results[0]
    return results


def get_adjustments_in_table(
    table, pattern, assumed_currency=None, locale=Locale_en_US) -> List[Adjustment]:
    """ Parse price adjustments in shipping or payment tables. Returns list of adjustments.
    """
    adjustments = []
    for label, amount_str in get_field_in_table(
            table, pattern, allow_multiple=True, return_label=True):
        adjustments.append(
            Adjustment(amount=locale.parse_amount(amount_str, assumed_currency), 
                    description=label))
    return adjustments


def reduce_adjustments(adjustments: Sequence[Adjustment]) -> List[Adjustment]:
    """ Takes list of adjustments and reduces duplicates by summing up the amounts.
    """
    # create dict like {adjustment: [amount1, amount2, ...]}
    all_adjustments = collections.OrderedDict()  # type: Dict[str, List[Amount]]
    for adjustment in adjustments:
        all_adjustments.setdefault(adjustment.description,
                                   []).append(adjustment.amount)
    # sum over amounts and convert back to list of Adjustment
    return [
        Adjustment(k, reduce_amounts(v)) for k, v in all_adjustments.items()
    ]


def is_items_ordered_header(node, locale=Locale_en_US) -> bool:
    """
    Identify Header of Items Ordered table (within shipment table)
    """
    if node.name != 'tr':
        return False
    tds = node('td')
    if len(tds) < 2:
        return False
    m1 = re.match(locale.items_ordered, tds[0].text.strip())
    m2 = re.match(locale.price, tds[1].text.strip())
    return(m1 is not None and m2 is not None)


def parse_shipments(soup, locale=Locale_en_US) -> List[Shipment]:
    """
    Parses Shipment Table Part of HTML document (1st Table)
    """
    def is_shipment_header_table(node):
        if node.name != 'table':
            return False
        text = node.text.strip()
        m = re.match(locale.shipment_shipped_pattern, text)
        # return True for both shipped and nonshipped table headers
        return m is not None or text in locale.shipment_nonshipped_headers

    header_tables = soup.find_all(is_shipment_header_table)

    if header_tables is []:
        # no shipment tables
        # e.g. if only gift cards in order
        logger.debug('no shipment table found')
        return []

    shipments = []  # type: List[Shipment]
    errors = []  # type: Errors

    for header_table in header_tables:
        logger.debug('extracting shipped date...')
        text = header_table.text.strip()
        shipped_date = None
        if text not in locale.shipment_nonshipped_headers:
            # extract shipped date if order already shipped
            m = re.match(locale.shipment_shipped_pattern, text)
            assert m is not None
            shipped_date = locale.parse_date(m.group(1))

        logger.debug('parsing shipment items...')
        items = []  # type: List[Item]

        shipment_table = header_table.find_parent('table')
        items_ordered_header = shipment_table.find(
            lambda node: is_items_ordered_header(node, locale))
        item_rows = items_ordered_header.find_next_siblings('tr')

        for item_row in item_rows:
            tds = item_row('td')
            description_node = tds[0]
            price_node = tds[1]
            price = price_node.text.strip()

            if price is None:
                price = Amount(D(0), locale.currency)
            else:
                price = locale.parse_amount(price)

            # 1 of: 365 Everyday Value, Potato Yellow Bag Organic, 48 Ounce
            # 2 (1.04 lb) of: Broccoli Crowns Conventional, 1 Each
            # 2.07 lb of: Pork Sausage Link Italian Mild Step 1

            m = re.match(locale.shipment_quantity, description_node.text, re.UNICODE|re.DOTALL)
            
            quantity = None
            if m is not None:
                # Amazon will say you got, e.g. 2 broccoli crowns at $1.69/lb - but then this code multiplies the 2 by the price listed
                # on the invoice, which is the total price in this case (but the per-unit price in other cases) - so if there's a quantity
                # and a weight, ignore the quantity and treat it as 1
                # alternately, capture the weight and the per-unit price and multiply out

                # 'quantity' group: integer, no weight units, no decimals
                quantity = m.group("quantity")
                # set silently to 1 if other regex groups match
                if quantity is None:
                    quantity = 1
            else:
                # regex did not match at all -> log warning
                quantity = 1
                errors.append("Unable to extract quantity, using 1: %s" % description_node.text)

            quantity = D(quantity)

            text = description_node.text.split(locale.shipment_of, 1)[1]

            m = re.match(locale.shipment_sold_by + locale.shipment_condition,
                         text, re.UNICODE | re.DOTALL)
            if m is None:
                m = re.match(locale.shipment_sold_by, text, re.UNICODE | re.DOTALL)
            if m is None:
                errors.append("Could not extract item from row {}".format(text))
                raise Exception("Could not extract item from row", text)
            
            description = re.sub(r'\s+', ' ', m.group('description').strip())
            sold_by = re.sub(r'\s+', ' ', m.group('sold_by').strip())
            try:
                condition = re.sub(r'\s+', ' ', m.group('condition').strip())
            except IndexError:
                condition = None
            suffix = locale.shipment_seller_profile
            if sold_by.endswith(suffix):
                sold_by = sold_by[:-len(suffix)]
            items.append(
                Item(
                    quantity=quantity,
                    description=description,
                    sold_by=sold_by,
                    condition=condition,
                    price=price,
                ))
        
        shipments.append(parse_shipment_payments(
            shipment_table,
            items,
            errors,
            shipped_date=shipped_date,
            locale=locale
        ))

    return shipments

def parse_gift_cards(soup, locale=Locale_en_US) -> List[Shipment]:
    """
    Parses Gift Card Table Part of HTML document (1st Table)
    """
    def is_gift_card_header_table(node):
        if node.name != 'table':
            return False
        text = node.text.strip()
        m = re.match(locale.gift_card, text)
        if m is not None:
            # check if a matching subtable exists
            sub_table = node.find_all(is_gift_card_header_table)
            if sub_table == []:
                # only match if it is the innermost table
                return True
        return False

    header_tables = soup.find_all(is_gift_card_header_table)

    if header_tables is []:
        # if no gift cards in order
        logger.debug('no gift card table found')
        return []

    shipments = []  # type: List[Shipment]
    errors = []  # type: Errors

    for header_table in header_tables:
        logger.debug('parsing gift card items...')
        items = []  # type: List[Item]

        shipment_table = header_table.find_parent('table')
        items_ordered_header = shipment_table.find(
            lambda node: is_items_ordered_header(node, locale))
        item_rows = [items_ordered_header]

        for item_row in item_rows:
            tds = item_row('td')
            description_node = tds[0]
            price_node = tds[1]
            price = price_node.text.strip()
            price = price.split('\n')[1]

            if price is None:
                price = Amount(D(0), locale.currency)
            else:
                price = locale.parse_amount(price)

            m = re.search(locale.gift_card_to, description_node.text.strip(), re.MULTILINE|re.UNICODE)
            if m is None:
                # if no match is found
                # check if Amazon account has been charged up
                m = re.search(locale.gift_card_amazon_account, description_node.text.strip(), re.MULTILINE|re.UNICODE)
            if m is None:
                errors.append('Failed to extract item description')
                description=''
            else:
                description = m.group('type').strip() + ' ' + m.group('sent_to').strip()

            items.append(
                Item(
                    quantity=D(1),
                    description=description,
                    sold_by=None,
                    condition=None,
                    price=price,
                ))

        shipments.append(parse_shipment_payments(
            shipment_table,
            items,
            errors,
            shipped_date=None,
            locale=locale
        ))

    return shipments


def parse_shipment_payments(
        shipment_table,
        items, errors,
        shipped_date=None,
        locale=Locale_en_US) -> Shipment:
    """ Parse payment information of single shipments and gift card orders.
    """
    logger.debug('parsing shipment amounts...')
    # consistency check: shipment subtotal against sum of item prices
    items_subtotal = locale.parse_amount(
        get_field_in_table(shipment_table, locale.items_subtotal))

    expected_items_subtotal = reduce_amounts_may_return_none(
        beancount.core.amount.mul(x.price, D(x.quantity)) for x in items)
    if (items_subtotal is not None and
        expected_items_subtotal != items_subtotal):
        errors.append(
            'expected items subtotal is %r, but parsed value is %r' %
            (expected_items_subtotal, items_subtotal))

    # parse pre- and posttax adjustments for shipment
    output_fields = dict()
    output_fields['pretax_adjustments'] = get_adjustments_in_table(
        shipment_table, locale.pretax_adjustment_fields_pattern, locale=locale)
    output_fields['posttax_adjustments'] = get_adjustments_in_table(
        shipment_table, locale.posttax_adjustment_fields_pattern, locale=locale)
    # compare total before tax
    pretax_parts = [items_subtotal or expected_items_subtotal] + [
        a.amount for a in output_fields['pretax_adjustments']
    ]
    expected_total_before_tax = reduce_amounts(pretax_parts)
    total_before_tax = locale.parse_amount(
        get_field_in_table(shipment_table, locale.total_before_tax))
    if total_before_tax is None:
        total_before_tax = expected_total_before_tax
    elif expected_total_before_tax != total_before_tax:
        errors.append(
            'expected total before tax is %s, but parsed value is %s' %
            (expected_total_before_tax, total_before_tax))

    sales_tax = get_adjustments_in_table(shipment_table, locale.shipment_sales_tax, locale=locale)

    if locale.tax_included_in_price:
        # tax is already inlcuded in item prices
        # do not add additional Adjustment for taxes
        sales_tax = []
    
    # compare total
    posttax_parts = (
        [total_before_tax] + [a.amount for a in sales_tax] +
        [a.amount for a in output_fields['posttax_adjustments']])
    expected_total = reduce_amounts(posttax_parts)
    total = locale.parse_amount(
        get_field_in_table(shipment_table, locale.shipment_total))
    if total is None:
        total = expected_total
    elif expected_total != total:
        errors.append('expected total is %s, but parsed value is %s' %
                        (expected_total, total))

    logger.debug('...finshed parsing shipment')
    return Shipment(
            shipped_date=shipped_date,
            items=items,
            items_subtotal=items_subtotal,
            total_before_tax=total_before_tax,
            tax=sales_tax,
            total=total,
            errors=errors,
            **output_fields)


def parse_credit_card_transactions_from_payments_table(
        payment_table,
        order_date: datetime.date,
        locale=Locale_en_US) -> List[CreditCardTransaction]:
    """ Parse payment information from payments table.
    Only type and last digits are given, no amount (assuming grand total).
    Other payment methods than credit card are possible:
    - Direct Debit (DE: Bankeinzug)
    """
    payment_text = '\n'.join(payment_table.strings)
    m = re.search(locale.grand_total, payment_text)
    assert m is not None
    grand_total = locale.parse_amount(m.group(1).strip())

    for regex in locale.payment_type:
        m = re.search(regex, payment_text)
        if m is not None:
            # only take first matching regex, discard others!
            break

    if m is None:
        return []

    credit_card_transactions = [
        CreditCardTransaction(
            date=order_date,
            amount=grand_total,
            card_description=m.group(1).strip(),
            card_ending_in=m.group(2).strip(),
        )
    ]
    return credit_card_transactions


def parse_credit_card_transactions(soup, locale=Locale_en_US) -> List[CreditCardTransaction]:
    """ Parse Credit Card Transactions from bottom sub-table of payments table.
    Transactions are listed with type, 4 digits, transaction date and amount.
    """
    def is_header_node(node):
        return node.name == 'td' and node.text.strip(
        ) == locale.credit_card_transactions

    header_node = soup.find(is_header_node)
    if header_node is None:
        return []
    sibling = header_node.find_next_sibling('td')
    rows = sibling.find_all('tr')
    transactions = []  # type: List[CreditCardTransaction]
    for row in rows:
        if not row.text.strip():
            continue
        tds = row('td')
        description = tds[0].text.strip()
        amount_text = tds[1].text.strip()
        m = re.match(locale.credit_card_last_digits, description,
                    re.UNICODE)
        assert m is not None
        transactions.append(
            CreditCardTransaction(
                date=locale.parse_date(m.group(3)),
                card_description=m.group(1),
                card_ending_in=m.group(2),
                amount=locale.parse_amount(amount_text),
            ))
    return transactions


def parse_invoice(path: str, locale=Locale_en_US) -> Optional[Order]:
    """ 1st method to call, distinguish between regular and digital invoice.
    """
    if os.path.basename(path).startswith('D'):
        logger.debug('identified as digital invoice')
        return parse_digital_order_invoice(path, locale=locale)
    logger.debug('identified as regular invoice')
    order = parse_regular_order_invoice(path, locale=locale)
    receipt_path = get_wfm_receipt_path(path)
    if order is not None and os.path.exists(receipt_path):
        logger.debug('applying Whole Foods receipt %s', receipt_path)
        order = apply_wfm_receipt(
            order, parse_wfm_receipt(receipt_path, locale=locale))
    return order


def parse_regular_order_invoice(path: str, locale=Locale_en_US) -> Order:
    """ Parse regular order type invoice (HTML document)
    1. parse all shipment tables with individual items
    2. parse payment table
    3. sanity check totals extracted from item prices and payment table
    """
    errors = []  # type: Errors
    with open(path, 'rb') as f:
        soup: BeautifulSoup = bs4.BeautifulSoup(f.read(), 'lxml')
    
    # -----------------
    # Check for new style invoice
    # -----------------
    if soup.find(attrs={"data-component": "orderSummary"}):
        logger.debug('identified as new style invoice')
        return parse_new_style_invoice(soup, locale)

    # -----------------
    # Order ID & Order placed date
    # -----------------
    logger.debug('parsing order id and order placed date...')
    title_element = soup.find('title')
    if title_element is None:
        raise ValueError("soup.find('title') returned None")
    else:
        title = title_element.text.strip()
    m = re.fullmatch(locale.regular_order_id, title.strip())
    assert m is not None
    order_id=m.group(1)

    def is_order_placed_node(node):
        m = re.fullmatch(locale.regular_order_placed, node.text.strip())
        return m is not None

    node = require_find(soup,is_order_placed_node)
    m = re.fullmatch(locale.regular_order_placed, node.text.strip())
    assert m is not None
    order_date = locale.parse_date(m.group(1))

    # ----------------------
    # Shipments & Gift Cards
    # ----------------------
    logger.debug('parsing shipments...')
    shipments = parse_shipments(soup, locale=locale)
    if hasattr(locale, 'gift_card'):
        shipments += parse_gift_cards(soup, locale=locale)
    if len(shipments) == 0:
        # no shipment or gift card tables found
        msg = ('Identified regular order invoice but no items were found '
               + '(neither shipments nor gift cards). This may be a new type. '
               + 'Consider opening an issue at jbms/beancount-import on github.')
        logger.warning(msg)
        errors.append(msg)
        # do not throw exception, continue parsing the payment table
    logger.debug('finished parsing shipments')

    # -------------------------------------------
    # Payment Table: Pre- and Posttax Adjustments
    # -------------------------------------------
    # Aim: Parse all pre- and posttax adjustments
    #      consistency check grand total against sum of item costs
    logger.debug('parsing payment table...')

    matcher : Callable[[Tag], bool] = lambda node: node.name == 'table' and re.match(
        locale.payment_information, node.text.strip()) is not None
    payment_table_header = require_find(soup, matcher)

    payment_table = payment_table_header.find_parent('table')

    logger.debug('parsing pretax adjustments...')
    output_fields = dict()  # type: Dict[str, List[Adjustment]]
    output_fields['pretax_adjustments'] = get_adjustments_in_table(
        payment_table, locale.pretax_adjustment_fields_pattern, locale=locale)
    
    # older invoices put pre-tax amounts on a per-shipment basis
    # new invoices only put pre-tax amounts on the overall payments section
    # detect which this is
    
    # payment table pretax adjustments
    pretax_amount = reduce_amounts_may_return_none(
        a.amount for a in output_fields['pretax_adjustments'])
    
    shipments_pretax_amount = None
    if any(s.pretax_adjustments for s in shipments):
        # sum over all shipment pretax amounts
        shipments_pretax_amount = reduce_amounts(a.amount
            for shipment in shipments
            for a in shipment.pretax_adjustments)            

        if shipments_pretax_amount != pretax_amount:
            errors.append(
                'expected total pretax adjustment to be %s, but parsed total is %s'
                % (shipments_pretax_amount, pretax_amount))

    logger.debug('parsing posttax adjustments...')
    # parse first to get an idea of the working currency
    grand_total = locale.parse_amount(
        get_field_in_table(payment_table, locale.regular_total_order))

    payment_adjustments = collections.OrderedDict()  # type: Dict[str, Amount]
    payments_total_adjustments = []  # type: List[Amount]
    shipments_total_adjustments = []  # type: List[Amount]

    def resolve_posttax_adjustments() -> List[Adjustment]:
        """ Extract and compare posttax adjustments
        from shipment and payment tables.
        Returns list of reduced Adjustments.
        """
        # get reduced form of adjustments from payment table
        payment_adjustments.update(
            reduce_adjustments(
                get_adjustments_in_table(payment_table,
                                        locale.posttax_adjustment_fields_pattern,
                                        assumed_currency=grand_total.currency,
                                        locale=locale)))
        # adjustments from all shipments, reduced
        all_shipments_adjustments = collections.OrderedDict(
            reduce_adjustments(
                sum((x.posttax_adjustments for x in shipments), [])))
        
        # initialize dict with all adjustment keys, values not used
        # dict ensures that keys are unique
        all_keys = collections.OrderedDict(payment_adjustments.items())
        all_keys.update(all_shipments_adjustments.items())
        
        # combine shipment and payment adjustments
        # make sure that shipment adjustments match payment adjustments
        all_adjustments = collections.OrderedDict()  # type: Dict[str, Amount]
        for key in all_keys:
            payment_amount = payment_adjustments.get(key)
            shipments_amount = all_shipments_adjustments.get(key)
            amount = payment_amount
            if payment_amount is None and shipments_amount is not None:
                # Amazon sometimes doesn't include adjustments in the Payments table
                amount = shipments_amount
                payments_total_adjustments.append(amount)
            elif payment_amount is not None and shipments_amount is None:
                # Amazon sometimes doesn't include these adjustments in the Shipment table
                shipments_total_adjustments.append(amount)
            elif payment_amount != shipments_amount:
                # Both tables include adjustment with same label, but amount does not match
                errors.append(
                    'expected total %r to be %s, but parsed total is %s' %
                    (key, shipments_amount, payment_amount))
            all_adjustments[key] = amount
        return [Adjustment(k, v) for k, v in all_adjustments.items()]

    output_fields['posttax_adjustments'] = resolve_posttax_adjustments()

    logger.debug('consistency check taxes...')
    # tax from payment table
    tax = locale.parse_amount(
        get_field_in_table(payment_table, locale.regular_estimated_tax))

    # tax from shipment tables
    shipment_amounts = [a.amount for shipment in shipments for a in shipment.tax]
    if len(shipment_amounts) == 0:
        # tax not given on shipment level
        if not locale.tax_included_in_price:
            # add tax to adjustments if not already included in item prices
            shipments_total_adjustments.append(tax)
    else:
        expected_tax = reduce_amounts(shipment_amounts)
        if expected_tax != tax:
            errors.append(
                'expected tax is %s, but parsed value is %s' % (expected_tax, tax))

    if locale.tax_included_in_price:
        # tax is already inlcuded in item prices
        # do not add additional transaction for taxes
        tax = None

    logger.debug('consistency check grand total...')
    payments_total_adjustment = reduce_amounts_may_return_none(payments_total_adjustments)
    shipments_total_adjustment = reduce_amounts_may_return_none(shipments_total_adjustments)

    expected_total = add_amount(shipments_total_adjustment,
                                reduce_amounts(x.total for x in shipments))

    # if no shipments pre-tax section, then the expected total isn't accounting
    # for the pre-tax adjustments yet since they are only in the grand total section
    if shipments_pretax_amount is None:
        expected_total = add_amount(expected_total, pretax_amount)

    adjusted_grand_total = add_amount(payments_total_adjustment, grand_total)
    if expected_total != adjusted_grand_total:
        errors.append('expected grand total is %s, but parsed value is %s' %
                    (expected_total, adjusted_grand_total))

    # ---------------------------------------
    # Payment Table: Credit Card Transactions
    # ---------------------------------------
    logger.debug('parsing credit card transactions...')
    credit_card_transactions = parse_credit_card_transactions(soup, locale=locale)
    if not credit_card_transactions:
        # no explicit credit card transaction table
        logger.debug('no credit card transactions table given, falling back to payments table')
        credit_card_transactions = parse_credit_card_transactions_from_payments_table(
            payment_table, order_date, locale=locale)

    if credit_card_transactions:
        total_payments = reduce_amounts(
            x.amount for x in credit_card_transactions)
    else:
        logger.debug('no payment transactions found, assumig grand total as total payment amount')
        total_payments = grand_total
    if total_payments != adjusted_grand_total:
        errors.append('total payment amount is %s, but grand total is %s' %
                      (total_payments, adjusted_grand_total))

    logger.debug('...finished parsing regular invoice.')
    order = Order(
        order_date=order_date,
        order_id=order_id,
        shipments=shipments,
        credit_card_transactions=credit_card_transactions,
        tax=tax,
        errors=sum((shipment.errors
                    for shipment in shipments), cast(Errors, [])) + errors,
        **output_fields)
    return order


# ---------------------------------------------------------------------------
# Whole Foods Market receipts
#
# The print-invoice page that finance-dl saves for a Whole Foods Market order
# lists only item list prices: its tax is $0.00 and it omits promotions, so its
# grand total does not match what the card was charged.  The order-details page
# (https://www.amazon.com/fopo/order-details?orderID=...) has a "Purchase
# Summary" with the real savings, tax and amount charged.  finance-dl stores
# that page as <directory>/wfm/<order-id>.html next to the invoice.
# ---------------------------------------------------------------------------

WFM_RECEIPT_DIRNAME = 'wfm'
WFM_PROMOTIONS_DESCRIPTION = 'Whole Foods promotions'

WfmReceipt = NamedTuple('WfmReceipt', [
    ('order_id', str),
    ('item_subtotal', Amount),
    ('savings', Amount),  # zero or negative
    ('tax', Amount),
    ('grand_total', Amount),
    ('payments', List[Tuple[str, Amount]]),  # (card last 4 digits, amount)
])


def get_wfm_receipt_path(invoice_path: str) -> str:
    directory, filename = os.path.split(invoice_path)
    return os.path.join(directory, WFM_RECEIPT_DIRNAME, filename)


def parse_wfm_receipt(path: str, locale=Locale_en_US) -> WfmReceipt:
    """Parse the "Purchase Summary" of a Whole Foods Market order-details page."""
    with open(path, 'rb') as f:
        soup: BeautifulSoup = bs4.BeautifulSoup(f.read(), 'lxml')

    def find_amount(element_id: str, required: bool) -> Amount:
        node = soup.find(id=element_id)
        if node is None:
            if required:
                raise ValueError('%s: no element %r' % (path, element_id))
            return Amount(ZERO, 'USD')
        return locale.parse_amount(node.get_text(strip=True))

    summary = require_find(soup, id='wfm-subtotal-row').find_parent('div', class_='a-row').find_previous_sibling('div')
    m = re.search(r'Order #:\s*(\d{3}-\d{7}-\d{7})', summary.get_text(' ', strip=True))
    if m is None:
        raise ValueError('%s: could not find order id in Purchase Summary' % path)

    savings = find_amount('wfm-total-savings-amount', required=False)
    payments = []
    i = 0
    while soup.find(id='wfm-%d-card-tail' % i) is not None:
        tail = soup.find(id='wfm-%d-card-tail' % i).get_text(strip=True)
        payments.append((tail.lstrip('*'),
                         find_amount('wfm-%d-amount-charged' % i, True)))
        i += 1
    return WfmReceipt(
        order_id=m.group(1),
        item_subtotal=find_amount('wfm-subtotal-amount', True),
        savings=Amount(-abs(savings.number), savings.currency),
        tax=find_amount('wfm-tax-total-amount', False),
        grand_total=find_amount('wfm-grand-total-amount', True),
        payments=payments,
    )


def apply_wfm_receipt(order: Order, receipt: WfmReceipt) -> Order:
    """Replace the print invoice's totals with the receipt's actual ones."""
    errors = list(order.errors)
    if receipt.order_id != order.order_id:
        errors.append('Whole Foods receipt is for order %s' % receipt.order_id)
        return order._replace(errors=errors)
    if len(order.shipments) != 1:
        errors.append('Whole Foods receipt ignored: expected 1 shipment, got %d'
                      % len(order.shipments))
        return order._replace(errors=errors)

    shipment = order.shipments[0]
    if shipment.items_subtotal != receipt.item_subtotal:
        errors.append('Whole Foods receipt item subtotal is %s, but invoice has %s'
                      % (receipt.item_subtotal, shipment.items_subtotal))
    total_before_tax = add_amount(receipt.item_subtotal, receipt.savings)
    expected_total = add_amount(total_before_tax, receipt.tax)
    if expected_total != receipt.grand_total:
        errors.append('Whole Foods receipt grand total is %s, but parts sum to %s'
                      % (receipt.grand_total, expected_total))

    shipment = shipment._replace(
        pretax_adjustments=([Adjustment(WFM_PROMOTIONS_DESCRIPTION, receipt.savings)]
                            if receipt.savings.number != ZERO else []),
        total_before_tax=total_before_tax,
        tax=([Adjustment('Sales Tax', receipt.tax)]
             if receipt.tax.number != ZERO else []),
        total=receipt.grand_total,
    )

    card_descriptions = {
        c.card_ending_in: c.card_description
        for c in order.credit_card_transactions}
    payments = [
        CreditCardTransaction(
            date=order.order_date,
            card_description=card_descriptions.get(tail, 'Whole Foods receipt'),
            card_ending_in=tail,
            amount=amount)
        for tail, amount in receipt.payments]
    if not payments:
        payments = order.credit_card_transactions
    elif reduce_amounts(p.amount for p in payments) != receipt.grand_total:
        errors.append('Whole Foods receipt payments do not sum to grand total %s'
                      % receipt.grand_total)

    return order._replace(
        shipments=[shipment],
        credit_card_transactions=payments,
        tax=receipt.tax,
        errors=errors,
    )


def get_text_lines(parent_node):
    """ Format nodes into list of strings
    """
    text_lines = ['']
    for node in parent_node.children:
        if isinstance(node, bs4.NavigableString):
            text_lines[-1] += str(node)
        elif node.name == 'br':
            text_lines.append('')
        else:
            text_lines[-1] += node.text
    return text_lines


def parse_digital_order_invoice(path: str, locale=Locale_en_US) -> Optional[Order]:
    """ Parse digital order type invoice (HTML document)
    1. parse all digital items tables
    2. parse amounts
    3. parse payment table
    """
    errors = []  # type: Errors
    with open(path, 'rb') as f:
        soup = bs4.BeautifulSoup(f.read(), 'lxml')

    logger.debug('check if order has been cancelled...')
    def is_cancelled_order(node):
        return node.text.strip() == locale.digital_order_cancelled

    if soup.find(is_cancelled_order):
        return None

    # --------------------------------------------------
    # Find Digital Order Header, parse date and order ID
    # --------------------------------------------------
    logger.debug('parsing header...')
    def is_digital_order_row(node):
        if node.name != 'tr':
            return False
        m = re.match(locale.digital_order, node.text.strip())
        if m is None:
            return False
        try:
            locale.parse_date(m.group(1))
            return True
        except:
            return False

    digital_order_header = require_find(soup, is_digital_order_row)
    digital_order_table : Tag = require_find_parent(digital_order_header, 'table')
    m = re.match(locale.digital_order, digital_order_header.text.strip())
    if m is None:
        msg = ('Identified digital order invoice but no digital orders were found.')
        logger.warning(msg)
        errors.append(msg)
        # throw exception since there is no other possibility to get order_date
        assert m is not None
    order_date = locale.parse_date(m.group(1))

    matcher: Callable[[Tag], bool] = lambda node: node.name == 'td' and re.match(locale.digital_order_id, node.text.strip()) is not None

    order_id_td = require_find(soup, matcher)
    m = re.match(locale.digital_order_id, order_id_td.text.strip())
    assert m is not None
    order_id = m.group(1)

    # -----------
    # Parse Items
    # -----------
    logger.debug('parsing items...')
    items_ordered_header = require_find(digital_order_table,
        lambda node: is_items_ordered_header(node, locale))
    item_rows_raw : ResultSet[PageElement] = items_ordered_header.find_next_siblings('tr')
    # the find_all below needs them to be the narrower type Tag, so cast right away
    item_rows : ResultSet[Tag] = cast(ResultSet[Tag], item_rows_raw)

    items = []  # Sequence[DigitalItem]
    other_fields_td = None

    for item_row in item_rows:
        tds = item_row.find_all('td')
        if len(tds) != 2:
            # payment information on order level (not payment table)
            # differently formatted, take first column only
            other_fields_td = tds[0]
            continue
        description_node : Tag = cast(Tag, tds[0])
        price_node = tds[1]
        price = price_node.text.strip()

        a = cast(Tag,description_node.find('a'))
        if a is not None:
            description = a.text.strip()
            url = cast(str,a['href'])
        else:
            bold_node = description_node.find('b')
            description = bold_node.text.strip()
            url = None

        text_lines = get_text_lines(description_node)

        def get_label_value(label):
            for line in text_lines:
                m = re.match(r'^\s*' + label + ': (.*)$', line,
                            re.UNICODE | re.DOTALL)
                if m is None:
                    continue
                return m.group(1)

        by = get_label_value(locale.digital_by)
        sold_by = get_label_value(locale.digital_sold_by)

        items.append(
            DigitalItem(
                description=description,
                by=by,
                sold_by=sold_by,
                url=url,
                price=locale.parse_amount(price),
            ))

    other_fields_text_lines = get_text_lines(other_fields_td)

    # -------------------------------------------
    # Parse Amounts, Pre- and Posttax Adjustments
    # -------------------------------------------
    logger.debug('parsing amounts...')
    def get_other_field(pattern, allow_multiple=False, return_label=False):
        """ Look for pattern in other_fields_text_lines
        """
        results = []
        for line in other_fields_text_lines:
            r = r'^\s*(' + pattern + r')\s+(.*[^\s])\s*$'
            m = re.match(r, line, re.UNICODE)
            if m is not None:
                results.append((m.group(1).strip(':'), m.group(2)))
        if not return_label:
            results = [r[1] for r in results]
        if not allow_multiple:
            if not results:
                return None
            return results[0]
        return results

    def get_adjustments(pattern):
        adjustments = []
        for label, amount_str in get_other_field(
                pattern, allow_multiple=True, return_label=True):
            adjustments.append(
                Adjustment(amount=locale.parse_amount(amount_str), description=label))
        return adjustments

    def get_amounts_in_text(pattern_map):
        amounts = dict()
        for key, label in pattern_map.items():
            amount = locale.parse_amount(get_other_field(label))
            amounts[key] = amount
        return amounts
    
    items_subtotal = locale.parse_amount(
        get_other_field(locale.items_subtotal))
    total_before_tax = locale.parse_amount(
        get_other_field(locale.total_before_tax))
    tax = get_adjustments(locale.digital_tax_collected)
    total_for_this_order = locale.parse_amount(
        get_other_field(locale.digital_total_order))
    
    logger.debug('parsing pretax adjustments...')
    output_fields = dict()
    output_fields['pretax_adjustments'] = get_adjustments(
        locale.pretax_adjustment_fields_pattern)
    pretax_parts = ([items_subtotal] +
                    [a.amount for a in output_fields['pretax_adjustments']])
    expected_total_before_tax = reduce_amounts_may_return_none(pretax_parts)
    if expected_total_before_tax != total_before_tax:
        errors.append('expected total before tax is %s, but parsed value is %s'
                    % (expected_total_before_tax, total_before_tax))
    
    logger.debug('parsing posttax adjustments...')
    output_fields['posttax_adjustments'] = get_adjustments(
        locale.posttax_adjustment_fields_pattern)
    posttax_parts = ([total_before_tax] + [a.amount for a in tax] +
                     [a.amount for a in output_fields['posttax_adjustments']])
    expected_total = reduce_amounts(posttax_parts)

    if expected_total != total_for_this_order:
        errors.append('expected total is %s, but parsed value is %s' %
                      (expected_total, total_for_this_order))

    if locale.tax_included_in_price:
        # tax is already inlcuded in item prices
        # do not add additional transaction for taxes
        tax = []

    shipment = Shipment(
        shipped_date=order_date,
        items=cast(List[Union[Item, DigitalItem]], items),
        items_subtotal=items_subtotal,
        total_before_tax=total_before_tax,
        tax=tax,
        total=total_for_this_order,
        errors=errors,
        **output_fields)

    # -------------
    # Payment Table
    # -------------
    logger.debug('parsing payment information...')
    payment_table = soup.find(
        lambda node: node.name == 'table' and
        node.text.strip().startswith(locale.digital_payment_information)
        )
    credit_card_transactions = parse_credit_card_transactions_from_payments_table(
        payment_table, order_date, locale=locale)

    logger.debug('...finished parsing digital invoice.')

    return Order(
        order_date=order_date,
        order_id=order_id,
        shipments=[shipment],
        credit_card_transactions=credit_card_transactions,
        pretax_adjustments=[],
        posttax_adjustments=output_fields['posttax_adjustments'],
        # tax given on "shipment level"
        # for digital orders tax is always given on shipment level
        # therefore tax on order level is irrelevant
        tax=None,
        errors=[])


def main():
    import argparse
    import sys
    import json
    ap = argparse.ArgumentParser()
    ap.add_argument('-q', '--quiet', default=False, action='store_true')
    ap.add_argument(
        '--json',
        default=False,
        action='store_true',
        help='Output in JSON format.')
    ap.add_argument(
        '--locale', default='en_US', help='Local Amazon settings, defaults to en_US')
    ap.add_argument('paths', nargs='*')

    args = ap.parse_args()
    locale = LOCALES[args.locale]
    results = []
    for path in args.paths:
        try:
            result = parse_invoice(path, locale=locale)
            results.append(result)
        except:
            sys.stderr.write('Error reading: %s\n' % path)
            if not args.quiet:
                raise
        if not args.quiet and not args.json:
            print(repr(result))
    if args.json:
        print(json.dumps(to_json(results), indent=4))


if __name__ == '__main__':
    main()

def parse_new_style_invoice(soup, locale=Locale_en_US) -> Order:
    """ Parse new style regular order invoice (div-based with data-component attributes)
    """
    errors = []  # type: Errors

    # Order ID
    order_id_div = soup.find(attrs={"data-component": "orderId"})
    if order_id_div:
        # Extract "111-0000001-0000001" from span
        # Structure: <div ...> ... <span>111-0000001-0000001</span> ... </div>
        # The span is deeply nested.
        # We can just look for the text that matches the order ID pattern within this div
        order_id_text = order_id_div.get_text(strip=True)
        # Use regex to find the ID in the text
        m = re.search(r'(\d{3}-\d{7}-\d{7})', order_id_text)
        if m:
            order_id = m.group(1)
        else:
             # Fallback or error
             errors.append("Could not extract order ID from data-component='orderId'")
             order_id = "UNKNOWN"
    else:
        errors.append("Could not find data-component='orderId'")
        order_id = "UNKNOWN"

    # Order Date
    order_date_div = soup.find(attrs={"data-component": "orderDate"})
    if order_date_div:
        # Structure: ... <span>March 15, 2025 <i ...></i></span>
        # Text might be "March 15, 2025 "
        date_text = order_date_div.get_text(strip=True)
        try:
            order_date = locale.parse_date(date_text)
        except Exception as e:
            errors.append(f"Could not parse order date: {date_text} ({e})")
            order_date = datetime.date.today() # Fallback
    else:
        errors.append("Could not find data-component='orderDate'")
        order_date = datetime.date.today()

    # Items
    items = []
    # Items are in data-component="purchasedItems" -> "purchasedItemsLeftGrid" -> "itemTitle", "itemImage", "quantity", "unitPrice", "orderedMerchant"
    # But the structure is nested. We can iterate over "purchasedItems" container.
    # Actually, looking at the HTML, "purchasedItems" contains the list.
    # Inside, it seems to be a list of items.
    # Let's try to find all "itemTitle" components, and for each, find the corresponding other components.
    # However, they might be in separate grids.
    # The HTML structure showed "purchasedItemsLeftGrid" containing "itemImage" and "quantity" (sometimes?),
    # and "purchasedItemsRightGrid" containing "itemTitle", "orderedMerchant", "unitPrice".
    # This suggests we need to iterate over rows or containers that group these.
    # In the provided HTML, "purchasedItems" seems to wrap the whole list?
    # Let's look at the structure again.
    # <div data-component="purchasedItems">
    #   <div data-component="purchasedItemsLeftGrid"> ... itemImage ... </div>
    #   <div data-component="purchasedItemsRightGrid"> ... itemTitle ... unitPrice ... orderedMerchant ... </div>
    # </div>
    # It seems there is one "purchasedItems" container per item? Or one big container?
    # The grep showed only one "purchasedItems" (line 2462).
    # But multiple "itemTitle" (line 2655).
    # Wait, grep showed:
    # {"File":... "LineNumber":2462, "LineContent":"  <div class=\"\" data-component=\"purchasedItems\">"}
    # {"File":... "LineNumber":2655, "LineContent":"  <div class=\"\" data-component=\"itemTitle\">"}
    # {"File":... "LineNumber":3139, "LineContent":"  <div class=\"\" data-component=\"unitPrice\">"}
    #
    # If there are multiple items, there should be multiple "itemTitle"s.
    # In the example HTML, there is only 1 item (quantity 2).
    # So we might need to find all containers that represent an item.
    # Usually, these are grouped.
    # Let's assume we can find all `data-component="itemTitle"` and traverse up/around to find others,
    # OR find a common parent for each item.
    #
    # Let's try to find all elements with `data-component="purchasedItemsLeftGrid"` (assuming one per item).
    # Or `data-component="purchasedItemsRightGrid"`.
    
    item_containers = soup.find_all(attrs={"data-component": "purchasedItemsRightGrid"})
    # We also need the left grid for quantity (maybe?)
    # In the example, quantity was in "quantity" component (line 3078), which was inside... where?
    # The view_file for quantity showed it near unitPrice? No, line 3078. Unit price 3139.
    # They seem to be in the same container or close.
    
    for item_container in item_containers:
        # Title
        title_div = item_container.find(attrs={"data-component": "itemTitle"})
        description = title_div.get_text(strip=True) if title_div else "Unknown Item"
        
        # Merchant
        merchant_div = item_container.find(attrs={"data-component": "orderedMerchant"})
        sold_by = merchant_div.get_text(strip=True).replace("Sold by:", "").strip() if merchant_div else None
        
        # Price
        price_div = item_container.find(attrs={"data-component": "unitPrice"})
        price_text = price_div.get_text(strip=True) if price_div else None
        # price_text might be "$10.25$10.25" (offscreen + visible)
        # We need to extract one valid price.
        if price_text:
             # Look for the first occurrence of a price pattern
             m_price = re.search(r'[\d.,]+', price_text)
             # This is risky if it catches garbage.
             # Better: find the span with aria-hidden="true" or class="a-offscreen"
             price_span = price_div.find(class_="a-offscreen")
             if price_span:
                 price_text = price_span.get_text(strip=True)
             else:
                 # Fallback
                 pass
             price = locale.parse_amount(price_text)
        else:
            price = Amount(D(0), locale.currency)

        # Quantity
        # In the HTML view:
        # <div ... data-component="quantity">
        #   <span ...>Qty:</span> <span>2</span>
        # </div>
        qty_div = item_container.find(attrs={"data-component": "quantity"})
        quantity = None
        if qty_div:
             qty_text = qty_div.get_text(strip=True)
             m_qty = re.search(r'Qty:\s*(\d+)', qty_text, re.IGNORECASE)
             if m_qty:
                 quantity = D(m_qty.group(1))
             else:
                 try:
                     q = D(qty_text)
                     if q > 0: quantity = q
                 except:
                     pass
        
        if quantity is None:
            # Try to find it in the sibling left grid
            # item_container is purchasedItemsRightGrid
            # parent should be the wrapper
            parent = item_container.parent
            if parent:
                left_grid = parent.find(attrs={"data-component": "purchasedItemsLeftGrid"})
                if left_grid:
                    # Look for od-item-view-qty
                    qty_view = left_grid.find(class_="od-item-view-qty")
                    if qty_view:
                        qty_text = qty_view.get_text(strip=True)
                        try:
                            quantity = D(qty_text)
                        except:
                            pass
        
        if quantity is None:
            quantity = D(1)

        items.append(Item(
            quantity=quantity,
            description=description,
            sold_by=sold_by,
            condition=None, # Not easily found yet
            price=price
        ))

    # Totals from Order Summary
    order_summary_div = soup.find(attrs={"data-component": "orderSummary"})
    
    # Subtotal
    # We can try to find "Item(s) Subtotal:" and the value next to it.
    # The structure is messy divs.
    # We can iterate all text nodes or look for specific labels.
    
    def get_summary_value(label_pattern):
        if not order_summary_div: return None
        # Find element containing the label, case insensitive
        label_el = order_summary_div.find(string=re.compile(label_pattern, re.IGNORECASE))
        if label_el:
            # The value is usually in a corresponding column.
            # In the HTML:
            # <div ... label ...> ... </div>
            # <div ... content ...> ... $20.50 ... </div>
            # They are in "od-line-item-row-label" and "od-line-item-row-content"
            # We can traverse up to "od-line-item-row" and find the content div.
            row = label_el.find_parent(class_="od-line-item-row")
            if row:
                content_div = row.find(class_="od-line-item-row-content")
                if content_div:
                    return content_div.get_text(strip=True)
        return None

    items_subtotal_text = get_summary_value(locale.items_subtotal)
    items_subtotal = locale.parse_amount(items_subtotal_text) if items_subtotal_text else None

    # Adjustments (Shipping, Promotion, etc.)
    # We iterate over the patterns defined in the locale
    
    # Pretax Adjustments
    pretax_adjustments = []
    
    # Helper to find adjustments based on regex pattern
    def find_adjustments(pattern_str):
        adjustments = []
        if not order_summary_div: return adjustments
        # The pattern might be complex (joined by |), so we search for text matching it
        # We can iterate over all rows in the summary
        rows = order_summary_div.find_all(class_="od-line-item-row")
        for row in rows:
            label_div = row.find(class_="od-line-item-row-label")
            if not label_div: continue
            label_text = label_div.get_text(strip=True)
            
            # Check if label matches the pattern
            # The pattern in locale is often like "(?:Shipping|Promotion ...):"
            # We need to be careful with the trailing colon which might or might not be in the text
            # The locale patterns usually include the colon.
            
            if re.match(pattern_str, label_text, re.IGNORECASE):
                content_div = row.find(class_="od-line-item-row-content")
                if content_div:
                    amount_text = content_div.get_text(strip=True)
                    amount = locale.parse_amount(amount_text)
                    if amount:
                        # Clean label (remove colon)
                        clean_label = label_text.rstrip(':').strip()
                        adjustments.append(Adjustment(description=clean_label, amount=amount))
        return adjustments

    pretax_adjustments.extend(find_adjustments(locale.pretax_adjustment_fields_pattern))

    # Total before tax
    total_before_tax_text = get_summary_value(locale.total_before_tax)
    total_before_tax = locale.parse_amount(total_before_tax_text) if total_before_tax_text else None

    # Tax (and other posttax adjustments)
    # Tax is usually separate but might be in posttax patterns for some locales?
    # In en_US, tax is separate.
    
    tax_text = get_summary_value(locale.regular_estimated_tax)
    tax_amount = locale.parse_amount(tax_text) if tax_text else Amount(D(0), locale.currency)
    tax = [Adjustment(description="Estimated tax to be collected", amount=tax_amount)] if tax_amount and tax_amount.number != ZERO else []
    
    # Posttax Adjustments
    posttax_adjustments = find_adjustments(locale.posttax_adjustment_fields_pattern)

    # Grand Total
    grand_total_text = get_summary_value(locale.regular_total_order)
    total = locale.parse_amount(grand_total_text) if grand_total_text else None

    # Payment / Credit Card
    # "payment_information" component?
    # <div data-component="payment_information"> ... </div> ?
    # In HTML: <div data-component="viewPaymentPlanSummaryWidget"> ?
    # Or just "Grand Total" section implies payment?
    # The example has "Prime Visa ending in 0000".
    # We can look for "paymentMethod" or similar.
    # In the HTML, line 1592: <span ... class="pmts-payments-instrument-detail-box-paystationpaymentmethod ...">Prime Visa ... ending in 0000</span>
    # This is inside data-component="orderSummary" -> "shippingAddress" (weirdly?) or just before it.
    # Actually, it's in a separate box often.
    # Let's look for "Credit Card transactions" or similar text if present.
    # In the new format, it might be different.
    # The example JSON has "Prime Visa" and "0000".
    # Let's try to find the payment instrument.
    # We can search for "ending in" pattern.
    
    credit_card_transactions = []
    # Try to find payment info text by looking for "ending in" pattern
    # This is more robust than looking for specific classes which might change
    ending_in_nodes = soup.find_all(string=re.compile(r"ending in \d+"))
    for node in ending_in_nodes:
        # node is the text element, e.g. "ending in 0000"
        # We want the surrounding context to get the card name
        # Traverse up to a block element or reasonable container
        container = node.parent
        # Go up a few levels to find the full description line, but stop at div/table
        # In the example: <span>Prime Visa... <span>ending in...</span></span>
        # So going up to the parent span/div should be enough.
        # We'll try to get text from the parent, and if it doesn't have the card name, go up one more.
        
        # Limit recursion to avoid grabbing too much text
        for _ in range(3):
            if container is None: break
            text = container.get_text(" ", strip=True)
            m = re.search(r'(.*) ending in (\d+)', text)
            if m:
                card_desc = m.group(1).strip()
                # Clean up description (remove "Earns 5%..." if it got included, though usually that's a sibling)
                # In the example: "Prime Visa ending in 0000" is clean.
                # But sometimes it might capture more.
                # "Prime Visa ending in 0000 Earns 5%..."
                # The regex (.*) is greedy.
                # We might want to restrict it or clean it.
                # For now, assume it's reasonably short or the container is specific enough.
                
                last_digits = m.group(2)
                
                # Check for duplicates
                if not any(c.card_ending_in == last_digits for c in credit_card_transactions):
                    credit_card_transactions.append(CreditCardTransaction(
                        date=order_date,
                        card_description=card_desc,
                        card_ending_in=last_digits,
                        amount=total if total else Amount(D(0), locale.currency)
                    ))
                break # Found valid match for this node
            container = container.parent

    # Construct Shipment
    shipment = Shipment(
        shipped_date=None, # Not easily found in this view, often null for new orders
        items=items,
        items_subtotal=items_subtotal,
        pretax_adjustments=pretax_adjustments,
        total_before_tax=total_before_tax,
        posttax_adjustments=[],
        tax=tax,
        total=total,
        errors=errors
    )

    return Order(
        order_id=order_id,
        order_date=order_date,
        shipments=[shipment],
        credit_card_transactions=credit_card_transactions,
        pretax_adjustments=pretax_adjustments,
        tax=tax_amount,
        posttax_adjustments=posttax_adjustments,
        errors=errors
    )
