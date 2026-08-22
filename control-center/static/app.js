const COLOR_ACCENT_NAMES = new Set(["blue", "red", "green", "orange", "purple"]);
const MAX_PROVIDER_PROMPT_BYTES = 16 * 1024;
const MAX_PROJECT_BRIEF_CHARS = 20000;
const MAX_SEED_NOTES_CHARS = 12000;
const MAX_CHAT_ATTACHMENTS = 20;
const MAX_CHAT_ATTACHMENT_BYTES = 20 * 1024 * 1024;
const MAX_CHAT_ATTACHMENTS_TOTAL_BYTES = 20 * 1024 * 1024;
const MAX_PROJECT_ASSETS = 500;
const MAX_PROJECT_ASSET_BYTES = 15 * 1024 * 1024;
const MAX_PROJECT_ASSETS_TOTAL_BYTES = 20 * 1024 * 1024;
const MAX_PROJECT_DROP_ENTRIES = 2000;
const MAX_PROJECT_DROP_DEPTH = 32;
const AUTH_TOKEN_PATTERN = /^[A-Za-z0-9_-]{16,128}$/;
const AUTH_STORAGE_KEY = "awesome-webkit-control-token";

function safeLocalStorageGet(key) {
  try { return window.localStorage.getItem(key); } catch (_error) { return null; }
}

function safeLocalStorageSet(key, value) {
  try { window.localStorage.setItem(key, value); } catch (_error) {}
}

function controlCenterToken() {
  const fragment = new URLSearchParams(window.location.hash.replace(/^#/, ""));
  const launched = fragment.get("token") || "";
  if (AUTH_TOKEN_PATTERN.test(launched)) {
    try { window.sessionStorage.setItem(AUTH_STORAGE_KEY, launched); } catch (_error) {}
    window.history.replaceState(null, "", window.location.pathname);
    return launched;
  }
  try {
    const stored = window.sessionStorage.getItem(AUTH_STORAGE_KEY) || "";
    return AUTH_TOKEN_PATTERN.test(stored) ? stored : "";
  } catch (_error) {
    return "";
  }
}

const CONTROL_CENTER_TOKEN = controlCenterToken();

const DEFAULT_SETTINGS = {
  dictationMode: "speech",
  interactionMode: "browse-default",
  toggleHotkey: "KeyC",
  dictateHotkey: "KeyV",
};
const RESERVED_HOTKEYS = new Set([
  "AltLeft", "AltRight", "ControlLeft", "ControlRight",
  "MetaLeft", "MetaRight", "ShiftLeft", "ShiftRight", "Escape",
]);
const REASONING_LEVELS = {
  codex: ["low", "medium", "high", "xhigh"],
  claude: ["low", "medium", "high", "xhigh", "max"],
};

const state = {
  providers: [],
  projects: [],
  system: {},
  settings: { ...DEFAULT_SETTINGS },
  selectedProjectId: null,
  homeSelected: false,
  chatSessionId: null,
  chatCursor: 0,
  projectMode: "create",
  confirmCallback: null,
  pendingAttachments: [],
  chatAttachmentReadsPending: 0,
  chatSendInFlight: false,
  projectsSignature: "",
  createStep: 1,
  onboardingAssets: [],
  seedSessionId: null,
  seedProjectId: null,
  seedCurrentId: null,
  seedSelected: new Set(),
  seedSignature: "",
  seedData: null,
  seedSelectionInFlight: false,
  chatGeneration: 0,
  chatReasoningRequest: 0,
  chatReturnFocus: null,
  seedGeneration: 0,
  projectGeneration: 0,
  projectSaveInFlight: false,
  projectAssetOperationsPending: 0,
  confirmGeneration: 0,
  confirmBusy: false,
  settingsGeneration: 0,
  settingsSaveInFlight: false,
  chatTurn: null,
};
let hotkeyDraft = { toggleHotkey: "KeyC", dictateHotkey: "KeyV" };
let hotkeyCapture = null;
let projectsRefreshPromise = null;
let projectsRefreshQueued = false;
let eventsPollPromise = null;
let eventsPollQueued = false;
let seedPollPromise = null;
let seedPollQueued = false;
const previewTabs = new Map();

const $ = (selector) => document.querySelector(selector);

async function api(path, options = {}) {
  if (!CONTROL_CENTER_TOKEN) throw new Error("Open the Control Center with its launcher.");
  const request = {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
      "X-WKCC-Token": CONTROL_CENTER_TOKEN,
    },
  };
  if (request.body && typeof request.body !== "string") request.body = JSON.stringify(request.body);
  const response = await fetch(path, request);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(data.error || `Request failed (${response.status})`);
    error.status = response.status;
    error.details = data.details && typeof data.details === "object" ? data.details : {};
    throw error;
  }
  return data;
}

function toast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { node.hidden = true; }, 3800);
}

function reservePreviewWindow(name) {
  const tab = window.open("about:blank", name);
  if (tab) tab.opener = null;
  return tab;
}

function navigatePreviewWindow(tab, url) {
  if (!tab) return false;
  tab.location.replace(url);
  tab.focus();
  return true;
}

function rememberPreviewWindow(session, tab) {
  if (session?.id && session.kind !== "support" && tab) {
    previewTabs.set(session.id, {
      tab,
      revision: Number.isInteger(session.previewRevision) ? session.previewRevision : 0,
    });
  }
}

function refreshChangedPreviewWindows(projects) {
  const sessions = new Map();
  (projects || []).forEach((project) => {
    (project.sessions || []).forEach((session) => sessions.set(session.id, session));
  });
  previewTabs.forEach((entry, sessionId) => {
    const session = sessions.get(sessionId);
    if (!session) return;
    const holder = entry?.tab ? entry : { tab: entry, revision: 0 };
    const revision = Number.isInteger(session.previewRevision) ? session.previewRevision : 0;
    if (revision <= holder.revision) return;
    try {
      if (holder.tab && !holder.tab.closed) holder.tab.location.replace(session.previewUrl);
    } catch (_error) {}
    holder.revision = revision;
    previewTabs.set(sessionId, holder);
  });
}

function closeFinishedPreviewWindows(projects) {
  const liveSessionIds = new Set();
  (projects || []).forEach((project) => {
    (project.sessions || []).forEach((session) => liveSessionIds.add(session.id));
  });
  previewTabs.forEach((entry, sessionId) => {
    if (liveSessionIds.has(sessionId)) return;
    previewTabs.delete(sessionId);
    const tab = entry?.tab || entry;
    try {
      if (tab && !tab.closed) tab.close();
    } catch (_error) {}
  });
}

function setBusy(button, busy, label) {
  button.disabled = busy;
  button.setAttribute("aria-busy", String(busy));
  const existingContent = button.querySelector(":scope > .busy-preserved-content");
  const existingLabel = button.querySelector(":scope > .busy-status-label");
  if (busy && !existingContent) {
    const preserved = document.createElement("span");
    preserved.className = "busy-preserved-content";
    preserved.hidden = true;
    while (button.firstChild) preserved.appendChild(button.firstChild);
    const status = document.createElement("span");
    status.className = "busy-status-label";
    status.textContent = label;
    button.append(preserved, status);
  } else if (busy && existingLabel) {
    existingLabel.textContent = label;
  } else if (!busy && existingContent) {
    const children = [...existingContent.childNodes];
    existingLabel?.remove();
    existingContent.replaceWith(...children);
  }
}

function reasoningLevels(provider) {
  return REASONING_LEVELS[provider] || REASONING_LEVELS.codex;
}

function fillReasoning(select, provider, value) {
  select.replaceChildren();
  reasoningLevels(provider).forEach((level) => {
    const option = document.createElement("option");
    option.value = level;
    option.textContent = level === "xhigh" ? "Extra high" : level[0].toUpperCase() + level.slice(1);
    select.appendChild(option);
  });
  select.value = reasoningLevels(provider).includes(value) ? value : "medium";
}

function hotkeyLabel(code) {
  if (/^Key[A-Z]$/.test(code)) return code.slice(3);
  if (/^Digit[0-9]$/.test(code)) return code.slice(5);
  return ({ Backquote: "`", Space: "Space", Enter: "↵", ArrowUp: "↑", ArrowDown: "↓", ArrowLeft: "←", ArrowRight: "→" })[code] || code;
}

function renderShortcutGuide() {
  $("#guideToggleKey").textContent = hotkeyLabel(state.settings.toggleHotkey || "KeyC");
  $("#guideDictateKey").textContent = hotkeyLabel(state.settings.dictateHotkey || "KeyV");
  const optionAction = state.settings.interactionMode === "draw-default" ? "Click" : "Draw";
  $("#guideOptionAction").textContent = optionAction;
  const optionLabel = state.system.platform === "darwin" ? "⌥" : "Alt";
  document.querySelectorAll(".option-key").forEach((node) => { node.textContent = optionLabel; });
}

function renderHotkeySettings() {
  $("#settingsToggleKey").textContent = hotkeyLabel(hotkeyDraft.toggleHotkey);
  $("#settingsDictateKey").textContent = hotkeyLabel(hotkeyDraft.dictateHotkey);
  const selectedInteraction = document.querySelector('input[name="interactionMode"]:checked');
  const interactionMode = selectedInteraction ? selectedInteraction.value : state.settings.interactionMode;
  $("#settingsOptionAction").textContent = interactionMode === "draw-default"
    ? "Use the website"
    : "Draw a rectangle";
  document.querySelectorAll("[data-hotkey-setting]").forEach((button) => {
    const listening = button.dataset.hotkeySetting === hotkeyCapture;
    button.classList.toggle("listening", listening);
    button.querySelector("small").textContent = listening
      ? "Press a key now · Escape cancels"
      : "Click, then press your preferred key";
  });
}

function beginHotkeyCapture(event) {
  hotkeyCapture = event.currentTarget.dataset.hotkeySetting;
  $("#settingsError").textContent = "";
  renderHotkeySettings();
}

