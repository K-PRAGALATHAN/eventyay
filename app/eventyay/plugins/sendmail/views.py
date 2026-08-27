import logging

import nh3
import uuid
from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import OuterRef, Subquery
from django.http import HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.functional import cached_property
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext_lazy
from django.views.generic import FormView, ListView, TemplateView, UpdateView, View

from eventyay.base.email import get_available_placeholders
from eventyay.base.i18n import language
from eventyay.base.meetup import is_meetup_event
from eventyay.base.models.base import CachedFile
from eventyay.base.models.event import Event
from eventyay.base.models.orders import InvoiceAddress
from eventyay.base.templatetags.rich_text import (
    build_email_preview_context,
    compile_email_body,
    is_placeholder_html_sample,
)
from eventyay.base.services.mail import (
    SendMailException,
    TolerantDict,
    expand_email_variable_chips,
)
# Aliased: ``mail`` is used as a loop variable by the outbox and sent-mail views.
from eventyay.base.services.mail import mail as send_single_email
from eventyay.common.mail import get_reply_to_address
from eventyay.control.permissions import EventPermissionRequiredMixin
from eventyay.control.views.event import EventSettingsFormView, EventSettingsViewMixin
from eventyay.helpers.timezone import format_scheduled_datetime
from eventyay.plugins.sendmail.forms import EmailQueueEditForm
from eventyay.plugins.sendmail.mixins import AudienceFilterMixin, CopyDraftMixin, QueryFilterOrderingMixin
from eventyay.plugins.sendmail.models import (
    ComposingFor,
    EmailQueue,
    EmailQueueFilter,
    EmailQueueToUser,
    RecipientRole,
    resolve_recipients,
)
from eventyay.plugins.sendmail.tasks import send_queued_mail

from . import forms
from .forms import MailContentSettingsForm, TeamMailForm


logger = logging.getLogger(__name__)


#: Placeholder categories offered by the composer's "Insert placeholder" drawer.
#: Anything an event exposes beyond these lands in a trailing "Other" group.
PLACEHOLDER_GROUPS = (
    (_('Recipient'), (
        'name', 'name_given_name', 'name_family_name', 'first_name', 'last_name',
        'email', 'attendee_name',
    )),
    (_('Order'), (
        'code', 'order_code', 'order_qr', 'order_status', 'order_created_at', 'total',
        'total_with_currency', 'url', 'url_cancel', 'url_info_change', 'url_products_change',
        'expire_date',
    )),
    (_('Ticket'), ('ticket_name', 'ticket_qr', 'download_tickets_pdf', 'check_in_status')),
    (_('Event'), ('event', 'event_name', 'event_slug', 'event_dates', 'join_online_event')),
    (_('Invoice and payment'), ('invoice_company', 'invoice_name', 'currency', 'payment_status')),
)


class BulkReplyToMixin:
    """Mixin for bulk email views to resolve Reply-To address."""

    def _get_reply_to_for_bulk_email(self):
        event = self.request.event
        sender = event.settings.get('mail_from') if event else settings.DEFAULT_FROM_EMAIL
        sender = sender or settings.DEFAULT_FROM_EMAIL
        return get_reply_to_address(event, sender_email=sender)


class ComposeMailChoice(EventPermissionRequiredMixin, TemplateView):
    permission_required = 'can_change_orders'
    template_name = 'pretixplugins/sendmail/compose_choice.html'


