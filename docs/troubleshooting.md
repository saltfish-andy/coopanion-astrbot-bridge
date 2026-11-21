# 排查手册

按这个顺序从下往上查，哪一步断了就停在哪。

```bash
curl http://127.0.0.1:8898/          # 1. 代理活着吗
curl http://127.0.0.1:8899/health    # 2. 桥活着吗，平台连上没有
```

然后看两个日志：

```
<这个仓库>/logs/coopanion_proxy.log       代理
<AstrBot>/run.log                        桥（搜 "Coopanion"）
```

---

## 桌宠一声不吭，但日志显示请求成功

**最可能：返回的是纯文本，不是 `pet_say`。**

Coopanion 只认 `pet_say` / `pet_ask`。纯文本回复会安安静静写进它的 session 文件，界面上什么都不显示。

确认方法，在代理日志里找这一行：

```
[proxy] pet_say -> '在呀 刚忙完'
```

**没有这一行** → 往下看。

### 情况 A：请求的工具单里根本没有 `pet_say`

开调试日志重新跑一次：

```bash
set COOPANION_DEBUG=1
python proxy/coopanion_proxy.py 8898
```

会看到：

```
[proxy] chat tools=[...] 尾部角色=[...] pet_say=False
```

如果 `tools` 里是 `read_file / write_file / glob_files` 这种，说明这个请求来自**别的 world**（比如文件操作、控制台），不是桌面宠物。那个 world 本来就不该说话，纯文本是正确行为。

如果 `tools` 是空数组或者没有 `tools` 字段，说明 Coopanion 那个 provider 被配错了地方——检查你改的是不是当前 `activeProvider` 指向的那份配置。

### 情况 B：工具单里有 `pet_say`，但代理没包

看日志里有没有 `pet_say 已执行过，本轮直接收尾`。有的话说明**上一轮已经说过了**，这是正常的收尾，不是故障。

如果既没有 `pet_say ->` 也没有 `已执行过`，那就是 `_said_already()` 误判了——检查 `role=tool` 的内容里是不是恰好含「已开始显示」这类字样。

### 情况 C：真有 `pet_say ->` 但还是不显示

问题在 Coopanion 那边。看它自己的日志：

```
<Coopanion>/data/logs/core.log
```

找 `pet_say` 的执行回执。正常是 `已开始显示,这段约 5 秒。`；如果是 `[pet_say 没执行]` 开头，会说原因，最常见的是**桌宠窗口没连上**（比如窗口被关了，只剩托盘）。

---

## QQ 收到了桌宠的回复

**`event.send` 没被顶掉。**

先确认日志：

```
[Coopanion] 拦截发送，文本 21 字
[CompanionV2] AstrBot 核心回复阶段结束 send_attempted=False send_state=not_started
```

`send_attempted=False` 就对了。

如果看到的是 `Prepare to send` 之后**没有** `拦截发送`，说明拦截没挂上。检查两件事：

1. **`on_decorating_result` 里有没有再设一次 `event.send`**

   只设 `on_llm_request` 是不够的，AstrBot 中途会重置。两个钩子都要设。代码见 `main.py` 的 `arm_intercept` 和 `capture`。

2. **钩子的 `priority` 够不够小**

   要用 `priority=-2000000`（数值越小越晚执行，也就是越靠近流水线末尾）。如果别的插件也在同一个钩子上抢先做了发送，你的替换就白做了。

还有一个隐蔽的情况：**别的插件自己发消息**。有些 companion 类插件会绕过 `event.send` 直接用平台 API 推消息。这种在插件层面拦不住，得去那个插件里关掉。

---

## 同一句话连弹好几次

**Coopanion 在重试。**

它判定失败会自动重发同一轮，最多 5 次，间隔 8 秒左右。代理里有 60 秒的按文本去重。

如果还是重复：

