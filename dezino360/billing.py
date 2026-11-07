"""Billing (phase 8): Stripe, TEST ONLY (a Stripe sandbox). Design: infra/reports/phase-8-design.md.

Stripe sends signed events to the agency workspace (app.dezino360.com):
    POST /api/method/dezino360.billing.stripe_webhook
The signature is checked with Stripe's official library (HMAC-SHA256 of the raw body, 5-minute tolerance). Each event is
logged once (Dezino360 Stripe Event) and processed in the background. Processing never trusts the order events arrive
in: it fetches the subscription's current state from Stripe and applies that (owner, 6 Nov 2026).

Statuses (the approved terms: 3 retries in 7 days, pause day 14, cancel day 30):
    Active     paid (Stripe "active")
    Overdue    a payment failed (Stripe "past_due" / "unpaid"); the date is kept
    Paused     still unpaid 14 days after the first failure (daily check; a status and a note, nothing switched off)
    Cancelled  cancelled in Stripe; at day 30 the daily check cancels it in Stripe first

Test only: the key must be a Stripe test key (sk_test_ / rk_test_), and live-mode events are refused. Keys live in the
site's settings file only: dezino360_stripe_key (hub-stripe-secret) and dezino360_stripe_webhook_secret (the sandbox
set-up script). Card details never reach the Hub.
"""

import json
from datetime import datetime, timedelta, timezone

import frappe
from frappe.rate_limiter import rate_limit
from frappe.utils import cint, get_datetime, now_datetime

SETTINGS = "Dezino360 Settings"
SUB = "Dezino360 Subscription"
EVENT = "Dezino360 Stripe Event"
KEY_CONF = "dezino360_stripe_key"
SECRET_CONF = "dezino360_stripe_webhook_secret"
TOLERANCE = 300  # seconds; Stripe's default

# The only events the webhook acts on (the sandbox endpoint listens to these and nothing else).
EVENTS = {"checkout.session.completed", "invoice.paid", "invoice.payment_failed", "customer.subscription.updated",
	"customer.subscription.deleted"}

# The approved US plans (brand-canon, owner-approved 29 Oct 2026), in US cents. Setup fee on monthly plans only.
PLANS = {
	"starter": {"monthly": 9900, "quarterly": 29700, "annual": 99000, "setup": 19900},
	"master": {"monthly": 29900, "quarterly": 89700, "annual": 299000, "setup": 49900},
	"blaster": {"monthly": 49900, "quarterly": 149700, "annual": 499000, "setup": 79900},
}
TERMS = {"monthly": ("month", 1), "quarterly": ("month", 3), "annual": ("year", 1)}
PLAN_LABEL = {"starter": "Starter", "master": "Master", "blaster": "Blaster"}
TERM_LABEL = {"monthly": "Monthly", "quarterly": "Every 3 months", "annual": "Annual"}
STRIPE_STATUS = {"active": "Active", "trialing": "Active", "past_due": "Overdue", "unpaid": "Overdue",
	"canceled": "Cancelled", "incomplete_expired": "Cancelled", "incomplete": "Incomplete", "paused": "Paused"}
PAUSE_DAYS, CANCEL_DAYS = 14, 30


class Refused(Exception):
	pass


class NotStarted(Exception):
	"""A subscription whose first payment hasn't gone through (declined card): logged Ignored, no record."""


# ---- keys -----------------------------------------------------------------------------------------------------------

def _key():
	key = (frappe.conf.get(KEY_CONF) or "").strip()
	if not key.startswith(("sk_test_", "rk_test_")):
		raise Refused("billing works with a Stripe test key only (none set, or not a test key)")
	return key


def _enabled():
	return bool(cint(frappe.db.get_single_value(SETTINGS, "billing_enabled")))


# ---- the webhook ----------------------------------------------------------------------------------------------------

