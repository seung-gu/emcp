[English](multi-user-auth.md) | [한국어](multi-user-auth.ko.md)

# Serving more than one person: accounts, devices, access

Right now the server is open to anyone. `/mcp` answers 200 with no credentials, and its tools
include writes such as `set_weather_location` and `turn_on_led`. Anyone who has the URL can read
`/dashboard`. Reports pile into one `reports.jsonl`, and nothing in it says which device sent
them.

This is the design for letting several people use the server and each see only their own devices.
The work is broken up in [multi-user-auth-plan.md](multi-user-auth-plan.md).

## Who is involved

| | What | Credential |
|---|---|---|
| User | A person | Google sign-in |
| Claude | A third party acting for the user over MCP | OAuth token |
| Device | ESP32, reporting every 30 minutes | Device token |
| Browser | The dashboard | Session cookie |

## Three ways in, one access check

| Request | How we know who it is | What they may see |
|---|---|---|
| Device → `POST /weather` | Device token | Only writes its own, so no check needed |
| Claude → `/mcp` | `subject` on the OAuth access token | `device_access` |
| Browser → `/dashboard` | `user_id` on the session cookie | `device_access` |

There are three ways to authenticate and one place that decides access. Every path goes through
`device_access`, so the rule lives in one spot.

```python
def devices_for(user_id):      # every read goes through here
    ...JOIN device_access...
```

This only holds if handlers never write their own queries. A missing filter does not raise an
error; it just serves the data. So there should be no route that skips the check.

## Why OAuth is in here at all

If the browser were the only way in, a session cookie would be enough. OAuth is here because of
Claude.

Claude is a third party making requests on the user's behalf. Handing it a password would give it
the whole account with no expiry, and revoking that would mean changing the password. OAuth issues
a separate key that can be thrown away on its own. Implementing OAuth is the price of working with
the data through Claude, which is what this project wanted in the first place.

## The flow

```
1. Claude → /mcp (no token)     → 401, plus where to go to get one
2. Claude → metadata            → finds the authorize / token URLs
3. Claude → registers itself      register_client
4. Browser → /authorize           user signs in with Google
                                  → redirected back to Claude with a code
5. Claude → /token (code)         load_authorization_code
                                  exchange_authorization_code → tokens
6. Claude → /mcp (Bearer)         load_access_token            ← every call
7. Expiry                         exchange_refresh_token
8. Disconnect                     revoke_token
```

Step 4 is the only one a person sees. Claude does the rest by itself.

Steps 4 and 5 are separate because step 4's redirect travels through the browser's address bar,
where it lands in history and logs. So that leg carries only a single-use code good for seconds,
and the real tokens are exchanged over a direct call the browser never touches.

`subject` is the value that links the steps:

```
Google sign-in         → users.id
authorize()            → recorded on code.subject
exchange_auth_code()   → carried to access_token.subject
load_access_token()    → returned on every call → current_user()
                       → filter through device_access
```

### What the SDK does, and what it does not

The MCP Python SDK (`mcp>=1.28`) ships the `OAuthAuthorizationServerProvider` protocol, the
routing, PKCE verification and the error shapes. There is no reference implementation, so you
write all nine methods yourself:

`get_client` · `register_client` · `authorize` · `load_authorization_code` ·
`exchange_authorization_code` · `load_refresh_token` · `exchange_refresh_token` ·
`load_access_token` · `revoke_token`

OAuth also says nothing about how the sign-in inside `authorize()` works, so the user table, the
session cookie and the Google integration are yours to build too. That part is most of why
implementing an OAuth server is a large job.

## Why Google rather than GitHub

People who are not developers will use this, and a GitHub account is a barrier by itself. Taking
passwords directly would drag in hashing, a signup form, reset-by-email and rate limiting; Google
sign-in removes all of it.

Google layers OpenID Connect on top of OAuth, so an ID token (a signed JWT) arrives with the
access token, and no second call is needed to find out who signed in.

Key users on `sub`, Google's stable identifier. Email addresses change and get reused, so they
make poor keys.

## Registering a device

### First boot

```
Wi-Fi setup finishes
  → device generates a random token and a six-character code, stores both in NVS
  → code goes on the e-Paper screen
  → first report: { mac, token, claim_code }
  → server: a pending_registrations row, holding the token's hash
```

