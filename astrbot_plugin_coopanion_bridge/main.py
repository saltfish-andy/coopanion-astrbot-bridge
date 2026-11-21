"""AstrBot 插件：把 Coopanion 桌宠接进 AstrBot 的回复链路。

做了什么：
  1. 起一个本地 HTTP 服务（默认 8899），代理把「人说的话」POST 到 /ask
  2. 收到后伪造一条 QQ 私聊事件，打上标记，塞进 AstrBot 的事件总线
  3. AstrBot 正常跑完人格、记忆、工具这些流水线
  4. 在结果装饰阶段把回复截下来，交回给代理；同时挡住，不让它发到 QQ

为什么不在发消息的地方直接拦：
  AstrBot 在流水线中途会把 event.send 重置回自己的 send_parts，
  只在 on_llm_request 里替换会失效。所以 on_decorating_result 里要再挂一次。
  详见 docs/troubleshooting.md。
"""

from __future__ import annotations

import asyncio
import json
import logging
import pathlib
import uuid

from aiohttp import web

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType

logger = logging.getLogger("astrbot")

# 打在伪造事件上的标记。所有钩子都先检查它，避免影响真实的 QQ 消息。
FLAG = "_coopanion_desktop"

PLUGIN_DIR = pathlib.Path(__file__).resolve().parent
CONFIG_FILE = PLUGIN_DIR / "config.json"

DEFAULTS = {
    # 机器人自己的 QQ 号。AstrBot 靠它定位平台实例。
    "self_id": "",
    # 以谁的身份跟 AstrBot 说话。填你自己的号，人格和记忆才认得出来是谁。
    "owner_id": "",
    "owner_nick": "",
    # 桥监听的端口，要和代理里的地址对上。
    "port": 8899,
    # 等 AstrBot 生成回复的秒数。
    "timeout": 180,
}


def load_config() -> dict:
    """先读插件目录里的 config.json，缺的键用默认值补。"""
    config = dict(DEFAULTS)
    if CONFIG_FILE.exists():
        try:
            config.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except Exception as error:  # noqa: BLE001
            logger.warning("[Coopanion] config.json 读不动，用默认值：%s", error)
    return config


