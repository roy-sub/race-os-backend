# Security

What the application guarantees, how it does it, and what a reviewer should
check before believing any of it. Findings from the review of 19 September
2026 are recorded at the end, including the ones that were wrong in ways worth
remembering.

---

## The model in one page

| Concern | How it is handled | Where |
|---|---|---|
| Passwords | argon2id, parameters from config, opportunistic rehash on login | `services/security.py` |
| Sessions | RS256 access token (15 min) + opaque refresh cookie (httpOnly, `SameSite=None`, `Secure` outside development) | `services/security.py`, `api/routers/auth.py` |
| Refresh | Rotated on every use. A rotated token presented again is treated as theft and **revokes the whole family** | `services/auth_service.refresh_session` |
| Global sign-out | `users.sessions_invalidated_before` compared against every token's `iat` | `security.token_is_live` |
| Stored tokens | Only SHA-256 hashes. Raw values exist in one response and nowhere else | `services/security.issue_token` |
| Authorization | Checked per request against the live row, never cached into a token | `api/deps.py` |
| Rate limiting | Per route where it matters, and a default ceiling under everything else | `services/rate_limit.py`, `deps.enforce_default_rate_limit` |
| Payment callbacks | HMAC over the raw body, constant-time, with a replay window | `payments/base.verify_signature` |
| Background jobs | Shared secret, constant-time, refuses when unconfigured | `deps.require_internal_secret` |
| Secrets in logs | Startup config dump redacts every secret; a test asserts it | `config.redacted_dump` |

Four structural guarantees have tests that attempt the forbidden action
through every path that exists — see the README's table. This document covers
the ambient posture rather than those.

---

## Things that are easy to get wrong here

**The refresh cookie is `SameSite=None` in production**, because the site and
the API are on different registrable domains and a `Lax` cookie would never be
sent. That means the browser will attach it to cross-site requests, so
`/auth/refresh` additionally requires `X-RaceOS-Client`. That header is not on
the CORS safelist, so a cross-origin caller cannot send it without a preflight,
and the preflight is answered against `CORS_ALLOWED_ORIGINS`. Removing the
header requirement re-opens a cross-site forced-logout.

**`TRUST_PROXY_HEADERS` must match the deployment.** On, the leftmost
`X-Forwarded-For` entry identifies the caller. Off, the socket address does.
Behind a proxy with it off, every caller shares one rate-limit bucket and one
attacker can exhaust the quota for everyone. Directly reachable with it on, a
caller sets their own address and walks through every per-IP limit. There is
no setting that is safe in both places, which is why it has no clever default.

**The rate limiter commits its own counter** before the request it guards
runs. If that commit is removed, every request that raises stops being
counted — which is every failed login — and the limiter silently starts
protecting only the traffic that was going to succeed.

**Nothing may write a credential down.** Reset links, verification links and
coach invites all carry a bearer token. Only the hash is stored; the rendered
message is persisted with the token redacted, and the log line carries the
shape of the message and not its body. `RenderedEmail.secret` is what makes
redaction possible, so a new template that embeds a token must set it.

---

## Reviewing a change

- `make lint && make typecheck && make test` — the suite includes
  `tests/integration/test_security_hardening.py`, which is the regression net
  for everything below.
- `pip-audit -r backend/requirements.txt` — pins make a build reproducible and
  keep it exactly as vulnerable as the day they were written unless somebody
  re-audits. This belongs in the release routine.
- `npm audit` in the frontend, and a look at `public/_headers`, which is
  generated at build time and is the only place the static site's headers can
  come from.

---

## Review of 19 September 2026

Ordered by what an attacker would have got.

### 1 · Password-reset tokens were written down in three places — **fixed**

`PasswordResetToken.delivery_link` stored the full reset URL beside the hash;
`email_messages.body_text` stored the rendered message containing it; and the
logging transport wrote both the body and the link to the application log at
INFO. The columns were **write-only** — nothing in the codebase ever read
them.

Read access to the database, a stale backup, an over-broad support query or
the platform's log viewer was therefore account takeover for every user who
had ever asked for a reset. Logs are the worst of the three: they leave the
system entirely, to whatever drain is attached.

