const state = {
  csrf: "",
  features: {},
  updateInventoryDefault: true,
  idleTimeoutMinutes: 15,
  insecureTestMode: false,
  departments: null,
  buildings: null,
  groups: null,
  device: null,
  busy: false,
};

// The Jamf server is typed on every sign-in. It is never read from the address
// bar, from storage, or from a cookie. The only cookie is the opaque session id.

const loginView = document.querySelector("#login-view");
const appView = document.querySelector("#app-view");
const sessionBar = document.querySelector("#session-bar");
const who = document.querySelector("#who");
const results = document.querySelector("#results");
const notice = document.querySelector("#notice");
const loginAlert = document.querySelector("#login-alert");
const confirmDialog = document.querySelector("#confirm-dialog");
const lostDialog = document.querySelector("#lost-dialog");
const wipeDialog = document.querySelector("#wipe-dialog");

document.querySelector("#login-form").addEventListener("submit", onLogin);
document.querySelector("#jamf-url").addEventListener("input", scheduleHostPreview);
document.querySelector("#logout").addEventListener("click", onLogout);
document.querySelector("#search-form").addEventListener("submit", onSearch);
document.querySelector("#confirm-cancel").addEventListener("click", () => confirmDialog.close("cancel"));
document.querySelector("#lost-cancel").addEventListener("click", () => lostDialog.close("cancel"));
document.querySelector("#wipe-cancel").addEventListener("click", () => wipeDialog.close("cancel"));
document.querySelector("#confirm-form").addEventListener("submit", (event) => {
  event.preventDefault();
  confirmDialog.close("confirm");
});
document.querySelector("#lost-form").addEventListener("submit", onLostSubmit);
document.querySelector("#wipe-form").addEventListener("submit", onWipeSubmit);
document.querySelector("#wipe-input").addEventListener("input", () => {
  const expected = wipeDialog.dataset.serial || "";
  document.querySelector("#wipe-ok").disabled = document.querySelector("#wipe-input").value !== expected;
});

boot();

async function boot() {
  try {
    const data = await api("/api/bootstrap");
    applySession(data);
  } catch (error) {
    showLogin(error.message);
  }
}

function applySession(data) {
  if (data.csrfToken) state.csrf = data.csrfToken;
  state.features = data.features || {};
  state.updateInventoryDefault = Boolean(data.updateInventoryDefault);
  state.idleTimeoutMinutes = data.idleTimeoutMinutes || 15;
  if ("insecureTestMode" in data) {
    state.insecureTestMode = Boolean(data.insecureTestMode);
    document.querySelector("#insecure-banner").hidden = !state.insecureTestMode;
  }
  if ("auditWarning" in data) showAuditWarning(data.auditWarning);
  document.querySelector("#foot").textContent =
    `Signed-in sessions idle out after ${state.idleTimeoutMinutes} minutes. Changes are written to the audit log and run with your own Jamf privileges.`;
  if (data.pageHttpsRequired && data.pageIsHttps === false) {
    showLogin(data.httpsRequiredMessage || "Sign-in is only available over HTTPS. Open the HTTPS address for this portal.");
    setLoginEnabled(false);
    return;
  }
  setLoginEnabled(true);
  if (data.authenticated) {
    who.textContent = data.jamfHost ? `${data.username} · ${data.jamfHost}` : data.username;
    sessionBar.hidden = false;
    loginView.hidden = true;
    appView.hidden = false;
    loginAlert.hidden = true;
  } else {
    if (data.expired) clearSignInForm();
    showLogin(data.expired ? "Your session timed out. Sign in again." : "");
  }
}

function showAuditWarning(text) {
  const node = document.querySelector("#audit-warning");
  if (!text) {
    node.hidden = true;
    node.textContent = "";
    return;
  }
  node.hidden = false;
  node.textContent = text;
}

