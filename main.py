import time
from collections import defaultdict, deque
from typing import Dict, List, Tuple

from astrbot.api import AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.event.filter import EventMessageType, PermissionType
from astrbot.api.star import Context, Star, register
from astrbot.api.provider import ProviderRequest


def _parse_limit_list(raw: list) -> Dict[str, int]:
    """将 ["id:count", ...] 格式的列表解析为 {id: count} 字典。"""
    result = {}
    for entry in raw:
        entry = str(entry).strip()
        if ":" not in entry:
            continue
        parts = entry.split(":", 1)
        try:
            result[parts[0].strip()] = int(parts[1].strip())
        except (ValueError, IndexError):
            continue
    return result


def _dump_limit_dict(d: Dict[str, int]) -> List[str]:
    """将 {id: count} 字典序列化为 ["id:count", ...] 列表。"""
    return [f"{k}:{v}" for k, v in d.items()]


@register("astrbot_plugin_rate_limit", "Antigravity", "限制用户请求 LLM 的频率，支持白名单和分组限频", "1.1.0")
class RateLimitPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._reload_config()

        # 内存中的滑动窗口计数器: user_id -> deque[timestamp]
        self._request_records: dict[str, deque] = defaultdict(deque)

    def _reload_config(self):
        """从配置对象加载/重新加载所有参数。"""
        self.max_requests: int = self.config.get("max_requests", 6)
        self.time_window: int = self.config.get("time_window_seconds", 60)
        self.whitelist: list = self.config.get("whitelist", [])
        self.group_limits: Dict[str, int] = _parse_limit_list(self.config.get("group_limits", []))
        self.user_limits: Dict[str, int] = _parse_limit_list(self.config.get("user_limits", []))
        self.tip_message: str = self.config.get(
            "tip_message",
            "⚠️ 请求过于频繁，请在 {cooldown} 秒后再试。（限制：{window} 秒内最多 {max} 次）"
        )

    def _save_limits(self):
        """将 group_limits 和 user_limits 字典序列化后保存到配置。"""
        self.config["group_limits"] = _dump_limit_dict(self.group_limits)
        self.config["user_limits"] = _dump_limit_dict(self.user_limits)
        self.config.save_config()

    # ─── 核心逻辑 ────────────────────────────────────────────────

    def _resolve_max_requests(self, user_id: str, group_id: str | None) -> int:
        """根据优先级解析该用户的最大请求数。

        优先级: 用户自定义 > 群组自定义 > 全局默认
        """
        if user_id in self.user_limits:
            return self.user_limits[user_id]
        if group_id and group_id in self.group_limits:
            return self.group_limits[group_id]
        return self.max_requests

    def _check_rate_limit(self, user_id: str, max_req: int, now: float = None) -> Tuple[bool, float]:
        """检查用户是否超出频率限制。

        Returns:
            (allowed, cooldown_seconds)
        """
        if now is None:
            now = time.time()
        window_start = now - self.time_window
        records = self._request_records[user_id]

        while records and records[0] <= window_start:
            records.popleft()

        if len(records) >= max_req:
            cooldown = records[0] - window_start
            return False, round(cooldown, 1)

        records.append(now)
        return True, 0.0

    # ─── Hook: LLM 请求前拦截 ────────────────────────────────────

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, request: ProviderRequest):
        """在 LLM 请求发送前检查频率限制。"""
        user_id = event.get_sender_id()

        if user_id in self.whitelist:
            return

        group_id = event.get_group_id()
        max_req = self._resolve_max_requests(user_id, group_id)
        allowed, cooldown = self._check_rate_limit(user_id, max_req)

        if not allowed:
            tip = self.tip_message.format(
                cooldown=cooldown,
                max=max_req,
                window=self.time_window,
            )
            await event.send(event.plain_result(tip))
            event.stop_event()

    # ─── 管理指令组 /rl ──────────────────────────────────────────

    @filter.command_group("rl")
    def rl_group(self):
        pass

    @rl_group.command("status")
    @filter.permission_type(PermissionType.ADMIN)
    async def rl_status(self, event: AstrMessageEvent):
        """查看当前频率限制状态。"""
        self._reload_config()
        active_users = sum(1 for q in self._request_records.values() if q)
        lines = [
            "📊 LLM 频率限制状态",
            f"├ 全局默认: {self.max_requests} 次/{self.time_window} 秒",
            f"├ 群组自定义: {len(self.group_limits)} 个",
            f"├ 用户自定义: {len(self.user_limits)} 个",
            f"├ 白名单人数: {len(self.whitelist)}",
            f"└ 当前活跃用户数: {active_users}",
        ]
        if self.group_limits:
            lines.append("  📁 群组限制:")
            for gid, limit in self.group_limits.items():
                lines.append(f"    · {gid}: {limit} 次")
        if self.user_limits:
            lines.append("  👤 用户限制:")
            for uid, limit in self.user_limits.items():
                lines.append(f"    · {uid}: {limit} 次")
        yield event.plain_result("\n".join(lines))

    # ── 白名单管理 ──

    @rl_group.command("wl_add")
    @filter.permission_type(PermissionType.ADMIN)
    async def rl_whitelist_add(self, event: AstrMessageEvent, user_id: str):
        """添加用户到白名单。"""
        if user_id in self.whitelist:
            yield event.plain_result(f"ℹ️ 用户 {user_id} 已在白名单中。")
            return
        self.whitelist.append(user_id)
        self.config["whitelist"] = self.whitelist
        self.config.save_config()
        yield event.plain_result(f"✅ 已将用户 {user_id} 添加到白名单。")

    @rl_group.command("wl_del")
    @filter.permission_type(PermissionType.ADMIN)
    async def rl_whitelist_remove(self, event: AstrMessageEvent, user_id: str):
        """从白名单移除用户。"""
        if user_id not in self.whitelist:
            yield event.plain_result(f"ℹ️ 用户 {user_id} 不在白名单中。")
            return
        self.whitelist.remove(user_id)
        self.config["whitelist"] = self.whitelist
        self.config.save_config()
        self._request_records.pop(user_id, None)
        yield event.plain_result(f"✅ 已将用户 {user_id} 从白名单移除。")

    @rl_group.command("wl_list")
    @filter.permission_type(PermissionType.ADMIN)
    async def rl_whitelist_list(self, event: AstrMessageEvent):
        """查看白名单列表。"""
        self._reload_config()
        if not self.whitelist:
            yield event.plain_result("📋 白名单为空。")
            return
        lines = ["📋 白名单用户列表:"]
        for i, uid in enumerate(self.whitelist, 1):
            lines.append(f"  {i}. {uid}")
        yield event.plain_result("\n".join(lines))

    # ── 全局参数设置 ──

    @rl_group.command("set_rate")
    @filter.permission_type(PermissionType.ADMIN)
    async def rl_set_rate(self, event: AstrMessageEvent, count: int):
        """设置全局默认最大请求次数。"""
        if count < 1:
            yield event.plain_result("❌ 最大请求次数必须 ≥ 1。")
            return
        self.max_requests = count
        self.config["max_requests"] = count
        self.config.save_config()
        yield event.plain_result(f"✅ 全局最大请求次数已设置为 {count} 次/{self.time_window} 秒。")

    @rl_group.command("set_window")
    @filter.permission_type(PermissionType.ADMIN)
    async def rl_set_window(self, event: AstrMessageEvent, seconds: int):
        """设置时间窗口长度（秒）。"""
        if seconds < 1:
            yield event.plain_result("❌ 时间窗口必须 ≥ 1 秒。")
            return
        self.time_window = seconds
        self.config["time_window_seconds"] = seconds
        self.config.save_config()
        self._request_records.clear()
        yield event.plain_result(f"✅ 时间窗口已设置为 {seconds} 秒（已重置所有计数器）。")

    # ── 群组限制管理 ──

    @rl_group.command("group_set")
    @filter.permission_type(PermissionType.ADMIN)
    async def rl_group_set(self, event: AstrMessageEvent, group_id: str, count: int):
        """为指定群组设置自定义频率限制。用法: /rl group_set <群组ID> <次数>"""
        if count < 1:
            yield event.plain_result("❌ 最大请求次数必须 ≥ 1。")
            return
        self.group_limits[group_id] = count
        self._save_limits()
        yield event.plain_result(f"✅ 群组 {group_id} 的频率限制已设置为 {count} 次/{self.time_window} 秒。")

    @rl_group.command("group_del")
    @filter.permission_type(PermissionType.ADMIN)
    async def rl_group_del(self, event: AstrMessageEvent, group_id: str):
        """移除指定群组的自定义频率限制。用法: /rl group_del <群组ID>"""
        if group_id not in self.group_limits:
            yield event.plain_result(f"ℹ️ 群组 {group_id} 没有自定义限制。")
            return
        del self.group_limits[group_id]
        self._save_limits()
        yield event.plain_result(f"✅ 已移除群组 {group_id} 的自定义限制，恢复全局默认 ({self.max_requests} 次)。")

    @rl_group.command("group_list")
    @filter.permission_type(PermissionType.ADMIN)
    async def rl_group_list(self, event: AstrMessageEvent):
        """查看所有群组的自定义频率限制。"""
        self._reload_config()
        if not self.group_limits:
            yield event.plain_result("📁 没有群组自定义限制，全部使用全局默认。")
            return
        lines = [f"📁 群组自定义限制 (默认: {self.max_requests} 次):"]
        for gid, limit in self.group_limits.items():
            lines.append(f"  · {gid}: {limit} 次/{self.time_window} 秒")
        yield event.plain_result("\n".join(lines))

    # ── 用户限制管理 ──

    @rl_group.command("user_set")
    @filter.permission_type(PermissionType.ADMIN)
    async def rl_user_set(self, event: AstrMessageEvent, user_id: str, count: int):
        """为指定用户设置自定义频率限制（优先级最高）。用法: /rl user_set <用户ID> <次数>"""
        if count < 1:
            yield event.plain_result("❌ 最大请求次数必须 ≥ 1。")
            return
        self.user_limits[user_id] = count
        self._save_limits()
        self._request_records.pop(user_id, None)
        yield event.plain_result(f"✅ 用户 {user_id} 的频率限制已设置为 {count} 次/{self.time_window} 秒。")

    @rl_group.command("user_del")
    @filter.permission_type(PermissionType.ADMIN)
    async def rl_user_del(self, event: AstrMessageEvent, user_id: str):
        """移除指定用户的自定义频率限制。用法: /rl user_del <用户ID>"""
        if user_id not in self.user_limits:
            yield event.plain_result(f"ℹ️ 用户 {user_id} 没有自定义限制。")
            return
        del self.user_limits[user_id]
        self._save_limits()
        yield event.plain_result(f"✅ 已移除用户 {user_id} 的自定义限制。")

    @rl_group.command("user_list")
    @filter.permission_type(PermissionType.ADMIN)
    async def rl_user_list(self, event: AstrMessageEvent):
        """查看所有用户的自定义频率限制。"""
        self._reload_config()
        if not self.user_limits:
            yield event.plain_result("👤 没有用户自定义限制。")
            return
        lines = [f"👤 用户自定义限制 (优先级最高):"]
        for uid, limit in self.user_limits.items():
            lines.append(f"  · {uid}: {limit} 次/{self.time_window} 秒")
        yield event.plain_result("\n".join(lines))