class SenderView(EventPermissionRequiredMixin, AudienceFilterMixin, CopyDraftMixin, BulkReplyToMixin, FormView):
    template_name = 'pretixplugins/sendmail/send_form.html'
    permission = 'can_change_orders'
    form_class = forms.MailForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['event'] = self.request.event
        self.load_copy_draft(self.request, kwargs)
        return kwargs

    def form_invalid(self, form):
        messages.error(self.request, _('We could not queue the email. See below for details.'))
        return super().form_invalid(form)

    def form_valid(self, form):
        orders = self.get_matching_orders(form.cleaned_data)

        if not orders:
            messages.error(self.request, _('There are no orders matching this selection.'))
            return self.get(self.request, *self.args, **self.kwargs)

        if self.request.POST.get('action') == 'preview':
            self.output = {}
            for l in self.request.event.settings.locales:
                with language(l, self.request.event.settings.region):
                    context_dict = build_email_preview_context(
                        self.request.event, ['event', 'order', 'position_or_address']
                    )
                    subject = nh3.clean(form.cleaned_data['subject'].localize(l), tags=set())
                    preview_subject = nh3.clean(subject.format_map(context_dict), tags=set())
                    message = form.cleaned_data['message'].localize(l)
                    message_preview = expand_email_variable_chips(
                        message.format_map(context_dict), dict(context_dict)
                    )
                    preview_text = compile_email_body(message_preview)

                    self.output[l] = {
                        'subject': _('Subject: {subject}').format(subject=preview_subject),
                        'html': preview_text,
                    }

            return self.get(self.request, *self.args, **self.kwargs)

        is_draft = self.request.POST.get('action') == 'draft'
        # A draft is never scheduled: it waits in the outbox until it is sent.
        scheduled_at = None if is_draft else form.cleaned_data.get('scheduled_at')
        qm = EmailQueue.objects.create(
            event=self.request.event,
            user=self.request.user,
            subject=form.cleaned_data['subject'].data,
            message=form.cleaned_data['message'].data,
            attachments=[form.cleaned_data['attachment'].id] if form.cleaned_data.get('attachment') else [],
            locale=self.request.event.settings.locale,
            reply_to=form.cleaned_data.get('reply_to') or self._get_reply_to_for_bulk_email() or '',
            bcc=form.cleaned_data.get('bcc') or self.request.event.settings.get('mail_bcc'),
            composing_for=ComposingFor.ATTENDEES,
            scheduled_at=scheduled_at,
            is_draft=is_draft,
        )

        EmailQueueFilter.objects.create(
            mail=qm,
            recipients=form.cleaned_data['recipients'],
            order_status=form.cleaned_data['order_status'],
            orders=list(orders.values_list('pk', flat=True)),
            products=[i.pk for i in form.cleaned_data.get('products')],
            checkin_lists=[cl.pk for cl in form.cleaned_data.get('checkin_lists')],
            has_filter_checkins=form.cleaned_data.get('has_filter_checkins'),
            not_checked_in=form.cleaned_data.get('not_checked_in'),
            subevent=form.cleaned_data.get('subevent').pk if form.cleaned_data.get('subevent') else None,
            subevents_from=form.cleaned_data.get('subevents_from'),
            subevents_to=form.cleaned_data.get('subevents_to'),
            order_created_from=form.cleaned_data.get('order_created_from'),
            order_created_to=form.cleaned_data.get('order_created_to'),
        )

        qm.populate_to_users()

        if is_draft:
            messages.success(
                self.request,
                _('Your draft has been saved. You can find it in the outbox.')
            )
        elif scheduled_at:
            send_queued_mail.apply_async(args=[self.request.event.pk, qm.pk], eta=scheduled_at)
            self.request.event.log_action(
                'eventyay.sendmail.scheduled',
                user=self.request.user,
                data={'email_queue_id': qm.pk, 'scheduled_at': scheduled_at.isoformat()},
            )
            messages.success(
                self.request,
                _('Your email has been scheduled for {datetime} ({timezone}).').format(
                    datetime=format_scheduled_datetime(self.request.event, scheduled_at),
                    timezone=self.request.event.timezone,
                )
            )
        else:
            messages.success(
                self.request,
                _('Your email has been sent to the outbox.')
            )

        return redirect(
            'control:event.mail.send',
            event=self.request.event.slug,
            organizer=self.request.event.organizer.slug,
        )

    def get_context_data(self, *args, **kwargs):
        ctx = super().get_context_data(*args, **kwargs)
        ctx['output'] = getattr(self, 'output', None)
        ctx['placeholder_groups'] = self.get_placeholder_groups()
        return ctx

    def get_placeholder_groups(self):
        """Group the available placeholders for the "Insert placeholder" drawer."""
        available = get_available_placeholders(self.request.event, ['event', 'order', 'position_or_address'])

        groups = []
        grouped_names = set()
        for label, names in PLACEHOLDER_GROUPS:
            items = [self.describe_placeholder(n, available[n]) for n in names if n in available]
            grouped_names.update(n for n in names if n in available)
            if items:
                groups.append({'label': label, 'items': items})

        remaining = sorted(set(available) - grouped_names)
        if remaining:
            groups.append({
                'label': _('Other'),
                'items': [self.describe_placeholder(n, available[n]) for n in remaining],
            })
        return groups

    #: Longest sample shown next to a placeholder before it is trimmed.
    sample_length = 80

    def describe_placeholder(self, name, placeholder):
        try:
            sample = str(placeholder.render_sample(self.request.event))
        except Exception:
            # A placeholder without a usable sample is still insertable.
            logger.debug('No sample available for placeholder %s', name, exc_info=True)
            sample = ''

        # Samples such as {order_qr} are whole HTML tags carrying a base64
        # image; showing that markup would bury the drawer in noise.
        if is_placeholder_html_sample(sample):
            sample = ''
        elif len(sample) > self.sample_length:
            sample = sample[: self.sample_length - 1] + '…'

        return {'name': name, 'token': '{' + name + '}', 'sample': sample}