function showLogin(message) {
  state.device = null;
  state.departments = null;
  state.buildings = null;
  state.groups = null;
  sessionBar.hidden = true;
  appView.hidden = true;
  loginView.hidden = false;
  if (message) {
    loginAlert.hidden = false;
    loginAlert.textContent = message;
  }
}

let hostPreviewTimer = 0;
let hostPreviewSeq = 0;

function scheduleHostPreview() {
  window.clearTimeout(hostPreviewTimer);
  hostPreviewTimer = window.setTimeout(() => {
    refreshHostPreview();
  }, 200);
}

async function refreshHostPreview() {
  const raw = document.querySelector("#jamf-url").value.trim();
  const node = document.querySelector("#jamf-host");
  const seq = ++hostPreviewSeq;
  if (!raw) {
    node.hidden = true;
    node.textContent = "";
    return null;
  }
  try {
    const data = await api("/api/jamf-url/preview", { method: "POST", body: { jamfUrl: raw } });
    if (seq !== hostPreviewSeq) return data;
    showJamfHost(data.host);
    return data;
  } catch (error) {
    if (seq !== hostPreviewSeq) return null;
    node.hidden = false;
    node.textContent = error.message;
    return null;
  }
}

function showJamfHost(host) {
  const node = document.querySelector("#jamf-host");
  node.hidden = false;
  node.textContent = "Jamf server: ";
  const name = document.createElement("strong");
  name.textContent = host;
  node.append(name);
}

function setLoginEnabled(enabled) {
  document.querySelectorAll("#login-form input, #login-form button").forEach((node) => {
    node.disabled = !enabled;
  });
}

function clearSignInForm() {
  document.querySelector("#jamf-url").value = "";
  document.querySelector("#username").value = "";
  document.querySelector("#password").value = "";
  const node = document.querySelector("#jamf-host");
  node.hidden = true;
  node.textContent = "";
}

async function onLogin(event) {
  event.preventDefault();
  const button = document.querySelector("#login-button");
  const jamfUrl = document.querySelector("#jamf-url").value.trim();
  const username = document.querySelector("#username").value.trim();
  const password = document.querySelector("#password").value;
  button.disabled = true;
  try {
    const preview = await refreshHostPreview();
    if (!preview || !preview.host) return;
    showJamfHost(preview.host);
    await new Promise((resolve) => {
      window.requestAnimationFrame(() => window.requestAnimationFrame(resolve));
    });
    const data = await api("/api/login", { method: "POST", body: { jamfUrl, username, password } });
    clearSignInForm();
    applySession(data);
    document.querySelector("#query").focus();
  } catch (error) {
    document.querySelector("#password").value = "";
    showLogin(error.message);
  } finally {
    if (!document.querySelector("#jamf-url").disabled) button.disabled = false;
  }
}

async function onLogout() {
  try {
    await api("/api/logout", { method: "POST", body: {} });
  } catch (_error) {
    /* The local session is cleared even if Jamf could not be reached. */
  }
  clearSignInForm();
  const data = await api("/api/bootstrap");
  applySession(data);
  showLogin();
}

async function onSearch(event) {
  event.preventDefault();
  if (state.busy) return;
  const query = document.querySelector("#query").value.trim();
  setNotice("");
  setBusy(true);
  try {
    const data = await api("/api/devices?q=" + encodeURIComponent(query));
    await ensureLists();
    if (data.device) {
      renderDevice(data.device);
    } else {
      renderMatches(data.matches || [], data.message);
    }
  } catch (error) {
    setNotice(error.message, "error");
  } finally {
    setBusy(false);
  }
}

async function openDevice(id) {
  setBusy(true);
  try {
    await ensureLists();
    const data = await api("/api/devices/" + encodeURIComponent(id));
    renderDevice(data.device);
  } catch (error) {
    setNotice(error.message, "error");
  } finally {
    setBusy(false);
  }
}

