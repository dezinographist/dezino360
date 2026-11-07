"""AI receptionist (phase 7): signed calls from the voice program (`voice/` in the repo) to this workspace.

POST https://<site>/api/method/dezino360.voice.<name>, JSON body, signed like the lead intake
(X-D360-Timestamp / X-D360-Signature: hex HMAC-SHA256 of "<timestamp>.<raw body>"), but with this
site's own receptionist secret `dezino360_voice_secret` (made by hub-voice-link; never shown to anyone).

    config    what the receptionist needs at the start of a call: business name, call name, on/off
    slots     the next free times from the booking rules (phase 5), shown in the caller's time zone; the times
              offered are remembered for that call (call_id)
    book      books one of them: calendar, lead (Meeting Booked), confirmation email. Only a time that slots
              offered on the same call, still free; a valid email; name and time zone required. Anything
              else is refused with a clear error (HTTP 400 or 422) and nothing is booked.
    call_end  the lead, the call record (summary, transcript, recording file name), the minutes used

slots and book work only while "Receptionist on" is ticked; call_end always records the call.
Email permission: only a clear "yes" from the caller (email_consent "Yes") turns the lead's consent on;
it never turns consent off.
"""

import re
from datetime import datetime
from zoneinfo import ZoneInfo

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import cint, flt, get_datetime, now_datetime

from dezino360 import booking
from dezino360.intake import Refused, add_note, clean, find_duplicate, phone_key, verify

SETTINGS = "Dezino360 Settings"
CALL = "Dezino360 Call"
SOURCE = "AI Receptionist"


def _signed(handler):
	"""Checks the signature, runs the handler as the system, and answers {"ok": ...} JSON."""
	raw = frappe.request.get_data() or b""
	h = frappe.request.headers
	try:
		data = verify(raw, h.get("X-D360-Timestamp", ""), h.get("X-D360-Signature", ""), "dezino360_voice_secret", "receptionist")
		frappe.set_user("Administrator")  # the signature proved the sender
		return {"ok": True, **handler(data)}
	except Refused as e:
		frappe.local.response["http_status_code"] = e.status
		return {"ok": False, "error": e.reason}
	except frappe.ValidationError as e:
		frappe.db.rollback()
		frappe.clear_messages()
		frappe.local.response["http_status_code"] = 422
		return {"ok": False, "error": str(e)}


def _require_on():
	if not cint(frappe.get_cached_doc(SETTINGS).voice_enabled):
		raise Refused("the receptionist is switched off on this workspace", 403)


def _zone(name, fallback="UTC"):
	try:
		return ZoneInfo((name or "").strip()), (name or "").strip()
	except Exception:
		return ZoneInfo(fallback), fallback


def _required(data, *fields):
	missing = [f for f in fields if not str(data.get(f) or "").strip()]
	if missing:
		raise Refused(f"missing: {', '.join(missing)}", 400)


def _caller_zone(name):
	"""The caller's time zone, which must be a real IANA name (e.g. America/Chicago)."""
	try:
		return ZoneInfo(str(name or "").strip()), str(name).strip()
	except Exception:
		raise Refused(f"unknown time zone {str(name)[:40]!r}: use an IANA name such as America/Chicago", 400)


OFFERED_TTL = 3 * 60 * 60  # the times offered on a call are remembered for 3 hours


def _offered_key(call_id):
	return f"dezino360:voice:offered:{call_id}"


def offered(call_id):
	"""Every start time offered on this call, in the order offered (a list per slots request)."""
	return frappe.cache.get_value(_offered_key(call_id)) or []


