# 更新记录

## 1.1.0

- 优化发送拦截：在 `on_decorating_result` 里再挂一次 `event.send`，修掉 QQ 收到桌宠回复的问题（AstrBot 会在流水线中途重置它）
- 移除 `on_decorating_result` 里重复的兜底逻辑，合并成 `_silent_send`
- 配置从环境变量读：`COOPANION_BRIDGE` / `COOPANION_TIMEOUT` / `COOPANION_DEDUPE_TTL` / `COOPANION_DEBUG`
- 代理自己写日志文件，不再依赖 shell 重定向
- 插件配置从硬编码改为 `config.json`，首次启动没配会打警告

## 1.0.0

- 首版：代理 + AstrBot 插件
- 支持从 `role=tool` 的 `[typed]` 里提取用户输入
- 自动过滤内部事件
- 按文本去重，挡住 Coopanion 的重试
- 把回复包成 `pet_say` 工具调用，让桌宠真的开口
- 支持 chat 和 responses 两种协议
