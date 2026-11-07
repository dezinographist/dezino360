"""The Dezino360 CRM set-up on every site: lead sources, lead statuses, deal stages,
custom fields, where those fields show in the CRM, and the lead assignment rule.

Statuses, stages and sources are set once per site (SETUP_VERSION), so later changes made
in the CRM's own settings are kept. Raise SETUP_VERSION to apply a changed set again.
Custom fields and the page layout are checked after every migrate.
"""

import json

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

SETUP_VERSION = 1
SETUP_KEY = "dezino360_crm_setup"

LEAD_SOURCES = ["Website Form", "Chat", "Phone", "Booking", "Meta Lead Ad", "Manual", "AI Receptionist"]

# (name, type, colour). "Lost" types need a lost reason (Frappe CRM's own rule).
# Meeting Booked, Test and Junk Lead are the exact names used in the live Zoho set-up.
LEAD_STATUSES = [
	("New", "Open", "gray"),
	("Contacted", "Ongoing", "orange"),
	("Meeting Booked", "Ongoing", "blue"),
	("Qualified", "Won", "green"),
	("Unqualified", "Lost", "red"),
	("Test", "On Hold", "cyan"),
	("Junk Lead", "Lost", "purple"),
]

# (name, type, colour, probability %). Lost needs a reason (Frappe CRM checks it).
DEAL_STAGES = [
	("Discovery", "Open", "gray", 10),
	("Proposal Sent", "Ongoing", "blue", 40),
	("Negotiation", "Ongoing", "amber", 70),
	("Won", "Won", "green", 100),
	("Lost", "Lost", "red", 0),
]

# Added to Frappe CRM's own list (Pricing, Competition, ... Other), so junk is one click.
EXTRA_LOST_REASONS = ["Spam or junk"]

PLANS = ["Starter", "Master", "Blaster", "Custom"]
MARKETS = ["Australia", "North America", "India"]  # house order: AU -> NA -> India

# Same field names on Lead and Deal, so converting a lead copies them to the deal.
FIELDS = [
	{"fieldname": "plan_of_interest", "label": "Plan of Interest", "fieldtype": "Select", "options": "\n" + "\n".join(PLANS)},
	{"fieldname": "market", "label": "Market", "fieldtype": "Select", "options": "\n" + "\n".join(MARKETS)},
	{"fieldname": "consent_to_contact", "label": "Consent to Contact", "fieldtype": "Check", "default": "0"},
	{
		"fieldname": "consent_timestamp",
		"label": "Consent Given At",
		"fieldtype": "Datetime",
		"description": "Filled in automatically when consent is ticked; cleared when it is unticked.",
	},
	{"fieldname": "first_touch_page", "label": "First-Touch Page", "fieldtype": "Data", "length": 500},
	{"fieldname": "utm_source", "label": "UTM Source", "fieldtype": "Data"},
	{"fieldname": "utm_medium", "label": "UTM Medium", "fieldtype": "Data"},
	{"fieldname": "utm_campaign", "label": "UTM Campaign", "fieldtype": "Data"},
]
LEAD_ONLY_FIELDS = [
	{
		"fieldname": "d360_follow_ups_sent",
		"label": "Follow-ups Sent",
		"fieldtype": "Int",
		"default": "0",
		"read_only": 1,
		"no_copy": 1,
		"description": "How many of the day 4 / 8 / 12 follow-up emails this lead has had.",
	},
]

