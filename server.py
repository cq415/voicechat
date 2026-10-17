# -*- coding: utf-8 -*-
"""
VoiceChat —— 支持语音对话的智能聊天机器人（Flask 后端服务）

================ 功能总览 ================
  1. Python + Flask 后端，SQLite 做账号 / API Key 持久化
  2. OpenAI 兼容协议接入，可在前端下拉框切换不同 AI 大模型
     （豆包 / DeepSeek / Kimi / 通义千问 / OpenAI / 自定义 OpenAI 兼容接口 / 本地演示模型）
  3. 服务端按会话保存完整消息历史，每次请求带上全部消息 -> 实现多轮上下文；
     通过 SSE（Server-Sent Events）把模型增量内容实时推给前端
  4. 每次对话自动落盘为 data/history/<用户名>/<32位会话ID>.json，服务重启不丢失
  5. 用户注册 / 登录（Flask Session + 密码 SHA-256 加盐哈希）

================ 请求主流程 ================
  前端 POST /api/chat {message, model_id, conv_id}
    -> 校验登录 / 参数 / API Key
    -> 读取（或新建）会话 JSON，追加本轮用户消息
    -> 组装 system + 历史消息，调用上游大模型流式接口
    -> SSE 逐块回传：先回 conv_id，再持续回 delta，最后 [DONE]
    -> 流结束后把完整 AI 回复写回会话文件

================ 目录结构 ================
  server.py            本文件：全部后端逻辑
  config.json          可用模型清单（id / 名称 / base_url / model）
  static/              前端页面（index.html / style.css / app.js）
  data/users.db        SQLite：用户表 + 各用户 API Key 表
  data/history/<用户>/<会话>.json  聊天记录文件
"""
# ---- 标准库 ----
import hashlib   # 密码 SHA-256 哈希
import json      # 读写配置与聊天记录 JSON
import os        # 路径 / 目录 / 文件操作
import re        # 用户名与会话 ID 的正则校验
import secrets   # 生成 Flask session 随机密钥
import sqlite3   # 内置轻量数据库
import time      # 时间戳
import uuid      # 生成 32 位会话 ID

# ---- 第三方库（见 requirements.txt）----
import requests as http_requests            # 调用上游大模型 HTTP 接口
from flask import Flask, request, jsonify, Response, session
# Flask: Web 框架；request: 当前请求；jsonify: 返回 JSON；
# Response: 用于返回 SSE 流；session: 基于 Cookie 的登录态

# ---- 全局路径 ----
BASE_DIR = os.path.dirname(os.path.abspath(__file__))       # server.py 所在目录（绝对路径）
DATA_DIR = os.path.join(BASE_DIR, "data")                   # 所有运行期数据
HISTORY_DIR = os.path.join(DATA_DIR, "history")             # 聊天记录根目录
os.makedirs(HISTORY_DIR, exist_ok=True)                     # 目录不存在则创建

# ---- 读取模型配置 ----
# config.json 中每个模型形如：
# {"id":"doubao","name":"豆包...","provider":"doubao","base_url":"https://...","model":"doubao-xxx"}
with open(os.path.join(BASE_DIR, "config.json"), "r", encoding="utf-8") as f:
    CONFIG = json.load(f)
# 转成 {模型id: 模型配置} 的字典，便于按 model_id 快速查找
MODELS = {m["id"]: m for m in CONFIG["models"]}

# ---- Flask 应用 ----
app = Flask(__name__, static_folder="static", static_url_path="")
# 静态资源直接挂到根路径（/、/app.js 等），前后端同源，省去跨域处理
app.secret_key = secrets.token_hex(16)
# session 加密密钥：进程重启后旧登录态失效（演示用；生产环境应固定到环境变量）

# 用户名校验正则：2-20 位，允许字母/数字/下划线(\w) + 中文汉字
# 一(U+4E00) 到 龥(U+9FA5) 是 Unicode 中日韩统一表意文字常用区间的起止
USERNAME_RE = re.compile(r"^[\w一-龥]{2,20}$")