class RecipientListView(EventPermissionRequiredMixin, AudienceFilterMixin, View):
    """JSON endpoint behind the live recipient count and the recipient preview.

    It runs the audience filters through the same code path as the actual send,
    so the number an organiser sees is the number of emails they will queue.
    """

    permission = 'can_change_orders'
    preview_limit = 100

    role_labels = {
        RecipientRole.ATTENDEE: _('Attendee'),
        RecipientRole.ATTENDEE_FALLBACK: _('Attendee (order contact)'),
        RecipientRole.BUYER: _('Ticket buyer'),
    }

    def post(self, request, *args, **kwargs):
        form = forms.RecipientQueryForm(data=request.POST, event=request.event)
        if not form.is_valid():
            return JsonResponse({'valid': False, 'count': 0, 'errors': form.errors.get_json_data()})

        orders = list(
            self.get_matching_orders(form.cleaned_data)
            .prefetch_related('positions__product', 'positions__checkins')
        )
        recipients = resolve_recipients(orders, form.cleaned_data.get('recipients') or 'orders')

        payload = {'valid': True, 'count': len(recipients)}
        if request.POST.get('preview'):
            payload['recipients'] = self.build_rows(recipients, orders)
            payload['limit'] = self.preview_limit
            payload['truncated'] = len(recipients) > self.preview_limit
        return JsonResponse(payload)

    def build_rows(self, recipients, orders):
        orders_by_pk = {o.pk: o for o in orders}
        positions_by_pk = {p.pk: p for o in orders for p in o.positions.all()}

        rows = []
        for email in sorted(recipients)[: self.preview_limit]:
            data = recipients[email]
            recipient_orders = [orders_by_pk[pk] for pk in sorted(data['orders']) if pk in orders_by_pk]
            positions = [positions_by_pk[pk] for pk in sorted(data['positions']) if pk in positions_by_pk]
            roles = data['roles']

            rows.append({
                'name': self.get_display_name(email, positions, recipient_orders),
                'email': email,
                'type': ', '.join(str(self.role_labels[r]) for r in sorted(roles) if r in self.role_labels),
                'order_codes': [o.code for o in recipient_orders],
                'order_status': (
                    str(recipient_orders[0].get_status_display()) if recipient_orders else ''
                ),
                'products': sorted({str(p.product) for p in positions if p.product_id}),
                'checked_in': any(p.checkins.all() for p in positions),
                'reason': str(self.get_reason(roles)),
            })
        return rows

    @staticmethod
    def get_display_name(email, positions, orders):
        for position in positions:
            if position.attendee_email and position.attendee_email.strip().lower() == email:
                if position.attendee_name:
                    return str(position.attendee_name)
        for order in orders:
            try:
                if order.invoice_address.name:
                    return str(order.invoice_address.name)
            except InvoiceAddress.DoesNotExist:
                continue
        return ''

    @staticmethod
    def get_reason(roles):
        if RecipientRole.BUYER in roles and (
            RecipientRole.ATTENDEE in roles or RecipientRole.ATTENDEE_FALLBACK in roles
        ):
            return _('Both ticket buyer and attendee — receives a single email.')
        if RecipientRole.ATTENDEE in roles:
            return _('Attendee address on a matching ticket.')
        if RecipientRole.ATTENDEE_FALLBACK in roles:
            return _('Order contact used because no attendee address is set.')
        return _('Order contact address of a matching order.')


