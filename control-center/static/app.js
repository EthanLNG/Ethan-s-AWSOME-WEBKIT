const COLORS = [
  { slug: "blue", emoji: "🔵", hex: "#3294e2" },
  { slug: "red", emoji: "🔴", hex: "#ed5d55" },
  { slug: "green", emoji: "🟢", hex: "#44aa71" },
  { slug: "orange", emoji: "🟠", hex: "#ee9c3a" },
  { slug: "purple", emoji: "🟣", hex: "#9b6bd6" },
];

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
  chatSessionId: null,
  chatCursor: 0,
  projectMode: "create",
  confirmCallback: null,
  pendingAttachments: [],
};
let hotkeyDraft = { toggleHotkey: "KeyC", dictateHotkey: "KeyV" };
let hotkeyCapture = null;

const $ = (selector) => document.querySelector(selector);

async function api(path, options = {}) {
  const request = { ...options, headers: { "Content-Type": "application/json", ...(options.headers || {}) } };
  if (request.body && typeof request.body !== "string") request.body = JSON.stringify(request.body);
  const response = await fetch(path, request);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

function toast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { node.hidden = true; }, 3800);
}

function setBusy(button, busy, label) {
  if (!button.dataset.label) button.dataset.label = button.textContent;
  button.disabled = busy;
  button.textContent = busy ? label : button.dataset.label;
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
    $("#settingsError").textContent = "Choose a regular key—not a modifier or Escape.";
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
    tools.indexOf(name) === index && !(state.system[name] || {}).installed
  ));
  visibleTools.forEach((name) => {
    const info = state.system[name] || { installed: false };
    const pill = document.createElement(name === "git" ? "button" : "span");
    pill.className = "status-pill missing";
    pill.textContent = `${name === "claude" ? "Claude" : name[0].toUpperCase() + name.slice(1)} missing`;
    pill.title = info.version || info.path || "Not found on PATH";
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
    : "CLI not found — install it before adding";
  $("#settingsClaudeStatus").textContent = state.system.claude?.installed
    ? "Ready on this computer"
    : "CLI not found — install it before adding";
  const github = state.system.github || { installed: false, authenticated: false };
  $("#githubConnectionStatus").textContent = github.authenticated
    ? "GitHub CLI is connected. New websites become private GitHub repositories and accepted merges push automatically."
    : "Connect GitHub to Codex or Claude Code, or run gh auth login. Existing GitHub remotes are detected automatically.";
  hotkeyDraft = {
    toggleHotkey: state.settings.toggleHotkey || "KeyC",
    dictateHotkey: state.settings.dictateHotkey || "KeyV",
  };
  hotkeyCapture = null;
  renderHotkeySettings();
  $("#settingsError").textContent = "";
  $("#settingsDialog").showModal();
}

async function saveSettings(event) {
  event.preventDefault();
  const dictationInput = document.querySelector('input[name="dictationMode"]:checked');
  const interactionInput = document.querySelector('input[name="interactionMode"]:checked');
  const providers = [...document.querySelectorAll('input[name="settingsProvider"]:checked')]
    .map((input) => input.value);
  const button = $("#saveSettings");
  $("#settingsError").textContent = "";
  setBusy(button, true, "Saving…");
  try {
    const providerResult = await api("/api/providers", {
      method: "POST",
      body: { providers },
    });
    const result = await api("/api/settings", {
      method: "POST",
      body: {
        dictationMode: dictationInput ? dictationInput.value : "speech",
        interactionMode: interactionInput ? interactionInput.value : "browse-default",
        toggleHotkey: hotkeyDraft.toggleHotkey,
        dictateHotkey: hotkeyDraft.dictateHotkey,
      },
    });
    state.providers = providerResult.providers;
    state.settings = result.settings;
    renderStatus();
    renderShortcutGuide();
    $("#settingsDialog").close();
    const restarted = result.previews.restarted || 0;
    const deferred = result.previews.deferred || 0;
    toast(`Settings saved.${restarted ? ` Restarted ${restarted} preview${restarted === 1 ? "" : "s"}.` : ""}${deferred ? ` ${deferred} busy preview will use it next time.` : ""}`);
  } catch (error) {
    $("#settingsError").textContent = error.message;
  } finally { setBusy(button, false); }
}

function renderProjects() {
  const list = $("#projectList");
  list.replaceChildren();
  state.projects.forEach((project) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `project-link ${project.id === state.selectedProjectId ? "active" : ""}`;
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

function selectProject(id) {
  state.selectedProjectId = id;
  renderProjects();
  renderProjectView();
}

function renderProjectView() {
  const project = selectedProject();
  if (!project) return;
  $("#projectName").textContent = project.name;
  $("#projectPath").textContent = project.path;
  $("#projectProvider").textContent = `${project.provider === "codex" ? "Codex" : "Claude Code"} project`;
  const defaultEffort = localStorage.getItem(`wkcc:reasoning:${project.provider}`) || "medium";
  fillReasoning($("#newAgentReasoning"), project.provider, defaultEffort);
  const active = (project.sessions || []).filter((session) => ["active", "busy", "merging", "error"].includes(session.status));
  const byColor = new Map(active.map((session) => [session.color, session]));
  const grid = $("#colorGrid");
  grid.replaceChildren();
  COLORS.forEach((color) => {
    const session = byColor.get(color.slug);
    const card = document.createElement("button");
    card.type = "button";
    card.className = `color-card ${session ? "active" : ""}`;
    card.style.setProperty("--color", color.hex);
    const emoji = document.createElement("span");
    emoji.className = "emoji";
    emoji.textContent = color.emoji;
    const title = document.createElement("strong");
    title.textContent = color.slug;
    const hint = document.createElement("small");
    hint.textContent = session ? `${session.provider} · ${session.status}` : "Start isolated agent";
    hint.dataset.working = String(!!session && ["busy", "merging"].includes(session.status));
    card.append(emoji, title, hint);
    if (session) {
      const live = document.createElement("span");
      live.className = "active-label";
      live.textContent = "Open";
      card.appendChild(live);
      card.addEventListener("click", () => {
        window.open(session.previewUrl, `webkit-${project.id}-${color.slug}`);
      });
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
    title.textContent = `${capitalize(session.color)} · ${session.provider === "codex" ? "Codex" : "Claude Code"}`;
    const branch = document.createElement("small");
    branch.textContent = session.branch;
    info.append(title, branch);
    const status = document.createElement("span");
    status.className = `session-state ${session.status}`;
    status.textContent = session.status;
    const open = document.createElement("button");
    open.type = "button";
    open.className = "open-chat";
    open.textContent = "Chat";
    open.addEventListener("click", () => openSession(session));
    row.append(emoji, info, status, open);
    list.appendChild(row);
  });
}

async function startColor(color, button) {
  const project = selectedProject();
  if (!project) return;
  const tabName = `webkit-${project.id}-${color}`;
  const previewTab = window.open("about:blank", tabName);
  setBusy(button, true, "Starting…");
  try {
    const { session } = await api("/api/sessions/start", {
      method: "POST", body: { projectId: project.id, color, reasoningEffort: $("#newAgentReasoning").value },
    });
    if (previewTab) previewTab.location.replace(session.previewUrl);
    else toast(`Preview ready at ${session.previewUrl}. Allow popups to open it automatically.`);
    await refreshProjects();
    toast(`${session.emoji} ${capitalize(color)} agent is live.`);
  } catch (error) {
    if (previewTab) previewTab.close();
    toast(error.message);
  } finally { setBusy(button, false); }
}

function openSession(session) {
  state.chatSessionId = session.id;
  state.chatCursor = 0;
  $("#chatDrawer").hidden = false;
  $("#chatColor").textContent = `${session.emoji} ${session.color} worktree`;
  $("#chatTitle").textContent = session.provider === "codex" ? "Codex" : "Claude Code";
  $("#chatStatus").className = `live-dot ${session.status}`;
  $("#chatReasoningLabel").textContent = session.provider === "codex" ? "Codex reasoning" : "Claude effort";
  fillReasoning($("#chatReasoning"), session.provider, session.reasoningEffort || "medium");
  state.pendingAttachments = [];
  renderAttachments();
  $("#chatEvents").replaceChildren();
  pollEvents();
}

function currentSession() {
  for (const project of state.projects) {
    const session = (project.sessions || []).find((item) => item.id === state.chatSessionId);
    if (session) return session;
  }
  return null;
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

async function addChatFiles(files) {
  const incoming = [...files].filter((file) => file && file.size);
  for (const file of incoming) {
    if (file.size > 20 * 1024 * 1024) {
      toast(`${file.name} is larger than 20 MB.`);
      continue;
    }
    const data = await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result).split(",", 2)[1] || "");
      reader.onerror = reject;
      reader.readAsDataURL(file);
    });
    state.pendingAttachments.push({ name: file.name || "pasted-file", type: file.type || "application/octet-stream", data });
  }
  renderAttachments();
}

async function changeChatReasoning() {
  const session = currentSession();
  if (!session) return;
  try {
    const result = await api(`/api/sessions/${session.id}/reasoning`, {
      method: "POST", body: { reasoningEffort: $("#chatReasoning").value },
    });
    session.reasoningEffort = result.reasoningEffort;
    toast(`${session.provider === "codex" ? "Codex reasoning" : "Claude effort"} set to ${result.reasoningEffort}.`);
  } catch (error) { toast(error.message); }
}

async function pollEvents() {
  if (!state.chatSessionId || $("#chatDrawer").hidden) return;
  const expected = state.chatSessionId;
  try {
    const data = await api(`/api/sessions/${expected}/events?after=${state.chatCursor}`);
    if (expected !== state.chatSessionId) return;
    const target = $("#chatEvents");
    data.events.forEach((event) => {
      const node = document.createElement("div");
      node.className = `event ${event.role} ${event.kind}`;
      node.textContent = event.text;
      target.appendChild(node);
    });
    if (data.events.length) target.scrollTop = target.scrollHeight;
    state.chatCursor = data.next;
  } catch (error) {
    if (expected === state.chatSessionId) toast(error.message);
  }
}

async function sendChat(event) {
  event.preventDefault();
  const input = $("#chatInput");
  const message = input.value.trim();
  if ((!message && !state.pendingAttachments.length) || !state.chatSessionId) return;
  const button = $("#chatForm button.primary");
  setBusy(button, true, "Queued");
  try {
    await api(`/api/sessions/${state.chatSessionId}/message`, {
      method: "POST", body: { message, attachments: state.pendingAttachments },
    });
    input.value = "";
    state.pendingAttachments = [];
    renderAttachments();
    setTimeout(pollEvents, 150);
  } catch (error) { toast(error.message); }
  finally { setBusy(button, false); }
}

function askConfirm(kind) {
  const session = currentSession();
  if (!session) return;
  const isMerge = kind === "merge";
  $("#confirmEyebrow").textContent = isMerge ? "Keep the work" : "Permanent action";
  $("#confirmTitle").textContent = isMerge ? "Merge this color into main?" : "Discard this color session?";
  $("#confirmText").textContent = isMerge
    ? `The ${session.emoji} branch will be merged into main, then its worktree will close.`
    : `All unmerged work in ${session.branch} will be deleted. This cannot be undone.`;
  $("#confirmAction").textContent = isMerge ? "Merge to main" : "Discard forever";
  $("#confirmAction").className = isMerge ? "primary" : "danger";
  state.confirmCallback = () => finishSession(session, kind);
  $("#confirmDialog").showModal();
}

async function finishSession(session, kind) {
  const button = $("#confirmAction");
  setBusy(button, true, kind === "merge" ? "Merging…" : "Discarding…");
  try {
    const body = kind === "discard" ? { confirmation: session.id } : {};
    const result = await api(`/api/sessions/${session.id}/${kind}`, { method: "POST", body });
    $("#confirmDialog").close();
    if (kind === "merge") {
      if (result.queued) {
        toast("The agent is integrating the branch. Open chat if it needs help with a conflict.");
        await refreshProjects();
        return;
      }
      const github = result.github || { connected: false, pushed: false };
      if (github.pushed) toast("Merged and updated main on GitHub.");
      else if (github.connected) toast(`Merged locally, but GitHub push failed: ${github.error || "try again when connected"}`);
      else toast("Merged locally. Connect GitHub to your coding agent so future merges update main automatically.");
    } else toast("Color worktree discarded.");
    $("#chatDrawer").hidden = true;
    state.chatSessionId = null;
    await refreshProjects();
  } catch (error) { toast(error.message); }
  finally { setBusy(button, false); }
}

function openProjectDialog() {
  $("#projectError").textContent = "";
  const select = $("#projectProviderSelect");
  select.replaceChildren();
  state.providers.forEach((provider) => {
    const option = document.createElement("option");
    option.value = provider;
    option.textContent = provider === "codex" ? "Codex" : "Claude Code";
    select.appendChild(option);
  });
  $("#projectDialog").showModal();
}

function setProjectMode(mode) {
  state.projectMode = mode;
  document.querySelectorAll("[data-project-mode]").forEach((button) => {
    button.classList.toggle("active", button.dataset.projectMode === mode);
  });
  $("#createFields").hidden = mode !== "create";
  $("#existingFields").hidden = mode !== "existing";
  $("#saveProject").textContent = mode === "create" ? "Create website" : "Add project";
}

async function chooseProjectFolder(event) {
  const button = event.currentTarget;
  const field = $(`#${button.dataset.folderTarget}`);
  const errorNode = $("#projectError");
  errorNode.textContent = "";
  setBusy(button, true, "Choosing…");
  try {
    const result = await api("/api/system/choose-folder", {
      method: "POST",
      body: { initial: field.value, purpose: button.dataset.folderPurpose },
    });
    if (!result.cancelled && result.path) {
      field.value = result.path;
      field.title = result.path;
    }
  } catch (error) { errorNode.textContent = error.message; }
  finally { setBusy(button, false); }
}

async function saveProject(event) {
  event.preventDefault();
  const button = $("#saveProject");
  const errorNode = $("#projectError");
  errorNode.textContent = "";
  setBusy(button, true, state.projectMode === "create" ? "Creating…" : "Adding…");
  try {
    const provider = $("#projectProviderSelect").value;
    const isCreate = state.projectMode === "create";
    const path = isCreate ? "/api/projects/create" : "/api/projects/existing";
    const body = isCreate
      ? { name: $("#newName").value, parent: $("#newParent").value, provider }
      : { path: $("#existingPath").value, provider };
    const { project } = await api(path, { method: "POST", body });
    $("#projectDialog").close();
    await refreshProjects();
    selectProject(project.id);
    const registeredProject = state.projects.find((item) => item.id === project.id) || project;
    toast(registeredProject.github?.connected
      ? "Project ready with automatic GitHub sync."
      : "Project ready locally. Connect GitHub to your coding agent and WebKit will detect it automatically.");
  } catch (error) { errorNode.textContent = error.message; }
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
  const data = await api("/api/projects");
  state.projects = data.projects;
  if (state.selectedProjectId && !state.projects.some((p) => p.id === state.selectedProjectId)) state.selectedProjectId = null;
  if (!state.selectedProjectId && state.projects.length) state.selectedProjectId = state.projects[0].id;
  renderProjects();
  renderProjectView();
  const session = currentSession();
  if (session) $("#chatStatus").className = `live-dot ${session.status}`;
}

async function initialize() {
  try {
    const data = await api("/api/bootstrap");
    state.providers = data.providers;
    state.projects = data.projects;
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

$("#homeButton").addEventListener("click", () => { state.selectedProjectId = null; renderProjects(); });
[$("#addProjectButton"), $("#emptyAddButton")].forEach((button) => button.addEventListener("click", openProjectDialog));
$("#saveProviders").addEventListener("click", saveProviders);
$("#installShortcutButton").addEventListener("click", installShortcut);
$("#settingsButton").addEventListener("click", openSettings);
$("#shortcutGuide").addEventListener("click", openSettings);
$("#settingsForm").addEventListener("submit", saveSettings);
document.querySelectorAll("[data-hotkey-setting]").forEach((button) => button.addEventListener("click", beginHotkeyCapture));
document.querySelectorAll('input[name="interactionMode"]').forEach((input) => input.addEventListener("change", renderHotkeySettings));
document.addEventListener("keydown", captureHotkey, true);
document.querySelectorAll("[data-project-mode]").forEach((button) => button.addEventListener("click", () => setProjectMode(button.dataset.projectMode)));
document.querySelectorAll("[data-folder-target]").forEach((button) => button.addEventListener("click", chooseProjectFolder));
$("#projectForm").addEventListener("submit", saveProject);
$("#projectDialogClose").addEventListener("click", () => $("#projectDialog").close());
$("#settingsDialogClose").addEventListener("click", () => $("#settingsDialog").close());
$("#settingsDialog").addEventListener("close", () => { hotkeyCapture = null; });
$("#closeChat").addEventListener("click", () => { $("#chatDrawer").hidden = true; state.chatSessionId = null; });
$("#chatForm").addEventListener("submit", sendChat);
$("#newAgentReasoning").addEventListener("change", () => {
  const project = selectedProject();
  if (project) localStorage.setItem(`wkcc:reasoning:${project.provider}`, $("#newAgentReasoning").value);
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

initialize();
setInterval(() => refreshProjects().catch(() => {}), 2200);
setInterval(() => pollEvents().catch(() => {}), 900);
