import frappe
from frappe.model.document import Document


class DezinoWorkspace(Document):
	def validate(self):
		self.workspace = (self.workspace or "").strip().lower()
		self.site = f"{self.workspace}.dezino360.com"
		if not frappe.conf.get("dezino360_site_role") == "agency":
			frappe.throw("Workspaces are managed on the agency site only.")
