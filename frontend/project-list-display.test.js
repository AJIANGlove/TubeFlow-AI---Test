import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  ACTIVE_CHANNEL_STORAGE_KEY,
  ALL_CHANNELS_STORAGE_VALUE,
  activeChannelStorageValue,
  channelDeletionConfirmation,
  channelDisplayName,
  formatDateTime,
  projectCoverMediaPath,
  projectFilterCategory,
  projectFlowBadge,
  projectIsPublished,
  projectListEmptyKind,
  projectMatchesStatusFilter,
  projectPublicationState,
  projectStatusCounts,
  resolveActiveChannelId,
  sortProjectsByCreated,
  visibleProjects,
} from "./project-list-display.js";

const appSource = readFileSync(new URL("../app.js", import.meta.url), "utf8").replaceAll("\r\n", "\n");

function productionFunction(name) {
  let start = appSource.indexOf(`async function ${name}(`);
  if (start < 0) start = appSource.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `missing production function ${name}`);
  const end = appSource.indexOf("\n}\n", start);
  assert.notEqual(end, -1, `unterminated production function ${name}`);
  return appSource.slice(start, end + 2);
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

test("channel display name prefers trimmed custom name", () => {
  assert.equal(channelDisplayName({ name: "  游戏实况  " }, 0), "游戏实况");
  assert.equal(channelDisplayName({ name: "Music" }, 4), "Music");
});

test("channel display name falls back to padded auto index", () => {
  assert.equal(channelDisplayName({ name: null }, 0), "频道 01");
  assert.equal(channelDisplayName({ name: "" }, 2), "频道 03");
  assert.equal(channelDisplayName({ name: "   " }, 9), "频道 10");
  assert.equal(channelDisplayName(null, 1), "频道 02");
  assert.equal(channelDisplayName(undefined, -1), "频道 00");
});

test("channel deletion confirmation prefers the trimmed name", () => {
  assert.equal(
    channelDeletionConfirmation({ name: "  测试频道  ", url: "https://example.test/a" }),
    "测试频道",
  );
  assert.equal(
    channelDeletionConfirmation({ name: "Music", url: "https://example.test/b" }),
    "Music",
  );
});

test("channel deletion confirmation falls back to the full url, never the index name", () => {
  const url = "https://www.youtube.com/@unnamed";
  assert.equal(channelDeletionConfirmation({ name: null, url }), url);
  assert.equal(channelDeletionConfirmation({ name: "", url }), url);
  assert.equal(channelDeletionConfirmation({ name: "   ", url }), url);
  assert.notEqual(channelDeletionConfirmation({ name: "", url }, 3), "频道 04");
});

test("flow badge reports unavailable without flow facts", () => {
  assert.deepEqual(projectFlowBadge({}), { text: "状态不可用", tone: "muted" });
  assert.deepEqual(projectFlowBadge({ flow: null }), { text: "状态不可用", tone: "muted" });
  assert.deepEqual(projectFlowBadge(null), { text: "状态不可用", tone: "muted" });
});

test("flow badge attention wins over any stage", () => {
  assert.deepEqual(
    projectFlowBadge({ flow: { stage: "VIDEO", attention: true } }),
    { text: "需要恢复", tone: "error" },
  );
  assert.deepEqual(
    projectFlowBadge({ flow: { stage: "DRAFT", attention: 1 } }),
    { text: "需要恢复", tone: "error" },
  );
});

