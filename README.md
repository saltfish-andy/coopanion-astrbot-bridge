# Coopanion ↔ AstrBot 桥

> 让 [Coopanion](https://github.com/Pal-AI-Lab/Coopanion) 桌面宠物用上 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 的人格、记忆和插件，而不用重新做一只。

桌宠那层壳还是 Coopanion，说话的脑子换成 AstrBot。你在桌宠里打的字，会走一遍 AstrBot 的完整链路（人格、长期记忆、表情包、联网搜索……），回复再回到桌宠的气泡里。

QQ 那边不受影响：QQ 的消息还是 QQ 回，桌宠的消息只在桌宠里回。

---

## 它解决的两个问题

这两个坑不填，直接把 Coopanion 的模型地址指向 AstrBot 是连不通的。

### 1. 你打的字不在 `role=user` 里

Coopanion 不会把你说的话放进标准的 `user` 消息。它把所有事件（包括你的输入）打包成内部帧，然后：

- `role=user` 里装的是 `[system] 3 new events ...` 这种占位提示
- 你说的话在 `role=tool` 的工具结果里，形如：

```
[1 new event]
[01-01 09:30] [typed] 用户:你吃了什么
```

一个只会读 `user` 消息的接口，永远拿不到你说的话。

### 2. 桌宠只有调 `pet_say` 才开口

Coopanion 的桌宠气泡和对话页，只从 `pet_say` / `pet_ask` 这两个工具产生的事件来渲染（见它源码里的 `SPOKEN_TOOLS`）。

**纯文本的 assistant 回复只会写进它自己的 session 记录，一个字都不会显示。**

所以你把 AstrBot 的回复当普通文本返回，会看到这个现象：日志显示请求成功、session 里也能查到回复全文，但桌宠一声不吭。

---

### 顺带支持：语音输入

Coopanion 的语音识别（本地 SenseVoice）认出来的话，走的不是 `[typed]` 而是 `[voice]`：

```
[1 new event]
[01-01 09:30] [voice] 用户:你吃了什么
```

代理两种都认，一行正则的事：

```python
_SAID = re.compile(r"\[(?:typed|voice)\]\s*[^:：\n]{1,40}[:：]\s*(.+)")
```

**有一种情况处理不了**：如果把 Coopanion 的 `asr.recording.transcript` 关掉，它就不生成文字了，只发录音：

```
[voice] 用户说了一段话,内容在下面的录音里
```

这种没有冒号，匹配不上，会被当成内部事件跳过。**要让它能听懂，那个转写开关必须开着。**

---

## 架构

```
┌──────────────────────────┐
│  Coopanion 桌宠 (Electron)│
│  你打字 → 事件队列 → LLM   │
└───────────┬──────────────┘
            │ OpenAI 兼容请求（chat / responses）
            │ baseUrl 指向 127.0.0.1:8898
            ▼
┌──────────────────────────┐
│  coopanion_proxy.py       │  ① 从 role=tool 里抠出你说的话
│  127.0.0.1:8898           │  ② 丢掉内部事件
│                          │  ③ 按文本去重（Co 会重试）
└───────────┬──────────────┘
            │ POST /ask {"text": "..."}
            ▼
┌──────────────────────────┐
│  coopanion_bridge 插件     │  ④ 伪造一条 QQ 私聊事件
│  AstrBot 内，127.0.0.1:8899│     注入 AstrBot 流水线
└───────────┬──────────────┘
            │
            ▼
┌──────────────────────────┐
│  AstrBot 流水线            │  ⑤ 人格 / 记忆 / 搜图 / 表情 / ...
│  → 大模型 → 结果装饰       │  ⑥ 半路截住，不发 QQ
└───────────┬──────────────┘
            │ 回复文本
            ▼
┌──────────────────────────┐
│  coopanion_proxy.py       │  ⑦ 包成 pet_say 工具调用
└───────────┬──────────────┘
            │ finish_reason: tool_calls
            ▼
┌──────────────────────────┐
│  Coopanion 桌宠            │  ⑧ 执行 pet_say → 气泡弹出来
└──────────────────────────┘
```

一句话：**代理负责「抠出人话」和「让桌宠开口」，插件负责「借 AstrBot 的脑子」和「别让回复漏到 QQ」。**

---

## 快速开始

### 前置条件

- Windows（本方案在 Windows 11 上验证，其他系统理论上可用，未测）
- Python 3.10+
- 已装好的 [AstrBot](https://github.com/AstrBotDevs/AstrBot)（4.x，需有 aiocqhttp 适配器）
- 已装好的 [Coopanion](https://github.com/Pal-AI-Lab/Coopanion)（0.2.x）

### 第 1 步：装 AstrBot 插件

把 `astrbot_plugin_coopanion_bridge/` 整个目录复制到 AstrBot 的插件目录：

```
<AstrBot>/data/plugins/coopanion_bridge/
├── main.py
├── metadata.yaml
└── config.json          ← 你没建也会用默认值，见第 2 步
```

然后重启 AstrBot。日志里应该出现：

```
[Coopanion] 桥已启动 http://127.0.0.1:8899
```

### 第 2 步：配插件

在插件目录里复制一份配置：

```bash
cp config.example.json config.json
```

改这三个值（**必填，不填会广播到错误的人**）：

```json
{
  "self_id": "你的机器人QQ号",
  "owner_id": "你自己的QQ号",
  "owner_nick": "你的昵称"
}
```

- `owner_id`：桌宠会以「这个 QQ 好友」的身份跟 AstrBot 说话。用你自己的号，这样人格和记忆才知道是谁在聊。
- `self_id`：机器人的号。AstrBot 靠它定位平台实例。
- `port`：桥的端口，默认 8899，一般不用改。

### 第 3 步：起代理

```bash
python proxy/coopanion_proxy.py 8898
```

看到这行就对了：

```
coopanion-proxy listening on http://127.0.0.1:8898
```

想开机自启，参考 `examples/start-proxy.ps1`。

### 第 4 步：把 Coopanion 指过来

编辑 Coopanion 的 provider 配置（路径形如 `data/home/providers/<名字>/config.json`）：

```json
{
  "kind": "coo",
  "baseUrl": "http://127.0.0.1:8898",
  "secret": "DEEPSEEK_API_KEY",
  "spec": {
    "model": "whatever",
    "maxTokens": 8192
  },
  "multimodal": true,
  "pricing": [],
  "options": {
    "protocol": "chat"
  }
}
```

两个要点：

- **`protocol` 必须是 `chat`**。Coopanion 默认走 `responses`，代理虽然两种都支持，但 `chat` 更稳。
- **`secret` 填的是「环境变量名」，不是密钥本身**。Coopanion 会去同目录的 `.env` 里找这个变量。随便给个占位值就行：

```bash
# <provider 目录>/.env
DEEPSEEK_API_KEY=sk-local-proxy-placeholder
```

不给 `.env`，Coopanion 会一直显示「请配置 API Key」，连请求都不会发。

### 第 5 步：验证

顺着这个顺序查，哪一步断了就停在哪：

```bash
# 1. 代理活着
curl http://127.0.0.1:8898/

# 2. 桥活着（platform 显示你的机器人平台 id）
curl http://127.0.0.1:8899/health

# 3. 整条链路通（会真的叫一次大模型，约 10 秒）
curl -X POST http://127.0.0.1:8899/ask \
  -H "Content-Type: application/json" \
  -d "{\"text\":\"在吗\"}"
```

第 3 步能拿到 `{"ok": true, "reply": "..."}`，就说明 AstrBot 那半边全通了。

最后去 Coopanion 里发一句话。气泡弹出来，收工。

---

## 配置项

### 插件 `config.json`

| 键 | 默认 | 说明 |
|---|---|---|
| `self_id` | `""` | 机器人 QQ 号 |
| `owner_id` | `""` | 以谁的身份跟 AstrBot 说话 |
| `owner_nick` | `""` | 显示昵称 |
| `port` | `8899` | 桥监听端口 |
| `timeout` | `180` | 等 AstrBot 回复的秒数 |
| `pet_say` | `true` | 是否让代理把回复包成 `pet_say` |

### 代理环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `COOPANION_BRIDGE` | `http://127.0.0.1:8899/ask` | 桥的地址 |
| `COOPANION_TIMEOUT` | `200` | 请求桥的超时秒数 |
| `COOPANION_DEDUPE_TTL` | `60` | 同文本去重窗口（秒） |

端口也可以直接写在命令行参数里：`python coopanion_proxy.py 8898`。

---

## 工作原理

想看细节的进 [`docs/how-it-works.md`](docs/how-it-works.md)，这里只讲骨架。

**代理（8898）** 是个假的 OpenAI 接口。它不生成任何文字，只做四件事：

1. 从请求里把「人真正说的话」找出来（`role=tool` 里的 `[typed]`）
2. 内部事件（`[system]` 开头、`【新事件】`、`(...)`）一律回空，不打扰模型
3. 把文本转给桥，等回复
4. 把回复包成 `pet_say` 工具调用返回

**插件（8899）** 在 AstrBot 进程里，做两件事：

1. 收到 `/ask` 就伪造一条 QQ 私聊事件，打上标记，塞进 AstrBot 的事件总线
2. 在流水线尽头把这条回复截下来交回去，**不让它发到 QQ**

第 2 件是整个项目最容易踩坑的地方，原因写在 [`docs/troubleshooting.md`](docs/troubleshooting.md)。

---

## 常见问题

**桌宠一声不吭，但日志显示请求成功**

看 AstrBot 返回的内容是不是纯文本。Coopanion 只认 `pet_say`。检查代理日志里有没有 `pet_say -> '...'` 这一行。没有的话，看请求里的 `tools` 有没有 `pet_say`——有些 world（比如文件操作那个）根本不带这个工具，那种情况就该走纯文本。

**QQ 收到了桌宠说的话**

发送拦截没挂上。见 [`docs/troubleshooting.md`](docs/troubleshooting.md#qq-收到了桌宠的回复)。

**同一句话在桌宠里连弹好几次**

Coopanion 判定失败会自动重试，同一轮最多发 5 次。代理里有按文本去重（默认 60 秒），如果还是重复，说明每次请求的文本有细微差别，把 `COOPANION_DEDUPE_TTL` 调大试试。

**Coopanion 一直显示「请配置 API Key」**

缺 `.env`。见第 4 步。

**`Chat transport ended without finish_reason`**

代理返回的格式和 Coopanion 请求的 `protocol` 对不上。把 provider 的 `options.protocol` 设成 `chat`。

---

## 已知限制

- 只在 **Windows + AstrBot 4.29 + Coopanion 0.2.23** 上验证过，其他版本可能要调。
- 代理是「一个请求一个回复」的同步模型，**不支持流式增量输出**。桌宠那边会等完整回复，通常 5～15 秒。
- 桥里有个全局锁，**同一时刻只能处理一条**。并发请求会收到 `429 busy`。桌面宠物场景够用，不适合多用户。
- 不做图片/语音的双向传递。你在桌宠里发图，代理只处理文字部分。
- 依赖 Coopanion 当前的内部实现（`[typed]` 格式、`pet_say` 工具名）。它改版了这里就要跟着改。

---

## 目录结构

```
.
├── astrbot_plugin_coopanion_bridge/   AstrBot 插件
│   ├── main.py
│   ├── metadata.yaml
│   └── config.example.json
├── proxy/
│   └── coopanion_proxy.py             OpenAI 兼容代理
├── examples/
│   ├── provider-config.json           Coopanion 的 provider 样例
│   ├── provider.env.example
│   └── start-proxy.ps1                Windows 开机自启参考
├── docs/
│   ├── how-it-works.md                实现细节与协议对照
│   └── troubleshooting.md             踩过的坑和排查顺序
└── README.md
```

---

## 致谢

- [Coopanion](https://github.com/Pal-AI-Lab/Coopanion) — 桌面宠物本体
- [AstrBot](https://github.com/AstrBotDevs/AstrBot) — 聊天机器人框架

## 许可证

MIT。见 [LICENSE](LICENSE)。