class TestEmailView(EventPermissionRequiredMixin, BulkReplyToMixin, View):
    """Sends the email being composed to a single address using sample data.

    The test recipient never joins the campaign: nothing is queued, so it is not
    counted and cannot be sent to the real audience by accident.
    """

    permission = 'can_change_orders'

    @staticmethod
    def build_sample_context(event):
        """Plain sample values for the placeholders of a test email.

        ``build_email_preview_context`` wraps every sample in an HTML chip so it
        stands out on screen. A real email needs the bare values instead.
        """
        context = TolerantDict()
        for key, placeholder in get_available_placeholders(
            event, ['event', 'order', 'position_or_address']
        ).items():
            try:
                context[key] = str(placeholder.render_sample(event))
            except Exception:
                logger.debug('No sample available for placeholder %s', key, exc_info=True)
                context[key] = ''
        return context

    def post(self, request, *args, **kwargs):
        address = (request.POST.get('test_email') or '').strip()
        try:
            validate_email(address)
        except ValidationError:
            return JsonResponse(
                {'sent': False, 'error': str(_('Please enter a valid email address.'))},
                status=400,
            )

        form = forms.MailForm(data=request.POST, event=request.event)
        if not form.is_valid():
            # Name the fields that are actually in the way: a test send fails on
            # an empty audience just as often as on an empty subject.
            labels = [
                str(form.fields[name].label or name)
                for name in form.errors
                if name in form.fields
            ]
            error = (
                _('Please complete these fields first: {fields}.').format(fields=', '.join(labels))
                if labels
                else _('Please correct the errors in the form first.')
            )
            return JsonResponse(
                {'sent': False, 'error': str(error), 'errors': form.errors.get_json_data()},
                status=400,
            )

        event = request.event
        locale = event.settings.locale
        with language(locale, event.settings.region):
            context_dict = self.build_sample_context(event)
            try:
                send_single_email(
                    email=address,
                    subject=form.cleaned_data['subject'],
                    template=form.cleaned_data['message'],
                    context=dict(context_dict),
                    event=event,
                    locale=locale,
                    sender=event.settings.get('mail_from'),
                    event_reply_to=form.cleaned_data.get('reply_to') or self._get_reply_to_for_bulk_email() or None,
                    attach_cached_files=(
                        [form.cleaned_data['attachment'].id] if form.cleaned_data.get('attachment') else None
                    ),
                    user=request.user,
                    auto_email=False,
                    sync_send=True,
                )
            except SendMailException as error:
                logger.exception('Could not send test email for event %s', event.slug)
                return JsonResponse({'sent': False, 'error': str(error)}, status=502)

        return JsonResponse({
            'sent': True,
            'message': str(_('Test email sent to {email}.').format(email=address)),
        })


class MailTemplatesView(EventSettingsViewMixin, EventSettingsFormView):
    model = Event
    template_name = 'pretixplugins/sendmail/mail_templates.html'
    form_class = MailContentSettingsForm
    permission = 'can_change_event_settings'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['is_meetup_event'] = is_meetup_event(self.request.event)
        return context

    def form_invalid(self, form):
        messages.error(
            self.request,
            _('We could not save your changes. See below for details.'),
        )
        return super().form_invalid(form)

    @transaction.atomic
    def post(self, request, *args, **kwargs):
        form = self.get_form()
        if not form.is_valid():
            return self.form_invalid(form)

        form.save()
        if form.has_changed():
            self.request.event.log_action(
                'eventyay.event.settings',
                user=self.request.user,
                data={k: form.cleaned_data.get(k) for k in form.changed_data},
            )
        messages.success(self.request, _('Your changes have been saved.'))
        return redirect(reverse(
            'control:event.mail.templates',
            kwargs={
                'organizer': self.request.event.organizer.slug,
                'event': self.request.event.slug,
            },
        ))