# ================= 数据库层 =================
# 使用 Python 内置 SQLite，数据文件为 data/users.db，无需额外安装数据库服务。
# 共两张表：
#   users  —— 用户账号（用户名唯一，密码只存哈希值）
#   apikeys —— 每个用户为各模型厂商(provider)保存的 API Key，按 (用户名,厂商) 唯一
def db():
    """打开一个 SQLite 连接。

    :return: sqlite3.Connection，行工厂设为 Row，
             之后可以用 row["列名"] 的方式取字段（像字典一样）。
    注意：SQLite 连接不能跨线程复用，因此每个请求内部现场开、用完即关。
    """
    conn = sqlite3.connect(os.path.join(DATA_DIR, "users.db"))
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """建表（幂等）。服务启动时调用一次，IF NOT EXISTS 保证重复执行不报错。"""
    conn = db()
    # 用户表：id 自增主键；username 唯一且非空；password 存加盐哈希；created 注册时间戳
    conn.execute("""CREATE TABLE IF NOT EXISTS users
                    (
                        id
                        INTEGER
                        PRIMARY
                        KEY
                        AUTOINCREMENT,
                        username
                        TEXT
                        UNIQUE
                        NOT
                        NULL,
                        password
                        TEXT
                        NOT
                        NULL,
                        created
                        REAL
                    )""")
    # API Key 表：联合主键 (username, provider)，
    # 即"一个用户对一家厂商只保存一个 Key"，重复保存时用 INSERT OR REPLACE 覆盖
    conn.execute("""CREATE TABLE IF NOT EXISTS apikeys
    (
        username
        TEXT
        NOT
        NULL,
        provider
        TEXT
        NOT
        NULL,
        apikey
        TEXT
        NOT
        NULL,
        PRIMARY
        KEY
                    (
        username,
        provider
                    ))""")
    conn.commit()
    conn.close()


def hash_pw(pw: str) -> str:
    """对明文密码做加盐 SHA-256 哈希。

    :param pw: 用户输入的明文密码
    :return: 64 位十六进制哈希字符串
    说明：固定盐值 "vc_salt_" 用于演示；生产环境建议每用户独立随机盐（可用 bcrypt/argon2）。
    """
    return hashlib.sha256(("vc_salt_" + pw).encode("utf-8")).hexdigest()


def current_user():
    """获取当前登录用户名。

    :return: 已登录返回用户名字符串；未登录（session 中无 user）返回 None
    所有需要登录的接口都会先调用它做鉴权。
    """
    return session.get("user")


# ================= 聊天记录文件层 =================
# 不把聊天内容放进数据库，而是每个会话一个 JSON 文件，方便备份、导出和人工查看。
# 文件结构示例：
# {
#   "id": "32位hex", "title": "首条消息前20字", "created": 时间戳, "updated": 时间戳,
#   "model_name": "豆包 Doubao",
#   "messages": [
#     {"role":"user",      "content":"你好", "time": ...},
#     {"role":"assistant","content":"你好！","time": ...}
#   ]
# }
def user_dir(user: str) -> str:
    """返回（并确保存在）某个用户的聊天记录目录。"""
    d = os.path.join(HISTORY_DIR, user)
    os.makedirs(d, exist_ok=True)
    return d


def conv_path(user: str, cid: str) -> str:
    """拼接会话文件完整路径：data/history/<用户名>/<会话id>.json"""
    return os.path.join(user_dir(user), cid + ".json")


def valid_cid(cid: str) -> bool:
    """校验会话 ID 是否为 32 位十六进制串。

    作用：防止前端传入 ../ 之类的路径片段造成目录穿越（任意文件读取/删除）。
    """
    return bool(cid) and re.fullmatch(r"[0-9a-f]{32}", cid or "")


def load_conv(user: str, cid: str):
    """读取一个会话的完整 JSON。文件不存在时返回 None。"""
    p = conv_path(user, cid)
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def save_conv(user: str, conv: dict):
    """把会话整体写回文件（覆盖写）。

    :param conv: 会话字典，必须含 id 字段
    ensure_ascii=False 让中文直接以 UTF-8 字符写入，而不是 \\uXXXX 转义，便于阅读；
    indent=2 美化缩进。每次写入前刷新 updated 时间戳，用于会话列表排序。
    """
    conv["updated"] = time.time()
    with open(conv_path(user, conv["id"]), "w", encoding="utf-8") as f:
        json.dump(conv, f, ensure_ascii=False, indent=2)