function captureHotkey(event) {
  if (!hotkeyCapture || !$("#settingsDialog").open) return;
  event.preventDefault();
  event.stopPropagation();
  if (event.code === "Escape") {
    hotkeyCapture = null;
    renderHotkeySettings();
    return;
  }
  if (!event.code || event.code === "Unidentified" || RESERVED_HOTKEYS.has(event.code)) {
    $("#settingsError").textContent = "Choose a regular key, not a modifier or Escape.";
    return;
  }
  const otherSetting = hotkeyCapture === "toggleHotkey" ? "dictateHotkey" : "toggleHotkey";
  if (hotkeyDraft[otherSetting] === event.code) {
    $("#settingsError").textContent = "Open/close and voice need different keys.";
    return;
  }
  hotkeyDraft[hotkeyCapture] = event.code;
  hotkeyCapture = null;
  $("#settingsError").textContent = "";
  renderHotkeySettings();
}

function renderStatus() {
  const target = $("#systemStatus");
  target.replaceChildren();
  const visibleTools = ["git", ...state.providers].filter((name, index, tools) => (
    tools.indexOf(name) === index
      && (!(state.system[name] || {}).installed || (state.system[name] || {}).supported === false)
  ));
  visibleTools.forEach((name) => {
    const info = state.system[name] || { installed: false };
    const outdatedGit = name === "git" && info.supported === false;
    const minimumGit = typeof info.minimumVersion === "string"
      ? info.minimumVersion.replace(/\.0$/, "")
      : "2.30";
    const pill = document.createElement(name === "git" ? "button" : "span");
    pill.className = "status-pill missing";
    pill.textContent = outdatedGit
      ? `Git ${minimumGit}+ required`
      : `${name === "claude" ? "Claude" : name[0].toUpperCase() + name.slice(1)} missing`;
    pill.title = outdatedGit
      ? `Detected ${info.version || "an older Git version"}. Git ${minimumGit} or newer is required.`
      : info.version || info.path || "Not found on PATH";
    if (name === "git") pill.addEventListener("click", installGit);
    target.appendChild(pill);
  });
}

async function installGit() {
  if (!window.confirm("Open your system's Git installer?")) return;
  try {
    const result = await api("/api/system/install-git", { method: "POST", body: { confirmed: true } });
    toast(result.message);
  } catch (error) { toast(error.message); }
}

async function installShortcut() {
  const button = $("#installShortcutButton");
  setBusy(button, true, "Installing…");
  try {
    const result = await api("/api/system/install-shortcut", { method: "POST", body: {} });
    toast(result.message);
  } catch (error) {
    toast(error.message);
  } finally {
    setBusy(button, false);
  }
}

function openSettings() {
  state.settingsGeneration += 1;
  const capability = state.system.voiceTranscription || { available: false };
  const voiceInput = document.querySelector('input[name="dictationMode"][value="voice-note"]');
  voiceInput.disabled = !capability.available;
  $("#voiceNoteOption").classList.toggle("disabled", !capability.available);
  $("#voiceCapability").textContent = capability.available
    ? `Ready: ${capability.engine}. Recordings stay on this computer.`
    : (capability.help || "Install local Whisper to enable agent voice notes.");
  const selected = document.querySelector(`input[name="dictationMode"][value="${state.settings.dictationMode}"]`)
    || document.querySelector('input[name="dictationMode"][value="speech"]');
  selected.checked = true;
  const interaction = document.querySelector(`input[name="interactionMode"][value="${state.settings.interactionMode}"]`)
    || document.querySelector('input[name="interactionMode"][value="browse-default"]');
  interaction.checked = true;
  document.querySelectorAll('input[name="settingsProvider"]').forEach((input) => {
    input.checked = state.providers.includes(input.value);
  });
  $("#settingsCodexStatus").textContent = state.system.codex?.installed
    ? "Ready on this computer"
    : "CLI not found; install it before adding";
  $("#settingsClaudeStatus").textContent = state.system.claude?.installed
    ? "Ready on this computer"
    : "CLI not found; install it before adding";
  const github = state.system.github || { installed: false, authenticated: false };
  $("#githubConnectionStatus").textContent = github.authenticated
    ? "GitHub CLI is connected. New websites become private GitHub repositories and accepted merges push automatically."
    : "Connect GitHub to Codex or Claude Code, or run gh auth login. Existing GitHub remotes are detected automatically.";
  hotkeyDraft = {
    toggleHotkey: state.settings.toggleHotkey || "KeyC",
    dictateHotkey: state.settings.dictateHotkey || "KeyV",
  };
  hotkeyCapture = null;
  $("#settingsDialogClose").disabled = state.settingsSaveInFlight;
  setBusy($("#saveSettings"), state.settingsSaveInFlight, "Saving…");
  renderHotkeySettings();
  $("#settingsError").textContent = "";
  const settingsForm = $("#settingsForm");
  settingsForm.scrollTop = 0;
  $("#settingsDialog").showModal();
  $("#settingsDialogClose").focus({ preventScroll: true });
  settingsForm.scrollTop = 0;
}

async function saveSettings(event) {
  event.preventDefault();
  if (state.settingsSaveInFlight) return;
  const generation = state.settingsGeneration;
  const dictationInput = document.querySelector('input[name="dictationMode"]:checked');
  const interactionInput = document.querySelector('input[name="interactionMode"]:checked');
  const providers = [...document.querySelectorAll('input[name="settingsProvider"]:checked')]
    .map((input) => input.value);
  const preferences = {
    providers,
    dictationMode: dictationInput ? dictationInput.value : "speech",
    interactionMode: interactionInput ? interactionInput.value : "browse-default",
    toggleHotkey: hotkeyDraft.toggleHotkey,
    dictateHotkey: hotkeyDraft.dictateHotkey,
  };
  const submitted = JSON.stringify({ ...preferences, providers: [...providers].sort() });
  const button = $("#saveSettings");
  $("#settingsError").textContent = "";
  state.settingsSaveInFlight = true;
  $("#settingsDialogClose").disabled = true;
  setBusy(button, true, "Saving…");
  try {
    const result = await api("/api/preferences", {
      method: "POST",
      body: preferences,
    });
    state.providers = result.providers;
    renderStatus();
    state.settings = result.settings;
    renderShortcutGuide();
    if (generation !== state.settingsGeneration || !$("#settingsDialog").open) return;
    const current = JSON.stringify({
      providers: [...document.querySelectorAll('input[name="settingsProvider"]:checked')]
        .map((input) => input.value).sort(),
      dictationMode: document.querySelector('input[name="dictationMode"]:checked')?.value || "speech",
      interactionMode: document.querySelector('input[name="interactionMode"]:checked')?.value || "browse-default",
      toggleHotkey: hotkeyDraft.toggleHotkey,
      dictateHotkey: hotkeyDraft.dictateHotkey,
    });
    if (current !== submitted) {
      $("#settingsError").textContent = "The submitted settings were saved. Review and save your newer edits.";
      return;
    }
    $("#settingsDialog").close();
    const restarted = result.previews.restarted || 0;
    const deferred = result.previews.deferred || 0;
    toast(`Settings saved.${restarted ? ` Restarted ${restarted} preview${restarted === 1 ? "" : "s"}.` : ""}${deferred ? ` ${deferred} busy preview will use it next time.` : ""}`);
  } catch (error) {
    if (generation !== state.settingsGeneration || !$("#settingsDialog").open) return;
    $("#settingsError").textContent = error.message;
  } finally {
    state.settingsSaveInFlight = false;
    if ($("#settingsDialog").open) {
      $("#settingsDialogClose").disabled = false;
      setBusy(button, false);
    }
  }
}

function renderProjects() {
  const list = $("#projectList");
  list.replaceChildren();
  state.projects.forEach((project) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `project-link ${project.id === state.selectedProjectId ? "active" : ""}`;
    if (project.id === state.selectedProjectId) button.setAttribute("aria-current", "page");
    const icon = document.createElement("span");
    icon.className = "project-icon";
    icon.textContent = project.name.slice(0, 2);
    const name = document.createElement("span");
    name.textContent = project.name;
    button.append(icon, name);
    button.addEventListener("click", () => selectProject(project.id));
    list.appendChild(button);
  });
  const hasProjects = state.projects.length > 0;
  $("#emptyState").hidden = hasProjects && !!state.selectedProjectId;
  $("#projectView").hidden = !hasProjects || !state.selectedProjectId;
}

function selectedProject() {
  return state.projects.find((project) => project.id === state.selectedProjectId) || null;
}

function targetBranch(project = selectedProject()) {
  return project?.targetBranch || "main";
}

function projectForSessionId(sessionId) {
  return state.projects.find((project) => (
    project.sessions || []
  ).some((session) => session.id === sessionId)) || null;
}

function targetBranchForSession(session) {
  return targetBranch(projectForSessionId(session?.id));
}

function renderProjectIssue(project = selectedProject()) {
  const panel = $("#projectIssue");
  const issue = project?.issue;
  if (!issue || issue.action !== "handle_with_agent") {
    panel.hidden = true;
    $("#projectIssueMessage").textContent = "";
    setBusy($("#handleProjectIssue"), false);
    return;
  }
  const session = (project.sessions || []).find((item) => (
    item.id === issue.supportSessionId
    && item.kind === "support"
    && ["active", "busy", "merging", "error"].includes(item.status)
  ));
  $("#projectIssueMessage").textContent = issue.message;
  $("#handleProjectIssue").textContent = session ? "Open agent" : "Handle with agent";
  $("#handleProjectIssue").dataset.sessionId = session?.id || "";
  panel.hidden = false;
}

function retainActionableIssue(error, project) {
  const issue = error?.details?.issue;
  if (!project || issue?.action !== "handle_with_agent") return false;
  project.issue = issue;
  if (selectedProject()?.id === project.id) renderProjectIssue(project);
  return true;
}

