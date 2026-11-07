"""Booking page (phase 5): free times and booking a call, for the page at /book.

Rules per workspace (Dezino360 Settings → Booking page): days, hours and time zone, call length,
gap between calls, minimum notice and how far ahead. Visitors see times in their own time zone;
everything is stored and exchanged in UTC (the system time zone is UTC).

Booking a call: checks the time is still free, puts it in the calendar, creates or updates the
lead (status Meeting Booked, which stops follow-ups), and emails the visitor a confirmation. Until
a sending service is approved (phase 6), that email only reaches the test mail catcher. The
confirmation carries an .ics file; reminders go 24 hours and 1 hour before the call (skipped when the
call was booked closer than that). Every booking passes Cloudflare Turnstile (the bot check) first.

Calendars:
  - "Test calendar (stand-in)": the Hub's own bookings are the calendar (no Google, no Meet link).
  - "Google Calendar": free/busy from the workspace's calendar and an event with a Meet link.
    Needs the Google set-up (OAuth client and refresh token in site_config, owner-approved).
"""

import secrets
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import frappe
import requests
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import cint, get_datetime, now_datetime

SETTINGS = "Dezino360 Settings"
BOOKING = "Dezino360 Booking"
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
UTC = timezone.utc


def settings():
	return frappe.get_cached_doc(SETTINGS)


def rules(s=None):
	s = s or settings()
	return frappe._dict(
		tz=ZoneInfo((s.booking_time_zone or "Australia/Adelaide").strip()),
		days={d.strip()[:3].title() for d in (s.booking_days or "").split(",") if d.strip()},
		start=_time(s.booking_start or "06:00:00"),
		end=_time(s.booking_end or "10:00:00"),
		length=max(cint(s.meeting_minutes) or 15, 5),
		gap=max(cint(s.gap_minutes), 0),
		notice=max(cint(s.notice_hours), 0),
		ahead=max(cint(s.days_ahead) or 30, 1),
	)


def _time(value):
	parts = [int(p) for p in str(value).split(":")[:2]]
	return timedelta(hours=parts[0], minutes=parts[1])


def utc_now():
	return now_datetime().replace(tzinfo=UTC)  # the system time zone is UTC


def free_slots(now=None, r=None, fresh=True):
	"""Every free start time (UTC, aware) from now + notice to the end of the booking window. fresh=False may use the
	Google busy times read in the last 60 seconds (offering times); a booking always checks fresh."""
	r = r or rules()
	now = now or utc_now()
	earliest = now + timedelta(hours=r.notice)
	first_day = now.astimezone(r.tz).date()
	last = now + timedelta(days=r.ahead)
	busy = busy_times(now, last + timedelta(days=1), fresh=fresh)
	gap = timedelta(minutes=r.gap)
	length = timedelta(minutes=r.length)
	out = []
	for i in range(r.ahead + 1):
		day = first_day + timedelta(days=i)
		if DAYS[day.weekday()] not in r.days:
			continue
		midnight = datetime(day.year, day.month, day.day, tzinfo=r.tz)
		start, end = midnight + r.start, midnight + r.end
		t = start
		while t + length <= end:
			s_utc = t.astimezone(UTC)
			if earliest <= s_utc <= last and not any(
				s_utc < b_end + gap and b_start - gap < s_utc + length for b_start, b_end in busy
			):
				out.append(s_utc)
			t += length + gap
	return out


GOOGLE_BUSY_CACHE = "dezino360:google_busy"
GOOGLE_BUSY_SECONDS = 60  # measured 5 Nov 2026: Google's free/busy takes about 0.4 s a request


def busy_times(start, end, fresh=True):
	"""Busy periods (UTC, aware): the Hub's own bookings (always read now), plus the Google calendar if used. With
	fresh=False, Google's answer from the last 60 seconds is reused when it covers the same window."""
	rows = frappe.get_all(
		BOOKING,
		filters={"status": "Booked", "end_utc": [">", start.replace(tzinfo=None)], "start_utc": ["<", end.replace(tzinfo=None)]},
		fields=["start_utc", "end_utc"],
	)
	busy = [(r.start_utc.replace(tzinfo=UTC), r.end_utc.replace(tzinfo=UTC)) for r in rows]
	if settings().calendar_provider == "Google Calendar":
		# expires=True: an expiring key, so a miss isn't remembered for the rest of the request
		cached = None if fresh else frappe.cache.get_value(GOOGLE_BUSY_CACHE, expires=True)
		if cached and cached["start"] <= start and cached["end"] >= end:
			busy += cached["busy"]
		else:
			google = GoogleCalendar().busy(start, end)
			frappe.cache.set_value(GOOGLE_BUSY_CACHE, {"start": start, "end": end + timedelta(hours=1), "busy": google},
				expires_in_sec=GOOGLE_BUSY_SECONDS)
			busy += google
	return busy


