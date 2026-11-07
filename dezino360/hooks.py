from dezino360 import __version__

app_name = "dezino360"
app_title = "Dezino360"
app_publisher = "Dezino Graphist"
app_description = "Dezino360 Hub: the front door, client workspaces and branding on Frappe CRM"
app_email = "grow@dezino360.com"
app_license = "agpl-3.0"
app_logo_url = "/assets/dezino360/images/logo.svg"

required_apps = ["crm"]

# Shown on the apps screen of the agency site only (app.dezino360.com).
add_to_apps_screen = [
	{
		"name": "dezino360",
		"logo": "/assets/dezino360/images/logo.svg",
		"title": "Workspaces",
		"route": "/workspaces",
		"has_permission": "dezino360.workspaces.has_app_permission",
	}
]

# Brand: website pages (login, switcher) and the desk. The ?v= changes with each release so
# Cloudflare (which keeps these files for 4 hours) fetches the new styles straight away.
web_include_css = f"/assets/dezino360/css/dezino360-web.css?v={__version__}"
app_include_css = f"/assets/dezino360/css/dezino360-desk.css?v={__version__}"

# Client users on the agency site are website users; they land on the switcher.
role_home_page = {"Dezino Workspace Member": "workspaces"}

after_install = "dezino360.install.after_install"
after_migrate = "dezino360.install.after_migrate"

# The login page is titled "Login | Dezino360".
update_website_context = "dezino360.website.update_website_context"

doc_events = {
	"CRM Lead": {
		"validate": "dezino360.crm_setup.set_consent_timestamp",
		"after_insert": "dezino360.alerts.new_lead_alert",
	},
	"CRM Deal": {"validate": "dezino360.crm_setup.set_consent_timestamp"},
	"Email Account": {"validate": "dezino360.alerts.guard_email_account"},
}

scheduler_events = {
	"hourly": ["dezino360.alerts.send_follow_ups"],
	# Billing (phase 8, test only): Overdue for 14 days -> Paused; unpaid at day 30 -> cancelled in Stripe.
	"daily": ["dezino360.billing.daily_check"],
	# Booking reminders, 24 hours and 1 hour before each call.
	"cron": {"*/5 * * * *": ["dezino360.booking.send_reminders"]},
}
