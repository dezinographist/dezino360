"""Lead intake from the dezino360.com Worker (phase 4, shadow mode).

POST https://<site>/api/method/dezino360.intake.lead, JSON body, two headers:
    X-D360-Timestamp: Unix time in seconds when the Worker signed the request
    X-D360-Signature: hex HMAC-SHA256 of "<timestamp>.<raw body>" with the site's shared secret

The request is refused (and logged in "Dezino360 Intake Log") when:
    - the intake secret isn't set on this site, or a header is missing;
    - the signature doesn't match;
    - the timestamp is more than 5 minutes old (or in the future);
    - the same signed request was already received (replay);
    - the body isn't JSON, or has no valid email, or an unknown source.
A lead whose email or phone is already in the CRM is not created again: the new details go on
the existing lead as a note ("duplicate"). Nothing here ever blocks the Worker's Zoho path.

The secret lives only in this site's site_config.json (`dezino360_intake_secret`), made by
`provision.make_intake_secret`; the Worker gets the same value as its secret HUB_INTAKE_SECRET.
"""

import hashlib
import hmac
import json
import re
import time

import frappe
from frappe.rate_limiter import rate_limit
from frappe.utils import cint, now_datetime

MAX_AGE_SECONDS = 300
SOURCES = {"Website Form", "Chat", "Phone", "Booking", "Meta Lead Ad", "Manual"}
LOG = "Dezino360 Intake Log"


class Refused(Exception):
	def __init__(self, reason, status=403):
		super().__init__(reason)
		self.reason, self.status = reason, status


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=120, seconds=60 * 60)
def lead():
	raw = frappe.request.get_data() or b""
	headers = frappe.request.headers
	ts, sig = headers.get("X-D360-Timestamp", ""), headers.get("X-D360-Signature", "")
	try:
		data = verify(raw, ts, sig)
		result, name = save_lead(data)
	except Refused as e:
		log("Refused", e.reason, ts, raw)
		frappe.db.commit()
		frappe.local.response["http_status_code"] = e.status
		return {"ok": False, "error": e.reason}
	log("Accepted" if result == "created" else "Duplicate", result, ts, raw, name)
	frappe.db.commit()
	return {"ok": True, "result": result, "lead": name}


def verify(raw, ts, sig, setting="dezino360_intake_secret", what="intake"):
	"""Also used by the AI receptionist (voice.py), with its own secret."""
	secret = frappe.conf.get(setting)
	if not secret:
		raise Refused(f"{what} is not set up on this site", 503)
	if not ts or not sig:
		raise Refused("missing signature headers", 401)
	expected = hmac.new(secret.encode(), ts.encode() + b"." + raw, hashlib.sha256).hexdigest()
	if not hmac.compare_digest(expected, sig.strip().lower()):
		raise Refused("bad signature", 401)
	age = time.time() - cint(ts)
	if not ts.isdigit() or age > MAX_AGE_SECONDS or age < -60:
		raise Refused(f"timestamp out of range ({int(age)} s old; limit {MAX_AGE_SECONDS} s)", 401)
	# Each signed request is accepted once. Kept a little longer than the 5-minute window.
	seen = f"dezino360:{what}:seen:{sig}"
	if frappe.cache.get_value(seen):
		raise Refused("replay of an earlier request", 409)
	frappe.cache.set_value(seen, 1, expires_in_sec=MAX_AGE_SECONDS * 2)
	try:
		data = json.loads(raw)
	except ValueError:
		raise Refused("body is not JSON", 400)
	if not isinstance(data, dict):
		raise Refused("body is not a JSON object", 400)
	return data


def clean(value, length=140):
	return re.sub(r"\s+", " ", str(value or "")).strip()[:length]


def phone_key(phone):
	"""Digits only, last 10 (so +1 555 010 0199 and (555) 010-0199 match)."""
	digits = re.sub(r"\D", "", phone or "")
	return digits[-10:] if len(digits) >= 7 else ""