function selectProject(id) {
  state.homeSelected = false;
  state.selectedProjectId = id;
  renderProjects();
  renderProjectView();
}

function openSessionPreview(session) {
  const tab = reservePreviewWindow(`webkit-${session.projectId}-${session.color}`);
  if (navigatePreviewWindow(tab, session.previewUrl)) {
    rememberPreviewWindow(session, tab);
    return;
  }
  else toast(`Preview ready at ${session.previewUrl}. Allow popups to focus it automatically.`);
}

function renderProjectView() {
  const project = selectedProject();
  if (!project) {
    renderProjectIssue(null);
    return;
  }
  renderProjectIssue(project);
  $("#projectName").textContent = project.name;
  $("#projectPath").textContent = project.path;
  $("#projectProvider").textContent = `${project.provider === "codex" ? "Codex" : "Claude Code"} project${
    project.sourceIntegrationPending ? " · Local checkout sync pending" : ""
  }`;
  const chatSession = currentSession();
  $("#mergeButton").textContent = `Merge to ${
    chatSession ? targetBranchForSession(chatSession) : targetBranch(project)
  }`;
  const github = project.github || {};
  const updateButton = $("#updateProjectWebkitButton");
  const webkitUpdate = project.webkitUpdate;
  updateButton.hidden = !webkitUpdate || webkitUpdate.code !== "webkit_update_required";
  updateButton.textContent = webkitUpdate?.requiredVersion
    ? `Update Webkit to ${webkitUpdate.requiredVersion}`
    : "Update Webkit";
  const pushButton = $("#pushGithubButton");
  pushButton.hidden = !(github.connected && github.unpushed);
  const commits = Number(github.ahead) || 0;
  const pushLabel = commits === 1
    ? "Push 1 commit to GitHub"
    : `Push ${commits} commits to GitHub`;
  $("#pushGithubLabel").textContent = project.sourceIntegrationPending
    ? `Sync local checkout and ${pushLabel.toLowerCase()}`
    : pushLabel;
  const onboarding = project.onboarding || {};
  const seedButton = $("#seedOnboardingButton");
  seedButton.hidden = !onboarding.status || onboarding.status === "complete";
  seedButton.textContent = ({
    generating: "Seeds are generating…",
    review: "Review design seeds",
    finalizing: "Building selected direction…",
    error: onboarding.sessionId ? "Seed agent needs attention" : "Retry seed onboarding",
  })[onboarding.status] || "Continue seed onboarding";
  const defaultEffort = safeLocalStorageGet(`wkcc:reasoning:${project.provider}`) || "medium";
  fillReasoning($("#newAgentReasoning"), project.provider, defaultEffort);
  const live = (project.sessions || []).filter((session) => (
    ["active", "busy", "merging", "error"].includes(session.status)
  ));
  const active = live.filter((session) => session.kind !== "seeds");
  const byColor = new Map(live.map((session) => [session.color, session]));
  const grid = $("#colorGrid");
  grid.replaceChildren();
  if (project.configError) {
    const error = document.createElement("p");
    error.className = "modal-error";
    error.setAttribute("role", "alert");
    error.textContent = project.configError;
    grid.appendChild(error);
  }
  (project.palette || []).forEach((color) => {
    const session = byColor.get(color.slug);
    const card = document.createElement("button");
    card.type = "button";
    card.className = `color-card ${session ? "active" : ""}`;
    card.disabled = !session && webkitUpdate?.code === "webkit_update_required";
    card.dataset.color = COLOR_ACCENT_NAMES.has(color.slug) ? color.slug : "neutral";
    const emoji = document.createElement("span");
    emoji.className = "emoji";
    emoji.textContent = color.emoji;
    const title = document.createElement("strong");
    title.textContent = color.slug;
    const hint = document.createElement("small");
    hint.textContent = session
      ? (session.status === "active" ? "Currently active" : session.status)
      : (webkitUpdate?.code === "webkit_update_required"
        ? "Update Webkit first"
        : "Start isolated agent");
    hint.dataset.working = String(!!session && ["busy", "merging"].includes(session.status));
    card.append(emoji, title, hint);
    if (session) {
      card.addEventListener("click", () => openSessionPreview(session));
    } else {
      card.addEventListener("click", () => startColor(color.slug, card));
    }
    grid.appendChild(card);
  });
  renderSessions(active);
}

function renderSessions(sessions) {
  const list = $("#sessionList");
  list.replaceChildren();
  if (!sessions.length) {
    const empty = document.createElement("div");
    empty.className = "no-sessions";
    empty.textContent = "No agents running yet. Pick a color above.";
    list.appendChild(empty);
    return;
  }
  sessions.forEach((session) => {
    const row = document.createElement("article");
    row.className = "session-row";
    const emoji = document.createElement("span");
    emoji.className = "session-emoji";
    emoji.textContent = session.emoji;
    const info = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = `${session.kind === "support" ? "Agent" : capitalize(session.color)} · ${session.provider === "codex" ? "Codex" : "Claude Code"}`;
    const branch = document.createElement("small");
    branch.textContent = session.branch;
    info.append(title, branch);
    const status = document.createElement("span");
    status.className = `session-state ${session.status}`;
    status.textContent = session.status;
    const actions = document.createElement("div");
    actions.className = "session-actions";
    const chat = document.createElement("button");
    chat.type = "button";
    chat.className = "open-chat";
    chat.textContent = "Chat";
    chat.addEventListener("click", (event) => openSession(session, event.currentTarget));
    if (session.kind !== "support") {
      const preview = document.createElement("button");
      preview.type = "button";
      preview.className = "open-chat";
      preview.textContent = "Preview";
      preview.setAttribute("aria-label", `Open ${session.color} website preview`);
      preview.addEventListener("click", () => openSessionPreview(session));
      actions.append(preview);
    }
    actions.append(chat);
    row.append(emoji, info, status, actions);
    list.appendChild(row);
  });
}

async function startColor(color, button) {
  const project = selectedProject();
  if (!project) return;
  const tabName = `webkit-${project.id}-${color}`;
  const previewTab = reservePreviewWindow(tabName);
  let startedSession = null;
  setBusy(button, true, "Starting…");
  try {
    const { session } = await api("/api/sessions/start", {
      method: "POST", body: { projectId: project.id, color, reasoningEffort: $("#newAgentReasoning").value },
    });
    startedSession = session;
    rememberPreviewWindow(session, previewTab);
    if (previewTab) navigatePreviewWindow(previewTab, session.previewUrl);
    else toast(`Preview ready at ${session.previewUrl}. Allow popups to open it automatically.`);
    await refreshProjects();
    toast(`${session.emoji} ${capitalize(color)} agent is live.`);
  } catch (error) {
    if (startedSession) {
      toast(`${startedSession.emoji} ${capitalize(color)} agent is live, but the project list could not refresh yet: ${error.message}`);
    } else {
      if (previewTab) previewTab.close();
      toast(error.message);
    }
  } finally { setBusy(button, false); }
}

async function pushSelectedProject() {
  const project = selectedProject();
  if (!project) return;
  const button = $("#pushGithubButton");
  let pushResult = null;
  setBusy(button, true, "Pushing…");
  try {
    const result = await api(`/api/projects/${project.id}/push`, { method: "POST", body: {} });
    pushResult = result;
    await refreshProjects();
    toast(result.alreadyCurrent
      ? "GitHub is already up to date."
      : `${targetBranch(project)} is now updated on GitHub.`);
  } catch (error) {
    if (!pushResult && retainActionableIssue(error, project)) {
      try { await refreshProjects(); } catch (_refreshError) {}
    } else {
      toast(pushResult
        ? `GitHub was updated, but the project list could not refresh yet: ${error.message}`
        : error.message);
    }
  } finally {
    setBusy(button, false);
  }
}

async function updateSelectedProjectWebkit() {
  const project = selectedProject();
  if (!project?.webkitUpdate || project.webkitUpdate.code !== "webkit_update_required") return;
  const button = $("#updateProjectWebkitButton");
  setBusy(button, true, "Updating…");
  try {
    const result = await api("/api/projects/existing", {
      method: "POST",
      body: {
        path: project.sourcePath || project.path,
        provider: project.provider,
        updateWebkit: true,
      },
    });
    await refreshProjects();
    const updated = result.project?.webkitUpdated;
    toast(updated
      ? `Webkit updated from ${updated.installedVersion} to ${updated.requiredVersion}.`
      : "Webkit is already current.");
  } catch (error) {
    toast(error.message);
  } finally {
    setBusy(button, false);
  }
}

async function handleProjectIssue(event) {
  const project = selectedProject();
  const issue = project?.issue;
  if (!project || !issue || issue.action !== "handle_with_agent") return;
  const button = event.currentTarget;
  const existing = (project.sessions || []).find((session) => (
    session.id === issue.supportSessionId
    && session.kind === "support"
    && ["active", "busy", "merging", "error"].includes(session.status)
  ));
  if (existing) {
    openSession(existing, button);
    return;
  }
  setBusy(button, true, "Starting agent…");
  try {
    const { session } = await api(`/api/projects/${project.id}/agent`, {
      method: "POST",
      body: {
        issueCode: issue.code,
        reasoningEffort: "high",
      },
    });
    try { await refreshProjects(); } catch (_refreshError) {}
    const refreshed = projectForSessionId(session.id);
    const current = (refreshed?.sessions || []).find((item) => item.id === session.id) || session;
    openSession(current, button);
  } catch (error) {
    toast(error.message);
  } finally {
    setBusy(button, false);
  }
}

function setChatStatus(session) {
  $("#chatStatus").className = `live-dot ${session.status}`;
  $("#chatStatusText").textContent = `Session status: ${session.status}`;
}

