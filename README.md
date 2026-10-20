# iPad Tech Portal

A small internal site for technicians who need to make a limited set of changes to Jamf Pro-managed iPads. It is not tied to one organization. Each technician signs in with their own Jamf Pro account and the address of that organization's Jamf Pro server, whether it is on-prem or Jamf Cloud. The server holds the Jamf bearer token and that server address for the sign-in only. The browser never talks to Jamf.

About 2,000 devices is the expected size. Lookups are a single filtered inventory query. Nothing polls Jamf.

## Stack

Python, [FastAPI](https://fastapi.tiangolo.com/), and [uvicorn](https://www.uvicorn.org/), with a single HTML page and no frontend build.

FastAPI fits this better than a full ASP.NET Core app. The portal is a session, a Jamf client, and one page. Request validation, cookie handling, and tests stay in a small Python process. uvicorn is the ASGI server FastAPI runs on, including under Windows. Waitress is a WSGI server, so it would need another adapter.

The same process is the core for every install. [Choose a deployment](#choose-a-deployment) covers the Windows service, the desktop window, and Docker Compose. None of them fork the request handling.

Jamf Pro 11.30 is called with the signed-in technician's token. Settings come from environment variables, from `config.yaml`, or from both. An environment variable overrides the same setting in the file. The file is optional. Neither place holds a password or a Jamf server URL. The technician types the URL on the sign-in page, every time.

Install it as a Windows service behind IIS, as a desktop window on an RDS host, or as a single container. The steps for all three are in [Choose a deployment](#choose-a-deployment).

## What a technician can do

1. Look up an iPad by exact serial number or exact asset tag.
2. Change department, building, and room. Department and building are dropdowns from Jamf. Room is free text, because Jamf has no room catalog.
3. Change the assigned user (username, full name, email) and the asset tag.
4. Add or remove the iPad from static mobile device groups that pass the allowlist or name-prefix filter.
5. Queue Restart Device, Update Inventory, and Clear Passcode.
6. Enable Lost Mode (message, phone, footnote) after a confirm dialog, disable Lost Mode, and queue Erase Device. Erase asks the technician to type the serial, and the button is omitted unless the account has the erase privilege.
7. After a location, user, or asset-tag change, optionally queue Update Inventory so smart groups can re-evaluate. The checkbox defaults from config.

Actions the account cannot perform are hidden. If Jamf still returns 403, the page says the account doesn't have permission for that action.

## Jamf Pro endpoints

The Pro API is used everywhere it covers the job. The Classic API is used for one read: the groups a single device belongs to. The Pro API can list the members of a group, but it cannot return "which static groups is this device in" in one call. If that Classic read is unavailable (HTTP 404), the portal checks each *visible* group with the Pro membership endpoint. It does not walk the fleet.

| Purpose | Method and path |
| --- | --- |
| Exchange username and password for a bearer token | `POST /api/v1/auth/token` (Basic auth) |
| Refresh the bearer token | `POST /api/v1/auth/keep-alive` |
| Invalidate the token on logout | `POST /api/v1/auth/invalidate-token` |
| Current account privileges | `GET /api/v1/auth` |
| Find or re-read an iPad | `GET /api/v2/mobile-devices/detail` with `section=GENERAL`, `HARDWARE`, `USER_AND_LOCATION`, `SECURITY` and an RSQL `filter` |
| Change location, user, or asset tag | `PATCH /api/v2/mobile-devices/{id}` |
| Department dropdown | `GET /api/v1/departments` |
| Building dropdown | `GET /api/v1/buildings` |
| Static groups the filter might allow | `GET /api/v2/mobile-device-groups/static-groups` (falls back to `/api/v1/...` on 404) |
| Groups one device is in | `GET /JSSResource/mobiledevices/id/{id}/subset/MobileDeviceGroups` (Classic, `Accept: application/json`) |
| Fallback membership check | `GET /api/v2/mobile-device-groups/static-group-membership/{groupId}?filter=mobileDeviceId=="{id}"` |
| Add or remove one device | `PATCH /api/v2/mobile-device-groups/static-groups/{id}` with `{ "assignments": [{ "mobileDeviceId": "...", "selected": true }] }` (`selected: false` removes). Falls back to the v1 path on 404. |
| MDM commands | `POST /api/v2/mdm/commands` (Update Inventory falls back to Classic `POST /JSSResource/mobiledevicecommands/command/UpdateInventory/id/{id}`) |
| Wipe with Return to Service | `POST /api/v2/mobile-devices/{id}/erase` `{"returnToService": true}` (uses the PreStage Wi-Fi profile) |

MDM `commandData.commandType` values:

| Button | `commandType` | Extra fields |
| --- | --- | --- |
| Restart device | `RESTART_DEVICE` | none |
| Update inventory | `UPDATE_INVENTORY` | none |
| Clear passcode | `CLEAR_PASSCODE` | none |
| Enable Lost Mode | `ENABLE_LOST_MODE` | `lostModeMessage`, `lostModePhone`, `lostModeFootnote` (only non-empty fields; message or phone is required) |
| Disable Lost Mode | `DISABLE_LOST_MODE` | none |
| Erase device | `ERASE_DEVICE` | none. No macOS obliteration PIN is sent. |

Commands are addressed by the device `general.managementId`.

## Privileges

These are the privilege strings `GET /api/v1/auth` returns in `account.privilegesBySite`. In the Jamf account UI they sit under Jamf Pro Server Objects and Jamf Pro Server Actions. An account whose `privilegeSet` is `ADMINISTRATOR` is treated as allowed to use every feature. Anyone else needs every privilege in the row. Where two names are listed with "or", either name is accepted because older account UI labels differ.

| Feature | Privileges |
| --- | --- |
| Look up an iPad | Read Mobile Devices |
| Department, building, and room | Update Mobile Devices, Read Departments, Read Buildings |
| Assigned user | Update Mobile Devices |
| Asset tag | Update Mobile Devices |
| See static groups and which of them the iPad is in | Read Static Mobile Device Groups, Read Mobile Devices |
| Add or remove static group membership | Update Static Mobile Device Groups |
| Restart device | View MDM command information in Jamf Pro API, Send Mobile Device Restart Device Command |
| Update inventory | View MDM command information in Jamf Pro API, and Send Mobile Device Update Inventory Command *or* Send Inventory Requests to Mobile Devices |
| Clear passcode | View MDM command information in Jamf Pro API, and Send Mobile Device Clear Passcode Command *or* Send Mobile Device Remove Passcode Command |
| Enable Lost Mode | View MDM command information in Jamf Pro API, and Send Mobile Device Enable Lost Mode Command *or* Send Mobile Device Lost Mode Command |
| Disable Lost Mode | View MDM command information in Jamf Pro API, and Send Mobile Device Disable Lost Mode Command *or* Send Mobile Device Lost Mode Command |
| Erase device | View MDM command information in Jamf Pro API, Send Mobile Device Remote Wipe Command |

Jamf still applies site scope. A site-limited account only sees the devices Jamf returns. Restart, Lost Mode, and erase also depend on the device being managed, and Restart and Lost Mode require supervision. The portal refuses those when the inventory record says the device is not eligible, and Jamf can still reject the command.

Create a custom privilege set with only the rows you want technicians to have. Do not grant Create or Delete on mobile devices, and do not grant smart-group edit.

## Configure

```text
copy config.example.yaml config.yaml
```

Paths inside the file are relative to the file's directory, not the service's working directory. Settings:

| Key | Meaning |
| --- | --- |
| `insecure_test_mode` | Lab only. Default false. There is no control for this on the page. When true, an HTTP page and an `http://` Jamf URL are allowed, and every page shows a red Insecure test mode banner. On the Windows desktop app this key lives in the admin-only `%ProgramData%\ipad-tech-portal\config.yaml`. |
| `jamf.verify_tls` | Check the Jamf certificate. Leave true outside a lab. |
| `jamf.timeout_seconds` | Per-request timeout. |
| `server.host` / `server.port` | Where uvicorn listens. Use `127.0.0.1` and `8741` behind IIS. |
| `server.cookie_secure` | True on the HTTPS site. False only for a local HTTP demo, or the browser will drop the cookie. |
| `session.idle_timeout_minutes` | Idle time before the session is discarded. Default 15. |
| `groups.allowlist` | Exact static group names (case-insensitive). If this list is non-empty, the prefix is ignored. |
| `groups.name_prefix` | Used only when the allowlist is empty. Default `Tech-`. If both are empty, no groups are offered. |
| `audit.path` | Append-only JSON-lines change log. `{host}` and `{user}` expand to a sanitized computer name and account name. A path that starts with `\\` is kept as a UNC path. |
| `audit.retention_days` | How long rotated files are kept. Default 90. |
| `audit.max_bytes` | Rotate the active file at this size. It also rotates on the first write of a new UTC day. |
| `inventory.update_inventory_after_change` | Default for the "Queue Update Inventory" checkbox. |
| `logging.path` / `logging.level` | Application log. Passwords and tokens are redacted. |

There is no API client secret, no service account password, and no Jamf server URL in this file. `jamf.url` and `ITP_JAMF_URL` are ignored if an old file or environment still has them.

### Environment variables

Set these in the service environment, in Compose, or in the shell before `python -m app`. Each one overrides the YAML key in the previous table. An empty value does not override the file, except `ITP_GROUP_ALLOWLIST` and `ITP_GROUP_PREFIX`: an empty allowlist means no allowlist, and an empty prefix means no prefix.

`config.yaml` is optional. The process starts with the defaults below when the file is absent. `ITP_CONFIG` points at a file. A missing file at that explicit path is still an error. The Jamf server URL is not one of these settings.

Booleans accept `1`, `true`, `yes`, `on`, `0`, `false`, `no`, and `off`.

| Variable | Overrides | When the variable is unset and there is no file |
| --- | --- | --- |
| `ITP_CONFIG` | which YAML file is read | `config.yaml` in the working directory, if that file exists |
| `ITP_INSECURE_TEST_MODE` | `insecure_test_mode` | `false`. Docker lab switch. Allows an HTTP page and an `http://` Jamf URL, and shows the red banner. Never expose a control for this in the page |
| `ITP_VERIFY_TLS` | `jamf.verify_tls` | `true` |
| `ITP_TIMEOUT_SECONDS` | `jamf.timeout_seconds` | `30` |
| `ITP_HOST` | `server.host` | `127.0.0.1` |
| `ITP_PORT` | `server.port` | `8741` |
| `ITP_COOKIE_SECURE` | `server.cookie_secure` | `true` |
| `ITP_COOKIE_NAME` | `server.cookie_name` | `itp_session` |
| `ITP_IDLE_TIMEOUT_MINUTES` | `session.idle_timeout_minutes` | `15` |
| `ITP_GROUP_ALLOWLIST` | `groups.allowlist` | empty. Comma-separated exact names. Empty means no allowlist, so the prefix applies |
| `ITP_GROUP_PREFIX` | `groups.name_prefix` | `Tech-`. An empty value turns the prefix off |
| `ITP_AUDIT_PATH` | `audit.path` | `logs/audit.jsonl` |
| `ITP_AUDIT_RETENTION_DAYS` | `audit.retention_days` | `90` |
| `ITP_AUDIT_MAX_BYTES` | `audit.max_bytes` | `10485760` |
| `ITP_UPDATE_INVENTORY` | `inventory.update_inventory_after_change` | `true` |
| `ITP_LOG_PATH` | `logging.path` | `logs/app.log` |
| `ITP_LOG_LEVEL` | `logging.level` | `INFO` (`DEBUG`, `INFO`, `WARNING`, or `ERROR`) |

The from-source Compose file still reads two variables that Caddy uses. The Python process ignores them. The published image does not run Caddy.

| Variable | Meaning |
| --- | --- |
| `PORTAL_HOST` | Name on the from-source Caddy certificate. Default `ipad-tech.example.org`. |
| `CADDYFILE` | Which Caddyfile the from-source stack mounts. |

## Run locally against the mock

The mock speaks the endpoints above and keeps its data in memory. It is not Jamf. Two demo accounts exist only inside it:

| Username | Password | What it can do |
| --- | --- | --- |
| `tech` | `demo-tech` | Every action, including erase |
| `desk` | `demo-desk` | Every action except Erase Device and Enable Lost Mode |

Sample iPads use serials `DMPX2TEST001` through `DMPX2TEST004`. `IPAD-1001` matches two iPads. `DMPXPHONE001` is an iPhone and is rejected.

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy config.example.yaml config.yaml
```

On macOS or Linux the activate path is `.venv/bin/activate`, and `cp` replaces `copy`.

Edit `config.yaml` for the mock. The lab switch is what allows the HTTP page and the `http://` mock URL. Leave it false for anything else.

```yaml
insecure_test_mode: true
jamf:
  verify_tls: false
server:
  host: "127.0.0.1"
  port: 8741
  cookie_secure: false
```

Open `http://127.0.0.1:8741`. On the sign-in page, the Jamf Pro server is `http://127.0.0.1:8742`. The page shows that host before the password is sent. A link cannot fill the field in. The mock listens on port 8742 (`MOCK_JAMF_PORT` overrides it).

Start the mock, then the portal, from the repository root:

```bash
python -m mock_jamf
python -m app
```

Tests, with Jamf mocked in-process:

```bash
python -m pytest
```

## Choose a deployment

| | Windows service and IIS | Windows desktop app | Docker |
| --- | --- | --- | --- |
| Where it runs | One Windows Server, one service | One process per RDS session, published in Horizon | One portal container behind your reverse proxy |
| Who reaches it | Browsers on the internal network, through IIS | The technician's own window on that session | Browsers through the proxy, which forwards to port 8741 |
| HTTPS | IIS | The window talks to `127.0.0.1` over HTTP. A per-launch token stops other sessions on the same host from using it. The Jamf URL is still `https://` | The reverse proxy. It must send `X-Forwarded-Proto: https` |
| Config | `config.yaml` next to the app | `%ProgramData%\ipad-tech-portal\config.yaml`, readable by users and writable by administrators | No required environment variables. The image listens on `0.0.0.0:8741` |
| Audit log | A file on the server | A UNC path with a cross-process lock, or a local file if that path is down | Bind mount `/opt/dockers/appdata/ipad-tech-portal/logs` at `/app/logs` |
| Sessions | In memory, one worker | In memory, inside that user's process | In memory, one container |

### Windows service behind IIS

IIS is already installed and is the right place to terminate HTTPS for a shared internal site. uvicorn listens on localhost only. IIS reverse-proxies to it with URL Rewrite and Application Request Routing (ARR). The process is a Windows service via [NSSM](https://nssm.cc/).

These steps assume Windows Server with IIS already installed, a private network, and no SSO. Use an internal DNS name such as `ipad-tech.example.org`. The examples use `C:\ipad-tech-portal` and Python 3.12.

### 1. Python and the app

Install Python 3.12 (64-bit) for all users and include it on PATH. In an elevated PowerShell:

```powershell
New-Item -ItemType Directory -Path C:\ipad-tech-portal
# Copy this repository into C:\ipad-tech-portal
Set-Location C:\ipad-tech-portal
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item config.example.yaml config.yaml
notepad config.yaml
```

Leave `verify_tls` true, leave `server.host` as `127.0.0.1`, and set `server.cookie_secure` to `true`. Do not put a Jamf URL in the file. Technicians type it on the sign-in page. IIS must keep sending `X-Forwarded-Proto: https`, which `deploy\web.config` already sets. Sign-in is refused if that header is missing and the request is plain HTTP.

Confirm it listens before installing the service:

```powershell
.\.venv\Scripts\python.exe -m app
```

From another window: `Invoke-WebRequest http://127.0.0.1:8741/api/health`. Stop the process with Ctrl+C after it returns `{"status":"ok"}`.

### 2. Windows service (NSSM)

Download NSSM and use the `win64\nssm.exe`. A dedicated local account is better than Local System. Create one, grant it Modify on `C:\ipad-tech-portal`, and do not give it an interactive login. The examples below use `.\ipad-tech-svc`.

```powershell
nssm install IpadTechPortal C:\ipad-tech-portal\.venv\Scripts\python.exe
nssm set IpadTechPortal AppParameters -m app
nssm set IpadTechPortal AppDirectory C:\ipad-tech-portal
nssm set IpadTechPortal AppEnvironmentExtra ITP_CONFIG=C:\ipad-tech-portal\config.yaml
nssm set IpadTechPortal DisplayName "iPad Tech Portal"
nssm set IpadTechPortal Start SERVICE_AUTO_START
nssm set IpadTechPortal AppStdout C:\ipad-tech-portal\logs\service-stdout.log
nssm set IpadTechPortal AppStderr C:\ipad-tech-portal\logs\service-stderr.log
nssm set IpadTechPortal AppRotateFiles 1
nssm set IpadTechPortal ObjectName .\ipad-tech-svc "ReplaceWithTheAccountPassword"
nssm start IpadTechPortal
```

`ObjectName` is the service logon. That password is stored by Windows in the service configuration, not in `config.yaml`. Check `Get-Service IpadTechPortal` and `http://127.0.0.1:8741/api/health`.

Do not add `--workers` greater than 1. Sessions live in this process.

Useful later:

```powershell
nssm restart IpadTechPortal
nssm stop IpadTechPortal
nssm remove IpadTechPortal confirm
```

### 3. IIS HTTPS reverse proxy

Install these IIS extensions (they are not Windows features):

- [URL Rewrite](https://www.iis.net/downloads/microsoft/url-rewrite)
- [Application Request Routing](https://www.iis.net/downloads/microsoft/application-request-routing)

Enable the ARR proxy and allow the forwarded-header variables, in an elevated prompt:

```powershell
$appcmd = "$env:windir\system32\inetsrv\appcmd.exe"
& $appcmd set config -section:system.webServer/proxy /enabled:"True" /preserveHostHeader:"True" /reverseRewriteHostInResponseHeaders:"False" /commit:apphost
& $appcmd set config -section:system.webServer/rewrite/allowedServerVariables /+"[name='HTTP_X_FORWARDED_PROTO']" /commit:apphost
& $appcmd set config -section:system.webServer/rewrite/allowedServerVariables /+"[name='HTTP_X_FORWARDED_HOST']" /commit:apphost
```

If a variable is already allowed, `appcmd` reports that and you can continue.

Create a site whose physical path is the folder that contains `web.config`:

```powershell
New-Website -Name "IpadTechPortal" -PhysicalPath "C:\ipad-tech-portal\deploy" -Port 80 -HostHeader "ipad-tech.example.org"
```

`deploy\web.config` redirects HTTP to HTTPS and reverse-proxies every other request to `http://127.0.0.1:8741/{path}`. If you changed `server.port`, edit the rewrite URL in that file to match.

### 4. Certificate and the HTTPS binding

Use a certificate from your internal CA when you have one. Import it into `Local Machine\Personal`. For a lab without a CA, a self-signed certificate works if technicians trust it:

```powershell
$cert = New-SelfSignedCertificate `
  -DnsName "ipad-tech.example.org" `
  -CertStoreLocation "Cert:\LocalMachine\My" `
  -KeyAlgorithm RSA `
  -KeyLength 2048 `
  -NotAfter (Get-Date).AddYears(2)
$cert.Thumbprint
```

Bind HTTPS in IIS Manager, which is the reliable path:

1. Open **IIS Manager**.
2. Select the server node, open **Server Certificates**, and confirm the certificate is in the list.
3. Select the **IpadTechPortal** site, then **Bindings**.
4. Add a binding: type **https**, port **443**, host name `ipad-tech.example.org`, and select the certificate. Enable **Require Server Name Indication** when the host name is set.
5. Start the site.

The scripted equivalent, after the certificate is in `Cert:\LocalMachine\My`:

```powershell
New-WebBinding -Name "IpadTechPortal" -Protocol https -Port 443 -HostHeader "ipad-tech.example.org" -SslFlags 1
$thumb = (Get-ChildItem Cert:\LocalMachine\My | Where-Object { $_.Subject -like "*ipad-tech.example.org*" } | Select-Object -First 1).Thumbprint
(Get-WebBinding -Name "IpadTechPortal" -Protocol https -Port 443 -HostHeader "ipad-tech.example.org").AddSslCertificate($thumb, "My")
```

`SslFlags 1` is SNI. Point internal DNS (or a hosts entry) for `ipad-tech.example.org` at this server. Open TCP 443 in the firewall. Do not open TCP 8741.

```powershell
New-NetFirewallRule -DisplayName "iPad Tech Portal HTTPS" -Direction Inbound -Protocol TCP -LocalPort 443 -Action Allow
```

Browse `https://ipad-tech.example.org/api/health`, then sign in with a Jamf account and look up a known serial.

### 5. Check the service after a reboot

NSSM was set to start automatically. After a reboot, `https://ipad-tech.example.org/api/health` should answer without anyone logging on to the console. The application log is `C:\ipad-tech-portal\logs\app.log`. The change log is `C:\ipad-tech-portal\logs\audit.jsonl`.

### Windows desktop app for Horizon

Use this when technicians should launch the portal as a published application on a multi-session RDS host. Each Windows session starts its own process. The process binds `127.0.0.1` on a free port and puts a random token in the first page load. The page answers other requests only when they carry that token in an `HttpOnly`, `SameSite=Strict` cookie. Closing the window signs the technician out and calls Jamf `invalidate-token`.

The window is [pywebview](https://pywebview.flowrl.com/). On Windows it uses the Edge WebView2 that is already on the host, so the package does not ship a second browser. Electron would. A plain `msedge --app=` window would not give the process a reliable close event, so the Jamf token would be left in memory until the idle timeout. The build is a folder, plus an Inno Setup installer, because a one-file executable unpacks itself on every launch and that is wasteful when many sessions start it.

The RDS host needs the [WebView2 Evergreen Runtime](https://developer.microsoft.com/microsoft-edge/webview2/). Current Windows 11 and Windows Server 2022 images usually have it. The desktop build also needs Python 3.12, PyInstaller, and [Inno Setup 6](https://jrsoftware.org/isinfo.php).

This Linux workspace cannot produce the Windows executable. Build it on Windows, or run the GitHub Actions workflow `.github/workflows/desktop.yml` (`workflow_dispatch`, or a push to `main` that touches the app or the desktop files). The workflow runs the tests, then `desktop\build.ps1 -RequireInstaller`, and uploads `dist\ipad-tech-portal\` and `desktop\output\ipad-tech-portal-setup.exe`.

On a Windows build machine, from the repository root:

```powershell
py -3.12 -m pip install -r requirements.txt -r requirements-desktop.txt
powershell -ExecutionPolicy Bypass -File desktop\build.ps1
```

`desktop\build.ps1` writes the folder to `dist\ipad-tech-portal\`. When `ISCC.exe` from Inno Setup 6 is installed, it also writes `desktop\output\ipad-tech-portal-setup.exe`. Run the installer on the RDS host (or in the image you clone for the farm). It adds a Start Menu shortcut named **iPad Tech Portal** and, only if the file is missing, copies `desktop\config.example.yaml` to:

```text
%ProgramData%\ipad-tech-portal\config.yaml
```

The installer grants Users read and execute on that directory and Full control to Administrators. Edit the file as an administrator before technicians launch the app. Leave the file free of passwords and free of a Jamf URL. Technicians type the Jamf Pro URL in the window on every launch. The window's own page is `http://127.0.0.1` and is exempt from the HTTPS-page check. The Jamf URL still has to be `https://` unless an administrator sets `insecure_test_mode: true` in that file. There is no switch for that in the window. When it is on, every page shows a red Insecure test mode banner and an `http://` Jamf URL is accepted. The process ignores `server.host`, `server.port`, and `server.cookie_secure`: it always binds loopback on a free port, and it leaves the Secure flag off because the window is HTTP on loopback. The application log goes to `%LOCALAPPDATA%\ipad-tech-portal\logs\app.log` so it does not need write access to ProgramData.

Set `audit.path` to a UNC share the technician accounts can modify, for example `\\files.example.org\ipad-tech\audit.jsonl`. Appends take a lock file beside the log (`audit.jsonl.lock`), so several sessions and several RDS hosts can share one file. `{host}` and `{user}` in the path expand to the computer name and the Windows account when you want a separate file per person. Retention runs when a file is written, so a single shared file is the one that gets cleaned up by whoever is working. If the path cannot be opened within 5 seconds, that session writes `%LOCALAPPDATA%\ipad-tech-portal\logs\audit.jsonl` and the sign-in page shows a warning. Those lines stay on that computer.

Publish the installed executable in Horizon Console (Horizon 8):

1. Install the app on every RDS host in the farm, or in the image those hosts are cloned from. Confirm WebView2 is installed there too.
2. Confirm `%ProgramData%\ipad-tech-portal\config.yaml` is in place on those hosts. A cloned image can carry it.
3. Sign in to Horizon Console.
4. Open **Inventory > Applications > Add > Add Manually**. (If the agent has already scanned the Start Menu shortcut, **Add from Installed Applications** and the name **iPad Tech Portal** is the same program.)
5. Choose the RDS farm.
6. Set the display name to `iPad Tech Portal` and the ID to `ipad-tech-portal`.
7. Executable path: `C:\Program Files\ipad-tech-portal\ipad-tech-portal.exe`
8. Start folder: `C:\Program Files\ipad-tech-portal`
9. Leave the parameters empty. The launch token is created in the process and is not a command-line argument.
10. Finish the wizard.
11. Select the application, open **Entitlements > Add Entitlements**, and add the Active Directory group that contains the technicians.

A technician starts **iPad Tech Portal** from the Horizon Client. The window opens on the sign-in page. Closing it invalidates that session's Jamf token. Each RDS logon is a separate process, port, and token. Publish the executable, and leave the IIS service for a different host if you also want the browser site. Run one or the other on a given host so two copies are not both writing operational confusion; they can share the same audit UNC path.

### Deploy with Dockhand / any compose manager

Paste one Compose file. The manager pulls `ghcr.io/marlonso189/ipad-tech-portal`. It does not check out this repository, it does not build, and it does not need a config file or a Jamf URL. The same text is stored at `deploy/docker-compose.yml`.

Put the container behind an HTTPS reverse proxy (Pangolin, IIS, Caddy, or anything else that sets `X-Forwarded-Proto: https`). The image listens on `0.0.0.0:8741`. Publish that port only to the proxy. Sign-in is refused when the page is plain HTTP and that header is not `https`. Technicians type the Jamf Pro URL on the sign-in page. Sessions stay in this one container.

#### Put the repository on GitHub and publish the image

Development of this tree can stay on Cursor Origin (`marvin-alonso/ipad-tech-portal`). GitHub Actions does not run there. `.github/workflows/publish-image.yml` runs after the same commits are on a private GitHub repository, `github.com/Marlonso189/ipad-tech-portal`.

1. Create that GitHub repository as **private** if it does not exist yet.
2. Push `main` to it, or add a mirror from Origin to `https://github.com/Marlonso189/ipad-tech-portal.git`.
3. On every push to `main`, the workflow builds `linux/amd64` and `linux/arm64` and pushes `ghcr.io/marlonso189/ipad-tech-portal:latest` and `:sha` (the full commit SHA). A tag `vX.Y.Z` also pushes `:X.Y.Z` without the `v`. You can start the same workflow by hand with **Run workflow**.
4. The first push creates the GHCR package. Because the GitHub repository is private, the package is created private. The workflow uses the built-in `GITHUB_TOKEN` with `packages: write`. You do not add a registry secret for that workflow.

To publish from any machine that has Docker Buildx, without waiting for Actions:

```bash
export GHCR_USERNAME=Marlonso189
export GHCR_TOKEN=...
./scripts/publish-image.sh
```

`GHCR_TOKEN` is a classic personal access token with `write:packages` and `read:packages`. The script reads it from the environment and does not store it. Use that token only to publish. The token you give Dockhand is a different one, below.

#### Let the compose manager pull the private image

1. In GitHub, open **Settings → Developer settings → Personal access tokens → Tokens (classic) → Generate new token (classic)**.
2. Grant **`read:packages`** only. If GitHub later refuses the pull because the package is linked to a private repository, add the **`repo`** scope to that same token. Do not grant `write:packages` on the token the compose manager stores.
3. In Dockhand (or another compose manager), add a registry: host `ghcr.io`, username `Marlonso189`, password the token.
4. On a Docker host the same login is `docker login ghcr.io`. The username is `Marlonso189` and the password is the token.

#### Paste the stack

No environment variable is required. Remove `ITP_JAMF_URL` if a previous stack set it. The proxy's HTTPS address is what technicians open. Create the log directory and give it to uid 10001, which is the user in the image:

```bash
sudo mkdir -p /opt/dockers/appdata/ipad-tech-portal/logs
sudo chown 10001:10001 /opt/dockers/appdata/ipad-tech-portal/logs
```

```yaml
# Paste this file into Dockhand or any Compose manager.
# It pulls the image. It does not build, and it does not need a config file.
# No Jamf URL is set here. Technicians type it on the sign-in page.
# Run it behind an HTTPS reverse proxy that sets X-Forwarded-Proto.
# One container only: sessions live in this process.
# The process runs as uid 10001. The log directory must be writable by that user.

name: ipad-tech-portal

services:
  portal:
    image: ghcr.io/marlonso189/ipad-tech-portal:latest
    pull_policy: always
    restart: unless-stopped
    ports:
      - "8741:8741"
    volumes:
      - /opt/dockers/appdata/ipad-tech-portal/logs:/app/logs
    # Lab only. Leave this commented. It allows an HTTP page and an HTTP Jamf URL.
    # environment:
    #   ITP_INSECURE_TEST_MODE: "true"
```

Point the reverse proxy at `http://<docker-host>:8741` and set `X-Forwarded-Proto` to `https`. Do not publish 8741 to the general network if you can help it: a client that can reach the container directly can send that header itself. Optional settings such as `ITP_GROUP_ALLOWLIST` still work if you add them. Leave `ITP_INSECURE_TEST_MODE` commented out. Uncommenting it is the lab switch, and the page will show a red Insecure test mode banner.

#### Change the package between private and public

The image name stays `ghcr.io/marlonso189/ipad-tech-portal`. Visibility is a package setting, not a new build.

1. Sign in to GitHub as `Marlonso189`.
2. Open the profile menu and choose **Packages**, or open `https://github.com/users/Marlonso189/packages/container/package/ipad-tech-portal`.
3. Open **Package settings**.
4. At the bottom, choose **Change visibility**. Public lets any host pull without `docker login`. Private again means the compose manager needs the `ghcr.io` login from the section above.

### Docker Compose from source

Use this when you are changing the app and want to build the image on the same machine. It is separate from the paste-in file. Caddy terminates HTTPS. The portal container is one replica because sessions are in memory. The image runs as a non-root user and includes a health check. Nothing in the image is a Jamf password or token.

```bash
cp config.docker.example.yaml config.yaml
# Leave verify_tls true and cookie_secure true. Do not set a Jamf URL.
docker compose up -d --build
```

Copy `.env.example` to `.env` when you want a different hostname:

```text
PORTAL_HOST=ipad-tech.example.org
CADDYFILE=./deploy/Caddyfile.internal
```

Compose publishes ports 80 and 443. It does not publish 8741. Technicians type the Jamf Pro URL on the sign-in page. Caddy's `reverse_proxy` sets `X-Forwarded-Proto` from the browser's scheme, so a sign-in on the HTTPS site is accepted. A direct request to the container port is not.

The default Caddyfile uses `tls internal`. On first start, Caddy creates a local CA in the `caddy-data` volume. Export the root and trust it on technician machines:

```bash
docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt ./caddy-root.crt
```

Point internal DNS for `PORTAL_HOST` at this machine and open `https://ipad-tech.example.org`.

To use a certificate from your own CA instead, put `cert.pem` and `key.pem` in `./certs` and set `CADDYFILE=./deploy/Caddyfile`. That file loads `/certs/cert.pem` and `/certs/key.pem`. The `certs` directory is mounted read-only. The portal config is mounted read-only at `/app/config.yaml`. Audit and application logs go to the `portal-logs` volume, which is `/app/logs` in the container. To bind-mount a host directory instead, replace the `portal-logs` volume on the portal service with `./logs:/app/logs`.

Restart policy is `unless-stopped` on both services.

Demo against the mock, which is the same image with a different command:

```bash
cp config.docker.example.yaml config.yaml
docker compose -f docker-compose.yml -f docker-compose.mock.yml up --build
```

The overlay sets `ITP_INSECURE_TEST_MODE=true` and waits until the mock's `/health` responds. The mock listens on `0.0.0.0` inside the Compose network and its port is not published on the host. On the sign-in page, the Jamf Pro server is `http://mock:8742`. Sign in with the mock accounts from the local-run section. Trust the Caddy CA the same way, or the browser will warn about the certificate. The red Insecure test mode banner is expected for this demo.

Keep a single portal container. A second replica would have its own memory and would not see the first replica's sessions.

## Audit log

Each change is one JSON object per line: `timestamp`, `username`, `jamfHost`, `deviceId`, `serialNumber`, `action`, `before`, `after`, `result`, `detail`. `jamfHost` is the Jamf server hostname for that sign-in, with the port when it is not the default. `result` is `success`, `denied`, or `error`. The active file is append-only. Rotation renames it; files older than `audit.retention_days` are removed. Passwords, tokens, and the Jamf URL's userinfo are not written. The Jamf address itself is not written anywhere except this host field in the log and the application log line for a successful sign-in.

Writers take an exclusive lock on a sibling file (`<name>.lock`) before they rotate or append, so several desktop sessions can share one UNC file. On Windows the lock is a byte-range lock. On Linux it is `flock`. Retention still deletes rotated files older than `audit.retention_days` while that lock is held. The desktop app is the one that falls back to `%LOCALAPPDATA%\ipad-tech-portal\logs\audit.jsonl` when the configured path cannot be opened. The service and the container have no fallback: a bad path fails startup, which is what you want from a host you administer.

## Limitations

- Sessions are in memory. The Jamf token and the Jamf server address live only there. Logout, the idle timeout (15 minutes by default), and a process restart drop both. Nothing about the sign-in is written to disk except the logs. Run one process, not several workers, and not a web farm.
- The sign-in page asks for the Jamf server URL every time. A query string, a bookmark, or `ITP_JAMF_URL` cannot fill it in. Only `https://` URLs are accepted, with the path and a trailing slash removed, unless insecure test mode is on.
- The web and Docker sign-in is refused unless the page is HTTPS. `X-Forwarded-Proto: https` from the reverse proxy counts. The Windows desktop window is the exception, because it is HTTP on `127.0.0.1`. Its Jamf URL still has to be `https://`.
- Idle timeout is enforced on the next request. Abandoned sessions are dropped when any request touches the store. There is no timer thread.
- Search is an exact serial or an exact asset tag. It is not a substring search, which keeps a 2,000-device lookup to one Jamf query. Duplicate asset tags return a picker.
- Only iPad inventory records are shown. iPhones and other mobile devices are refused.
- Room is a string. Jamf does not provide a room list, so the room field is not a dropdown.
- Static group *membership for one device* is read from the Classic API. Updates go through the Pro API. The Pro API's static-group list does not include members, and returning members of every group would pull the whole fleet.
- If the Classic membership call is down, the portal asks the Pro API once per visible group. Keep the allowlist or prefix small.
- Group PATCH sends a delta (`selected` true or false) for the groups this portal is allowed to show. Other static groups on the device are not removed. The page says how many of those exist, without naming them.
- Update Inventory is offered after location, user, and asset-tag saves. Static group membership is already effective in Jamf without a device check-in.
- MDM commands are queued. The page does not wait for the iPad to check in, and it does not poll command status.
- Erase Device does not send a macOS PIN or a Return to Service profile. Lost Mode, Restart, and Erase still require whatever Apple and Jamf require of that device (supervised, managed, and a management id).
- Privileges are read at login. A privilege removed in Jamf afterward still fails the live call with a permission message. The hidden buttons update at the next sign-in.
- `cookie_secure: false` is for the local HTTP mock and for the desktop window, which loads `http://127.0.0.1`. The IIS site and the Docker site use HTTPS and `cookie_secure: true`. The desktop process forces the flag off even if the file says true. Insecure test mode also leaves the Secure flag off when the page itself is HTTP, so the lab session can stick. The cookie value is still only the opaque session id.
- Insecure test mode is off unless `ITP_INSECURE_TEST_MODE=true` (Docker) or `insecure_test_mode: true` in the admin-only Windows config. The page cannot turn it on. When it is on, a red Insecure test mode banner is on every page.
- The mock's usernames and passwords are fake and exist only in `mock_jamf`. Point a production config at your Jamf server and have technicians use their own accounts.
- The desktop launch token lives in process memory and in an HttpOnly cookie. It is not written to `config.yaml`. uvicorn access logs are off for that process so the token in the first URL is not written to disk.
- Other RDS sessions can open TCP to `127.0.0.1` on this host. Requests without this process's launch token get 404, including `/api/health`. The process does not listen on the LAN.
- If the audit share is down at startup, the desktop warning stays up for that run. Lines written to the local fallback are not copied to the share later.
- A per-user audit file (`{user}` in the path) is purged only when that file is written again.
- Docker runs one portal container. Scaling it would split sessions across processes. The published port is 8741, and the logs are a bind mount at `/opt/dockers/appdata/ipad-tech-portal/logs`. The image sets `ITP_HOST=0.0.0.0` so that port accepts connections from the reverse proxy without a compose environment variable.
- The prebuilt image is published from GitHub (`github.com/Marlonso189/ipad-tech-portal`), not from Cursor Origin. Until that push exists, `deploy/docker-compose.yml` has nothing to pull. `.github/workflows/publish-image.yml` and `scripts/publish-image.sh` are the two ways to create it.
- The Windows executable and the Inno Setup installer are built on Windows. The workflow is `.github/workflows/desktop.yml`. A Linux checkout can run the tests and the container build, and it cannot produce `ipad-tech-portal.exe`. That desktop workflow also runs on GitHub, after the repository is there.
- The desktop window needs the WebView2 Runtime on the RDS host.
