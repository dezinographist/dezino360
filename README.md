# dezino360

A custom [Frappe](https://frappe.io/framework) app that adds the Dezino360 Hub's features to
[Frappe CRM](https://github.com/frappe/crm). All custom work lives here; Frappe and Frappe CRM themselves are never edited.

It runs one Frappe site per client: an agency site that is the single login (the "front door"), and a workspace site for
each client, each with its own database.

## What it does

- **Front door and workspaces.** The agency site is the login provider for every client workspace ("Log in with
  Dezino360"). After logging in, agency staff see a workspace switcher; a client goes straight into their own workspace.
  Sign-ups are off, and visitors never see technical error details.
- **CRM set-up.** Lead sources, statuses, deal stages, custom fields and lead assignment, applied the same way on every
  site.
- **Signed lead intake.** An endpoint that accepts leads from a website lead engine. Requests must be signed with a
  shared secret (HMAC); unsigned, old or replayed requests are refused and logged.
- **Emails about leads.** A new-lead alert to the agency, and follow-up emails on day 4, 8 and 12 that stop when the
  lead books or unsubscribes (one-click unsubscribe). Texts are set per workspace. Newsletter unsubscribes from
  [Listmonk](https://listmonk.app) are synced back to the CRM every hour.
- **Booking page** (`/book`). Free times shown in the visitor's time zone, read from Google Calendar; a booking creates
  the calendar event with a Google Meet link, sends a confirmation with an `.ics` file and reminders 24 hours and 1 hour
  before, and marks the lead Meeting Booked. Protected by Cloudflare Turnstile.
- **AI receptionist, Hub side.** Signed endpoints that a separate voice program calls during a phone call (settings,
  free times, booking, and the call summary at the end), call records, and a minutes tracker per workspace with alerts
  at 80% and 100% of the plan's minutes. The voice program itself is not part of this app.
- **Billing.** A Stripe webhook (signature checked) that keeps a Subscription record per customer: Active, Overdue,
  Paused or Cancelled, with desk notifications on each change and a daily check for overdue accounts. The code refuses
  live keys and live-mode events; it is built and tested in Stripe's test mode only.
- **Branding.** Dezino360 colours, the Raleway and Sacramento fonts (served from the app) and the logo on the login,
  switcher and booking pages and in the desk.

Every feature that sends email or touches a third party is **off by default** and is switched on per site in
**Dezino360 Settings**.

## Requirements

- Frappe Framework 15 (tested on 15.125.0) and Frappe CRM 1.90.0
- Python 3.10 or newer
- [stripe](https://pypi.org/project/stripe/) 16.1.0 (installed with the app)

## Install

```bash
bench get-app https://github.com/dezinographist/dezino360
bench --site <your-site> install-app dezino360
bench --site <your-site> migrate
```

## Configuration

Secrets and per-site options go in each site's `site_config.json`, never in this repository. The app reads these keys
(all optional; a feature whose keys are missing stays off or refuses requests):

| Key | Used for |
|---|---|
| `dezino360_site_role`, `dezino360_front_door`, `dezino360_internal_url` | Which site is the front door, and how workspaces reach it |
| `dezino360_owner_email` | The agency owner's user (new leads are assigned to it) |
| `dezino360_intake_secret` | Shared secret for signed lead intake |
| `dezino360_google_client_id`, `dezino360_google_client_secret`, `dezino360_google_refresh_token` | Google Calendar for the booking page |
| `dezino360_turnstile_secret` | Cloudflare Turnstile on the booking page |
| `dezino360_listmonk_url`, `dezino360_listmonk_token` | Listmonk unsubscribe sync (read-only API user) |
| `dezino360_voice_secret` | Shared secret for the receptionist endpoints |
| `dezino360_stripe_key`, `dezino360_stripe_webhook_secret`, `dezino360_stripe_account` | Stripe (test mode) |
| `dezino360_mail_catcher` | Marks a site whose mail goes to a local test catcher |

## Not in this repository

The phone receptionist program, the website and its lead engine, and the server set-up scripts are separate programs
and are not published here.

## Licence

Copyright (C) 2026 AAS Groupe Pty Ltd.

This program is free software: you can redistribute it and/or modify it under the terms of the GNU Affero General Public
License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later
version. It is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied
warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See [`LICENSE`](LICENSE) for the full text.

The names Dezino360 and Dezino Graphist and the Dezino360 logo (`dezino360/public/images/logo.svg`) identify our
business. The licence does not grant any right to use them (AGPL section 7(e)); if you run a modified copy for others,
replace them with your own.

The Raleway and Sacramento fonts are under the SIL Open Font Licence 1.1 (see `dezino360/public/fonts/`).

## Security

Please report security problems privately to grow@dezino360.com, not in a public issue.