# ================= 大模型调用层 =================
# 豆包 / DeepSeek / Kimi / 通义千问 / OpenAI 都兼容 OpenAI 的 Chat Completions 协议，
# 差别仅在 base_url、model 名和 API Key，因此用同一个函数即可统一调用。
def stream_openai(base_url: str, api_key: str, model: str, messages: list):
    """调用 OpenAI 兼容的流式对话接口，生成器逐段产出内容。

    :param base_url: 厂商接口根地址，如 https://ark.cn-beijing.volces.com/api/v3
    :param api_key: 该厂商的 API Key
    :param model:    模型名或推理接入点 ID（豆包为 doubao-xxx 或 ep-xxx）
    :param messages: 消息列表 [{role, content}, ...]，含 system 与完整历史
    :yield: 二元组：
             ("delta", 增量文本) —— 正常的一段回复内容，可多次产出
             ("error", 错误信息) —— 网络 / HTTP / 解析出错时产出一次后结束
    """
    try:
        # stream=True：上游以 SSE 形式持续推送，而不是等整段回复生成完才返回
        with http_requests.post(
                base_url.rstrip("/") + "/chat/completions",
                headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
                json={"model": model, "messages": messages, "stream": True},
                stream=True, timeout=(10, 120),  # 连接超时 10s，读取超时 120s（等长回复）
        ) as r:
            if r.status_code != 200:
                # 非 200（如 401 Key 无效、404 模型名错误、429 限流）原样回传上游错误体
                yield ("error", "模型接口返回 %s：%s" % (r.status_code, r.content[:400].decode("utf-8", "replace")))
                return
            # 手动按字节累积、按 UTF-8 解码逐行处理。
            # 不能直接用 iter_lines(decode_unicode=True)：当响应头未声明 charset 时，
            # requests 会按 ISO-8859-1 解码，导致中文变成乱码（本项目实际踩过的坑）。
            buf = b""                       # 尚未凑成一整行的残留字节
            for chunk in r.iter_content(chunk_size=None):
                buf += chunk
                while b"\n" in buf:        # 缓冲区里有完整行就逐行取出
                    raw, buf = buf.split(b"\n", 1)
                    line = raw.decode("utf-8", "replace").strip()
                    if not line or not line.startswith("data:"):
                        continue            # SSE 心跳行 / 空行，跳过
                    data = line[5:].strip()  # 去掉 "data:" 前缀
                    if data == "[DONE]":     # 上游约定的结束标记
                        return
                    try:
                        # 每行是一个 JSON：choices[0].delta.content 为新增的一小段文本
                        j = json.loads(data)
                        delta = j["choices"][0].get("delta", {}).get("content")
                        if delta:
                            yield ("delta", delta)
                    except Exception:
                        # 个别行（如心跳、角色标记行）可能解析不到 content，忽略即可
                        continue
    except Exception as e:
        # 连接失败 / 超时等网络层异常
        yield ("error", "调用模型接口失败：%s" % e)


def stream_demo(messages: list):
    """本地演示模型：不需要任何 API Key，纯本地伪造一段流式回复。

    用途：在没有申请大模型 Key 时也能完整体验"注册→对话→流式输出→记录落盘"全链路。
    :param messages: 当前会话的完整消息列表
    :yield: ("delta", 单字)，每字间隔 15ms 模拟打字机效果
    """
    user_msg = messages[-1]["content"] if messages else ""   # 最后一条即本轮用户输入
    turn = sum(1 for m in messages if m["role"] == "user")   # 用户消息条数 = 这是第几轮
    text = ("【本地演示模式】我是内置的演示模型，无需 API Key。\n"
            "这是你本轮对话的第 %d 条消息，我记得我们的全部上下文。\n"
            "你刚才说：「%s」\n"
            "想接入真正的大模型？点击右上角「模型设置」，"
            "填入 DeepSeek / Kimi / 通义千问 / OpenAI 的 API Key 即可切换。") % (turn, user_msg)
    for ch in text:
        yield ("delta", ch)
        time.sleep(0.015)