def find_duplicate(email, phone):
	if email:
		name = frappe.db.get_value("CRM Lead", {"email": email})
		if name:
			return name
	key = phone_key(phone)
	if key:
		Lead = frappe.qb.DocType("CRM Lead")
		for row in (
			frappe.qb.from_(Lead)
			.select(Lead.name, Lead.mobile_no, Lead.phone)
			.where((Lead.mobile_no.like(f"%{key[-4:]}")) | (Lead.phone.like(f"%{key[-4:]}")))
			.run(as_dict=True)
		):
			if key in (phone_key(row.mobile_no), phone_key(row.phone)):
				return row.name
	return None


def save_lead(data):
	email = clean(data.get("email")).lower()
	if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", email):
		raise Refused("no valid email", 400)
	source = clean(data.get("source"), 60)
	if source not in SOURCES:
		raise Refused(f"unknown source {source!r}", 400)
	phone = clean(data.get("phone"), 40)
	first, last = clean(data.get("first_name")), clean(data.get("last_name"))
	if not first and data.get("name"):
		first, _, last = clean(data.get("name")).partition(" ")
	fields = {
		"first_name": first or email.split("@")[0],
		"last_name": last,
		"email": email,
		"mobile_no": phone,
		"organization": clean(data.get("company")),
		"source": source,
		"consent_to_contact": 1 if data.get("consent") is True else 0,
		"plan_of_interest": clean(data.get("plan")) if data.get("plan") in ("Starter", "Master", "Blaster", "Custom") else None,
		"market": data.get("market") if data.get("market") in ("Australia", "North America", "India") else None,
		"first_touch_page": clean(data.get("first_touch_page"), 500),
		"utm_source": clean(data.get("utm_source")),
		"utm_medium": clean(data.get("utm_medium")),
		"utm_campaign": clean(data.get("utm_campaign")),
	}
	message = str(data.get("message") or "").strip()[:4000]
	frappe.set_user("Administrator")  # the signature proved the sender; save as the system

	existing = find_duplicate(email, phone)
	if existing:
		lines = [f"Repeat inquiry from the website ({source})."]
		lines += [f"{k}: {v}" for k, v in fields.items() if v and k not in ("source",)]
		if message:
			lines.append(f"Message: {message}")
		add_note(existing, "Repeat inquiry", "<br>".join(frappe.utils.escape_html(l) for l in lines))
		return "duplicate", existing

	doc = frappe.get_doc({"doctype": "CRM Lead", **{k: v for k, v in fields.items() if v not in (None, "")}})
	if data.get("booked") is True:
		doc.status = "Meeting Booked"
	if fields["consent_to_contact"] and data.get("consent_at"):
		try:
			doc.consent_timestamp = frappe.utils.get_datetime(data["consent_at"]).replace(tzinfo=None)
		except Exception:
			pass  # the time it arrived is filled in instead
	doc.insert(ignore_permissions=True)
	if message:
		add_note(doc.name, "Message from the website", frappe.utils.escape_html(message).replace("\n", "<br>"))
	return "created", doc.name


def add_note(lead, title, html):
	frappe.get_doc(
		{
			"doctype": "FCRM Note",
			"title": title,
			"content": html,
			"reference_doctype": "CRM Lead",
			"reference_docname": lead,
		}
	).insert(ignore_permissions=True)


def log(result, reason, ts, raw, lead=None):
	"""Every request is logged; a refusal never stores the body, only a fingerprint."""
	frappe.get_doc(
		{
			"doctype": LOG,
			"result": result,
			"reason": reason[:140],
			"lead": lead,
			"remote_ip": frappe.local.request_ip,
			"signed_at": ts[:20] if ts else None,
			"body_sha256": hashlib.sha256(raw).hexdigest()[:16] if raw else None,
			"received_at": now_datetime(),
		}
	).insert(ignore_permissions=True)