The device makes its own token. Sending one from the server would need a safe way to reach the
device, and there isn't one: the server cannot call it, and putting a token in a response means
anyone who knows the MAC can collect it. When the device generates the token, it only ever
travels one way.

### Claiming

```
User → Claude or the dashboard:  "register 4J7K2P"
Server: find that code's pending row
        create devices + device_access(role=owner)
        drop the other pending rows for that MAC
Device: learns from the next response that it is registered, clears the code
```

Claiming only links rows on the server, and nothing is sent to the device.

The MCP tool and the dashboard form both call one function, `claim_device(user_id, code)`.

### Why the code goes on the screen

Only someone who can see the device should be able to claim it. With several users, the unclaimed
list holds other people's devices too, and a code that exists only on a screen keeps them out of
reach.

Do not derive the code from the MAC. If anyone can compute it, having to see the screen stops
meaning anything; if it takes a server secret to compute, it is a random code with extra steps.

### Pending rows are keyed by code, not by MAC

One `devices` row per MAC means whoever registers first takes the slot, and someone could park on
a MAC to keep the real device out.

Keying `pending_registrations` by the code lets several pending rows share a MAC. A row planted by
someone else sits until it expires, because nobody will ever type that code, while the real
device's code is on its screen for its owner to read. Registering first no longer blocks anyone.

## When NVS is wiped

Arduino-ESP32 erases the whole partition when `nvs_flash_init()` returns
`ESP_ERR_NVS_NO_FREE_PAGES` or `ESP_ERR_NVS_NEW_VERSION_FOUND`
(`esp32-hal-misc.c:250-255`). It does this without anyone asking for it.

The token, the code and the Wi-Fi credentials go with it, so the device raises its setup portal
and generates a new token and code. The server then sees a report from a known MAC carrying the
wrong token.

| What the server can do | Result |
|---|---|
| Reject | Safe, but the device is stuck until someone deletes and re-registers it by hand |
| Replace the token | Anyone who knows a MAC can take the device |
| Turn it back into a pending row | The code returns to the screen and the owner claims again |

The server takes the third option. It is one more pending row, which the structure above already
handles, and because reports are keyed by MAC the history continues instead of restarting.

Requiring the current owner's approval to re-register a known MAC closes the quiet-takeover case
as well. Wi-Fi has to be set up again anyway, so someone is already standing at the device.

## Tables

```
── people ──────────────────────────────────────────
users                  id, google_sub, email, is_admin
sessions               sid, user_id, expires_at

── OAuth, for Claude ───────────────────────────────
oauth_clients          client_id, redirect_uris, ...
auth_codes             code, client_id, subject, code_challenge, expires_at
access_tokens          token_hash, client_id, subject, expires_at
refresh_tokens         token_hash, client_id, subject, expires_at

── devices ─────────────────────────────────────────
pending_registrations  claim_code, mac, token_hash, expires_at
devices                mac, name, token_hash
device_access          mac, user_id, role          owner / viewer
reports                mac, at, battery_mv, ...
```

Tokens are stored hashed, so a leaked database does not hand over working credentials.

At this size (one report every 30 minutes, under a megabyte a year), SQLite is enough.

## Sharing and admin

Keeping ownership in `device_access` rows rather than a column on `devices` makes both come for
free.

| Goal | What happens |
|---|---|
| Share | Add a `role=viewer` row |
| Unshare | Delete that row |
| Admin | `users.is_admin`, skip the access check on reads |

Sharing with someone who has no account yet leaves a pending row that attaches the first time that
address signs in.

`is_admin` is a back door. This server holds where people live (the weather city) and when they are
around (when devices wake), so the admin view should show counts and health only and never need to
read anyone's reports.

## The dashboard

```
/                    signed out → a Google sign-in button
/dashboard           my devices, plus the claim-code form
/dashboard/<mac>     that device's charts and table
```

`/dashboard/<mac>` with someone else's MAC returns 404. A 403 would confirm the device exists,
which is enough to map out who owns what by trying MACs.

The chart and table code in `dashboard.py` stays as it is, because the rows arrive already
filtered.

## Known limits

- MACs can be forged. The token proves identity and the MAC is only a name tag. A forged MAC can
  create a pending row, but nobody will type its code, so it expires.
- Anyone who can read the screen can claim the device. For something that sits in a home, that is
  a fair model, and owner approval covers devices already claimed.
- A compromised admin account exposes everything. The mitigation is under "Sharing and admin"
  above.
