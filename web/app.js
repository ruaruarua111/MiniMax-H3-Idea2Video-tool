"use strict";

const STORAGE_KEY = "minimax-h3-prompt-studio-lmstudio-v1";
const MAX_IMAGE_BYTES = 10 * 1024 * 1024;

const fieldMap = {
  mode: "mode",
  duration: "duration",
  aspect_ratio: "aspectRatio",
  creative_brief: "creativeBrief",
  visual_style: "visualStyle",
  subjects: "subjects",
  scene_lighting: "sceneLighting",
  action_timeline: "actionTimeline",
  camera_motion: "cameraMotion",
  exact_dialogue: "exactDialogue",
  visible_text: "visibleText",
  ambient_sound: "ambientSound",
  music: "music",
  extra_constraints: "extraConstraints",
  picture1_description: "picture1Description",
  picture2_description: "picture2Description",
};

const elements = Object.fromEntries(
  [
    "providerDot", "providerStatus", "lmSessionPanel", "lmSessionSummary",
    "lmSessionDetail", "lmSessionStartBtn", "lmSessionPauseBtn",
    "endpointSettingsPanel", "endpointSettingsSummary", "endpointSettingsStatus",
    "studioPortInput", "lmstudioPortInput", "comfyuiPortInput",
    "lmstudioAutoStartInput", "saveEndpointSettingsBtn", "studioPortPill",
    "pictureSection", "picture2Card",
    "picture1File", "picture2File", "picture1Preview", "picture2Preview",
    "picture1Name", "picture2Name",
    "analyzeImagesBtn", "generateScriptBtn",
    "compileBtn", "scriptBadge", "scriptMeta", "shotEditor", "streamStatus",
    "streamOutput", "promptOutput", "copyBtn", "downloadBtn", "validationResult",
    "warningsResult", "usageResult", "toast",
    ...Object.values(fieldMap),
  ].map((id) => [id, document.getElementById(id)])
);

let scriptState = null;
let activeRequest = false;
let toastTimer = null;
const imageState = {
  picture1: { file: null, asset: null, objectUrl: "" },
  picture2: { file: null, asset: null, objectUrl: "" },
};
const usageState = {};
let boundLongSegmentId = "";
let selectedLongSegmentId = "";
let loadingBoundWorkspace = false;
let longWorkspaceSaveTimer = null;
let longWorkspaceSaveChain = Promise.resolve(true);
let lastCompileValidation = { valid: false, errors: [], warnings: [] };
const longEphemeralImages = new Map();
let standaloneSingleSnapshot = null;
let lmStudioState = null;
let lmSessionChanging = false;
let thinkingStreamStarted = false;
let finalStreamStarted = false;
let appConfigState = {
  ports: {
    configured: { studio: 0, lmstudio: 0, comfyui: 0 },
    active: { studio: Number(window.location.port || 0), lmstudio: 0, comfyui: 0 },
  },
  lmstudio_auto_start: { configured: true, active: true },
  restart_required: false,
};

function activePort(name) {
  return Number(appConfigState?.ports?.active?.[name] || appConfigState?.ports?.configured?.[name] || 0);
}

function activeComfyLabel() {
  const port = activePort("comfyui");
  return port ? `ComfyUI 127.0.0.1:${port}` : "ComfyUI";
}

function renderAppConfig(config, updateInputs = true) {
  if (!config?.ports?.configured || !config?.ports?.active) return;
  appConfigState = config;
  const configured = config.ports.configured;
  const active = config.ports.active;
  if (updateInputs) {
    elements.studioPortInput.value = String(configured.studio);
    elements.lmstudioPortInput.value = String(configured.lmstudio);
    elements.comfyuiPortInput.value = String(configured.comfyui);
    elements.lmstudioAutoStartInput.checked = Boolean(config.lmstudio_auto_start?.configured);
  }
  elements.studioPortPill.textContent = `本机 ${active.studio}`;
  const configuredAutoStart = Boolean(config.lmstudio_auto_start?.configured);
  const activeAutoStart = Boolean(config.lmstudio_auto_start?.active);
  elements.endpointSettingsSummary.textContent =
    `Studio ${configured.studio} · LM Studio ${configured.lmstudio}（${configuredAutoStart ? "自动启动" : "手动启动"}） · ComfyUI ${configured.comfyui}`;
  elements.endpointSettingsPanel.classList.toggle("restart-required", Boolean(config.restart_required));
  elements.saveEndpointSettingsBtn.disabled = false;
  elements.endpointSettingsStatus.textContent = config.restart_required
    ? `设置已保存；当前仍使用 Studio ${active.studio}、LM Studio ${active.lmstudio}（${activeAutoStart ? "自动" : "手动"}）、ComfyUI ${active.comfyui}。请停止本服务并重新双击 start.bat。`
    : "只连接本机端口；不读取 ComfyUI 安装目录。设置保存在 config.json，不写入 localStorage。";
  if (longElements?.checkComfyBtn) {
    longElements.checkComfyBtn.textContent = `检查 ComfyUI :${active.comfyui}`;
  }
}

async function loadAppConfig() {
  try {
    const config = await jsonRequest("/api/config");
    renderAppConfig(config);
    return config;
  } catch (error) {
    elements.endpointSettingsStatus.textContent = error.message || "端口配置读取失败。";
    elements.endpointSettingsPanel.classList.add("restart-required");
    return null;
  }
}

async function saveEndpointSettings() {
  const ports = {
    studio_port: Number(elements.studioPortInput.value),
    lmstudio_port: Number(elements.lmstudioPortInput.value),
    comfyui_port: Number(elements.comfyuiPortInput.value),
    lmstudio_auto_start: elements.lmstudioAutoStartInput.checked,
  };
  elements.saveEndpointSettingsBtn.disabled = true;
  try {
    const result = await postJson("/api/settings/ports", ports);
    renderAppConfig(result.config);
    showToast(result.message || "端口设置已保存。", false);
  } catch (error) {
    showToast(error.message || "端口设置保存失败。", true);
  } finally {
    elements.saveEndpointSettingsBtn.disabled = false;
  }
}

function showToast(message, isError = false) {
  elements.toast.textContent = message;
  elements.toast.classList.toggle("error", isError);
  elements.toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => elements.toast.classList.remove("show"), 3200);
}

function currentMode() {
  return elements.mode.value;
}

function collectForm() {
  const form = {};
  for (const [name, id] of Object.entries(fieldMap)) {
    form[name] = elements[id].value;
  }
  form.duration = Number.parseFloat(form.duration || "7");
  return form;
}

function persistState() {
  if (!loadingBoundWorkspace && !activeRequest && lastCompileValidation.valid) {
    lastCompileValidation = {
      valid: false,
      errors: ["表单或分镜已修改，请重新编译 H3 提示词。"],
      warnings: [],
    };
  }
  if (boundLongSegmentId) {
    if (!loadingBoundWorkspace) scheduleBoundWorkspaceSave();
    return;
  }
  const safeForm = {};
  document.querySelectorAll("[data-persist]").forEach((input) => {
    safeForm[input.id] = input.value;
  });
  const payload = { form: safeForm, script: scriptState };
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(payload));
  } catch (_error) {
    showToast("浏览器无法保存表单草稿。", true);
  }
}

function restoreState() {
  let stored = null;
  try {
    stored = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
  } catch (_error) {
    localStorage.removeItem(STORAGE_KEY);
  }
  if (!stored || typeof stored !== "object") return;
  if (stored.form && typeof stored.form === "object") {
    for (const [id, value] of Object.entries(stored.form)) {
      const input = document.getElementById(id);
      if (input && input.hasAttribute("data-persist")) input.value = String(value ?? "");
    }
  }
  if (stored.script && typeof stored.script === "object" && Array.isArray(stored.script.shots)) {
    scriptState = stored.script;
  }
}

function updateModeUI() {
  const mode = currentMode();
  elements.pictureSection.hidden = mode === "T2VA";
  elements.picture2Card.hidden = mode !== "FL2VA";
  elements.analyzeImagesBtn.querySelector("span").textContent =
    mode === "FL2VA" ? "分析两张参考图" : "分析 Picture 1";
  persistState();
}

function setBusy(busy, trigger = null) {
  activeRequest = busy;
  for (const button of [elements.generateScriptBtn, elements.analyzeImagesBtn, elements.compileBtn]) {
    button.disabled = busy || (button === elements.compileBtn && !scriptState);
    button.classList.toggle("busy", busy && button === trigger);
  }
  elements.streamStatus.classList.toggle("active", busy);
  if (!busy) elements.streamStatus.classList.remove("error");
}

function resetStream(label) {
  elements.streamOutput.textContent = "";
  thinkingStreamStarted = false;
  finalStreamStarted = false;
  elements.streamStatus.textContent = label;
  elements.streamStatus.classList.remove("error");
}

function appendStream(text) {
  if (thinkingStreamStarted && !finalStreamStarted) {
    elements.streamOutput.textContent += "\n\n【最终 JSON】\n";
    finalStreamStarted = true;
  }
  elements.streamOutput.textContent += text;
  elements.streamOutput.scrollTop = elements.streamOutput.scrollHeight;
}

function appendThinking(text) {
  if (!text) return;
  if (!thinkingStreamStarted) {
    elements.streamOutput.textContent += "【思考过程】\n";
    thinkingStreamStarted = true;
  }
  elements.streamOutput.textContent += text;
  elements.streamOutput.scrollTop = elements.streamOutput.scrollHeight;
}

function parseSSEBlock(block) {
  let event = "message";
  const dataLines = [];
  for (const line of block.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
  }
  if (!dataLines.length) return null;
  let data;
  try {
    data = JSON.parse(dataLines.join("\n"));
  } catch (_error) {
    throw new Error("本地流包含无法解析的数据。");
  }
  return { event, data };
}

async function streamRequest(path, payload, handlers) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    let message = `本地请求失败（HTTP ${response.status}）`;
    try {
      const body = await response.json();
      message = body?.error?.message || message;
    } catch (_error) {
      // Keep the generic message.
    }
    throw new Error(message);
  }
  if (!response.body) throw new Error("浏览器不支持流式响应。");

  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  let finished = false;
  try {
    while (!finished) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      buffer = buffer.replace(/\r\n/g, "\n");
      let boundary = buffer.indexOf("\n\n");
      while (boundary >= 0) {
        const block = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        boundary = buffer.indexOf("\n\n");
        if (!block.trim()) continue;
        const packet = parseSSEBlock(block);
        if (!packet) continue;
        if (packet.event === "error") throw new Error(packet.data.message || "模型请求失败。");
        if (packet.event === "done") {
          finished = true;
          break;
        }
        if (packet.event === "thinking") {
          if (handlers.thinking) handlers.thinking(packet.data);
          else if (handlers.status) handlers.status(packet.data);
          continue;
        }
        if (handlers[packet.event]) handlers[packet.event](packet.data);
      }
      if (done) break;
    }
  } finally {
    if (finished) await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}

function renderList(container, items, stateClass, emptyText) {
  container.className = `result-body ${stateClass}`;
  container.replaceChildren();
  if (!items.length) {
    container.textContent = emptyText;
    return;
  }
  const list = document.createElement("ul");
  for (const item of items) {
    const li = document.createElement("li");
    li.textContent = item;
    list.appendChild(li);
  }
  container.appendChild(list);
}

function flattenNumericValues(value, prefix = "") {
  const result = [];
  if (!value || typeof value !== "object") return result;
  for (const [key, nested] of Object.entries(value)) {
    const name = prefix ? `${prefix}.${key}` : key;
    if (typeof nested === "number") result.push([name, nested]);
    else if (nested && typeof nested === "object") result.push(...flattenNumericValues(nested, name));
  }
  return result;
}

function renderUsage() {
  elements.usageResult.replaceChildren();
  const stages = Object.entries(usageState);
  if (!stages.length) {
    elements.usageResult.className = "result-body neutral";
    elements.usageResult.textContent = "尚无调用记录。";
    return;
  }
  elements.usageResult.className = "result-body";
  const names = { script: "剧本", vision: "看图", compile: "编译" };
  for (const [stage, usage] of stages) {
    const title = document.createElement("strong");
    title.textContent = names[stage] || stage;
    elements.usageResult.appendChild(title);
    const table = document.createElement("div");
    table.className = "usage-table";
    const values = flattenNumericValues(usage);
    if (!values.length) values.push(["usage", "接口未返回"]);
    for (const [key, value] of values) {
      const label = document.createElement("span");
      label.textContent = key;
      const amount = document.createElement("span");
      amount.textContent = String(value);
      table.append(label, amount);
    }
    elements.usageResult.appendChild(table);
  }
}

function dialogueToLines(dialogue) {
  if (!Array.isArray(dialogue)) return "";
  return dialogue.map((item) => `[${item.language || "Chinese"}] ${item.text || ""}`).join("\n");
}

function detectLanguage(text) {
  if (/[\u3040-\u30ff]/u.test(text)) return "Japanese";
  if (/[\uac00-\ud7af]/u.test(text)) return "Korean";
  if (/[\u3400-\u9fff]/u.test(text)) return "Chinese";
  return "English";
}

function linesToDialogue(value) {
  return value.replace(/\r\n/g, "\n").split("\n").filter((line) => line.trim()).map((line) => {
    const match = line.match(/^\[([A-Za-z][A-Za-z -]*)\]\s(.*)$/s);
    return match
      ? { language: match[1], text: match[2] }
      : { language: detectLanguage(line), text: line };
  });
}

function makeTextField(labelText, value, onInput, rows = 2, wide = false) {
  const wrapper = document.createElement("div");
  if (wide) wrapper.className = "wide";
  const label = document.createElement("label");
  label.textContent = labelText;
  const input = document.createElement("textarea");
  input.rows = rows;
  input.value = value || "";
  input.addEventListener("input", () => onInput(input.value));
  wrapper.append(label, input);
  return wrapper;
}