function openSession(session, returnFocus = document.activeElement) {
  state.chatGeneration += 1;
  state.chatReasoningRequest += 1;
  state.chatSessionId = session.id;
  state.chatCursor = 0;
  state.chatTurn = null;
  state.chatReturnFocus = returnFocus instanceof HTMLElement ? returnFocus : null;
  $("#chatDrawer").hidden = false;
  $("#chatColor").textContent = session.kind === "support"
    ? `${session.emoji} issue agent`
    : `${session.emoji} ${session.color} worktree`;
  $("#chatTitle").textContent = session.provider === "codex" ? "Codex" : "Claude Code";
  setChatStatus(session);
  $("#chatReasoningLabel").textContent = session.provider === "codex" ? "Codex reasoning" : "Claude effort";
  $("#mergeButton").textContent = session.kind === "support"
    ? `Apply fix to ${targetBranchForSession(session)}`
    : `Merge to ${targetBranchForSession(session)}`;
  $("#mergeButton").hidden = session.kind === "seeds";
  $("#chatReasoning").disabled = false;
  fillReasoning($("#chatReasoning"), session.provider, session.reasoningEffort || "medium");
  $("#chatInput").value = "";
  $("#chatFiles").value = "";
  state.pendingAttachments = [];
  state.chatAttachmentReadsPending = 0;
  state.chatSendInFlight = false;
  renderAttachments();
  setBusy($("#chatForm button.primary"), false);
  $("#chatEvents").replaceChildren();
  pollEvents();
  requestAnimationFrame(() => $("#closeChat").focus());
}

function currentSession() {
  for (const project of state.projects) {
    const session = (project.sessions || []).find((item) => item.id === state.chatSessionId);
    if (session) return session;
  }
  return null;
}

function formatThinkingDuration(milliseconds) {
  const seconds = Math.max(0, Math.round((Number(milliseconds) || 0) / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  return remainder ? `${minutes}m ${remainder}s` : `${minutes}m`;
}

function chatEventNode(event) {
  const node = document.createElement("div");
  node.className = `event ${event.role} ${event.kind}`;
  node.textContent = event.text;
  return node;
}

function renderChatEvent(event, target) {
  if (event.kind === "turn_start") {
    const details = document.createElement("details");
    details.className = "thinking-group";
    details.open = true;
    const summary = document.createElement("summary");
    const label = document.createElement("span");
    label.textContent = "Agent is thinking…";
    summary.appendChild(label);
    const body = document.createElement("div");
    body.className = "thinking-events";
    details.append(summary, body);
    target.appendChild(details);
    state.chatTurn = { details, label, body, candidates: [], duration: "" };
    return;
  }

  if (event.kind === "turn_complete") {
    const turn = state.chatTurn;
    if (!turn) return;
    const finalNode = turn.candidates.at(-1) || null;
    if (finalNode) target.appendChild(finalNode);
    turn.duration = formatThinkingDuration(event.meta?.durationMs);
    const updateLabel = () => {
      turn.label.textContent = `${turn.details.open ? "Hide" : "View"} thinking · ${turn.duration}`;
    };
    turn.details.open = false;
    updateLabel();
    turn.details.addEventListener("toggle", updateLabel);
    state.chatTurn = null;
    return;
  }

  const node = chatEventNode(event);
  const turn = state.chatTurn;
  if (turn && event.role !== "user") {
    turn.body.appendChild(node);
    if ((event.role === "agent" && event.kind === "message") || event.kind === "error") {
      turn.candidates.push(node);
    }
  } else {
    target.appendChild(node);
  }
}

function renderAttachments() {
  const list = $("#attachmentList");
  list.replaceChildren();
  list.hidden = state.pendingAttachments.length === 0;
  state.pendingAttachments.forEach((item, index) => {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "attachment-chip";
    chip.textContent = `${item.name} ×`;
    chip.title = "Remove attachment";
    chip.addEventListener("click", () => {
      state.pendingAttachments.splice(index, 1);
      renderAttachments();
    });
    list.appendChild(chip);
  });
}

function beginChatAttachmentRead(expectedSessionId, generation) {
  if (expectedSessionId !== state.chatSessionId || generation !== state.chatGeneration) return false;
  state.chatAttachmentReadsPending += 1;
  setBusy($("#chatForm button.primary"), true, "Reading attachment…");
  return true;
}

function endChatAttachmentRead(expectedSessionId, generation) {
  if (expectedSessionId !== state.chatSessionId || generation !== state.chatGeneration) return;
  state.chatAttachmentReadsPending = Math.max(0, state.chatAttachmentReadsPending - 1);
  if (!state.chatAttachmentReadsPending && !state.chatSendInFlight) {
    setBusy($("#chatForm button.primary"), false);
  }
}

async function addChatFiles(files) {
  const expectedSessionId = state.chatSessionId;
  const generation = state.chatGeneration;
  if (!expectedSessionId) return;
  if (!beginChatAttachmentRead(expectedSessionId, generation)) return;
  try {
    const incoming = [...files].filter((file) => file && file.size);
    const combined = [...state.pendingAttachments, ...incoming];
    const total = combined.reduce((sum, file) => sum + (file.size || 0), 0);
    if (combined.length > MAX_CHAT_ATTACHMENTS) {
      toast(`Chat messages are limited to ${MAX_CHAT_ATTACHMENTS} attachments.`);
      return;
    }
    if (incoming.some((file) => file.size > MAX_CHAT_ATTACHMENT_BYTES)
        || total > MAX_CHAT_ATTACHMENTS_TOTAL_BYTES) {
      toast("Chat attachments must be 20 MB each and 20 MB total or smaller.");
      return;
    }
    const prepared = await Promise.all(incoming.map(async (file) => {
      const data = await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result).split(",", 2)[1] || "");
      reader.onerror = reject;
      reader.readAsDataURL(file);
      });
      return {
        name: file.name || "pasted-file",
        type: file.type || "application/octet-stream",
        size: file.size,
        data,
      };
    }));
    if (expectedSessionId !== state.chatSessionId || generation !== state.chatGeneration) return;
    const updated = [...state.pendingAttachments, ...prepared];
    const updatedTotal = updated.reduce((sum, file) => sum + (file.size || 0), 0);
    if (updated.length > MAX_CHAT_ATTACHMENTS) {
      toast(`Chat messages are limited to ${MAX_CHAT_ATTACHMENTS} attachments.`);
      return;
    }
    if (updatedTotal > MAX_CHAT_ATTACHMENTS_TOTAL_BYTES) {
      toast("Chat attachments must be 20 MB each and 20 MB total or smaller.");
      return;
    }
    state.pendingAttachments.push(...prepared);
    renderAttachments();
  } catch (error) {
    if (expectedSessionId === state.chatSessionId && generation === state.chatGeneration) {
      toast(`Could not read the selected attachments: ${error.message || error}`);
    }
    return;
  } finally {
    endChatAttachmentRead(expectedSessionId, generation);
  }
}

async function changeChatReasoning() {
  const session = currentSession();
  if (!session) return;
  const expectedSessionId = session.id;
  const generation = state.chatGeneration;
  const select = $("#chatReasoning");
  const reasoningEffort = select.value;
  const previous = session.reasoningEffort || "medium";
  const request = ++state.chatReasoningRequest;
  select.disabled = true;
  try {
    const result = await api(`/api/sessions/${session.id}/reasoning`, {
      method: "POST", body: { reasoningEffort },
    });
    if (expectedSessionId !== state.chatSessionId || generation !== state.chatGeneration
        || request !== state.chatReasoningRequest) return;
    session.reasoningEffort = result.reasoningEffort;
    select.value = result.reasoningEffort;
    toast(`${session.provider === "codex" ? "Codex reasoning" : "Claude effort"} set to ${result.reasoningEffort}.`);
  } catch (error) {
    if (expectedSessionId === state.chatSessionId && generation === state.chatGeneration
        && request === state.chatReasoningRequest) {
      select.value = reasoningLevels(session.provider).includes(previous) ? previous : "medium";
      toast(error.message);
    }
  } finally {
    if (expectedSessionId === state.chatSessionId && generation === state.chatGeneration
        && request === state.chatReasoningRequest) select.disabled = false;
  }
}

async function pollEvents() {
  if (eventsPollPromise) {
    eventsPollQueued = true;
    return eventsPollPromise;
  }
  eventsPollPromise = (async () => {
    do {
      eventsPollQueued = false;
      if (!state.chatSessionId || $("#chatDrawer").hidden) return;
      const expected = state.chatSessionId;
      const generation = state.chatGeneration;
      const cursor = state.chatCursor;
      try {
        const data = await api(`/api/sessions/${expected}/events?after=${cursor}`);
        if (expected !== state.chatSessionId || generation !== state.chatGeneration) continue;
        const target = $("#chatEvents");
        if (data.reset) {
          target.replaceChildren();
          state.chatTurn = null;
        }
        data.events.forEach((event) => {
          renderChatEvent(event, target);
        });
        if (data.events.length) target.scrollTop = target.scrollHeight;
        state.chatCursor = data.next;
      } catch (error) {
        if (expected === state.chatSessionId && generation === state.chatGeneration) toast(error.message);
      }
    } while (eventsPollQueued);
  })();
  try {
    await eventsPollPromise;
  } finally {
    eventsPollPromise = null;
  }
}

