"use strict";

const $ = (id) => document.getElementById(id);

const STATUS_LABELS = {
  queued: "排队中",
  submitted: "已提交",
  running: "生成中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

let tasks = [];
let workflows = [];
let currentWorkflowId = null;

/* ---------- 表单 ---------- */

$("prompt").addEventListener("input", () => {
  $("prompt-count").textContent = $("prompt").value.length;
});

$("size-preset").addEventListener("change", () => {
  const value = $("size-preset").value;
  $("custom-size").hidden = value !== "custom";
  if (value !== "custom") {
    $("width").value = value;
    $("height").value = value;
  }
});

$("disable-lora").addEventListener("change", () => {
  const disabled = $("disable-lora").checked;
  $("lora-file").disabled = disabled;
  $("lora-strength").disabled = disabled;
});

function showError(message) {
  const box = $("form-error");
  if (message) {
    box.textContent = message;
    box.hidden = false;
  } else {
    box.hidden = true;
  }
}

function collectForm() {
  const payload = {};
  const prompt = $("prompt").value.trim();
  const layout = $("layout").value.trim();
  if (layout) {
    if (prompt) {
      showError("layout JSON 与正向提示词不能同时填写");
      return null;
    }
    try {
      payload.layout = JSON.parse(layout);
    } catch (error) {
      showError("layout JSON 解析失败：" + error.message);
      return null;
    }
  } else {
    if (!prompt) {
      showError("请填写提示词");
      return null;
    }
    payload.prompt = prompt;
  }
  const negative = $("negative").value.trim();
  if (negative) payload.negative_prompt = negative;

  const steps = parseInt($("steps").value, 10);
  if (Number.isFinite(steps)) payload.steps = steps;
  const seedValue = $("seed").value.trim();
  if (seedValue !== "") {
    const seed = parseInt(seedValue, 10);
    if (Number.isFinite(seed)) payload.seed = seed;
  }
  if ($("custom-size").hidden === false || $("size-preset").value === "custom") {
    payload.width = parseInt($("width").value, 10) || undefined;
    payload.height = parseInt($("height").value, 10) || undefined;
  } else {
    const size = parseInt($("size-preset").value, 10);
    if (Number.isFinite(size)) {
      payload.width = size;
      payload.height = size;
    }
  }

  payload.disable_lora = $("disable-lora").checked;
  if (!payload.disable_lora) {
    const loraFile = $("lora-file").value.trim();
    if (loraFile) payload.lora_filename = loraFile;
    const strength = parseFloat($("lora-strength").value);
    if (Number.isFinite(strength)) payload.lora_strength = strength;
  }
  if (currentWorkflowId) payload.workflow_id = currentWorkflowId;
  return payload;
}

$("gen-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = collectForm();
  if (!payload) return;
  const button = $("submit-btn");
  button.disabled = true;
  button.classList.add("busy");
  button.textContent = "提交中…";
  showError("");
  try {
    const response = await fetch("/api/submit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = typeof data.detail === "string" ? data.detail : "提交失败";
      showError(detail);
      return;
    }
    $("seed").value = "";
    await refreshTasks();
  } catch (error) {
    showError("无法连接服务：" + error.message);
  } finally {
    button.disabled = false;
    button.classList.remove("busy");
    button.textContent = "开始生成";
  }
});

/* ---------- 状态轮询 ---------- */

async function refreshComfyStatus() {
  try {
    const response = await fetch("/api/comfyui/status");
    if (!response.ok) throw new Error("bad status");
    const data = await response.json();
    const pill = $("comfy-status");
    pill.classList.remove("online", "offline", "unknown");
    if (data.reachable) {
      pill.classList.add("online");
      $("comfy-status-text").textContent = "ComfyUI 在线";
    } else {
      pill.classList.add("offline");
      $("comfy-status-text").textContent = "ComfyUI 离线";
    }
    const queueParts = [];
    if (data.local_queued > 0) queueParts.push(`本地排队 ${data.local_queued}`);
    if (data.local_active > 0) queueParts.push(`本地执行中 ${data.local_active}`);
    if (data.reachable && data.comfy_pending > 0) {
      queueParts.push(`ComfyUI 队列 ${data.comfy_pending}`);
    }
    $("queue-info").textContent = "队列：" + (queueParts.length ? queueParts.join(" · ") : "空闲");
  } catch {
    const pill = $("comfy-status");
    pill.classList.remove("online", "offline");
    pill.classList.add("unknown");
    $("comfy-status-text").textContent = "服务未连接";
  }
}

async function refreshTasks() {
  try {
    const response = await fetch("/api/tasks");
    if (!response.ok) throw new Error("bad status");
    const data = await response.json();
    tasks = data.items || [];
    renderTasks();
  } catch {
    /* 服务未就绪时静默 */
  }
}

