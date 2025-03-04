from xml.etree import cElementTree as ElementTree
from django.conf import settings
from django.http import HttpResponseRedirect
from six import text_type
from six.moves.urllib.request import Request, urlopen
from django.urls import reverse

from .models import Transaction


def _get_setting(name):
    return getattr(settings, name,
                   Exception("Please specify %s in settings." % name))


PXPAY_URL = getattr(settings, 'PXPAY_URL',
                    'https://sec.paymentexpress.com/pxaccess/pxpay.aspx')
PXPOST_URL = getattr(settings, 'PXPOST_URL',
                     'https://sec.paymentexpress.com/pxpost.aspx')
DEFAULT_CURRENCY = getattr(settings, 'DEFAULT_CURRENCY', 'NZD')


PXPAY_DEFAULTS = {
    "TxnType": "Purchase",
    "PxPayUserId": _get_setting("PXPAY_USERID"),
    "PxPayKey": _get_setting("PXPAY_KEY"),
    "CurrencyInput": DEFAULT_CURRENCY}


PXPOST_DEFAULTS = {
    "TxnType": "Purchase",
    "InputCurrency": DEFAULT_CURRENCY,
    "PostUsername": _get_setting("PXPOST_USERID"),
    "PostPassword": _get_setting("PXPOST_KEY")}


def _get_response(url, xml_body):
    """Takes and returns an ElementTree xml document."""
    req = Request(url, ElementTree.tostring(xml_body, encoding='utf-8'))
    response = urlopen(req)
    ret = ElementTree.fromstring(response.read())
    response.close()
    return ret


def _params_to_xml_doc(params, root="GenerateRequest"):
    """This function works in this simpler form because we never have
    to send nested structures to DPS; all sent structures are just a
    list of key=value inside a single root tag."""
    root_tag = ElementTree.Element(root)
    for (key, value) in params.items():
        # No params will be modified beyond this point, so if we still
        # have an Exception placeholder it's time to throw it.
        if isinstance(value, Exception):
            raise value
        elem = ElementTree.Element(key)
        elem.text = text_type(value)
        root_tag.append(elem)

    return root_tag


def begin_interactive(params):
    """Takes a params dictionary, returns the redirect to the DPS page
    to complete payment."""
    assert "UrlFail" in params
    assert "UrlSuccess" in params
    assert "MerchantReference" in params
    assert "AmountInput" in params

    merged_params = {}
    merged_params.update(PXPAY_DEFAULTS)
    merged_params.update(params)

    response = _get_response(PXPAY_URL,
                             _params_to_xml_doc(merged_params,
                                                root="GenerateRequest"))

    return HttpResponseRedirect(response.find("URI").text)


def get_interactive_result(result_key, param_overrides={}):
    """Unfortunately PxPay and PxPost have different XML reprs for
    transaction results, so we need a specific function for each.

    This function returns a dictionary of all the available params."""
    params = {
        "PxPayUserId": _get_setting("PXPAY_USERID"),
        "PxPayKey": _get_setting("PXPAY_KEY"),
        "Response": result_key}
    params.update(param_overrides)
    result = _get_response(PXPAY_URL,
                           _params_to_xml_doc(params, root="ProcessResponse"))

    output = {}
    for key in ["Success", "TxnType", "CurrencyInput", "MerchantReference",
                "TxnData1", "TxnData2", "TxnData3", "AuthCode", "CardName",
                "CardHolderName", "CardNumber", "DateExpiry", "ClientInfo",
                "TxnId", "EmailAddress", "DpsTxnRef", "BillingId",
                "DpsBillingId", "TxnMac", "ResponseText", "CardNumber2"]:
        output[key] = result.find(key).text

    output["valid"] = result.get("valid")

    return output


def offline_payment(params):
    """Make a non-interactive payment. Synchronous. Returns (success?, result).
    """
    try:
        assert (params.get("BillingId", None) or
                params.get("DpsBillingId", None) or
                (params.get("CardNumber", None) and params.get("Cvc2", None)))
        assert params.get("TxnId", None)
    except AssertionError as e:
        return (False, e)

    merged_params = {}
    merged_params.update(PXPOST_DEFAULTS)
    merged_params.update(params)

    result = _get_response(PXPOST_URL,
                           _params_to_xml_doc(merged_params, root="Txn"))

    # the following assumes that requesting a status returns the same
    # kind of markup as the original request. This assumption may not
    # be correct.
    status_required = result.find(".//StatusRequired")
    if status_required is not None and status_required.text == "1":
        status_params = {
            "PostUsername": _get_setting("PXPOST_USERID"),
            "PostPassword": _get_setting("PXPOST_KEY"),
            "TxnType": "Status",
            "TxnId": params.get("TxnId")}
        result = _get_response(PXPOST_URL,
                               _params_to_xml_doc(status_params, root="Txn"))

    success = result.find(".//Authorized").text == "1"

    result_dict = {}
    for node in result.findall('.//'):
        if node.text:
            result_dict[node.tag] = node.text
    return (success, result_dict)


def make_payment(content_object, request=None, transaction_opts={},
                 get_return_url=None):
    """Main entry point. If we have a request we do it interactive, otherwise
       it's a batch/offline payment."""

    trans = Transaction(content_object=content_object)
    trans.status = Transaction.PROCESSING
    trans.save()

    # PxPost and PxPay have different element names.
    amount_name = "AmountInput" if request else "Amount"

    # Basic params, needed in all cases.
    params = {amount_name: u"%.2f" % trans.amount,
              "MerchantReference": trans.merchant_reference}

    if request:
        # set up params for an interactive payment
        if get_return_url:
            return_url = get_return_url(trans)
        else:
            return_url = reverse('dps_process_transaction',
                                 args=(trans.secret, ))
        return_url = u"http://%s" % request.META['HTTP_HOST'] + \
                     return_url
        params.update({"UrlFail": return_url,
                       "UrlSuccess": return_url})
        if getattr(content_object, "is_recurring", lambda: False)():
            assert hasattr(content_object, "set_billing_token")
            assert hasattr(content_object, "get_billing_token")
            params["EnableAddBillCard"] = "1"
    else:
        # set up for an offline/batch payment.
        params.update({"TxnId": trans.transaction_id})
        if hasattr(content_object, 'get_billing_token'):
            params.update({"DpsBillingId": content_object.get_billing_token()})

    params.update(transaction_opts)

    if request:
        return begin_interactive(params)
    else:
        (success, result) = offline_payment(params)
        if success:
            status_updated = trans.complete_transaction(True)
            callback = getattr(content_object, "transaction_succeeded",
                               lambda *args: None)
        else:
            status_updated = trans.complete_transaction(False)
            callback = getattr(content_object, "transaction_failed",
                               lambda *args: None)
        trans.result_dict = result
        trans.save()
        redirect_url = callback(transaction=trans, interactive=False, status_updated=status_updated)
        return (success, trans, redirect_url)