async function sendChat(event) {
  event.preventDefault();
  const input = $("#chatInput");
  const draft = input.value;
  const message = input.value.trim();
  if ((!message && !state.pendingAttachments.length) || !state.chatSessionId) return;
  if (state.chatAttachmentReadsPending) {
    toast("Wait for the selected attachments to finish loading.");
    return;
  }
  if (state.chatSendInFlight) return;
  const expectedSessionId = state.chatSessionId;
  const generation = state.chatGeneration;
  const attachments = [...state.pendingAttachments];
  const estimatedPathBytes = attachments.reduce(
    (sum, item) => sum + new TextEncoder().encode(item.name || "attachment").length + 256,
    80,
  );
  if (new TextEncoder().encode(message).length + estimatedPathBytes > MAX_PROVIDER_PROMPT_BYTES) {
    toast("The message and attachment names exceed the 16 KB agent prompt limit.");
    return;
  }
  const button = $("#chatForm button.primary");
  state.chatSendInFlight = true;
  setBusy(button, true, "Queued");
  try {
    await api(`/api/sessions/${expectedSessionId}/message`, {
      method: "POST", body: { message, attachments },
    });
    if (expectedSessionId === state.chatSessionId && generation === state.chatGeneration) {
      if (input.value === draft) input.value = "";
      const sent = new Set(attachments);
      state.pendingAttachments = state.pendingAttachments.filter((item) => !sent.has(item));
      renderAttachments();
      setTimeout(pollEvents, 150);
    }
  } catch (error) {
    if (expectedSessionId === state.chatSessionId && generation === state.chatGeneration) toast(error.message);
  } finally {
    if (expectedSessionId === state.chatSessionId && generation === state.chatGeneration) {
      state.chatSendInFlight = false;
      if (!state.chatAttachmentReadsPending) setBusy(button, false);
    }
  }
}

function askConfirm(kind) {
  const session = currentSession();
  if (!session) return;
  state.confirmGeneration += 1;
  state.confirmBusy = false;
  const generation = state.confirmGeneration;
  const isMerge = kind === "merge";
  const branch = targetBranchForSession(session);
  const support = session.kind === "support";
  $("#confirmEyebrow").textContent = isMerge ? "Keep the work" : "Permanent action";
  $("#confirmTitle").textContent = isMerge
    ? `${support ? "Apply this fix" : "Merge this color"} to ${branch}?`
    : `Discard this ${support ? "agent" : "color"} session?`;
  $("#confirmText").textContent = isMerge
    ? `The ${session.emoji} branch will be merged into ${branch}, then its worktree will close.`
    : `All unmerged work in ${session.branch} will be deleted. This cannot be undone.`;
  $("#confirmAction").textContent = isMerge
    ? `${support ? "Apply fix to" : "Merge to"} ${branch}`
    : "Discard forever";
  $("#confirmAction").className = isMerge ? "primary" : "danger";
  setBusy($("#confirmAction"), false);
  $("#confirmDialog button[value=\"cancel\"]").disabled = false;
  state.confirmCallback = () => finishSession(session, kind, generation);
  $("#confirmDialog").showModal();
}

async function finishSession(session, kind, confirmationGeneration) {
  if (confirmationGeneration !== state.confirmGeneration || state.confirmBusy) return;
  const button = $("#confirmAction");
  const cancel = $("#confirmDialog button[value=\"cancel\"]");
  const branch = targetBranchForSession(session);
  const expectedChatSessionId = state.chatSessionId;
  const chatGeneration = state.chatGeneration;
  state.confirmBusy = true;
  cancel.disabled = true;
  setBusy(button, true, kind === "merge" ? "Merging…" : "Discarding…");
  try {
    const body = kind === "discard" ? { confirmation: session.id } : {};
    const result = await api(`/api/sessions/${session.id}/${kind}`, { method: "POST", body });
    const confirmationCurrent = confirmationGeneration === state.confirmGeneration;
    if (confirmationCurrent) {
      state.confirmBusy = false;
      cancel.disabled = false;
      setBusy(button, false);
      $("#confirmDialog").close();
    }
    if (kind === "merge") {
      if (result.queued) {
        if (confirmationCurrent) toast("The agent is integrating the branch. Open chat if it needs help with a conflict.");
        await refreshProjects();
        return;
      }
      const github = result.github || { connected: false, pushed: false };
      if (confirmationCurrent) {
        if (github.pushed) toast(`Merged and updated ${branch} on GitHub.`);
        else if (github.connected) toast(`Merged locally, but GitHub push failed: ${github.error || "try again when connected"}`);
        else toast(`Merged locally. Connect GitHub so future merges update ${branch} automatically.`);
      }
    } else if (confirmationCurrent) {
      toast(session.kind === "support" ? "Agent worktree discarded." : "Color worktree discarded.");
    }
    if (expectedChatSessionId === state.chatSessionId && chatGeneration === state.chatGeneration) {
      state.chatGeneration += 1;
      $("#chatDrawer").hidden = true;
      state.chatSessionId = null;
    }
    await refreshProjects();
  } catch (error) {
    if (confirmationGeneration === state.confirmGeneration) {
      const project = projectForSessionId(session.id);
      if (retainActionableIssue(error, project)) {
        try { await refreshProjects(); } catch (_refreshError) {}
      } else {
        toast(error.message);
      }
    }
  } finally {
    if (confirmationGeneration === state.confirmGeneration) {
      state.confirmBusy = false;
      cancel.disabled = false;
      setBusy(button, false);
    }
  }
}

function openProjectDialog() {
  state.projectGeneration += 1;
  $("#projectError").textContent = "";
  $("#projectUpdatePrompt").hidden = true;
  state.createStep = 1;
  state.onboardingAssets = [];
  $("#newBrandBrief").value = "";
  $("#newSeedCount").value = "10";
  renderProjectAssets();
  const select = $("#projectProviderSelect");
  select.replaceChildren();
  state.providers.forEach((provider) => {
    const option = document.createElement("option");
    option.value = provider;
    option.textContent = provider === "codex" ? "Codex" : "Claude Code";
    select.appendChild(option);
  });
  setBusy($("#saveProject"), state.projectSaveInFlight, "Finishing previous request…");
  document.querySelectorAll("[data-folder-target]").forEach((button) => setBusy(button, false));
  renderProjectWizard();
  $("#projectDialog").showModal();
}

function renderProjectWizard() {
  const creating = state.projectMode === "create";
  document.querySelectorAll("[data-create-step]").forEach((node) => {
    node.hidden = !creating || Number(node.dataset.createStep) !== state.createStep;
  });
  document.querySelectorAll("[data-onboarding-dot]").forEach((node) => {
    node.classList.toggle("active", Number(node.dataset.onboardingDot) <= state.createStep);
  });
  $("#projectModeTabs").hidden = creating && state.createStep > 1;
  $("#projectProviderField").hidden = creating && state.createStep > 1;
  $("#projectGithubNote").hidden = creating && state.createStep > 1;
  $("#projectBack").hidden = !creating || state.createStep === 1;
  $("#projectDialogTitle").textContent = !creating
    ? "Bring your website"
    : ({ 1: "Create a new website", 2: "Give the agent context", 3: "Explore before committing" })[state.createStep];
  $("#saveProject").textContent = !creating
    ? "Add project"
    : (state.createStep < 3 ? "Continue" : "Create website & seeds");
}

function setProjectMode(mode) {
  state.projectMode = mode;
  state.createStep = 1;
  document.querySelectorAll("[data-project-mode]").forEach((button) => {
    const selected = button.dataset.projectMode === mode;
    button.classList.toggle("active", selected);
    button.setAttribute("aria-selected", String(selected));
    button.tabIndex = selected ? 0 : -1;
  });
  $("#createFields").hidden = mode !== "create";
  $("#existingFields").hidden = mode !== "existing";
  $("#projectUpdatePrompt").hidden = true;
  renderProjectWizard();
}

function renderProjectAssets() {
  const list = $("#projectAssetList");
  list.replaceChildren();
  list.hidden = state.onboardingAssets.length === 0;
  state.onboardingAssets.forEach((item, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = `${item.path || item.name} ×`;
    button.title = "Remove reference";
    button.addEventListener("click", () => {
      state.onboardingAssets.splice(index, 1);
      renderProjectAssets();
    });
    list.appendChild(button);
  });
}

function beginProjectAssetOperation(expectedGeneration) {
  if (expectedGeneration !== state.projectGeneration || !$("#projectDialog").open) return false;
  state.projectAssetOperationsPending += 1;
  setBusy($("#saveProject"), true, "Reading references…");
  return true;
}

function endProjectAssetOperation(expectedGeneration) {
  if (expectedGeneration !== state.projectGeneration) return;
  state.projectAssetOperationsPending = Math.max(0, state.projectAssetOperationsPending - 1);
  if (!state.projectAssetOperationsPending && !state.projectSaveInFlight) {
    setBusy($("#saveProject"), false);
  }
}

async function addProjectAssets(files, expectedGeneration = state.projectGeneration) {
  if (!beginProjectAssetOperation(expectedGeneration)) return;
  try {
    const incoming = [...files].filter((item) => item && item.size);
    const combinedCount = state.onboardingAssets.length + incoming.length;
    const total = state.onboardingAssets.reduce((sum, item) => sum + (item.size || 0), 0)
      + incoming.reduce((sum, item) => sum + item.size, 0);
    if (combinedCount > MAX_PROJECT_ASSETS) {
      toast(`Project references are limited to ${MAX_PROJECT_ASSETS} files.`);
      return;
    }
    if (incoming.some((file) => file.size > MAX_PROJECT_ASSET_BYTES)
        || total > MAX_PROJECT_ASSETS_TOTAL_BYTES) {
      toast("Project references must be 15 MB each and 20 MB total or smaller.");
      return;
    }
    const prepared = await Promise.all(incoming.map(async (file) => {
      const data = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result).split(",", 2)[1] || "");
        reader.onerror = reject;
        reader.readAsDataURL(file);
      });
      const relativePath = String(file.webkitRelativePath || file._relativePath || file.name || "reference")
        .replace(/\\/g, "/").replace(/^\/+/, "");
      return {
        name: file.name || "reference",
        path: relativePath,
        type: file.type || "application/octet-stream",
        size: file.size,
        data,
      };
    }));
    if (expectedGeneration !== state.projectGeneration || !$("#projectDialog").open) return;
    const updated = [...state.onboardingAssets, ...prepared];
    const updatedTotal = updated.reduce((sum, item) => sum + (item.size || 0), 0);
    if (updated.length > MAX_PROJECT_ASSETS) {
      toast(`Project references are limited to ${MAX_PROJECT_ASSETS} files.`);
      return;
    }
    if (updatedTotal > MAX_PROJECT_ASSETS_TOTAL_BYTES) {
      toast("Project references must be 15 MB each and 20 MB total or smaller.");
      return;
    }
    state.onboardingAssets.push(...prepared);
    renderProjectAssets();
  } catch (error) {
    if (expectedGeneration === state.projectGeneration && $("#projectDialog").open) {
      toast(`Could not read the selected project references: ${error.message || error}`);
    }
    return;
  } finally {
    endProjectAssetOperation(expectedGeneration);
  }
}

