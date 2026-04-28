import time
from collections import defaultdict, deque
from itertools import islice

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.event.filter import PermissionType
from astrbot.api.star import Context, Star
from astrbot.api.provider import ProviderRequest


def _load_limits(raw) -> dict[str, int]:
    """加载限制配置，同时支持字典格式和旧版列表格式。

    字典格式: {"id": count, ...}
    旧版列表格式: ["id:count", ...]
    自动过滤 key 为空或 value <= 0 的条目。
    """
    if isinstance(raw, dict):
        return {str(k).strip(): int(v) for k, v in raw.items()
                if str(k).strip() and _safe_int(v, 0) > 0}
    if not isinstance(raw, list):
        return {}
    # 兼容旧版 ["id:count", ...] 格式
    result = {}
    for entry in raw:
        entry = str(entry).strip()
        if ":" not in entry:
            continue
        parts = entry.rsplit(":", 1)
        try:
            key = parts[0].strip()
            val = int(parts[1].strip())
            if key and val > 0:
                result[key] = val
        except (ValueError, IndexError):
            continue
    return result


def _safe_bool(val, default: bool) -> bool:
    """安全解析布尔配置值，支持字符串 'false'/'0' 等。"""
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() not in ('false', '0', 'no', 'off', '')
    if val is None:
        return default
    return bool(val)

def _safe_int(val, default: int) -> int:
    """安全将配置值转为 int，失败时返回默认值。"""
    try:
        return int(val)
    except (TypeError, ValueError):
        logger.warning(f"[rate_limit] 配置值 '{val}' 无法转换为整数，使用默认值 {default}")
        return default