# ---- the page's two calls --------------------------------------------------------------------


def _require_on():
	if not cint(settings().booking_enabled):
		frappe.throw(_("Booking is not open on this workspace."), frappe.PermissionError)


@frappe.whitelist(allow_guest=True, methods=["GET"])
@rate_limit(limit=120, seconds=60 * 10)
def slots():
	"""Free start times as UTC ISO strings; the page groups them by the visitor's own date."""
	_require_on()
	r = rules()
	return {"length": r.length, "slots": [s.strftime("%Y-%m-%dT%H:%M:%SZ") for s in free_slots(r=r)]}


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=10, seconds=60 * 60)
def book(start, full_name, email, phone=None, notes=None, time_zone=None, turnstile=None):
	_require_on()
	if not turnstile_ok(turnstile):
		frappe.throw(_("Please complete the quick check and try again."))
	return book_call(start, full_name, email, phone, notes, time_zone, source="Booking", where=_("on the booking page"))


def book_call(start, full_name, email, phone=None, notes=None, time_zone=None, source="Booking", where=""):
	"""The booking itself, for the page above and the AI receptionist (voice.py): same rules, same lock."""
	email = (email or "").strip().lower()
	full_name = " ".join((full_name or "").split())[:140]
	if not full_name or not frappe.utils.validate_email_address(email):
		frappe.throw(_("Please give your name and a valid email address."))
	visitor_tz = (time_zone or "").strip()[:60]
	try:
		ZoneInfo(visitor_tz)
	except Exception:
		visitor_tz = "UTC"
	try:
		start_utc = get_datetime(start.replace("Z", "+00:00")).astimezone(UTC)
	except Exception:
		frappe.throw(_("That time isn't valid."))
	r = rules()
	# One booking at a time: lock the settings rows until this booking is saved.
	frappe.db.sql("select `field` from `tabSingles` where doctype=%s for update", SETTINGS)
	if start_utc not in free_slots(r=r):
		frappe.throw(_("Sorry, that time has just been taken or is no longer available. Please pick another."))
	end_utc = start_utc + timedelta(minutes=r.length)

	frappe.set_user("Administrator")  # the visitor is a guest; the booking is saved by the system
	lead = upsert_lead(full_name, email, phone, source)
	event = calendar().create_event(start_utc, end_utc, full_name, email, phone, notes)
	frappe.cache.delete_value(GOOGLE_BUSY_CACHE)  # the next offer reads Google again
	doc = frappe.get_doc(
		{
			"doctype": BOOKING,
			"start_utc": start_utc.replace(tzinfo=None),
			"end_utc": end_utc.replace(tzinfo=None),
			"status": "Booked",
			"lead": lead,
			"full_name": full_name,
			"email": email,
			"phone": (phone or "").strip()[:40],
			"visitor_time_zone": visitor_tz,
			"calendar_provider": settings().calendar_provider,
			"calendar_event_id": event.get("id"),
			"meeting_link": event.get("meet"),
			"notes": (notes or "").strip()[:1000],
		}
	).insert(ignore_permissions=True)
	frappe.get_doc("CRM Lead", lead).add_comment(
		"Info", _("Booked a call for {0} (UTC) {1} ({2}).").format(start_utc.strftime("%Y-%m-%d %H:%M"), where, doc.name)
	)
	send_confirmation(doc, visitor_tz)
	frappe.db.commit()
	local = start_utc.astimezone(ZoneInfo(visitor_tz))
	return {"booked": True, "when": local.strftime("%A %-d %B, %-I:%M %p") + f" ({visitor_tz})", "meeting_link": event.get("meet"),
		"booking": doc.name, "lead": lead}


def turnstile_ok(token):
	"""Cloudflare Turnstile, checked on the server (siteverify). The secret is only in site_config."""
	secret = frappe.conf.get("dezino360_turnstile_secret")
	if not secret or not token:
		return False
	try:
		r = requests.post(
			"https://challenges.cloudflare.com/turnstile/v0/siteverify",
			data={"secret": secret, "response": token, "remoteip": frappe.local.request_ip or ""},
			timeout=10,
		)
		return bool(r.json().get("success"))
	except Exception:
		return False


