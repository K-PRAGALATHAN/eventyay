import datetime

import pytest
from django.utils.timezone import now

from eventyay.base.models import Event, Order, OrderPosition, Organizer, Product
from eventyay.plugins.sendmail.forms import MailForm, RecipientQueryForm
from eventyay.plugins.sendmail.models import (
    EmailQueue,
    EmailQueueFilter,
    RecipientRole,
    resolve_recipients,
)


@pytest.fixture
def event():
    organizer = Organizer.objects.create(name='Dummy', slug='dummy')
    return Event.objects.create(
        organizer=organizer,
        name='Dummy',
        slug='dummy',
        date_from=now(),
        plugins='tests.tickets.testdummy',
    )


@pytest.fixture
def product(event):
    return Product.objects.create(name='Test product', event=event, default_price=13)


def make_order(product, code, email, attendee_emails):
    order = Order.objects.create(
        event=product.event,
        status=Order.STATUS_PENDING,
        expires=now() + datetime.timedelta(hours=1),
        total=13,
        code=code,
        email=email,
        datetime=now(),
        locale='en',
    )
    for position_id, attendee_email in enumerate(attendee_emails, start=1):
        OrderPosition.objects.create(
            order=order,
            product=product,
            price=13,
            positionid=position_id,
            attendee_email=attendee_email,
        )
    return order


@pytest.fixture
def orders(product):
    """One order with an attendee address, one without."""
    return [
        make_order(product, 'AAAAA', 'buyer-a@dummy.test', ['alice@dummy.test']),
        make_order(product, 'BBBBB', 'buyer-b@dummy.test', ['']),
    ]


@pytest.mark.django_db
def test_resolve_recipients_attendees_mode_falls_back_to_order_contact(orders):
    recipients = resolve_recipients(orders, 'attendees')

    assert set(recipients) == {'alice@dummy.test', 'buyer-b@dummy.test'}
    assert recipients['alice@dummy.test']['roles'] == {RecipientRole.ATTENDEE}
    assert recipients['buyer-b@dummy.test']['roles'] == {RecipientRole.ATTENDEE_FALLBACK}


@pytest.mark.django_db
def test_resolve_recipients_both_mode_includes_order_contacts(orders):
    recipients = resolve_recipients(orders, 'both')

    assert set(recipients) == {
        'alice@dummy.test',
        'buyer-a@dummy.test',
        'buyer-b@dummy.test',
    }
    assert RecipientRole.BUYER in recipients['buyer-a@dummy.test']['roles']


@pytest.mark.django_db
def test_resolve_recipients_deduplicates_shared_address(product):
    """An address that is both buyer and attendee is only mailed once."""
    order = make_order(product, 'CCCCC', 'same@dummy.test', ['same@dummy.test'])

    recipients = resolve_recipients([order], 'both')

    assert set(recipients) == {'same@dummy.test'}
    assert recipients['same@dummy.test']['roles'] == {
        RecipientRole.ATTENDEE,
        RecipientRole.BUYER,
    }


@pytest.mark.django_db
def test_preview_and_send_resolve_the_same_recipients(event, orders):
    """The recipient preview must never disagree with what is actually sent."""
    mail = EmailQueue.objects.create(
        event=event, subject={'en': 'Subject'}, message={'en': 'Body'}, locale='en'
    )
    EmailQueueFilter.objects.create(
        mail=mail,
        recipients='both',
        order_status=['n'],
        orders=[order.pk for order in orders],
        products=[],
        checkin_lists=[],
    )

    mail.populate_to_users()

    assert set(mail.get_recipient_emails()) == set(resolve_recipients(orders, 'both'))


@pytest.mark.django_db
def test_recipient_query_form_allows_an_unwritten_email(event, product):
    """Counting recipients happens while subject and message are still empty."""
    form = RecipientQueryForm(
        data={
            'recipients': 'orders',
            'order_status': ['p'],
            'products': [str(product.pk)],
        },
        event=event,
    )

    assert form.is_valid(), form.errors
    assert form.cleaned_data['order_status'] == ['p']


@pytest.mark.django_db
def test_recipient_query_form_still_validates_filters(event):
    form = RecipientQueryForm(
        data={'recipients': 'orders', 'order_status': ['p']},
        event=event,
    )

    assert not form.is_valid()
    assert 'products' in form.errors


def compose_data(product, **overrides):
    data = {
        'recipients': 'orders',
        'order_status': ['p'],
        'products': [str(product.pk)],
        'subject_0': 'Subject',
        'message_0': 'Body',
        'delivery': 'now',
    }
    data.update(overrides)
    return data


@pytest.mark.django_db
def test_scheduling_requires_a_send_time(event, product):
    form = MailForm(data=compose_data(product, delivery='later'), event=event)

    assert not form.is_valid()
    assert 'scheduled_at' in form.errors


@pytest.mark.django_db
def test_send_now_ignores_a_leftover_schedule(event, product):
    """Switching back to "Send now" must not send at a stale scheduled time."""
    later = now() + datetime.timedelta(days=1)
    form = MailForm(
        data=compose_data(
            product,
            delivery='now',
            scheduled_at_0=later.strftime('%Y-%m-%d'),
            scheduled_at_1=later.strftime('%H:%M:%S'),
        ),
        event=event,
    )

    assert form.is_valid(), form.errors
    assert form.cleaned_data['scheduled_at'] is None


@pytest.mark.django_db
def test_placeholders_are_not_dumped_into_help_text(event, product):
    """They belong in the Insert placeholder drawer, not in a wall of help text."""
    form = MailForm(data=compose_data(product), event=event)

    for name in ('subject', 'message'):
        assert 'Available placeholders' not in str(form.fields[name].help_text or '')


@pytest.mark.django_db
def test_a_draft_is_never_sent_on_its_own(event, orders):
    mail = EmailQueue.objects.create(
        event=event,
        subject={'en': 'Subject'},
        message={'en': 'Body'},
        locale='en',
        is_draft=True,
    )
    EmailQueueFilter.objects.create(
        mail=mail,
        recipients='orders',
        order_status=['n'],
        orders=[order.pk for order in orders],
        products=[],
        checkin_lists=[],
    )
    mail.populate_to_users()

    assert mail.send(async_send=False) is False
    assert mail.sent_at is None
    assert not mail.recipients.filter(sent=True).exists()