def _remember_offered(call_id, starts):
	frappe.cache.set_value(_offered_key(call_id), [*offered(call_id), starts], expires_in_sec=OFFERED_TTL)


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=600, seconds=60 * 60)
def config():
	def run(data):
		s = frappe.get_cached_doc(SETTINGS)
		return {
			"enabled": bool(cint(s.voice_enabled)),
			"business_name": s.business_name or "",
			"call_name": s.booking_title or "",
			"call_minutes": cint(s.meeting_minutes) or 15,
			"booking_time_zone": s.booking_time_zone or "",
			"website": s.website or "",
			"agent_name": (s.voice_agent_name or "").strip() or "Daisy",
			"language": s.voice_language or "English (US)",
			"emergency_number": s.voice_emergency_number or "911",
			"facts": s.voice_facts or "",
		}

	return _signed(run)


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=600, seconds=60 * 60)
def slots():
	"""{"call_id", "time_zone": caller's IANA zone, "from_date": "YYYY-MM-DD" (optional, caller's date), "limit": 6,
	"prefetch": true to only warm the calendar cache (nothing is offered, so nothing from it can be booked)}"""

	def run(data):
		_require_on()
		_required(data, "call_id", "time_zone")
		tz, tz_name = _caller_zone(data.get("time_zone"))
		limit = min(max(cint(data.get("limit")) or 6, 1), 12)
		from_date = None
		if data.get("from_date"):
			try:
				from_date = datetime.strptime(str(data["from_date"])[:10], "%Y-%m-%d").date()
			except ValueError:
				raise Refused("from_date must be YYYY-MM-DD", 400)
		out = []
		for s in booking.free_slots(fresh=False):
			local = s.astimezone(tz)
			if from_date and local.date() < from_date:
				continue
			out.append({"start": s.strftime("%Y-%m-%dT%H:%M:%SZ"), "say": local.strftime("%A %-d %B at %-I:%M %p")})
			if len(out) >= limit:
				break
		if data.get("prefetch") is True:
			return {"prefetched": len(out)}
		_remember_offered(clean(data["call_id"], 140), [s["start"] for s in out])
		return {"time_zone": tz_name, "slots": out, "call_minutes": booking.rules().length}

	return _signed(run)


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=60, seconds=60 * 60)
def book():
	"""{"call_id", "start": a "start" that slots offered on this call, "full_name", "email", "phone", "time_zone", "notes"}"""

	def run(data):
		_require_on()
		_required(data, "call_id", "start", "full_name", "email", "time_zone")
		_tz, tz_name = _caller_zone(data.get("time_zone"))
		email = clean(data.get("email")).lower()
		if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", email) or not frappe.utils.validate_email_address(email):
			raise Refused(f"not a valid email address: {email[:80]!r}; check the spelling with the caller", 422)
		start = str(data.get("start")).strip()
		if start not in {s for batch in offered(clean(data["call_id"], 140)) for s in batch}:
			raise Refused("that time wasn't offered on this call; offer times from find_free_times", 422)
		r = booking.book_call(
			start,
			clean(data.get("full_name")),
			email,
			clean(data.get("phone"), 40),
			str(data.get("notes") or "")[:1000],
			tz_name,
			source=SOURCE,
			where=_("by phone, with the AI receptionist"),
		)
		return {"when": r["when"], "booking": r["booking"], "lead": r["lead"]}

	return _signed(run)


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=600, seconds=60 * 60)
def call_end():
	"""{"call_id", "from_number", "to_number", "started_at" (ISO), "duration_seconds",
	"caller": {"first_name", "last_name", "email", "company", "need"}, "email_consent": "Yes"|"Not asked",
	"summary", "transcript", "recording_file", "booking": a booking's name}"""

	def run(data):
		call_id = clean(data.get("call_id"), 140)
		if not call_id:
			raise Refused("call_id is required", 400)
		existing = frappe.db.get_value(CALL, {"call_id": call_id})
		if existing:
			return {"call": existing, "result": "already recorded"}
		started = _utc_naive(data.get("started_at"))
		seconds = max(cint(data.get("duration_seconds")), 0)
		consent = "Yes" if data.get("email_consent") == "Yes" else "Not asked"
		booking_name = data.get("booking") if data.get("booking") and frappe.db.exists("Dezino360 Booking", data.get("booking")) else None
		summary = str(data.get("summary") or "").strip()[:4000]
		# Only a real number goes on the lead ("anonymous", a hidden caller ID or a browser test don't).
		number = clean(data.get("from_number"), 40)
		phone = number if phone_key(number) else ""
		lead = save_caller(data.get("caller") or {}, phone, consent, summary, seconds, booking_name)
		doc = frappe.get_doc(
			{
				"doctype": CALL,
				"call_id": call_id,
				"started_at": started,
				"duration_seconds": seconds,
				"minutes": round(seconds / 60, 1),
				"from_number": number or _("not given"),
				"to_number": clean(data.get("to_number"), 40),
				"lead": lead,
				"booking": booking_name,
				"email_consent": consent,
				"summary": summary,
				"transcript": str(data.get("transcript") or "")[:200000],
				"recording_file": clean(data.get("recording_file"), 200),
			}
		).insert(ignore_permissions=True)
		used = count_minutes(seconds / 60, started)
		frappe.db.commit()
		return {"call": doc.name, "lead": lead, "minutes_used": used}

	return _signed(run)


