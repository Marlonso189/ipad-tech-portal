# Security

This portal is an internal tool. It is meant to sit on a private network, behind HTTPS, and to act only with the Jamf Pro privileges of the technician who signed in.

## Authentication

Technicians sign in with their organization's Jamf Pro server URL and their own Jamf Pro username and password. The server sends those credentials once, as HTTP Basic authentication, to `POST /api/v1/auth/token` on that server. Jamf returns a bearer token. The password is not written to the session store, the audit log, the application log, a cookie, or disk. The Jamf URL is not read from the query string, from `ITP_JAMF_URL`, or from `jamf.url`.

The bearer token and the normalized Jamf origin are kept in process memory, keyed by a random session id. The browser receives only that session id, in a cookie marked `HttpOnly`, `SameSite=Strict`, and `Secure` when the page is HTTPS. JavaScript cannot read the cookie. The browser never calls Jamf. Logout, the idle timeout, and a process restart drop the token and the Jamf address. The only cookie besides the desktop launch cookie is that session id.

Logout calls `POST /api/v1/auth/invalidate-token` and deletes the session. An idle session (15 minutes by default) is deleted on the next request. There is no background poll. Restarting the process drops every session; technicians sign in again. Run a single worker, a single desktop process per person, or a single container, so sessions are not split across processes.

The desktop app adds a second cookie, `itp_launch`. Each launch creates a random token and the window opens `http://127.0.0.1:<port>/?t=<token>`. The server sets the cookie and redirects to drop the token from the address. Later requests must present that cookie, and the `Host` must be `127.0.0.1` or `localhost`. The cookie is `HttpOnly` and `SameSite=Strict`. The desktop process forces `cookie_secure` off because the window is plain HTTP on loopback; WebView2 would drop a Secure cookie there. The token is not stored in the config file, and the desktop process turns uvicorn access logs off so the first URL is not written to the application log. Closing the window invalidates every Jamf token that process still holds.

On each authenticated request, if the Jamf token is within five minutes of the expiry Jamf reported, the server calls `POST /api/v1/auth/keep-alive` and replaces the stored token. The previous token is not logged.

A failed login returns the same message for an unknown user and a wrong password. Validation errors do not echo the request body, so a rejected login cannot reflect the password.

## Authorization

Every Jamf call uses the signed-in technician's token. Jamf enforces that account's privileges and site scope. On login the portal reads `GET /api/v1/auth` and hides actions the account cannot perform. Wipe is not rendered at all without the erase privilege. The server checks the same privileges again before it calls Jamf.

If Jamf returns 403, the page says the account doesn't have permission for that action. If Jamf returns 401, the portal tries one keep-alive. If the token is no longer valid, the technician is asked to sign in again. Jamf uses 401 for a bad or expired token and 403 for a missing privilege.

Static group changes are limited to the configured allowlist or name prefix. If both are empty, no group is offered. A request that names any other group is rejected and audited. Membership outside that filter is left unchanged.

Erase Device is refused unless the typed serial matches the serial Jamf has for that record.

## CSRF and browser protections

State-changing requests must send the `X-CSRF-Token` header issued for the current session. The token is rotated at login. `SameSite=Strict` blocks cross-site cookie use. Responses also set:

- `Content-Security-Policy` limited to same-origin scripts and styles
- `X-Frame-Options: DENY`
- `X-Content-Type-Options: nosniff`
- `Referrer-Policy: no-referrer`
- `Cache-Control: no-store`

## Audit log

Device changes are appended as JSON lines. Each line has the time, Jamf username, Jamf host, device id, serial, action, before and after values, and the result. The active file is never rewritten. It is renamed aside when it crosses the size limit or on the first write of a new UTC day. Rotated files older than the retention period (90 days by default) are deleted.

The writer redacts values whose keys are passwords, tokens, authorization headers, cookies, or secrets, and it strips `Bearer` and `Basic` credentials from strings. Do not add those fields to an audit event.

A lock file next to the log serializes appends and rotation across processes. The desktop config can point `audit.path` at a UNC share. If that path cannot be opened within five seconds, the process writes under `%LOCALAPPDATA%\ipad-tech-portal\logs\` and the page shows a warning. The service and the container do not fall back; they fail to start when the configured path cannot be created. The machine config under `%ProgramData%\ipad-tech-portal\` is installed so Users can read it and Administrators can change it. The program never writes that file.

Application logs go through the same text redaction. The HTTP client library is kept at warning level so it does not log request headers.

## Network

For the Windows service, bind the Python process to `127.0.0.1` and publish only the IIS HTTPS site. Leave port 8741 closed on the firewall. The desktop process also binds `127.0.0.1`, on a port chosen at launch, and it is not meant to be reached from another machine. That loopback page is allowed to be HTTP. The Jamf URL typed in the window still has to be `https://` unless an administrator sets `insecure_test_mode: true` in `%ProgramData%\ipad-tech-portal\config.yaml`. The published container listens on `0.0.0.0:8741` so a reverse proxy can connect. Leave `jamf.verify_tls` true except for a lab server whose certificate you cannot install. A warning is written at startup when verification is off.

The other `ITP_*` variables may replace the YAML file. They hold site settings such as the group filter and the listen address. They are not a place to put a technician password, a Jamf token, or a Jamf server URL. `ITP_JAMF_URL` is ignored. `ITP_INSECURE_TEST_MODE=true` is the Docker lab switch. It is off by default, it is not shown as a control in the page, and turning it on allows an HTTP page and an `http://` Jamf URL and paints a red Insecure test mode banner on every page. The published image does not contain `config.yaml`. The GHCR package `ghcr.io/marlonso189/ipad-tech-portal` is private when the GitHub repository is private. A compose manager pulls it with a classic token that has `read:packages` (and `repo` only if GitHub requires it for that private package). Keep a separate token with `write:packages` for publishing, and do not give that token to Dockhand. Changing the package to public later is done in GitHub package settings and does not put a secret in the image.

The reverse proxy should be the only client of port 8741. The portal treats `X-Forwarded-Proto: https` as the page being HTTPS, which is what a proxy such as Pangolin or IIS sends. A client that can reach 8741 directly can send that header too, so do not publish the port beyond the proxy. IIS terminates TLS. Use an internal certificate. `server.cookie_secure` must be true on that HTTPS site, or the browser will not keep the session. Sign-in over plain HTTP is refused, with a message to open the HTTPS address, unless insecure test mode is on or the request is the desktop loopback window.

Request bodies that include passwords can still be captured by a misconfigured proxy that logs them. Do not enable IIS failed-request tracing of request bodies for this site, and do not put an extra proxy in front that logs POST bodies.

## What this does not do

There is no SSO, no shared secret for the portal itself, and no second account store. A technician who can sign in to Jamf can open the portal, and can do only what that Jamf account allows. Lost Mode and Erase Device still depend on the device being managed, supervised where Apple requires it, and checking in. The portal queues the command; it does not confirm that the device has applied it.

Sessions are not encrypted at rest because they are not written to disk. Anyone who can read the process memory of the service account, or of the technician's desktop process, can read the in-memory tokens and the Jamf address for that sign-in. Run the Windows service as a dedicated account. On an RDS host the desktop process runs as the technician, which is what makes the Jamf calls use their own account. The from-source compose file can still terminate TLS with Caddy, using its internal CA or a certificate and key mounted from `./certs`. Those files are not baked into the image. The published compose file does not run Caddy. Put your own proxy in front of port 8741.

## Reporting

Report a problem with this portal to your internal security contact. This repository has no vendor security mailbox.
