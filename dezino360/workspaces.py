"""The front door: which workspaces a user may open, and the hand-off into one.

The agency site (app.dezino360.com) is the login provider. Each client workspace
(<client>.dezino360.com) is its own Frappe site that logs users in through it.
"""

from urllib.parse import urlencode

import frappe
from frappe import _

from dezino360.install import AGENCY_ROLE

PROVIDER = "dezino360"
DEFAULT_LANDING = "/crm"


def site_role():
	return frappe.conf.get("dezino360_site_role")


def is_agency_site():
	return site_role() == "agency"


def has_app_permission():
	return is_agency_site() and AGENCY_ROLE in frappe.get_roles()


def get_workspaces_for_user(user=None):
	"""Active workspaces this user may open. The agency sees all of them."""
	user = user or frappe.session.user
	if user == "Guest" or not is_agency_site():
		return []
	roles = set(frappe.get_roles(user))
	rows = frappe.get_all(
		"Dezino Workspace",
		filters={"status": "Active"},
		fields=["name", "client_name", "site", "role"],
		order_by="client_name asc",
	)
	if AGENCY_ROLE in roles:
		return rows
	return [row for row in rows if row.role in roles]


def open_url(site):
	return f"https://{site}/api/method/dezino360.workspaces.start_login?" + urlencode(
		{"redirect_to": DEFAULT_LANDING}
	)


@frappe.whitelist(allow_guest=True, methods=["GET"])
def start_login(redirect_to=DEFAULT_LANDING):
	"""On a client workspace: send the visitor to the Dezino360 login, then back here."""
	if site_role() != "client" or not frappe.db.get_value("Social Login Key", PROVIDER, "enable_social_login"):
		frappe.throw(_("Dezino360 login is not set up on this workspace."), frappe.PermissionError)

	if not redirect_to or not redirect_to.startswith("/") or redirect_to.startswith("//"):
		redirect_to = DEFAULT_LANDING

	if frappe.session.user == "Guest":
		from frappe.utils.oauth import get_oauth2_authorize_url

		location = get_oauth2_authorize_url(PROVIDER, redirect_to)
	else:
		location = redirect_to

	frappe.local.response["type"] = "redirect"
	frappe.local.response["location"] = location
