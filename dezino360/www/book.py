import frappe
from frappe.utils import cint

no_cache = 1


def get_context(context):
	s = frappe.get_cached_doc("Dezino360 Settings")
	context.no_breadcrumbs = 1
	context.open = bool(cint(s.booking_enabled))
	context.call_title = s.booking_title or "Book a call"
	context.title = f"{context.call_title} | {s.business_name}" if s.business_name else context.call_title
	context.intro = s.booking_intro
	context.business_name = s.business_name
	context.privacy_url = s.privacy_url
	context.minutes = cint(s.meeting_minutes) or 15
	context.logo = s.booking_logo
	context.turnstile_site_key = s.turnstile_site_key