class OutboxListView(EventPermissionRequiredMixin, QueryFilterOrderingMixin, ListView):
    model = EmailQueue
    context_object_name = 'mails'
    template_name = 'pretixplugins/sendmail/outbox_list.html'
    permission_required = 'can_change_orders'
    paginate_by = 25

    def get_template_names(self):
        if self.request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return ['pretixplugins/sendmail/outbox_list_content.html']
        return super().get_template_names()

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        query = self.request.GET.get('q', '')
        ordering = self.request.GET.get('ordering')

        ctx['headers'] = [
            ('subject', _('Subject')),
            ('recipient', _('To')),
        ]
        ctx['current_ordering'] = ordering
        ctx['query'] = query
        ctx['pending_mail_count'] = ctx['paginator'].count

        MAX_ERRORS_TO_SHOW = 2
        for mail in ctx['mails']:
            mail.recipient_emails_display = ", ".join(mail.get_recipient_emails())
            all_recipients = mail.recipients.all()
            errors = [r for r in all_recipients if r.error]
            mail.recipient_errors_preview = errors[:MAX_ERRORS_TO_SHOW]
            mail.recipient_error_count = len(errors)

        return ctx

    def get_queryset(self):
        first_recipient_email = EmailQueueToUser.objects.filter(
            mail=OuterRef('pk')
        ).order_by('id').values('email')[:1]

        base_qs = self.model.objects.filter(
            event=self.request.event,
            sent_at__isnull=True
        ).select_related('event', 'user').prefetch_related('recipients').annotate(
            first_recipient_email=Subquery(first_recipient_email)
        )

        return self.get_filtered_queryset(base_qs)


class SendEmailQueueView(EventPermissionRequiredMixin, View):
    permission_required = 'can_change_orders'

    def post(self, request, *args, **kwargs):
        mail = get_object_or_404(
            EmailQueue,
            event=request.event,
            pk=kwargs['pk']
        )

        if mail.sent_at:
            messages.warning(request, _('This mail has already been sent.'))
        else:
            # Sending a draft is the explicit release the draft flag waits for.
            if mail.is_draft:
                mail.is_draft = False
                mail.save(update_fields=['is_draft'])
            # Enqueue the Celery task
            send_queued_mail.apply_async(args=[request.event.pk, mail.pk])
            messages.success(
                request,
                _('The mail has been queued for sending.')
            )

        return HttpResponseRedirect(reverse('control:event.mail.outbox', kwargs={
            'organizer': request.event.organizer.slug,
            'event': request.event.slug,
        }))


class EditEmailQueueView(EventPermissionRequiredMixin, UpdateView):
    model = EmailQueue
    form_class = EmailQueueEditForm
    template_name = 'pretixplugins/sendmail/outbox_form.html'
    permission_required = 'can_change_orders'

    def get_object(self, queryset=None):
        return get_object_or_404(
            EmailQueue, event=self.request.event, pk=self.kwargs["pk"]
        )

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['event'] = self.request.event
        kwargs['read_only'] = bool(self.object.sent_at)
        return kwargs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['read_only'] = bool(self.object.sent_at)

        if self.object.attachments:
            ctx['attachments_files'] = CachedFile.objects.filter(
                id__in=self.object.attachments
            )
        else:
            ctx['attachments_files'] = []

        ctx['output'] = getattr(self, 'output', None)

        return ctx

    def form_invalid(self, form):
        messages.error(self.request, _('We could not save the email. See below for details.'))
        return super().form_invalid(form)

    def form_valid(self, form):
        if form.instance.sent_at:
            messages.error(self.request, _('This email has already been sent and cannot be edited.'))
            return self.form_invalid(form)

        if self.request.POST.get('action') == 'preview':
            self.output = {}
            event = self.request.event
            subject = form.cleaned_data['subject']
            message = form.cleaned_data['message']

            if form.instance.composing_for == ComposingFor.TEAMS:
                base_placeholders = ['event', 'team']
            else:
                base_placeholders = ['event', 'order', 'position_or_address']

            for l in event.settings.locales:
                with language(l, event.settings.region):
                    context_dict = build_email_preview_context(event, base_placeholders)

                    try:
                        subject_preview = nh3.clean(
                            subject.localize(l).format_map(context_dict),
                            tags=set(),
                        )
                    except KeyError as e:
                        form.add_error('subject', _('Invalid placeholder(s): {}').format(str(e)))
                        return self.form_invalid(form)

                    try:
                        message_preview = expand_email_variable_chips(
                            message.localize(l).format_map(context_dict),
                            dict(context_dict),
                        )
                    except KeyError as e:
                        form.add_error('message', _('Invalid placeholder(s): {}').format(str(e)))
                        return self.form_invalid(form)

                    self.output[l] = {
                        'subject': _('Subject: {subject}').format(subject=subject_preview),
                        'html': compile_email_body(message_preview),
                    }

            return self.get(self.request, *self.args, **self.kwargs)

        response = super().form_valid(form)
        messages.success(self.request, _('Your changes have been saved.'))
        return response

    def get_success_url(self):
        return reverse('control:event.mail.outbox', kwargs={
            'organizer': self.request.event.organizer.slug,
            'event': self.request.event.slug
        })


