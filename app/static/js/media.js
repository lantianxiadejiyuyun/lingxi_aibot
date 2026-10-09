/* Media UI: account-scoped JSON endpoints, escaped DOM text and event replay. */
(function () {
  "use strict";
  var root = document.getElementById("media-app");
  if (!root) return;
  var base = root.dataset.apiBase.replace(/\/*$/, "/");
  var state = { tasks: [], locations: [], selected: null, after: 0, events: new Set(),
    path: "", tab: "downloads", run: null, refreshing: false, configLoaded: false,
    taskSignature: "", detailSignature: "", runSignature: "", runListSignature: "",
    fileVersion: 0, createKey: null, agentKey: null, configOwner: "", statusLoading: false };
  var statusLabels = {
    searching: "AI 搜索中", recovering: "AI 重新搜源", selecting: "选择资源", submitting: "提交下载",
    downloading: "下载中", retry_wait: "等待重试", retrying: "重试中", waiting_infrastructure: "等待连接恢复", finalizing: "校验归档中",
    waiting_storage: "等待存储恢复", waiting_downloader: "等待下载器", verifying: "校验文件",
    paused: "已暂停", needs_input: "需要处理", failed: "失败", completed: "已完成", cancelled: "已取消",
    queued: "排队中", running: "执行中", succeeded: "已完成", interrupted: "待恢复", budget_exhausted: "预算已用完"
  };
  var eventLabels = {
    created: "任务已建立", control_requested: "已提交操作", ai_search_started: "AI 开始搜源",
    ai_search_finished: "AI 完成搜源", candidate_selected: "已选择资源", submitted: "已提交下载",
    progress: "下载进度更新", retry_scheduled: "已安排重试", retry: "重新下载", recovery_started: "开始寻找新资源",
    completed: "下载完成", failed: "下载失败", paused: "任务已暂停", resumed: "任务已恢复",
    cancelled: "任务已取消", waiting_infrastructure: "等待存储或下载器恢复", agent_event: "AI 执行动态",
    state_changed: "任务状态更新", research_queued: "已安排 AI 重新搜源", verifying: "开始校验文件", needs_input: "任务需要处理"
  };

  function el(id) { return document.getElementById(id); }
  function text(tag, value, className) {
    var node = document.createElement(tag);
    node.textContent = value == null ? "" : String(value);
    if (className) node.className = className;
    return node;
  }
  function button(label, handler, className) {
    var node = text("button", label, className || "btn btn-ghost btn-sm");
    node.type = "button";
    node.addEventListener("click", handler);
    return node;
  }
  function feedback(message, bad) {
    var box = el("media-feedback");
    box.textContent = message;
    box.className = "media-feedback" + (bad ? " error" : "");
    box.hidden = !message;
  }
  function showError(error) { feedback(error && error.message || "操作失败，请稍后重试", true); }
  async function request(path, method, data) {
    var controller = new AbortController();
    var timer = setTimeout(function () { controller.abort(); }, path === "anime/search" ? 120000 : 30000);
    var options = { method: method || "GET", credentials: "same-origin", signal: controller.signal,
      headers: window.csrfHeaders({ "Accept": "application/json" }) };
    if (data !== undefined) { options.headers["Content-Type"] = "application/json"; options.body = JSON.stringify(data); }
    try {
      var response = await fetch(base + path, options);
      var payload;
      try { payload = await response.json(); }
      catch (_) { throw new Error(response.status === 401 ? "登录已失效，请重新登录。" : "服务器响应异常，请稍后重试。"); }
      if (!response.ok || payload.ok === false) {
        var error = new Error(payload.error || "操作失败（HTTP " + response.status + "）");
        error.code = payload.code;
        throw error;
      }
      return payload.data;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("请求超时，请检查连接后重试。");
      throw error;
    } finally { clearTimeout(timer); }
  }
  async function busy(node, label, action) {
    var old = node.textContent;
    node.disabled = true; node.textContent = label;
    try { return await action(); }
    catch (error) { showError(error); }
    finally { node.disabled = false; node.textContent = old; }
  }
  function bytes(value) {
    var amount = Math.max(0, Number(value) || 0), unit = 0, units = ["B", "KB", "MB", "GB", "TB"];
    while (amount >= 1024 && unit < units.length - 1) { amount /= 1024; unit++; }
    return (unit ? amount.toFixed(amount >= 10 ? 1 : 2) : amount.toFixed(0)) + " " + units[unit];
  }
  function date(value) { var d = new Date(value); return Number.isNaN(d.getTime()) ? "" : d.toLocaleString(); }
  function requestKey() { return window.crypto && crypto.randomUUID ? crypto.randomUUID() : Date.now() + "-" + Math.random().toString(36).slice(2); }
  function terminal(value) { return ["completed", "failed", "cancelled"].indexOf(value) >= 0; }
  function badge(value) {
    var kind = ["completed", "succeeded"].indexOf(value) >= 0 ? "success" :
      ["failed", "budget_exhausted"].indexOf(value) >= 0 ? "error" :
      ["paused", "needs_input", "waiting_infrastructure", "waiting_storage", "waiting_downloader"].indexOf(value) >= 0 ? "warning" :
      value === "cancelled" ? "" : "running";
    return text("span", statusLabels[value] || value, "media-badge " + kind);
  }
  function progress(task) {
    var value = task.progress || {}, total = Number(value.total_bytes) || 0;
    var percent = task.state === "completed" ? 100 : total > 0 ? Math.min(100, 100 * (Number(value.downloaded_bytes) || 0) / total) : 0;
    var node = document.createElement("progress");
    node.className = "media-progress"; node.max = 100; node.value = percent;
    node.setAttribute("aria-label", "下载进度 " + percent.toFixed(1) + "%");
    return node;
  }
  function progressText(task) {
    var p = task.progress || {};
    if (task.state === "completed") return "已保存 " + bytes((task.result || {}).total_bytes || p.total_bytes);
    return bytes(p.downloaded_bytes) + " / " + (p.total_bytes ? bytes(p.total_bytes) : "大小待确认") +
      (p.speed ? " · " + bytes(p.speed) + "/s" : "") + " · AI 重新搜源 " + (task.recovery_count || 0) + " 次";
  }
  function requirement() {
    var out = {};
    ["title", "aliases", "season", "episode", "quality", "subtitle", "notes"].forEach(function (key) { out[key] = el("media-" + key).value.trim(); });
    return out;
  }
  function chatLink(conversationId) {
    var link = text("a", "打开关联 AI 会话", "btn btn-ghost btn-sm");
    link.href = root.dataset.chatUrl + "?conversation=" + encodeURIComponent(conversationId);
    return link;
  }

  async function loadStorage() {
    state.locations = await request("storage-locations") || [];
    ["media-storage", "media-files-storage"].forEach(function (id) {
      var select = el(id), selected = select.value;
      select.replaceChildren(text("option", state.locations.length ? "选择存储位置" : "尚未登记存储位置"));
      select.firstChild.value = "";
      state.locations.forEach(function (item) { if (!item.enabled) return; var option = text("option", item.name); option.value = item.id; select.appendChild(option); });
      if (state.locations.some(function (item) { return String(item.id) === selected; })) select.value = selected;
      else if (state.locations.length === 1 && state.locations[0].enabled) select.value = state.locations[0].id;
    });
    el("media-storage-hint").textContent = state.locations.length ? "文件将在完成校验后出现在“文件”中。" : "请先在“存储与下载器”中登记目录，或联系管理员。";
    var list = el("media-storage-list");
    if (list) {
      list.replaceChildren();
      if (!state.locations.length) list.appendChild(text("p", "尚未添加存储位置。", "media-help"));
      state.locations.forEach(function (item) {
        var row = text("div", "", "media-storage-item");
        row.appendChild(text("strong", item.name));
        row.appendChild(text("p", (item.kind === "mount" ? "NAS 挂载" : "本机硬盘") + " · 预留 " + bytes(item.min_free_bytes), "media-help"));
        if (item.root_path) row.appendChild(text("p", "灵犀：" + item.root_path, "media-help"));
        if (item.download_path) row.appendChild(text("p", "下载器：" + item.download_path, "media-help"));
        row.appendChild(button("检查连接", function (event) { busy(event.currentTarget, "检查中…", async function () {
          var result = await request("storage-locations/" + item.id + "/probe", "POST", {});
          feedback(item.name + "：" + result.message + (result.free_bytes != null ? " · 剩余 " + bytes(result.free_bytes) : ""), !result.ok);
        }); }));
        list.appendChild(row);
      });
    }
  }

  function renderTasks() {
    var filter = el("media-filter").value;
    var rows = state.tasks.filter(function (task) {
      if (filter === "active") return !terminal(task.state);
      if (filter === "completed") return task.state === "completed";
      if (filter === "failed") return ["failed", "needs_input", "waiting_infrastructure"].indexOf(task.state) >= 0;
      return true;
    });
    el("media-task-count").textContent = state.tasks.length;
    var signature = JSON.stringify([rows, state.selected, filter]);
    if (signature === state.taskSignature) return;
    state.taskSignature = signature;
    var focused = document.activeElement && document.activeElement.dataset.taskId;
    var list = el("media-task-list"); list.replaceChildren();
    if (!rows.length) list.appendChild(text("p", state.tasks.length ? "没有符合条件的任务。" : "还没有下载任务。填写左侧条件，让 AI 开始寻找资源。", "empty"));
    rows.forEach(function (task) {
      var row = button("", function () { selectTask(task.id).catch(showError); }, "media-task-row" + (state.selected === task.id ? " active" : ""));
      row.dataset.taskId = task.id; row.setAttribute("aria-pressed", String(state.selected === task.id));
      var head = text("div", "", "media-row-head");
      head.append(text("span", (task.requirement || {}).title || "下载任务 #" + task.id, "media-row-title"), badge(task.state));
      row.append(head, progress(task), text("div", progressText(task), "media-row-meta"));
      if (task.error) row.appendChild(text("div", task.error, "media-row-error"));
      list.appendChild(row);
      if (focused === String(task.id)) row.focus({ preventScroll: true });
    });
  }

  async function selectTask(id) {
    state.selected = id; state.after = 0; state.events.clear(); state.detailSignature = "";
    el("media-events").replaceChildren(); el("media-task-detail").hidden = false;
    renderTasks(); await loadDetail(id);
  }
  async function taskAction(id, action, node) {
    await busy(node, "提交中…", async function () {
      await request("downloads/" + id + "/actions", "POST", { action: action });
      feedback("操作已提交，任务状态会自动更新。");
      await refresh();
    });
  }
  function renderDetail(task) {
    var signature = JSON.stringify(task);
    if (signature === state.detailSignature) return;
    state.detailSignature = signature;
    var summary = el("media-task-summary"); summary.replaceChildren();
    var head = text("div", "", "media-section-head");
    head.append(text("h2", (task.requirement || {}).title || "下载任务 #" + task.id), badge(task.state));
    var req = task.requirement || {};
    summary.append(head, text("p", [req.season, req.episode, req.quality, req.subtitle].filter(Boolean).join(" · ") || "按番剧名称搜索", "media-help"), progress(task), text("p", progressText(task), "media-help"));
    if (task.error) summary.appendChild(text("p", task.error, "media-row-error"));
    if (task.control) summary.appendChild(text("p", "操作已提交，等待后台处理：" + ({ pause: "暂停", resume: "恢复", cancel: "取消", retry: "重试" }[task.control] || task.control), "media-help"));
    var actions = text("div", "", "media-detail-buttons");
    function addAction(label, action) {
      var node = button(label, function (event) { taskAction(task.id, action, event.currentTarget); });
      node.disabled = !!task.control; actions.appendChild(node);
    }
    if (!terminal(task.state)) {
      addAction(task.state === "paused" ? "继续下载" : "暂停", task.state === "paused" ? "resume" : "pause");
      addAction("取消下载", "cancel");
    }
    if (task.state === "failed" || task.state === "needs_input") addAction("再次尝试", "retry");
    if (task.conversation_id) actions.appendChild(chatLink(task.conversation_id));
    if (task.agent_run_id) actions.appendChild(button("查看 AI 执行过程", function () { setTab("agents"); selectRun(task.agent_run_id).catch(showError); }));
    if (task.state === "completed" && task.result && task.result.relative_path) actions.appendChild(button("打开下载目录", function () {
      el("media-files-storage").value = task.storage_id; state.path = task.result.relative_path; setTab("files");
    }));
    summary.appendChild(actions);
    var attempts = el("media-attempts"); attempts.replaceChildren();
    (task.attempts || []).forEach(function (attempt, index) {
      var row = text("div", "", "media-attempt");
      var candidate = (task.candidates || []).find(function (item) { return item.id === attempt.candidate_id; });
      row.append(text("strong", "第 " + (index + 1) + " 个资源"), text("p", candidate && candidate.title || "资源 #" + attempt.candidate_id, "media-help"), badge(attempt.state), text("p", bytes(attempt.downloaded_bytes) + " / " + bytes(attempt.total_bytes) + " · 重试 " + attempt.retry_count + " 次", "media-help"));
      if (attempt.error) row.appendChild(text("p", attempt.error, "media-row-error"));
      attempts.appendChild(row);
    });
    if (!(task.attempts || []).length) attempts.appendChild(text("p", "AI 正在寻找和核对资源。", "media-help"));
  }
  async function loadDetail(id) {
    var results = await Promise.all([request("downloads/" + id), request("downloads/" + id + "/events?after=" + state.after)]);
    if (state.selected !== id) return;
    renderDetail(results[0]);
    (results[1] || []).forEach(function (event) {
      state.after = Math.max(state.after, Number(event.seq) || 0);
      if (state.events.has(event.seq)) return;
      state.events.add(event.seq);
      var node = text("li", "", "media-event"), data = event.data || {};
      node.appendChild(text("strong", eventLabels[event.type] || statusLabels[event.type] || event.type));
      var message = data.message || data.error || data.title || (data.state ? statusLabels[data.state] || data.state : "");
      if (data.action) message = { pause: "暂停", resume: "恢复", cancel: "取消", retry: "重试" }[data.action] || data.action;
      if (message) node.appendChild(text("p", String(message).slice(0, 1000)));
      node.appendChild(text("time", date(event.created_at)));
      el("media-events").prepend(node);
      while (el("media-events").children.length > 200) el("media-events").lastElementChild.remove();
    });
  }

  async function loadFiles() {
    var storageId = el("media-files-storage").value, version = ++state.fileVersion;
    el("media-file-path").textContent = "/" + state.path;
    el("media-files-up").disabled = !state.path;
    if (!storageId) { el("media-files-list").replaceChildren(text("p", "请选择存储位置。", "empty")); return; }
    var result = await request("files?storage_id=" + encodeURIComponent(storageId) + "&path=" + encodeURIComponent(state.path));
    if (version !== state.fileVersion) return;
    var list = el("media-files-list"); list.replaceChildren();
    (result.entries || []).forEach(function (file) {
      var row = text("div", "", "media-file-row"), icon = text("i", "", file.type === "directory" ? "bi bi-folder" : "bi bi-file-earmark");
      icon.setAttribute("aria-hidden", "true"); row.appendChild(icon);
      if (file.type === "directory") row.appendChild(button(file.name, function () { state.path = file.path; loadFiles().catch(showError); }, "media-file-name"));
      else {
        row.append(text("span", file.name, "media-file-name"), text("span", bytes(file.size), "media-file-size"));
        var link = text("a", "下载", "btn btn-ghost btn-sm");
        link.href = base + "files/download?storage_id=" + encodeURIComponent(storageId) + "&path=" + encodeURIComponent(file.path);
        link.setAttribute("download", ""); row.appendChild(link);
      }
      list.appendChild(row);
    });
    if (!(result.entries || []).length) list.appendChild(text("p", "此目录暂无文件。", "empty"));
    if (result.truncated) list.appendChild(text("p", "目录内容较多，仅显示前 200 项。", "media-help"));
  }
  async function loadConfig() {
    if (!el("media-config-form") || state.configLoaded || el("media-config-owner").value.trim()) return;
    var config = await request("media/config");
    if (el("media-config-owner").value.trim()) return;
    ["qb", "aria"].forEach(function (prefix) { el("media-" + prefix + "-url").value = ""; el("media-" + prefix + "-status").textContent = "尚未配置"; });
    (config.downloaders || []).forEach(function (item) {
      var prefix = item.kind === "qbittorrent" ? "qb" : "aria";
      el("media-" + prefix + "-url").value = item.base_url || "";
      el("media-" + prefix + "-status").textContent = item.configured ? "已保存配置。凭据留空可直接保留。" : "尚未配置";
    });
    el("media-sources-status").textContent = (config.sources || []).length ? "已保存 " + config.sources.length + " 个搜索源：" + config.sources.map(function (source) { return source.name || "搜索源"; }).join("、") : "默认使用设置中的联网搜索。可添加 RSS 订阅。";
    state.configLoaded = true;
  }
  async function loadAgents() {
    var rows = await request("agent-runs"), list = el("media-agent-list"), signature = JSON.stringify(rows);
    if (signature !== state.runListSignature) {
      state.runListSignature = signature; list.replaceChildren();
      (rows || []).forEach(function (run) {
      var node = button("", function () { selectRun(run.id || run.run_id).catch(showError); }, "media-task-row media-agent-list-item");
      var role = { coordinator: "主代理", search: "搜索子代理", parse: "解析子代理", verify: "核对子代理", recover: "恢复子代理" }[run.role] || "子代理";
      node.append(text("div", role + " #" + (run.id || run.run_id), "media-row-title"), badge(run.status), text("div", date(run.created_at), "media-row-meta"));
        list.appendChild(node);
      });
      if (!(rows || []).length) list.appendChild(text("p", "后台任务开始后，这里会显示执行记录。", "empty"));
    }
    if (state.run) await selectRun(state.run);
  }
  async function selectRun(id) {
    state.run = id;
    var run = await request("agent-runs/" + id);
    if (state.run !== id) return;
    var signature = JSON.stringify(run);
    if (signature === state.runSignature) return;
    state.runSignature = signature;
    var detail = el("media-agent-detail"); detail.replaceChildren();
    detail.append(text("h2", "AI 执行 #" + id), badge(run.status));
    var budget = run.budget || {};
    detail.appendChild(text("p", "模型调用 " + (budget.rounds || 0) + " / " + (budget.max_rounds || 0) + " · 工具调用 " + (budget.tools || 0) + " / " + (budget.max_tools || 0) + " · 子代理 " + (budget.children || 0) + " / " + (budget.max_children || 0), "media-help"));
    var actions = text("div", "", "media-detail-buttons");
    if (run.conversation_id) actions.appendChild(chatLink(run.conversation_id));
    if (["completed", "succeeded", "failed", "cancelled", "budget_exhausted"].indexOf(run.status) < 0) {
      var cancel = button(run.cancel_requested ? "正在取消…" : "取消 AI 任务", function (event) {
        busy(event.currentTarget, "提交中…", async function () {
          await request("agent-runs/" + id + "/cancel", "POST", {});
          feedback("已请求取消 AI 任务。"); await selectRun(id);
        });
      });
      cancel.disabled = !!run.cancel_requested; actions.appendChild(cancel);
    }
    detail.appendChild(actions);
    if (run.error) detail.appendChild(text("p", run.error, "media-row-error"));
    if (run.result) detail.appendChild(text("pre", run.result, "media-agent-output"));
    (run.children || []).forEach(function (child) { detail.appendChild(button("子代理 #" + child.id + " · " + (statusLabels[child.status] || child.status), function () { selectRun(child.id).catch(showError); })); });
    (run.steps || []).forEach(function (step) {
      var node = text("div", "", "media-agent-step");
      node.append(text("strong", "步骤 " + step.sequence + " · " + (step.tool || step.kind)), text("span", " · " + (statusLabels[step.status] || step.status), "media-help"));
      if (step.result) { var details = document.createElement("details"); details.append(text("summary", "查看结果"), text("pre", step.result, "media-agent-output")); node.appendChild(details); }
      detail.appendChild(node);
    });
  }

  async function loadWorkerStatus() {
    if (state.statusLoading) return;
    state.statusLoading = true;
    var node = el("media-worker-status");
    try {
      var data = await request("media/status");
      if (!data || typeof data.worker_active !== "boolean") return;
      var message = data.worker_active ? "后台服务在线" : "后台服务未连接，请启动 media-worker";
      if (node.textContent !== message) node.textContent = message;
      node.className = "media-worker-status " + (data.worker_active ? "online" : "offline");
      node.title = data.worker_last_seen ? "最近心跳：" + date(data.worker_last_seen) : "尚未收到后台服务心跳";
    } catch (_) {
      // Status is advisory: an older server or a transient status failure must
      // never prevent browsing or enqueuing download tasks.
      node.textContent = "后台服务状态暂不可用";
      node.className = "media-worker-status";
    } finally { state.statusLoading = false; }
  }
  async function refresh() {
    if (state.refreshing) return;
    state.refreshing = true;
    loadWorkerStatus();
    try {
      state.tasks = await request("downloads") || []; renderTasks();
      if (state.selected && state.tab === "downloads") await loadDetail(state.selected);
      if (state.tab === "agents") await loadAgents();
    } finally { state.refreshing = false; }
  }
  function setTab(name) {
    state.tab = name;
    root.querySelectorAll("[data-media-tab]").forEach(function (node) { var active = node.dataset.mediaTab === name; node.classList.toggle("active", active); node.setAttribute("aria-selected", String(active)); });
    root.querySelectorAll(".media-panel").forEach(function (node) { node.hidden = node.id !== "media-" + name + "-panel"; });
    if (name === "files") loadFiles().catch(showError);
    if (name === "settings") loadConfig().catch(showError);
    if (name === "agents") loadAgents().catch(showError);
  }
  root.querySelectorAll("[data-media-tab]").forEach(function (node, index, nodes) {
    node.addEventListener("click", function () { setTab(node.dataset.mediaTab); });
    node.addEventListener("keydown", function (event) {
      var next;
      if (event.key === "ArrowRight") next = (index + 1) % nodes.length;
      if (event.key === "ArrowLeft") next = (index + nodes.length - 1) % nodes.length;
      if (event.key === "Home") next = 0;
      if (event.key === "End") next = nodes.length - 1;
      if (next !== undefined) { event.preventDefault(); nodes[next].focus(); setTab(nodes[next].dataset.mediaTab); }
    });
  });
  el("media-filter").addEventListener("change", renderTasks);
  el("media-refresh").addEventListener("click", function (event) { busy(event.currentTarget, "刷新中…", async function () { await loadStorage(); await refresh(); if (state.tab === "files") await loadFiles(); feedback("已刷新。"); }); });
  el("media-create-form").addEventListener("input", function () { state.createKey = null; });
  el("media-create-form").addEventListener("submit", function (event) {
    event.preventDefault();
    busy(el("media-create"), "建立任务…", async function () {
      if (!state.createKey) state.createKey = requestKey();
      var task = await request("downloads", "POST", { requirement: requirement(), storage_id: Number(el("media-storage").value), request_key: state.createKey,
        policy: { max_retries: Number(el("media-retries").value), max_recoveries: Number(el("media-recoveries").value) } });
      state.createKey = null;
      feedback("任务已建立，后台会继续搜索和下载。");
      await refresh(); await selectTask(task.id);
    });
  });
  el("media-search").addEventListener("click", function (event) {
    if (!el("media-title").reportValidity()) return;
    busy(event.currentTarget, "搜索中…", async function () {
      var rows = await request("anime/search", "POST", { requirement: requirement() }), box = el("media-search-results");
      box.hidden = false; box.replaceChildren(text("h3", "搜索结果"));
      (rows || []).forEach(function (item) {
        var row = text("div", "", "media-candidate"); row.append(text("p", item.title || "未命名资源"), text("span", { magnet: "磁力链接", torrent: "种子文件", http: "直链资源" }[item.kind] || "资源")); box.appendChild(row);
      });
      if (!(rows || []).length) box.appendChild(text("p", "暂未找到资源。可补充别名、放宽条件或添加 RSS 搜索源。", "media-help"));
    });
  });
  el("media-files-storage").addEventListener("change", function () { state.path = ""; el("media-health").textContent = ""; loadFiles().catch(showError); });
  el("media-agent-prompt").addEventListener("input", function () { state.agentKey = null; });
  el("media-agent-form").addEventListener("submit", function (event) {
    event.preventDefault(); busy(el("media-agent-start"), "建立任务…", async function () {
      if (!state.agentKey) state.agentKey = requestKey();
      var run = await request("agent-runs", "POST", { prompt: el("media-agent-prompt").value.trim(), request_key: state.agentKey });
      state.agentKey = null; await loadAgents(); await selectRun(run.id || run.run_id);
      feedback("后台 AI 任务已建立，可在下方查看执行过程。");
    });
  });
  el("media-files-up").addEventListener("click", function () { state.path = state.path.split("/").slice(0, -1).join("/"); loadFiles().catch(showError); });
  el("media-probe").addEventListener("click", function (event) {
    var id = el("media-files-storage").value; if (!id) { feedback("请先选择存储位置。", true); return; }
    busy(event.currentTarget, "检查中…", async function () { var result = await request("storage-locations/" + id + "/probe", "POST", {}); el("media-health").textContent = result.message + (result.free_bytes != null ? " · 剩余 " + bytes(result.free_bytes) : ""); });
  });
  if (el("media-storage-form")) el("media-storage-form").addEventListener("submit", function (event) {
    event.preventDefault(); busy(event.target.querySelector('[type="submit"]'), "验证中…", async function () {
      var data = { name: el("media-storage-name").value.trim(), kind: el("media-storage-kind").value, root_path: el("media-storage-root").value.trim(), download_path: el("media-storage-download-root").value.trim() };
      var assigned = el("media-storage-owner").value.trim();
      if (assigned) data.owner_id = Number(assigned);
      var location = await request("storage-locations", "POST", data);
      el("media-storage-form").reset(); await loadStorage();
      feedback(assigned ? "存储位置已验证并分配给用户 #" + location.user_id + "，该用户登录后即可使用。" : "存储位置已验证并保存。");
    });
  });
  if (el("media-edit-sources")) el("media-edit-sources").addEventListener("change", function (event) { el("media-source-editor").hidden = !event.target.checked; });
  if (el("media-config-owner")) el("media-config-owner").addEventListener("input", function (event) {
    var owner = event.target.value.trim();
    if (owner === state.configOwner) return;
    state.configOwner = owner; state.configLoaded = false;
    ["media-qb-url", "media-qb-user", "media-qb-password", "media-aria-url", "media-aria-secret", "media-rss"].forEach(function (id) { el(id).value = ""; });
    el("media-qb-status").textContent = ""; el("media-aria-status").textContent = "";
    el("media-edit-sources").checked = false; el("media-source-editor").hidden = true;
    el("media-sources-status").textContent = owner ? "正在为其他账号配置；未修改的搜索源会保留该账号原值。" : "加载当前账号配置…";
    if (!owner) loadConfig().catch(showError);
  });
  if (el("media-config-form")) el("media-config-form").addEventListener("submit", function (event) {
    event.preventDefault(); busy(event.target.querySelector('[type="submit"]'), "保存中…", async function () {
      var data = { downloaders: [] };
      if (el("media-qb-url").value.trim()) data.downloaders.push({ kind: "qbittorrent", base_url: el("media-qb-url").value.trim(), username: el("media-qb-user").value, password: el("media-qb-password").value });
      if (el("media-aria-url").value.trim()) data.downloaders.push({ kind: "aria2", base_url: el("media-aria-url").value.trim(), secret: el("media-aria-secret").value });
      var owner = el("media-config-owner").value.trim();
      if (owner) { data.owner_id = Number(owner); if (!data.downloaders.length) delete data.downloaders; }
      if (el("media-edit-sources").checked) {
        data.sources = el("media-rss").value.split(/\r?\n/).map(function (line) { return line.trim(); }).filter(Boolean).map(function (line, index) {
          var separator = line.indexOf("|"), name = separator >= 0 ? line.slice(0, separator).trim() : "RSS " + (index + 1), url = separator >= 0 ? line.slice(separator + 1).trim() : line;
          return { kind: "rss", name: name, url: url, enabled: true };
        });
        data.sources.push({ kind: "search", name: "联网搜索", enabled: el("media-web-search").checked });
      }
      await request("media/config", "PUT", data);
      ["media-qb-user", "media-qb-password", "media-aria-secret", "media-rss"].forEach(function (id) { el(id).value = ""; });
      el("media-edit-sources").checked = false; el("media-source-editor").hidden = true;
      el("media-config-owner").value = ""; state.configOwner = "";
      state.configLoaded = false; await loadConfig();
      feedback(owner ? "已为用户 #" + owner + " 保存下载器与搜索配置。" : "下载器与搜索配置已保存。");
    });
  });
  Promise.all([loadStorage(), refresh()]).catch(showError);
  setInterval(function () { if (!document.hidden && (state.tab === "downloads" || state.tab === "agents")) refresh().catch(showError); }, 5000);
})();