test("flow badge maps each known stage", () => {
  assert.deepEqual(
    projectFlowBadge({ flow: { stage: "DRAFT" } }),
    { text: "草稿可编辑", tone: "muted" },
  );
  assert.deepEqual(
    projectFlowBadge({ flow: { stage: "NARRATION", sentences: 8, ready_sentences: 3 } }),
    { text: "配音进行中 3/8", tone: "purple" },
  );
  assert.deepEqual(
    projectFlowBadge({ flow: { stage: "NARRATION", sentences: 0, ready_sentences: 0 } }),
    { text: "配音进行中", tone: "purple" },
  );
  assert.deepEqual(
    projectFlowBadge({ flow: { stage: "DIRECTING" } }),
    { text: "导演方案进行中", tone: "purple" },
  );
  assert.deepEqual(
    projectFlowBadge({ flow: { stage: "STORYBOARD" } }),
    { text: "故事板生成中", tone: "purple" },
  );
  assert.deepEqual(
    projectFlowBadge({ flow: { stage: "VIDEO" } }),
    { text: "视频生成中", tone: "purple" },
  );
  assert.deepEqual(
    projectFlowBadge({ flow: { stage: "VIDEO_READY", compositions: 2 } }),
    { text: "视频生产完成", tone: "green" },
  );
  assert.deepEqual(
    projectFlowBadge({ flow: { stage: "VIDEO_READY", compositions: 0 } }),
    { text: "待合成成片", tone: "amber" },
  );
  assert.deepEqual(
    projectFlowBadge({ flow: { stage: "DELETING" } }),
    { text: "删除中", tone: "muted" },
  );
});

test("flow badge falls back to raw stage for unknown states", () => {
  assert.deepEqual(
    projectFlowBadge({ flow: { stage: "PAUSED" } }),
    { text: "PAUSED", tone: "muted" },
  );
  assert.deepEqual(
    projectFlowBadge({ flow: {} }),
    { text: "未知状态", tone: "muted" },
  );
});