function renderScriptEditor() {
  if (!scriptState || !Array.isArray(scriptState.shots)) {
    elements.scriptMeta.hidden = true;
    elements.shotEditor.className = "shot-editor empty-state";
    elements.shotEditor.innerHTML = '<div class="empty-icon">⌁</div><p>填写左侧创意后生成剧本。生成结果会在这里变成可编辑镜头卡片。</p>';
    elements.scriptBadge.textContent = "尚未生成";
    elements.scriptBadge.className = "pill muted";
    elements.compileBtn.disabled = true;
    return;
  }

  elements.scriptBadge.textContent = `${scriptState.shots.length} 个镜头 · ${scriptState.duration}s`;
  elements.scriptBadge.className = "pill accent";
  elements.scriptMeta.hidden = false;
  elements.scriptMeta.replaceChildren();

  const metaFields = [
    ["标题", "title"],
    ["一句话概述", "logline"],
  ];
  for (const [labelText, key] of metaFields) {
    const wrapper = document.createElement("div");
    const label = document.createElement("label");
    label.textContent = labelText;
    const input = document.createElement("input");
    input.value = scriptState[key] || "";
    input.addEventListener("input", () => {
      scriptState[key] = input.value;
      persistState();
    });
    wrapper.append(label, input);
    elements.scriptMeta.appendChild(wrapper);
  }

  elements.shotEditor.className = "shot-editor";
  elements.shotEditor.replaceChildren();
  scriptState.shots.forEach((shot, index) => {
    const card = document.createElement("article");
    card.className = "shot-card";
    const header = document.createElement("div");
    header.className = "shot-card-header";
    const title = document.createElement("strong");
    title.textContent = `SHOT ${shot.shot}`;
    const times = document.createElement("div");
    times.className = "time-fields";
    for (const key of ["start", "end"]) {
      const input = document.createElement("input");
      input.type = "number";
      input.step = "0.001";
      input.min = "0";
      input.max = String(elements.duration.value || 15);
      input.value = String(shot[key]);
      input.title = key === "start" ? "开始时间" : "结束时间";
      input.addEventListener("input", () => {
        shot[key] = Number.parseFloat(input.value || "0");
        persistState();
      });
      times.appendChild(input);
    }
    header.append(title, times);
    card.appendChild(header);

    const grid = document.createElement("div");
    grid.className = "shot-grid";
    grid.append(
      makeTextField("画面 / 构图", shot.visual, (value) => { shot.visual = value; persistState(); }, 3),
      makeTextField("动作 / 变化", shot.action, (value) => { shot.action = value; persistState(); }, 3),
      makeTextField("镜头运动", shot.camera, (value) => { shot.camera = value; persistState(); }),
      makeTextField("环境音 / 动作音", shot.sound, (value) => { shot.sound = value; persistState(); }),
      makeTextField("对白（每行 [Language] 原文）", dialogueToLines(shot.dialogue), (value) => {
        shot.dialogue = linesToDialogue(value); persistState();
      }, 3, true),
      makeTextField("画面可见文字（每行一条）", (shot.visible_text || []).join("\n"), (value) => {
        shot.visible_text = value.replace(/\r\n/g, "\n").split("\n").filter((line) => line.trim()); persistState();
      }, 2, true),
      makeTextField("非画内配乐", shot.music, (value) => { shot.music = value; persistState(); }, 2, true),
    );
    card.appendChild(grid);
    elements.shotEditor.appendChild(card);
  });
  elements.compileBtn.disabled = activeRequest;
}

async function runScriptGeneration() {
  if (activeRequest) return false;
  setBusy(true, elements.generateScriptBtn);
  resetStream("生成剧本中");
  let finalResult = null;
  let atomicallySaved = false;
  const requestMetadata = boundRequestMetadata();
  let succeeded = false;
  try {
    await ensureLMStudioSession();
    await streamRequest("/api/script/stream", {
      form: collectForm(),
      ...requestMetadata,
    }, {
      status: (data) => { elements.streamStatus.textContent = data.message || "生成中"; },
      thinking: (data) => { elements.streamStatus.textContent = data.message || "本地 Qwen 正在思考…"; appendThinking(data.text || ""); },
      delta: (data) => appendStream(data.text || ""),
      usage: (data) => { usageState.script = data.usage || {}; renderUsage(); },
      saved: (data) => { atomicallySaved = acceptAtomicSave(data); },
      result: (data) => { finalResult = data.result; },
    });
    if (!finalResult?.script) throw new Error("剧本结果为空。");
    scriptState = finalResult.script;
    elements.promptOutput.value = "";
    lastCompileValidation = {
      valid: false,
      errors: ["分镜已重新生成，请重新编译 H3 提示词。"],
      warnings: [],
    };
    elements.copyBtn.disabled = true;
    elements.downloadBtn.disabled = true;
    renderScriptEditor();
    renderList(elements.warningsResult, finalResult.warnings || [], "neutral", "暂无 warnings。")
    persistState();
    if (requestMetadata.binding && !atomicallySaved) {
      throw new Error("分镜已生成，但服务端没有返回原子保存回执；结果未标记为完成。");
    }
    elements.streamStatus.textContent = "剧本完成";
    showToast("分镜剧本已生成，可以逐项编辑。")
    succeeded = true;
  } catch (error) {
    elements.streamStatus.textContent = "失败";
    elements.streamStatus.classList.add("error");
    if (!elements.streamOutput.textContent) elements.streamOutput.textContent = "请求未产生输出。";
    showToast(error.message || "生成剧本失败。", true);
  } finally {
    setBusy(false);
    renderScriptEditor();
  }
  return succeeded;
}

function validateLocalImage(file) {
  const allowed = new Set(["image/png", "image/jpeg", "image/webp"]);
  if (!file) throw new Error("请选择参考图。")
  if (!allowed.has(file.type)) throw new Error("图片只支持 PNG、JPEG 或 WebP。")
  if (file.size <= 0 || file.size > MAX_IMAGE_BYTES) throw new Error("每张图片必须小于等于 10 MiB。")
}

function fileToDataURL(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("无法读取图片。"));
    reader.onload = () => resolve(String(reader.result));
    reader.readAsDataURL(file);
  });
}

function imageElements(slot) {
  return slot === "picture1"
    ? {
        input: elements.picture1File,
        preview: elements.picture1Preview,
        name: elements.picture1Name,
      }
    : {
        input: elements.picture2File,
        preview: elements.picture2Preview,
        name: elements.picture2Name,
      };
}

function resetImageSlot(slot) {
  const controls = imageElements(slot);
  if (imageState[slot].objectUrl) URL.revokeObjectURL(imageState[slot].objectUrl);
  imageState[slot] = { file: null, asset: null, objectUrl: "" };
  controls.preview.onerror = null;
  controls.preview.removeAttribute("src");
  controls.preview.hidden = true;
  controls.name.textContent = "未选择文件";
}

function formatImageSize(size) {
  return `${(Number(size) / 1024 / 1024).toFixed(2)} MiB`;
}

function handleFileChange(slot, input, preview, nameElement) {
  const file = input.files?.[0] || null;
  resetImageSlot(slot);
  if (!file) {
    persistState();
    return;
  }
  try {
    validateLocalImage(file);
  } catch (error) {
    input.value = "";
    showToast(error.message, true);
    return;
  }
  const objectUrl = URL.createObjectURL(file);
  imageState[slot] = { file, asset: null, objectUrl };
  preview.src = objectUrl;
  preview.hidden = false;
  nameElement.textContent = `${file.name} · ${formatImageSize(file.size)}`;
  persistState();
}

async function assetToDataURL(asset) {
  const response = await fetch(asset.url, { cache: "no-store" });
  if (!response.ok) throw new Error("无法读取已保存的项目图片。");
  const blob = await response.blob();
  return fileToDataURL(blob);
}

function mergeLines(current, additions) {
  const values = current.replace(/\r\n/g, "\n").split("\n").filter((line) => line.trim());
  for (const addition of additions) if (addition && !values.includes(addition)) values.push(addition);
  return values.join("\n");
}

async function runImageAnalysis() {
  if (activeRequest) return false;
  const mode = currentMode();
  const selections = mode === "FL2VA"
    ? [imageState.picture1, imageState.picture2]
    : [imageState.picture1];
  try {
    if (boundLongSegmentId) await persistSelectedAssets();
    for (const selection of selections) {
      if (selection.file) validateLocalImage(selection.file);
      else if (!selection.asset) throw new Error("请选择参考图。");
    }
  } catch (error) {
    showToast(error.message, true);
    return false;
  }

  setBusy(true, elements.analyzeImagesBtn);
  resetStream("分析图片中");
  let finalResult = null;
  let succeeded = false;
  try {
    await ensureLMStudioSession();
    const images = [];
    for (const selection of selections) {
      images.push({
        name: selection.file?.name || selection.asset?.original_name || "project-image",
        data_url: selection.file
          ? await fileToDataURL(selection.file)
          : await assetToDataURL(selection.asset),
      });
    }
    await streamRequest("/api/analyze-images/stream", {
      form: collectForm(),
      images,
    }, {
      status: (data) => { elements.streamStatus.textContent = data.message || "看图中"; },
      thinking: (data) => { elements.streamStatus.textContent = data.message || "本地 Qwen 正在思考…"; appendThinking(data.text || ""); },
      delta: (data) => appendStream(data.text || ""),
      usage: (data) => { usageState.vision = data.usage || {}; renderUsage(); },
      result: (data) => { finalResult = data.result; },
    });
    if (!finalResult?.pictures?.length) throw new Error("图片分析结果为空。");
    elements.picture1Description.value = finalResult.pictures[0].description || "";
    if (mode === "FL2VA") {
      const transition = finalResult.transition_observations
        ? `\n\n首尾可见差异：${finalResult.transition_observations}` : "";
      elements.picture2Description.value = `${finalResult.pictures[1].description || ""}${transition}`;
    }
    const detectedText = finalResult.pictures.flatMap((picture) => picture.visible_text || []);
    elements.visibleText.value = mergeLines(elements.visibleText.value, detectedText);
    renderList(elements.warningsResult, finalResult.warnings || [], "neutral", "图片分析无 warnings。")
    elements.promptOutput.value = "";
    lastCompileValidation = {
      valid: false,
      errors: ["图片描述已更新，请重新编译 H3 提示词。"],
      warnings: [],
    };
    elements.copyBtn.disabled = true;
    elements.downloadBtn.disabled = true;
    persistState();
    if (boundLongSegmentId && !(await saveBoundLongWorkspace(false))) {
      throw new Error("图片描述已生成，但保存到当前长视频段失败。");
    }
    elements.streamStatus.textContent = "图片分析完成";
    showToast("参考图已转换成文字描述。")
    succeeded = true;
  } catch (error) {
    elements.streamStatus.textContent = "失败";
    elements.streamStatus.classList.add("error");
    showToast(error.message || "图片分析失败。", true);
  } finally {
    setBusy(false);
  }
  return succeeded;
}

async function runCompile() {
  if (activeRequest || !scriptState) return false;
  setBusy(true, elements.compileBtn);
  resetStream("编译 H3 中");
  let finalResult = null;
  let atomicallySaved = false;
  const requestMetadata = boundRequestMetadata();
  let succeeded = false;
  try {
    await ensureLMStudioSession();
    await streamRequest("/api/compile/stream", {
      form: collectForm(),
      script: scriptState,
      ...requestMetadata,
    }, {
      status: (data) => { elements.streamStatus.textContent = data.message || "编译中"; },
      thinking: (data) => { elements.streamStatus.textContent = data.message || "本地 Qwen 正在思考…"; appendThinking(data.text || ""); },
      delta: (data) => appendStream(data.text || ""),
      usage: (data) => { usageState.compile = data.usage || {}; renderUsage(); },
      saved: (data) => { atomicallySaved = acceptAtomicSave(data); },
      result: (data) => { finalResult = data.result; },
    });
    if (!finalResult || typeof finalResult.prompt !== "string") throw new Error("H3 编译结果为空。");
    elements.promptOutput.value = finalResult.prompt;
    elements.copyBtn.disabled = !finalResult.prompt;
    elements.downloadBtn.disabled = !finalResult.prompt;
    const validation = finalResult.validation || { valid: false, errors: ["缺少验证结果。"], warnings: [] };
    lastCompileValidation = cloneJson(validation);
    if (validation.valid) {
      renderList(elements.validationResult, ["通过：模式、字段顺序、时间、图片引用、对白与可见文字均符合规则。"], "success", "验证通过。")
    } else {
      renderList(elements.validationResult, validation.errors || [], "error", "验证失败。")
    }
    renderList(
      elements.warningsResult,
      [...(finalResult.warnings || []), ...(validation.warnings || [])],
      "neutral",
      "暂无 warnings。"
    );
    elements.streamStatus.textContent = validation.valid ? "编译并验证通过" : "编译完成，但验证未通过";
    showToast(validation.valid ? "H3 提示词已通过本地校验。" : "提示词已生成，但请先查看验证错误。", !validation.valid);
    persistState();
    if (requestMetadata.binding && !atomicallySaved) {
      throw new Error("H3 提示词已生成，但服务端没有返回原子保存回执；结果未标记为完成。");
    }
    succeeded = validation.valid;
  } catch (error) {
    elements.streamStatus.textContent = "失败";
    elements.streamStatus.classList.add("error");
    showToast(error.message || "编译 H3 提示词失败。", true);
  } finally {
    setBusy(false);
    renderScriptEditor();
  }
  return succeeded;
}

async function copyText(text, message = "内容已复制。") {
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
  } catch (_error) {
    const fallback = document.createElement("textarea");
    fallback.value = text;
    fallback.setAttribute("readonly", "");
    fallback.style.position = "fixed";
    fallback.style.opacity = "0";
    document.body.appendChild(fallback);
    fallback.select();
    document.execCommand("copy");
    fallback.remove();
  }
  showToast(message);
}