def _answer(status, message):
	frappe.local.response["http_status_code"] = status
	return {"ok": status < 300, "message_text": message}


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=600, seconds=60 * 60)
def stripe_webhook():
	import stripe

	raw = frappe.request.get_data() or b""
	secret = (frappe.conf.get(SECRET_CONF) or "").strip()
	if not secret:
		return _answer(503, "billing isn't set up on this workspace")
	try:
		stripe.Webhook.construct_event(raw.decode("utf-8"), frappe.request.headers.get("Stripe-Signature", ""), secret,
			tolerance=TOLERANCE)
		event = json.loads(raw)
	except (ValueError, UnicodeDecodeError, stripe.SignatureVerificationError):
		return _answer(400, "signature check failed")
	frappe.set_user("Administrator")  # the signature proved the sender
	if frappe.db.exists(EVENT, {"event_id": event.get("id")}):
		return _answer(200, "already received")
	if event.get("livemode"):
		_log(event, "Refused", "a live-mode event: this Hub takes test events only")
		return _answer(400, "live-mode events are refused")
	if event.get("type") not in EVENTS:
		_log(event, "Ignored", "not an event the Hub acts on")
		return _answer(200, "ignored")
	if not _enabled():
		_log(event, "Ignored", "billing is off on this workspace")
		return _answer(200, "billing is off")
	name = _log(event, "Received")
	frappe.enqueue("dezino360.billing.process_event", event_log=name, queue="short", enqueue_after_commit=True)
	return _answer(200, "received")


def _ids(event):
	"""(subscription ID, checkout session ID) named in an event, for any of the five types."""
	obj = (event.get("data") or {}).get("object") or {}
	kind = obj.get("object")
	if kind == "subscription":
		return obj.get("id"), None
	if kind == "checkout.session":
		return obj.get("subscription"), obj.get("id")
	if kind == "invoice":  # newer API versions name the subscription under parent.subscription_details
		parent = ((obj.get("parent") or {}).get("subscription_details") or {}).get("subscription")
		return obj.get("subscription") or parent, None
	return None, None


def _log(event, status, note=""):
	sub_id, session_id = _ids(event)
	doc = frappe.get_doc({"doctype": EVENT, "event_id": event.get("id"), "event_type": event.get("type"),
		"livemode": 1 if event.get("livemode") else 0, "status": status, "stripe_subscription": sub_id or "",
		"checkout_session": session_id or "", "received_at": now_datetime(), "note": note})
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc.name


# ---- processing: Stripe's current state, not the event order ------------------------------------------------------

def _plain(obj):
	return json.loads(str(obj)) if obj is not None and not isinstance(obj, dict) else (obj or {})


def fetch_subscription(sub_id):
	import stripe

	return _plain(stripe.Subscription.retrieve(sub_id, api_key=_key(), expand=["customer", "latest_invoice"]))


def fetch_session(session_id):
	import stripe

	return _plain(stripe.checkout.Session.retrieve(session_id, api_key=_key()))


def process_event(event_log, fetch_sub=None, fetch_sess=None, now=None):
	"""Background job for one logged event: fetch the subscription (and the checkout session) as they are now in
	Stripe, and apply that. fetch_sub / fetch_sess / now are for tests."""
	frappe.set_user("Administrator")
	log = frappe.get_doc(EVENT, event_log)
	try:
		if not log.stripe_subscription:
			raise Refused("no subscription named in this event")
		sub = (fetch_sub or fetch_subscription)(log.stripe_subscription)
		session = (fetch_sess or fetch_session)(log.checkout_session) if log.checkout_session else None
		doc = apply_state(sub, session, now=now)
		log.db_set({"status": "Applied", "subscription": doc.name, "note": f"now {doc.status}"})
	except NotStarted as e:
		log.db_set({"status": "Ignored", "note": str(e)[:240]})
	except Refused as e:
		log.db_set({"status": "Refused", "note": str(e)[:240]})
	except Exception as e:  # logged for the team; Stripe isn't asked to retry (the event was received)
		log.db_set({"status": "Error", "note": f"{type(e).__name__}: {str(e)[:220]}"})
		frappe.log_error(title="Stripe event not applied", message=frappe.get_traceback())
	frappe.db.commit()


