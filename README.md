# astrbot_plugin_rate_limit

AstrBot 插件 —— 限制用户请求 LLM 的频率，防止滥用。支持白名单功能。

## 功能

- 🚦 **频率限制**：基于滑动窗口算法，限制每个用户在指定时间窗口内的 LLM 请求次数（默认 60 秒内 6 次）
- 📋 **白名单**：白名单用户不受频率限制
- ⚙️ **WebUI 配置**：所有参数均可在 AstrBot WebUI 中可视化配置
- 🔧 **运行时管理**：通过聊天指令动态调整参数和管理白名单

## 安装

将本插件目录放入 AstrBot 的 `data/plugins/` 目录下，重启 AstrBot 即可。

或者在 AstrBot WebUI 的插件市场中搜索安装。

## 配置

在 AstrBot WebUI 中进入插件配置页面，可修改以下参数：

| 参数 | 说明 | 默认值 |
|---|---|---|
| `max_requests` | 时间窗口内允许的最大请求次数 | 6 |
| `time_window_seconds` | 时间窗口长度（秒） | 60 |
| `whitelist` | 白名单用户 ID 列表 | [] |
| `tip_message` | 超限提示消息模板 | 见下方 |

### 提示消息模板变量

- `{cooldown}` —— 剩余冷却秒数
- `{max}` —— 最大请求次数
- `{window}` —— 时间窗口秒数

默认模板：`⚠️ 请求过于频繁，请在 {cooldown} 秒后再试。（限制：{window} 秒内最多 {max} 次）`

## 管理指令

以下指令需要管理员权限：

| 指令 | 说明 |
|---|---|
| `/rl status` | 查看当前配置和限制状态 |
| `/rl whitelist add <user_id>` | 添加用户到白名单 |
| `/rl whitelist remove <user_id>` | 从白名单移除用户 |
| `/rl whitelist list` | 查看白名单列表 |
| `/rl set rate <次数>` | 设置最大请求次数 |
| `/rl set window <秒数>` | 设置时间窗口长度 |

## License

MIT