# ================= 认证相关接口 =================
# 登录态基于 Flask session（签名 Cookie），前端无需手动管理 token。
@app.route("/api/register", methods=["POST"])
def api_register():
    """用户注册。请求体 JSON：{"username": ..., "password": ...}

    成功：写入 users 表并直接置为登录态，返回 {"ok":true, "username":...}
    失败：HTTP 400 + {"error": 原因}
    """
    d = request.get_json(force=True)                       # force=True 表示不校验 Content-Type
    u, p = (d.get("username") or "").strip(), d.get("password") or ""
    if not USERNAME_RE.match(u):
        return jsonify({"error": "用户名需为 2-20 位中英文/数字/下划线"}), 400
    if len(p) < 4:
        return jsonify({"error": "密码至少 4 位"}), 400
    conn = db()
    try:
        # 密码入库前先哈希；created 存 Unix 时间戳
        conn.execute("INSERT INTO users(username,password,created) VALUES(?,?,?)",
                     (u, hash_pw(p), time.time()))
        conn.commit()
    except sqlite3.IntegrityError:
        # username 有 UNIQUE 约束，重复插入会抛 IntegrityError
        return jsonify({"error": "用户名已存在"}), 400
    finally:
        conn.close()
    session["user"] = u                                     # 注册成功即登录
    return jsonify({"ok": True, "username": u})


@app.route("/api/login", methods=["POST"])
def api_login():
    """用户登录。请求体 JSON：{"username": ..., "password": ...}

    用户名不存在或密码哈希不一致都返回相同提示，避免暴露"用户是否存在"。
    """
    d = request.get_json(force=True)
    u, p = (d.get("username") or "").strip(), d.get("password") or ""
    conn = db()
    row = conn.execute("SELECT * FROM users WHERE username=?", (u,)).fetchone()
    conn.close()
    if not row or row["password"] != hash_pw(p):
        return jsonify({"error": "用户名或密码错误"}), 400
    session["user"] = u                                     # 写入签名 Cookie，后续请求自动带上
    return jsonify({"ok": True, "username": u})


@app.route("/api/logout", methods=["POST"])
def api_logout():
    """退出登录：清空服务端 session。"""
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/me")
def api_me():
    """查询当前登录状态。前端启动时调用，决定显示登录页还是主界面。"""
    u = current_user()
    return jsonify({"username": u} if u else {"username": None})


# ================= 模型列表与 API Key 管理 =================
@app.route("/api/models")
def api_models():
    """返回可选模型列表，以及当前用户已经配置过 Key 的厂商清单。

    返回：{"models": config.json 中的全部模型, "keyed": ["doubao", ...]}
    前端据此在下拉框给已配置的模型打 🔑✓ 标记。
    """
    u = current_user()
    if not u:
        return jsonify({"error": "未登录"}), 401
    conn = db()
    rows = conn.execute("SELECT provider FROM apikeys WHERE username=?", (u,)).fetchall()
    conn.close()
    keyed = [r["provider"] for r in rows]
    return jsonify({"models": CONFIG["models"], "keyed": keyed})


@app.route("/api/apikey", methods=["POST"])
def api_apikey():
    """保存（或覆盖）当前用户某个厂商的 API Key。

    请求体：{"provider": "doubao", "apikey": "xxxx"}
    靠表上的联合主键 + INSERT OR REPLACE 实现"同一厂商再次提交即更新"。
    """
    u = current_user()
    if not u:
        return jsonify({"error": "未登录"}), 401
    d = request.get_json(force=True)
    provider, key = d.get("provider"), (d.get("apikey") or "").strip()
    if not provider or not key:
        return jsonify({"error": "provider 与 apikey 不能为空"}), 400
    conn = db()
    conn.execute("INSERT OR REPLACE INTO apikeys(username,provider,apikey) VALUES(?,?,?)",
                 (u, provider, key))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


