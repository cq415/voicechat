/* ============ VoiceChat 前端逻辑 ============ */
const $ = (s) => document.querySelector(s);
const state = {
  user: null,
  models: [], keyed: [],
  modelId: localStorage.getItem("vc_model") || "demo",
  convId: null,          // 当前会话
  sending: false,
  ttsOn: localStorage.getItem("vc_tts") === "1",
  recog: null, recording: false
};

async function api(path, method = "GET", body) {
  const r = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined
  });
  return r.json();
}
const esc = (s) => s.replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* ============ 认证 ============ */
async function doAuth(path) {
  const username = $("#authUser").value.trim(), password = $("#authPass").value;
  const r = await api(path, "POST", { username, password });
  if (r.error) { $("#authErr").textContent = r.error; return; }
  state.user = r.username;
  enterMain();
}
$("#loginBtn").onclick = () => doAuth("/api/login");
$("#registerBtn").onclick = () => doAuth("/api/register");
$("#authPass").onkeydown = (e) => { if (e.key === "Enter") doAuth("/api/login"); };
$("#logoutBtn").onclick = async () => { await api("/api/logout", "POST"); location.reload(); };

/* ============ 主界面 ============ */
async function enterMain() {
  $("#authView").classList.add("hidden");
  $("#mainView").classList.remove("hidden");
  $("#meName").textContent = "👤 " + state.user;
  await loadModels();
  await loadConvs();
  renderEmpty();
}
function renderEmpty() {
  $("#messages").innerHTML = `<div class="empty-tip"><div class="big">🎙️</div>
    <p>选择模型后开始对话，或点击麦克风直接说话</p>
    <p class="mini">提示：「本地演示模型」无需 API Key 即可体验完整流程</p></div>`;
}

async function loadModels() {
  const r = await api("/api/models");
  state.models = r.models; state.keyed = r.keyed;
  const sel = $("#modelSelect");
  sel.innerHTML = state.models.map(m => {
    const needKey = m.provider !== "demo";
    const hasKey = state.keyed.includes(m.provider);
    const label = m.name + (needKey ? (hasKey ? " 🔑✓" : " 🔑") : "");
    return `<option value="${m.id}">${label}</option>`;
  }).join("");
  sel.value = state.modelId;
}
$("#modelSelect").onchange = () => {
  const m = state.models.find(x => x.id === $("#modelSelect").value);
  if (!m) return;
  state.modelId = m.id;
  localStorage.setItem("vc_model", state.modelId);
  if (m.provider !== "demo" && !state.keyed.includes(m.provider)) openSettings();
};

/* ---------- 模型设置弹窗 ---------- */
function openSettings() {
  const m = state.models.find(x => x.id === state.modelId) || state.models[0];
  if (!m) return;
  const tips = {
    demo: "（本地演示，无需配置）",
    custom: "（需要接口地址、模型名和 API Key）",
    doubao: "（需火山方舟 API Key；模型名支持 ep-xxxx 接入点 ID 或 doubao-xxx 模型名，可在设置中修改）"
  };
  $("#settingsHint").textContent = "当前模型：" + m.name + (tips[m.provider] || "（需要 API Key）");
  $("#keyField").classList.toggle("hidden", m.provider === "demo");
  $("#customFields").classList.toggle("hidden", m.provider !== "custom");
  $("#customBase").value = localStorage.getItem("vc_custom_base") || "";
  $("#customModel").value = localStorage.getItem("vc_custom_model") || "";
  $("#apiKeyInput").value = "";
  $("#settingsModal").classList.remove("hidden");
}
$("#settingsBtn").onclick = openSettings;
$("#settingsCancel").onclick = () => $("#settingsModal").classList.add("hidden");
$("#settingsSave").onclick = async () => {
  const m = state.models.find(x => x.id === state.modelId);
  if (m.provider === "custom") {
    localStorage.setItem("vc_custom_base", $("#customBase").value.trim());
    localStorage.setItem("vc_custom_model", $("#customModel").value.trim());
  }
  if (m.provider !== "demo") {
    const key = $("#apiKeyInput").value.trim();
    if (key) {
      const r = await api("/api/apikey", "POST", { provider: m.provider, apikey: key });
      if (r.error) { alert(r.error); return; }
    }
  }
  $("#settingsModal").classList.add("hidden");
  await loadModels();
};

/* ---------- 会话列表 ---------- */
async function loadConvs() {
  const r = await api("/api/conversations");
  const list = $("#convList");
  list.innerHTML = "";
  (r.conversations || []).forEach(c => {
    const el = document.createElement("div");
    el.className = "conv-item" + (c.id === state.convId ? " active" : "");
    el.innerHTML = `<div style="overflow:hidden;flex:1"><div class="t">${esc(c.title)}</div><div class="m">${esc(c.model || "")}</div></div><span class="del">✕</span>`;
    el.onclick = () => openConv(c.id);
    el.querySelector(".del").onclick = async (e) => {
      e.stopPropagation();
      await api(`/api/conversation/${c.id}/delete`, "POST");
      if (state.convId === c.id) { state.convId = null; renderEmpty(); }
      loadConvs();
    };
    list.appendChild(el);
  });
}
async function openConv(cid) {
  const c = await api("/api/conversation/" + cid);
  if (c.error) return;
  state.convId = c.id;
  const box = $("#messages");
  box.innerHTML = "";
  c.messages.forEach(m => appendMsg(m.role, m.content));
  scrollBottom();
  loadConvs();
}
$("#newChatBtn").onclick = () => { state.convId = null; renderEmpty(); loadConvs(); };