function readDroppedEntry(entry, prefix, budget, depth) {
  if (!entry) return Promise.resolve([]);
  if (depth > MAX_PROJECT_DROP_DEPTH) {
    return Promise.reject(new Error(`Dropped folders may be at most ${MAX_PROJECT_DROP_DEPTH} levels deep.`));
  }
  budget.entries += 1;
  if (budget.entries > MAX_PROJECT_DROP_ENTRIES) {
    return Promise.reject(new Error(`Dropped folders may contain at most ${MAX_PROJECT_DROP_ENTRIES} entries.`));
  }
  const relativePath = `${prefix}${entry.name}`;
  if (entry.isFile) {
    budget.files += 1;
    if (budget.files > budget.maxFiles) {
      return Promise.reject(new Error(`Project references are limited to ${MAX_PROJECT_ASSETS} files.`));
    }
    return new Promise((resolve, reject) => entry.file((file) => {
      Object.defineProperty(file, "_relativePath", { value: relativePath, configurable: true });
      resolve([file]);
    }, reject));
  }
  if (!entry.isDirectory) return Promise.resolve([]);
  return new Promise((resolve, reject) => {
    const reader = entry.createReader();
    const results = [];
    const readBatch = () => reader.readEntries(async (batch) => {
      if (!batch.length) {
        resolve(results);
        return;
      }
      try {
        for (const child of batch) {
          results.push(...await readDroppedEntry(child, `${relativePath}/`, budget, depth + 1));
        }
        readBatch();
      } catch (error) { reject(error); }
    }, reject);
    readBatch();
  });
}

async function projectFilesFromDrop(dataTransfer) {
  const entries = [...(dataTransfer?.items || [])]
    .map((item) => item.webkitGetAsEntry?.()).filter(Boolean);
  if (!entries.length) {
    const files = [...(dataTransfer?.files || [])];
    if (state.onboardingAssets.length + files.length > MAX_PROJECT_ASSETS) {
      throw new Error(`Project references are limited to ${MAX_PROJECT_ASSETS} files.`);
    }
    return files;
  }
  const budget = {
    entries: 0,
    files: 0,
    maxFiles: Math.max(0, MAX_PROJECT_ASSETS - state.onboardingAssets.length),
  };
  const files = [];
  for (const entry of entries) files.push(...await readDroppedEntry(entry, "", budget, 0));
  return files;
}

async function chooseProjectFolder(event) {
  const button = event.currentTarget;
  const generation = state.projectGeneration;
  const field = $(`#${button.dataset.folderTarget}`);
  const errorNode = $("#projectError");
  errorNode.textContent = "";
  $("#projectUpdatePrompt").hidden = true;
  setBusy(button, true, "Choosing…");
  try {
    const result = await api("/api/system/choose-folder", {
      method: "POST",
      body: { initial: field.value, purpose: button.dataset.folderPurpose },
    });
    if (generation === state.projectGeneration && $("#projectDialog").open
        && !result.cancelled && result.path) {
      field.value = result.path;
      field.title = result.path;
    }
  } catch (error) {
    if (generation === state.projectGeneration && $("#projectDialog").open) errorNode.textContent = error.message;
  } finally {
    if (generation === state.projectGeneration) setBusy(button, false);
  }
}

async function saveProject(event) {
  event.preventDefault();
  const generation = state.projectGeneration;
  const button = $("#saveProject");
  const errorNode = $("#projectError");
  const updatePrompt = $("#projectUpdatePrompt");
  const updateWebkit = event.submitter?.id === "updateProjectWebkit";
  let createdResult = null;
  errorNode.textContent = "";
  updatePrompt.hidden = true;
  if (state.projectMode === "create" && state.createStep < 3) {
    if (state.createStep === 1 && (!$("#newName").value.trim() || !$("#newParent").value.trim())) {
      errorNode.textContent = "Enter a website name and choose its parent folder.";
      return;
    }
    if (state.projectAssetOperationsPending) {
      errorNode.textContent = "Wait for the selected project references to finish loading.";
      return;
    }
    state.createStep += 1;
    renderProjectWizard();
    return;
  }
  if (state.projectMode === "create" && $("#newBrandBrief").value.length > MAX_PROJECT_BRIEF_CHARS) {
    errorNode.textContent = `Brand and design brief must be ${MAX_PROJECT_BRIEF_CHARS} characters or fewer.`;
    return;
  }
  if (state.projectAssetOperationsPending) {
    errorNode.textContent = "Wait for the selected project references to finish loading.";
    return;
  }
  if (state.projectSaveInFlight) {
    errorNode.textContent = "Wait for the previous project request to finish.";
    return;
  }
  state.projectSaveInFlight = true;
  setBusy(button, true, state.projectMode === "create" ? "Creating & starting seeds…" : "Adding…");
  try {
    const provider = $("#projectProviderSelect").value;
    const isCreate = state.projectMode === "create";
    const path = isCreate ? "/api/projects/create" : "/api/projects/existing";
    const body = isCreate
      ? {
        name: $("#newName").value,
        parent: $("#newParent").value,
        provider,
        onboarding: {
          brief: $("#newBrandBrief").value,
          seedCount: Math.max(2, Math.min(20, Number($("#newSeedCount").value) || 10)),
          assets: state.onboardingAssets.map(({ name, path: relativePath, type, data }) => ({
            name, path: relativePath, type, data,
          })),
        },
      }
      : { path: $("#existingPath").value, provider, updateWebkit };
    const result = await api(path, { method: "POST", body });
    createdResult = result;
    const project = result.project;
    const updateNotice = !isCreate && project.webkitUpdated
      ? `Webkit updated from ${project.webkitUpdated.installedVersion} to ${project.webkitUpdated.requiredVersion}. `
      : "";
    const isolatedNotice = !isCreate && project.sourceIntegrationPending
      ? `${updateNotice}Project added in an isolated checkout. Your local changes were left untouched. Commit or stash them before merging or pushing back.`
      : "";
    await refreshProjects();
    if (generation !== state.projectGeneration || !$("#projectDialog").open) {
      toast(isolatedNotice || `${project.name || "Project"} was added.`);
      return;
    }
    $("#projectDialog").close();
    selectProject(project.id);
    const githubSetup = result.githubSetup;
    const githubWarning = isCreate && githubSetup?.attempted && !githubSetup?.verified
      ? " GitHub setup did not verify the initial push. Your local project is safe; retry from Push to GitHub."
      : "";
    if (isCreate && result.seedSession) {
      openSeedOnboarding(result.seedSession.id);
      toast(`Creating ${body.onboarding.seedCount} distinct directions in an isolated worktree.${githubWarning}`);
      return;
    }
    if (isCreate && result.seedError) {
      toast(`Website created, but seeds could not start: ${result.seedError}${githubWarning}`);
      return;
    }
    const registeredProject = state.projects.find((item) => item.id === project.id) || project;
    if (isolatedNotice) {
      toast(isolatedNotice);
      return;
    }
    if (updateNotice) {
      toast(`${updateNotice}Project ready locally.`);
      return;
    }
    const verifiedCurrent = githubSetup?.verified === true || (
      registeredProject.github?.connected && !registeredProject.github?.unpushed
    );
    toast(githubWarning || (verifiedCurrent
      ? "Project ready with automatic GitHub sync."
      : "Project ready locally. Connect GitHub to your coding agent and WebKit will detect it automatically."));
  } catch (error) {
    if (createdResult) {
      if (generation === state.projectGeneration && $("#projectDialog").open) $("#projectDialog").close();
      toast(`${createdResult.project?.name || "Project"} was added, but the project list could not refresh yet: ${error.message}`);
      setTimeout(() => refreshProjects().catch(() => {}), 500);
    } else if (generation === state.projectGeneration && $("#projectDialog").open) {
      errorNode.textContent = error.message;
      const details = error.details || {};
      if (!updateWebkit && details.code === "webkit_update_required") {
        $("#projectUpdateVersions").textContent = `${details.installedVersion} to ${details.requiredVersion}`;
        updatePrompt.hidden = false;
      }
    }
  } finally {
    state.projectSaveInFlight = false;
    if ($("#projectDialog").open) setBusy(button, false);
  }
}

function sessionById(sessionId) {
  for (const project of state.projects) {
    const session = (project.sessions || []).find((item) => item.id === sessionId);
    if (session) return session;
  }
  return null;
}

function openSeedOnboarding(sessionId) {
  state.seedGeneration += 1;
  state.seedSessionId = sessionId;
  state.seedProjectId = projectForSessionId(sessionId)?.id || state.selectedProjectId;
  state.seedCurrentId = null;
  state.seedSelected = new Set();
  state.seedSignature = "";
  state.seedData = null;
  $("#seedReview").hidden = true;
  $("#seedLoading").hidden = false;
  $("#openSeedChat").hidden = true;
  $("#seedError").textContent = "";
  $("#seedDialogEyebrow").textContent = "Creating directions";
  $("#seedDialogTitle").textContent = "Your seeds are growing.";
  $("#seedDialogText").textContent = "The agent is building distinct visual approaches in an isolated worktree.";
  setBusy($("#finishSeeds"), state.seedSelectionInFlight, "Starting…");
  if (!$("#seedDialog").open) $("#seedDialog").showModal();
  pollSeedStatus();
}

