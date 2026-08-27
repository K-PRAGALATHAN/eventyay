import logging
import dateutil.parser

from django.db.models import Exists, OuterRef, Q
from django.utils.timezone import now

from eventyay.base.models import CachedFile
from eventyay.base.models.event import SubEvent
from eventyay.base.models.orders import Order, OrderPosition
from eventyay.helpers.timezone import (
    attach_timezone_to_naive_clock_time,
    get_browser_timezone,
)

from .models import EmailQueue, EmailQueueFilter


logger = logging.getLogger(__name__)


class AudienceFilterMixin:
    """Resolves the orders matched by the composer's audience filters.

    This is the single source of truth for recipient selection: the live
    recipient count, the recipient preview and the actual send all go through
    it, so they can never disagree about who receives an email.
    """

    def get_matching_orders(self, cleaned_data):
        qs = Order.objects.filter(event=self.request.event)
        statusq = Q(status__in=cleaned_data['order_status'])
        if 'overdue' in cleaned_data['order_status']:
            statusq |= Q(status=Order.STATUS_PENDING, expires__lt=now())
        if 'pa' in cleaned_data['order_status']:
            statusq |= Q(status=Order.STATUS_PENDING, require_approval=True)
        if 'na' in cleaned_data['order_status']:
            statusq |= Q(status=Order.STATUS_PENDING, require_approval=False)
        orders = qs.filter(statusq)

        opq = OrderPosition.objects.filter(
            order=OuterRef('pk'),
            canceled=False,
            product_id__in=[p.pk for p in cleaned_data.get('products')],
        )

        if cleaned_data.get('has_filter_checkins'):
            ql = []
            if cleaned_data.get('not_checked_in'):
                ql.append(Q(checkins__list_id=None))
            if cleaned_data.get('checkin_lists'):
                ql.append(
                    Q(
                        checkins__list_id__in=[i.pk for i in cleaned_data.get('checkin_lists', [])],
                    )
                )
            if len(ql) == 2:
                opq = opq.filter(ql[0] | ql[1])
            elif ql:
                opq = opq.filter(ql[0])
            else:
                opq = opq.none()

        if cleaned_data.get('subevent'):
            opq = opq.filter(subevent=cleaned_data.get('subevent'))
        if cleaned_data.get('subevents_from'):
            opq = opq.filter(subevent__date_from__gte=cleaned_data.get('subevents_from'))
        if cleaned_data.get('subevents_to'):
            opq = opq.filter(subevent__date_from__lt=cleaned_data.get('subevents_to'))
        if cleaned_data.get('order_created_from') or cleaned_data.get('order_created_to'):
            browser_tz = get_browser_timezone(cleaned_data.get('browser_timezone'))

            def attach_timezone(dt_value):
                return attach_timezone_to_naive_clock_time(dt_value, browser_tz)

            if cleaned_data.get('order_created_from'):
                opq = opq.filter(order__datetime__gte=attach_timezone(cleaned_data['order_created_from']))
            if cleaned_data.get('order_created_to'):
                opq = opq.filter(order__datetime__lt=attach_timezone(cleaned_data['order_created_to']))

        return orders.annotate(match_pos=Exists(opq)).filter(match_pos=True).distinct()


class CopyDraftMixin:
    """
    Mixin to load a queued mail as an initial draft in a compose form via ?copyToDraft=<id>
    Supports both team and attendee email composition modes.
    """
    def load_copy_draft(self, request, form_kwargs, team_mode=False):
        if copy_id := request.GET.get('copyToDraft'):
            try:
                mail_id = int(copy_id)
                qm = EmailQueue.objects.get(id=mail_id, event=request.event)
                form_kwargs['initial'] = form_kwargs.get('initial', {})

                subject = qm.subject
                message = qm.message

                attachment = CachedFile.objects.filter(id__in=qm.attachments).first()

                try:
                    qmf = EmailQueueFilter.objects.get(mail=qm)
                except EmailQueueFilter.DoesNotExist:
                    qmf = None

                form_kwargs['initial'].update({
                    'subject': subject,
                    'message': message,
                    'reply_to': qm.reply_to,
                    'bcc': qm.bcc,
                })

                if attachment:
                    form_kwargs['initial']['attachment'] = attachment

                if qmf:
                    if team_mode:
                        form_kwargs['initial']['teams'] = qmf.teams or []
                    else:
                        form_kwargs['initial'].update({
                            'recipients': qmf.recipients or [],
                            'order_status': qmf.order_status or ['p', 'na'],
                            'has_filter_checkins': qmf.has_filter_checkins,
                            'not_checked_in': qmf.not_checked_in,
                        })

                        if qmf.products:
                            form_kwargs['initial']['products'] = request.event.products.filter(id__in=qmf.products)

                        if qmf.checkin_lists:
                            form_kwargs['initial']['checkin_lists'] = request.event.checkin_lists.filter(
                                id__in=qmf.checkin_lists
                            )

                        if qmf.subevent:
                            try:
                                form_kwargs['initial']['subevent'] = request.event.subevents.get(id=qmf.subevent)
                            except SubEvent.DoesNotExist:
                                # It's possible that the referenced subevent no longer exists; ignore in this case.
                                pass

                        for field in ['subevents_from', 'subevents_to', 'order_created_from', 'order_created_to']:
                            value = getattr(qmf, field, None)
                            if value:
                                form_kwargs['initial'][field] = dateutil.parser.isoparse(value) if isinstance(value, str) else value


            except (EmailQueue.DoesNotExist, ValueError, TypeError) as e:
                logger.warning('Failed to load EmailQueue for copyToDraft: %s' % e)


class QueryFilterOrderingMixin:
    """
    Mixin to provide search and dynamic ordering to list views using ?q= and ?ordering=
    """
    ordering_map = {
    'subject': 'subject',
    'recipient': 'first_recipient_email',
    '-subject': '-subject',
    '-recipient': '-first_recipient_email',
    'created': 'sent_at',
    '-created': '-sent_at',
    }

    def get_ordering(self):
        return self.ordering_map.get(self.request.GET.get('ordering'), '-sent_at')

    def get_filtered_queryset(self, base_qs):
        if query := self.request.GET.get('q'):
            base_qs = base_qs.filter(
                Q(subject__icontains=query) |
                Q(recipients__email__icontains=query)
            ).distinct()
        return base_qs.order_by(self.get_ordering())
