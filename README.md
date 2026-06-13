# astrbot_plugin_rate_limit

AstrBot 插件 —— 限制用户请求 LLM 的频率，防止滥用。支持白名单功能。

## 功能

- 🚦 **频率限制**：基于滑动窗口算法，限制每个用户在指定时间窗口内的 LLM 请求次数（默认 60 秒内 6 次）
- 🏢 **群组总量限制**：限制群组内所有用户合计的请求次数，支持拉黑群组（设为 0）
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
| `enable_user_limit` | 启用个人频率限制 | true |
| `enable_group_total_limit` | 启用群组总量限制 | true |
| `max_requests` | 时间窗口内允许的最大请求次数 | 6 |
| `time_window_seconds` | 时间窗口长度（秒） | 60 |
| `default_group_total` | 全局默认群组总量限制（0=不限制） | 0 |
| `whitelist` | 白名单用户 ID 列表 | [] |
| `group_limits` | 群组内每个用户的自定义频率限制 | [] |
| `group_total_limits` | 群组整体总请求次数限制（0=拉黑） | [] |
| `user_limits` | 用户自定义频率限制 | [] |
| `tip_message` | 超限提示消息模板 | 见下方 |
| `group_tip_message` | 群组总量超限提示消息模板 | 见下方 |

### 提示消息模板变量

**用户级限制提示 (`tip_message`)**：
- `{cooldown}` —— 剩余冷却秒数
- `{max}` —— 最大请求次数
- `{window}` —— 时间窗口秒数

默认模板：`⚠️ 请求过于频繁，请在 {cooldown} 秒后再试。（限制：{window} 秒内最多 {max} 次）`

**群组总量限制提示 (`group_tip_message`)**：
- `{cooldown}` —— 剩余冷却秒数
- `{max}` —— 群组总限制次数
- `{window}` —— 时间窗口秒数

默认模板：`⚠️ 本群请求过于频繁，请在 {cooldown} 秒后再试。（群限制：{window} 秒内合计最多 {max} 次）`

### 群组拉黑功能

在 `group_total_limits` 中将群组限制设为 `0` 可以拉黑该群组，完全禁止该群使用 LLM 功能。

例如：`1103526495:0` 表示拉黑群组 1103526495。

## 管理指令

以下指令需要管理员权限：

| 指令 | 说明 |
|---|---|
| `/rl status` | 查看当前配置和限制状态 |
| `/rl wl_add <user_id>` | 添加用户到白名单 |
| `/rl wl_del <user_id>` | 从白名单移除用户 |
| `/rl wl_list` | 查看白名单列表 |
| `/rl set_rate <次数>` | 设置全局默认最大请求次数 |
| `/rl set_window <秒数>` | 设置时间窗口长度 |
| `/rl set_gtotal_default <次数>` | 设置全局默认群总量限制（0=不限制） |
| `/rl group_set <群组ID> <次数>` | 设置群组内每用户限制 |
| `/rl group_del <群组ID>` | 移除群组内每用户限制 |
| `/rl group_list` | 查看所有群组每用户限制 |
| `/rl gtotal_set <群组ID> <次数>` | 设置群组总量限制（0=拉黑） |
| `/rl gtotal_del <群组ID>` | 移除群组总量限制 |
| `/rl gtotal_list` | 查看所有群组总量限制 |
| `/rl user_set <用户ID> <次数>` | 设置用户自定义限制（优先级最高） |
| `/rl user_del <用户ID>` | 移除用户自定义限制 |
| `/rl user_list` | 查看所有用户自定义限制 |

## License

MIT