def upsert_lead(full_name, email, phone, source="Booking"):
	"""The lead with this email gets status Meeting Booked (follow-ups stop); or a new lead."""
	name = frappe.db.get_value("CRM Lead", {"email": email})
	if name:
		frappe.db.set_value("CRM Lead", name, "status", "Meeting Booked")
		return name
	from dezino360.crm_setup import owner_email

	first, _sep, last = full_name.partition(" ")
	owner = owner_email()
	return (
		frappe.get_doc(
			{
				"doctype": "CRM Lead",
				"first_name": first,
				"last_name": last,
				"email": email,
				"mobile_no": (phone or "").strip()[:40],
				"source": source,
				"status": "Meeting Booked",
				# Every lead goes to the owner. The assignment rule only fires for status New,
				# so a lead that starts as Meeting Booked gets its owner here.
				"lead_owner": owner if frappe.db.exists("User", owner) else None,
			}
		)
		.insert(ignore_permissions=True)
		.name
	)


def send_confirmation(booking, visitor_tz):
	from email.utils import formataddr

	s = settings()
	local = booking.start_utc.replace(tzinfo=UTC).astimezone(ZoneInfo(visitor_tz))
	frappe.sendmail(
		recipients=[booking.email],
		sender=formataddr((s.business_name or "", frappe.conf.get("auto_email_id") or s.alert_to)),
		subject=_("Booked: {0}, {1}").format(s.booking_title, local.strftime("%a %-d %b, %-I:%M %p")),
		template="d360_booking_confirmation",
		args={
			"first_name": booking.full_name.split(" ")[0],
			"title": s.booking_title,
			"when": local.strftime("%A %-d %B %Y, %-I:%M %p"),
			"time_zone": visitor_tz,
			"meeting_link": booking.meeting_link,
			"business_name": s.business_name,
			"postal_address": " ".join((s.postal_address or "").split()),
		},
		reference_doctype=BOOKING,
		reference_name=booking.name,
		add_unsubscribe_link=0,
		attachments=[{"fname": "booking.ics", "fcontent": ics(booking)}],
	)