async function ensureLists() {
  const jobs = [];
  if (state.features.update_location && !state.departments) {
    jobs.push(api("/api/departments").then((data) => { state.departments = data.departments; }));
    jobs.push(api("/api/buildings").then((data) => { state.buildings = data.buildings; }));
  }
  if (state.features.view_groups && !state.groups) {
    jobs.push(api("/api/groups").then((data) => { state.groups = data.groups; }));
  }
  if (!jobs.length) return;
  const settled = await Promise.allSettled(jobs);
  const failed = settled.find((item) => item.status === "rejected");
  if (failed) setNotice(failed.reason.message, "error");
}

function renderMatches(matches, message) {
  const card = el("section", { className: "card" });
  card.append(el("h2", { text: message || "More than one iPad matched." }));
  const list = el("div", { className: "match-list" });
  matches.forEach((match) => {
    const button = el("button", { className: "match", type: "button" });
    button.append(el("strong", { text: match.name || "Unnamed iPad" }));
    button.append(el("span", { text: `${match.model || "iPad"} · ${match.serialNumber} · ${match.assetTag || "No asset tag"}` }));
    button.addEventListener("click", () => openDevice(match.id));
    list.append(button);
  });
  card.append(list);
  results.replaceChildren(card);
}

function renderDevice(device) {
  state.device = device;
  const card = el("section", { className: "card" });
  const identity = el("div", { className: "identity" });
  const title = el("div");
  title.append(el("h2", { text: device.name || "Unnamed iPad" }));
  title.append(el("p", { text: [device.model, device.osVersion ? `iPadOS ${device.osVersion}` : ""].filter(Boolean).join(" · ") }));
  identity.append(title);
  const pills = el("div", { className: "pills" });
  pills.append(pill(device.managed ? "Managed" : "Not managed", device.managed ? "on" : "bad"));
  pills.append(pill(device.supervised ? "Supervised" : "Not supervised", device.supervised ? "on" : "off"));
  pills.append(pill(device.lostModeEnabled ? "Lost Mode" : "Not in Lost Mode", device.lostModeEnabled ? "off" : "on"));
  identity.append(pills);
  card.append(identity);

  const facts = el("dl", { className: "facts" });
  fact(facts, "Serial", device.serialNumber, true);
  fact(facts, "Asset tag", device.assetTag || "—", true);
  fact(facts, "Assigned user", device.username || "—");
  fact(facts, "Full name", device.fullName || "—");
  fact(facts, "Email", device.email || "—");
  fact(facts, "Department", device.department || "—");
  fact(facts, "Building", device.building || "—");
  fact(facts, "Room", device.room || "—");
  fact(facts, "Last inventory", formatWhen(device.lastInventory));
  card.append(facts);

  const blocks = [card];
  if (state.features.update_location) blocks.push(locationCard(device));
  if (state.features.update_user || state.features.update_asset_tag) blocks.push(userCard(device));
  if (state.features.view_groups) blocks.push(groupCard(device));
  const commands = commandCard(device);
  if (commands) blocks.push(commands);
  if (state.features.wipe) blocks.push(wipeCard(device));
  results.replaceChildren(...blocks);
}

function locationCard(device) {
  const card = el("section", { className: "card" });
  card.append(el("h2", { text: "Department, building, and room" }));
  card.append(el("p", { className: "hint", text: "Department and building come from Jamf. Room is free text because Jamf has no room list." }));
  const form = el("form");
  const grid = el("div", { className: "grid-3" });
  const department = selectList("Department", state.departments || [], device.departmentId, device.department);
  const building = selectList("Building", state.buildings || [], device.buildingId, device.building);
  const room = field("Room", "text", device.room || "");
  grid.append(department.label, building.label, room.label);
  form.append(grid);
  const inventory = inventoryBox();
  form.append(inventory.label);
  const save = el("button", { className: "primary", type: "submit", text: "Save location" });
  form.append(save);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    await saveChange(save, `/api/devices/${device.id}/location`, "PATCH", {
      departmentId: department.input.value,
      buildingId: building.input.value,
      room: room.input.value,
      updateInventory: inventory.input.checked,
    }, "Location saved.");
  });
  card.append(form);
  return card;
}

