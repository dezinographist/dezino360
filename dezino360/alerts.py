"""Lead emails: the new-lead alert to the owner, and follow-ups to the lead on day 4, 8 and 12.

Both are switched OFF by default (Dezino360 Settings) and stay off until the owner approves
them in writing: the alert is a send, and the follow-ups go to real people. Until a sending
service is chosen (phase 6), every email the Hub sends goes to the test mail catcher, which
never delivers; guard_email_account stops anyone adding a real outgoing mail account.
"""

from email.utils import formataddr

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, get_url, now_datetime, time_diff_in_seconds

SETTINGS = "Dezino360 Settings"
FOLLOW_UP_DAYS = (4, 8, 12)
# A late step is sent only within this many days of its due day; older ones are skipped,
# so switching follow-ups on never sends a burst of old emails.
SEND_WINDOW_DAYS = 3
# Follow-ups stop as soon as a lead leaves these statuses (e.g. Meeting Booked).
FOLLOW_UP_STATUSES = ("New", "Contacted")
LEAD_FIELDS = [
	"name",
	"lead_name",
	"first_name",
	"email",
	"mobile_no",
	"source",
	"status",
	"plan_of_interest",
	"market",
	"consent_to_contact",
	"first_touch_page",
	"utm_source",
	"utm_medium",
	"utm_campaign",
	"creation",
	"d360_follow_ups_sent",
]


def settings():
	return frappe.get_cached_doc(SETTINGS)


def lead_url(name):
	return get_url(f"/crm/leads/{name}")


# ---- new-lead alert (to the owner) -------------------------------------------------


def new_lead_alert(doc, method=None):
	"""CRM Lead after_insert."""
	s = settings()
	if not cint(s.new_lead_alert) or not s.alert_to:
		return
	lead = frappe._dict({f: doc.get(f) for f in LEAD_FIELDS})
	frappe.sendmail(
		recipients=[s.alert_to],
		subject=_("New lead: {0} ({1})").format(lead.lead_name or lead.email, lead.source or _("no source")),
		template="d360_new_lead_alert",
		args={"lead": lead, "lead_url": lead_url(doc.name), "site": frappe.local.site},
		reference_doctype="CRM Lead",
		reference_name=doc.name,
	)


# ---- follow-ups (to the lead) ------------------------------------------------------


def follow_ups_ready(s=None):
	"""Follow-ups are on and every setting they show is filled in (postal address included)."""
	from dezino360.dezino360.doctype.dezino360_settings.dezino360_settings import FOLLOW_UP_FIELDS, is_filled

	s = s or settings()
	return cint(s.follow_ups) and all(is_filled(s.get(f)) for f in FOLLOW_UP_FIELDS)


def send_follow_ups(now=None):
	"""Hourly. `now` lets the tests pretend days have passed."""
	from dezino360 import listmonk_sync

	# Newsletter unsubscribes first, so nobody who has just left the newsletter gets a follow-up.
	# If Listmonk can't be read, this hour's follow-ups wait.
	try:
		listmonk_sync.sync_unsubscribes()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title="Dezino360: Listmonk unsubscribe sync failed; follow-ups held this hour")
		return []
	if not follow_ups_ready():
		return []
	now = frappe.utils.get_datetime(now) if now else now_datetime()
	oldest = add_to_date(now, days=-(FOLLOW_UP_DAYS[-1] + SEND_WINDOW_DAYS))
	leads = frappe.get_all(
		"CRM Lead",
		filters={
			"status": ["in", FOLLOW_UP_STATUSES],
			"converted": 0,
			"consent_to_contact": 1,
			"email": ["is", "set"],
			"d360_follow_ups_sent": ["<", len(FOLLOW_UP_DAYS)],
			"creation": [">=", oldest],
		},
		fields=LEAD_FIELDS,
	)
	sent = []
	for lead in leads:
		step = due_step(lead, now)
		if step:
			send_follow_up(lead, step)
			sent.append((lead.name, step))
	return sent