# The live Worker's day 4 / 8 / 12 emails (dezino360 src/emails.js), one call to action each.
# DRAFT for the reviewer. The default for the agency site only: every client workspace writes its
# own in its settings, and can't switch follow-ups on until it has.
DEFAULT_FOLLOW_UPS = {
	1: {
		"subject": "Not sure where to start? A 15-minute call can help",
		"body": "<p>You got in touch with us recently and we have not managed to speak yet.</p>"
		"<p>Many business owners tell us the hard part is knowing where to start: the logo, the website, "
		"or getting found online. A short call helps us point you to the right first step, even if that "
		"step is not with us.</p>",
		"button": "Pick a time for a 15-minute call",
	},
	2: {
		"subject": "Three things your website should say in five seconds",
		"body": "<p>A quick tip you can use today, whoever builds your website.</p>"
		"<p>A visitor should be able to answer three questions within five seconds of landing:</p>"
		"<ol><li>What do you do?</li><li>Who is it for?</li><li>What should I do next?</li></ol>"
		"<p>If any answer is unclear, that is usually the first thing to fix. We are happy to look at your "
		"page with you on a free 15-minute call and tell you what we see.</p>",
		"button": "Book your 15-minute call",
	},
	3: {
		"subject": "Should we close your inquiry?",
		"body": "<p>This is our last note about your inquiry. We do not want to fill your inbox.</p>"
		"<p>If your project is still on your list, the quickest next step is a free 15-minute call. "
		"If the timing is wrong, no problem.</p>",
		"button": "Book your 15-minute call",
	},
}

LAYOUT_SECTION = "d360_section"
ASSIGNMENT_RULE = "Dezino360: new leads to the owner"


def owner_email():
	return (frappe.conf.get("dezino360_owner_email") or "grow@dezino360.com").strip().lower()


def apply():
	set_lead_sources()  # only adds missing ones (AI Receptionist came with phase 7)
	if frappe.db.get_default(SETUP_KEY) != str(SETUP_VERSION):
		set_statuses("CRM Lead Status", "lead_status", LEAD_STATUSES, "CRM Lead")
		set_statuses("CRM Deal Status", "deal_status", DEAL_STAGES, "CRM Deal")
		add_lost_reasons()
		frappe.db.set_default(SETUP_KEY, str(SETUP_VERSION))
	add_custom_fields()
	add_to_layouts()
	ensure_assignment_rule()
	once("dezino360_time_zone", set_system_time_zone)
	once("dezino360_owner_settings", owner_user_settings)
	once("dezino360_owner_no_assignment_email", owner_no_assignment_email)


def set_lead_sources():
	for source in LEAD_SOURCES:
		if not frappe.db.exists("CRM Lead Source", source):
			frappe.get_doc({"doctype": "CRM Lead Source", "source_name": source}).insert(ignore_permissions=True)
	# Frappe CRM's sample sources go, unless a lead or deal already uses one.
	for name in frappe.get_all("CRM Lead Source", pluck="name"):
		if name not in LEAD_SOURCES:
			_delete_if_unused("CRM Lead Source", name, [("CRM Lead", "source"), ("CRM Deal", "source")])


def set_statuses(doctype, name_field, wanted, used_by):
	names = [row[0] for row in wanted]
	for position, row in enumerate(wanted, start=1):
		name, kind, colour = row[0], row[1], row[2]
		doc = frappe.get_doc(doctype, name) if frappe.db.exists(doctype, name) else frappe.new_doc(doctype)
		doc.update({name_field: name, "type": kind, "color": colour, "position": position})
		if len(row) > 3:
			doc.probability = row[3]
		doc.save(ignore_permissions=True)
	# Frappe CRM's sample statuses go, unless a record already uses one.
	for name in frappe.get_all(doctype, pluck="name"):
		if name not in names:
			_delete_if_unused(doctype, name, [(used_by, "status")])


def add_lost_reasons():
	for reason in EXTRA_LOST_REASONS:
		if not frappe.db.exists("CRM Lost Reason", reason):
			frappe.get_doc({"doctype": "CRM Lost Reason", "lost_reason": reason}).insert(ignore_permissions=True)


def _delete_if_unused(doctype, name, links):
	if any(frappe.db.exists(dt, {field: name}) for dt, field in links):
		return
	frappe.delete_doc(doctype, name, ignore_permissions=True, force=True)


def add_custom_fields():
	def place(fields, after):
		rows = []
		for f in fields:
			rows.append({**f, "insert_after": after})
			after = f["fieldname"]
		return rows

	create_custom_fields(
		{
			"CRM Lead": place(FIELDS + LEAD_ONLY_FIELDS, "source"),
			"CRM Deal": place(FIELDS, "source"),
		},
		ignore_validate=True,
	)