class RateLimitPlugin(Star):
    _CLEANUP_INTERVAL = 300  # 自动清理间隔（秒）
    _CLEANUP_BATCH = 200     # 单次清理最多处理的 key 数
    _MAX_DISPLAY = 30        # 列表命令最大显示条数

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._reload_config()

        # 用户级滑动窗口: user_id -> deque[timestamp]
        self._request_records: dict[str, deque[float]] = defaultdict(deque)
        # 群组级滑动窗口: group_id -> deque[timestamp]
        self._group_records: dict[str, deque[float]] = defaultdict(deque)
        # 上次自动清理时间
        self._last_cleanup: float = time.monotonic()
        # 清理游标：在多轮清理中轮转遍历所有 key
        self._cleanup_cursor: int = 0

    def _reload_config(self):
        """从配置对象加载/重新加载所有参数。"""
        self.enable_user_limit: bool = _safe_bool(self.config.get("enable_user_limit", True), True)
        self.enable_group_total_limit: bool = _safe_bool(self.config.get("enable_group_total_limit", True), True)
        self.max_requests: int = max(1, _safe_int(self.config.get("max_requests", 6), 6))
        self.time_window: int = max(1, _safe_int(self.config.get("time_window_seconds", 60), 60))
        self.default_group_total: int = max(0, _safe_int(self.config.get("default_group_total", 0), 0))
        # 白名单 ID 统一转 str 并去重
        self.whitelist: set[str] = {str(x).strip() for x in (self.config.get("whitelist") or []) if str(x).strip()}
        self.group_limits: dict[str, int] = _load_limits(self.config.get("group_limits") or {})
        self.group_total_limits: dict[str, int] = _load_limits(self.config.get("group_total_limits") or {})
        self.user_limits: dict[str, int] = _load_limits(self.config.get("user_limits") or {})
        self.tip_message: str = self.config.get("tip_message") or \
            "⚠️ 请求过于频繁，请在 {cooldown} 秒后再试。（限制：{window} 秒内最多 {max} 次）"
        self.group_tip_message: str = self.config.get("group_tip_message") or \
            "⚠️ 本群请求过于频繁，请在 {cooldown} 秒后再试。（群限制：{window} 秒内合计最多 {max} 次）"
        logger.debug(f"[rate_limit] 配置已加载: 用户限制={self.enable_user_limit}, "
                     f"群总量限制={self.enable_group_total_limit}, "
                     f"每用户={self.max_requests}/{self.time_window}s, "
                     f"群默认总量={self.default_group_total}")

    def _save_limits(self):
        """将所有限制字典直接保存到配置。保存失败时回滚内存状态。"""
        backup = (
            self.config.get("group_limits"),
            self.config.get("group_total_limits"),
            self.config.get("user_limits"),
        )
        self.config["group_limits"] = dict(self.group_limits)
        self.config["group_total_limits"] = dict(self.group_total_limits)
        self.config["user_limits"] = dict(self.user_limits)
        try:
            self.config.save_config()
        except Exception as e:
            # 回滚
            self.config["group_limits"], self.config["group_total_limits"], self.config["user_limits"] = backup
            self._reload_config()
            logger.error(f"[rate_limit] 保存配置失败，已回滚: {e}")
            raise

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

    def _resolve_group_total(self, group_id: str) -> int:
        """解析群组的总量限制。

        优先级: 群组自定义总量 > 全局默认群总量
        返回 0 表示不限制。
        """
        if group_id in self.group_total_limits:
            return self.group_total_limits[group_id]
        return self.default_group_total

    @staticmethod
    def _sliding_window_check(records: deque, max_req: int, time_window: int,
                              now: float) -> tuple[bool, float]:
        """通用滑动窗口检查（不记录，仅判断 + 返回冷却时间）。"""
        if max_req <= 0:
            return False, 0.0
        window_start = now - time_window
        while records and records[0] <= window_start:
            records.popleft()
        if len(records) >= max_req:
            cooldown = records[0] - window_start
            return False, round(cooldown, 1)
        return True, 0.0

    @staticmethod
    def _sliding_window_record(records: deque, now: float):
        """记录一次请求时间戳。"""
        records.append(now)

    def _maybe_auto_cleanup(self, now: float):
        """定期自动清理过期记录和空 key，防止内存膨胀。

        使用游标轮转 + 批量限制，确保所有 key 在多轮内都能被覆盖。
        空 key 在遍历中即时删除，避免额外全量扫描。
        """
        if now - self._last_cleanup < self._CLEANUP_INTERVAL:
            return
        self._last_cleanup = now
        window_start = now - self.time_window
        budget = self._CLEANUP_BATCH

        for d in (self._request_records, self._group_records):
            keys = list(d.keys())
            if not keys:
                continue
            total = len(keys)
            start = self._cleanup_cursor % total if total else 0
            # 从游标位置开始轮转遍历
            indices = list(range(start, total)) + list(range(0, start))
            to_delete = []
            for idx in indices[:budget]:
                k = keys[idx]
                records = d[k]
                while records and records[0] <= window_start:
                    records.popleft()
                if not records:
                    to_delete.append(k)
            for k in to_delete:
                del d[k]
            budget -= min(budget, len(indices))
            if budget <= 0:
                break

        self._cleanup_cursor += self._CLEANUP_BATCH

    # ─── Hook: LLM 请求前拦截 ────────────────────────────────────

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, request: ProviderRequest):
        """在 LLM 请求发送前检查频率限制。

        检查顺序:
        1. 白名单 → 跳过所有检查
        2. 用户级频率限制（用户自定义 > 群组自定义 > 全局默认）
        3. 群组总量限制（群内所有用户共享计数器）
        两个检查都通过后才记录请求。
        """
        user_id = str(event.get_sender_id())

        # 白名单用户跳过所有检查
        if user_id in self.whitelist:
            return

        now = time.monotonic()
        group_id = event.get_group_id()
        if group_id is not None:
            group_id = str(group_id)

        # 定期自动清理过期记录
        self._maybe_auto_cleanup(now)

        # ── 检查 1: 用户级频率 ──
        if self.enable_user_limit:
            max_req = self._resolve_max_requests(user_id, group_id)
            user_records = self._request_records[user_id]
            allowed, cooldown = self._sliding_window_check(
                user_records, max_req, self.time_window, now
            )
            if not allowed:
                try:
                    tip = self.tip_message.format(
                        cooldown=cooldown, max=max_req, window=self.time_window
                    )
                except (KeyError, ValueError, IndexError):
                    tip = f"⚠️ 请求过于频繁，请稍后再试。"
                event.set_result(tip)
                event.stop_event()
                return

        # ── 检查 2: 群组总量 ──
        group_max = 0
        if self.enable_group_total_limit and group_id:
            group_max = self._resolve_group_total(group_id)
        if group_id and group_max > 0:
            group_records = self._group_records[group_id]
            g_allowed, g_cooldown = self._sliding_window_check(
                group_records, group_max, self.time_window, now
            )
            if not g_allowed:
                try:
                    tip = self.group_tip_message.format(
                        cooldown=g_cooldown, max=group_max, window=self.time_window
                    )
                except (KeyError, ValueError, IndexError):
                    tip = f"⚠️ 本群请求过于频繁，请稍后再试。"
                event.set_result(tip)
                event.stop_event()
                return

        # ── 两项检查都通过，记录请求 ──
        if self.enable_user_limit:
            self._sliding_window_record(self._request_records[user_id], now)
        if group_id and group_max > 0:
            self._sliding_window_record(self._group_records[group_id], now)

    # ─── 管理指令组 /rl ──────────────────────────────────────────

    @filter.command_group("rl")
    def rl_group(self):
        pass

    @rl_group.command("status")
    @filter.permission_type(PermissionType.ADMIN)
    async def rl_status(self, event: AstrMessageEvent):
        """查看当前频率限制状态。"""
        # 复用定时清理逻辑
        self._maybe_auto_cleanup(time.monotonic())
        active_users = len(self._request_records)
        active_groups = len(self._group_records)
        gt_default = f"{self.default_group_total} 次" if self.default_group_total > 0 else "不限制"
        ul_status = "✅ 开启" if self.enable_user_limit else "❌ 关闭"
        gl_status = "✅ 开启" if self.enable_group_total_limit else "❌ 关闭"
        lines = [
            "📊 LLM 频率限制状态",
            f"├ 个人限制: {ul_status}",
            f"├ 群总量限制: {gl_status}",
            f"├ 全局每用户默认: {self.max_requests} 次/{self.time_window} 秒",
            f"├ 全局群总量默认: {gt_default}/{self.time_window} 秒",
            f"├ 群组每用户自定义: {len(self.group_limits)} 个",
            f"├ 群组总量自定义: {len(self.group_total_limits)} 个",
            f"├ 用户自定义: {len(self.user_limits)} 个",
            f"├ 白名单人数: {len(self.whitelist)}",
            f"├ 当前活跃用户数: {active_users}",
            f"└ 当前活跃群组数: {active_groups}",
        ]
        if self.group_limits:
            lines.append("  📁 群组每用户限制:")
            items = list(self.group_limits.items())
            for gid, limit in items[:self._MAX_DISPLAY]:
                lines.append(f"    · {gid}: {limit} 次/人")
            if len(items) > self._MAX_DISPLAY:
                lines.append(f"    ... 省略 {len(items) - self._MAX_DISPLAY} 条")
        if self.group_total_limits:
            lines.append("  🏢 群组总量限制:")
            items = list(self.group_total_limits.items())
            for gid, limit in items[:self._MAX_DISPLAY]:
                grp_records = self._group_records.get(gid)
                used = len(grp_records) if grp_records else 0
                lines.append(f"    · {gid}: {limit} 次（已用 {used}）")
            if len(items) > self._MAX_DISPLAY:
                lines.append(f"    ... 省略 {len(items) - self._MAX_DISPLAY} 条")
        if self.user_limits:
            lines.append("  👤 用户限制:")
            items = list(self.user_limits.items())
            for uid, limit in items[:self._MAX_DISPLAY]:
                lines.append(f"    · {uid}: {limit} 次")
            if len(items) > self._MAX_DISPLAY:
                lines.append(f"    ... 省略 {len(items) - self._MAX_DISPLAY} 条")
        yield event.plain_result("\n".join(lines))

    # ── 白名单管理 ──

    @rl_group.command("wl_add")
    @filter.permission_type(PermissionType.ADMIN)
    async def rl_whitelist_add(self, event: AstrMessageEvent, user_id: str):
        """添加用户到白名单。"""
        user_id = user_id.strip()
        if not user_id:
            yield event.plain_result("❌ 用户 ID 不能为空。")
            return
        if user_id in self.whitelist:
            yield event.plain_result(f"ℹ️ 用户 {user_id} 已在白名单中。")
            return
        self.whitelist.add(user_id)
        self.config["whitelist"] = list(self.whitelist)
        try:
            self.config.save_config()
        except Exception:
            self.whitelist.discard(user_id)
            yield event.plain_result("❌ 保存配置失败。")
            return
        yield event.plain_result(f"✅ 已将用户 {user_id} 添加到白名单。")

    @rl_group.command("wl_del")
    @filter.permission_type(PermissionType.ADMIN)
    async def rl_whitelist_remove(self, event: AstrMessageEvent, user_id: str):
        """从白名单移除用户。"""
        user_id = user_id.strip()
        if user_id not in self.whitelist:
            yield event.plain_result(f"ℹ️ 用户 {user_id} 不在白名单中。")
            return
        self.whitelist.discard(user_id)
        self.config["whitelist"] = list(self.whitelist)
        try:
            self.config.save_config()
        except Exception:
            self.whitelist.add(user_id)
            yield event.plain_result("❌ 保存配置失败。")
            return
        self._request_records.pop(user_id, None)
        yield event.plain_result(f"✅ 已将用户 {user_id} 从白名单移除。")

    @rl_group.command("wl_list")
    @filter.permission_type(PermissionType.ADMIN)
    async def rl_whitelist_list(self, event: AstrMessageEvent):
        """查看白名单列表。"""
        if not self.whitelist:
            yield event.plain_result("📋 白名单为空。")
            return
        wl = sorted(self.whitelist)
        lines = ["📋 白名单用户列表:"]
        for i, uid in enumerate(wl[:self._MAX_DISPLAY], 1):
            lines.append(f"  {i}. {uid}")
        if len(wl) > self._MAX_DISPLAY:
            lines.append(f"  ... 省略 {len(wl) - self._MAX_DISPLAY} 人")
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
        self._group_records.clear()
        yield event.plain_result(f"✅ 时间窗口已设置为 {seconds} 秒（已重置所有计数器）。")

    @rl_group.command("set_gtotal_default")
    @filter.permission_type(PermissionType.ADMIN)
    async def rl_set_gtotal_default(self, event: AstrMessageEvent, count: int):
        """设置全局默认群总量限制。用法: /rl set_gtotal_default <次数> (0=不限制)"""
        if count < 0:
            yield event.plain_result("❌ 总次数必须 ≥ 0。")
            return
        self.default_group_total = count
        self.config["default_group_total"] = count
        self.config.save_config()
        if count == 0:
            yield event.plain_result("✅ 已关闭全局默认群总量限制。")
        else:
            yield event.plain_result(
                f"✅ 全局默认群总量已设置为 {count} 次/{self.time_window} 秒（单独配置的群不受影响）。"
            )

    # ── 群组每用户限制管理 ──

    @rl_group.command("group_set")
    @filter.permission_type(PermissionType.ADMIN)
    async def rl_group_set(self, event: AstrMessageEvent, group_id: str, count: int):
        """为群组设置每用户频率限制。用法: /rl group_set <群组ID> <次数>"""
        group_id = group_id.strip()
        if not group_id:
            yield event.plain_result("❌ 群组 ID 不能为空。")
            return
        if count < 1:
            yield event.plain_result("❌ 最大请求次数必须 ≥ 1。")
            return
        self.group_limits[group_id] = count
        try:
            self._save_limits()
        except Exception:
            self.group_limits.pop(group_id, None)
            yield event.plain_result("❌ 保存配置失败。")
            return
        yield event.plain_result(f"✅ 群组 {group_id} 的每用户限制已设置为 {count} 次/{self.time_window} 秒。")

    @rl_group.command("group_del")
    @filter.permission_type(PermissionType.ADMIN)
    async def rl_group_del(self, event: AstrMessageEvent, group_id: str):
        """移除群组的每用户频率限制。用法: /rl group_del <群组ID>"""
        if group_id not in self.group_limits:
            yield event.plain_result(f"ℹ️ 群组 {group_id} 没有每用户自定义限制。")
            return
        del self.group_limits[group_id]
        self._save_limits()
        yield event.plain_result(f"✅ 已移除群组 {group_id} 的每用户限制，恢复全局默认 ({self.max_requests} 次)。")

    @rl_group.command("group_list")
    @filter.permission_type(PermissionType.ADMIN)
    async def rl_group_list(self, event: AstrMessageEvent):
        """查看所有群组自定义限制。"""
        if not self.group_limits:
            yield event.plain_result("📁 没有群组每用户自定义限制。")
            return
        lines = [f"📁 群组每用户限制 (默认: {self.max_requests} 次):"]
        items = list(self.group_limits.items())
        for gid, limit in items[:self._MAX_DISPLAY]:
            lines.append(f"  · {gid}: {limit} 次/{self.time_window} 秒")
        if len(items) > self._MAX_DISPLAY:
            lines.append(f"  ... 省略 {len(items) - self._MAX_DISPLAY} 条")
        yield event.plain_result("\n".join(lines))

    # ── 群组总量限制管理 ──

    @rl_group.command("gtotal_set")
    @filter.permission_type(PermissionType.ADMIN)
    async def rl_gtotal_set(self, event: AstrMessageEvent, group_id: str, count: int):
        """为群组设置总请求次数限制。用法: /rl gtotal_set <群组ID> <总次数>"""
        group_id = group_id.strip()
        if not group_id:
            yield event.plain_result("❌ 群组 ID 不能为空。")
            return
        if count < 1:
            yield event.plain_result("❌ 总次数必须 ≥ 1。")
            return
        self.group_total_limits[group_id] = count
        try:
            self._save_limits()
        except Exception:
            self.group_total_limits.pop(group_id, None)
            yield event.plain_result("❌ 保存配置失败。")
            return
        yield event.plain_result(
            f"✅ 群组 {group_id} 的总量限制已设置为 {count} 次/{self.time_window} 秒（全群共享）。"
        )

    @rl_group.command("gtotal_del")
    @filter.permission_type(PermissionType.ADMIN)
    async def rl_gtotal_del(self, event: AstrMessageEvent, group_id: str):
        """移除群组的总量限制。用法: /rl gtotal_del <群组ID>"""
        if group_id not in self.group_total_limits:
            yield event.plain_result(f"ℹ️ 群组 {group_id} 没有总量限制。")
            return
        del self.group_total_limits[group_id]
        self._save_limits()
        self._group_records.pop(group_id, None)
        yield event.plain_result(f"✅ 已移除群组 {group_id} 的总量限制。")

    @rl_group.command("gtotal_list")
    @filter.permission_type(PermissionType.ADMIN)
    async def rl_gtotal_list(self, event: AstrMessageEvent):
        """查看所有群组总量限制。"""
        if not self.group_total_limits:
            yield event.plain_result("🏢 没有群组总量限制。")
            return
        lines = ["🏢 群组总量限制:"]
        items = list(self.group_total_limits.items())
        for gid, limit in items[:self._MAX_DISPLAY]:
            grp_records = self._group_records.get(gid)
            used = len(grp_records) if grp_records else 0
            lines.append(f"  · {gid}: {limit} 次/{self.time_window} 秒（已用 {used}）")
        if len(items) > self._MAX_DISPLAY:
            lines.append(f"  ... 省略 {len(items) - self._MAX_DISPLAY} 条")
        yield event.plain_result("\n".join(lines))

    # ── 用户限制管理 ──

    @rl_group.command("user_set")
    @filter.permission_type(PermissionType.ADMIN)
    async def rl_user_set(self, event: AstrMessageEvent, user_id: str, count: int):
        """为用户设置自定义频率限制（优先级最高）。用法: /rl user_set <用户ID> <次数>"""
        user_id = user_id.strip()
        if not user_id:
            yield event.plain_result("❌ 用户 ID 不能为空。")
            return
        if count < 1:
            yield event.plain_result("❌ 最大请求次数必须 ≥ 1。")
            return
        self.user_limits[user_id] = count
        try:
            self._save_limits()
        except Exception:
            self.user_limits.pop(user_id, None)
            yield event.plain_result("❌ 保存配置失败。")
            return
        self._request_records.pop(user_id, None)
        yield event.plain_result(f"✅ 用户 {user_id} 的频率限制已设置为 {count} 次/{self.time_window} 秒。")

    @rl_group.command("user_del")
    @filter.permission_type(PermissionType.ADMIN)
    async def rl_user_del(self, event: AstrMessageEvent, user_id: str):
        """移除用户的自定义频率限制。用法: /rl user_del <用户ID>"""
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
        if not self.user_limits:
            yield event.plain_result("👤 没有用户自定义限制。")
            return
        lines = ["👤 用户自定义限制 (优先级最高):"]
        items = list(self.user_limits.items())
        for uid, limit in items[:self._MAX_DISPLAY]:
            lines.append(f"  · {uid}: {limit} 次/{self.time_window} 秒")
        if len(items) > self._MAX_DISPLAY:
            lines.append(f"  ... 省略 {len(items) - self._MAX_DISPLAY} 条")
        yield event.plain_result("\n".join(lines))