Now only the hash is stored. The transport gets the real message; everything
else gets it with the token replaced. The log line carries recipient (masked),
subject, template and whether the message carried a credential at all.

### 2 · The rate limiter did not count failed requests — **fixed**

`check_rate_limit` incremented a counter inside the request's transaction. A
request that raised rolled it back. Since a rejected login raises, the per-IP
limiter on `/auth/login` counted only *successful* logins — it throttled
legitimate traffic and let brute force through unmetered. The account lockout
still capped attempts per account, so credential stuffing spread across many
accounts was the unprotected case.

Rate limiting was also disabled in the entire test suite, which is why nothing
caught it. It is exercised now.

### 3 · Every caller shared one rate-limit bucket in production — **fixed**

`request.client.host` behind Render's proxy is the proxy. Gunicorn was not
started with `--forwarded-allow-ips`, and nothing read `X-Forwarded-For`, so
every request looked like one address: the per-IP limits were a single shared
quota that one attacker could exhaust for everybody, turning the limiter into
the denial of service. Fixed by `TRUST_PROXY_HEADERS` plus the gunicorn flag.

### 4 · `/auth/refresh` allowed cross-site forced logout — **fixed**

The endpoint is authenticated by the cookie alone, and in production that
cookie is `SameSite=None`. A POST carrying only safelisted headers is a CORS
simple request, so any page could make it with the victim's cookie attached.
The attacker could not read the reply, but `Set-Cookie` still applied: the
token rotated under the victim's own tab, their next real refresh presented a
rotated token, reuse detection fired exactly as designed, and every session was
revoked. Any website could sign any athlete out, repeatedly. Fixed with a
header off the CORS safelist.

### 5 · Validation errors echoed what was sent — **fixed**

Pydantic puts the offending value in `input`, and the handler forwarded every
key. A mistyped password came back in the response body verbatim, into
devtools, any client-side error reporter, proxy logs and support screenshots.
The response now carries `type`, `loc` and `msg` only.

### 6 · Uploaded XML could be an expansion bomb — **fixed**

GPX and TCX are parsed with the stdlib parser, directly and through `gpxpy`.
It does not resolve external entities, so this was never XXE — but it does
expand internal ones, and a few hundred bytes of nested declarations expand to
gigabytes. The 20 MB upload cap does not help, because being small is the
point. No exporter emits a DTD, so one is now refused outright.

### 7 · The frontend shipped with no security headers — **fixed**

A static export has no server to set them, and `headers()` in
`next.config.ts` does nothing under `output: "export"` — so the app that
handles sign-in and payment was framable by any origin, with no CSP behind its
one `dangerouslySetInnerHTML`. `public/_headers` is now generated at build
time, with `connect-src` naming the API exactly, and was verified against the
running app for violations rather than assumed.

### 8 · Dependencies carried 78 known vulnerabilities — **fixed**

Across PyJWT (the library verifying our own auth tokens), python-multipart
(which parses every upload), starlette, lxml, WeasyPrint and Pillow. Upgrading
surfaced a real incompatibility — `Image.getdata` is deprecated in Pillow 12 —
which the suite caught as a 500 on the terrain endpoint.

### 9 · Three route-table tests, one of them a security test, covered nothing

FastAPI 0.141 nests included routers behind a wrapper. Two tests raised on it.
The third — the one asserting no constraint route lets a caller name another
athlete — used `getattr(route, "path", "")`, matched nothing, asserted over an
empty list and passed. It covers four routes again. A security test that
silently stops covering anything is worse than one that fails.

### Recorded and not changed

- **Login says how many attempts remain**, which distinguishes a registered
  address from an unregistered one. Signup already answers that question
  directly ("that email address is already registered"), which is the normal
  trade for a product people sign up to, so login's copy adds no capability an
  attacker did not have. Forgot-password stays silent, which is the endpoint
  where it matters.
- **`raceos:last-user` in `localStorage`** holds a name and email so an offline
  boot can greet the athlete. It is not a credential and cannot reach anyone's
  data, but it does leave an address on a shared device.
- **Email delivery is off**, so password reset cannot complete. Not a
  vulnerability, but it means the recovery path does not work today.