def add_to_layouts():
	"""Show the Dezino360 fields in the CRM: on the lead and deal side panels and pages."""
	fields = [f["fieldname"] for f in FIELDS]
	for dt in ("CRM Lead", "CRM Deal"):
		for kind in ("Side Panel", "Data Fields"):
			name = frappe.db.get_value("CRM Fields Layout", {"dt": dt, "type": kind})
			if not name:
				continue
			layout = json.loads(frappe.db.get_value("CRM Fields Layout", name, "layout") or "[]")
			# Some layouts are tabs holding sections, others a plain list of sections.
			sections = layout[0]["sections"] if layout and "sections" in layout[0] else layout
			if any(s.get("name") == LAYOUT_SECTION for s in sections):
				continue  # already there (maybe rearranged by hand; leave it)
			if kind == "Side Panel":
				columns = [{"name": "d360_column_1", "fields": fields}]
			else:
				columns = [
					{"name": "d360_column_1", "fields": fields[:4]},
					{"name": "d360_column_2", "fields": fields[4:]},
				]
			sections.append({"label": "Dezino360", "name": LAYOUT_SECTION, "opened": True, "columns": columns})
			frappe.db.set_value("CRM Fields Layout", name, "layout", json.dumps(layout))


def ensure_assignment_rule():
	"""Every new lead is assigned to the owner (for now). Made once; later edits are kept."""
	owner = owner_email()
	if frappe.db.exists("Assignment Rule", ASSIGNMENT_RULE) or not frappe.db.exists("User", owner):
		return  # a new client site gets it once the agency users exist (hub-new-client)
	frappe.get_doc(
		{
			"doctype": "Assignment Rule",
			"name": ASSIGNMENT_RULE,
			"document_type": "CRM Lead",
			"description": "Every new lead goes to the owner. Set up by the dezino360 app.",
			"priority": 1,
			"rule": "Round Robin",
			"assign_condition": "status == 'New'",
			"users": [{"user": owner}],
			"assignment_days": [
				{"day": d} for d in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
			],
		}
	).insert(ignore_permissions=True)


def once(key, step):
	"""Run a set-up step until it has worked once on this site; later changes are kept."""
	if frappe.db.get_default(key) != "1" and step() is not False:
		frappe.db.set_default(key, "1")


def set_system_time_zone():
	# Owner, 1 Nov 2026: the system stores times in UTC; each person sees their own time zone.
	frappe.db.set_single_value("System Settings", "time_zone", "UTC")


def owner_user_settings():
	"""The owner sees Adelaide time. False until the owner's user exists here."""
	owner = owner_email()
	if not frappe.db.exists("User", owner):
		return False
	frappe.db.set_value("User", owner, "time_zone", "Australia/Adelaide")


def owner_no_assignment_email():
	"""The owner gets no "assigned to you" email for every new lead (the new-lead alert does that
	job). This Frappe goes by the "Email notification types" list, not the older checkbox, so
	"Assignment" comes off that list. False until the owner's user exists here."""
	owner = owner_email()
	if not frappe.db.exists("User", owner):
		return False
	if not frappe.db.exists("Notification Settings", owner):
		frappe.get_doc({"doctype": "Notification Settings", "name": owner}).insert(ignore_permissions=True)
	doc = frappe.get_doc("Notification Settings", owner)
	doc.enable_email_assignment = 0
	doc.set("email_notification_types", [r for r in doc.email_notification_types if r.notification_type != "Assignment"])
	doc.save(ignore_permissions=True)


def set_consent_timestamp(doc, method=None):
	"""CRM Lead / CRM Deal: the time consent was given, kept with the yes/no."""
	if doc.get("consent_to_contact") and not doc.get("consent_timestamp"):
		doc.consent_timestamp = frappe.utils.now_datetime()
	elif not doc.get("consent_to_contact"):
		doc.consent_timestamp = None
