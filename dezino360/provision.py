"""Set-up steps run by the server scripts (hub-front-door, hub-new-client) through
`bench --site <site> execute dezino360.provision.<function>`.

Secrets never pass through arguments or output: the workspace login secret goes from
the agency site to the client site in a file only the bench user can read, which the
client site deletes after reading, and passwords arrive on standard input.
"""

import json
import os
import re
import secrets
import sys

import frappe
from frappe.utils import get_bench_path

from dezino360.install import AGENCY_ROLE, MEMBER_ROLE, ensure_role
from dezino360.workspaces import PROVIDER, site_role

DOMAIN = "dezino360.com"
RESERVED = {"www", "app", "mail", "send", "in", "au", "us", "api", "admin", "hub", "status"}
CALLBACK = f"/api/method/frappe.integrations.oauth2_logins.custom/{PROVIDER}"
HANDOFF_FILE = "dezino360-sso.json"


def front_door():
	return frappe.conf.get("dezino360_front_door") or f"app.{DOMAIN}"


def internal_front_door():
	# Server-to-server calls stay inside the Docker network (no trip out through Cloudflare).
	return frappe.conf.get("dezino360_internal_url") or f"http://{front_door()}:8080"


def _require(role):
	if site_role() != role:
		frappe.throw(f"This step runs on the {role} site; this site is '{site_role()}'.")


def _check_workspace(workspace):
	if not re.fullmatch(r"[a-z][a-z0-9-]{0,30}[a-z0-9]", workspace or "") or workspace in RESERVED:
		frappe.throw(f"'{workspace}' can't be a workspace name.")


def workspace_role(workspace):
	return f"Workspace {workspace}"


# ---- agency site (app.dezino360.com) ----------------------------------------------


def setup_agency():
	_require("agency")
	frappe.db.set_single_value("System Settings", "default_app", "dezino360")
	frappe.db.set_single_value("Website Settings", "home_page", "workspaces")
	frappe.db.commit()


def register_workspace(workspace, client_name):
	"""Create or update the workspace record, its role and its login client."""
	_require("agency")
	_check_workspace(workspace)
	site = f"{workspace}.{DOMAIN}"
	if not os.path.isdir(os.path.join(get_bench_path(), "sites", site)):
		frappe.throw(f"Site {site} does not exist yet.")

	role = workspace_role(workspace)
	ensure_role(role, desk_access=0)

	existing = frappe.db.get_value("Dezino Workspace", workspace, "oauth_client")
	client = frappe.get_doc("OAuth Client", existing) if existing else frappe.new_doc("OAuth Client")
	redirect = f"https://{site}{CALLBACK}"
	client.update(
		{
			"app_name": f"Dezino360 workspace: {client_name}",
			"redirect_uris": redirect,
			"default_redirect_uri": redirect,
			"scopes": "openid",
			"skip_authorization": 1,
			"grant_type": "Authorization Code",
			"response_type": "Code",
			"client_secret": secrets.token_urlsafe(32),
		}
	)
	client.set("allowed_roles", [{"role": role}, {"role": AGENCY_ROLE}])
	client.save(ignore_permissions=True)

	ws = (
		frappe.get_doc("Dezino Workspace", workspace)
		if frappe.db.exists("Dezino Workspace", workspace)
		else frappe.new_doc("Dezino Workspace")
	)
	ws.update(
		{
			"workspace": workspace,
			"client_name": client_name,
			"site": site,
			"status": "Active",
			"role": role,
			"oauth_client": client.name,
		}
	)
	ws.save(ignore_permissions=True)

	_write_handoff(
		site,
		{
			"client_id": client.client_id,
			"client_secret": client.client_secret,
			"base_url": f"https://{front_door()}",
			"authorize_url": f"https://{front_door()}/api/method/frappe.integrations.oauth2.authorize",
			"access_token_url": f"{internal_front_door()}/api/method/frappe.integrations.oauth2.get_token",
			"api_endpoint": f"{internal_front_door()}/api/method/frappe.integrations.oauth2.openid_profile",
		},
	)
	frappe.db.commit()


def _write_handoff(site, data):
	folder = os.path.join(get_bench_path(), "sites", site, "private")
	os.makedirs(folder, exist_ok=True)
	path = os.path.join(folder, HANDOFF_FILE)
	fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
	with os.fdopen(fd, "w") as f:
		json.dump(data, f)


def list_agency_users():
	_require("agency")
	users = frappe.get_all(
		"Has Role",
		filters={"role": AGENCY_ROLE, "parenttype": "User", "parent": ["not in", ["Administrator", "Guest"]]},
		pluck="parent",
		distinct=True,
	)
	rows = frappe.get_all(
		"User", filters={"name": ["in", users or [""]], "enabled": 1}, fields=["email", "first_name", "last_name"]
	)
	print(json.dumps(rows))


