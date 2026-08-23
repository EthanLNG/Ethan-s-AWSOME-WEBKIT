import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "control-center" / "static" / "app.js"
INDEX_HTML = ROOT / "control-center" / "static" / "index.html"
STYLES_CSS = ROOT / "control-center" / "static" / "styles.css"


NODE_HARNESS = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const source = fs.readFileSync(process.argv[2], "utf8");

function extractFunction(name) {
  const plain = `function ${name}(`;
  const asynchronous = `async function ${name}(`;
  let start = source.indexOf(asynchronous);
  if (start < 0) start = source.indexOf(plain);
  assert.notEqual(start, -1, `missing function ${name}`);
  const next = /\n(?:async )?function [A-Za-z_$][\w$]*\(/g;
  next.lastIndex = start + 1;
  const match = next.exec(source);
  return source.slice(start, match ? match.index + 1 : source.length);
}

function context(values = {}) {
  return vm.createContext({
    console,
    Promise,
    Set,
    TextEncoder,
    Object,
    String,
    Error,
    ...values,
  });
}

function install(ctx, ...names) {
  for (const name of names) {
    vm.runInContext(`${extractFunction(name)}\nthis.${name} = ${name};`, ctx);
  }
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((accept, decline) => {
    resolve = accept;
    reject = decline;
  });
  return { promise, resolve, reject };
}

async function testHomeSelectionPersists() {
  const projects = [{ id: "project-one", sessions: [] }];
  let projectRenders = 0;
  const ctx = context({
    state: {
      projects,
      projectsSignature: JSON.stringify(projects),
      selectedProjectId: null,
      homeSelected: true,
    },
    projectsRefreshPromise: null,
    projectsRefreshQueued: false,
    api: async () => ({ projects }),
    renderProjects: () => { projectRenders += 1; },
    renderProjectView: () => { projectRenders += 1; },
    currentSession: () => null,
    setChatStatus: () => {},
    refreshChangedPreviewWindows: () => {},
    closeFinishedPreviewWindows: () => {},
  });
  install(ctx, "refreshProjects", "selectProject");

  await ctx.refreshProjects();
  assert.equal(ctx.state.selectedProjectId, null, "refresh must preserve intentional Home selection");
  assert.equal(projectRenders, 0, "unchanged data should not trigger a hidden project render");

  ctx.renderProjects = () => {};
  ctx.renderProjectView = () => {};
  ctx.selectProject("project-one");
  assert.equal(ctx.state.homeSelected, false, "selecting a project must leave Home mode");

  const handlerStart = source.indexOf('$("#homeButton").addEventListener("click"');
  assert.notEqual(handlerStart, -1, "missing Home click handler");
  const handler = source.slice(handlerStart, source.indexOf("\n});", handlerStart) + 4);
  assert.match(handler, /state\.homeSelected\s*=\s*true/);
  assert.match(handler, /state\.selectedProjectId\s*=\s*null/);
}

async function testFinishedSessionsCloseTheirOwnedPreviewWindows() {
  let closes = 0;
  const replacements = [];
  const tab = {
    closed: false,
    location: { replace(url) { replacements.push(url); } },
    close() {
      this.closed = true;
      closes += 1;
    },
  };
  const previewTabs = new Map();
  const ctx = context({ previewTabs });
  install(ctx, "rememberPreviewWindow", "refreshChangedPreviewWindows", "closeFinishedPreviewWindows");
  const session = { id: "session-a", kind: "color", previewRevision: 0, previewUrl: "http://127.0.0.1:5311/" };

  ctx.rememberPreviewWindow(session, tab);
  ctx.refreshChangedPreviewWindows([{ sessions: [session] }]);
  assert.equal(replacements.length, 0, "an unchanged preview revision must not reload");
  ctx.refreshChangedPreviewWindows([{ sessions: [{ ...session, previewRevision: 1 }] }]);
  assert.deepEqual(replacements, [session.previewUrl], "a changed preview revision must reload the owned tab");
  ctx.closeFinishedPreviewWindows([{ sessions: [session] }]);
  assert.equal(closes, 0, "a live session must keep its preview tab");
  assert.equal(previewTabs.has(session.id), true);

  ctx.closeFinishedPreviewWindows([{ sessions: [] }]);
  assert.equal(closes, 1, "a completed session must close its preview tab");
  assert.equal(previewTabs.has(session.id), false);

  ctx.rememberPreviewWindow({ id: "support-a", kind: "support" }, tab);
  assert.equal(previewTabs.has("support-a"), false, "uncolored support agents have no preview tab");
  assert.match(extractFunction("startColor"), /rememberPreviewWindow\(session, previewTab\)/);
  assert.match(extractFunction("openSessionPreview"), /rememberPreviewWindow\(session, tab\)/);
}

function testMergedSessionsRenderBelowActiveSessions() {
  const ctx = context();
  install(ctx, "sessionsForDisplay");
  const displayed = ctx.sessionsForDisplay({ sessions: [
    { id: "merged-old", status: "merged", updatedAt: "2026-01-01", kind: "color" },
    { id: "active", status: "active", updatedAt: "2026-01-03", kind: "color" },
    { id: "merged-new", status: "merged", updatedAt: "2026-01-02", kind: "color" },
    { id: "seed", status: "merged", updatedAt: "2026-01-04", kind: "seeds" },
  ] });
  assert.deepEqual(
    Array.from(displayed, (session) => session.id),
    ["active", "merged-new", "merged-old"],
    "merged cards must stay below active cards and newest merged cards come first",
  );
  const renderSessionsSource = extractFunction("renderSessions");
  assert.match(renderSessionsSource, /row\.className = `session-row\$\{merged/);
  assert.match(renderSessionsSource, /click to clear/);
  assert.match(extractFunction("dismissMergedSession"), /\/dismiss/);
}

async function testApiPreservesStructuredErrorDetails() {
  const ctx = context({
    CONTROL_CENTER_TOKEN: "test-token-1234567890",
    fetch: async () => ({
      ok: false,
      status: 409,
      json: async () => ({
        error: "Update required.",
        details: {
          code: "webkit_update_required",
          installedVersion: "0.4.1",
          requiredVersion: "0.8.14",
        },
      }),
    }),
  });
  install(ctx, "api");
  let failure = null;
  try {
    await ctx.api("/api/projects/existing", { method: "POST", body: {} });
  } catch (error) {
    failure = error;
  }
  assert.ok(failure);
  assert.equal(failure.status, 409);
  assert.equal(failure.details.code, "webkit_update_required");
  assert.equal(failure.details.installedVersion, "0.4.1");
  assert.equal(failure.details.requiredVersion, "0.8.14");
}

async function testRegisteredProjectCanUpdateItsVendoredWebkit() {
  const requests = [];
  const busy = [];
  const toasts = [];
  let refreshes = 0;
  const project = {
    id: "project-one",
    path: "/managed/project-one",
    sourcePath: "/source/project-one",
    provider: "codex",
    webkitUpdate: {
      code: "webkit_update_required",
      installedVersion: "0.8.3",
      requiredVersion: "0.8.14",
    },
  };
  const ctx = context({
    selectedProject: () => project,
    $: () => ({}),
    setBusy: (_button, value) => busy.push(value),
    api: async (path, options) => {
      requests.push({ path, options });
      return { project: { webkitUpdated: project.webkitUpdate } };
    },
    refreshProjects: async () => { refreshes += 1; },
    toast: (message) => toasts.push(message),
  });
  install(ctx, "updateSelectedProjectWebkit");

  await ctx.updateSelectedProjectWebkit();

  assert.equal(requests.length, 1);
  assert.equal(requests[0].path, "/api/projects/existing");
  assert.equal(requests[0].options.body.path, project.sourcePath);
  assert.equal(requests[0].options.body.provider, "codex");
  assert.equal(requests[0].options.body.updateWebkit, true);
  assert.deepEqual(busy, [true, false]);
  assert.equal(refreshes, 1);
  assert.match(toasts[0], /0\.8\.3 to 0\.8\.14/);
}

async function testChatAsyncWorkStaysWithItsSession() {
  const openSessionSource = extractFunction("openSession");
  assert.match(openSessionSource, /state\.chatAttachmentReadsPending\s*=\s*0/);
  assert.match(openSessionSource, /state\.chatSendInFlight\s*=\s*false/);
  const closeChatStart = source.indexOf('$("#closeChat").addEventListener("click"');
  assert.notEqual(closeChatStart, -1, "missing chat close handler");
  const closeChatHandler = source.slice(closeChatStart, source.indexOf("\n});", closeChatStart) + 4);
  assert.match(closeChatHandler, /state\.chatGeneration\s*\+=\s*1/);
  assert.match(closeChatHandler, /state\.chatAttachmentReadsPending\s*=\s*0/);
  assert.match(closeChatHandler, /state\.chatSendInFlight\s*=\s*false/);

  const readers = [];
  class FileReader {
    readAsDataURL(file) {
      this.file = file;
      readers.push(this);
    }
  }
  const toasts = [];
  let attachmentRenders = 0;
  const state = {
    chatSessionId: "session-a",
    chatGeneration: 1,
    pendingAttachments: [],
    chatAttachmentReadsPending: 0,
    chatSendInFlight: false,
  };
  const chatButton = {};
  const ctx = context({
    state,
    FileReader,
    MAX_CHAT_ATTACHMENTS: 20,
    MAX_CHAT_ATTACHMENT_BYTES: 20 * 1024 * 1024,
    MAX_CHAT_ATTACHMENTS_TOTAL_BYTES: 20 * 1024 * 1024,
    toast: (message) => toasts.push(message),
    renderAttachments: () => { attachmentRenders += 1; },
    setBusy: () => {},
    $: () => chatButton,
  });
  install(ctx, "beginChatAttachmentRead", "endChatAttachmentRead", "addChatFiles");

  const staleSuccess = ctx.addChatFiles([{ name: "old.png", type: "image/png", size: 5 }]);
  assert.equal(readers.length, 1);
  state.chatSessionId = "session-b";
  state.chatGeneration += 1;
  state.chatAttachmentReadsPending = 0;
  readers[0].result = "data:image/png;base64,b2xk";
  readers[0].onload();
  await staleSuccess;
  assert.equal(state.pendingAttachments.length, 0, "old-session files must not enter the new session");
  assert.equal(state.chatAttachmentReadsPending, 0, "old reads must not change the new session counter");
  assert.equal(attachmentRenders, 0);

  state.chatSessionId = "session-a";
  state.chatGeneration += 1;
  const staleFailure = ctx.addChatFiles([{ name: "broken.png", type: "image/png", size: 5 }]);
  assert.equal(readers.length, 2);
  state.chatSessionId = "session-b";
  state.chatGeneration += 1;
  state.chatAttachmentReadsPending = 0;
  readers[1].onerror(new Error("old read failed"));
  await staleFailure;
  assert.equal(toasts.length, 0, "old-session FileReader errors must not bleed into the new session");

  const oldAttachment = { name: "old.txt", size: 2, data: "b2xk" };
  const newAttachment = { name: "new.txt", size: 2, data: "bmV3" };
  const input = { value: "message for A" };
  const button = {};
  const request = deferred();
  let busyCalls = 0;
  let eventPolls = 0;
  let chatApiCalls = 0;
  Object.assign(ctx, {
    state: {
      chatSessionId: "session-a",
      chatGeneration: 10,
      pendingAttachments: [oldAttachment],
      chatAttachmentReadsPending: 1,
      chatSendInFlight: false,
    },
    MAX_PROVIDER_PROMPT_BYTES: 16 * 1024,
    $: (selector) => selector === "#chatInput" ? input : button,
    api: () => {
      chatApiCalls += 1;
      return request.promise;
    },
    setBusy: () => { busyCalls += 1; },
    renderAttachments: () => { attachmentRenders += 1; },
    setTimeout: () => { eventPolls += 1; },
    pollEvents: () => {},
    toast: (message) => toasts.push(message),
  });
  install(ctx, "sendChat");
  await ctx.sendChat({ preventDefault() {} });
  assert.equal(chatApiCalls, 0, "send must wait instead of omitting an attachment still being read");
  assert.ok(toasts.some((message) => message.includes("finish loading")));
  ctx.state.chatAttachmentReadsPending = 0;
  const sending = ctx.sendChat({ preventDefault() {} });
  assert.equal(chatApiCalls, 1);
  ctx.state.chatSessionId = "session-b";
  ctx.state.chatGeneration += 1;
  ctx.state.pendingAttachments = [newAttachment];
  ctx.state.chatSendInFlight = false;
  input.value = "new draft";
  request.resolve({});
  await sending;
  assert.equal(input.value, "new draft", "old send completion must preserve the new draft");
  assert.equal(ctx.state.pendingAttachments.length, 1);
  assert.equal(ctx.state.pendingAttachments[0], newAttachment);
  assert.equal(ctx.state.chatSendInFlight, false, "old send must not change the new session lock");
  assert.equal(busyCalls, 1, "old completion must not restore a reused session button");
  assert.equal(eventPolls, 0, "old completion must not schedule polling for the new session");
}

async function testSharedModalCompletionsAreGenerationScoped() {
  const reasoningRequest = deferred();
  const reasoningToasts = [];
  const reasoningSelect = { value: "high", disabled: false };
  const session = {
    id: "session-a",
    provider: "codex",
    reasoningEffort: "medium",
  };
  const reasoningState = {
    chatSessionId: "session-a",
    chatGeneration: 1,
    chatReasoningRequest: 0,
  };
  const reasoning = context({
    state: reasoningState,
    currentSession: () => session,
    $: () => reasoningSelect,
    api: () => reasoningRequest.promise,
    reasoningLevels: () => ["low", "medium", "high", "xhigh"],
    toast: (message) => reasoningToasts.push(message),
  });
  install(reasoning, "changeChatReasoning");
  const changing = reasoning.changeChatReasoning();
  assert.equal(reasoningSelect.disabled, true);
  reasoningState.chatSessionId = "session-b";
  reasoningState.chatGeneration += 1;
  reasoningState.chatReasoningRequest += 1;
  reasoningSelect.disabled = false;
  reasoningSelect.value = "xhigh";
  reasoningRequest.resolve({ reasoningEffort: "high" });
  await changing;
  assert.equal(session.reasoningEffort, "medium", "old reasoning response must not mutate stale session state");
  assert.equal(reasoningSelect.value, "xhigh", "old reasoning response must preserve the new session control");
  assert.equal(reasoningSelect.disabled, false);
  assert.equal(reasoningToasts.length, 0);

  const finishRequest = deferred();
  const finishToasts = [];
  const finishBusy = [];
  let refreshes = 0;
  let closes = 0;
  const confirmButton = {};
  const cancelButton = { disabled: false };
  const confirmDialog = { close() { closes += 1; } };
  const drawer = { hidden: false };
  const finishState = {
    confirmGeneration: 4,
    confirmBusy: false,
    chatSessionId: "session-a",
    chatGeneration: 9,
  };
  const finishing = context({
    state: finishState,
    $: (selector) => ({
      "#confirmAction": confirmButton,
      '#confirmDialog button[value="cancel"]': cancelButton,
      "#confirmDialog": confirmDialog,
      "#chatDrawer": drawer,
    })[selector],
    targetBranchForSession: () => "main",
    setBusy: (_button, busy) => finishBusy.push(busy),
    api: () => finishRequest.promise,
    refreshProjects: async () => { refreshes += 1; },
    toast: (message) => finishToasts.push(message),
  });
  install(finishing, "finishSession");
  const finish = finishing.finishSession({ id: "session-a" }, "discard", 4);
  assert.equal(cancelButton.disabled, true);
  finishState.confirmGeneration = 5;
  finishState.confirmBusy = false;
  finishState.chatSessionId = "session-b";
  finishState.chatGeneration += 1;
  cancelButton.disabled = false;
  finishRequest.resolve({});
  await finish;
  assert.equal(closes, 0, "old operation must not close a newly reused confirmation dialog");
  assert.equal(drawer.hidden, false, "old operation must not hide a newly opened chat drawer");
  assert.equal(finishState.chatSessionId, "session-b");
  assert.equal(finishBusy.length, 1, "old operation must not restore a reused confirmation button");
  assert.equal(finishToasts.length, 0);
  assert.equal(refreshes, 1, "project state must still refresh after the completed operation");

  const folderRequest = deferred();
  const folderButton = { dataset: { folderTarget: "newParent", folderPurpose: "parent" } };
  const folderField = { value: "/new-attempt", title: "/new-attempt" };
  const folderError = { textContent: "" };
  const folderUpdatePrompt = { hidden: true };
  const projectDialog = { open: true };
  const folderBusy = [];
  const projectState = { projectGeneration: 2 };
  const folder = context({
    state: projectState,
    $: (selector) => ({
      "#newParent": folderField,
      "#projectError": folderError,
      "#projectUpdatePrompt": folderUpdatePrompt,
      "#projectDialog": projectDialog,
    })[selector],
    api: () => folderRequest.promise,
    setBusy: (_button, busy) => folderBusy.push(busy),
  });
  install(folder, "chooseProjectFolder");
  const choosing = folder.chooseProjectFolder({ currentTarget: folderButton });
  projectState.projectGeneration += 1;
  folderRequest.resolve({ cancelled: false, path: "/old-attempt" });
  await choosing;
  assert.equal(folderField.value, "/new-attempt", "old folder result must not populate a reopened dialog");
  assert.equal(folderError.textContent, "");
  assert.equal(folderBusy.length, 1, "old folder result must not restore a reused button");
}

async function testSettingsCompletionIsGenerationScoped() {
  const openSettingsSource = extractFunction("openSettings");
  const initialScrollReset = openSettingsSource.indexOf("settingsForm.scrollTop = 0");
  const showDialog = openSettingsSource.indexOf('$("#settingsDialog").showModal()');
  const closeFocus = openSettingsSource.indexOf('$("#settingsDialogClose").focus({ preventScroll: true })');
  const restoredScrollReset = openSettingsSource.lastIndexOf("settingsForm.scrollTop = 0");
  assert.ok(initialScrollReset >= 0, "settings must reopen at the top of the form");
  assert.ok(initialScrollReset < showDialog, "settings scroll must reset before the dialog opens");
  assert.ok(showDialog < closeFocus, "settings must focus its close control after opening");
  assert.ok(closeFocus < restoredScrollReset, "settings must defeat native focus-scroll restoration");

  const request = deferred();
  const provider = { value: "codex", checked: true };
  const dictation = { value: "speech", checked: true };
  const interaction = { value: "browse-default", checked: true };
  const saveButton = {};
  const closeButton = { disabled: false };
  const error = { textContent: "" };
  let closes = 0;
  const dialog = { open: true, close() { closes += 1; } };
  const busy = [];
  const toasts = [];
  const requests = [];
  const state = {
    settingsGeneration: 7,
    settingsSaveInFlight: false,
    providers: [],
    settings: {},
  };
  const ctx = context({
    state,
    hotkeyDraft: { toggleHotkey: "KeyC", dictateHotkey: "KeyV" },
    document: {
      querySelector(selector) {
        if (selector === 'input[name="dictationMode"]:checked') return dictation;
        if (selector === 'input[name="interactionMode"]:checked') return interaction;
        return null;
      },
      querySelectorAll(selector) {
        return selector === 'input[name="settingsProvider"]:checked' ? [provider] : [];
      },
    },
    $: (selector) => ({
      "#saveSettings": saveButton,
      "#settingsDialogClose": closeButton,
      "#settingsError": error,
      "#settingsDialog": dialog,
    })[selector],
    api: (path, options) => {
      requests.push({ path, options });
      return request.promise;
    },
    setBusy: (_button, value) => busy.push(value),
    renderStatus: () => {},
    renderShortcutGuide: () => {},
    toast: (message) => toasts.push(message),
  });
  install(ctx, "saveSettings");

  const saving = ctx.saveSettings({ preventDefault() {} });
  assert.equal(requests.length, 1);
  assert.equal(requests[0].path, "/api/preferences", "settings must use one atomic preferences request");
  assert.equal(requests[0].options.body.providers.length, 1);
  assert.equal(requests[0].options.body.providers[0], "codex");
  assert.equal(closeButton.disabled, true);
  state.settingsGeneration += 1;
  error.textContent = "new dialog state";
  request.resolve({
    providers: ["codex"],
    settings: {
      dictationMode: "speech",
      interactionMode: "browse-default",
      toggleHotkey: "KeyC",
      dictateHotkey: "KeyV",
    },
    previews: { restarted: 0, deferred: 0 },
  });
  await saving;
  assert.equal(closes, 0, "old settings completion must not close a reopened dialog");
  assert.equal(error.textContent, "new dialog state", "old settings completion must not overwrite new dialog feedback");
  assert.equal(toasts.length, 0);
  assert.equal(state.providers.length, 1, "saved global preferences should still synchronize locally");
  assert.equal(state.settingsSaveInFlight, false);
  assert.deepEqual(busy, [true, false]);
  assert.equal(closeButton.disabled, false);
}

function directory(name, children) {
  return {
    name,
    isFile: false,
    isDirectory: true,
    createReader() {
      let sent = false;
      return {
        readEntries(success) {
          if (sent) success([]);
          else {
            sent = true;
            success(children);
          }
        },
      };
    },
  };
}

function fileEntry(name) {
  return {
    name,
    isFile: true,
    isDirectory: false,
    file(success) { success({ name, size: 1, type: "text/plain" }); },
  };
}

async function testProjectDropsAreBoundedAndRejected() {
  const ctx = context({
    state: { onboardingAssets: [] },
    MAX_PROJECT_ASSETS: 3,
    MAX_PROJECT_DROP_ENTRIES: 5,
    MAX_PROJECT_DROP_DEPTH: 2,
  });
  install(ctx, "readDroppedEntry", "projectFilesFromDrop");

  const tooManyEntries = directory("root", [
    { name: "1", isFile: false, isDirectory: false },
    { name: "2", isFile: false, isDirectory: false },
    { name: "3", isFile: false, isDirectory: false },
    { name: "4", isFile: false, isDirectory: false },
    { name: "5", isFile: false, isDirectory: false },
  ]);
  await assert.rejects(
    ctx.readDroppedEntry(tooManyEntries, "", { entries: 0, files: 0, maxFiles: 3 }, 0),
    /at most 5 entries/,
  );

  const tooDeep = directory("a", [directory("b", [directory("c", [directory("d", [])])])]);
  await assert.rejects(
    ctx.readDroppedEntry(tooDeep, "", { entries: 0, files: 0, maxFiles: 3 }, 0),
    /at most 2 levels deep/,
  );

  const tooManyFiles = directory("root", [
    fileEntry("1"), fileEntry("2"), fileEntry("3"), fileEntry("4"),
  ]);
  await assert.rejects(
    ctx.readDroppedEntry(tooManyFiles, "", { entries: 0, files: 0, maxFiles: 3 }, 0),
    /limited to 3 files/,
  );

  await assert.rejects(
    ctx.projectFilesFromDrop({ items: [], files: [{}, {}, {}, {}] }),
    /limited to 3 files/,
  );

  const dropStart = source.indexOf('$("#projectAssetsDropzone").addEventListener("drop"');
  assert.notEqual(dropStart, -1, "missing project drop handler");
  const dropHandler = source.slice(dropStart, source.indexOf("\n});", dropStart) + 4);
  assert.match(dropHandler, /try\s*\{/);
  assert.match(dropHandler, /catch\s*\(error\)/);
}

async function testConcurrentProjectReadsCannotExceedLimits() {
  const projectCloseStart = source.indexOf('$("#projectDialog").addEventListener("close"');
  assert.notEqual(projectCloseStart, -1, "missing project dialog close handler");
  const projectCloseHandler = source.slice(
    projectCloseStart,
    source.indexOf("\n});", projectCloseStart) + 4,
  );
  assert.match(projectCloseHandler, /state\.projectGeneration\s*\+=\s*1/);
  assert.match(projectCloseHandler, /state\.projectAssetOperationsPending\s*=\s*0/);

  const readers = [];
  class FileReader {
    readAsDataURL(file) {
      this.file = file;
      readers.push(this);
    }
  }
  const toasts = [];
  const state = {
    onboardingAssets: [],
    projectGeneration: 1,
    projectSaveInFlight: false,
    projectAssetOperationsPending: 0,
  };
  const ctx = context({
    state,
    projectGeneration: 1,
    FileReader,
    MAX_PROJECT_ASSETS: 1,
    MAX_PROJECT_ASSET_BYTES: 15 * 1024 * 1024,
    MAX_PROJECT_ASSETS_TOTAL_BYTES: 20 * 1024 * 1024,
    toast: (message) => toasts.push(message),
    renderProjectAssets: () => {},
    setBusy: () => {},
    $: (selector) => selector === "#projectDialog" ? { open: true } : {},
  });
  install(ctx, "beginProjectAssetOperation", "endProjectAssetOperation", "addProjectAssets");

  const first = ctx.addProjectAssets([{ name: "first.txt", size: 1, type: "text/plain" }]);
  const second = ctx.addProjectAssets([{ name: "second.txt", size: 1, type: "text/plain" }]);
  assert.equal(readers.length, 2);
  readers[0].result = "data:text/plain;base64,Zmlyc3Q=";
  readers[0].onload();
  await first;
  readers[1].result = "data:text/plain;base64,c2Vjb25k";
  readers[1].onload();
  await second;
  assert.equal(state.onboardingAssets.length, 1, "overlapping reads must recheck the current file limit");
  assert.ok(toasts.some((message) => message.includes("limited to 1 files")));
  assert.equal(state.projectAssetOperationsPending, 0);

  state.onboardingAssets = [];
  state.projectGeneration = 4;
  const stale = ctx.addProjectAssets([{ name: "old.txt", size: 1, type: "text/plain" }]);
  assert.equal(readers.length, 3);
  state.projectGeneration = 5;
  state.projectAssetOperationsPending = 0;
  readers[2].result = "data:text/plain;base64,b2xk";
  readers[2].onload();
  await stale;
  assert.equal(state.onboardingAssets.length, 0, "old project files must not enter a reopened dialog");
  assert.equal(state.projectAssetOperationsPending, 0, "old project completion must not change the new dialog counter");
}

async function testProjectSaveWaitsForPendingReferences() {
  let apiCalls = 0;
  const error = { textContent: "" };
  const fields = {
    "#saveProject": {},
    "#projectError": error,
    "#projectUpdatePrompt": { hidden: true },
    "#newBrandBrief": { value: "" },
  };
  const ctx = context({
    state: {
      projectGeneration: 3,
      projectMode: "create",
      createStep: 3,
      projectAssetOperationsPending: 1,
      projectSaveInFlight: false,
    },
    MAX_PROJECT_BRIEF_CHARS: 20000,
    $: (selector) => fields[selector],
    api: async () => { apiCalls += 1; },
  });
  install(ctx, "saveProject");
  await ctx.saveProject({ preventDefault() {} });
  assert.equal(apiCalls, 0, "project creation must not snapshot assets while references are still loading");
  assert.match(error.textContent, /finish loading/);
}

async function testExistingProjectOffersAndRequestsWebkitUpdate() {
  const requests = [];
  const errorNode = { textContent: "" };
  const updatePrompt = { hidden: true };
  const versions = { textContent: "" };
  const dialog = { open: true };
  const saveButton = { id: "saveProject" };
  const updateButton = { id: "updateProjectWebkit" };
  const fields = {
    "#saveProject": saveButton,
    "#projectError": errorNode,
    "#projectUpdatePrompt": updatePrompt,
    "#projectUpdateVersions": versions,
    "#projectDialog": dialog,
    "#projectProviderSelect": { value: "codex" },
    "#existingPath": { value: "/projects/old-site" },
    "#newBrandBrief": { value: "" },
  };
  const state = {
    projectGeneration: 4,
    projectMode: "existing",
    createStep: 1,
    projectAssetOperationsPending: 0,
    projectSaveInFlight: false,
    onboardingAssets: [],
  };
  const ctx = context({
    state,
    MAX_PROJECT_BRIEF_CHARS: 20000,
    $: (selector) => fields[selector],
    setBusy: () => {},
    api: async (path, options) => {
      requests.push({ path, options });
      const failure = new Error("This project needs a Webkit update.");
      failure.details = requests.length === 1 ? {
        code: "webkit_update_required",
        installedVersion: "0.4.1",
        requiredVersion: "0.8.14",
      } : {};
      throw failure;
    },
  });
  install(ctx, "saveProject");

  await ctx.saveProject({ preventDefault() {}, submitter: saveButton });
  assert.equal(requests[0].path, "/api/projects/existing");
  assert.equal(requests[0].options.body.updateWebkit, false);
  assert.equal(updatePrompt.hidden, false, "old kits should reveal the inline update action");
  assert.equal(versions.textContent, "0.4.1 to 0.8.14");

  await ctx.saveProject({ preventDefault() {}, submitter: updateButton });
  assert.equal(requests[1].options.body.updateWebkit, true);
  assert.equal(updatePrompt.hidden, true, "the update offer should clear while retrying");
}

async function testPushFailureBecomesPersistentAgentIssue() {
  const issue = {
    code: "github_target_diverged",
    message: "The GitHub target is not an ancestor of the validated local commit.",
    action: "handle_with_agent",
  };
  const project = { id: "project-1", sessions: [] };
  const rendered = [];
  const toasts = [];
  const busy = [];
  let refreshes = 0;
  const ctx = context({
    state: { projects: [project], selectedProjectId: project.id },
    $: () => ({}),
    api: async () => {
      const failure = new Error(issue.message);
      failure.details = { issue };
      throw failure;
    },
    refreshProjects: async () => { refreshes += 1; },
    renderProjectIssue: (value) => rendered.push(value.issue),
    setBusy: (_button, value) => busy.push(value),
    toast: (message) => toasts.push(message),
  });
  install(ctx, "selectedProject", "retainActionableIssue", "pushSelectedProject");

  await ctx.pushSelectedProject();
  assert.deepEqual(project.issue, issue);
  assert.equal(rendered.length, 1, "the actionable error must render immediately");
  assert.equal(refreshes, 1, "the persisted server issue must be refreshed");
  assert.equal(toasts.length, 0, "the persistent issue must replace the transient toast");
  assert.deepEqual(busy, [true, false]);
  assert.match(
    extractFunction("finishSession"),
    /retainActionableIssue\(error, project\)/,
    "automatic post-merge push failures must keep the same persistent issue",
  );
}

async function testIssueActionStartsAnUncoloredAgent() {
  const issue = {
    code: "github_target_diverged",
    message: "Sync before pushing.",
    action: "handle_with_agent",
  };
  const project = { id: "project-1", sessions: [], issue };
  const session = {
    id: "support-1",
    projectId: project.id,
    kind: "support",
    color: "agent",
    emoji: "🛠️",
  };
  const requests = [];
  const opened = [];
  const busy = [];
  const button = {};
  const ctx = context({
    state: { projects: [project], selectedProjectId: project.id },
    $: () => ({}),
    api: async (path, options) => {
      requests.push({ path, options });
      return { session };
    },
    refreshProjects: async () => { project.sessions = [session]; },
    openSession: (value) => opened.push(value),
    setBusy: (_button, value) => busy.push(value),
    toast: () => {},
  });
  install(ctx, "selectedProject", "projectForSessionId", "handleProjectIssue");

  await ctx.handleProjectIssue({ currentTarget: button });
  assert.equal(requests[0].path, "/api/projects/project-1/agent");
  assert.equal(requests[0].options.body.issueCode, issue.code);
  assert.equal(requests[0].options.body.reasoningEffort, "high");
  assert.equal(opened[0].kind, "support");
  assert.equal(opened[0].color, "agent");
  assert.deepEqual(busy, [true, false]);

  const renderSessionsSource = extractFunction("renderSessions");
  assert.match(renderSessionsSource, /session\.kind !== "support"/);
  const openSessionSource = extractFunction("openSession");
  assert.match(openSessionSource, /session\.kind === "support"/);
  assert.match(openSessionSource, /Apply fix to/);
}

async function testFastModeNoticeIsAcknowledgedOnlyOnce() {
  let opens = 0;
  let closes = 0;
  let saves = 0;
  const dialog = {
    showModal() { opens += 1; },
    close() { closes += 1; },
  };
  const error = { textContent: "" };
  const button = {};
  const controls = {
    "#fastModeNoticeDialog": dialog,
    "#fastModeNoticeError": error,
    "#fastModeNoticeOk": button,
  };
  const ctx = context({
    state: { settings: { fastModeNoticeSeen: false } },
    fastModeNoticePromise: null,
    resolveFastModeNotice: null,
    $: (selector) => controls[selector],
    api: async () => {
      saves += 1;
      return { fastModeNoticeSeen: true };
    },
    setBusy: () => {},
  });
  install(ctx, "ensureFastModeNotice", "acknowledgeFastModeNotice");

  const first = ctx.ensureFastModeNotice();
  const second = ctx.ensureFastModeNotice();
  assert.equal(first, second, "concurrent fast selections must share one notice");
  assert.equal(opens, 1);
  await ctx.acknowledgeFastModeNotice();
  await first;
  assert.equal(saves, 1);
  assert.equal(closes, 1);
  assert.equal(ctx.state.settings.fastModeNoticeSeen, true);
  await ctx.ensureFastModeNotice();
  assert.equal(opens, 1, "acknowledged notice must not reopen");
}

(async () => {
  await testHomeSelectionPersists();
  await testFinishedSessionsCloseTheirOwnedPreviewWindows();
  testMergedSessionsRenderBelowActiveSessions();
  await testApiPreservesStructuredErrorDetails();
  await testRegisteredProjectCanUpdateItsVendoredWebkit();
  await testChatAsyncWorkStaysWithItsSession();
  await testSharedModalCompletionsAreGenerationScoped();
  await testSettingsCompletionIsGenerationScoped();
  await testProjectDropsAreBoundedAndRejected();
  await testConcurrentProjectReadsCannotExceedLimits();
  await testProjectSaveWaitsForPendingReferences();
  await testExistingProjectOffersAndRequestsWebkitUpdate();
  await testPushFailureBecomesPersistentAgentIssue();
  await testIssueActionStartsAnUncoloredAgent();
  await testFastModeNoticeIsAcknowledgedOnlyOnce();
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exitCode = 1;
});
"""


class ControlCenterFrontendTests(unittest.TestCase):
    def test_agent_speed_controls_and_persistent_notice_are_wired(self):
        markup = INDEX_HTML.read_text(encoding="utf-8")
        source = APP_JS.read_text(encoding="utf-8")
        styles = STYLES_CSS.read_text(encoding="utf-8")
        self.assertIn('id="newAgentSpeed"', markup)
        self.assertIn('id="chatSpeed"', markup)
        self.assertIn('id="fastModeNoticeDialog"', markup)
        self.assertIn('id="fastModeNoticeOk"', markup)
        self.assertIn("Codex Fast uses extra ChatGPT plan credits", markup)
        self.assertIn("Claude Fast requires paid extra usage", markup)
        self.assertIn("instead of falling back to an API key", markup)
        self.assertIn('api("/api/notices/fast-mode"', source)
        self.assertIn("state.settings.fastModeNoticeSeen", source)
        self.assertIn('speedMode: $("#newAgentSpeed").value', source)
        self.assertIn("/speed`,", source)
        self.assertIn(".agent-setting-controls", styles)
        self.assertIn(".chat-settings", styles)

    def test_registered_project_update_action_is_visible_in_markup(self):
        markup = INDEX_HTML.read_text(encoding="utf-8")
        source = APP_JS.read_text(encoding="utf-8")
        self.assertIn('id="updateProjectWebkitButton"', markup)
        self.assertIn("Update Webkit to ${webkitUpdate.requiredVersion}", source)
        self.assertIn("Update Webkit first", source)

    def test_completed_agent_thinking_is_collapsible_with_duration(self):
        source = APP_JS.read_text(encoding="utf-8")
        styles = STYLES_CSS.read_text(encoding="utf-8")
        self.assertIn('event.kind === "turn_start"', source)
        self.assertIn('event.kind === "turn_complete"', source)
        self.assertIn('"Hide" : "View"} thinking', source)
        self.assertIn("formatThinkingDuration(event.meta?.durationMs)", source)
        self.assertIn("details.open = false", source)
        self.assertIn('indicator.className = "thinking-indicator"', source)
        self.assertIn('dots.className = "thinking-dots"', source)
        self.assertIn(".thinking-group", styles)
        self.assertIn(".thinking-events", styles)
        self.assertIn("@keyframes thinking-ellipsis", styles)

    def test_project_issue_has_persistent_actionable_markup(self):
        markup = INDEX_HTML.read_text(encoding="utf-8")
        styles = STYLES_CSS.read_text(encoding="utf-8")
        self.assertIn('id="projectIssue" role="alert"', markup)
        self.assertIn('id="handleProjectIssue"', markup)
        self.assertIn('class="project-issue-action"', markup)
        self.assertIn("Handle with agent", markup)
        self.assertIn(".project-issue[hidden]", styles)
        self.assertIn(".project-issue-action", styles)
        self.assertIn("border-radius: 50%", styles)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for front-end regression tests")
    def test_async_state_and_drop_regressions(self):
        with tempfile.TemporaryDirectory() as raw:
            harness = Path(raw) / "control-center-frontend-harness.js"
            harness.write_text(NODE_HARNESS, encoding="utf-8")
            result = subprocess.run(
                [shutil.which("node"), str(harness), str(APP_JS)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
