# 实现细节

这份文档写给想改代码的人。想看怎么用，回 [README](../README.md)。

---

## 1. Coopanion 发过来的请求长什么样

理解这个项目，先看清楚 Coopanion 到底发了什么。

### 它不用标准的 user 消息

你在桌宠里打一句「你吃了什么」，它不会发成这样：

```json
{"role": "user", "content": "你吃了什么"}
```

而是把**所有东西**都当事件（触摸、拖拽、你打字的、它自己的设置变化），攒成帧再交给模型。所以真实请求长这样：

```json
{
  "model": "deepseek-flash",
  "stream": true,
  "tools": [
    {"type": "function", "function": {"name": "pet_say", "parameters": {...}}},
    {"type": "function", "function": {"name": "pet_ask", "parameters": {...}}},
    {"type": "function", "function": {"name": "external_event_frame", "parameters": {...}}},
    ...
  ],
  "messages": [
    {"role": "system", "content": "...一长串人格和环境说明..."},
    {"role": "user", "content": [{"type": "input_text", "text": "[system] 3 new events, 2026-01-01 Thu 09:00–09:30."}]},
    {"role": "assistant", "content": null, "tool_calls": [
      {"id": "evf_70", "type": "function", "function": {"name": "external_event_frame", "arguments": "{}"}}
    ]},
    {"role": "tool", "content": "[3 new events]\n[09:00] [touch] 用户把你拎起来甩了出去\n[09:30] [typed] 用户:在吗"}
  ]
}
```

关键点：

- `role=user` 里装的是 `[system] N new events` 这种**占位提示**，不是你说的话
- 你说的话在 **`role=tool`** 的工具结果里，格式是 `[typed] <昵称>:<内容>`
- `external_event_frame` 这个工具调用是 Coopanion 自己加的，不是模型调的

所以代理的核心工作之一，就是从 `role=tool` 里把这行捞出来。用的是这条正则：

```python
_TYPED = re.compile(r"\[typed\]\s*[^:：\n]{1,40}[:：]\s*(.+)")
```

### 内部事件占了绝大多数请求

Coopanion 很爱发请求。拖一下桌宠、换个皮肤、切个设置、机器唤醒……都会触发一轮。这些请求的 `role=user` 里全是 `[system] ...`，没有 `[typed]`。

代理的判断很简单：**整份请求里找不到人话，就回一个空的 `finish_reason: stop`**，不去打扰 AstrBot。

不这么做的话，你拖一下桌宠，QQ 那边就会莫名其妙收到一句话。

---

## 2. 为什么必须返回 `pet_say`

这是整个项目踩得最久的一个坑。

Coopanion 的桌宠气泡和对话页，**只从 `pet_say` / `pet_ask` 这两个工具产生的 `desktop-pet.self` 事件来渲染**。它源码里的 `chat.ts` 有这么一行：

```ts
const SPOKEN_TOOLS = new Set(['pet_say', 'pet_ask']);
```

也就是说：

| 代理返回什么 | 桌宠气泡 | 对话页 | session 记录 |
|---|---|---|---|
| 纯文本 `content` | 不显示 | 不显示 | 有 |
| `tool_calls: pet_say` | **显示** | **显示** | 有 |

这个现象非常有迷惑性：日志显示 HTTP 200、请求成功、session 文件里能搜到回复全文——**但用户一个字都看不见**。

所以代理拿到 AstrBot 的回复后，把它包成这样：

```json
{
  "choices": [{
    "index": 0,
    "delta": {
      "role": "assistant",
      "tool_calls": [{
        "index": 0,
        "id": "call_coopanion_say",
        "type": "function",
        "function": {
          "name": "pet_say",
          "arguments": "{\"script\": \"在呀 刚忙完\"}"
        }
      }]
    },
    "finish_reason": null
  }]
}
```

Coopanion 执行 `pet_say`，气泡弹出来，然后它把执行回执作为 `role=tool` 再发一轮过来。回执长这样：

```
已开始显示,这段约 5 秒。
```

代理看到回执里含「已开始显示 / 已排队 / [pet_say 没执行]」，就知道这轮说完了，**直接回空收尾**，不再走一次 AstrBot。不然你会收到两遍回复。

### 不是所有请求都带 pet_say

Coopanion 有多个 world（桌面宠物、文件操作、控制台……）。**只有桌面宠物那个 world 才把 `pet_say` 放进 `tools`**。

所以代理会先检查 `tools` 里有没有 `pet_say`，没有就走纯文本分支。这个判断不能省，否则文件操作那类请求会收到莫名其妙的工具调用。

开 `COOPANION_DEBUG=1` 能看到每个请求的工具单：

```
[proxy] chat tools=['read_file', 'write_file', ..., 'save_blob'] 尾部角色=[...] pet_say=False
```

---

## 3. SSE 必须一次给完

Coopanion 的流式解析器（`response-assembly.ts`）对格式很挑：

- **chat 协议**：只认 `choices[].finish_reason`。整段流里没有它，就抛 `Chat transport ended without finish_reason`。
- **responses 协议**：只认带 `type` 字段的事件。

而且实测**逐块推会失败**——它等不到 `finish_reason` 就断连了。所以代理的实现是：

1. 先把完整回复从 AstrBot 拿到
2. 一次性把整个 chunk 推过去，紧跟一个 `finish_reason`
3. 结束