def get_apikey(user: str, provider: str):
    """读取某用户某厂商的 API Key；未配置返回 None。"""
    conn = db()
    row = conn.execute("SELECT apikey FROM apikeys WHERE username=? AND provider=?",
                       (user, provider)).fetchone()
    conn.close()
    return row["apikey"] if row else None


# ================= 会话管理接口 =================
# 用户只能读写 data/history/<自己的用户名>/ 下的文件，天然按目录隔离。
@app.route("/api/conversations")
def api_conversations():
    """会话列表：扫描当前用户目录下所有 JSON，只取摘要字段（不加载完整消息）。

    返回按最近更新时间倒序排列的列表，供左侧边栏展示。
    """
    u = current_user()
    if not u:
        return jsonify({"error": "未登录"}), 401
    items = []
    for fn in os.listdir(user_dir(u)):
        if fn.endswith(".json"):
            try:
                with open(os.path.join(user_dir(u), fn), "r", encoding="utf-8") as f:
                    c = json.load(f)
                items.append({"id": c["id"], "title": c.get("title", "未命名会话"),
                              "updated": c.get("updated", 0), "model": c.get("model_name", "")})
            except Exception:
                # 个别损坏 / 写了一半的文件直接跳过，不影响整个列表
                continue
    items.sort(key=lambda x: x["updated"], reverse=True)   # 最近的会话排最前
    return jsonify({"conversations": items})


@app.route("/api/conversation/<cid>")
def api_conversation(cid):
    """获取单个会话的完整内容（含全部消息），用于点击历史会话时回显。"""
    u = current_user()
    if not u:
        return jsonify({"error": "未登录"}), 401
    if not valid_cid(cid):
        return jsonify({"error": "非法会话 ID"}), 400
    conv = load_conv(u, cid)
    if not conv:
        return jsonify({"error": "会话不存在"}), 404
    return jsonify(conv)


@app.route("/api/conversation/<cid>/delete", methods=["POST"])
def api_delete_conv(cid):
    """删除一个会话文件。cid 非法时静默成功（前端容错，不暴露文件信息）。"""
    u = current_user()
    if not u:
        return jsonify({"error": "未登录"}), 401
    if valid_cid(cid):
        p = conv_path(u, cid)
        if os.path.exists(p):
            os.remove(p)
    return jsonify({"ok": True})