def _plan_term(sub):
	"""The plan and term of the subscription's repeating price, checked against the approved prices."""
	items = ((sub.get("items") or {}).get("data")) or []
	for item in items:
		price = item.get("price") or {}
		meta = price.get("metadata") or {}
		plan, term = meta.get("d360_plan"), meta.get("d360_term")
		if plan in PLANS and term in TERMS:
			rec = price.get("recurring") or {}
			if (price.get("currency") != "usd" or price.get("unit_amount") != PLANS[plan][term]
					or (rec.get("interval"), rec.get("interval_count", 1)) != TERMS[term]):
				raise Refused(f"price {price.get('id')} doesn't match the approved {plan} {term} price")
			return plan, term, item
	raise Refused("no approved Dezino360 plan price on this subscription")


def _ts(value):
	return datetime.fromtimestamp(int(value), tz=timezone.utc).replace(tzinfo=None) if value else None


def apply_state(sub, session=None, now=None):
	"""Creates or updates the Dezino360 Subscription for this Stripe subscription, from its current state. Returns it."""
	now = get_datetime(now) if now else now_datetime()
	if sub.get("livemode"):
		raise Refused("a live-mode subscription: this Hub takes test data only")
	plan, term, item = _plan_term(sub)
	name = frappe.db.get_value(SUB, {"stripe_subscription": sub["id"]})
	if not name and sub.get("status") in ("incomplete", "incomplete_expired"):
		# design 3.3: a declined first payment creates nothing; the record starts once Stripe says it's paid
		raise NotStarted("first payment not completed: no record until it's paid")
	doc = frappe.get_doc(SUB, name) if name else frappe.new_doc(SUB)
	before = doc.status if name else None
	status = STRIPE_STATUS.get(sub.get("status"), "Incomplete")
	if status == "Overdue":
		doc.overdue_since = doc.overdue_since or now
		if before == "Paused":  # the Hub's own day-14 status stays until Stripe says otherwise
			status = "Paused"
	elif status == "Active":
		doc.overdue_since = None
	if status == "Cancelled" and not doc.cancelled_at:
		doc.cancelled_at = _ts(sub.get("canceled_at")) or now
	customer = sub.get("customer") if isinstance(sub.get("customer"), dict) else {}
	invoice = sub.get("latest_invoice") if isinstance(sub.get("latest_invoice"), dict) else {}
	doc.update({
		"stripe_subscription": sub["id"],
		"stripe_customer": customer.get("id") or (sub.get("customer") if isinstance(sub.get("customer"), str) else ""),
		"plan": PLAN_LABEL[plan], "term": TERM_LABEL[term], "status": status,
		"current_period_end": _ts(item.get("current_period_end") or sub.get("current_period_end")),
		"email": doc.email or customer.get("email") or "",
		"contact_name": doc.contact_name or customer.get("individual_name") or customer.get("name") or "",
		"test_mode": 1,
	})
	if status == "Active" and not doc.first_paid_at:
		doc.first_paid_at = now
	# setup fee: on monthly plans, the first paid invoice is the monthly price plus the setup fee
	if term == "monthly" and invoice.get("status") == "paid" and invoice.get("amount_paid") == PLANS[plan]["monthly"] + PLANS[plan]["setup"]:
		doc.setup_fee_paid = 1
	if session:
		fields = {f.get("key"): ((f.get("text") or {}).get("value") or "") for f in session.get("custom_fields") or []}
		details = session.get("customer_details") or {}
		doc.business_name = fields.get("business_name") or doc.business_name
		doc.email = details.get("email") or doc.email
		# the full name the customer typed (the links ask for it) before the name on the card
		named = session.get("collected_information") or {}
		doc.contact_name = (named.get("individual_name") or details.get("individual_name") or details.get("name")
			or doc.contact_name)
		doc.client_reference = session.get("client_reference_id") or doc.client_reference
		doc.stripe_checkout_session = session.get("id") or doc.stripe_checkout_session
	if not doc.business_name:
		doc.business_name = doc.contact_name or doc.email or "(name to come from checkout)"
	if not doc.lead:
		ref = doc.client_reference
		doc.lead = (ref if ref and frappe.db.exists("CRM Lead", ref) else None) or (
			frappe.db.get_value("CRM Lead", {"email": doc.email}) if doc.email else None)
	doc.flags.ignore_permissions = True
	doc.save()
	if before != doc.status:
		doc.add_comment("Info", f"Status: {doc.status} (from Stripe, test mode){'' if before is None else f', was {before}'}")
		notify_status(doc)
	return doc