function userCard(device) {
  const card = el("section", { className: "card" });
  card.append(el("h2", { text: "Assigned user and asset tag" }));
  if (state.features.update_user) {
    const form = el("form");
    const grid = el("div", { className: "grid-3" });
    const username = field("Username", "text", device.username || "");
    const fullName = field("Full name", "text", device.fullName || "");
    const email = field("Email", "email", device.email || "");
    grid.append(username.label, fullName.label, email.label);
    form.append(grid);
    const inventory = inventoryBox();
    form.append(inventory.label);
    const save = el("button", { className: "primary", type: "submit", text: "Save assigned user" });
    form.append(save);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      await saveChange(save, `/api/devices/${device.id}/user`, "PATCH", {
        username: username.input.value,
        fullName: fullName.input.value,
        email: email.input.value,
        updateInventory: inventory.input.checked,
      }, "Assigned user saved.");
    });
    card.append(form);
  }
  if (state.features.update_asset_tag) {
    const form = el("form");
    const asset = field("Asset tag", "text", device.assetTag || "");
    form.append(asset.label);
    const inventory = inventoryBox();
    form.append(inventory.label);
    const save = el("button", { className: "primary", type: "submit", text: "Save asset tag" });
    form.append(save);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      await saveChange(save, `/api/devices/${device.id}/asset-tag`, "PATCH", {
        assetTag: asset.input.value,
        updateInventory: inventory.input.checked,
      }, "Asset tag saved.");
    });
    card.append(form);
  }
  return card;
}

function groupCard(device) {
  const card = el("section", { className: "card" });
  card.append(el("h2", { text: "Static groups" }));
  if (device.groupsError) {
    card.append(el("p", { className: "notice error", text: device.groupsError }));
    return card;
  }
  const groups = state.groups || [];
  if (!groups.length) {
    card.append(el("p", { text: "No static groups are available with the current allowlist or name prefix." }));
    return card;
  }
  const memberIds = new Set((device.staticGroups || []).map((group) => group.id));
  const form = el("form");
  const list = el("div", { className: "group-list" });
  const boxes = [];
  groups.forEach((group) => {
    const label = el("label", { className: "check" });
    const input = el("input");
    input.type = "checkbox";
    input.checked = memberIds.has(group.id);
    input.disabled = !state.features.update_groups;
    label.append(input, document.createTextNode(group.name));
    list.append(label);
    boxes.push({ id: group.id, input });
  });
  form.append(list);
  if (device.otherStaticGroupCount) {
    form.append(el("p", {
      className: "hint",
      text: `This iPad is also in ${device.otherStaticGroupCount} other static group${device.otherStaticGroupCount === 1 ? "" : "s"} that this portal cannot change.`,
    }));
  }
  if (state.features.update_groups) {
    const save = el("button", { className: "primary", type: "submit", text: "Save groups" });
    form.append(save);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const groupIds = boxes.filter((box) => box.input.checked).map((box) => box.id);
      await saveChange(save, `/api/devices/${device.id}/groups`, "POST", { groupIds }, "Group membership saved.");
    });
  } else {
    form.append(el("p", { className: "hint", text: "Your Jamf account can view these groups but cannot change membership." }));
  }
  card.append(form);
  return card;
}

