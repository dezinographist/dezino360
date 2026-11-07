"""Newsletter (Listmonk) unsubscribes reach the CRM, every hour.

Someone who unsubscribes from a Listmonk list, or is blocklisted there, gets consent off on each of
their leads, which stops the day 4 / 8 / 12 follow-ups. Consent given again after that unsubscribe
(say, a new enquiry with the box ticked) is kept. It runs at the start of the hourly follow-up job,
so nobody who has just left the newsletter gets a follow-up that hour.

Only on a workspace whose settings file has dezino360_listmonk_url and dezino360_listmonk_token
(set by /srv/hub/listmonk/hub-link.sh); anywhere else it does nothing. The token belongs to the
Listmonk user "hub", which may only read subscribers and lists.
"""

from datetime import UTC, datetime

import frappe
import requests
from frappe import _
from frappe.utils import get_datetime

# Listmonk's subscriber search: blocklisted, or unsubscribed from at least one list.
OPTED_OUT = (
	"subscribers.status = 'blocklisted' OR subscribers.id IN "
	"(SELECT subscriber_id FROM subscriber_lists WHERE status = 'unsubscribed')"
)
PER_PAGE = 500


def linked():
	return bool(frappe.conf.get("dezino360_listmonk_url") and frappe.conf.get("dezino360_listmonk_token"))


def opted_out():
	"""[(email, when they opted out, "blocklisted" or "unsubscribed")], from Listmonk."""
	url = frappe.conf.get("dezino360_listmonk_url").rstrip("/")
	headers = {"Authorization": "token " + frappe.conf.get("dezino360_listmonk_token")}
	out, page = [], 1
	while True:
		r = requests.get(
			f"{url}/api/subscribers",
			params={"query": OPTED_OUT, "page": page, "per_page": PER_PAGE},
			headers=headers,
			timeout=20,
		)
		r.raise_for_status()
		data = r.json()["data"]
		for s in data["results"]:
			if s["status"] == "blocklisted":
				out.append((s["email"], s["updated_at"], "blocklisted"))
				continue
			times = [
				x["subscription_updated_at"]
				for x in s.get("lists") or []
				if x.get("subscription_status") == "unsubscribed"
			]
			out.append((s["email"], max(times) if times else s["updated_at"], "unsubscribed"))
		if page * PER_PAGE >= data["total"]:
			return out
		page += 1


def _utc(value):
	"""Listmonk's time (ISO, with a zone) as the naive UTC the Hub stores."""
	d = datetime.fromisoformat(value) if isinstance(value, str) else value
	return d.astimezone(UTC).replace(tzinfo=None) if d.tzinfo else d


def sync_unsubscribes(records=None):
	"""Turns consent off on the leads of everyone who opted out in Listmonk. `records` lets the
	tests pass Listmonk's answer in. Returns the leads changed; [] when this site isn't linked."""
	if records is None:
		if not linked():
			return []
		records = opted_out()
	changed = []
	for email, when, how in records:
		when = _utc(when)
		leads = frappe.get_all(
			"CRM Lead",
			filters={"email": email, "consent_to_contact": 1},
			fields=["name", "consent_timestamp"],
		)
		for lead in leads:
			if lead.consent_timestamp and get_datetime(lead.consent_timestamp) > when:
				continue  # consent given again after the unsubscribe
			frappe.db.set_value("CRM Lead", lead.name, {"consent_to_contact": 0, "consent_timestamp": None})
			frappe.get_doc("CRM Lead", lead.name).add_comment(
				"Info",
				_("Left the newsletter ({0} in Listmonk): consent turned off, so follow-up emails stop.").format(
					_(how)
				),
			)
			changed.append(lead.name)
	frappe.db.commit()
	return changed
