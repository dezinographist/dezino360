"""0.1.20 (6 Nov 2026): every existing workspace's receptionist gets the default name "Daisy" (owner). New workspaces get
it from the setting's default. A name already set is kept."""

import frappe


def execute():
	if not frappe.db.get_single_value("Dezino360 Settings", "voice_agent_name"):
		frappe.db.set_single_value("Dezino360 Settings", "voice_agent_name", "Daisy")