function loadWorkflows() {
  fetch("/api/workflows")
    .then((response) => response.json())
    .then((data) => {
      workflows = data.items || [];
      const select = $("workflow-select");
      select.replaceChildren();
      for (const workflow of workflows) {
        const option = document.createElement("option");
        option.value = workflow.id;
        option.textContent = workflow.name;
        select.appendChild(option);
      }
      currentWorkflowId = data.default_id || (workflows[0] && workflows[0].id) || null;
      if (currentWorkflowId) select.value = currentWorkflowId;
      updateWorkflowUI();
    })
    .catch(() => {});
}

function currentWorkflow() {
  return workflows.find((item) => item.id === currentWorkflowId) || null;
}

function updateWorkflowUI() {
  const workflow = currentWorkflow();
  if (!workflow) return;
  // layout JSON 区域只对支持的工作流显示
  const layoutDetails = document.getElementById("layout-details");
  if (layoutDetails) layoutDetails.hidden = !workflow.supports_layout;
  if (!workflow.supports_layout) {
    $("layout").value = "";
  }
  // LoRA 提示与可用性
  const hasLoraNode = workflow.has_lora_node;
  if (workflow.default_lora) {
    $("lora-hint").textContent = "默认：" + workflow.default_lora;
    $("lora-file").placeholder = workflow.default_lora;
  } else if (hasLoraNode) {
    $("lora-hint").textContent = "填训练好的 LoRA 文件名；留空则本次不使用 LoRA";
    $("lora-file").placeholder = "留空不使用 LoRA";
  } else {
    $("lora-hint").textContent = "此工作流没有 LoRA 节点";
    $("lora-file").disabled = true;
    $("lora-strength").disabled = true;
  }
}

$("workflow-select").addEventListener("change", () => {
  currentWorkflowId = $("workflow-select").value;
  updateWorkflowUI();
});

/* ---------- 渲染 ---------- */

function formatTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (num) => String(num).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function taskCard(task) {
  const card = document.createElement("div");
  card.className = "task-card";
  card.dataset.jobId = task.job_id;

  const thumb = document.createElement("div");
  thumb.className = "task-thumb";
  const badge = document.createElement("span");
  badge.className = "task-badge " + task.status;
  badge.textContent = STATUS_LABELS[task.status] || task.status;
  thumb.appendChild(badge);
  if (task.thumb_url) {
    const img = document.createElement("img");
    img.src = task.thumb_url;
    img.alt = "生成结果";
    img.loading = "lazy";
    thumb.appendChild(img);
    thumb.addEventListener("click", () => openLightbox(task));
  } else {
    const placeholder = document.createElement("div");
    placeholder.className = "placeholder";
    placeholder.textContent = task.status === "failed" ? "✕" : "⏳";
    thumb.appendChild(placeholder);
  }
  if (task.queue_position != null) {
    const pos = document.createElement("span");
    pos.className = "task-pos";
    pos.textContent = `队列 #${task.queue_position}`;
    thumb.appendChild(pos);
  }
  card.appendChild(thumb);

  const body = document.createElement("div");
  body.className = "task-body";

  const promptEl = document.createElement("div");
  promptEl.className = "task-prompt";
  promptEl.textContent = task.prompt || "(layout JSON)";
  promptEl.title = task.prompt || "";
  body.appendChild(promptEl);

  const infoParts = [];
  if (task.workflow_name) infoParts.push(task.workflow_name);
  if (task.width && task.height) infoParts.push(`${task.width}×${task.height}`);
  if (task.steps) infoParts.push(`steps ${task.steps}`);
  if (task.seed != null) infoParts.push(`seed ${task.seed}`);
  if (task.lora_disabled) infoParts.push("无 LoRA");
  else if (task.lora_filename) infoParts.push("LoRA");
  const info = document.createElement("div");
  info.className = "task-info";
  info.textContent = infoParts.join(" · ");
  body.appendChild(info);

  if (task.error) {
    const errorEl = document.createElement("div");
    errorEl.className = "task-error";
    errorEl.textContent = task.error;
    errorEl.title = task.error;
    body.appendChild(errorEl);
  }

  const actions = document.createElement("div");
  actions.className = "task-actions";
  if (task.has_result) {
    const viewBtn = document.createElement("button");
    viewBtn.textContent = "查看";
    viewBtn.addEventListener("click", () => openLightbox(task));
    actions.appendChild(viewBtn);
    const downloadLink = document.createElement("a");
    downloadLink.className = "ghost-btn";
    downloadLink.textContent = "下载";
    downloadLink.href = task.result_url;
    downloadLink.setAttribute("download", "");
    downloadLink.style.flex = "1";
    downloadLink.style.textAlign = "center";
    actions.appendChild(downloadLink);
  }
  const copyBtn = document.createElement("button");
  copyBtn.textContent = "复制参数";
  copyBtn.addEventListener("click", () => copyParams(task));
  actions.appendChild(copyBtn);
  if (["queued", "submitted", "running"].includes(task.status)) {
    const cancelBtn = document.createElement("button");
    cancelBtn.textContent = "取消";
    cancelBtn.className = "danger";
    cancelBtn.addEventListener("click", () => cancelTask(task.job_id));
    actions.appendChild(cancelBtn);
  }
  const deleteBtn = document.createElement("button");
  deleteBtn.textContent = "删除";
  deleteBtn.className = "danger";
  deleteBtn.addEventListener("click", () => deleteTask(task.job_id));
  actions.appendChild(deleteBtn);
  body.appendChild(actions);

  const time = document.createElement("div");
  time.className = "task-time";
  time.textContent = formatTime(task.created_at);
  body.appendChild(time);

  card.appendChild(body);
  return card;
}

