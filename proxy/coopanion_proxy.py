"""Coopanion → AstrBot 的 OpenAI 兼容代理。

它不生成任何文字，只做四件事：

  1. 从请求里把「人真正说的话」找出来（在 role=tool 的 [typed] 里，不在 role=user）
  2. 内部事件一律回空，不打扰模型
  3. 转给 AstrBot 桥，等回复
  4. 把回复包成 pet_say 工具调用——桌宠只认这个，纯文本它不显示

支持两种请求格式：
  * Chat Completions —— messages 数组（推荐，Coopanion 里把 protocol 设成 chat）
  * Responses API    —— input 数组（Coopanion 默认走这个）

环境变量：
  COOPANION_BRIDGE       桥的地址，默认 http://127.0.0.1:8899/ask
  COOPANION_TIMEOUT      请求桥的超时秒数，默认 200
  COOPANION_DEDUPE_TTL   同一句话的去重窗口秒数，默认 60
  COOPANION_DEBUG        设成 1 才打印每个请求的结构
  COOPANION_LOG          日志文件路径，默认 ../logs/coopanion_proxy.log
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BRIDGE = os.environ.get("COOPANION_BRIDGE", "http://127.0.0.1:8899/ask")
TIMEOUT = float(os.environ.get("COOPANION_TIMEOUT", "200"))
DEDUPE_TTL = float(os.environ.get("COOPANION_DEDUPE_TTL", "60"))
DEBUG = os.environ.get("COOPANION_DEBUG") == "1"

# Co 会把各种内部事件当 user 消息发过来，形如 [system] / [设置变化] / 【新事件】。
# 这些都不是人打的话，一律跳过；整份请求里只剩它们时直接回空。
SYSTEM_BRACKETS = ("[", "【", "(")


def log(message: str) -> None:
    print(message, flush=True)


# Co 把人打的字包在 tool 结果里，形如：
#   [1 new event]\n[01-01 09:30] [typed] 用户:你吃了什么
_TYPED = re.compile(r"\[typed\]\s*[^:：\n]{1,40}[:：]\s*(.+)")


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict)
            and part.get("type") in ("text", "input_text", "output_text")
        )
    return ""


def _is_system(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    return stripped.startswith(SYSTEM_BRACKETS)


def _from_tool(text: str) -> str:
    """从 tool 结果里取最后一条 [typed] 后面的话。"""
    best = ""
    for line in text.splitlines():
        match = _TYPED.search(line)
        if match:
            best = match.group(1).strip()
    return best


def _from_messages(messages) -> str:
    # 1) 优先从 tool 结果的 [typed] 里取（Co 把用户输入放这儿）
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        typed = _from_tool(_content_text(message.get("content")))
        if typed and not _is_system(typed):
            return typed
    # 2) 退回到普通 user 消息
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        text = _content_text(message.get("content"))
        if text and text.strip() and not _is_system(text):
            return text
    return ""


def _from_input(items) -> str:
    for item in reversed(items):
        if not isinstance(item, dict):
            continue
        if item.get("type") != "message" or item.get("role") != "user":
            continue
        text = _content_text(item.get("content"))
        if text and text.strip() and not _is_system(text):
            return text
    return ""


def extract_text(body: dict):
    """返回 (格式, 用户文本)。格式是 'chat' 或 'responses'。"""
    messages = body.get("messages")
    if isinstance(messages, list):
        return "chat", _from_messages(messages)
    inputs = body.get("input")
    if isinstance(inputs, list):
        return "responses", _from_input(inputs)
    return "chat", ""


# ---------------------------------------------------------------- pet_say

# 桌宠要说话必须调 pet_say 工具。纯文本的 assistant 消息只会写进它自己的 session，
# 桌宠气泡和对话页都不显示（Coopanion 源码里 SPOKEN_TOOLS 只认 pet_say / pet_ask）。
# 所以拿到 AstrBot 的回复后，代理替它发一个 pet_say 工具调用。
SAY_RECEIPTS = ("已开始显示", "已排队", "[pet_say 没执行]")
SAY_SCRIPT_MAX = 160


def _tool_name(tool):
    if not isinstance(tool, dict):
        return None
    function = tool.get("function")
    if isinstance(function, dict):
        return function.get("name")
    return tool.get("name")


def _has_tool(body: dict, wanted: str) -> bool:
    tools = body.get("tools")
    if not isinstance(tools, list):
        return False
    return any(_tool_name(tool) == wanted for tool in tools)


def _last_tool_text(messages) -> str:
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "tool":
            return _content_text(message.get("content"))
    return ""


def _said_already(messages) -> bool:
    """第二轮：Co 已经把 pet_say 的回执发回来了，别再让它说一遍。"""
    text = _last_tool_text(messages)
    return any(mark in text for mark in SAY_RECEIPTS)


def _bubble_script(text: str) -> str:
    """气泡一次只放一两句，换行压成空格，太长截断。"""
    one_line = " ".join(text.split())
    return one_line[:SAY_SCRIPT_MAX]


# ---------------------------------------------------------------- 响应体


def chat_body(model, reply):
    return {
        "id": "coopanion-bridge",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model or "astrbot",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": reply},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def say_call_body(model, script):
    return {
        "id": "coopanion-bridge",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model or "astrbot",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_coopanion_say",
                    "type": "function",
                    "function": {
                        "name": "pet_say",
                        "arguments": json.dumps({"script": script}, ensure_ascii=False),
                    },
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def responses_body(model, reply):
    return {
        "id": "resp_coopanion",
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": model or "astrbot",
        "output": [{
            "type": "message",
            "id": "msg_coopanion",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": reply, "annotations": []}],
        }],
        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
    }


def _print_request_shape(kind, body, messages, want_say):
    """COOPANION_DEBUG=1 时把请求结构印出来，方便确认工具单里有没有 pet_say。"""
    try:
        tool_names = [name for name in (_tool_name(t) for t in (body.get("tools") or [])) if name]
        if isinstance(messages, list):
            roles = [m.get("role") for m in messages if isinstance(m, dict)]
        else:
            roles = [item.get("role") for item in (body.get("input") or []) if isinstance(item, dict)]
        log(f"[proxy] {kind} tools={tool_names[:10]} 尾部角色=[{' '.join(roles[-6:])}] pet_say={want_say}")
    except Exception as error:  # noqa: BLE001
        log(f"[proxy] 结构打印失败 {error}")


def ask_bridge(text: str) -> str:
    payload = json.dumps({"text": text}).encode()
    request = urllib.request.Request(BRIDGE, data=payload, method="POST")
    request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        data = json.loads(response.read().decode())
    if not data.get("ok"):
        raise RuntimeError(data.get("error") or "bridge error")
    return data.get("reply") or ""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # Co 一旦判定失败就会把同一句重发好几遍（最多 5 次）。
    # 窗口内重复的文本直接用上次结果，不再打扰 AstrBot。
    _recent = {}
    _recent_lock = threading.Lock()
    RECENT_TTL = DEDUPE_TTL

    # ------------------------------------------------------------- 缓存

    def _cached(self, text):
        now = time.time()
        with Handler._recent_lock:
            for key in list(Handler._recent):
                if now - Handler._recent[key][0] > Handler.RECENT_TTL:
                    Handler._recent.pop(key, None)
            hit = Handler._recent.get(text)
        return hit[1] if hit else None

    def _remember(self, text, reply):
        with Handler._recent_lock:
            Handler._recent[text] = (time.time(), reply)

    # ------------------------------------------------------------- 基础

    def _send_json(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._send_json(200, {"ok": True, "service": "coopanion-proxy"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw or b"{}")
        except Exception:  # noqa: BLE001
            self._send_json(400, {"error": {"message": "bad json"}})
            return

        kind, text = extract_text(body)
        text = text.strip()
        want_stream = bool(body.get("stream"))
        model = body.get("model")
        messages = body.get("messages")

        said = kind == "chat" and isinstance(messages, list) and _said_already(messages)
        # 只有 Co 真把 pet_say 摆在工具单上时才发工具调用；
        # 像文件操作那个 world 根本不带这个工具，那种就维持纯文本。
        want_say = (
            kind == "chat"
            and isinstance(messages, list)
            and _has_tool(body, "pet_say")
            and not said
        )
        if DEBUG:
            _print_request_shape(kind, body, messages, want_say)

        # 气泡已经说过了，这轮只要收尾；再走一次 AstrBot 就是重复回复
        if said:
            log("[proxy] pet_say 已执行过，本轮直接收尾")
            self._reply_empty(kind, model, want_stream)
            return

        # 纯内部事件：不打扰模型
        if not text:
            if DEBUG:
                log(f"[proxy] {kind} 跳过 stream={want_stream}")
            self._reply_empty(kind, model, want_stream)
            return

        log(f"[proxy] {kind} -> {text[:60]!r}")

        cached = self._cached(text)
        if cached is not None:
            log(f"[proxy] 命中去重（{self.RECENT_TTL:.0f}s 内问过同样的）")
            self._deliver(kind, model, cached, want_say, want_stream)
            return

        started = time.time()
        try:
            reply = ask_bridge(text)
        except Exception as error:  # noqa: BLE001
            log(f"[proxy] 失败 {type(error).__name__}: {error}")
            self._send_json(502, {"error": {"message": str(error)[:200]}})
            return
        log(f"[proxy] <- {len(reply)} 字，用时 {time.time()-started:.1f}s")
        self._remember(text, reply)

        self._deliver(kind, model, reply, want_say, want_stream)

    # ------------------------------------------------------------- 投递

    def _reply_empty(self, kind, model, want_stream):
        if want_stream:
            self._stream_empty(kind, model)
        else:
            self._send_json(200, responses_body(model, "") if kind == "responses" else chat_body(model, ""))

    def _deliver(self, kind, model, reply, want_say, want_stream):
        """want_say：把回复包成 pet_say 工具调用，让桌宠真的开口。"""
        script = _bubble_script(reply) if want_say else ""
        if script:
            log(f"[proxy] pet_say -> {script[:60]!r}")
            if want_stream:
                self._stream_say(model, script)
            else:
                self._send_json(200, say_call_body(model, script))
            return
        if want_stream:
            self._stream(kind, model, reply)
        else:
            self._send_json(200, responses_body(model, reply) if kind == "responses" else chat_body(model, reply))

    # ------------------------------------------------------------- SSE

    def _start_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def _chunk(self, payload: bytes):
        self.wfile.write(f"{len(payload):X}\r\n".encode() + payload + b"\r\n")
        self.wfile.flush()

    def _sse(self, obj, event=None):
        data = ""
        if event:
            data += f"event: {event}\n"
        data += "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"
        self._chunk(data.encode("utf-8"))

    def _sse_done(self):
        self._chunk(b"data: [DONE]\n\n")
        self._chunk(b"")

    def _stream_empty(self, kind, model):
        self._start_sse()
        if kind == "responses":
            self._sse(
                {"type": "response.completed", "response": responses_body(model, "")},
                event="response.completed",
            )
        else:
            self._sse({
                "id": "coopanion-bridge",
                "object": "chat.completion.chunk",
                "model": model or "astrbot",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            })
        self._sse_done()

    def _stream(self, kind, model, reply):
        """按 Co 请求的格式回，而且一次给完。

        chat 分支的解析器只认 choices[].finish_reason，responses 分支只认带 type 的事件。
        给错格式它会判失败并重发整轮。一次给完是因为逐块推时它等不到 finish_reason 就断连。
        """
        self._start_sse()

        if kind == "responses":
            self._sse(
                {"type": "response.output_text.delta", "delta": reply},
                event="response.output_text.delta",
            )
            self._sse(
                {"type": "response.completed", "response": responses_body(model, reply)},
                event="response.completed",
            )
        else:
            self._sse({
                "id": "coopanion-bridge",
                "object": "chat.completion.chunk",
                "model": model or "astrbot",
                "choices": [{
                    "index": 0,
                    "delta": {"role": "assistant", "content": reply},
                    "finish_reason": None,
                }],
            })
            self._sse({
                "id": "coopanion-bridge",
                "object": "chat.completion.chunk",
                "model": model or "astrbot",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            })

        self._sse_done()

    def _stream_say(self, model, script):
        """chat 协议下把 pet_say 当一个工具调用推回去；同样一次给完。"""
        self._start_sse()
        self._sse({
            "id": "coopanion-bridge",
            "object": "chat.completion.chunk",
            "model": model or "astrbot",
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
                            "arguments": json.dumps({"script": script}, ensure_ascii=False),
                        },
                    }],
                },
                "finish_reason": None,
            }],
        })
        self._sse({
            "id": "coopanion-bridge",
            "object": "chat.completion.chunk",
            "model": model or "astrbot",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
        })
        self._sse_done()

    def log_message(self, *args):
        pass


def _redirect_log() -> None:
    """自己写日志：用计划任务、wmic 这类脱离 shell 的方式启动时也留得下记录。"""
    path = os.environ.get("COOPANION_LOG")
    if not path:
        here = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(os.path.dirname(here), "logs", "coopanion_proxy.log")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        stream = open(path, "a", encoding="utf-8", buffering=1)
        sys.stdout = stream
        sys.stderr = stream
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("COOPANION_PORT", "8898"))
    _redirect_log()
    log(f"coopanion-proxy listening on http://127.0.0.1:{port}")
    log(f"  桥地址 {BRIDGE}｜超时 {TIMEOUT:.0f}s｜去重 {DEDUPE_TTL:.0f}s")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
