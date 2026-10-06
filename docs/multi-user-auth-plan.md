[English](multi-user-auth-plan.md) | [한국어](multi-user-auth-plan.ko.md)

# The work, in order

The design is in [multi-user-auth.md](multi-user-auth.md). These are in dependency order, each cut
small enough to ship on its own.

The first three are worth having whether or not OAuth ever lands, so nothing is wasted if the rest
slips.

---

## 1. Put the device ID on the report

**Why** `reports.jsonl` has nothing saying which device sent a row. Without it there is nothing to
filter "only mine" against. It is a prerequisite for everything below, even with one device.

**Scope**
- Firmware: add `mac` to `wakeReport()` (`WiFi.macAddress()`)
- Server: `weather_report()` stores it on the row
- Dashboard: unchanged

**Done when** new reports carry `mac`, and older rows without it still render.

**Depends on** nothing

---

## 2. Move to SQLite

**Why** Once reads filter by device and by user, reading the whole file and filtering in Python
each time stops making sense. And there are ten tables coming.

**Scope**
- A `reports` table (`mac, at, battery_mv, wifi_ms, rssi, reset_reason, wifi_attempts, chip_c, prev_awake_ms, nvs_free, nvs_total, fw`)
- `log` as its own table or a JSON column
- Replace `_append_report` / `_read_reports` with SQL — nothing else sees anything but a list
- A one-time script to move `reports.jsonl` off the volume
- Keep `data/seed-reports.jsonl` as the record

**Done when** the dashboard looks the same as before and the row count survives a redeploy.

**Depends on** 1

---

## 3. Google sign-in and sessions

**Why** "My devices" needs the server to know who "I" am. `authorize()` will lean on this sign-in
later, so it comes first.

**Scope**
- An OAuth client and consent screen in the Google Cloud Console
- `users` (`id, google_sub, email, is_admin`), `sessions` (`sid, user_id, expires_at`)
- `/login` → Google, `/auth/callback` → set the session cookie
  (`HttpOnly`, `Secure`, `SameSite=Lax`)
- `/logout`
- Move `/dashboard` behind the sign-in

**Done when** `/dashboard` is unreachable signed out and unchanged signed in.

**Depends on** nothing — can run alongside 2

---

## 4. An OAuth server on MCP

**Why** It is what lets Claude act for a user. It also closes the hole `/mcp` has been sitting on.

**Scope**
- `oauth_clients` · `auth_codes` · `access_tokens` · `refresh_tokens`
- The nine `OAuthAuthorizationServerProvider` methods
- `authorize()` checks the session from 3 and sends the user to Google if there isn't one
- `FastMCP(auth_server_provider=..., auth=AuthSettings(...))`
- Reconnect the connector and walk the whole flow

**Done when** `/mcp` answers 401 without credentials, and reconnecting the connector goes through
Google sign-in and then works.

**Depends on** 3

**Note** This is the biggest ticket. Split it into (a) metadata and registration, (b) authorize and
token, (c) verification and revocation if it needs to be smaller.

---

## 5. Device tokens and pending registrations

**Why** A device has to prove who it is, or anyone can push junk into someone else's data.

**Scope**
- Firmware: generate a random token and a six-character code at setup, store in NVS, send on every
  report
- Server: `pending_registrations` (keyed by `claim_code`), `devices`
- `POST /weather` verifies the token hash; unknown devices land in pending
- A known MAC with the wrong token becomes a new pending row (the NVS-wipe case)

**Done when** reports without a token are rejected, new devices arrive as pending, and existing
reports are untouched.

**Depends on** 1, 2

---

## 6. Show the claim code on the screen

**Why** So only someone who can see the device can claim it.

**Scope**
- Firmware: draw the code on the e-Paper while unclaimed
- Clear it once a response says the device is registered
- Server: say so in the response

**Done when** a freshly set-up device shows a code, and it disappears when claimed.

**Depends on** 5

---

## 7. Ownership and the access filter

**Why** This is where "only I see my device" starts actually being true.

**Scope**
- `device_access` (`mac, user_id, role`)
- `claim_device(user_id, code)`, called by both the MCP tool and the dashboard form
- `devices_for(user_id)` — every read goes through it
- Remove every query written directly in a handler

**Done when** only claimed devices appear, and someone else's MAC in the URL returns 404.

**Depends on** 4, 5

---

## 8. Split the dashboard per device

**Scope**
- `/dashboard` — my devices, last seen, the claim-code form
- `/dashboard/<mac>` — today's charts and table; 404 if the access check fails
- Chart and table code in `dashboard.py` stays as it is; `rows` arrives filtered

**Done when** two devices show separately and never mix.

**Depends on** 7

---

## 9. Sharing and admin

**Scope**
- `share_device(mac, email)` / `unshare_device(mac, email)`, from both MCP and the dashboard
- An address with no account leaves a pending row that attaches on first sign-in
- `users.is_admin` — a view of counts and health that never reads report contents

**Done when** someone shared with sees the device, and loses it the moment it is unshared.

**Depends on** 7

---

## 10. Query the data through MCP

**Why** This is what the project wanted in the first place. The dashboard is for glancing; the
actual questions get asked of Claude.

**Scope**
- `get_reports(mac, since)`, through `devices_for`
- `list_devices()`
- Move `_location` off the global and onto the device — today everyone shares one city

**Done when** asking Claude something like "how many brownouts last week" answers from your own
devices.

**Depends on** 7

---

## What does not have to wait

- **3** can run alongside 1 and 2
- **10** can come any time after 7, and it is the closest thing here to the original point of the
  project
- **9** can wait indefinitely — there is nothing to share while one person is using this