function downloadText(text, filename) {
  if (!text) return;
  const blob = new Blob(["\ufeff", text], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

async function copyPrompt() {
  await copyText(elements.promptOutput.value, "提示词已复制。");
}

function downloadPrompt() {
  downloadText(
    elements.promptOutput.value,
    `MiniMax_H3_${currentMode()}_${Number(elements.duration.value).toFixed(2)}s.txt`
  );
}

function renderLMStudioStatus(status) {
  lmStudioState = status || null;
  const model = status?.model || {};
  const conflicts = model.external_conflicts || [];
  const loaded = Boolean(model.owned_loaded);
  const serverRunning = Boolean(status?.server?.running);
  const lmstudioPort = activePort("lmstudio");
  const serverAddress = status?.server?.base_url || (lmstudioPort
    ? `http://127.0.0.1:${lmstudioPort}/api/v1`
    : "LM Studio API");
  const failed = Boolean(status?.last_error || model.identifier_conflict || conflicts.length);
  elements.providerDot.className = `status-dot ${failed ? "warning" : loaded ? "ready" : "warning"}`;
  elements.lmSessionPanel.className = `lm-session-panel ${failed ? "failed" : loaded ? "ready" : "warning"}`;
  if (model.identifier_conflict) {
    elements.lmSessionSummary.textContent = "项目实例名被其他模型占用";
    elements.lmSessionDetail.textContent = `${model.identifier_conflict.model || "未知模型"}；请先在 LM Studio 释放。`;
  } else if (conflicts.length) {
    elements.lmSessionSummary.textContent = "目标模型正由外部会话占用";
    elements.lmSessionDetail.textContent = `实例：${conflicts.map((item) => item.identifier).join("、")}；请先在 Chatbox / LM Studio 释放。`;
  } else if (status?.last_error) {
    elements.lmSessionSummary.textContent = "LM Studio 状态不可用";
    elements.lmSessionDetail.textContent = status.last_error.message || "请确认 LM Studio 已安装。";
  } else if (loaded) {
    elements.lmSessionSummary.textContent = "剧本编辑模式已开启 · 模型在显存中";
    elements.lmSessionDetail.textContent = `h3-script-editor · ${serverAddress} · 活跃请求 ${status.active_requests || 0} · 无 TTL，完成后请显式释放`;
  } else {
    elements.lmSessionSummary.textContent = "本地模型未加载 · GPU 显存未被本项目占用";
    elements.lmSessionDetail.textContent = `${serverRunning ? "LM Studio API 已启动" : "LM Studio API 未启动"} · ${serverAddress} · 点击进入编辑模式时自动加载`;
  }
  elements.providerStatus.textContent = loaded
    ? `已加载 · 请求 ${status.active_requests || 0}`
    : failed ? "需要处理冲突或配置" : "待进入编辑模式";
  elements.lmSessionStartBtn.disabled = lmSessionChanging || loaded || failed;
  elements.lmSessionPauseBtn.disabled = lmSessionChanging || !loaded || Number(status?.active_requests || 0) > 0;
  if (typeof setLongControls === "function" && longElements?.startLongRenderBtn) setLongControls();
  if (typeof setContextControls === "function" && contextElements?.contextRenderBtn) setContextControls();
}

async function loadProviderStatus(showFeedback = false) {
  try {
    const status = await jsonRequest("/api/lmstudio/status");
    renderLMStudioStatus(status);
    if (showFeedback) showToast("LM Studio 状态已刷新。", false);
    return status;
  } catch (error) {
    elements.providerDot.className = "status-dot warning";
    elements.providerStatus.textContent = "LM Studio 状态检查失败";
    elements.lmSessionPanel.className = "lm-session-panel failed";
    elements.lmSessionSummary.textContent = "无法读取 LM Studio 状态";
    elements.lmSessionDetail.textContent = error.message || "本地服务状态接口不可用。";
    if (showFeedback) showToast(error.message || "LM Studio 状态检查失败。", true);
    return null;
  }
}

async function bootstrapLocalServices() {
  const config = await loadAppConfig();
  const status = await loadProviderStatus(false);
  const autoStart = Boolean(config?.lmstudio_auto_start?.active);
  if (!status || status.server?.running || !autoStart || config?.restart_required) return;
  elements.lmSessionSummary.textContent = "正在启动 LM Studio 本地 API…";
  elements.lmSessionDetail.textContent =
    `目标端口 ${activePort("lmstudio")}；此步骤只启动 API，不加载 27B 模型。`;
  try {
    const result = await postJson("/api/lmstudio/server/start", {});
    renderLMStudioStatus(result.status);
  } catch (error) {
    elements.lmSessionPanel.className = "lm-session-panel failed";
    elements.lmSessionSummary.textContent =
      error.code === "lmstudio_port_occupied" ? "LM Studio 端口已被其他程序占用" : "LM Studio API 自动启动失败";
    elements.lmSessionDetail.textContent = error.message || "请检查 LM Studio 与端口设置。";
    elements.endpointSettingsPanel.open = true;
    elements.endpointSettingsPanel.classList.add("restart-required");
    elements.endpointSettingsStatus.textContent =
      `${error.message || "LM Studio API 自动启动失败。"} 请选择可用端口，保存后重启 Idea2Video。`;
  }
}

async function ensureLMStudioSession() {
  if (lmStudioState?.model?.owned_loaded && !(lmStudioState.model.external_conflicts || []).length) {
    return lmStudioState;
  }
  lmSessionChanging = true;
  elements.lmSessionStartBtn.disabled = true;
  elements.lmSessionSummary.textContent = "正在启动 LM Studio 并加载本地模型…";
  try {
    const result = await postJson("/api/lmstudio/session/start", {});
    renderLMStudioStatus(result.status);
    showToast("本地 Qwen 已加载，剧本编辑模式已开启。", false);
    return result.status;
  } catch (error) {
    await loadProviderStatus(false);
    throw error;
  } finally {
    lmSessionChanging = false;
    if (lmStudioState) renderLMStudioStatus(lmStudioState);
  }
}

async function releaseLMStudioSession(mode = "pause") {
  if (lmSessionChanging) return false;
  lmSessionChanging = true;
  try {
    if (mode === "confirm" && boundLongSegmentId && !(await saveBoundLongWorkspace(false))) {
      throw new Error("当前单段工作区保存失败，未确认也未释放模型。")
    }
    const result = await postJson("/api/lmstudio/session/release", {
      mode,
      project_id: mode === "confirm" ? currentLongProject?.id || "" : "",
    });
    renderLMStudioStatus(result.status);
    if (result.project) renderLongProject(result.project, result.readiness);
    showToast(
      mode === "confirm"
        ? "当前剧本与全部 H3 提示词已确认，项目模型显存已释放。"
        : "项目模型显存已释放；未写入创作完成确认。",
      false
    );
    return true;
  } catch (error) {
    await loadProviderStatus(false);
    showToast(error.message || "释放本地模型失败。", true);
    return false;
  } finally {
    lmSessionChanging = false;
    if (lmStudioState) renderLMStudioStatus(lmStudioState);
  }
}

function initialize() {
  restoreState();
  updateModeUI();
  renderScriptEditor();
  renderUsage();
  bootstrapLocalServices();

  document.querySelectorAll("[data-persist]").forEach((input) => {
    input.addEventListener("input", persistState);
    input.addEventListener("change", persistState);
  });
  elements.mode.addEventListener("change", updateModeUI);
  elements.picture1File.addEventListener("change", () => handleFileChange(
    "picture1", elements.picture1File, elements.picture1Preview, elements.picture1Name
  ));
  elements.picture2File.addEventListener("change", () => handleFileChange(
    "picture2", elements.picture2File, elements.picture2Preview, elements.picture2Name
  ));
  elements.generateScriptBtn.addEventListener("click", runScriptGeneration);
  elements.analyzeImagesBtn.addEventListener("click", runImageAnalysis);
  elements.compileBtn.addEventListener("click", runCompile);
  elements.copyBtn.addEventListener("click", copyPrompt);
  elements.downloadBtn.addEventListener("click", downloadPrompt);
  elements.lmSessionStartBtn.addEventListener("click", () => ensureLMStudioSession().catch((error) => showToast(error.message, true)));
  elements.lmSessionPauseBtn.addEventListener("click", () => releaseLMStudioSession("pause"));
  elements.saveEndpointSettingsBtn.addEventListener("click", saveEndpointSettings);
  elements.promptOutput.addEventListener("input", () => {
    lastCompileValidation = {
      valid: false,
      errors: ["提示词已手动修改，请重新点击编译以执行完整校验。"],
      warnings: [],
    };
    if (elements.promptOutput.value) {
      elements.copyBtn.disabled = false;
      elements.downloadBtn.disabled = false;
    }
    persistState();
  });
  window.addEventListener("beforeunload", () => {
    for (const item of Object.values(imageState)) if (item.objectUrl) URL.revokeObjectURL(item.objectUrl);
  });
  initializeLongWorkspace();
  initializeContextWorkspace();
}

const LONG_STORAGE_KEY = "minimax-h3-prompt-studio-long-form-lmstudio-v1";
const longElements = Object.fromEntries(
  [
    "shortTabBtn", "longTabBtn", "shortWorkspace", "longWorkspace", "longIdea",
    "longTargetSeconds", "longVisualStyle", "longCharacters", "longExactDialogue",
    "longMusic", "longConstraints",
    "longAutoCompile",
    "createLongProjectBtn", "longProjectSelect", "refreshLongProjectsBtn", "checkComfyBtn",
    "generateAllLongPromptsBtn", "enterAuthoringBtn", "pauseAuthoringBtn", "confirmAuthoringBtn",
    "comfyStatus", "stopLongTaskBtn", "startLongRenderBtn", "longTaskPanel",
    "longTaskTitle", "longTaskMessage", "longProgressBar", "longLiveOutput",
    "longProjectSummary", "longProjectTitle", "longProjectMeta", "longWarnings",
    "longReadiness", "longSaveStatus",
    "longDeliverables", "longDeliverablesStatus", "longFullStoryOutput", "longPromptOutputs",
    "copyLongStoryBtn", "copyAllLongPromptsBtn", "downloadAllLongPromptsBtn",
    "longSegments", "longRevisionSelect", "restoreRevisionBtn", "regenerateDialog",
    "regenerateTitle", "regenerateChoices", "regenerateCount", "confirmRegenerateBtn",
    "longSingleStage", "longSingleHost", "longSelectedSegmentTitle",
    "longSelectedSegmentHint", "saveLongWorkspaceBtn",
  ].map((id) => [id, document.getElementById(id)])
);

let currentLongProject = null;
let currentLongTask = null;
let currentLongTaskId = "";
let longTaskPollTimer = null;
let regenerateEditedIndex = 0;
let currentLongReadiness = null;

function createElement(tag, className = "", text = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

function longTaskIsActive() {
  return Boolean(currentLongTask && ["queued", "running", "retrying"].includes(currentLongTask.state));
}

function missingLongDialogue(project) {
  const present = new Set(
    (project?.segments || []).flatMap((segment) => (segment.dialogue || []).map((item) => item.text || ""))
  );
  return (project?.exact_dialogue_required || []).filter((line) => !present.has(line));
}

function duplicatedLongDialogue(project) {
  const required = new Set(project?.exact_dialogue_required || []);
  const counts = new Map();
  for (const segment of project?.segments || []) {
    for (const item of segment.dialogue || []) {
      if (required.has(item.text)) counts.set(item.text, (counts.get(item.text) || 0) + 1);
    }
  }
  return Array.from(counts.entries()).filter(([, count]) => count > 1).map(([text]) => text);
}

async function jsonRequest(path, options = {}) {
  const response = await fetch(path, {
    cache: "no-store",
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  let body = null;
  try {
    body = await response.json();
  } catch (_error) {
    body = null;
  }
  if (!response.ok) {
    const error = new Error(body?.error?.message || `本地请求失败（HTTP ${response.status}）。`);
    error.code = body?.error?.code || "http_error";
    error.status = response.status;
    throw error;
  }
  return body || {};
}

function postJson(path, payload) {
  return jsonRequest(path, { method: "POST", body: JSON.stringify(payload) });
}

function persistLongForm() {
  const safe = {};
  document.querySelectorAll("[data-long-persist]").forEach((input) => {
    safe[input.id] = input.type === "checkbox" ? input.checked : input.value;
  });
  try {
    localStorage.setItem(LONG_STORAGE_KEY, JSON.stringify(safe));
  } catch (_error) {
    showToast("浏览器无法保存长视频表单草稿。", true);
  }
}

function restoreLongForm() {
  let safe = null;
  try {
    safe = JSON.parse(localStorage.getItem(LONG_STORAGE_KEY) || "null");
  } catch (_error) {
    localStorage.removeItem(LONG_STORAGE_KEY);
  }
  if (!safe || typeof safe !== "object") return;
  for (const [id, value] of Object.entries(safe)) {
    const input = document.getElementById(id);
    if (!input?.hasAttribute("data-long-persist")) continue;
    if (input.type === "checkbox") input.checked = Boolean(value);
    else input.value = String(value ?? "");
  }
}

async function activateWorkspace(name) {
  const useLong = name === "long";
  const useContext = name === "context";
  if (!useLong && boundLongSegmentId) {
    if (activeRequest) {
      showToast("当前 AI 操作尚未结束，暂不能离开已绑定的单段工作区。", true);
      return;
    }
    if (!(await saveBoundLongWorkspace(false))) {
      showToast("当前单段工作区保存失败，已取消切换。", true);
      return;
    }
  }
  if (useLong) {
    longElements.longWorkspace.hidden = false;
    contextElements.contextWorkspace.hidden = true;
    if (selectedLongSegmentId) selectLongSegment(selectedLongSegmentId, false);
    else longElements.shortWorkspace.hidden = true;
  } else if (useContext) {
    unmountSingleWorkspaceFromLong();
    longElements.shortWorkspace.hidden = true;
    longElements.longWorkspace.hidden = true;
    contextElements.contextWorkspace.hidden = false;
    loadContextProjects(false);
  } else {
    unmountSingleWorkspaceFromLong();
    longElements.shortWorkspace.hidden = false;
    longElements.longWorkspace.hidden = true;
    contextElements.contextWorkspace.hidden = true;
  }
  longElements.shortTabBtn.classList.toggle("active", !useLong && !useContext);
  longElements.longTabBtn.classList.toggle("active", useLong);
  contextElements.contextTabBtn.classList.toggle("active", useContext);
  longElements.shortTabBtn.setAttribute("aria-selected", String(!useLong && !useContext));
  longElements.longTabBtn.setAttribute("aria-selected", String(useLong));
  contextElements.contextTabBtn.setAttribute("aria-selected", String(useContext));
  if (useLong) loadLongProjects(false);
}

function collectLongProjectRequest() {
  const targetText = longElements.longTargetSeconds.value.trim();
  return {
    idea: longElements.longIdea.value.trim(),
    target_seconds: targetText ? Number.parseFloat(targetText) : null,
    visual_style: longElements.longVisualStyle.value.trim(),
    characters: longElements.longCharacters.value.trim(),
    exact_dialogue: longElements.longExactDialogue.value.trim(),
    music: longElements.longMusic.value.trim(),
    constraints: longElements.longConstraints.value.trim(),
    initial_frame: null,
    identity_references: [],
  };
}

function cloneJson(value) {
  return value == null ? value : JSON.parse(JSON.stringify(value));
}

function currentBoundSegment() {
  return currentLongProject?.segments?.find((item) => item.id === boundLongSegmentId) || null;
}

function boundRequestMetadata() {
  const segment = currentBoundSegment();
  if (!segment || !currentLongProject) return {};
  return {
    binding: {
      project_id: currentLongProject.id,
      segment_id: segment.id,
      workspace_revision: Number(segment.single_workspace?.revision || 0),
    },
    workspace_pictures: snapshotSingleWorkspace().pictures,
  };
}

function setLongSaveStatus(state, message) {
  longElements.longSaveStatus.className = `long-save-status ${state}`;
  longElements.longSaveStatus.textContent = message;
}

function acceptAtomicSave(data) {
  if (!data?.project || !data?.receipt) return false;
  clearTimeout(longWorkspaceSaveTimer);
  currentLongProject = data.project;
  currentLongReadiness = data.receipt.readiness || null;
  const revision = Number(data.receipt.workspace_revision || 0);
  const valid = data.receipt.states?.workspace === "valid";
  setLongSaveStatus(
    valid ? "saved" : "neutral",
    `${valid ? "已原子保存" : "已落盘，等待完成校验"} · 工作区 v${revision}`
  );
  renderLongDeliverables(currentLongProject);
  renderLongReadiness(currentLongReadiness);
  renderLongSegments(currentLongProject);
  setLongControls();
  return true;
}

function rememberBoundEphemeralImages() {
  if (!boundLongSegmentId) return;
  longEphemeralImages.set(boundLongSegmentId, {
    picture1: imageState.picture1.asset ? null : imageState.picture1.file || null,
    picture2: imageState.picture2.asset ? null : imageState.picture2.file || null,
  });
}

function snapshotSingleWorkspace() {
  const segment = currentBoundSegment();
  const stored = segment?.single_workspace || {};
  const pictures = cloneJson(stored.pictures || {
    picture1: { source: "none", input_path: "", temporary_name: "" },
    picture2: { source: "none", input_path: "", temporary_name: "" },
  });
  for (const slot of ["picture1", "picture2"]) {
    const selected = imageState[slot];
    if (selected.asset) {
      pictures[slot] = cloneJson(selected.asset);
    } else if (selected.file) {
      pictures[slot] = { source: "temporary", input_path: "", temporary_name: selected.file.name };
    } else if (!(slot === "picture1" && segment?.index > 1 && segment.boundary_before === "continuous")) {
      pictures[slot] = { source: "none", input_path: "", temporary_name: "" };
    }
  }
  if (segment?.index > 1 && segment.boundary_before === "continuous") {
    pictures.picture1 = { source: "auto_tail", input_path: "", temporary_name: "" };
  }
  return {
    revision: Number(stored.revision || 0),
    state: elements.promptOutput.value && lastCompileValidation.valid
      ? "valid" : (scriptState ? "draft" : "empty"),
    form: collectForm(),
    pictures,
    script: cloneJson(scriptState),
    prompt: elements.promptOutput.value,
    validation: cloneJson(lastCompileValidation),
    warnings: cloneJson(stored.warnings || []),
    usage: cloneJson(usageState),
  };
}

function restoreEphemeralImage(slot, file) {
  if (!file) return;
  const controls = imageElements(slot);
  resetImageSlot(slot);
  const objectUrl = URL.createObjectURL(file);
  imageState[slot] = { file, asset: null, objectUrl };
  controls.preview.src = objectUrl;
  controls.preview.hidden = false;
  controls.name.textContent = `${file.name} · ${formatImageSize(file.size)} · 临时`;
}

function applySingleWorkspace(workspace, segment = null) {
  loadingBoundWorkspace = true;
  try {
    const form = workspace?.form || {};
    for (const [name, id] of Object.entries(fieldMap)) {
      if (Object.prototype.hasOwnProperty.call(form, name)) {
        elements[id].value = String(form[name] ?? "");
      }
    }
    if (segment) {
      elements.duration.value = String(Number(segment.frames) / 24);
      elements.aspectRatio.value = "9:16";
      if (segment.index > 1 && segment.boundary_before === "continuous") {
        if (!new Set(["I2VA", "FL2VA"]).has(elements.mode.value)) elements.mode.value = "I2VA";
        if (!elements.picture1Description.value) {
          elements.picture1Description.value = segment.story_card?.opening_state || "";
        }
      }
    }
    scriptState = cloneJson(workspace?.script || null);
    for (const key of Object.keys(usageState)) delete usageState[key];
    Object.assign(usageState, cloneJson(workspace?.usage || {}));
    elements.promptOutput.value = String(workspace?.prompt || "");
    lastCompileValidation = cloneJson(
      workspace?.validation || { valid: false, errors: [], warnings: [] }
    );
    resetImageSlot("picture1");
    resetImageSlot("picture2");
    const pictures = workspace?.pictures || {};
    for (const slot of ["picture1", "picture2"]) {
      const picture = pictures[slot] || {};
      if (picture.source === "project_asset" && picture.url) {
        const controls = imageElements(slot);
        imageState[slot] = { file: null, asset: cloneJson(picture), objectUrl: "" };
        controls.preview.src = picture.url;
        controls.preview.hidden = false;
        controls.name.textContent = `${picture.original_name || picture.filename || "项目图片"} · 已保存到项目`;
      }
    }
    const ephemeral = segment ? longEphemeralImages.get(segment.id) : null;
    if (ephemeral) {
      if (
        pictures.picture1?.source !== "project_asset"
        && !(segment.index > 1 && segment.boundary_before === "continuous")
      ) {
        restoreEphemeralImage("picture1", ephemeral.picture1);
      }
      if (pictures.picture2?.source !== "project_asset") {
        restoreEphemeralImage("picture2", ephemeral.picture2);
      }
    }
    updateModeUI();
    renderScriptEditor();
    renderUsage();
    elements.copyBtn.disabled = !elements.promptOutput.value;
    elements.downloadBtn.disabled = !elements.promptOutput.value;
    if (lastCompileValidation.valid) {
      renderList(elements.validationResult, ["已保存的 H3 提示词通过本地校验。"], "success", "验证通过。");
    } else {
      renderList(elements.validationResult, lastCompileValidation.errors || [], "neutral", "尚未编译。");
    }
  } finally {
    loadingBoundWorkspace = false;
  }
}

function mountSingleWorkspaceForLong() {
  longElements.longSingleStage.hidden = false;
  if (longElements.shortWorkspace.parentElement !== longElements.longSingleHost) {
    longElements.longSingleHost.appendChild(longElements.shortWorkspace);
  }
  longElements.shortWorkspace.hidden = false;
  longElements.shortWorkspace.classList.add("embedded-long-single");
}

function unmountSingleWorkspaceFromLong() {
  rememberBoundEphemeralImages();
  longElements.shortWorkspace.classList.remove("embedded-long-single");
  if (longElements.shortWorkspace.parentElement !== document.body) {
    document.body.insertBefore(longElements.shortWorkspace, longElements.longWorkspace);
  }
  boundLongSegmentId = "";
  setLongSaveStatus("neutral", "尚未绑定段落");
  if (standaloneSingleSnapshot) applySingleWorkspace(standaloneSingleSnapshot, null);
}

function scheduleBoundWorkspaceSave() {
  clearTimeout(longWorkspaceSaveTimer);
  longWorkspaceSaveTimer = setTimeout(() => saveBoundLongWorkspace(false), 700);
}

async function persistSelectedAssets() {
  if (!currentLongProject || !boundLongSegmentId) return;
  for (const slot of ["picture1", "picture2"]) {
    const selected = imageState[slot];
    if (!selected.file || selected.asset) continue;
    validateLocalImage(selected.file);
    const result = await postJson(
      `/api/long/projects/${encodeURIComponent(currentLongProject.id)}/assets`,
      {
        name: selected.file.name,
        data_url: await fileToDataURL(selected.file),
      }
    );
    if (!result.asset) throw new Error("项目图片保存接口没有返回资产信息。");
    imageState[slot].asset = result.asset;
    imageElements(slot).name.textContent =
      `${result.asset.original_name || selected.file.name} · 已保存到项目`;
  }
}

function saveBoundLongWorkspace(showFeedback = true) {
  const requestedSegmentId = boundLongSegmentId;
  const operation = async () => {
    if (requestedSegmentId !== boundLongSegmentId) return false;
    return saveBoundLongWorkspaceNow(showFeedback);
  };
  longWorkspaceSaveChain = longWorkspaceSaveChain
    .catch(() => false)
    .then(operation);
  return longWorkspaceSaveChain;
}

async function saveBoundLongWorkspaceNow(showFeedback = true) {
  if (!boundLongSegmentId || !currentLongProject || loadingBoundWorkspace) {
    setLongSaveStatus("failed", "保存失败：当前页面没有绑定具体剧情段");
    return false;
  }
  clearTimeout(longWorkspaceSaveTimer);
  rememberBoundEphemeralImages();
  const segment = currentBoundSegment();
  setLongSaveStatus("saving", "正在保存项目图片与项目 JSON…");
  try {
    await persistSelectedAssets();
    const result = await postJson(
      `/api/long/projects/${encodeURIComponent(currentLongProject.id)}/segments/${encodeURIComponent(boundLongSegmentId)}`,
      {
        single_workspace: snapshotSingleWorkspace(),
        expected_revision: Number(segment?.single_workspace?.revision || 0),
      }
    );
    currentLongProject = result.project;
    currentLongReadiness = result.receipt?.readiness || null;
    const revision = Number(result.receipt?.workspace_revision || 0);
    const savedSegment = currentBoundSegment();
    if (savedSegment?.single_workspace?.validation) {
      lastCompileValidation = cloneJson(savedSegment.single_workspace.validation);
      if (lastCompileValidation.valid) {
        renderList(
          elements.validationResult,
          ["已由服务端重新校验并保存。"],
          "success",
          "验证通过。"
        );
      } else {
        renderList(
          elements.validationResult,
          lastCompileValidation.errors || [],
          "error",
          "尚未通过校验。"
        );
      }
    }
    const workspaceValid = result.receipt?.states?.workspace === "valid";
    setLongSaveStatus(
      workspaceValid ? "saved" : "neutral",
      `${workspaceValid ? "已保存并验证" : "已保存草稿"} · 工作区 v${revision}`
    );
    renderLongDeliverables(currentLongProject);
    renderLongReadiness(currentLongReadiness);
    renderLongSegments(currentLongProject);
    if (showFeedback) showToast("当前单段工作区已保存。");
    setLongControls();
    return true;
  } catch (error) {
    setLongSaveStatus("failed", `保存失败：${error.message || "未知错误"}`);
    if (showFeedback) showToast(error.message || "保存单段工作区失败。", true);
    return false;
  }
}

async function selectLongSegment(segmentId, scroll = true) {
  if (!currentLongProject) return;
  try {
    await ensureLMStudioSession();
  } catch (error) {
    showToast(error.message || "本地模型加载失败，无法进入剧本编辑模式。", true);
    return;
  }
  if (boundLongSegmentId && boundLongSegmentId !== segmentId) {
    if (!(await saveBoundLongWorkspace(false))) {
      showToast("当前单段工作区保存失败，仍停留在原段落。", true);
      return;
    }
  }
  const segment = currentLongProject.segments.find((item) => item.id === segmentId);
  if (!segment) return;
  if (!standaloneSingleSnapshot) standaloneSingleSnapshot = snapshotSingleWorkspace();
  selectedLongSegmentId = segmentId;
  boundLongSegmentId = segmentId;
  mountSingleWorkspaceForLong();
  applySingleWorkspace(segment.single_workspace || {}, segment);
  const workspaceRevision = Number(segment.single_workspace?.revision || 0);
  const workspaceState = segment.single_workspace?.state || "empty";
  setLongSaveStatus(
    workspaceState === "valid" ? "saved" : "neutral",
    `已载入工作区 v${workspaceRevision} · ${workspaceState}`
  );
  longElements.longSelectedSegmentTitle.textContent = `第 ${segment.index} 段 · ${segment.duration_display} · ${segment.story_card?.title || "未命名"}`;
  longElements.longSelectedSegmentHint.textContent = segment.index > 1 && segment.boundary_before === "continuous"
    ? "Picture 1 将在运行时自动使用上一段原生尾帧；当前描述来自计划开场状态。"
    : "图片选择、分镜生成和 H3 编译均使用原有单段逻辑。";
  renderLongSegments(currentLongProject);
  setLongControls();
  if (scroll) longElements.longSingleStage.scrollIntoView({ behavior: "smooth", block: "start" });
}

function setLongControls() {
  const active = longTaskIsActive();
  longElements.createLongProjectBtn.disabled = active;
  longElements.stopLongTaskBtn.disabled = !active;
  longElements.longProjectSelect.disabled = active;
  longElements.restoreRevisionBtn.disabled = active;
  longElements.longSegments.querySelectorAll("button, input, textarea, select").forEach((control) => {
    control.disabled = active || control.dataset.contentSyncDisabled === "true";
  });
  const fallbackReady = currentLongProject
    && currentLongProject.segments.every((item) => item.content_sync?.state === "clean")
    && currentLongProject.segments.every((item) => item.script_state === "ready")
    && currentLongProject.segments.every((item) => item.timeline_state === "valid")
    && currentLongProject.segments.every((item) => item.prompt_state === "valid")
    && currentLongProject.segments.every((item) => item.single_workspace?.state === "valid")
    && missingLongDialogue(currentLongProject).length === 0
    && duplicatedLongDialogue(currentLongProject).length === 0;
  const contentReady = Boolean(
    currentLongProject
    && (currentLongReadiness
      ? (currentLongReadiness.segments || []).every((item) => Object.values(item.checks || {}).every(Boolean))
        && !(currentLongReadiness.blockers || []).some((item) => item.code !== "authoring_confirmation_required")
      : fallbackReady)
  );
  const confirmationCurrent = Boolean(currentLongReadiness?.authoring_confirmation?.current);
  const modelUnloaded = Boolean(lmStudioState && !lmStudioState.model?.owned_loaded);
  const ready = contentReady && confirmationCurrent && modelUnloaded
    && currentLongProject?.status !== "completed";
  longElements.startLongRenderBtn.disabled = active || !ready;
  longElements.confirmAuthoringBtn.disabled = active || !contentReady || !currentLongProject;
  longElements.enterAuthoringBtn.disabled = active || lmSessionChanging || Boolean(lmStudioState?.model?.owned_loaded);
  longElements.pauseAuthoringBtn.disabled = active || lmSessionChanging || !lmStudioState?.model?.owned_loaded;
  longElements.startLongRenderBtn.title = !contentReady
    ? "请先完成并保存全部剧本、Shot、时间轴和 H3 提示词。"
    : !confirmationCurrent
      ? "请点击“确认剧本与全部 H3 完成”，保存内容指纹并释放模型显存。"
      : !modelUnloaded ? "本地模型仍占用显存，请先释放。" : "";
  const hasUncompiled = currentLongProject?.segments?.some(
    (item) => item.prompt_state !== "valid" || item.single_workspace?.state !== "valid"
  );
  const allContentSynchronized = currentLongProject?.segments?.every(
    (item) => item.content_sync?.state === "clean"
  );
  longElements.generateAllLongPromptsBtn.disabled = active
    || !currentLongProject
    || !hasUncompiled
    || !allContentSynchronized;
  longElements.generateAllLongPromptsBtn.title = allContentSynchronized
    ? ""
    : "请先同步当前修改段；上游导致的后续失效请使用“重新生成后续段”。";
  longElements.saveLongWorkspaceBtn.disabled = active || !boundLongSegmentId;
  elements.generateScriptBtn.disabled = active || activeRequest;
  elements.analyzeImagesBtn.disabled = active || activeRequest;
  elements.compileBtn.disabled = active || activeRequest || !scriptState;
  longElements.refreshLongProjectsBtn.disabled = active;
  if (currentLongProject?.status === "completed") {
    longElements.startLongRenderBtn.querySelector("span").textContent = "总片已完成";
  } else if (currentLongProject?.segments?.some((item) => item.render_state === "accepted")) {
    longElements.startLongRenderBtn.querySelector("span").textContent = "继续连续生成";
  } else {
    longElements.startLongRenderBtn.querySelector("span").textContent = "开始连续生成";
  }
}

function renderLongTask(task) {
  currentLongTask = task || null;
  const panel = longElements.longTaskPanel;
  panel.className = "long-task-panel " + (task?.state || "neutral");
  if (!task) {
    longElements.longTaskTitle.textContent = "尚无后台任务";
    longElements.longTaskMessage.textContent = "先生成或载入一个项目。";
    longElements.longProgressBar.style.width = "0%";
    longElements.longLiveOutput.textContent = "等待任务…";
    setLongControls();
    return;
  }
  const labels = {
    script: "长剧本生成",
    regenerate: "后段重生成",
    reconcile: "剧情状态同步",
    precompile: "H3 原子预编译",
    render: "连续视频生成",
  };
  longElements.longTaskTitle.textContent = `${labels[task.kind] || task.kind} · ${task.state}`;
  longElements.longTaskMessage.textContent = task.message || task.stage || "处理中…";
  const total = Number(task.total || 0);
  const current = Number(task.current || 0);
  const percent = total > 0 ? Math.min(100, Math.max(0, current / total * 100)) : 0;
  longElements.longProgressBar.style.width = `${percent}%`;
  const startedAt = Date.parse(task.started_at || "");
  const elapsedSeconds = Number.isFinite(startedAt)
    ? Math.max(0, Math.floor((Date.now() - startedAt) / 1000))
    : 0;
  const waitingMessage = ["script", "regenerate", "reconcile", "precompile"].includes(task.kind)
    ? `LM Studio 本地 Qwen 正在思考并流式生成；思考过程和最终 JSON 会分区显示。\n任务已运行 ${elapsedSeconds} 秒。`
    : "等待后台任务输出…";
  const thought = task.thinking_text
    ? `【思考过程】\n${task.thinking_text}${task.live_text ? "\n\n【最终 JSON】\n" : ""}`
    : "";
  longElements.longLiveOutput.textContent = thought + (task.live_text || task.error?.message || (!thought ? waitingMessage : ""));
  setLongControls();
}

function scheduleLongTaskPoll(taskId) {
  currentLongTaskId = taskId;
  clearTimeout(longTaskPollTimer);
  const poll = async () => {
    try {
      const result = await jsonRequest(`/api/long/tasks/${encodeURIComponent(taskId)}`);
      renderLongTask(result.task);
      if (["queued", "running", "retrying"].includes(result.task.state)) {
        if (result.task.project_id) await loadLongProject(result.task.project_id, false, true);
        longTaskPollTimer = setTimeout(poll, 1400);
        return;
      }
      currentLongTaskId = "";
      await loadProviderStatus(false);
      if (result.task.project_id) await loadLongProject(result.task.project_id, false);
      await loadLongProjects(false);
      if (
        result.task.state === "completed"
        && result.task.kind === "reconcile"
        && result.task.result?.requires_boundary_confirmation
      ) {
        const proposal = result.task.result;
        const accept = window.confirm(
          `Qwen 建议把本段边界从 ${proposal.current_boundary_before} 改为 ${proposal.recommended_boundary_before}。\n\n${proposal.boundary_reason || "未提供额外说明。"}\n\n点击“确定”采用建议；点击“取消”保留当前边界。`
        );
        try {
          const committed = await postJson(
            `/api/long/projects/${encodeURIComponent(result.task.project_id)}/reconcile/commit`,
            { proposal_id: proposal.proposal_id, accept_boundary: accept }
          );
          renderLongProject(committed.project, committed.readiness);
          if (selectedLongSegmentId) await selectLongSegment(selectedLongSegmentId, false);
          showToast("剧情状态已同步；请重新编译当前段 H3 提示词，再处理后续段。");
        } catch (error) {
          showToast(error.message || "边界确认保存失败。", true);
        }
        return;
      }
      showToast(
        result.task.state === "completed" ? result.task.message || "任务已完成。" : result.task.message || "任务已停止。",
        result.task.state === "failed"
      );
      const shouldAutoCompile = result.task.state === "completed"
        && ["script", "regenerate"].includes(result.task.kind)
        && longElements.longAutoCompile.checked
        && currentLongProject?.segments?.some(
          (item) => item.prompt_state !== "valid" || item.single_workspace?.state !== "valid"
        );
      if (shouldAutoCompile) {
        showToast("长剧本已完成，正在自动继续生成全部 H3 完整提示词；不会启动 GPU。", false);
        await generateAllLongPrompts({ automatic: true });
      }
    } catch (error) {
      currentLongTaskId = "";
      renderLongTask({ kind: "task", state: "failed", message: error.message, error: { message: error.message } });
      showToast(error.message, true);
    }
  };
  poll();
}

async function beginLongTask(result) {
  const task = result?.task;
  if (!task?.id) throw new Error("本地服务没有返回任务 ID。");
  renderLongTask(task);
  if (result.project_id) {
    const temporary = createElement("option", "", `正在生成 · ${result.project_id}`);
    temporary.value = result.project_id;
    longElements.longProjectSelect.appendChild(temporary);
    longElements.longProjectSelect.value = result.project_id;
  }
  scheduleLongTaskPoll(task.id);
}

async function createLongProject() {
  if (longTaskIsActive()) return;
  const payload = collectLongProjectRequest();
  if (!payload.idea) {
    showToast("请先填写长视频 Idea。", true);
    longElements.longIdea.focus();
    return;
  }
  persistLongForm();
  try {
    await ensureLMStudioSession();
    const result = await postJson("/api/long/projects", payload);
    await beginLongTask(result);
    showToast("已开始生成长剧本；此阶段不会启动 GPU。", false);
  } catch (error) {
    showToast(error.message, true);
  }
}

async function loadLongProjects(showFeedback = false) {
  try {
    const result = await jsonRequest("/api/long/projects");
    const selected = currentLongProject?.id || longElements.longProjectSelect.value;
    longElements.longProjectSelect.replaceChildren();
    const placeholder = createElement("option", "", result.projects?.length ? "选择已保存项目" : "暂无项目");
    placeholder.value = "";
    longElements.longProjectSelect.appendChild(placeholder);
    for (const project of result.projects || []) {
      const option = createElement(
        "option",
        "",
        `${project.title} · ${project.actual_seconds.toFixed(2)}s · ${project.segment_count} 段 · ${project.status}`
      );
      option.value = project.id;
      longElements.longProjectSelect.appendChild(option);
    }
    if (selected && (result.projects || []).some((item) => item.id === selected)) {
      longElements.longProjectSelect.value = selected;
    } else if (!currentLongProject && result.projects?.length) {
      const latestProjectId = result.projects[0].id;
      longElements.longProjectSelect.value = latestProjectId;
      await loadLongProject(latestProjectId, false, true);
    }
    if (showFeedback) showToast(`已刷新，共 ${result.projects?.length || 0} 个项目。`);
  } catch (error) {
    if (showFeedback) showToast(error.message, true);
  }
}

function dialogueToText(dialogue) {
  return (dialogue || []).map((item) => `${item.speaker || ""}|${item.language || "Chinese"}|${item.text || ""}`).join("\n");
}

function textToDialogue(text) {
  return String(text || "").replace(/\r\n/g, "\n").split("\n").map((line) => line.trim()).filter(Boolean).map((line) => {
    const parts = line.split("|");
    if (parts.length >= 3) {
      return { speaker: parts.shift().trim(), language: parts.shift().trim() || "Chinese", text: parts.join("|").trim() };
    }
    return { speaker: "角色", language: "Chinese", text: line };
  });
}

function addSegmentField(container, labelText, value, name, rows = 2) {
  const label = createElement("label", "", labelText);
  const textarea = createElement("textarea", "segment-edit-field");
  textarea.dataset.segmentField = name;
  textarea.rows = rows;
  textarea.value = value || "";
  container.append(label, textarea);
}

function updateBeatFrameHint(row) {
  const start = Number.parseFloat(row.querySelector("[data-beat-start]").value);
  const end = Number.parseFloat(row.querySelector("[data-beat-end]").value);
  const hint = row.querySelector(".beat-frame-hint");
  if (!Number.isFinite(start) || !Number.isFinite(end)) {
    hint.textContent = "秒数无效";
    hint.classList.add("invalid");
    return;
  }
  const startFrame = Math.round(start * 24);
  const endFrame = Math.round(end * 24);
  hint.textContent = `[${startFrame}, ${endFrame}) · ${Math.max(0, endFrame - startFrame)} 帧`;
  hint.classList.toggle("invalid", endFrame <= startFrame);
}

function appendBeatRow(container, beat, segment) {
  const canonicalSegmentEnd = (Number(segment.frames) / 24).toFixed(3);
  const row = createElement("div", "segment-beat-row");
  const startLabel = createElement("label", "beat-time-field", "开始秒");
  const start = createElement("input", "segment-beat-time");
  start.type = "number";
  start.step = "0.001";
  start.min = "0";
  start.max = canonicalSegmentEnd;
  start.value = String(beat.start_seconds ?? "0.000");
  start.dataset.beatStart = "";
  startLabel.appendChild(start);

  const endLabel = createElement("label", "beat-time-field", "结束秒");
  const end = createElement("input", "segment-beat-time");
  end.type = "number";
  end.step = "0.001";
  end.min = "0";
  end.max = canonicalSegmentEnd;
  end.value = String(beat.end_seconds ?? canonicalSegmentEnd);
  end.dataset.beatEnd = "";
  endLabel.appendChild(end);

  const hint = createElement("span", "beat-frame-hint");
  const remove = createElement("button", "button ghost beat-remove", "删除");
  remove.type = "button";
  remove.addEventListener("click", () => {
    const rows = Array.from(container.querySelectorAll(".segment-beat-row"));
    if (rows.length <= 1) {
      showToast("每段至少需要一个 Beat。", true);
      return;
    }
    const position = rows.indexOf(row);
    const removedStart = start.value;
    const removedEnd = end.value;
    row.remove();
    const remaining = Array.from(container.querySelectorAll(".segment-beat-row"));
    if (position < remaining.length) {
      remaining[position].querySelector("[data-beat-start]").value = removedStart;
      updateBeatFrameHint(remaining[position]);
    } else {
      const previous = remaining[remaining.length - 1];
      previous.querySelector("[data-beat-end]").value = removedEnd;
      updateBeatFrameHint(previous);
    }
  });

  const actionLabel = createElement("label", "beat-action-field", "该时段动作");
  const action = createElement("textarea", "segment-edit-field segment-beat-action");
  action.rows = 2;
  action.value = beat.action || "";
  action.dataset.beatAction = "";
  actionLabel.appendChild(action);
  row.append(startLabel, endLabel, hint, remove, actionLabel);
  start.addEventListener("input", () => updateBeatFrameHint(row));
  end.addEventListener("input", () => updateBeatFrameHint(row));
  container.appendChild(row);
  updateBeatFrameHint(row);
  return row;
}

function addBeatEditor(card, segment) {
  const canonicalSegmentEnd = (Number(segment.frames) / 24).toFixed(3);
  const section = createElement("section", "segment-beat-editor");
  section.dataset.beatEditor = "";
  const header = createElement("div", "beat-editor-header");
  const title = createElement("strong", "", "动作与时间线（秒级 Beats）");
  const note = createElement(
    "span", "field-note",
    `服务端按 24fps 吸附；必须连续覆盖 0.000–${canonicalSegmentEnd} 秒。`
  );
  header.append(title, note);
  section.appendChild(header);
  if (segment.timeline_state !== "valid") {
    const warning = createElement(
      "div", "beat-review-warning",
      segment.timeline_state === "needs_review"
        ? "这是旧式时间文本迁移结果，请检查并保存 Beats 后再生成视频。"
        : "本段时间轴无效，请补齐 Beats。"
    );
    section.appendChild(warning);
  }
  const rows = createElement("div", "segment-beat-rows");
  section.appendChild(rows);
  const initialBeats = segment.beats?.length
    ? segment.beats
    : [{ start_seconds: "0.000", end_seconds: canonicalSegmentEnd, action: segment.legacy_action || "" }];
  for (const beat of initialBeats) appendBeatRow(rows, beat, segment);

  const add = createElement("button", "button ghost beat-add", "拆分最后一个 Beat");
  add.type = "button";
  add.addEventListener("click", () => {
    const items = Array.from(rows.querySelectorAll(".segment-beat-row"));
    if (items.length >= 64) {
      showToast("单段最多 64 个 Beats。", true);
      return;
    }
    const last = items[items.length - 1];
    const startInput = last.querySelector("[data-beat-start]");
    const endInput = last.querySelector("[data-beat-end]");
    const startSeconds = Number.parseFloat(startInput.value);
    const endSeconds = Number.parseFloat(endInput.value);
    const startFrame = Math.round(startSeconds * 24);
    const endFrame = Math.round(endSeconds * 24);
    if (!Number.isFinite(startSeconds) || !Number.isFinite(endSeconds) || endFrame - startFrame < 2) {
      showToast("最后一个 Beat 太短，无法继续拆分。", true);
      return;
    }
    const middleFrame = startFrame + Math.floor((endFrame - startFrame) / 2);
    const middle = (middleFrame / 24).toFixed(3);
    endInput.value = middle;
    updateBeatFrameHint(last);
    appendBeatRow(rows, { start_seconds: middle, end_seconds: endInput.max || canonicalSegmentEnd, action: "" }, segment);
    const newLast = rows.lastElementChild;
    newLast.querySelector("[data-beat-end]").value = (endFrame / 24).toFixed(3);
    updateBeatFrameHint(newLast);
  });
  section.appendChild(add);
  card.appendChild(section);
}

function renderStoryCardSegments(project) {
  longElements.longSegments.replaceChildren();
  for (const segment of project.segments) {
    const story = segment.story_card || {};
    const selected = segment.id === selectedLongSegmentId;
    const workspaceState = segment.single_workspace?.state || "empty";
    const contentSync = segment.content_sync || { state: "clean", source: "legacy" };
    const contentSyncLabel = {
      clean: "已同步",
      story_dirty: "剧情待同步",
      shots_dirty: "Shot 待同步",
      failed: "同步失败",
    }[contentSync.state] || contentSync.state;
    const card = createElement(
      "article",
      `long-segment-card story-card${selected ? " selected" : ""}`
    );
    card.dataset.segmentId = segment.id;

    const header = createElement("div", "segment-card-header");
    const headingWrap = createElement("div", "story-card-title-row");
    const dragHandle = createElement("span", "story-drag-handle", "⠿");
    dragHandle.title = "拖动调整顺序";
    dragHandle.draggable = true;
    const heading = createElement("h3", "", `第 ${segment.index} 段 · ${segment.duration_display}`);
    headingWrap.append(dragHandle, heading);
    const states = createElement(
      "span",
      "segment-state",
      `剧情 ${segment.provenance === "manual" ? "手动" : "AI"} · 内容 ${contentSyncLabel} · 单段 ${workspaceState} · 提示词 ${segment.prompt_state} · 视频 ${segment.render_state}`
    );
    header.append(headingWrap, states);
    card.appendChild(header);

    const target = segment.story_target || {};
    const targetDetails = createElement("details", "segment-story-target");
    const targetSummary = createElement(
      "summary",
      "",
      `剧情目标 · 章节 ${(target.chapter_numbers || []).join("、") || "未分配"}${target.must_close_story ? " · 必须闭合结尾" : ""}`
    );
    const targetBody = createElement("pre");
    targetBody.textContent = JSON.stringify({
      chapter_phases: target.chapter_phases || [],
      outline_chapters: target.outline_chapters || [],
      required_ending_conditions: target.required_ending_conditions || [],
    }, null, 2);
    targetDetails.append(targetSummary, targetBody);
    card.appendChild(targetDetails);

    const boundaryLabel = createElement("label", "", "与上一段的边界");
    const boundary = createElement("select", "segment-boundary");
    boundary.dataset.storyField = "boundary_before";
    for (const [value, label] of segment.index === 1
      ? [["start", "起始段"]]
      : [["continuous", "连续：使用上一段原生尾帧"], ["cut", "明确切镜：独立 T2VA"]]) {
      const option = createElement("option", "", label);
      option.value = value;
      boundary.appendChild(option);
    }
    boundary.value = story.boundary_before || segment.boundary_before;
    boundary.disabled = segment.index === 1;
    card.append(boundaryLabel, boundary);

    const titleLabel = createElement("label", "", "剧情卡片标题");
    const titleInput = createElement("input", "segment-edit-field");
    titleInput.dataset.storyField = "title";
    titleInput.value = story.title || `第 ${segment.index} 段`;
    titleInput.readOnly = true;
    titleInput.title = "由“同步本段剧情状态”根据剧情与 Shot 生成";
    card.append(titleLabel, titleInput);
    const addStoryField = (labelText, value, name, rows = 2, readOnly = false) => {
      const label = createElement("label", "", labelText);
      const textarea = createElement("textarea", "segment-edit-field");
      textarea.dataset.storyField = name;
      textarea.rows = rows;
      textarea.value = value || "";
      textarea.readOnly = readOnly;
      if (readOnly) textarea.title = "派生字段；请通过显式内容同步更新";
      card.append(label, textarea);
    };
    addStoryField("中文剧情正文", story.story_text, "story_text", 5, false);
    addStoryField(
      "精确对白（每行 speaker|language|原文）",
      dialogueToText(story.dialogue),
      "dialogue",
      2,
      true
    );
    addStoryField("本段开始状态（固定为上一段真实尾帧）", story.opening_state, "opening_state", 2, true);
    addStoryField("本段结束状态（同步生成）", story.ending_state, "ending_state", 2, true);
    addStoryField(
      "出场人物（每行一个）",
      (story.present_characters || []).join("\n"),
      "present_characters",
      2,
      true
    );

    const actions = createElement("div", "segment-card-actions");
    const open = createElement(
      "button",
      "button primary",
      selected ? "正在编辑此段" : "用单段工作台编辑"
    );
    open.type = "button";
    open.addEventListener("click", () => selectLongSegment(segment.id));
    const save = createElement("button", "button ghost", "应用剧情修改");
    save.type = "button";
    save.addEventListener("click", () => saveLongStoryCard(card, segment));
    actions.append(open, save);
    const sync = createElement("button", "button secondary", "同步本段剧情状态");
    sync.type = "button";
    const downstreamDirty = contentSync.state === "story_dirty"
      && String(contentSync.source || "").startsWith("upstream_");
    sync.disabled = contentSync.state === "clean" || downstreamDirty;
    sync.dataset.contentSyncDisabled = String(sync.disabled);
    sync.title = downstreamDirty
      ? "这是上游改动造成的后续失效，请使用“重新生成后续段”"
      : "明确调用 Qwen，同步剧情正文、Shot、结束状态与出场人物";
    sync.addEventListener("click", () => reconcileLongSegment(card, segment));
    actions.appendChild(sync);
    const structure = [
      ["move_up", "上移", segment.index === 1],
      ["move_down", "下移", segment.index === project.segments.length],
      ["add_after", "后面新增", false],
      ["split", "拆成两段", false],
      ["merge_next", "与后段合并", segment.index === project.segments.length],
      ["delete", "删除", project.segments.length === 1],
    ];
    for (const [operation, label, disabled] of structure) {
      const button = createElement("button", "button ghost compact-button", label);
      button.type = "button";
      button.disabled = disabled;
      button.addEventListener("click", () => runTimelineOperation(operation, segment));
      actions.appendChild(button);
    }
    if (segment.index < project.segments.length) {
      const regenerate = createElement("button", "button secondary", "重新生成后续段…");
      regenerate.type = "button";
      regenerate.addEventListener("click", () => openRegenerationDialog(segment.index));
      actions.appendChild(regenerate);
    }
    card.appendChild(actions);

    dragHandle.addEventListener("dragstart", (event) => {
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", segment.id);
      card.classList.add("dragging");
    });
    dragHandle.addEventListener("dragend", () => card.classList.remove("dragging"));
    card.addEventListener("dragover", (event) => {
      event.preventDefault();
      event.dataTransfer.dropEffect = "move";
      card.classList.add("drag-target");
    });
    card.addEventListener("dragleave", () => card.classList.remove("drag-target"));
    card.addEventListener("drop", (event) => {
      event.preventDefault();
      card.classList.remove("drag-target");
      const sourceId = event.dataTransfer.getData("text/plain");
      if (sourceId && sourceId !== segment.id) {
        runTimelineOperation("move_to", { id: sourceId, index: 0 }, segment.index);
      }
    });
    longElements.longSegments.appendChild(card);
  }
}

function renderLongSegments(project) {
  renderStoryCardSegments(project);
}

async function saveLongStoryCard(card, segment) {
  if (!currentLongProject || longTaskIsActive()) return false;
  if (boundLongSegmentId === segment.id && !(await saveBoundLongWorkspace(false))) {
    showToast("当前单段工作区尚未保存，已取消剧情修改。", true);
    return false;
  }
  const fields = Object.fromEntries(
    Array.from(card.querySelectorAll("[data-story-field]")).map((input) => [
      input.dataset.storyField,
      input.value,
    ])
  );
  const storyCard = {
    story_text: fields.story_text || "",
    boundary_before: fields.boundary_before || segment.boundary_before,
  };
  const liveSegment = currentLongProject.segments.find((item) => item.id === segment.id) || segment;
  const existing = {
    story_text: liveSegment.story_card?.story_text || "",
    boundary_before: liveSegment.story_card?.boundary_before || liveSegment.boundary_before,
  };
  if (JSON.stringify(storyCard) === JSON.stringify(existing)) {
    showToast(`第 ${segment.index} 段剧情内容未变化；现有分镜和 H3 提示词保持有效。`);
    return true;
  }
  const discardsSavedWork = currentLongProject.segments
    .slice(Number(liveSegment.index) - 1)
    .some((item) => item.prompt_state === "valid" || item.single_workspace?.state === "valid");
  if (discardsSavedWork && !window.confirm(
    `修改第 ${liveSegment.index} 段剧情会使本段及后续已经保存的分镜、H3 提示词失效。是否继续？`
  )) return false;
  try {
    const result = await postJson(
      `/api/long/projects/${encodeURIComponent(currentLongProject.id)}/segments/${encodeURIComponent(segment.id)}`,
      { story_card: storyCard, confirm_invalidate: discardsSavedWork }
    );
    currentLongProject = result.project;
    selectedLongSegmentId = segment.id;
    boundLongSegmentId = "";
    renderLongProject(currentLongProject, result.receipt?.readiness);
    await selectLongSegment(selectedLongSegmentId, false);
    showToast(`第 ${segment.index} 段剧情修改已应用；本段及后续受影响内容已明确标记为待重生成。`);
    return true;
  } catch (error) {
    showToast(error.message || "保存剧情卡片失败。", true);
    return false;
  }
}

async function reconcileLongSegment(card, segment) {
  if (!currentLongProject || longTaskIsActive()) return;
  if (!(await saveLongStoryCard(card, segment))) return;
  const live = currentLongProject.segments.find((item) => item.id === segment.id);
  if (!live) return;
  const syncState = live.content_sync?.state || "clean";
  if (syncState === "clean") {
    showToast("本段剧情正文与 Shot 没有未同步改动。", false);
    return;
  }
  if (
    syncState === "story_dirty"
    && String(live.content_sync?.source || "").startsWith("upstream_")
  ) {
    showToast("该段由上游改动而失效，请从上游段点击“重新生成后续段”。", true);
    return;
  }
  try {
    await ensureLMStudioSession();
    const result = await postJson(
      `/api/long/projects/${encodeURIComponent(currentLongProject.id)}/segments/${encodeURIComponent(segment.id)}/reconcile`,
      {}
    );
    await beginLongTask(result);
    showToast("已开始显式同步；自动保存本身不会调用 Qwen。", false);
  } catch (error) {
    showToast(error.message || "剧情状态同步启动失败。", true);
  }
}

async function runTimelineOperation(operation, segment, destinationIndex = null) {
  if (!currentLongProject || longTaskIsActive()) return;
  if (operation === "delete" && !window.confirm(`删除第 ${segment.index} 段并重新均分总帧数？`)) return;
  if (boundLongSegmentId && !(await saveBoundLongWorkspace(false))) {
    showToast("当前单段工作区尚未保存，已取消时间线操作。", true);
    return;
  }
  const sourceIndex = Number(segment.index || 1);
  try {
    const result = await postJson(
      `/api/long/projects/${encodeURIComponent(currentLongProject.id)}/timeline`,
      {
        operation,
        segment_id: segment.id,
        destination_index: destinationIndex,
      }
    );
    currentLongProject = result.project;
    longEphemeralImages.clear();
    let nextIndex = sourceIndex;
    if (operation === "move_up") nextIndex = sourceIndex - 1;
    if (operation === "move_down" || operation === "add_after") nextIndex = sourceIndex + 1;
    if (operation === "move_to") nextIndex = Number(destinationIndex || sourceIndex);
    nextIndex = Math.max(1, Math.min(currentLongProject.segments.length, nextIndex));
    selectedLongSegmentId = currentLongProject.segments[nextIndex - 1].id;
    boundLongSegmentId = "";
    renderLongProject(currentLongProject, result.readiness);
    await selectLongSegment(selectedLongSegmentId, false);
    showToast("时间线结构已更新；总帧数保持不变，受影响片段需重新编译。");
  } catch (error) {
    showToast(error.message || "时间线操作失败。", true);
  }
}

function longSegmentPrompt(segment) {
  if (segment?.prompt_state !== "valid" || segment?.single_workspace?.state !== "valid") return "";
  return String(segment?.h3_prompt || segment?.single_workspace?.prompt || "").trim();
}

function longSegmentMode(segment) {
  const saved = String(segment?.single_workspace?.form?.mode || "").trim();
  if (saved) return saved;
  return segment?.index > 1 && segment?.boundary_before === "continuous" ? "I2VA" : "T2VA";
}

function buildLongStoryOutput(project) {
  if (!project) return "";
  const lines = [
    project.title || "未命名长视频",
    `总时长：${Number(project.actual_seconds || 0).toFixed(3)} 秒 · ${project.segments?.length || 0} 段 · ${project.fps || 24}fps`,
  ];
  if (project.story_bible?.premise) lines.push(`故事总述：${project.story_bible.premise}`);
  lines.push("");
  for (const segment of project.segments || []) {
    const story = segment.story_card || {};
    lines.push(`===== 第 ${segment.index} 段 · ${segment.duration_display} · ${story.title || "未命名"} =====`);
    lines.push(story.story_text || "（本段剧情尚未填写）");
    const dialogue = Array.isArray(story.dialogue) ? story.dialogue : [];
    if (dialogue.length) {
      lines.push("对白：");
      for (const item of dialogue) {
        const speaker = item.speaker ? `${item.speaker} ` : "";
        const language = item.language ? `[${item.language}] ` : "";
        lines.push(`- ${speaker}${language}${item.text || ""}`);
      }
    }
    if (story.opening_state) lines.push(`开始状态：${story.opening_state}`);
    if (story.ending_state) lines.push(`结束状态：${story.ending_state}`);
    lines.push("");
  }
  return lines.join("\n").trim();
}

function buildAllLongPromptsOutput(project) {
  const blocks = [];
  for (const segment of project?.segments || []) {
    const prompt = longSegmentPrompt(segment);
    if (!prompt) continue;
    blocks.push(
      `===== 第 ${segment.index} 段 · ${segment.duration_display} · ${longSegmentMode(segment)} · ${segment.story_card?.title || "未命名"} =====\n${prompt}`
    );
  }
  return blocks.join("\n\n");
}

function renderLongDeliverables(project) {
  const storyText = buildLongStoryOutput(project);
  const allPromptsText = buildAllLongPromptsOutput(project);
  const compiledCount = (project?.segments || []).filter((item) => Boolean(longSegmentPrompt(item))).length;
  const totalCount = project?.segments?.length || 0;
  const allReady = totalCount > 0 && compiledCount === totalCount
    && project.segments.every((item) => item.prompt_state === "valid" && item.single_workspace?.state === "valid");

  longElements.longFullStoryOutput.textContent = storyText || "尚未生成长剧本。";
  longElements.copyLongStoryBtn.disabled = !storyText;
  longElements.copyAllLongPromptsBtn.disabled = !allPromptsText;
  longElements.downloadAllLongPromptsBtn.disabled = !allPromptsText;
  longElements.copyAllLongPromptsBtn.textContent = compiledCount
    ? `复制全部已生成 H3 提示词（${compiledCount}/${totalCount}）`
    : "复制全部 H3 提示词";
  longElements.longDeliverablesStatus.textContent = allReady
    ? `完整长剧本与 ${totalCount} 段 H3 提示词均已生成并通过校验，可以逐段检查或复制下载。`
    : `完整长剧本已生成；H3 完整提示词 ${compiledCount}/${totalCount} 段。请点击上方“第 2 步：生成全部 H3 完整提示词”。`;

  longElements.longPromptOutputs.replaceChildren();
  for (const segment of project?.segments || []) {
    const prompt = longSegmentPrompt(segment);
    const details = createElement("details", `long-prompt-output${prompt ? " ready" : " pending"}`);
    details.dataset.promptSegmentId = segment.id;
    details.open = Boolean(prompt) && (allReady || segment.id === selectedLongSegmentId);
    const summary = createElement(
      "summary",
      "",
      `第 ${segment.index} 段 · ${segment.duration_display} · ${longSegmentMode(segment)} · ${prompt ? "完整提示词" : "待生成"}`
    );
    details.appendChild(summary);
    if (!prompt) {
      details.appendChild(createElement(
        "p",
        "long-prompt-empty",
        "这一段目前只有剧情卡片，还没有调用 Qwen 生成分镜并编译成 MiniMax H3 完整提示词。"
      ));
    } else {
      const actions = createElement("div", "long-prompt-actions");
      const copy = createElement("button", "button ghost compact-button", "复制本段");
      copy.type = "button";
      copy.addEventListener("click", () => copyText(prompt, `第 ${segment.index} 段 H3 提示词已复制。`));
      const download = createElement("button", "button ghost compact-button", "下载本段 TXT");
      download.type = "button";
      download.addEventListener("click", () => downloadText(
        prompt,
        `${project.id}_segment_${String(segment.index).padStart(2, "0")}_${longSegmentMode(segment)}.txt`
      ));
      actions.append(copy, download);
      const output = createElement("pre", "long-full-prompt");
      output.textContent = prompt;
      details.append(actions, output);
    }
    longElements.longPromptOutputs.appendChild(details);
  }
}

function renderLongReadiness(readiness) {
  currentLongReadiness = readiness || null;
  if (!readiness) {
    longElements.longReadiness.hidden = true;
    longElements.longReadiness.replaceChildren();
    return;
  }
  longElements.longReadiness.hidden = false;
  longElements.longReadiness.className = `long-readiness ${readiness.ready ? "ready" : "blocked"}`;
  const title = createElement(
    "strong",
    "",
    readiness.ready
      ? `视频生成前检查通过 · ${readiness.ready_segments}/${readiness.total_segments} 段已可靠落盘`
      : `视频生成尚未就绪 · ${readiness.ready_segments}/${readiness.total_segments} 段完成`
  );
  longElements.longReadiness.replaceChildren(title);
  const confirmation = readiness.authoring_confirmation || {};
  longElements.longReadiness.appendChild(createElement(
    "p",
    "field-help",
    confirmation.current
      ? `创作内容指纹已确认${confirmation.confirmed_at ? ` · ${confirmation.confirmed_at}` : ""}；项目模型必须保持已卸载。`
      : "创作内容尚未按当前版本确认；任何剧本或提示词修改都会使旧确认自动过期。"
  ));
  if (!readiness.ready) {
    const list = createElement("ul");
    for (const blocker of (readiness.blockers || []).slice(0, 12)) {
      list.appendChild(createElement("li", "", blocker.message || blocker.code));
    }
    longElements.longReadiness.appendChild(list);
  }
}

async function generateAllLongPrompts(options = {}) {
  if (!currentLongProject || longTaskIsActive()) return;
  const automatic = Boolean(options?.automatic);
  try {
    if (boundLongSegmentId && !(await saveBoundLongWorkspace(false))) {
      throw new Error("当前单段工作区保存失败，预编译没有启动。");
    }
    await ensureLMStudioSession();
    const result = await postJson(
      `/api/long/projects/${encodeURIComponent(currentLongProject.id)}/precompile`,
      {}
    );
    await beginLongTask(result);
    showToast(
      automatic
        ? "长剧本已完成，服务端正在逐段生成并原子保存 H3 提示词。"
        : "服务端预编译已开始；关闭浏览器也会继续，不会启动 GPU。"
    );
    return true;
  } catch (error) {
    setLongSaveStatus("failed", `预编译启动失败：${error.message || "未知错误"}`);
    showToast(error.message || "批量生成提示词失败。", true);
    return false;
  }
}

function renderLongProject(project, readiness = undefined) {
  const projectChanged = currentLongProject?.id && currentLongProject.id !== project.id;
  currentLongProject = project;
  if (readiness !== undefined) currentLongReadiness = readiness;
  else if (projectChanged) currentLongReadiness = null;
  if (!project.segments.some((item) => item.id === selectedLongSegmentId)) {
    selectedLongSegmentId = project.segments[0]?.id || "";
  }
  longElements.longProjectSummary.hidden = false;
  longElements.longProjectTitle.textContent = project.title || "未命名长视频";
  const distribution = project.segments.map((item) => item.frames).join(" / ");
  const master = project.master?.path ? ` · 总片：${project.master.path}` : "";
  const totalTokens = Number(project.usage?.total_tokens || 0);
  const cachedTokens = Number(project.usage?.prompt_tokens_details?.cached_tokens || 0);
  const usage = totalTokens ? ` · Token ${totalTokens}${cachedTokens ? `（缓存 ${cachedTokens}）` : ""}` : "";
  longElements.longProjectMeta.textContent =
    `${project.actual_seconds.toFixed(3)} 秒 · ${project.target_frames} 帧 @ ${project.fps}fps · ${project.segments.length} 段 · 帧分配 ${distribution} · ${project.status}${usage}${master}`;
  const warnings = [...(project.warnings || [])];
  const missingDialogue = missingLongDialogue(project);
  if (missingDialogue.length) warnings.push(`尚未逐字放入指定对白：${missingDialogue.join("；")}`);
  const duplicatedDialogue = duplicatedLongDialogue(project);
  if (duplicatedDialogue.length) warnings.push(`指定对白被重复使用：${duplicatedDialogue.join("；")}`);
  const pendingWorkspace = project.segments
    .filter((item) => item.single_workspace?.state !== "valid" || item.prompt_state !== "valid")
    .map((item) => item.index);
  if (pendingWorkspace.length) warnings.push(`尚未完成单段分镜与 H3 编译：${pendingWorkspace.join("、")}`);
  if (project.scheduler?.last_error?.message) warnings.push(project.scheduler.last_error.message);
  longElements.longWarnings.hidden = warnings.length === 0;
  longElements.longWarnings.textContent = warnings.join("\n");
  longElements.longRevisionSelect.replaceChildren();
  const revisionEmpty = createElement("option", "", "选择 Revision");
  revisionEmpty.value = "";
  longElements.longRevisionSelect.appendChild(revisionEmpty);
  for (const revision of [...(project.revision_history || [])].reverse()) {
    const option = createElement("option", "", `r${revision.revision} · ${revision.reason}`);
    option.value = String(revision.revision);
    longElements.longRevisionSelect.appendChild(option);
  }
  renderLongDeliverables(project);
  renderLongReadiness(currentLongReadiness);
  renderLongSegments(project);
  setLongControls();
}

async function loadLongProject(projectId, showFeedback = false, quietMissing = false) {
  if (!projectId) return;
  try {
    const switchingProject = Boolean(currentLongProject && currentLongProject.id !== projectId);
    if (switchingProject && boundLongSegmentId) {
      if (!(await saveBoundLongWorkspace(false))) {
        showToast("当前单段工作区保存失败，已取消切换项目。", true);
        longElements.longProjectSelect.value = currentLongProject.id;
        return;
      }
      rememberBoundEphemeralImages();
      boundLongSegmentId = "";
      selectedLongSegmentId = "";
    }
    const result = await jsonRequest(`/api/long/projects/${encodeURIComponent(projectId)}`);
    renderLongProject(result.project, result.readiness);
    longElements.longProjectSelect.value = projectId;
    const tasks = Array.isArray(result.tasks) ? result.tasks : [];
    const activeTask = [...tasks].reverse().find((task) =>
      ["queued", "running", "retrying"].includes(task.state)
    );
    if (activeTask && activeTask.id !== currentLongTaskId) {
      renderLongTask(activeTask);
      scheduleLongTaskPoll(activeTask.id);
    } else if (!activeTask && !currentLongTaskId && tasks.length) {
      renderLongTask(tasks[tasks.length - 1]);
    }
    if (!longElements.longWorkspace.hidden && selectedLongSegmentId) {
      await selectLongSegment(selectedLongSegmentId, false);
    }
    if (showFeedback) showToast("长视频项目已载入。", false);
  } catch (error) {
    if (!quietMissing) showToast(error.message, true);
  }
}

async function saveLongSegment(card, segment) {
  const changes = {};
  for (const input of card.querySelectorAll("[data-segment-field]")) {
    const name = input.dataset.segmentField;
    if (name === "dialogue") changes[name] = textToDialogue(input.value);
    else if (["visible_text", "present_characters"].includes(name)) {
      changes[name] = input.value.replace(/\r\n/g, "\n").split(/\n|,/).map((item) => item.trim()).filter(Boolean);
    } else changes[name] = input.value;
  }
  changes.beats = Array.from(card.querySelectorAll(".segment-beat-row")).map((row) => ({
    start_seconds: row.querySelector("[data-beat-start]").value,
    end_seconds: row.querySelector("[data-beat-end]").value,
    action: row.querySelector("[data-beat-action]").value,
  }));
  try {
    const result = await postJson(
      `/api/long/projects/${encodeURIComponent(currentLongProject.id)}/segments/${encodeURIComponent(segment.id)}`,
      { changes }
    );
    renderLongProject(result.project, result.receipt?.readiness);
    showToast(`第 ${segment.index} 段已保存；该段及后续视频已标记为待重跑。`);
  } catch (error) {
    showToast(error.message, true);
  }
}

function openRegenerationDialog(editedIndex) {
  regenerateEditedIndex = editedIndex;
  longElements.regenerateTitle.textContent = `从第 ${editedIndex + 1} 段开始重生成`;
  longElements.regenerateChoices.replaceChildren();
  for (const segment of currentLongProject.segments.slice(editedIndex)) {
    const row = createElement("div", "regenerate-choice");
    const label = createElement("span", "", `第 ${segment.index} 段 · ${segment.summary || "无摘要"}`);
    const select = createElement("select");
    select.dataset.segmentId = segment.id;
    for (const [value, text] of [["rewrite", "重写"], ["keep", "保留为锚点"]]) {
      const option = createElement("option", "", text);
      option.value = value;
      select.appendChild(option);
    }
    select.value = segment.provenance === "manual" ? "keep" : "rewrite";
    row.append(label, select);
    longElements.regenerateChoices.appendChild(row);
  }
  document.querySelector('input[name="durationPolicy"][value="fixed"]').checked = true;
  longElements.regenerateCount.value = String(currentLongProject.segments.length);
  longElements.regenerateCount.min = String(editedIndex + 1);
  longElements.regenerateDialog.showModal();
}

async function confirmRegeneration() {
  const keepIds = Array.from(longElements.regenerateChoices.querySelectorAll("select"))
    .filter((item) => item.value === "keep")
    .map((item) => item.dataset.segmentId);
  const policy = document.querySelector('input[name="durationPolicy"]:checked')?.value || "fixed";
  const payload = {
    edited_index: regenerateEditedIndex,
    keep_segment_ids: keepIds,
    duration_policy: policy,
    new_segment_count: policy === "replan" ? Number.parseInt(longElements.regenerateCount.value, 10) : null,
  };
  try {
    await ensureLMStudioSession();
    const result = await postJson(
      `/api/long/projects/${encodeURIComponent(currentLongProject.id)}/regenerate`, payload
    );
    longElements.regenerateDialog.close();
    await beginLongTask(result);
  } catch (error) {
    showToast(error.message, true);
  }
}

async function startLongRender() {
  if (!currentLongProject || longTaskIsActive()) return;
  const confirmed = window.confirm(
    `将通过 ${activeComfyLabel()} 串行生成每一段，可能运行很久。不会清空或中断其他队列。确认开始吗？`
  );
  if (!confirmed) return;
  try {
    const result = await postJson(
      `/api/long/projects/${encodeURIComponent(currentLongProject.id)}/render`,
      {}
    );
    await beginLongTask(result);
  } catch (error) {
    showToast(error.message, true);
  }
}

async function stopLongTask() {
  if (!currentLongTaskId) return;
  try {
    const result = await postJson(`/api/long/tasks/${encodeURIComponent(currentLongTaskId)}/stop`, {});
    renderLongTask(result.task);
    showToast("已请求暂停；当前 API 调用或 GPU 段会正常完成。", false);
  } catch (error) {
    showToast(error.message, true);
  }
}

async function checkComfy() {
  longElements.comfyStatus.className = "comfy-status neutral";
  longElements.comfyStatus.textContent = `正在只读检查 ${activeComfyLabel()}…`;
  try {
    const result = await jsonRequest("/api/comfy/status");
    longElements.comfyStatus.className = "comfy-status ready";
    longElements.comfyStatus.textContent =
      `${result.base_url || activeComfyLabel()} · 节点/模型/${result.media_backend || "媒体后端"} 齐全 · 运行 ${result.queue_running} · 排队 ${result.queue_pending}`;
  } catch (error) {
    longElements.comfyStatus.className = "comfy-status failed";
    longElements.comfyStatus.textContent = error.message;
  }
}

async function restoreLongRevision() {
  if (!currentLongProject) return;
  const revision = Number.parseInt(longElements.longRevisionSelect.value, 10);
  if (!revision) {
    showToast("请先选择 Revision。", true);
    return;
  }
  if (!window.confirm(`恢复到 Revision ${revision}？当前状态会自动保存为可恢复快照。`)) return;
  try {
    const result = await postJson(
      `/api/long/projects/${encodeURIComponent(currentLongProject.id)}/restore`, { revision }
    );
    renderLongProject(result.project, result.readiness);
    showToast(`已恢复 Revision ${revision}。`);
  } catch (error) {
    showToast(error.message, true);
  }
}

function initializeLongWorkspace() {
  restoreLongForm();
  document.querySelectorAll("[data-long-persist]").forEach((input) => {
    input.addEventListener("input", persistLongForm);
    input.addEventListener("change", persistLongForm);
  });
  longElements.shortTabBtn.addEventListener("click", () => activateWorkspace("short"));
  longElements.longTabBtn.addEventListener("click", () => activateWorkspace("long"));
  longElements.createLongProjectBtn.addEventListener("click", createLongProject);
  longElements.refreshLongProjectsBtn.addEventListener("click", () => loadLongProjects(true));
  longElements.longProjectSelect.addEventListener("change", () => loadLongProject(longElements.longProjectSelect.value, true));
  longElements.checkComfyBtn.addEventListener("click", checkComfy);
  longElements.generateAllLongPromptsBtn.addEventListener("click", generateAllLongPrompts);
  longElements.enterAuthoringBtn.addEventListener("click", () => ensureLMStudioSession().catch((error) => showToast(error.message, true)));
  longElements.pauseAuthoringBtn.addEventListener("click", () => releaseLMStudioSession("pause"));
  longElements.confirmAuthoringBtn.addEventListener("click", () => releaseLMStudioSession("confirm"));
  longElements.copyLongStoryBtn.addEventListener("click", () => copyText(
    buildLongStoryOutput(currentLongProject),
    "完整长剧本已复制。"
  ));
  longElements.copyAllLongPromptsBtn.addEventListener("click", () => copyText(
    buildAllLongPromptsOutput(currentLongProject),
    "全部已生成的 H3 提示词已复制。"
  ));
  longElements.downloadAllLongPromptsBtn.addEventListener("click", () => downloadText(
    buildAllLongPromptsOutput(currentLongProject),
    `${currentLongProject?.id || "MiniMax_H3_long_project"}_all_H3_prompts.txt`
  ));
  longElements.saveLongWorkspaceBtn.addEventListener("click", () => saveBoundLongWorkspace(true));
  longElements.startLongRenderBtn.addEventListener("click", startLongRender);
  longElements.stopLongTaskBtn.addEventListener("click", stopLongTask);
  longElements.confirmRegenerateBtn.addEventListener("click", confirmRegeneration);
  longElements.restoreRevisionBtn.addEventListener("click", restoreLongRevision);
  renderLongTask(null);
  loadLongProjects(false);
}

const contextElements = Object.fromEntries(
  [
    "contextTabBtn", "contextWorkspace", "contextPluginStatus", "contextProjectSelect",
    "contextBaseSeed", "contextGenerateFrom",
    "contextRefreshBtn", "contextGenerateBtn", "contextTaskPanel", "contextTaskTitle",
    "contextTaskMessage", "contextProgressBar", "contextLiveOutput", "contextPlanPanel",
    "contextPlanTitle", "contextPlanMeta", "contextWarnings", "contextScenePrompts",
    "contextCopyAllBtn", "contextDownloadSpec", "contextDownloadPlan",
    "contextDownloadWorkflow", "contextDownloadApi", "contextRawJson",
    "contextFormatJsonBtn", "contextSaveJsonBtn", "contextUpscale1080",
    "contextRenderFrom", "contextOutputPaths", "contextStopBtn", "contextRenderBtn",
  ].map((id) => [id, document.getElementById(id)])
);

let currentContextProject = null;
let currentContextReadiness = null;
let currentContextSpec = null;
let currentContextTask = null;
let currentContextTaskId = "";
let contextTaskPollTimer = null;
let contextPluginInstalled = false;

function contextTaskIsActive() {
  return Boolean(currentContextTask && ["queued", "running", "retrying"].includes(currentContextTask.state));
}

function populateContextSceneSelect(select, count, selected, labelSuffix = "") {
  select.replaceChildren();
  for (let index = 1; index <= Math.max(1, count); index += 1) {
    const option = createElement("option", "", `第 ${index} 段${index === 1 ? "（完整开始）" : labelSuffix}`);
    option.value = String(index);
    select.appendChild(option);
  }
  select.value = String(Math.max(1, Math.min(Math.max(1, count), Number(selected || 1))));
}

function contextAllPromptsText(spec) {
  return (spec?.scenes || []).map((scene, offset) => (
    `===== 第 ${offset + 1} 段 · ${scene.id} · ${Number(scene.actual_seconds || 0).toFixed(3)}s · raw ${scene.raw_frames} / delivered ${scene.delivered_frames} =====\n${scene.prompt || ""}`
  )).join("\n\n");
}

function renderContextPlan(spec) {
  currentContextSpec = spec?.exists === false ? null : spec;
  if (!currentContextSpec) {
    contextElements.contextPlanPanel.hidden = true;
    populateContextSceneSelect(
      contextElements.contextGenerateFrom,
      currentContextProject?.segments?.length || 1,
      1,
      ""
    );
    setContextControls();
    return;
  }
  const value = currentContextSpec;
  contextElements.contextPlanPanel.hidden = false;
  contextElements.contextPlanTitle.textContent = `${currentContextProject?.title || value.project_id} · 只读规则计划`;
  contextElements.contextPlanMeta.textContent =
    `${value.scenes?.length || 0} 段 · ${Number(value.actual_seconds || 0).toFixed(3)} 秒 · `
    + `${value.total_delivered_frames || 0} 帧 @ 24fps · revision ${value.revision} · ${value.status} · API 调用 0`;
  const warnings = [...(value.warnings || [])];
  if (value.stale) warnings.unshift(`源项目从第 ${value.stale_from || 1} 段起已变化：请重新生成规则工作流。`);
  if (value.last_error?.message) warnings.push(value.last_error.message);
  if (value.render?.last_error?.message) warnings.push(value.render.last_error.message);
  contextElements.contextWarnings.hidden = warnings.length === 0;
  contextElements.contextWarnings.textContent = warnings.join("\n");

  contextElements.contextScenePrompts.replaceChildren();
  for (const [offset, scene] of (value.scenes || []).entries()) {
    const details = createElement("details", "context-scene-prompt");
    details.open = offset === 0;
    const summary = createElement(
      "summary",
      "",
      `第 ${offset + 1} 段 · ${scene.id} · ${scene.mode || "?"} · ${Number(scene.actual_seconds).toFixed(3)}s · raw ${scene.raw_frames} / exact ${scene.target_frames || scene.delivered_frames}`
    );
    const actions = createElement("div", "context-prompt-actions");
    const copy = createElement("button", "button ghost compact-button", "复制本段完整 prompt");
    copy.type = "button";
    copy.addEventListener("click", () => copyText(scene.prompt || "", `第 ${offset + 1} 段提示词已复制。`));
    const download = createElement("button", "button ghost compact-button", "下载 TXT");
    download.type = "button";
    download.addEventListener("click", () => downloadText(
      scene.prompt || "",
      `${value.project_id}_context_scene_${String(offset + 1).padStart(4, "0")}.txt`
    ));
    actions.append(copy, download);
    const output = createElement("pre");
    output.textContent = scene.prompt || "";
    details.append(summary, actions, output);
    contextElements.contextScenePrompts.appendChild(details);
  }
  contextElements.contextRawJson.value = JSON.stringify(value, null, 2);
  contextElements.contextUpscale1080.checked = Boolean(value.outputs?.upscale_1080);
  const generationStart = value.render?.start_scene || 1;
  populateContextSceneSelect(contextElements.contextGenerateFrom, value.scenes.length, generationStart, "起恢复");
  populateContextSceneSelect(
    contextElements.contextRenderFrom,
    value.scenes.length,
    value.render?.start_scene || 1,
    "起恢复"
  );
  const base = `/api/long/projects/${encodeURIComponent(value.project_id)}/context-loop/artifacts/`;
  contextElements.contextDownloadSpec.href = base + "spec";
  contextElements.contextDownloadPlan.href = base + "plan";
  contextElements.contextDownloadWorkflow.href = base + "workflow";
  contextElements.contextDownloadApi.href = base + "api_prompt";
  contextElements.contextOutputPaths.replaceChildren();
  const paths = [
    ["原生：", value.render?.native_path],
    ["1080p：", value.render?.upscaled_path],
  ].filter(([, path]) => Boolean(path));
  if (!paths.length) {
    contextElements.contextOutputPaths.textContent = "尚未生成视频。";
  } else {
    for (const [label, path] of paths) {
      const row = createElement("div");
      row.append(createElement("strong", "", label), document.createTextNode(String(path)));
      contextElements.contextOutputPaths.appendChild(row);
    }
  }
  setContextControls();
}

function setContextControls() {
  const active = contextTaskIsActive();
  const hasProject = Boolean(currentContextProject);
  const valid = Boolean(currentContextSpec && currentContextSpec.status === "valid" && !currentContextSpec.stale);
  contextElements.contextProjectSelect.disabled = active;
  contextElements.contextGenerateBtn.disabled = active || !hasProject;
  contextElements.contextRefreshBtn.disabled = active;
  contextElements.contextSaveJsonBtn.disabled = true;
  contextElements.contextFormatJsonBtn.disabled = active || !currentContextSpec;
  contextElements.contextStopBtn.disabled = !active;
  const authoringConfirmed = Boolean(currentContextReadiness?.authoring_confirmation?.current);
  const modelUnloaded = Boolean(lmStudioState && !lmStudioState.model?.owned_loaded);
  contextElements.contextRenderBtn.disabled = active || !valid || !contextPluginInstalled
    || !authoringConfirmed || !modelUnloaded;
  contextElements.contextRenderBtn.title = !authoringConfirmed
    ? "请先在长视频项目中确认剧本与全部 H3 提示词。"
    : !modelUnloaded ? "本地模型仍占用显存，请先释放。" : "";
  contextElements.contextRenderFrom.disabled = active || !valid;
  contextElements.contextUpscale1080.disabled = active;
}

function renderContextTask(task) {
  currentContextTask = task || null;
  contextElements.contextTaskPanel.className = `long-task-panel ${task?.state || "neutral"}`;
  if (!task) {
    contextElements.contextTaskTitle.textContent = "尚无规则任务";
    contextElements.contextTaskMessage.textContent = "先选择一个长剧本项目。";
    contextElements.contextProgressBar.style.width = "0%";
    contextElements.contextLiveOutput.textContent = "等待任务…";
    setContextControls();
    return;
  }
  const labels = { context_build: "规则工作流编译", context_render: "规则循环视频" };
  contextElements.contextTaskTitle.textContent = `${labels[task.kind] || task.kind} · ${task.state}`;
  contextElements.contextTaskMessage.textContent = task.message || task.stage || "处理中…";
  const total = Number(task.total || 0);
  const current = Number(task.current || 0);
  contextElements.contextProgressBar.style.width = `${total > 0 ? Math.min(100, Math.max(0, current / total * 100)) : 0}%`;
  contextElements.contextLiveOutput.textContent = task.live_text || task.error?.message || "等待后台输出…";
  setContextControls();
}

async function loadContextPluginStatus() {
  try {
    const status = await jsonRequest("/api/context-loop/plugin-status");
    contextPluginInstalled = Boolean(status.installed);
    contextElements.contextPluginStatus.className = `comfy-status ${status.installed ? "ready" : "failed"}`;
    contextElements.contextPluginStatus.textContent = status.installed
      ? `Context Loop v${status.version} + Rule Adapter 已安装 · ${String(status.commit || "").slice(0, 8)}`
      : status.conflict
        ? "检测到同名但非项目固定版本的节点目录；安装器不会覆盖，请先人工核对"
        : "插件尚未安装：运行 install_context_loop_node.bat 后重启 ComfyUI";
  } catch (error) {
    contextPluginInstalled = false;
    contextElements.contextPluginStatus.className = "comfy-status failed";
    contextElements.contextPluginStatus.textContent = error.message || "插件状态读取失败";
  }
  setContextControls();
}

async function loadContextProjects(showFeedback = false) {
  try {
    const result = await jsonRequest("/api/long/projects");
    const selected = currentContextProject?.id || contextElements.contextProjectSelect.value || currentLongProject?.id;
    contextElements.contextProjectSelect.replaceChildren();
    const placeholder = createElement("option", "", result.projects?.length ? "请选择已保存长剧本" : "暂无长剧本项目");
    placeholder.value = "";
    contextElements.contextProjectSelect.appendChild(placeholder);
    for (const project of result.projects || []) {
      const option = createElement("option", "", `${project.title} · ${project.actual_seconds.toFixed(2)}s · ${project.segment_count} 段`);
      option.value = project.id;
      contextElements.contextProjectSelect.appendChild(option);
    }
    const target = (result.projects || []).some((item) => item.id === selected)
      ? selected
      : result.projects?.[0]?.id || "";
    contextElements.contextProjectSelect.value = target;
    if (target) await loadContextProject(target, false);
    if (showFeedback) showToast(`规则工作流已刷新 ${result.projects?.length || 0} 个项目。`);
  } catch (error) {
    if (showFeedback) showToast(error.message, true);
  }
}

async function loadContextProject(projectId, showFeedback = false) {
  if (!projectId) {
    currentContextProject = null;
    currentContextReadiness = null;
    currentContextSpec = null;
    renderContextPlan(null);
    return;
  }
  try {
    const [projectResult, contextResult] = await Promise.all([
      jsonRequest(`/api/long/projects/${encodeURIComponent(projectId)}`),
      jsonRequest(`/api/long/projects/${encodeURIComponent(projectId)}/context-loop`),
    ]);
    currentContextProject = projectResult.project;
    currentContextReadiness = projectResult.readiness || null;
    const spec = contextResult.context_loop?.exists ? contextResult.context_loop : null;
    renderContextPlan(spec);
    const tasks = contextResult.tasks || [];
    const active = [...tasks].reverse().find((item) => ["queued", "running", "retrying"].includes(item.state));
    if (active && active.id !== currentContextTaskId) {
      renderContextTask(active);
      scheduleContextTaskPoll(active.id);
    } else if (!active && !currentContextTaskId && tasks.length) {
      renderContextTask(tasks[tasks.length - 1]);
    }
    if (showFeedback) showToast("规则工作流项目已载入。", false);
  } catch (error) {
    showToast(error.message || "规则工作流项目载入失败。", true);
  }
}

async function generateContextPlan() {
  if (!currentContextProject || contextTaskIsActive()) return;
  try {
    const startScene = Number.parseInt(contextElements.contextGenerateFrom.value || "1", 10);
    const seedText = contextElements.contextBaseSeed.value.trim();
    const result = await postJson(
      `/api/long/projects/${encodeURIComponent(currentContextProject.id)}/context-loop/generate`,
      {
        start_scene: startScene,
        base_seed: seedText || null,
        upscale_1080: contextElements.contextUpscale1080.checked,
      }
    );
    beginContextTask(result);
    showToast("正在离线编译规则工作流；不会调用 API，也不会启动 GPU。", false);
  } catch (error) {
    showToast(error.message || "规则工作流编译失败。", true);
  }
}


async function startContextRender() {
  if (!currentContextSpec || contextTaskIsActive()) return;
  const rawUpscale = Boolean(currentContextSpec.outputs?.upscale_1080);
  if (rawUpscale !== contextElements.contextUpscale1080.checked) {
    showToast("1080p 选项已变化，请先重新点击“生成规则工作流”。", true);
    return;
  }
  const startScene = Number.parseInt(contextElements.contextRenderFrom.value || "1", 10);
  const upscaleText = contextElements.contextUpscale1080.checked
    ? "原生链完成后还会逐段运行 RealESRGAN，并生成 1080×1920 总片。"
    : "只生成并保存 768×1344 原生总片。";
  if (!window.confirm(
    `将等待 ${activeComfyLabel()} 空闲，然后从第 ${startScene} 段连续运行到结尾。${upscaleText}\n\n不会清空或中断现有队列；任务开始后可以离开电脑。确认开始吗？`
  )) return;
  try {
    const result = await postJson(
      `/api/long/projects/${encodeURIComponent(currentContextProject.id)}/context-loop/render`,
      { start_scene: startScene }
    );
    beginContextTask(result);
  } catch (error) {
    showToast(error.message || "规则循环提交失败。", true);
  }
}

async function stopContextTask() {
  if (!currentContextTaskId) return;
  try {
    const result = await postJson(`/api/long/tasks/${encodeURIComponent(currentContextTaskId)}/stop`, {});
    renderContextTask(result.task);
    showToast("已请求在下一次 GPU 提交前暂停；正在执行的 ComfyUI 任务不会被打断。", false);
  } catch (error) {
    showToast(error.message, true);
  }
}

function scheduleContextTaskPoll(taskId) {
  currentContextTaskId = taskId;
  clearTimeout(contextTaskPollTimer);
  const poll = async () => {
    try {
      const result = await jsonRequest(`/api/long/tasks/${encodeURIComponent(taskId)}`);
      renderContextTask(result.task);
      if (["queued", "running", "retrying"].includes(result.task.state)) {
        if (result.task.project_id) await loadContextProject(result.task.project_id, false);
        contextTaskPollTimer = setTimeout(poll, 1400);
        return;
      }
      currentContextTaskId = "";
      if (result.task.project_id) await loadContextProject(result.task.project_id, false);
      showToast(
        result.task.message || (result.task.state === "completed" ? "规则任务已完成。" : "规则任务已停止。"),
        result.task.state === "failed"
      );
    } catch (error) {
      currentContextTaskId = "";
      renderContextTask({ kind: "context", state: "failed", message: error.message, error: { message: error.message } });
      showToast(error.message, true);
    }
  };
  poll();
}

function beginContextTask(result) {
  const task = result?.task;
  if (!task?.id) throw new Error("本地服务没有返回规则任务 ID。");
  renderContextTask(task);
  scheduleContextTaskPoll(task.id);
}

async function refreshContextWorkspace() {
  await loadContextPluginStatus();
  await loadContextProjects(true);
}

function initializeContextWorkspace() {
  contextElements.contextTabBtn.addEventListener("click", () => activateWorkspace("context"));
  contextElements.contextProjectSelect.addEventListener("change", () => (
    loadContextProject(contextElements.contextProjectSelect.value, true)
  ));
  contextElements.contextRefreshBtn.addEventListener("click", refreshContextWorkspace);
  contextElements.contextGenerateBtn.addEventListener("click", generateContextPlan);
  contextElements.contextCopyAllBtn.addEventListener("click", () => copyText(
    contextAllPromptsText(currentContextSpec), "全部规则工作流 H3 提示词已复制。"
  ));
  contextElements.contextFormatJsonBtn.addEventListener("click", () => {
    try {
      contextElements.contextRawJson.value = JSON.stringify(JSON.parse(contextElements.contextRawJson.value), null, 2);
    } catch (_error) {
      showToast("规则 JSON 语法无效，无法格式化。", true);
    }
  });
  contextElements.contextRenderBtn.addEventListener("click", startContextRender);
  contextElements.contextStopBtn.addEventListener("click", stopContextTask);
  renderContextTask(null);
  loadContextPluginStatus();
  loadContextProjects(false);
}

initialize();