function showSeed(seed) {
  if (!seed) return;
  state.seedCurrentId = seed.id;
  $("#seedPreviewTitle").textContent = seed.title;
  $("#seedPreviewSummary").textContent = [seed.direction, seed.summary].filter(Boolean).join(" ");
  if ($("#seedPreview").src !== seed.previewUrl) $("#seedPreview").src = seed.previewUrl;
  $("#openSeedPreview").dataset.url = seed.previewUrl;
  document.querySelectorAll(".seed-option").forEach((node) => {
    node.classList.toggle("current", node.dataset.seedId === seed.id);
  });
}

function renderSeedReview(data) {
  const signature = JSON.stringify(data.seeds);
  if (signature === state.seedSignature) return;
  state.seedSignature = signature;
  const ids = new Set(data.seeds.map((seed) => seed.id));
  state.seedSelected = new Set([...state.seedSelected].filter((id) => ids.has(id)));
  if (!state.seedSelected.size && data.seeds[0]) state.seedSelected.add(data.seeds[0].id);
  if (!ids.has(state.seedCurrentId)) state.seedCurrentId = data.seeds[0]?.id || null;
  const list = $("#seedList");
  list.replaceChildren();
  data.seeds.forEach((seed, index) => {
    const row = document.createElement("div");
    row.className = `seed-option${state.seedSelected.has(seed.id) ? " selected" : ""}${seed.id === state.seedCurrentId ? " current" : ""}`;
    row.dataset.seedId = seed.id;
    const check = document.createElement("input");
    check.type = "checkbox";
    check.checked = state.seedSelected.has(seed.id);
    check.setAttribute("aria-label", `Use ${seed.title}`);
    const copy = document.createElement("button");
    copy.type = "button";
    copy.className = "seed-option-view";
    const title = document.createElement("b");
    title.textContent = `${index + 1}. ${seed.title}`;
    const summary = document.createElement("small");
    summary.textContent = seed.direction || seed.summary || "Distinct direction";
    copy.append(title, summary);
    copy.addEventListener("click", () => showSeed(seed));
    check.addEventListener("change", () => {
      if (check.checked) state.seedSelected.add(seed.id);
      else state.seedSelected.delete(seed.id);
      row.classList.toggle("selected", check.checked);
      showSeed(seed);
    });
    row.append(check, copy);
    list.appendChild(row);
  });
  showSeed(data.seeds.find((seed) => seed.id === state.seedCurrentId) || data.seeds[0]);
}

async function pollSeedStatus() {
  if (seedPollPromise) {
    seedPollQueued = true;
    return seedPollPromise;
  }
  seedPollPromise = (async () => {
    do {
      seedPollQueued = false;
      const sessionId = state.seedSessionId;
      const generation = state.seedGeneration;
      if (!sessionId || !$("#seedDialog").open) return;
      const seedProject = state.projects.find((project) => project.id === state.seedProjectId);
      const seedTarget = targetBranch(seedProject);
      try {
        const data = await api(`/api/sessions/${sessionId}/seeds`);
        if (sessionId !== state.seedSessionId || generation !== state.seedGeneration) continue;
        state.seedData = data;
        if (data.complete) {
          toast(`The selected direction is now your website, and ${seedTarget} has been updated.`);
          $("#seedDialog").close();
          state.seedSessionId = null;
          await refreshProjects();
          return;
        }
        if (data.ready) {
          $("#seedLoading").hidden = true;
          $("#seedReview").hidden = false;
          $("#openSeedChat").hidden = false;
          $("#seedDialogEyebrow").textContent = `${data.seeds.length} design directions`;
          $("#seedDialogTitle").textContent = "Choose what should become the website.";
          $("#seedDialogText").textContent = "Preview every seed. Select one, or select several and describe which parts to combine.";
          renderSeedReview(data);
        } else {
          $("#seedReview").hidden = true;
          $("#seedLoading").hidden = false;
          const finalizing = data.status === "finalizing" || data.sessionStatus === "merging";
          $("#seedDialogEyebrow").textContent = finalizing ? "Building your selection" : "Creating directions";
          $("#seedDialogTitle").textContent = finalizing ? "Turning the seeds into one website." : "Your seeds are growing.";
          $("#seedDialogText").textContent = finalizing
            ? `The agent is combining your choices, cleaning up the exploration, and preparing ${seedTarget}.`
            : "The agent is building distinct visual approaches in an isolated worktree.";
          const errored = data.sessionStatus === "error" || data.status === "error";
          $("#openSeedChat").hidden = !errored;
          if (errored) {
            $("#seedLoading").hidden = true;
            $("#seedDialogTitle").textContent = "The seed agent needs a hand.";
            $("#seedDialogText").textContent = data.error || "Open the agent chat to resolve the issue, then return here.";
          }
        }
      } catch (error) {
        if (sessionId !== state.seedSessionId || generation !== state.seedGeneration) continue;
        $("#seedLoading").hidden = true;
        $("#openSeedChat").hidden = false;
        $("#seedDialogTitle").textContent = "Could not read the seed session.";
        $("#seedDialogText").textContent = error.message;
      }
    } while (seedPollQueued);
  })();
  try {
    await seedPollPromise;
  } finally {
    seedPollPromise = null;
  }
}

async function finishSeedSelection() {
  if (!state.seedSessionId) return;
  if (!state.seedSelected.size) {
    $("#seedError").textContent = "Choose at least one seed to continue.";
    return;
  }
  const button = $("#finishSeeds");
  $("#seedError").textContent = "";
  if ($("#seedCombinationNotes").value.length > MAX_SEED_NOTES_CHARS) {
    $("#seedError").textContent = `Seed combination notes must be ${MAX_SEED_NOTES_CHARS} characters or fewer.`;
    return;
  }
  if (state.seedSelectionInFlight) {
    $("#seedError").textContent = "Wait for the previous seed selection to finish.";
    return;
  }
  const sessionId = state.seedSessionId;
  const generation = state.seedGeneration;
  const selected = [...state.seedSelected];
  const notes = $("#seedCombinationNotes").value;
  let selectionAccepted = false;
  state.seedSelectionInFlight = true;
  setBusy(button, true, "Starting…");
  try {
    await api(`/api/sessions/${sessionId}/seeds-select`, {
      method: "POST",
      body: { selected, notes },
    });
    selectionAccepted = true;
    if (sessionId !== state.seedSessionId || generation !== state.seedGeneration) return;
    state.seedSignature = "";
    $("#seedReview").hidden = true;
    $("#seedLoading").hidden = false;
    await refreshProjects();
    if (sessionId === state.seedSessionId && generation === state.seedGeneration) pollSeedStatus();
  } catch (error) {
    if (selectionAccepted) {
      if (sessionId === state.seedSessionId && generation === state.seedGeneration) {
        toast(`The seed selection was accepted, but status could not refresh yet: ${error.message}`);
        pollSeedStatus();
      }
    } else if (sessionId === state.seedSessionId && generation === state.seedGeneration) {
      $("#seedError").textContent = error.message;
    }
  } finally {
    state.seedSelectionInFlight = false;
    if ($("#seedDialog").open) setBusy(button, false);
  }
}

async function continueSeedOnboarding() {
  const project = selectedProject();
  if (!project) return;
  const onboarding = project.onboarding || {};
  if (onboarding.sessionId) {
    openSeedOnboarding(onboarding.sessionId);
    return;
  }
  const button = $("#seedOnboardingButton");
  setBusy(button, true, "Starting…");
  try {
    const result = await api(`/api/projects/${project.id}/seeds-start`, { method: "POST", body: {} });
    try {
      await refreshProjects();
    } catch (error) {
      toast(`The seed agent started, but the project list could not refresh yet: ${error.message}`);
    }
    openSeedOnboarding(result.seedSession.id);
  } catch (error) { toast(error.message); }
  finally { setBusy(button, false); }
}

async function saveProviders() {
  const selected = [...document.querySelectorAll("#providerDialog input:checked")].map((input) => input.value);
  const button = $("#saveProviders");
  $("#providerError").textContent = "";
  setBusy(button, true, "Checking…");
  try {
    const result = await api("/api/providers", { method: "POST", body: { providers: selected } });
    state.providers = result.providers;
    $("#providerDialog").close();
    openProjectDialog();
  } catch (error) { $("#providerError").textContent = error.message; }
  finally { setBusy(button, false); }
}

async function refreshProjects() {
  if (projectsRefreshPromise) {
    projectsRefreshQueued = true;
    return projectsRefreshPromise;
  }
  projectsRefreshPromise = (async () => {
    do {
      projectsRefreshQueued = false;
      const data = await api("/api/projects");
      refreshChangedPreviewWindows(data.projects);
      closeFinishedPreviewWindows(data.projects);
      const signature = JSON.stringify(data.projects);
      const changed = signature !== state.projectsSignature;
      state.projects = data.projects;
      state.projectsSignature = signature;
      if (state.selectedProjectId && !state.projects.some((p) => p.id === state.selectedProjectId)) state.selectedProjectId = null;
      if (!state.homeSelected && !state.selectedProjectId && state.projects.length) state.selectedProjectId = state.projects[0].id;
      if (changed) {
        renderProjects();
        renderProjectView();
      }
      const session = currentSession();
      if (session) setChatStatus(session);
    } while (projectsRefreshQueued);
  })();
  try {
    await projectsRefreshPromise;
  } finally {
    projectsRefreshPromise = null;
  }
}

async function initialize() {
  try {
    const data = await api("/api/bootstrap");
    state.providers = data.providers;
    state.projects = data.projects;
    state.projectsSignature = JSON.stringify(data.projects);
    state.system = data.system;
    state.settings = { ...DEFAULT_SETTINGS, ...(data.settings || {}) };
    if (state.projects.length) state.selectedProjectId = state.projects[0].id;
    renderStatus();
    renderShortcutGuide();
    renderProjects();
    renderProjectView();
    if (!state.providers.length) $("#providerDialog").showModal();
  } catch (error) { toast(error.message); }
}