@register("coopanion_bridge", "local", "Coopanion 桥", "1.1.0", "local")
class CoopanionBridge(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        # 不叫 self.config：Star 基类自己可能占着这个名字
        self.settings = load_config()
        self._runner = None
        self._lock = asyncio.Lock()
        self._future = None

    # ---------------------------------------------------------------- 生命周期

    async def initialize(self):
        port = int(self.settings.get("port") or DEFAULTS["port"])
        app = web.Application()
        app.router.add_post("/ask", self.handle_ask)
        app.router.add_get("/health", self.handle_health)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        await web.TCPSite(self._runner, "127.0.0.1", port).start()
        logger.info(
            "[Coopanion] 桥已启动 http://127.0.0.1:%d 平台=%s",
            port, self._platform_id(),
        )
        if not self.settings.get("owner_id") or not self.settings.get("self_id"):
            logger.warning(
                "[Coopanion] 还没配 owner_id / self_id，"
                "请把 config.example.json 复制成 config.json 后填上"
            )

    async def terminate(self):
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # ------------------------------------------------------------------ 平台

    def _find_platform(self):
        """找到 aiocqhttp 那个平台实例，伪造的事件要挂在它下面。"""
        try:
            platforms = self.context.platform_manager.platform_insts
        except Exception:  # noqa: BLE001
            return None
        for platform in platforms:
            try:
                if platform.meta().name == "aiocqhttp":
                    return platform
            except Exception:  # noqa: BLE001
                continue
        return None

    def _platform_id(self):
        platform = self._find_platform()
        try:
            return platform.meta().id if platform else "-"
        except Exception:  # noqa: BLE001
            return "-"

    # ------------------------------------------------------------------ 路由

    async def handle_health(self, request):
        return web.json_response({"ok": True, "platform": self._platform_id()})

    async def handle_ask(self, request):
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"ok": False, "error": "bad json"}, status=400)

        text = str(body.get("text") or "").strip()
        if not text:
            return web.json_response({"ok": False, "error": "empty text"}, status=400)

        platform = self._find_platform()
        if platform is None:
            return web.json_response({"ok": False, "error": "aiocqhttp not ready"}, status=503)

        owner_id = str(self.settings.get("owner_id") or "")
        self_id = str(self.settings.get("self_id") or "")
        if not owner_id or not self_id:
            return web.json_response({"ok": False, "error": "owner_id / self_id 未配置"}, status=503)

        loop = asyncio.get_running_loop()
        future = loop.create_future()
        async with self._lock:
            if self._future is not None and not self._future.done():
                return web.json_response({"ok": False, "error": "busy"}, status=429)
            self._future = future

        try:
            message = AstrBotMessage()
            message.type = MessageType.FRIEND_MESSAGE
            message.session_id = owner_id
            message.self_id = self_id
            message.message_id = "coopanion-" + uuid.uuid4().hex
            message.message_str = text
            message.message = [Plain(text=text)]
            message.sender = MessageMember(
                user_id=owner_id,
                nickname=str(self.settings.get("owner_nick") or ""),
            )
            message.raw_message = None

            event = platform.create_event(message)
            event.set_extra(FLAG, True)
            platform.commit_event(event)
            logger.info(
                "[Coopanion] 注入消息 umo=%s text=%r",
                getattr(event, "unified_msg_origin", "?"), text[:40],
            )

            reply = await asyncio.wait_for(future, timeout=int(self.settings.get("timeout") or 180))
        except asyncio.TimeoutError:
            return web.json_response({"ok": False, "error": "timeout"}, status=504)
        except Exception as error:  # noqa: BLE001
            logger.warning("[Coopanion] 注入失败 %s: %s", type(error).__name__, error)
            return web.json_response({"ok": False, "error": str(error)[:200]}, status=500)
        finally:
            async with self._lock:
                self._future = None

        return web.json_response({"ok": True, "reply": reply})

    # ------------------------------------------------------------------ 拦截

    async def _silent_send(self, chain, *args, **kwargs):
        """顶替 event.send：只把文本交回给桌宠，不往 QQ 发。"""
        text = ""
        for part in getattr(chain, "chain", None) or ():
            value = getattr(part, "text", None)
            if isinstance(value, str):
                text += value
        logger.info("[Coopanion] 拦截发送，文本 %d 字", len(text))
        # 有没有文本都要把 future 完成掉，否则调用方会一直挂到超时
        await self._resolve(text)
        return None

    async def _resolve(self, text):
        async with self._lock:
            future = self._future
        if future is None:
            logger.info("[Coopanion] 没有等待中的请求，丢弃这次回复")
            return
        if future.done():
            logger.info("[Coopanion] 请求已完成，忽略重复回复")
            return
        future.set_result(text or "")
        logger.info("[Coopanion] 已交回回复 %d 字", len(text or ""))

    @filter.on_llm_request(priority=-2000000)
    async def arm_intercept(self, event: AstrMessageEvent, req=None):
        """尽量早地换掉 event.send。

        这一步挡不住（AstrBot 往下跑会重置），但能让大多数发送走我们的分支，
        而且能提前 fail fast。真正的保险在 capture 里。
        """
        if not event.get_extra(FLAG):
            return
        event.send = self._silent_send

    @filter.on_decorating_result(priority=-2000000)
    async def capture(self, event: AstrMessageEvent):
        """在 RespondStage 之前截住结果，并重新挂上发送拦截。

        这个钩子跑在 RespondStage 前面（日志里 Prepare to send 出现在它之后），
        所以在这里设 event.send 是有效的——这是拦住 QQ 发送的关键一步。
        """
        if not event.get_extra(FLAG):
            return

        result = event.get_result()
        text = ""
        for part in getattr(result, "chain", None) or ():
            value = getattr(part, "text", None)
            if isinstance(value, str):
                text += value
        logger.info("[Coopanion] 结果装饰阶段，结果 %d 字", len(text))
        if text.strip():
            await self._resolve(text)

        event.send = self._silent_send
        event.stop_event()