test("formatDateTime renders local YYYY-MM-DD HH:mm", () => {
  const iso = "2026-08-29T10:05:30.000Z";
  const date = new Date(iso);
  const pad = (value) => String(value).padStart(2, "0");
  const expected = `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
  assert.equal(formatDateTime(iso), expected);
  assert.match(formatDateTime(iso), /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/);
});

test("formatDateTime returns placeholder for empty or unparsable input", () => {
  assert.equal(formatDateTime(null), "—");
  assert.equal(formatDateTime(""), "—");
  assert.equal(formatDateTime(undefined), "—");
  assert.equal(formatDateTime("not-a-date"), "—");
});


test("video stage waits for manual generation when backend reports incomplete videos", () => {
  for (const videos of [0, 1]) {
    assert.deepEqual(
      projectFlowBadge({ flow: { stage: "VIDEO", blocking_reason: "videos_incomplete", groups: 2, videos } }),
      { text: "视频待生成", tone: "purple" },
    );
  }
});

test("video stage stays in progress for an active backend attempt", () => {
  assert.deepEqual(
    projectFlowBadge({ flow: { stage: "VIDEO", blocking_reason: "video_attempt_running", videos: 0 } }),
    { text: "视频生成中", tone: "purple" },
  );
});

test("failed attempts still take priority over pending video generation", () => {
  assert.deepEqual(
    projectFlowBadge({ flow: { stage: "VIDEO", blocking_reason: "videos_incomplete", attention: true } }),
    { text: "需要恢复", tone: "error" },
  );
});

test("projectCoverMediaPath builds the media path only for a real current cover", () => {
  assert.equal(
    projectCoverMediaPath({ id: "p1", has_current_cover: true, current_cover_image_generation_id: "g9" }),
    "/projects/p1/cover/images/g9/media",
  );
  assert.equal(projectCoverMediaPath({ id: "p1", has_current_cover: false, current_cover_image_generation_id: "g9" }), null);
  assert.equal(projectCoverMediaPath({ id: "p1", has_current_cover: true, current_cover_image_generation_id: null }), null);
  assert.equal(projectCoverMediaPath({ id: "p1", has_current_cover: true }), null);
  assert.equal(projectCoverMediaPath(null), null);
});

test("projectFilterCategory folds badges into four filter buckets", () => {
  assert.equal(projectFilterCategory({ flow: { attention: true, stage: "VIDEO" } }), "attention");
  assert.equal(projectFilterCategory({ flow: { stage: "VIDEO_READY", compositions: 1 } }), "done");
  assert.equal(projectFilterCategory({ flow: { stage: "DRAFT" } }), "draft");
  assert.equal(projectFilterCategory({ flow: { stage: "NARRATION" } }), "in_progress");
  assert.equal(projectFilterCategory({ flow: { stage: "VIDEO_READY", compositions: 0 } }), "in_progress");
  assert.equal(projectFilterCategory({ flow: { stage: "DELETING" } }), "in_progress");
  assert.equal(projectFilterCategory({}), "in_progress");
});

test("projectMatchesStatusFilter folds draft into the in_progress chip", () => {
  const draft = { flow: { stage: "DRAFT" } };
  const done = { flow: { stage: "VIDEO_READY", compositions: 2 } };
  const attention = { flow: { attention: true } };
  const running = { flow: { stage: "STORYBOARD" } };
  for (const project of [draft, done, attention, running]) {
    assert.equal(projectMatchesStatusFilter(project, "all"), true);
    assert.equal(projectMatchesStatusFilter(project, ""), true);
  }
  assert.equal(projectMatchesStatusFilter(draft, "in_progress"), true);
  assert.equal(projectMatchesStatusFilter(running, "in_progress"), true);
  assert.equal(projectMatchesStatusFilter(done, "in_progress"), false);
  assert.equal(projectMatchesStatusFilter(done, "done"), true);
  assert.equal(projectMatchesStatusFilter(attention, "attention"), true);
  assert.equal(projectMatchesStatusFilter(draft, "attention"), false);
});

test("projectStatusCounts counts per chip with draft folded into in_progress", () => {
  const projects = [
    { flow: { stage: "DRAFT" } },
    { flow: { stage: "NARRATION" } },
    { flow: { stage: "VIDEO_READY", compositions: 1 } },
    { flow: { attention: true } },
  ];
  assert.deepEqual(projectStatusCounts(projects), { all: 4, in_progress: 2, done: 1, attention: 1 });
  assert.deepEqual(projectStatusCounts([]), { all: 0, in_progress: 0, done: 0, attention: 0 });
  assert.deepEqual(projectStatusCounts(null), { all: 0, in_progress: 0, done: 0, attention: 0 });
});

test("publication state prefers the marker and requires a current composition", () => {
  assert.equal(projectIsPublished({ published_at: "2026-10-20T00:00:00Z" }), true);
  assert.equal(projectIsPublished({ published_at: null }), false);
  assert.equal(projectPublicationState({ published_at: "2026-10-20T00:00:00Z" }), "published");
  assert.equal(projectPublicationState({ published_at: null, flow: { compositions: 1 } }), "publishable");
  assert.equal(projectPublicationState({ flow: { compositions: 0 } }), "unpublished");
  assert.equal(projectPublicationState({}), "unpublished");
});

test("published and unpublished chips partition projects independently of flow status", () => {
  const draft = { flow: { stage: "DRAFT" } };
  const donePublished = { published_at: "2026-10-20T00:00:00Z", flow: { stage: "VIDEO_READY", compositions: 1 } };
  const doneUnpublished = { flow: { stage: "VIDEO_READY", compositions: 1 } };
  const attentionPublished = { published_at: "2026-10-20T00:00:00Z", flow: { attention: true } };

  assert.equal(projectMatchesStatusFilter(draft, "unpublished"), true);
  assert.equal(projectMatchesStatusFilter(doneUnpublished, "unpublished"), true);
  assert.equal(projectMatchesStatusFilter(donePublished, "unpublished"), false);
  assert.equal(projectMatchesStatusFilter(attentionPublished, "unpublished"), false);
  assert.equal(projectMatchesStatusFilter(donePublished, "published"), true);
  assert.equal(projectMatchesStatusFilter(draft, "published"), false);
  // 发布维度不影响原流程维度：已发布项目仍计入「已完成」。
  assert.equal(projectMatchesStatusFilter(donePublished, "done"), true);
});

test("active channel preference resolves all, valid, stale and empty values", () => {
  const channels = [{ id: "c1" }, { id: "c2" }];
  assert.equal(ACTIVE_CHANNEL_STORAGE_KEY, "tf-active-channel");
  assert.equal(ALL_CHANNELS_STORAGE_VALUE, "all");
  assert.equal(activeChannelStorageValue(null), "all");
  assert.equal(activeChannelStorageValue("c2"), "c2");
  assert.equal(resolveActiveChannelId(channels, "all"), null);
  assert.equal(resolveActiveChannelId(channels, "c2"), "c2");
  assert.equal(resolveActiveChannelId(channels, "missing"), "c1");
  assert.equal(resolveActiveChannelId(channels, null), "c1");
  assert.equal(resolveActiveChannelId([], "c1"), null);
  assert.equal(resolveActiveChannelId(null, "all"), null);
});

test("sortProjectsByCreated orders by created_at without mutating input", () => {
  const a = { id: "a", created_at: "2026-08-01T10:00:00Z" };
  const b = { id: "b", created_at: "2026-08-03T10:00:00Z" };
  const c = { id: "c", created_at: "2026-08-02T10:00:00Z" };
  const input = [a, b, c];
  assert.deepEqual(sortProjectsByCreated(input, "newest").map((project) => project.id), ["b", "c", "a"]);
  assert.deepEqual(sortProjectsByCreated(input, "oldest").map((project) => project.id), ["a", "c", "b"]);
  assert.deepEqual(input.map((project) => project.id), ["a", "b", "c"]);
});

test("sortProjectsByCreated puts unparsable dates at the old end", () => {
  const a = { id: "a", created_at: "2026-08-01T10:00:00Z" };
  const broken = { id: "x", created_at: "not-a-date" };
  assert.deepEqual(sortProjectsByCreated([broken, a], "newest").map((project) => project.id), ["a", "x"]);
  assert.deepEqual(sortProjectsByCreated([], "newest"), []);
});

test("visibleProjects combines query, status filter and sort", () => {
  const projects = [
    { title: "Alpha 教程", created_at: "2026-08-01T10:00:00Z", flow: { stage: "DRAFT" } },
    { title: "Beta 评测", created_at: "2026-08-03T10:00:00Z", flow: { stage: "VIDEO_READY", compositions: 1 } },
    { title: "alpha 花絮", created_at: "2026-08-02T10:00:00Z", flow: { stage: "NARRATION" } },
  ];
  const titleOf = (project) => project.title;
  assert.deepEqual(
    visibleProjects(projects, { query: "ALPHA", filter: "all", sort: "oldest" }, titleOf).map((project) => project.title),
    ["Alpha 教程", "alpha 花絮"],
  );
  assert.deepEqual(
    visibleProjects(projects, { query: "", filter: "done", sort: "newest" }, titleOf).map((project) => project.title),
    ["Beta 评测"],
  );
  assert.deepEqual(visibleProjects(projects, { query: "不存在的标题" }, titleOf), []);
});

test("projectListEmptyKind distinguishes search, filter and true empty states", () => {
  assert.equal(projectListEmptyKind(3, 2, "", "all"), null);
  assert.equal(projectListEmptyKind(0, 0, "关键词", "all"), "empty");
  assert.equal(projectListEmptyKind(5, 0, "关键词", "all"), "search");
  assert.equal(projectListEmptyKind(5, 0, "  ", "done"), "filter");
  assert.equal(projectListEmptyKind(5, 0, "", "all"), "empty");
});

function createProjectsPollingHarness(projectResult) {
  const state = {
    projects: [{ id: "old" }],
    projectsPollTimer: null,
    projectsPollInFlight: false,
    projectsPollGeneration: 0,
    projectPublicationPending: new Set(),
  };
  const searchElement = {};
  const document = { hidden: false, activeElement: null };
  const window = {
    setInterval: () => 73,
    clearInterval: () => {},
  };
  let projectCallCount = 0;
  const api = {
    projects: () => {
      projectCallCount += 1;
      return projectResult;
    },
  };
  let renderCount = 0;
  const renderProjects = () => {
    renderCount += 1;
  };
  const $ = (selector) => (selector === "#projectSearch" ? searchElement : null);
  const helpers = new Function(
    "state",
    "document",
    "window",
    "api",
    "renderProjects",
    "$",
    "PROJECTS_POLL_INTERVAL_MS",
    `${productionFunction("stopProjectsPolling")}
     ${productionFunction("startProjectsPolling")}
     ${productionFunction("pollProjectsOnce")}
     return { stopProjectsPolling, startProjectsPolling, pollProjectsOnce };`,
  )(state, document, window, api, renderProjects, $, 5000);
  return {
    state,
    document,
    searchElement,
    helpers,
    renderCount: () => renderCount,
    projectCallCount: () => projectCallCount,
  };
}

test("project list polling still renders changed data in the current generation", async () => {
  const harness = createProjectsPollingHarness([{ id: "new" }]);
  harness.helpers.startProjectsPolling();
  await harness.helpers.pollProjectsOnce();
  assert.deepEqual(harness.state.projects, [{ id: "new" }]);
  assert.equal(harness.renderCount(), 1);
  assert.equal(harness.state.projectsPollInFlight, false);
});

test("project list polling discards an in-flight response after its generation stops", async () => {
  const request = deferred();
  const harness = createProjectsPollingHarness(request.promise);
  harness.helpers.startProjectsPolling();
  const pending = harness.helpers.pollProjectsOnce();
  harness.helpers.stopProjectsPolling();
  request.resolve([{ id: "new" }]);
  await pending;
  assert.equal(harness.state.projectsPollGeneration, 2);
  assert.deepEqual(harness.state.projects, [{ id: "old" }]);
  assert.equal(harness.renderCount(), 0);
  assert.equal(harness.state.projectsPollInFlight, false);
  await harness.helpers.pollProjectsOnce();
  assert.equal(harness.projectCallCount(), 1);

  harness.helpers.startProjectsPolling();
  await harness.helpers.pollProjectsOnce();
  assert.deepEqual(harness.state.projects, [{ id: "new" }]);
  assert.equal(harness.renderCount(), 1);
  assert.equal(harness.projectCallCount(), 2);
});

test("polling pauses while a publication request is pending", async () => {
  const harness = createProjectsPollingHarness([{ id: "new" }]);
  harness.state.projectPublicationPending.add("p1");
  harness.helpers.startProjectsPolling();
  await harness.helpers.pollProjectsOnce();
  assert.equal(harness.projectCallCount(), 0);
  assert.deepEqual(harness.state.projects, [{ id: "old" }]);
  harness.state.projectPublicationPending.clear();
  await harness.helpers.pollProjectsOnce();
  assert.equal(harness.projectCallCount(), 1);
  assert.deepEqual(harness.state.projects, [{ id: "new" }]);
});

test("project list polling preserves unchanged-data and search-focus behavior", async () => {
  const harness = createProjectsPollingHarness([{ id: "old" }]);
  harness.helpers.startProjectsPolling();
  await harness.helpers.pollProjectsOnce();
  assert.equal(harness.renderCount(), 0);

  const changed = createProjectsPollingHarness([{ id: "new" }]);
  changed.helpers.startProjectsPolling();
  changed.document.activeElement = changed.searchElement;
  await changed.helpers.pollProjectsOnce();
  assert.deepEqual(changed.state.projects, [{ id: "new" }]);
  assert.equal(changed.renderCount(), 0);
});

function detailPublicationHeader() {
  return new Function(
    "escapeHtml",
    "formatDateTime",
    "projectIsPublished",
    `${productionFunction("publicationHeaderMarkup")}
     return publicationHeaderMarkup;`,
  )(
    (value) => String(value),
    (value) => (value ? "2026-10-20 18:00" : "—"),
    (value) => Boolean(value?.published_at),
  );
}

function createDetailPublicationHarness({ updated, failure = null } = {}) {
  const state = {
    project: { id: "p1", title: "项目一", published_at: null },
    projects: [
      { id: "p1", published_at: null, flow: { stage: "VIDEO_READY", compositions: 1 } },
      { id: "p2", flow: { stage: "DRAFT" } },
    ],
    projectPublicationPending: new Set(),
  };
  const calls = [];
  const toasts = [];
  const busy = [];
  let renders = 0;
  const api = {
    publishProject: (id) => {
      calls.push(id);
      return failure ? Promise.reject(failure) : Promise.resolve(updated ?? { published_at: "2026-10-20T10:00:00Z" });
    },
    project: (id) => Promise.resolve({ id, published_at: "2026-10-20T10:00:00Z" }),
  };
  const publishCurrentProject = new Function(
    "state",
    "api",
    "setBusy",
    "toast",
    "renderProjectWorkspace",
    "messageOf",
    "projectIsPublished",
    `${productionFunction("publishCurrentProject")}
     return publishCurrentProject;`,
  )(
    state,
    api,
    (value) => { busy.push(value); },
    (message, isError) => { toasts.push({ message, isError }); },
    () => { renders += 1; },
    (error) => error?.message || "请求失败",
    (project) => Boolean(project?.published_at),
  );
  return { state, calls, toasts, busy, publishCurrentProject, renderCount: () => renders };
}

test("detail header shows a publish button only while unpublished and publishable", () => {
  const markup = detailPublicationHeader();

  const unpublished = markup({ id: "p1", published_at: null }, true);
  assert.match(unpublished, /id="publishProjectFromStage"/);
  assert.match(unpublished, /class="ghost-button compact"/);
  assert.match(unpublished, />标记已发布<\/button>/);
  assert.match(unpublished, /title="标记为已发布；取消入口在项目列表"/);
  assert.doesNotMatch(unpublished, /\bdisabled\b/);

  const blocked = markup({ id: "p1", published_at: null }, false);
  assert.match(blocked, /title="需要当前成片后才能标记发布"/);
  assert.match(blocked, /\bdisabled\b/);

  // 同页第二个发布入口（STEP 09 的 SRT 预览面板）用自定义 id，避免与头部重复 id。
  const srtEntry = markup({ id: "p1", published_at: null }, true, "publishProjectFromSrt");
  assert.match(srtEntry, /id="publishProjectFromSrt"/);
  assert.match(srtEntry, />标记已发布<\/button>/);
  assert.doesNotMatch(srtEntry, /id="publishProjectFromStage"/);

  const published = markup({ id: "p1", published_at: "2026-10-20T10:00:00Z" }, true);
  assert.match(published, /class="publication-state is-published"/);
  assert.match(published, /title="发布于 2026-10-20 18:00"/);
  assert.match(published, />已发布</);
  assert.doesNotMatch(published, /<button/);
  assert.doesNotMatch(published, /取消已发布/);
});

test("detail publication posts once, patches detail and list caches, then re-renders", async () => {
  const harness = createDetailPublicationHarness();
  await Promise.all([harness.publishCurrentProject(), harness.publishCurrentProject()]);

  assert.deepEqual(harness.calls, ["p1"]);
  assert.equal(harness.state.projectPublicationPending.size, 0);
  assert.equal(harness.state.project.published_at, "2026-10-20T10:00:00Z");
  assert.equal(harness.state.projects[0].published_at, "2026-10-20T10:00:00Z");
  // 列表 flow 与其它项目不受影响，返回列表后不会短暂显示「未发布」。
  assert.equal(harness.state.projects[0].flow.stage, "VIDEO_READY");
  assert.equal("published_at" in harness.state.projects[1], false);
  assert.equal(harness.renderCount(), 1);
  assert.deepEqual(harness.busy, [true, false]);
  assert.deepEqual(harness.toasts, [{ message: "已标记为已发布。", isError: undefined }]);
});

test("detail publication translates 409 and never publishes twice", async () => {
  const failure = Object.assign(new Error("A current composition is required before publishing"), {
    status: 409,
    code: "project_composition_required",
  });
  const rejected = createDetailPublicationHarness({ failure });
  await rejected.publishCurrentProject();
  assert.equal(rejected.state.project.published_at, null);
  assert.equal(rejected.renderCount(), 1);
  assert.deepEqual(rejected.toasts, [{ message: "该项目还没有当前成片，暂时不能标记发布。", isError: true }]);
  assert.equal(rejected.state.projectPublicationPending.size, 0);

  const purging = createDetailPublicationHarness({
    failure: Object.assign(new Error("Project is purging"), { status: 409, code: "project_purging" }),
  });
  await purging.publishCurrentProject();
  assert.deepEqual(purging.toasts, [{ message: "项目正在删除中，不能标记发布。", isError: true }]);

  const alreadyPublished = createDetailPublicationHarness();
  alreadyPublished.state.project.published_at = "2026-10-20T09:00:00Z";
  await alreadyPublished.publishCurrentProject();
  assert.deepEqual(alreadyPublished.calls, []);
  assert.equal(alreadyPublished.renderCount(), 0);
});

test("create project entry stops list polling before any asynchronous work", async () => {
  const state = { renderEpoch: 0, activeChannelId: "channel-1" };
  let stopCount = 0;
  let assetsCount = 0;
  const renderCreateProject = new Function(
    "state",
    "stopProjectsPolling",
    "clearObjectUrls",
    "channelById",
    "toast",
    "renderAssets",
    `${productionFunction("renderCreateProject")}
     return renderCreateProject;`,
  )(
    state,
    () => { stopCount += 1; },
    () => {},
    () => null,
    () => {},
    () => { assetsCount += 1; },
  );
  await renderCreateProject();
  assert.equal(stopCount, 1);
  assert.equal(assetsCount, 1);
  assert.equal(state.renderEpoch, 1);
});

test("create project entry asks for a channel when all-channels view has channels", async () => {
  const state = { renderEpoch: 0, activeChannelId: null, channels: [{ id: "c1" }] };
  let stopCount = 0;
  let assetsCount = 0;
  let projectsCount = 0;
  const messages = [];
  const renderCreateProject = new Function(
    "state",
    "stopProjectsPolling",
    "clearObjectUrls",
    "channelById",
    "toast",
    "renderAssets",
    "renderProjects",
    `${productionFunction("renderCreateProject")}
     return renderCreateProject;`,
  )(
    state,
    () => { stopCount += 1; },
    () => {},
    () => null,
    (message, isError) => messages.push({ message, isError }),
    () => { assetsCount += 1; },
    () => { projectsCount += 1; },
  );
  await renderCreateProject();
  assert.equal(stopCount, 1);
  assert.equal(assetsCount, 0);
  assert.equal(projectsCount, 1);
  assert.deepEqual(messages, [{ message: "请先在左侧选择频道。", isError: true }]);
});

test("project card exposes one open target plus publication controls", () => {
  const card = productionFunction("projectCard");
  assert.match(card, /<article class="project-card">/);
  assert.match(card, /data-project-open=/);
  assert.doesNotMatch(card, /<button class="project-card"/);
  assert.match(card, /projectPublicationMarkup\(project\)/);
});

test("project card shows the unpublished badge next to the mark button", () => {
  const markup = new Function(
    "escapeHtml",
    "projectTitle",
    "projectPublicationState",
    `${productionFunction("projectPublicationMarkup")}
     return projectPublicationMarkup;`,
  )(
    (value) => String(value),
    (project) => project.title ?? project.id,
    (project) => (project.published_at
      ? "published"
      : Number(project?.flow?.compositions) > 0 ? "publishable" : "unpublished"),
  );
  const unpublishedBadge = /<span class="publication-state is-unpublished"><i aria-hidden="true"><\/i>未发布<\/span>/;

  // 有当前成片：状态徽章与「标记已发布」并存，且状态在左、按钮在右。
  const publishable = markup({ id: "p1", title: "项目一", published_at: null, flow: { stage: "VIDEO_READY", compositions: 1 } });
  assert.match(publishable, unpublishedBadge);
  assert.match(publishable, /<button class="publication-mark" type="button" data-publish-project="p1"/);
  assert.ok(
    publishable.indexOf("未发布") < publishable.indexOf("标记已发布"),
    "unpublished badge must precede the mark button",
  );

  // 无当前成片：只有状态，不出现按钮。
  const blocked = markup({ id: "p2", published_at: null, flow: { stage: "DRAFT" } });
  assert.match(blocked, unpublishedBadge);
  assert.doesNotMatch(blocked, /publication-mark/);

  // 删除中：即使仍有当前成片也不出按钮，只保留状态。
  const deleting = markup({ id: "p3", published_at: null, flow: { stage: "DELETING", compositions: 1 } });
  assert.match(deleting, unpublishedBadge);
  assert.doesNotMatch(deleting, /publication-mark/);

  // 已发布：绿色徽章与取消入口不变，不出现未发布字样。
  const published = markup({ id: "p4", published_at: "2026-10-20T10:00:00Z", flow: { compositions: 1 } });
  assert.doesNotMatch(published, /is-unpublished/);
  assert.match(published, /<span class="publication-state is-published"><i aria-hidden="true"><\/i>已发布<\/span>/);
  assert.match(published, /data-unpublish-project="p4"/);
});

test("both detail stage headers place the publication control left of refresh", () => {
  const auto = productionFunction("renderAutoProgressStage");
  assert.match(auto, /const canPublish = productionComplete && Boolean\(composition\);/);
  assert.match(auto, /publicationHeaderMarkup\(state\.project, canPublish\)/);
  assert.ok(
    auto.indexOf("publicationHeaderMarkup(state.project, canPublish)") < auto.indexOf('id="refreshAutoProgress"'),
    "auto production publish control must precede refresh",
  );
  assert.match(auto, /\$\("#publishProjectFromStage"\)\?\.addEventListener\("click", \(\) => void publishCurrentProject\(\)\)/);
  assert.doesNotMatch(auto, /取消已发布/);

  const composition = productionFunction("renderCompositionStage");
  assert.match(composition, /const canPublish = Boolean\(composition\);/);
  assert.match(composition, /publicationHeaderMarkup\(state\.project, canPublish\)/);
  assert.ok(
    composition.indexOf("publicationHeaderMarkup(state.project, canPublish)") < composition.indexOf('id="refreshComposition"'),
    "composition publish control must precede refresh",
  );
  assert.match(composition, /\$\("#publishProjectFromStage"\)\?\.addEventListener\("click", \(\) => void publishCurrentProject\(\)\)/);
  assert.doesNotMatch(composition, /取消已发布/);
});

test("opening a project and rendering assets do not rewrite the list channel filter", () => {
  assert.doesNotMatch(productionFunction("openProject"), /state\.activeChannelId\s*=/);
  assert.doesNotMatch(
    productionFunction("renderAssets"),
    /state\.activeChannelId = channel\.id;/,
  );
});

test("open project failure restores polling only when the list is still visible", async () => {
  for (const listVisible of [false, true]) {
    let stopCount = 0;
    let startCount = 0;
    let toastCount = 0;
    const busyStates = [];
    const openProject = new Function(
      "api",
      "setBusy",
      "stopProjectsPolling",
      "startProjectsPolling",
      "toast",
      "messageOf",
      "$",
      "flushDescriptionTemplateSaves",
      `${productionFunction("openProject")}
       return openProject;`,
    )(
      { project: async () => { throw new Error("open failed"); } },
      (busy) => { busyStates.push(busy); },
      () => { stopCount += 1; },
      () => { startCount += 1; },
      () => { toastCount += 1; },
      (error) => error.message,
      () => (listVisible ? {} : null),
      async () => true,
    );
    await openProject("project-1");
    assert.equal(stopCount, 1, `listVisible=${listVisible}`);
    assert.equal(startCount, listVisible ? 1 : 0, `listVisible=${listVisible}`);
    assert.equal(toastCount, 1, `listVisible=${listVisible}`);
    assert.deepEqual(busyStates, [true, false], `listVisible=${listVisible}`);
  }
});
