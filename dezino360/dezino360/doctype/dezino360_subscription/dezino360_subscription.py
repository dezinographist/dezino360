from frappe.model.document import Document


class Dezino360Subscription(Document):
	def validate(self):
		from dezino360.billing import stripe_link

		self.stripe_link = stripe_link(self.stripe_subscription)