# ================= 对话接口（核心，SSE 流式） =================
@app.route("/api/chat", methods=["POST"])
def api_chat():
    """发送一条消息，以 SSE 流的形式实时返回 AI 回复。

    请求体 JSON：
      message   str  必填，本轮用户消息
      model_id  str  模型 id（默认 demo 本地演示模型）
      conv_id   str  可选，32 位会话 ID；为空表示开启新会话
      custom    dict 可选，自定义模型的 {"base_url":..., "model":...}

    响应（text/event-stream），每行 data 为一个 JSON：
      data: {"conv_id": "...."}          —— 首帧，让前端记住会话 ID（新会话时尤其重要）
      data: {"delta": "你"}              —— 后续多帧，AI 回复的增量文本
      data: {"error": "..."}             —— 出错帧（出现后流结束，本次不保存 AI 回复）
      data: [DONE]                       —— 结束帧
    """
    # ---- 1. 鉴权与参数校验 ----
    u = current_user()
    if not u:
        return jsonify({"error": "未登录"}), 401
    d = request.get_json(force=True)
    message = (d.get("message") or "").strip()
    model_id = d.get("model_id") or "demo"
    cid = d.get("conv_id") or ""
    if not message:
        return jsonify({"error": "消息不能为空"}), 400
    if model_id not in MODELS:
        return jsonify({"error": "未知模型"}), 400
    mcfg = MODELS[model_id]

    # ---- 2. 自定义模型：base_url / model 名由客户端提交覆盖 ----
    if model_id == "custom":
        custom = d.get("custom") or {}
        base_url = (custom.get("base_url") or "").strip()
        model = (custom.get("model") or "").strip()
        # 用户填了任意一项自定义参数就按自定义走，没填的项回退到默认配置
        if base_url or model:
            mcfg = dict(mcfg,
                        base_url=base_url or mcfg["base_url"],
                        model=model or mcfg["model"],
                        name="自定义：" + (model or mcfg["model"]))
        if not mcfg["base_url"] or not mcfg["model"]:
            return jsonify({"error": "请在模型设置中填写自定义接口地址与模型名"}), 400

    # ---- 3. 加载已有会话或新建会话（上下文保持的关键：历史消息都在文件里）----
    if cid and valid_cid(cid):
        conv = load_conv(u, cid)
        if not conv:
            return jsonify({"error": "会话不存在"}), 404
    else:
        # 新会话：uuid4().hex 正好是 32 位十六进制；标题取首条消息前 20 字
        conv = {"id": uuid.uuid4().hex, "title": message[:20], "created": time.time(),
                "updated": time.time(), "model_name": mcfg["name"], "messages": []}
    # 先把本轮用户消息追加进上下文（此时尚未落盘，等 AI 回复完成后一起保存）
    conv["messages"].append({"role": "user", "content": message, "time": time.time()})

    # ---- 4. 取模型 API Key（演示模型除外）----
    api_key = None
    if mcfg["provider"] != "demo":
        api_key = get_apikey(u, mcfg["provider"])
        if not api_key:
            return jsonify({"error": "尚未配置该模型的 API Key，请点右上角「模型设置」"}), 400

    # 组装发给大模型的消息：1 条 system（人设）+ 该会话全部历史消息
    # 把每条历史都带上 -> 模型能看到之前的对话，实现多轮上下文
    sys_msgs = [{"role": "system", "content": CONFIG.get("system_prompt", "你是一个乐于助人的智能助手。")}]
    history = [{"role": m["role"], "content": m["content"]} for m in conv["messages"]]

    # ---- 5. SSE 生成器：Flask 会在迭代过程中把数据持续 flush 给浏览器 ----
    def generate():
        full = []          # 累积本次 AI 的全部增量，结束后拼成完整回复
        # 根据模型类型选择上游来源（本地演示 or 真实 OpenAI 兼容接口）
        stream = stream_demo(history) if mcfg["provider"] == "demo" else \
            stream_openai(mcfg["base_url"], api_key, mcfg["model"], sys_msgs + history)
        # 首帧先告诉前端会话 ID：新会话时前端凭它更新地址栏 / 侧边栏
        yield "data: %s\n\n" % json.dumps({"conv_id": conv["id"]}, ensure_ascii=False)
        for kind, payload in stream:
            if kind == "error":
                yield "data: %s\n\n" % json.dumps({"error": payload}, ensure_ascii=False)
                return                       # 出错直接结束，不写空回复进记录
            full.append(payload)
            # 每来一小段就立刻下发一帧 -> 前端实现打字机式实时显示
            yield "data: %s\n\n" % json.dumps({"delta": payload}, ensure_ascii=False)
        # ---- 6. 流正常结束：完整回复落盘保存 ----
        reply = "".join(full)
        conv["messages"].append({"role": "assistant", "content": reply, "time": time.time()})
        conv["model_name"] = mcfg["name"]
        save_conv(u, conv)                   # 聊天记录写入 JSON 文件
        yield "data: [DONE]\n\n"

    # text/event-stream 是 SSE 的标准 MIME；
    # Cache-Control:no-cache 防止代理缓存；X-Accel-Buffering:no 关闭 Nginx 缓冲
    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/")
def index():
    """根路径返回前端单页应用 static/index.html。"""
    return app.send_static_file("index.html")


# ================= 启动入口 =================
if __name__ == "__main__":
    init_db()           # 首次启动自动建库建表
    print("=" * 50)
    print("VoiceChat 已启动：http://localhost:8790")
    print("=" * 50)
    # threaded=True 让 Flask 开发服务器支持并发请求；
    # SSE 连接会被长时间占用，不开线程会导致后续请求排队
    app.run(host="0.0.0.0", port=8790, threaded=True)