class DeleteEmailQueueView(EventPermissionRequiredMixin, TemplateView):
    permission_required = 'can_change_orders'
    template_name = 'pretixplugins/sendmail/delete_confirmation.html'

    @cached_property
    def mail(self):
        return get_object_or_404(
            EmailQueue, event=self.request.event, pk=self.kwargs['pk']
        )

    def question(self):
        return _('Do you really want to delete this mail?')

    def post(self, request, *args, **kwargs):
        mail = self.mail
        if mail.sent_at:
            messages.error(
                request,
                _("This mail has already been sent and cannot be deleted.")
            )
        else:
            EmailQueueFilter.objects.filter(mail=mail).delete()
            EmailQueueToUser.objects.filter(mail=mail).delete()
            mail.delete()

            messages.success(
                request,
                _("The mail and its related data have been deleted.")
            )

        return redirect(reverse('control:event.mail.outbox', kwargs={
            'organizer': request.event.organizer.slug,
            'event': request.event.slug
        }))


class PurgeEmailQueuesView(EventPermissionRequiredMixin, TemplateView):
    permission_required = 'can_change_orders'
    template_name = 'pretixplugins/sendmail/purge_confirmation.html'

    def get_permission_object(self):
        return self.request.event

    def question(self):
        count = EmailQueue.objects.filter(event=self.request.event, sent_at__isnull=True).count()
        return ngettext_lazy(
            "Do you really want to purge the mail?",
            "Do you really want to purge {count} mails?",
            count
        ).format(count=count)

    def post(self, request, *args, **kwargs):
        mails = EmailQueue.objects.filter(event=request.event, sent_at__isnull=True)

        EmailQueueFilter.objects.filter(mail__in=mails).delete()
        EmailQueueToUser.objects.filter(mail__in=mails).delete()
        count = mails.count()
        mails.delete()

        messages.success(
            request,
            ngettext_lazy(
                "One mail has been discarded.",
                "{count} mails have been discarded.",
                count
            ).format(count=count)
        )

        return redirect(reverse('control:event.mail.outbox', kwargs={
            'organizer': request.event.organizer.slug,
            'event': request.event.slug
        }))


class SentMailView(EventPermissionRequiredMixin, QueryFilterOrderingMixin, ListView):
    model = EmailQueue
    context_object_name = "mails"
    template_name = "pretixplugins/sendmail/sent_list.html"
    permission_required = "can_change_orders"
    paginate_by = 25

    def get_queryset(self):
        first_recipient_email = EmailQueueToUser.objects.filter(
            mail=OuterRef('pk')
        ).order_by('pk').values('email')[:1]

        base_qs = self.model.objects.filter(
            event=self.request.event,
            sent_at__isnull=False
        ).select_related('event', 'user').prefetch_related('recipients').annotate(
            first_recipient_email=Subquery(first_recipient_email)
        )

        return self.get_filtered_queryset(base_qs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)

        query = self.request.GET.get('q', '')
        ordering = self.request.GET.get('ordering')

        ctx['headers'] = [
            ('subject', _('Subject')),
            ('recipient', _('To')),
            ('created', _('Sent at')),
        ]
        ctx['current_ordering'] = ordering
        ctx['query'] = query

        MAX_RECIPIENTS_TO_SHOW = 3
        for mail in ctx['mails']:
            users = EmailQueueToUser.objects.filter(mail=mail).order_by('pk')[:MAX_RECIPIENTS_TO_SHOW]
            mail.recipient_preview = [u.email or u.user_display or u.order_code for u in users]
            mail.recipient_total = EmailQueueToUser.objects.filter(mail=mail).count()

        return ctx