/* ---------- 消息渲染 ---------- */
function appendMsg(role, content, cls = "") {
  const box = $("#messages");
  box.querySelector(".empty-tip")?.remove();
  const el = document.createElement("div");
  el.className = `msg ${role} ${cls}`;
  el.innerHTML = esc(content) + `<div class="meta">${role === "user" ? esc(state.user) : "AI"} · ${new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}</div>`;
  box.appendChild(el);
  scrollBottom();
  return el;
}
const scrollBottom = () => { $("#messages").scrollTop = $("#messages").scrollHeight; };

/* ---------- 发送 + SSE 流式接收 ---------- */
async function send(text) {
  text = (text || $("#msgInput").value).trim();
  if (!text || state.sending) return;
  const m = state.models.find(x => x.id === state.modelId) || state.models[0];
  if (!m) return;
  if (m.provider !== "demo" && !state.keyed.includes(m.provider)) { openSettings(); return; }

  state.sending = true;
  $("#sendBtn").disabled = true;
  $("#msgInput").value = "";
  appendMsg("user", text);
  const bubble = appendMsg("assistant", "", "streaming");

  try {
    const payload = { message: text, model_id: state.modelId, conv_id: state.convId || "" };
    if (state.modelId === "custom") {
      payload.custom = { base_url: localStorage.getItem("vc_custom_base") || "", model: localStorage.getItem("vc_custom_model") || "" };
    }
    const r = await fetch("/api/chat", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload)
    });
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      bubble.classList.remove("streaming"); bubble.classList.add("error");
      bubble.innerHTML = esc(j.error || ("请求失败 " + r.status));
      return;
    }
    const reader = r.body.getReader();
    const decoder = new TextDecoder();
    let buf = "", full = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const lines = buf.split("\n");
      buf = lines.pop();
      for (const line of lines) {
        if (!line.startsWith("data:")) continue;
        const data = line.slice(5).trim();
        if (data === "[DONE]") continue;
        try {
          const j = JSON.parse(data);
          if (j.conv_id) state.convId = j.conv_id;
          if (j.error) { bubble.classList.add("error"); bubble.innerHTML = esc(j.error); full = null; break; }
          if (j.delta) {
            full += j.delta;
            bubble.innerHTML = esc(full);
            scrollBottom();
          }
        } catch (e) { /* 忽略解析异常 */ }
      }
      if (full === null) break;
    }
    bubble.classList.remove("streaming");
    if (full) {
      bubble.innerHTML = esc(full) + `<div class="meta">AI · ${new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}</div>`;
      if (state.ttsOn) speakText(full);
    }
    scrollBottom();
    loadConvs();
  } catch (e) {
    bubble.classList.remove("streaming"); bubble.classList.add("error");
    bubble.innerHTML = "网络错误：" + esc(String(e));
  } finally {
    state.sending = false;
    $("#sendBtn").disabled = false;
    $("#msgInput").focus();
  }
}
$("#sendBtn").onclick = () => send();
$("#msgInput").onkeydown = (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } };
$("#msgInput").oninput = function () { this.style.height = "auto"; this.style.height = Math.min(this.scrollHeight, 140) + "px"; };

/* ---------- 语音朗读 (TTS) ---------- */
function speakText(text) {
  if (!("speechSynthesis" in window)) return;
  speechSynthesis.cancel();
  const u = new SpeechSynthesisUtterance(text);
  u.lang = "zh-CN";
  u.rate = 1.05;
  speechSynthesis.speak(u);
}
function updateTtsBtn() { $("#ttsBtn").textContent = state.ttsOn ? "🔊 朗读开" : "🔇 朗读关"; }
$("#ttsBtn").onclick = () => {
  state.ttsOn = !state.ttsOn;
  localStorage.setItem("vc_tts", state.ttsOn ? "1" : "0");
  if (!state.ttsOn && "speechSynthesis" in window) speechSynthesis.cancel();
  updateTtsBtn();
};
updateTtsBtn();

/* ---------- 语音输入 (ASR) ---------- */
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
if (!SR) $("#micBtn").style.display = "none";
$("#micBtn").onclick = () => {
  if (state.recording) { state.recog && state.recog.stop(); return; }
  const rec = new SR();
  state.recog = rec;
  rec.lang = "zh-CN";
  rec.interimResults = true;
  rec.continuous = false;
  rec.onstart = () => { state.recording = true; $("#micBtn").classList.add("recording"); $("#msgInput").placeholder = "正在聆听，请说话…"; };
  rec.onresult = (e) => {
    let txt = "";
    for (let i = 0; i < e.results.length; i++) txt += e.results[i][0].transcript;
    $("#msgInput").value = txt;
    if (e.results[e.results.length - 1].isFinal) setTimeout(() => send(txt), 300);
  };
  rec.onerror = () => { $("#msgInput").placeholder = "语音识别失败，请检查麦克风权限"; };
  rec.onend = () => {
    state.recording = false;
    $("#micBtn").classList.remove("recording");
    $("#msgInput").placeholder = "输入消息，或点击麦克风语音输入… (Enter 发送，Shift+Enter 换行)";
  };
  rec.start();
};

/* ============ 启动 ============ */
(async function init() {
  const me = await api("/api/me");
  if (me.username) { state.user = me.username; enterMain(); }
  else $("#authView").classList.remove("hidden");
})();