def _ics_text(value):
	return (value or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def ics(booking):
	"""An add-to-calendar file for the visitor. METHOD:PUBLISH: not an invitation asking for a reply."""
	s = settings()

	def stamp(d):
		return get_datetime(d).strftime("%Y%m%dT%H%M%SZ")

	where = booking.meeting_link or "Video call (the link follows by email)"
	lines = [
		"BEGIN:VCALENDAR",
		"VERSION:2.0",
		"PRODID:-//Dezino360 Hub//Booking//EN",
		"CALSCALE:GREGORIAN",
		"METHOD:PUBLISH",
		"BEGIN:VEVENT",
		f"UID:{booking.name}@{frappe.local.site}",
		f"DTSTAMP:{stamp(now_datetime())}",
		f"DTSTART:{stamp(booking.start_utc)}",
		f"DTEND:{stamp(booking.end_utc)}",
		"SUMMARY:" + _ics_text(f"{s.booking_title} with {s.business_name}"),
		"LOCATION:" + _ics_text(where),
		"DESCRIPTION:" + _ics_text(where),
	]
	if booking.meeting_link:
		lines.append(f"URL:{booking.meeting_link}")
	lines += ["END:VEVENT", "END:VCALENDAR"]
	return "\r\n".join(lines) + "\r\n"


# ---- reminders (every 5 minutes) ------------------------------------------------------------

REMINDERS = (("reminder_24h", timedelta(hours=24)), ("reminder_1h", timedelta(hours=1)))


def send_reminders(now=None):
	"""Each booked call gets a reminder 24 hours and 1 hour before. A call booked closer than that
	gets no such reminder (marked Skipped). `now` lets the tests pretend time has passed."""
	now = get_datetime(now).replace(tzinfo=None) if now else now_datetime().replace(tzinfo=None)
	sent = []
	rows = frappe.get_all(
		BOOKING,
		filters=[["status", "=", "Booked"], ["start_utc", ">", now], ["start_utc", "<=", now + timedelta(hours=24)]],
		fields=["name", "start_utc", "creation", "reminder_24h", "reminder_1h"],
	)
	for b in rows:
		for field, before in REMINDERS:
			if b.get(field) or b.start_utc - now > before:
				continue
			if b.start_utc - b.creation < before:
				frappe.db.set_value(BOOKING, b.name, field, "Skipped", update_modified=False)
				continue
			send_reminder(frappe.get_doc(BOOKING, b.name), field, now)
			frappe.db.set_value(BOOKING, b.name, field, "Sent", update_modified=False)
			sent.append((b.name, field))
	frappe.db.commit()
	return sent


def send_reminder(booking, field, now):
	from email.utils import formataddr

	s = settings()
	tz = ZoneInfo(booking.visitor_time_zone or "UTC")
	local = get_datetime(booking.start_utc).replace(tzinfo=UTC).astimezone(tz)
	today = now.replace(tzinfo=UTC).astimezone(tz).date()
	soon = _("in 1 hour") if field == "reminder_1h" else (_("today") if local.date() == today else _("tomorrow"))
	frappe.sendmail(
		recipients=[booking.email],
		sender=formataddr((s.business_name or "", frappe.conf.get("auto_email_id") or s.alert_to)),
		subject=_("Reminder: {0}, {1}").format(s.booking_title, local.strftime("%a %-d %b, %-I:%M %p")),
		template="d360_booking_reminder",
		args={
			"first_name": booking.full_name.split(" ")[0],
			"title": s.booking_title,
			"soon": soon,
			"when": local.strftime("%A %-d %B %Y, %-I:%M %p"),
			"time_zone": booking.visitor_time_zone or "UTC",
			"meeting_link": booking.meeting_link,
			"business_name": s.business_name,
			"postal_address": " ".join((s.postal_address or "").split()),
		},
		reference_doctype=BOOKING,
		reference_name=booking.name,
		add_unsubscribe_link=0,
		attachments=[{"fname": "booking.ics", "fcontent": ics(booking)}],
	)


# ---- calendars --------------------------------------------------------------------------------


def calendar():
	return GoogleCalendar() if settings().calendar_provider == "Google Calendar" else TestCalendar()


class TestCalendar:
	"""Stand-in until the Google set-up: the Hub's own bookings are the calendar."""

	def create_event(self, start, end, full_name, email, phone, notes):
		return {"id": f"test-{secrets.token_hex(6)}", "meet": None}


class GoogleCalendar:
	"""Google Calendar API with an offline refresh token. Site config keys (never in the repo):
	dezino360_google_client_id, dezino360_google_client_secret, dezino360_google_refresh_token."""

	API = "https://www.googleapis.com/calendar/v3"

	def __init__(self):
		self.calendar_id = (settings().google_calendar_id or "").strip()
		if not self.calendar_id:
			frappe.throw(_("No Google calendar is set for this workspace."))

	TOKEN_CACHE = "dezino360:google_access_token"

	def _token(self):
		"""A Google access token (valid about an hour), kept in the Hub's internal cache until 2 minutes before it expires,
		so each calendar request doesn't first fetch a new one (measured 5 Nov 2026: about 0.2 s each). Never logged."""
		token = frappe.cache.get_value(self.TOKEN_CACHE, expires=True)
		if token:
			return token
		conf = frappe.conf
		r = requests.post(
			"https://oauth2.googleapis.com/token",
			data={
				"client_id": conf.get("dezino360_google_client_id"),
				"client_secret": conf.get("dezino360_google_client_secret"),
				"refresh_token": conf.get("dezino360_google_refresh_token"),
				"grant_type": "refresh_token",
			},
			timeout=15,
		)
		r.raise_for_status()
		answer = r.json()
		frappe.cache.set_value(self.TOKEN_CACHE, answer["access_token"], expires_in_sec=max(60, int(answer.get("expires_in", 3600)) - 120))
		return answer["access_token"]

	def busy(self, start, end):
		r = requests.post(
			f"{self.API}/freeBusy",
			json={"timeMin": start.isoformat(), "timeMax": end.isoformat(), "items": [{"id": self.calendar_id}]},
			headers={"Authorization": f"Bearer {self._token()}"},
			timeout=15,
		)
		r.raise_for_status()
		periods = r.json()["calendars"][self.calendar_id].get("busy", [])
		return [(get_datetime(p["start"]).astimezone(UTC), get_datetime(p["end"]).astimezone(UTC)) for p in periods]

	def create_event(self, start, end, full_name, email, phone, notes):
		s = settings()
		body = {
			"summary": f"{s.booking_title}: {full_name}",
			"description": "\n".join(filter(None, [f"Email: {email}", f"Phone: {phone}" if phone else "", notes or ""])),
			"start": {"dateTime": start.isoformat()},
			"end": {"dateTime": end.isoformat()},
			"conferenceData": {"createRequest": {"requestId": secrets.token_hex(8), "conferenceSolutionKey": {"type": "hangoutsMeet"}}},
		}
		# sendUpdates=none: Google sends no invitation; the Hub's own confirmation does that job.
		r = requests.post(
			f"{self.API}/calendars/{requests.utils.quote(self.calendar_id)}/events",
			params={"conferenceDataVersion": 1, "sendUpdates": "none"},
			json=body,
			headers={"Authorization": f"Bearer {self._token()}"},
			timeout=15,
		)
		r.raise_for_status()
		event = r.json()
		return {"id": event.get("id"), "meet": event.get("hangoutLink")}