function capitalize(value) { return value ? value[0].toUpperCase() + value.slice(1) : ""; }

$("#homeButton").addEventListener("click", () => {
  state.homeSelected = true;
  state.selectedProjectId = null;
  renderProjects();
  renderProjectIssue(null);
});
[$("#addProjectButton"), $("#emptyAddButton")].forEach((button) => button.addEventListener("click", openProjectDialog));
$("#saveProviders").addEventListener("click", saveProviders);
$("#installShortcutButton").addEventListener("click", installShortcut);
$("#settingsButton").addEventListener("click", openSettings);
$("#pushGithubButton").addEventListener("click", pushSelectedProject);
$("#updateProjectWebkitButton").addEventListener("click", updateSelectedProjectWebkit);
$("#handleProjectIssue").addEventListener("click", handleProjectIssue);
$("#seedOnboardingButton").addEventListener("click", continueSeedOnboarding);
$("#shortcutGuide").addEventListener("click", openSettings);
$("#settingsForm").addEventListener("submit", saveSettings);
document.querySelectorAll("[data-hotkey-setting]").forEach((button) => button.addEventListener("click", beginHotkeyCapture));
document.querySelectorAll('input[name="interactionMode"]').forEach((input) => input.addEventListener("change", renderHotkeySettings));
document.addEventListener("keydown", captureHotkey, true);
document.querySelectorAll("[data-project-mode]").forEach((button) => button.addEventListener("click", () => setProjectMode(button.dataset.projectMode)));
document.querySelectorAll("[data-project-mode]").forEach((button) => button.addEventListener("keydown", (event) => {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
  event.preventDefault();
  const tabs = [...document.querySelectorAll("[data-project-mode]")];
  const current = tabs.indexOf(event.currentTarget);
  const next = event.key === "Home" ? 0
    : event.key === "End" ? tabs.length - 1
      : (current + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
  setProjectMode(tabs[next].dataset.projectMode);
  tabs[next].focus();
}));
document.querySelectorAll("[data-folder-target]").forEach((button) => button.addEventListener("click", chooseProjectFolder));
$("#projectForm").addEventListener("submit", saveProject);
$("#projectBack").addEventListener("click", () => {
  state.createStep = Math.max(1, state.createStep - 1);
  $("#projectError").textContent = "";
  renderProjectWizard();
});
function setProjectAssetMenu(open) {
  $("#projectAssetMenu").hidden = !open;
  $("#projectAssetPicker").setAttribute("aria-expanded", String(open));
}

function openProjectAssetPicker({ folder = false } = {}) {
  // Build a new input for every invocation. Chromium can retain directory
  // chooser state on a reused file input; a fresh input also guarantees that
  // the individual-file path never carries webkitdirectory/directory.
  const input = document.createElement("input");
  input.type = "file";
  input.multiple = true;
  input.hidden = true;
  input.setAttribute("aria-hidden", "true");
  if (folder) input.setAttribute("webkitdirectory", "");
  const generation = state.projectGeneration;
  const cleanup = () => input.remove();
  input.addEventListener("change", async () => {
    try { await addProjectAssets(input.files || [], generation); }
    finally { cleanup(); }
  }, { once: true });
  input.addEventListener("cancel", cleanup, { once: true });
  document.body.appendChild(input);
  input.click();
}

$("#projectAssetPicker").addEventListener("click", (event) => {
  event.stopPropagation();
  setProjectAssetMenu($("#projectAssetMenu").hidden);
});
$("#chooseProjectAssets").addEventListener("click", (event) => {
  event.stopPropagation();
  setProjectAssetMenu(false);
  openProjectAssetPicker();
});
$("#chooseProjectFolder").addEventListener("click", (event) => {
  event.stopPropagation();
  setProjectAssetMenu(false);
  openProjectAssetPicker({ folder: true });
});
document.addEventListener("click", (event) => {
  if (!event.target.closest(".project-assets-actions")) setProjectAssetMenu(false);
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !$("#projectAssetMenu").hidden) setProjectAssetMenu(false);
}, true);
$("#projectAssetsDropzone").addEventListener("click", (event) => {
  if (event.target === event.currentTarget || event.target.tagName === "B" || event.target.tagName === "SMALL") {
    openProjectAssetPicker();
  }
});
$("#projectAssetsDropzone").addEventListener("dragover", (event) => {
  event.preventDefault();
  event.currentTarget.classList.add("dragging");
});
$("#projectAssetsDropzone").addEventListener("dragleave", (event) => event.currentTarget.classList.remove("dragging"));
$("#projectAssetsDropzone").addEventListener("drop", async (event) => {
  event.preventDefault();
  event.currentTarget.classList.remove("dragging");
  const generation = state.projectGeneration;
  if (!beginProjectAssetOperation(generation)) return;
  try {
    const files = await projectFilesFromDrop(event.dataTransfer);
    if (generation !== state.projectGeneration || !$("#projectDialog").open) return;
    await addProjectAssets(files, generation);
  } catch (error) {
    if (generation === state.projectGeneration && $("#projectDialog").open) {
      toast(`Could not add the dropped project references: ${error.message || error}`);
    }
  } finally {
    endProjectAssetOperation(generation);
  }
});
$("#seedMinus").addEventListener("click", () => {
  $("#newSeedCount").value = String(Math.max(2, (Number($("#newSeedCount").value) || 10) - 1));
});
$("#seedPlus").addEventListener("click", () => {
  $("#newSeedCount").value = String(Math.min(20, (Number($("#newSeedCount").value) || 10) + 1));
});
$("#projectDialogClose").addEventListener("click", () => $("#projectDialog").close());
$("#projectDialog").addEventListener("close", () => {
  state.projectGeneration += 1;
  state.projectAssetOperationsPending = 0;
  setProjectAssetMenu(false);
});
$("#seedDialogClose").addEventListener("click", () => $("#seedDialog").close());
$("#seedDialog").addEventListener("close", () => {
  state.seedGeneration += 1;
  state.seedSessionId = null;
  state.seedProjectId = null;
  $("#seedPreview").src = "about:blank";
});
$("#finishSeeds").addEventListener("click", finishSeedSelection);
$("#openSeedPreview").addEventListener("click", () => {
  const url = $("#openSeedPreview").dataset.url;
  if (url) {
    const tab = reservePreviewWindow(`webkit-seed-${state.seedCurrentId || "preview"}`);
    rememberPreviewWindow(sessionById(state.seedSessionId), tab);
    if (!navigatePreviewWindow(tab, url)) toast(`Preview ready at ${url}. Allow popups to open it automatically.`);
  }
});
$("#openSeedChat").addEventListener("click", () => {
  const session = sessionById(state.seedSessionId);
  if (!session) return;
  $("#seedDialog").close();
  openSession(session, $("#seedOnboardingButton"));
});
$("#settingsDialogClose").addEventListener("click", () => $("#settingsDialog").close());
$("#settingsDialog").addEventListener("cancel", (event) => {
  if (state.settingsSaveInFlight) event.preventDefault();
});
$("#settingsDialog").addEventListener("close", () => {
  state.settingsGeneration += 1;
  hotkeyCapture = null;
});
$("#closeChat").addEventListener("click", () => {
  const returnFocus = state.chatReturnFocus;
  state.chatGeneration += 1;
  state.chatAttachmentReadsPending = 0;
  state.chatSendInFlight = false;
  state.chatTurn = null;
  $("#chatDrawer").hidden = true;
  state.chatSessionId = null;
  state.chatReturnFocus = null;
  if (returnFocus && returnFocus.isConnected && !returnFocus.closest("[hidden]")) returnFocus.focus();
  else $("#addProjectButton").focus();
});
$("#chatForm").addEventListener("submit", sendChat);
$("#newAgentReasoning").addEventListener("change", () => {
  const project = selectedProject();
  if (project) safeLocalStorageSet(`wkcc:reasoning:${project.provider}`, $("#newAgentReasoning").value);
});
$("#chatReasoning").addEventListener("change", changeChatReasoning);
$("#attachButton").addEventListener("click", () => $("#chatFiles").click());
$("#chatFiles").addEventListener("change", async (event) => {
  await addChatFiles(event.target.files || []);
  event.target.value = "";
});
$("#chatForm").addEventListener("dragover", (event) => {
  event.preventDefault();
  $("#chatForm").classList.add("dragging");
});
$("#chatForm").addEventListener("dragleave", () => $("#chatForm").classList.remove("dragging"));
$("#chatForm").addEventListener("drop", async (event) => {
  event.preventDefault();
  $("#chatForm").classList.remove("dragging");
  await addChatFiles(event.dataTransfer?.files || []);
});
$("#chatInput").addEventListener("paste", async (event) => {
  const files = [...(event.clipboardData?.items || [])]
    .filter((item) => item.kind === "file").map((item) => item.getAsFile()).filter(Boolean);
  if (files.length) {
    event.preventDefault();
    await addChatFiles(files);
  }
});
$("#mergeButton").addEventListener("click", () => askConfirm("merge"));
$("#discardButton").addEventListener("click", () => askConfirm("discard"));
$("#confirmAction").addEventListener("click", () => state.confirmCallback && state.confirmCallback());
$("#confirmDialog").addEventListener("cancel", (event) => {
  if (state.confirmBusy) event.preventDefault();
});
$("#confirmDialog").addEventListener("close", () => {
  state.confirmGeneration += 1;
  state.confirmBusy = false;
  state.confirmCallback = null;
  $("#confirmDialog button[value=\"cancel\"]").disabled = false;
  setBusy($("#confirmAction"), false);
});

initialize();
setInterval(() => refreshProjects().catch(() => {}), 2200);
setInterval(() => pollEvents().catch(() => {}), 900);
setInterval(() => pollSeedStatus().catch(() => {}), 1600);
