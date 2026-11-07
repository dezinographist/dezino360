import frappe
from frappe import _
from frappe.model.document import Document

# Everything a follow-up email shows; none of it is typed into the email itself.
FOLLOW_UP_FIELDS = {
	"business_name": "Business name",
	"website": "Website",
	"sender_name": "Signature",
	"booking_url": "Booking link",
	"postal_address": "Postal address",
}
# Each workspace writes its own three emails (owner, 1 Nov 2026); only the agency site starts
# with texts filled in (the live Worker's, in crm_setup.DEFAULT_FOLLOW_UPS).
for _n in (1, 2, 3):
	FOLLOW_UP_FIELDS.update(
		{f"fu{_n}_subject": f"Follow-up {_n} subject", f"fu{_n}_body": f"Follow-up {_n} body", f"fu{_n}_button": f"Follow-up {_n} button text"}
	)


def is_filled(value):
	"""True if a setting has visible text (a text editor's empty value is just HTML tags)."""
	from frappe.utils import strip_html

	return bool(strip_html(value or "").strip())


class Dezino360Settings(Document):
	def validate(self):
		if self.follow_ups:
			missing = [label for field, label in FOLLOW_UP_FIELDS.items() if not is_filled(self.get(field))]
			if missing:
				frappe.throw(
					_("Follow-ups can't be switched on until these are filled in: {0}").format(", ".join(missing))
				)
		if self.booking_enabled:
			missing = [label for field, label in {"booking_title": "Call name", "business_name": "Business name",
				"booking_time_zone": "Time zone", "booking_days": "Days", "privacy_url": "Privacy policy link"}.items() if not is_filled(self.get(field))]
			if self.calendar_provider == "Google Calendar" and not is_filled(self.google_calendar_id):
				missing.append("Google calendar ID")
			# Owner, 1 Nov 2026: no booking page without the bot check.
			if not is_filled(self.turnstile_site_key):
				missing.append("Turnstile site key")
			elif not frappe.conf.get("dezino360_turnstile_secret"):
				missing.append("Turnstile secret (run hub-site-secret on the server)")
			if missing:
				frappe.throw(_("The booking page can't be switched on until these are filled in: {0}").format(", ".join(missing)))
			self.check_time_zone()
		if self.voice_enabled:
			missing = [label for field, label in {"booking_title": "Call name", "business_name": "Business name",
				"booking_time_zone": "Time zone", "booking_days": "Days", "voice_facts": "Receptionist facts",
				"voice_emergency_number": "Emergency number"}.items() if not is_filled(self.get(field))]
			if not frappe.conf.get("dezino360_voice_secret"):
				missing.append("the receptionist link (run hub-voice-link on the server)")
			if missing:
				frappe.throw(_("The receptionist can't be switched on until these are filled in: {0}").format(", ".join(missing)))
			self.check_time_zone()

	def check_time_zone(self):
		try:
			from zoneinfo import ZoneInfo

			ZoneInfo(self.booking_time_zone.strip())
		except Exception:
			frappe.throw(_("Time zone {0} isn't a known time zone (e.g. Australia/Adelaide).").format(self.booking_time_zone))