class ComposeTeamsMail(EventPermissionRequiredMixin, CopyDraftMixin, BulkReplyToMixin, FormView):
    template_name = 'pretixplugins/sendmail/send_team_form.html'
    permission = 'can_change_orders'
    form_class = TeamMailForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['event'] = self.request.event
        self.load_copy_draft(self.request, kwargs, team_mode=True)
        return kwargs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx['output'] = getattr(self, 'output', None)

        return ctx

    def form_invalid(self, form):
        messages.error(self.request, _('We could not save the email. See below for details.'))
        return super().form_invalid(form)

    def form_valid(self, form):
        event = self.request.event
        user = self.request.user
        subject = form.cleaned_data['subject']
        message = form.cleaned_data['message']

        self.output = {}
        for l in event.settings.locales:
            with language(l, event.settings.region):
                context_dict = build_email_preview_context(event, ['event', 'team'])

                try:
                    subject_preview = nh3.clean(
                        subject.localize(l).format_map(context_dict),
                        tags=set(),
                    )
                except KeyError as e:
                    form.add_error('subject', _('Invalid placeholder(s): {}').format(str(e)))
                    return self.form_invalid(form)

                try:
                    message_preview = expand_email_variable_chips(
                        message.localize(l).format_map(context_dict),
                        dict(context_dict),
                    )
                except KeyError as e:
                    form.add_error('message', _('Invalid placeholder(s): {}').format(str(e)))
                    return self.form_invalid(form)

                if self.request.POST.get('action') == 'preview':
                    self.output[l] = {
                        'subject': _('Subject: {subject}').format(subject=subject_preview),
                        'html': compile_email_body(message_preview),
                    }

        if self.request.POST.get('action') == 'preview':
            return self.get(self.request, *self.args, **self.kwargs)

        sent_emails = set()
        recipients_list = []
        for team in form.cleaned_data['teams']:
            for member in team.members.all():
                if not member.email or member.email in sent_emails:
                    continue
                recipients_list.append({
                    "email": member.email,
                    "team": team.pk,
                    "orders": [],
                    "positions": [],
                    "products": [],
                    "sent": False,
                    "error": None
                })

                sent_emails.add(member.email)

        if not recipients_list:
            messages.error(self.request, _('There are no valid recipients for the selected teams.'))
            return self.form_invalid(form)

        # Create the EmailQueue instance
        scheduled_at = form.cleaned_data.get('scheduled_at')
        mail_instance = EmailQueue.objects.create(
            event=event,
            user=user,
            composing_for=ComposingFor.TEAMS,
            subject=subject.data,
            message=message.data,
            locale=event.settings.locale,
            reply_to=self._get_reply_to_for_bulk_email() or '',
            bcc=event.settings.get('mail_bcc'),
            attachments=[form.cleaned_data['attachment'].id] if form.cleaned_data.get('attachment') else [],
            scheduled_at=scheduled_at,
        )

        # Create associated filter data for teams
        EmailQueueFilter.objects.create(
            mail=mail_instance,
            order_status=[],
            products=[],
            checkin_lists=[],
            has_filter_checkins=False,
            not_checked_in=False,
            subevent=None,
            subevents_from=None,
            subevents_to=None,
            order_created_from=None,
            order_created_to=None,
            orders=[],
            teams=[team.pk for team in form.cleaned_data['teams']],
        )

        # Create recipient entries for each team member
        recipient_objs = [
            EmailQueueToUser(
                mail=mail_instance,
                email=rec["email"],
                team=rec["team"],
                sent=rec["sent"],
                error=rec["error"]
            )
            for rec in recipients_list
        ]
        EmailQueueToUser.objects.bulk_create(recipient_objs)

        if scheduled_at:
            send_queued_mail.apply_async(args=[event.pk, mail_instance.pk], eta=scheduled_at)
            event.log_action(
                'eventyay.sendmail.scheduled',
                user=user,
                data={'email_queue_id': mail_instance.pk, 'scheduled_at': scheduled_at.isoformat()},
            )
            messages.success(
                self.request,
                _('Your email has been scheduled for {datetime} ({timezone}).').format(
                    datetime=format_scheduled_datetime(self.request.event, scheduled_at),
                    timezone=self.request.event.timezone,
                )
            )
        else:
            messages.success(
                self.request,
                _('Your email has been sent to the outbox.')
            )

        return redirect(reverse('control:event.mail.compose_teams', kwargs={
            'organizer': event.organizer.slug,
            'event': event.slug
        }))