def due_step(lead, now):
	"""The follow-up (1, 2 or 3) to send now, or None."""
	age_days = time_diff_in_seconds(now, lead.creation) / 86400
	due = 0
	for step, day in enumerate(FOLLOW_UP_DAYS, start=1):
		if age_days >= day:
			due = step
	if not due or due <= cint(lead.d360_follow_ups_sent):
		return None
	if age_days >= FOLLOW_UP_DAYS[due - 1] + SEND_WINDOW_DAYS:
		return None
	return due


def unsubscribe_url(lead_name):
	from frappe.utils.verified_command import get_signed_params

	return get_url("/api/method/dezino360.alerts.unsubscribe?" + get_signed_params({"lead": lead_name}))


def send_follow_up(lead, step):
	s = settings()
	subject = s.get(f"fu{step}_subject")
	sender = formataddr((s.business_name, frappe.conf.get("auto_email_id") or s.alert_to))
	content = frappe.render_template(
		"templates/emails/d360_follow_up.html",
		{
			"lead": lead,
			"body": s.get(f"fu{step}_body"),
			"button": s.get(f"fu{step}_button"),
			"business_name": s.business_name,
			"website": s.website,
			"signature": s.sender_name,
			"booking_url": s.booking_url,
			"postal_address": " ".join((s.postal_address or "").split()),
			"unsubscribe_url": unsubscribe_url(lead.name),
		},
	)
	# Recorded on the lead first (its activity shows the email), then sent through the queue.
	# Frappe's own "Leave this conversation" link is left out: the email has its own
	# unsubscribe link, which also turns the lead's consent off.
	comm = frappe.get_doc(
		{
			"doctype": "Communication",
			"communication_type": "Communication",
			"communication_medium": "Email",
			"sent_or_received": "Sent",
			"subject": subject,
			"content": content,
			"sender": sender,
			"recipients": lead.email,
			"reference_doctype": "CRM Lead",
			"reference_name": lead.name,
		}
	).insert(ignore_permissions=True)
	frappe.sendmail(
		recipients=[lead.email],
		sender=sender,
		subject=subject,
		message=content,
		reference_doctype="CRM Lead",
		reference_name=lead.name,
		communication=comm.name,
		add_unsubscribe_link=0,
	)
	frappe.db.set_value("CRM Lead", lead.name, "d360_follow_ups_sent", step, update_modified=False)


@frappe.whitelist(allow_guest=True, methods=["GET"])
def unsubscribe(lead=None, **kwargs):
	"""The link in every follow-up: one click turns consent off, which stops the follow-ups."""
	from frappe.utils.verified_command import verify_request

	if not verify_request():  # shows "Invalid Link" for a missing or changed signature
		return
	name = settings().business_name or _("us")
	if lead and frappe.db.exists("CRM Lead", lead):
		if frappe.db.get_value("CRM Lead", lead, "consent_to_contact"):
			frappe.db.set_value("CRM Lead", lead, {"consent_to_contact": 0, "consent_timestamp": None})
			frappe.get_doc("CRM Lead", lead).add_comment(
				"Info", _("Unsubscribed from follow-up emails with the link in an email.")
			)
			frappe.db.commit()
	frappe.respond_as_web_page(
		_("You're unsubscribed"),
		_("You won't get any more follow-up emails from {0}.").format(frappe.utils.escape_html(name)),
		indicator_color="green",
	)


# ---- only the mail catcher until a sending service is approved -------------------------


def guard_email_account(doc, method=None):
	"""Email Account validate: while `dezino360_mail_catcher` is set (common site config),
	outgoing mail may only go to that catcher."""
	catcher = frappe.conf.get("dezino360_mail_catcher")
	if catcher and cint(doc.enable_outgoing) and (doc.smtp_server or "").strip() != catcher:
		frappe.throw(
			_("Outgoing email can only go to the test mail catcher ({0}) until the owner approves a sending service.").format(
				catcher
			)
		)
