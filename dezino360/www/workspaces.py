import frappe

from dezino360.install import AGENCY_ROLE
from dezino360.workspaces import get_workspaces_for_user, is_agency_site, open_url

no_cache = 1


def get_context(context):
	if not is_agency_site():
		frappe.local.flags.redirect_location = "/crm"
		raise frappe.Redirect

	if frappe.session.user == "Guest":
		frappe.local.flags.redirect_location = "/login?redirect-to=/workspaces"
		raise frappe.Redirect

	is_agency = AGENCY_ROLE in frappe.get_roles()
	rows = get_workspaces_for_user()

	# A client with one workspace goes straight in; only the agency sees the switcher.
	if not is_agency and len(rows) == 1:
		frappe.local.flags.redirect_location = open_url(rows[0].site)
		raise frappe.Redirect

	context.no_breadcrumbs = 1
	context.title = "Workspaces"
	context.is_agency = is_agency
	context.workspaces = [
		{"client_name": r.client_name, "site": r.site, "url": open_url(r.site)} for r in rows
	]