- 把 `COOPANION_DEDUPE_TTL` 调大（比如 300）
- 看日志里两次 `[proxy] chat -> '...'` 的文本是不是完全一样。如果只差一点（比如带了不同的时间戳前缀），去重就会失效，得在 `_from_tool()` 里把时间戳剥掉

还有一种可能：**AstrBot 内部发了两遍**。查 `run.log` 里同一个 `message_id` 是不是出现了两次 `Prepare to send`。如果是，去查是不是有两个插件都在处理这条事件。

---

## Coopanion 一直显示「请配置 API Key」

**缺 `.env`。**

Coopanion 的 `secret` 字段填的**不是密钥本身，而是环境变量名**。它会在 provider 目录下找 `.env` 文件，用这个名字去里面查值。

```
<Coopanion>/data/home/providers/<你的provider名>/
├── config.json     ← "secret": "DEEPSEEK_API_KEY"
└── .env            ← DEEPSEEK_API_KEY=随便什么占位值
```

代理不校验密钥，所以值填什么都行，**但文件必须存在**。没有它，Coopanion 连请求都不会发。

---

## `Chat transport ended without finish_reason`

**协议对不上。**

Coopanion 的 provider 配置里：

```json
{"options": {"protocol": "chat"}}
```

代理虽然 `chat` 和 `responses` 都支持，但推荐 `chat`。换成 `responses` 的话，代理返回的事件必须带 `type` 字段（代码里已经处理了），但那条分支验证得少。

改完重启 Coopanion。

---

## 代理返回 429 `busy`

**上一条还没回完。**

插件里有个全局锁，同一时刻只处理一条请求。Coopanion 有时候会连着发两条（比如系统事件和你的输入挤在一起）。

正常情况下代理会把内部事件过滤掉，不会撞上。如果经常 429：

- 检查是不是关了内部事件过滤（`_is_system()`）
- 或者把 `config.json` 里的 `timeout` 调小，让卡住的请求早点释放

桌面宠物单用户场景，这个限制不是问题。

---

## 请求超时（504）

AstrBot 那边 180 秒没出结果。通常是：

- 模型 API 卡住了 / 限流了 —— 去 AstrBot 日志看模型调用
- 某个插件在 `on_llm_request` 里挂了 —— 逐个禁用插件排查
- 桥注入的事件没被处理（`platform not ready`）—— 看 `/health` 返回的平台 id 是不是 `-`

---

## `/health` 返回 `"platform": "-"`

**AstrBot 的 aiocqhttp 适配器还没连上。**

插件启动时如果平台还没就绪，会显示 `-`。这不影响后续请求（每次都用 `_find_platform()` 重新找），但如果一直不恢复，说明：

- 消息平台（比如 NapCat / SnowLuma）没启动
- 或者 AstrBot 的反向 WS 端口没开

去 AstrBot 的 WebUI 看「平台」那一页。

---

## Coopanion 卡住，说什么都没反应

看它的 `core.log` 有没有这种：

```
[core.loop] 投递水位停滞:有该进上下文的外部事件长时间没被投递
```

这是 Coopanion 自己的事件投递队列卡住了，跟本代理没关系。实测拖拽桌宠（`[touch]` 事件）偶尔会触发，通常下一次事件到达时会自己恢复。

如果一直不恢复，重启 Coopanion。

---

## 还是不行，怎么收集信息

三个地方一起看：

1. **代理日志**（开 `COOPANION_DEBUG=1` 重跑）
2. **AstrBot 的 `run.log`**，搜 `Coopanion`
3. **Coopanion 的 `core.log`** 和 `data/home/companion/data/session-main.jsonl`

`session-main.jsonl` 特别有用——它是 Coopanion 的 LLM 上下文记录，一行一条。看最后几行就知道它到底收到了什么、生成了什么：

```bash
tail -n 5 <Coopanion>/data/home/companion/data/session-main.jsonl
```

如果最后一条是 `role=assistant` 且 `content` 里有文字，但界面没显示 → 就是 `pet_say` 的问题。