function commandCard(device) {
  const features = state.features;
  const buttons = [];
  if (features.restart) buttons.push(["Restart device", "restart", true]);
  if (features.update_inventory) buttons.push(["Update inventory", "update-inventory", false]);
  if (features.clear_passcode) buttons.push(["Clear passcode", "clear-passcode", true]);
  if (features.enable_lost_mode) buttons.push(["Enable Lost Mode", "lost-on", false]);
  if (features.disable_lost_mode) buttons.push(["Disable Lost Mode", "lost-off", true]);
  if (!buttons.length) return null;
  const card = el("section", { className: "card" });
  card.append(el("h2", { text: "MDM commands" }));
  if (!device.managed) {
    card.append(el("p", { text: "This iPad is not managed, so Jamf cannot queue an MDM command." }));
  }
  const row = el("div", { className: "command-row" });
  buttons.forEach(([label, action, needsConfirm]) => {
    const button = el("button", { type: "button", className: "secondary", text: label });
    button.disabled = !device.managed;
    button.addEventListener("click", () => runCommand(device, action, label, needsConfirm, button));
    row.append(button);
  });
  card.append(row);
  return card;
}

function wipeCard(device) {
  const card = el("section", { className: "card danger-zone" });
  card.append(el("h2", { text: "Erase device" }));
  card.append(el("p", { text: "Wipe is available because your Jamf account has the erase privilege. You must type the serial number before it is queued." }));
  const button = el("button", { type: "button", className: "danger", text: "Erase device" });
  button.disabled = !device.managed;
  button.addEventListener("click", () => openWipe(device));
  card.append(button);
  return card;
}

async function runCommand(device, action, label, needsConfirm, button) {
  if (action === "lost-on") {
    lostDialog.dataset.deviceId = device.id;
    document.querySelector("#lost-message").value = "";
    document.querySelector("#lost-phone").value = "";
    document.querySelector("#lost-footnote").value = "";
    lostDialog.showModal();
    document.querySelector("#lost-message").focus();
    return;
  }
  if (needsConfirm) {
    const ok = await askConfirm(label, `Queue ${label} for ${device.name || "this iPad"}?`);
    if (!ok) return;
  }
  const path = action === "lost-off"
    ? `/api/devices/${device.id}/commands/lost-mode/disable`
    : `/api/devices/${device.id}/commands/${action}`;
  await saveChange(button, path, "POST", {}, `${label} was queued.`);
}

async function onLostSubmit(event) {
  event.preventDefault();
  const deviceId = lostDialog.dataset.deviceId;
  const button = document.querySelector("#lost-ok");
  button.disabled = true;
  try {
    const data = await api(`/api/devices/${deviceId}/commands/lost-mode/enable`, {
      method: "POST",
      body: {
        message: document.querySelector("#lost-message").value,
        phone: document.querySelector("#lost-phone").value,
        footnote: document.querySelector("#lost-footnote").value,
      },
    });
    lostDialog.close();
    if (data.device) renderDevice(data.device);
    setNotice("Enable Lost Mode was queued.");
  } catch (error) {
    setNotice(error.message, "error");
    lostDialog.close();
  } finally {
    button.disabled = false;
  }
}

function openWipe(device) {
  wipeDialog.dataset.serial = device.serialNumber;
  wipeDialog.dataset.deviceId = device.id;
  document.querySelector("#wipe-serial").textContent = device.serialNumber;
  const input = document.querySelector("#wipe-input");
  input.value = "";
  document.querySelector('input[name="wipe-type"][value="full"]').checked = true;
  document.querySelector("#wipe-ok").disabled = true;
  wipeDialog.showModal();
  input.focus();
}

async function onWipeSubmit(event) {
  event.preventDefault();
  const button = document.querySelector("#wipe-ok");
  button.disabled = true;
  try {
    const checked = document.querySelector('input[name="wipe-type"]:checked');
    const wipeType = checked ? checked.value : "full";
    const data = await api(`/api/devices/${wipeDialog.dataset.deviceId}/commands/erase`, {
      method: "POST",
      body: { confirmSerial: document.querySelector("#wipe-input").value, wipeType },
    });
    wipeDialog.close();
    if (data.device) renderDevice(data.device);
    setNotice(wipeType === "return_to_service" ? "Erase Device with Return to Service was queued." : "Erase Device was queued.");
  } catch (error) {
    setNotice(error.message, "error");
  } finally {
    button.disabled = document.querySelector("#wipe-input").value !== (wipeDialog.dataset.serial || "");
  }
}

