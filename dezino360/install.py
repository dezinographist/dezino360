"""Settings every Dezino360 site gets: no error details for visitors, no sign-ups, no
email-link login, no usage reports to Frappe, the brand, and the Dezino360 CRM set-up.

Runs after install and after every migrate, so a setting changed by hand is put back.
"""

import frappe

LOGO = "/assets/dezino360/images/logo.svg"
AGENCY_ROLE = "Dezino Agency"
MEMBER_ROLE = "Dezino Workspace Member"


def after_install():
	apply_site_settings()


def after_migrate():
	apply_site_settings()


def apply_site_settings():
	ensure_role(AGENCY_ROLE, desk_access=1)
	ensure_role(MEMBER_ROLE, desk_access=0)
	harden()
	brand()
	lead_email_defaults()
	from dezino360 import crm_setup

	crm_setup.apply()
	frappe.db.commit()


def ensure_role(role, desk_access):
	if not frappe.db.exists("Role", role):
		frappe.get_doc({"doctype": "Role", "role_name": role, "desk_access": desk_access}).insert(
			ignore_permissions=True
		)


def harden():
	# Visitors never see stack traces; admins still find them under Error Log.
	frappe.db.set_single_value("System Settings", "allow_error_traceback", 0)
	frappe.db.set_single_value("Website Settings", "disable_signup", 1)
	# Owner, 1 Nov 2026: no "Login with Email Link". Email is test-only for now, and on a
	# workspace the link would skip the front door.
	frappe.db.set_single_value("System Settings", "login_with_email_link", 0)
	frappe.db.set_single_value("System Settings", "enable_telemetry", 0)


def brand():
	frappe.db.set_single_value("System Settings", "app_name", "Dezino360")
	frappe.db.set_single_value(
		"Website Settings",
		{
			"app_name": "Dezino360",
			"app_logo": LOGO,
			"splash_image": LOGO,
			"brand_html": f'<img src="{LOGO}" alt="Dezino360" class="d360-brand-logo">',
			"show_footer_on_login": 1,
		},
	)
	frappe.db.set_single_value("Navbar Settings", "app_logo", LOGO)
	if frappe.db.exists("DocType", "FCRM Settings"):
		frappe.db.set_single_value("FCRM Settings", {"brand_name": "Dezino360", "brand_logo": LOGO})


def lead_email_defaults():
	"""Fill in empty Dezino360 Settings once. The on/off switches start off and are never changed
	here. Only the agency site gets Dezino360's own details; a client workspace gets its business
	name from hub-new-client and the rest from its own settings page."""
	from dezino360.crm_setup import owner_email
	from dezino360.workspaces import is_agency_site

	defaults = {
		"alert_to": owner_email(),
		# Booking rules (owner-confirmed, 1 Nov 2026); each workspace can change its own.
		"calendar_provider": "Test calendar (stand-in)",
		"booking_time_zone": "Australia/Adelaide",
		"booking_days": "Mon,Tue,Wed,Thu,Fri",
		"booking_start": "06:00:00",
		"booking_end": "10:00:00",
		"meeting_minutes": 15,
		"notice_hours": 4,
		"days_ahead": 30,
		# AI receptionist (phase 7): each workspace sets its own; the agency's line is US.
		"voice_language": "English (US)",
		"voice_emergency_number": "911",
		"voice_plan_minutes": 150,
		"voice_overage_rate": 0.35,
	}
	if is_agency_site():
		defaults.update(
			{
				"business_name": "Dezino360",
				"website": "dezino360.com",
				"sender_name": "The Dezino360 team",
				"booking_url": "https://dezino360.zohobookings.com.au/dezino360",
			}
		)
		# The live Zoho Bookings service, read from its public page (1 Nov 2026).
		defaults.update(
			{
				"booking_title": "15-min Discovery Call",
				"privacy_url": "https://dezino360.com/privacy",
				"booking_logo": LOGO,
				"booking_intro": "A quick 15-minute call to hear about your business and show you how Dezino360 "
				"can bring in and manage your leads. No pressure, no obligation.",
			}
		)
		from dezino360.crm_setup import DEFAULT_FOLLOW_UPS
		from dezino360.voice_facts import AGENCY_FACTS

		defaults["voice_facts"] = AGENCY_FACTS

		for n, text in DEFAULT_FOLLOW_UPS.items():
			defaults.update({f"fu{n}_{part}": value for part, value in text.items()})
	for field, value in defaults.items():
		if not frappe.db.get_single_value("Dezino360 Settings", field):
			frappe.db.set_single_value("Dezino360 Settings", field, value)
