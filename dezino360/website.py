"""Website page details shared by every Dezino360 site."""


def update_website_context(context):
	# Frappe's login page is titled just "Login"; the browser tab and bookmarks say whose it is.
	if context.get("path") == "login":
		return {"title": "Login | Dezino360"}