```python
self._sse({... "delta": {"content": reply}, "finish_reason": None})
self._sse({... "delta": {}, "finish_reason": "stop"})
self._sse_done()
```

代价是不支持真正的流式输出，桌宠要等完整回复（5～15 秒）。桌面宠物场景可以接受。

---

## 4. Coopanion 会重试，所以要去重

Coopanion 判定一次生成失败，会自动重发同一轮，最多 5 次。实测间隔 8 秒左右。

后果：同一句话在桌宠里连弹好几次，AstrBot 那边被叫好几次。

代理里有个按文本的短期缓存：

```python
Handler._recent = {}          # text -> (时间戳, 回复)
Handler.RECENT_TTL = 60       # 秒
```

窗口内同一句话再来，直接返回上次的结果，不去打扰 AstrBot。

如果还是重复，通常是两次请求的文本有细微差别（比如时间戳变了），把 `COOPANION_DEDUPE_TTL` 调大试试。

---

## 5. AstrBot 这边：怎么借脑子，又怎么不吵到 QQ

插件要解决两件事：**让 AstrBot 认为有一条真的 QQ 消息**，和**别让它真的发出去**。

### 5.1 伪造事件

```python
message = AstrBotMessage()
message.type = MessageType.FRIEND_MESSAGE      # 装成 QQ 私聊
message.session_id = owner_id                  # 以你自己的号说话
message.self_id = self_id                      # 机器人自己的号
message.message_str = text
message.message = [Plain(text=text)]
message.sender = MessageMember(user_id=owner_id, nickname=nickname)

event = platform.create_event(message)
event.set_extra(FLAG, True)      # 打标记，所有钩子都先检查它
platform.commit_event(event)     # 塞进事件总线
```

`FLAG` 这个标记是关键：所有钩子第一行都 `if not event.get_extra(FLAG): return`，保证**绝不碰真实的 QQ 消息**。

这样 AstrBot 会当成正常私聊处理——人格、长期记忆、表情包、联网搜索全都正常跑，跟你自己在 QQ 上说话没什么区别。

### 5.2 截住回复（这里有个坑）

最直觉的做法是在 `on_llm_request` 里把 `event.send` 换掉：

```python
event.send = self._silent_send
```

**这不够。** AstrBot 在流水线中途会把 `event.send` 重新绑回自己的 `send_parts`。日志能直接看到：

```
[Coopanion] 挂上发送拦截，当前 event.send=_silent_send
...
[Coopanion] 兜底钩子执行，结果 27 字，此刻 event.send=send_parts   ← 变了
[respond.stage] Prepare to send - 用户: 嗯 好呀            ← QQ 发出去了
```

正确的做法是**在 `on_decorating_result` 里再挂一次**。这个钩子跑在 `RespondStage` **之前**（日志顺序能证明），所以这时候替换是有效的：

```python
@filter.on_decorating_result(priority=-2000000)
async def capture(self, event):
    if not event.get_extra(FLAG):
        return
    # 取出文本，交回给代理
    ...
    event.send = self._silent_send     # ← 关键：这里再挂一次
    event.stop_event()
```

修好之后 AstrBot 自己的诊断日志会给你铁证：

```
[CompanionV2] AstrBot 核心回复阶段结束 send_attempted=False send_state=not_started
```

### 5.3 时序图

```
                    on_llm_request      ResultDecorateStage        RespondStage
                         │                    │                       │
  注入事件 ──► 建 LLM 请求 ──► 模型 ──► 结果装饰 hooks ──► 发送
                         │                    │                       │
                   挂 send（会被覆盖）    再挂一次 send（有效）     被 silent_send 顶掉
                                              │
                                         _resolve(文本) ──► 代理拿到回复
```

---

## 6. 一次完整往返

```
你打「在吗」
  │
  ├─ Coopanion 发请求到 :8898，人话在 role=tool 的 [typed] 里
  │
  ├─ 代理抠出「在吗」，POST /ask 给 :8899
  │
  ├─ 插件伪造 QQ 私聊事件，塞进 AstrBot
  │
  ├─ AstrBot 跑人格 + 记忆 + 搜索，调模型，拿到「在呀 刚忙完」
  │
  ├─ 结果装饰阶段：取出文本 → 交回代理；同时 event.send 换成 silent_send
  │     （QQ 那边发不出去，send_attempted=False）
  │
  ├─ 代理收到「在呀 刚忙完」，包成 pet_say 工具调用返回
  │
  ├─ Coopanion 执行 pet_say → 气泡弹出「在呀 刚忙完」
  │
  └─ Coopanion 再发一轮带「已开始显示」回执的请求 → 代理回空收尾
```

---

## 7. 想改的话，从哪下手

| 想改什么 | 改哪 |
|---|---|
| 换成别的聊天框架 | 改 `ask_bridge()`，POST 的格式随你 |
| 桌宠说话方式（长度、分段） | `_bubble_script()`，现在是压成一行、截 160 字 |
| 让桌宠用 `pet_ask` 反问 | 在 `_deliver()` 里加分支，返回 `pet_ask` 的 tool_call |
| 换 Coopanion 的协议 | 代理两种都支持，改 provider 里的 `options.protocol` |
| 转发图片/语音 | 现在只处理文字，多模态要自己从 `role=tool` 里解 blob |

改之前建议先开 `COOPANION_DEBUG=1` 跑几轮，看清楚 Coopanion 到底发了什么。它的内部格式没有公开文档，只能靠观察。