function renderTasks() {
  const list = $("task-list");
  list.replaceChildren();
  if (!tasks.length) {
    const tip = document.createElement("p");
    tip.className = "empty-tip";
    tip.textContent = "还没有任务，填写提示词开始生图吧。";
    list.appendChild(tip);
    return;
  }
  const fragment = document.createDocumentFragment();
  for (const task of tasks) fragment.appendChild(taskCard(task));
  list.appendChild(fragment);
}

/* ---------- 任务操作 ---------- */

async function postAction(url, jobId) {
  try {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ job_id: jobId }),
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      const detail = typeof data.detail === "string" ? data.detail : "操作失败";
      window.alert(detail);
      return false;
    }
    await refreshTasks();
    return true;
  } catch (error) {
    window.alert("无法连接服务：" + error.message);
    return false;
  }
}

function cancelTask(jobId) {
  if (window.confirm("确定取消这个任务吗？")) postAction("/api/cancel", jobId);
}

function deleteTask(jobId) {
  if (window.confirm("确定删除这个任务及图片吗？")) postAction("/api/delete", jobId);
}

function copyParams(task) {
  if (task.workflow_id && workflows.some((item) => item.id === task.workflow_id)) {
    $("workflow-select").value = task.workflow_id;
    currentWorkflowId = task.workflow_id;
    updateWorkflowUI();
  }
  $("prompt").value = task.prompt || "";
  $("prompt-count").textContent = $("prompt").value.length;
  $("layout").value = task.layout_json ? JSON.stringify(JSON.parse(task.layout_json), null, 2) : "";
  $("negative").value = task.negative_prompt || "";
  if (task.steps != null) $("steps").value = task.steps;
  $("seed").value = task.seed != null ? task.seed : "";
  if (task.width && task.height) {
    if (task.width === task.height && [512, 768, 1024].includes(task.width)) {
      $("size-preset").value = String(task.width);
      $("custom-size").hidden = true;
    } else {
      $("size-preset").value = "custom";
      $("custom-size").hidden = false;
    }
    $("width").value = task.width;
    $("height").value = task.height;
  }
  $("disable-lora").checked = Boolean(task.lora_disabled);
  $("disable-lora").dispatchEvent(new Event("change"));
  if (task.lora_filename) $("lora-file").value = task.lora_filename;
  if (task.lora_strength != null) $("lora-strength").value = task.lora_strength;
  $("advanced-panel").open = true;
  window.scrollTo({ top: 0, behavior: "smooth" });
}

/* ---------- 灯箱 ---------- */

function openLightbox(task) {
  $("lightbox-img").src = task.display_url || task.result_url;
  $("lightbox-download").href = task.result_url;
  const infoParts = [];
  if (task.prompt) infoParts.push("提示词：" + task.prompt);
  if (task.negative_prompt) infoParts.push("负面提示词：" + task.negative_prompt);
  if (task.layout_json) infoParts.push("layout：" + task.layout_json);
  if (task.width && task.height) infoParts.push(`分辨率：${task.width}×${task.height}`);
  if (task.steps) infoParts.push("步数：" + task.steps);
  if (task.seed != null) infoParts.push("种子：" + task.seed);
  if (task.lora_disabled) infoParts.push("LoRA：禁用");
  else if (task.lora_filename) {
    infoParts.push("LoRA：" + task.lora_filename + (task.lora_strength != null ? `（强度 ${task.lora_strength}）` : ""));
  }
  $("lightbox-info").textContent = infoParts.join("\n");
  $("lightbox").hidden = false;
}

function closeLightbox() {
  $("lightbox").hidden = true;
  $("lightbox-img").src = "";
}

$("lightbox-close").addEventListener("click", closeLightbox);
$("lightbox-backdrop").addEventListener("click", closeLightbox);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !$("lightbox").hidden) closeLightbox();
});

/* ---------- 初始化 ---------- */

$("refresh-btn").addEventListener("click", () => {
  refreshTasks();
  refreshComfyStatus();
});

loadWorkflows();
refreshTasks();
refreshComfyStatus();
setInterval(() => {
  refreshTasks();
  refreshComfyStatus();
}, 2000);
