import uuid
import json

from datetime import datetime

from django.db import models
from django.contrib.contenttypes.models import ContentType
from django.contrib.contenttypes.fields import GenericForeignKey
from six import python_2_unicode_compatible
from django.utils.six import text_type


def make_uuid():
    """the hyphens in uuids are unnecessary, and brevity will be an
    advantage in our urls."""
    u = uuid.uuid4()
    return text_type(u).replace('-', '')


class TransactionQuerySet(models.QuerySet):
    def for_object(self, obj):
        ctype = ContentType.objects.get_for_model(obj)
        return self.filter(content_type=ctype, object_id=obj.id)


@python_2_unicode_compatible
class Transaction(models.Model):
    PURCHASE = "Purchase"
    AUTH = "Auth"
    COMPLETE = "Complete"
    REFUND = "Refund"
    VALIDATE = "Validate"
    TYPE_CHOICES = [(s, s.title()) for s in
                    [PURCHASE, AUTH, COMPLETE, REFUND, VALIDATE]]

    PENDING = "pending"
    PROCESSING = "processing"
    SUCCESSFUL = "successful"
    FAILED = "failed"
    STATUS_CHOICES = [(s, s.title()) for s in
                      [PENDING, PROCESSING, SUCCESSFUL, FAILED]]

    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveIntegerField()
    content_object = GenericForeignKey('content_type', 'object_id')

    created = models.DateTimeField(default=datetime.now)
    transaction_type = models.CharField(max_length=16, choices=TYPE_CHOICES,
                                        default=PURCHASE)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES,
                              default=PENDING)
    amount = models.DecimalField(max_digits=10, decimal_places=2, null=True)

    secret = models.CharField(max_length=32, editable=False, default=make_uuid,
                              unique=True, db_index=True)
    result = models.TextField(blank=True)

    objects = TransactionQuerySet.as_manager()

    class Meta:
        ordering = ('-created', '-id')

    def __str__(self):
        return u"%s %s of $%.2f on %s" % (
            self.get_status_display(),
            self.get_transaction_type_display().lower(),
            self.amount, str(self.created))

    def complete_transaction(self, successful):
        '''Set the final transaction status (SUCCESSFUL or FAILED), but only if
           the previous status was PROCESSING. Return True in this case,
           otherwise False. '''

        status = self.SUCCESSFUL if successful else self.FAILED

        updated = bool(Transaction.objects.filter(id=self.id,
                                                  status=self.PROCESSING)
                                          .update(status=status))
        if updated:
            # set value on the instance too, so that subsequent save() calls
            # don't clobber the database update
            self.status = status

        return updated

    def get_result_dict(self):
        return json.loads(self.result)

    def set_result_dict(self, result_dict):
        self.result = json.dumps(result_dict, indent=4, sort_keys=True)

    result_dict = property(get_result_dict, set_result_dict)

    def save(self, **kwargs):
        if self.content_object and not self.amount:
            self.amount = self.content_object.get_amount()

        return super(Transaction, self).save(**kwargs)

    @property
    def merchant_reference(self):
        # Seems to have an undocumented 50 char limit
        return (u"(#%d) %s" % (self.pk, text_type(self.content_object)))[:50]

    @property
    def transaction_id(self):
        """DPS has a stupid 16-char limit on TxnId. We use the last
        half (most random part) of the uuid with pk appended."""
        return (u"%s/%d" % (self.secret, self.pk))[-16:]


# Two choices follow. BasicTransactionProtocol is the minimal subset
# required to get purchasing happening; FullTransactionProtocol
# supports notifications and recurring billing.

class BasicTransactionProtocol(object):
    """This is the minimal subset of the protocol required. Just
    implement 'amount'. This implementation will not support recurring
    payments, or success/failure notifications."""

    def get_amount(self):
        raise NotImplementedError()

    def is_recurring(self):
        return False


class FullTransactionProtocol(object):
    def get_amount(self):
        """Returns anything that can be cast to float via "%.2f"."""
        raise NotImplementedError()

    def is_recurring(self):
        """Returns boolean. If True, (set|get)_billing_token MUST also
        be implemented."""
        raise NotImplementedError()

    def set_billing_token(self, billing_token):
        """Store a billing token for recurring billing. Ignores return
        value."""
        raise NotImplementedError()

    def get_billing_token(self):
        """Returns recurring billing token or None."""
        raise NotImplementedError()

    def transaction_succeeded(self, transaction, interactive, status_updated):
        """Called when a payment succeeds. Optional."""
        pass

    def transaction_failed(self, transaction, interactive, status_updated):
        """Called when a payment fails. Optional."""
        pass

    def transaction_success_url(self, transaction=None):
        """Returns a success url to take the place of
           views.transaction_success. Optional."""
        pass

    def transaction_failure_url(self, transaction=None):
        """Returns a failure url to take the place of
           views.transaction_failure. Optional."""
        pass