def _utc_naive(value):
	try:
		d = get_datetime(str(value).replace("Z", "+00:00"))
		return d.astimezone(booking.UTC).replace(tzinfo=None) if d.tzinfo else d
	except Exception:
		return now_datetime()


def save_caller(caller, phone, consent, summary, seconds, booking_name):
	"""The caller's lead: the booking's lead, or the one with this email or phone, or a new one."""
	email = clean(caller.get("email")).lower()
	if email and not frappe.utils.validate_email_address(email):
		email = ""
	lead = frappe.db.get_value("Dezino360 Booking", booking_name, "lead") if booking_name else None
	lead = lead or find_duplicate(email, phone)
	note = _("Call with the AI receptionist ({0} min).").format(round(seconds / 60, 1))
	if summary:
		note += "\n" + summary
	if lead:
		updates = {}
		if email and not frappe.db.get_value("CRM Lead", lead, "email"):
			updates["email"] = email
		if consent == "Yes" and not frappe.db.get_value("CRM Lead", lead, "consent_to_contact"):
			updates.update({"consent_to_contact": 1, "consent_timestamp": now_datetime()})
		if updates:
			frappe.db.set_value("CRM Lead", lead, updates)
	elif email or phone or clean(caller.get("first_name")):
		first = clean(caller.get("first_name")) or _("Caller")
		lead = (
			frappe.get_doc(
				{
					"doctype": "CRM Lead",
					"first_name": first,
					"last_name": clean(caller.get("last_name")),
					"email": email or None,
					"mobile_no": phone,
					"organization": clean(caller.get("company")) or None,
					"source": SOURCE,
					"consent_to_contact": 1 if consent == "Yes" else 0,
				}
			)
			.insert(ignore_permissions=True)
			.name
		)
	if lead:
		need = clean(caller.get("need"), 500)
		html = frappe.utils.escape_html(note).replace("\n", "<br>")
		if need:
			html += "<br>" + frappe.utils.escape_html(_("What they need: {0}").format(need))
		html += "<br>" + frappe.utils.escape_html(_("Email permission: {0}").format(consent))
		add_note(lead, _("Phone call (AI receptionist)"), html)
	return lead


# ---- minutes per month, alerts at 80% and 100% ------------------------------------------------


def count_minutes(minutes, started):
	"""Adds a call's minutes to this month's count (the month in the booking time zone) and sends the
	80% / 100% alert once each per month. Returns the minutes used this month."""
	# One call at a time: lock the settings rows (as booking does) so two calls ending together both count.
	frappe.db.sql("select `field` from `tabSingles` where doctype=%s for update", SETTINGS)
	s = frappe.get_doc(SETTINGS)
	tz, _name = _zone(s.booking_time_zone, "Australia/Adelaide")
	month = started.replace(tzinfo=booking.UTC).astimezone(tz).strftime("%Y-%m")
	if s.voice_month != month:
		values = {"voice_month": month, "voice_minutes_used": 0, "voice_alert_80": 0, "voice_alert_100": 0}
	else:
		values = {}
	used = round(flt(values.get("voice_minutes_used", s.voice_minutes_used)) + minutes, 1)
	values["voice_minutes_used"] = used
	plan = cint(s.voice_plan_minutes)
	if plan > 0:
		for level, field in ((100, "voice_alert_100"), (80, "voice_alert_80")):
			already = values.get(field, cint(s.get(field)))
			if used >= plan * level / 100 and not already:
				values[field] = 1
				if level == 100:
					values["voice_alert_80"] = 1  # never a late 80% alert after the 100% one
				send_minutes_alert(s, level, used, plan, month)
				break
	frappe.db.set_single_value(SETTINGS, values)
	return used


def send_minutes_alert(s, level, used, plan, month):
	if not s.alert_to:
		return
	frappe.sendmail(
		recipients=[s.alert_to],
		subject=_("Receptionist minutes: {0}% used ({1} of {2}) on {3}").format(level, used, plan, frappe.local.site),
		template="d360_voice_minutes",
		args={
			"site": frappe.local.site,
			"business_name": s.business_name or frappe.local.site,
			"level": level,
			"used": used,
			"plan": plan,
			"month": month,
			"rate": flt(s.voice_overage_rate),
		},
		add_unsubscribe_link=0,
	)