# ---- the daily check: day 14 Paused, day 30 cancelled ---------------------------------------------------------------

def cancel_in_stripe(sub_id):
	import stripe

	return _plain(stripe.Subscription.cancel(sub_id, api_key=_key(), expand=["customer", "latest_invoice"]))


NOTIFY_STATUSES = ("Active", "Overdue", "Paused", "Cancelled")


def agency_users():
	"""The people who work in the agency desk: enabled desk users with System Manager or Sales Manager (not
	Administrator, which nobody logs in as)."""
	users = frappe.get_all("User", filters={"enabled": 1, "user_type": "System User",
		"name": ["not in", ["Administrator", "Guest"]]}, pluck="name")
	return [u for u in users if {"System Manager", "Sales Manager"} & set(frappe.get_roles(u))]


def notify_status(doc):
	"""A desk notification (the bell) when a subscription becomes Active, Overdue, Paused or Cancelled. Type "Alert":
	Frappe never emails these (design decision 6: no emails)."""
	if doc.status not in NOTIFY_STATUSES:
		return
	subject = f"Subscription {doc.status}: {doc.business_name} ({doc.plan}, {doc.term}){' · TEST' if doc.test_mode else ''}"
	for user in agency_users():
		frappe.get_doc({"doctype": "Notification Log", "for_user": user, "from_user": "Administrator", "type": "Alert",
			"subject": subject, "document_type": SUB, "document_name": doc.name}).insert(ignore_permissions=True)


def stripe_link(subscription_id):
	"""The subscription in Stripe's dashboard (test data). The sandbox's account ID, if set in the site's settings
	(dezino360_stripe_account), opens the right sandbox."""
	if not subscription_id:
		return ""
	account = (frappe.conf.get("dezino360_stripe_account") or "").strip()
	base = f"https://dashboard.stripe.com/{account}/test" if account.startswith("acct_") else "https://dashboard.stripe.com/test"
	return f"{base}/subscriptions/{subscription_id}"


def daily_check(now=None, cancel=None):
	"""Overdue for 14 days → Paused; unpaid for 30 days → cancelled in Stripe (test), then Cancelled. now / cancel are
	for tests. Runs every day (hooks.py) while billing is on."""
	if not _enabled():
		return []
	now = get_datetime(now) if now else now_datetime()
	done = []
	for row in frappe.get_all(SUB, filters={"status": ["in", ["Overdue", "Paused"]], "overdue_since": ["is", "set"]},
			fields=["name", "status", "overdue_since", "stripe_subscription"]):
		days = (now - get_datetime(row.overdue_since)).days
		doc = frappe.get_doc(SUB, row.name)
		if days >= CANCEL_DAYS:
			sub = (cancel or cancel_in_stripe)(row.stripe_subscription)
			apply_state(sub, now=now)
			done.append((row.name, "Cancelled"))
		elif days >= PAUSE_DAYS and row.status == "Overdue":
			doc.db_set("status", "Paused")
			doc.add_comment("Info", f"Status: Paused (unpaid for {days} days; services aren't switched off automatically)")
			notify_status(doc)
			done.append((row.name, "Paused"))
	frappe.db.commit()
	return done