function askConfirm(title, body) {
  document.querySelector("#confirm-title").textContent = title;
  document.querySelector("#confirm-body").textContent = body;
  confirmDialog.showModal();
  return new Promise((resolve) => {
    confirmDialog.addEventListener("close", () => resolve(confirmDialog.returnValue === "confirm"), { once: true });
  });
}

async function saveChange(button, path, method, body, success) {
  if (state.busy) return;
  setBusy(true);
  button.disabled = true;
  try {
    const data = await api(path, { method, body });
    if (data.device) renderDevice(data.device);
    const extra = data.warning || data.notice || "";
    setNotice(extra ? `${success} ${extra}` : success, data.warning ? "error" : "ok");
  } catch (error) {
    setNotice(error.message, "error");
  } finally {
    setBusy(false);
    button.disabled = false;
  }
}

function inventoryBox() {
  const label = el("label", { className: "check" });
  const input = el("input");
  input.type = "checkbox";
  input.checked = state.updateInventoryDefault;
  label.append(input, document.createTextNode("Queue Update Inventory after saving"));
  return { label, input };
}

function selectList(caption, items, currentId, currentName) {
  const label = el("label", { text: caption });
  const input = el("select");
  input.append(el("option", { value: "", text: "Unassigned" }));
  let found = !currentId;
  items.forEach((item) => {
    input.append(el("option", { value: item.id, text: item.name }));
    if (item.id === currentId) found = true;
  });
  if (currentId && !found) {
    input.append(el("option", { value: currentId, text: currentName || currentId }));
  }
  input.value = currentId || "";
  label.append(input);
  return { label, input };
}

function field(caption, type, value) {
  const label = el("label", { text: caption });
  const input = el("input", { type, value });
  label.append(input);
  return { label, input };
}

function fact(list, caption, value, mono) {
  const wrap = el("div");
  wrap.append(el("dt", { text: caption }));
  wrap.append(el("dd", { className: mono ? "mono" : "", text: value || "—" }));
  list.append(wrap);
}

function pill(text, kind) {
  return el("span", { className: `pill ${kind}`, text });
}

function formatWhen(value) {
  if (!value) return "No inventory yet";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function setNotice(message, kind) {
  notice.hidden = !message;
  notice.textContent = message || "";
  notice.className = kind === "error" ? "notice error" : "notice";
}

function setBusy(busy) {
  state.busy = busy;
  document.querySelector("#search-button").disabled = busy;
}

async function api(path, options = {}) {
  const method = options.method || "GET";
  const headers = { Accept: "application/json" };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET") headers["X-CSRF-Token"] = state.csrf;
  let response;
  try {
    response = await fetch(path, {
      method,
      headers,
      credentials: "same-origin",
      body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
    });
  } catch (_error) {
    throw new Error("The portal could not be reached.");
  }
  const data = await response.json().catch(() => ({}));
  const warning = response.headers.get("X-Audit-Warning");
  if (warning) showAuditWarning(warning);
  if (data.csrfToken) state.csrf = data.csrfToken;
  if (!response.ok) {
    if (response.status === 401 && data.error !== "credentials") {
      applySession({ authenticated: false, csrfToken: state.csrf });
      showLogin(data.message || "Sign in again.");
    }
    throw new Error(data.message || "The request failed.");
  }
  return data;
}

function el(tag, attrs = {}) {
  const node = document.createElement(tag);
  if (attrs.className) node.className = attrs.className;
  if (attrs.type) node.type = attrs.type;
  if (attrs.value !== undefined) node.value = attrs.value;
  if (attrs.text) node.textContent = attrs.text;
  return node;
}