def create_agency_user(email, first_name, last_name=""):
	_require("agency")
	_ensure_user(email, first_name, last_name, [AGENCY_ROLE, "System Manager", "Sales Manager"], "System User")
	frappe.db.commit()


def add_workspace_member(email, first_name, workspace, last_name=""):
	"""A client user on the agency site: a website user who can open one workspace."""
	_require("agency")
	_check_workspace(workspace)
	if not frappe.db.exists("Dezino Workspace", workspace):
		frappe.throw(f"Workspace {workspace} is not registered.")
	created = not frappe.db.exists("User", email.strip().lower())
	_ensure_user(email, first_name, last_name, [MEMBER_ROLE, workspace_role(workspace)], "Website User")
	frappe.db.commit()
	print(json.dumps({"created": created}))


# ---- client site (<client>.dezino360.com) ------------------------------------------


def configure_client_site():
	"""Turn on "Log in with Dezino360" using the hand-off file, then delete the file."""
	_require("client")
	path = frappe.get_site_path("private", HANDOFF_FILE)
	if not os.path.exists(path):
		frappe.throw("No login hand-off file. Run register_workspace on the agency site first.")
	with open(path) as f:
		data = json.load(f)
	os.remove(path)

	key = (
		frappe.get_doc("Social Login Key", PROVIDER)
		if frappe.db.exists("Social Login Key", PROVIDER)
		else frappe.new_doc("Social Login Key")
	)
	key.update(
		{
			"social_login_provider": "Custom",
			"provider_name": "Dezino360",
			"enable_social_login": 1,
			# base_url only makes the login page show the button; with custom_base_url off
			# the three URLs below are used exactly as given.
			"custom_base_url": 0,
			"base_url": data["base_url"],
			"client_id": data["client_id"],
			"client_secret": data["client_secret"],
			"authorize_url": data["authorize_url"],
			"access_token_url": data["access_token_url"],
			"api_endpoint": data["api_endpoint"],
			"redirect_url": CALLBACK,
			"auth_url_data": json.dumps({"response_type": "code", "scope": "openid"}),
			"user_id_property": "email",
			"sign_ups": "Deny",
			# Text-only button: the full logo doesn't fit inside it, and there is no small mark yet.
			"icon": "",
		}
	)
	key.save(ignore_permissions=True)
	frappe.db.set_single_value("System Settings", "default_app", "crm")
	frappe.db.commit()


def set_business(business_name):
	"""The workspace's own name on its lead emails (sender and footer). The website, signature,
	booking link and postal address are filled in on its settings page before follow-ups go on."""
	_require("client")
	frappe.db.set_single_value("Dezino360 Settings", "business_name", business_name)
	frappe.db.commit()


def ensure_users(users, roles):
	"""Create users that can log in here through Dezino360 (no password of their own)."""
	if isinstance(users, str):
		users = json.loads(users)
	for u in users:
		_ensure_user(u["email"], u.get("first_name") or u["email"], u.get("last_name") or "", roles, "System User")
	frappe.db.commit()


# ---- any site ----------------------------------------------------------------------


def set_password(user):
	"""Set a password read from standard input (never from arguments)."""
	from frappe.utils.password import update_password

	password = sys.stdin.readline().strip()
	if len(password) < 16:
		frappe.throw("Password must come on standard input and be at least 16 characters.")
	update_password(user, password)
	frappe.db.commit()


def set_intake_secret():
	"""The lead intake's shared secret, read from standard input (never from arguments), into
	this site's site_config.json only. Used by hub-intake-secret."""
	from frappe.installer import update_site_config

	secret = sys.stdin.readline().strip()
	if len(secret) < 32:
		frappe.throw("The intake secret must come on standard input and be at least 32 characters.")
	update_site_config("dezino360_intake_secret", secret)


def set_voice_secret():
	"""The AI receptionist's shared secret, read from standard input (never from arguments), into
	this site's site_config.json only. Used by hub-voice-link."""
	from frappe.installer import update_site_config

	secret = sys.stdin.readline().strip()
	if len(secret) < 32:
		frappe.throw("The receptionist secret must come on standard input and be at least 32 characters.")
	update_site_config("dezino360_voice_secret", secret)


def _ensure_user(email, first_name, last_name, roles, user_type):
	email = email.strip().lower()
	if frappe.db.exists("User", email):
		user = frappe.get_doc("User", email)
	else:
		user = frappe.new_doc("User")
		user.update({"email": email, "first_name": first_name, "last_name": last_name})
	user.update({"enabled": 1, "user_type": user_type, "send_welcome_email": 0})
	user.flags.no_welcome_mail = True
	have = {r.role for r in user.roles}
	for role in roles:
		if role not in have:
			user.append("roles", {"role": role})
	user.save(ignore_permissions=True)
