"""
独立单元测试 - 不依赖 AstrBot 运行时，测试频率限制核心逻辑。
覆盖：基础限频、窗口过期、用户隔离、白名单、群组限制、用户限制、优先级。
运行: python test_rate_limit.py
"""
import time
from collections import defaultdict, deque
from typing import Tuple


class RateLimiter:
    """从 main.py 提取的纯逻辑，用于独立测试。"""

    def __init__(self, max_requests=6, time_window=60, group_limits=None,
                 user_limits=None, whitelist=None):
        self.max_requests = max_requests
        self.time_window = time_window
        self.group_limits: dict = group_limits or {}
        self.user_limits: dict = user_limits or {}
        self.whitelist: list = whitelist or []
        self._request_records: dict[str, deque] = defaultdict(deque)

    def resolve_max_requests(self, user_id: str, group_id: str | None) -> int:
        """优先级: 用户自定义 > 群组自定义 > 全局默认"""
        if user_id in self.user_limits:
            return int(self.user_limits[user_id])
        if group_id and group_id in self.group_limits:
            return int(self.group_limits[group_id])
        return self.max_requests

    def check(self, user_id: str, max_req: int, now: float = None) -> Tuple[bool, float]:
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

    def request(self, user_id: str, group_id: str | None = None, now: float = None):
        """完整模拟一次请求（含白名单 + 优先级解析）。"""
        if user_id in self.whitelist:
            return True, 0.0
        max_req = self.resolve_max_requests(user_id, group_id)
        return self.check(user_id, max_req, now)


# ═══════════════════════════════════════════════════
# 基础测试
# ═══════════════════════════════════════════════════

def test_basic_allow():
    """正常请求应该被允许"""
    rl = RateLimiter(max_requests=3, time_window=60)
    for i in range(3):
        allowed, _ = rl.request("user1", now=100.0 + i)
        assert allowed, f"第 {i+1} 次请求应该被允许"
    print("✅ test_basic_allow")


def test_exceed_limit():
    """超出限制应该被拒绝"""
    rl = RateLimiter(max_requests=3, time_window=60)
    for i in range(3):
        rl.request("user1", now=100.0 + i)
    allowed, cd = rl.request("user1", now=103.0)
    assert not allowed
    assert cd > 0
    print(f"✅ test_exceed_limit (cooldown={cd}s)")


def test_window_expiry():
    """窗口过期后应该恢复"""
    rl = RateLimiter(max_requests=2, time_window=10)
    rl.request("u", now=0.0)
    rl.request("u", now=1.0)
    assert rl.request("u", now=5.0)[0] is False
    assert rl.request("u", now=11.0)[0] is True  # t=0 的记录过期
    print("✅ test_window_expiry")


def test_users_isolated():
    """不同用户之间计数隔离"""
    rl = RateLimiter(max_requests=1, time_window=60)
    rl.request("a", now=0.0)
    assert rl.request("a", now=1.0)[0] is False
    assert rl.request("b", now=1.0)[0] is True
    print("✅ test_users_isolated")


def test_whitelist():
    """白名单用户不受限制"""
    rl = RateLimiter(max_requests=1, time_window=60, whitelist=["vip"])
    rl.request("normal", now=0.0)
    assert rl.request("normal", now=1.0)[0] is False
    for i in range(20):
        assert rl.request("vip", now=float(i))[0] is True
    print("✅ test_whitelist")


def test_cooldown_accuracy():
    """冷却时间精确"""
    rl = RateLimiter(max_requests=3, time_window=60)
    rl.request("u", now=10.0)
    rl.request("u", now=20.0)
    rl.request("u", now=30.0)
    _, cd = rl.request("u", now=50.0)
    assert cd == 20.0, f"expected 20, got {cd}"
    print(f"✅ test_cooldown_accuracy (cooldown={cd}s)")


def test_rapid_burst():
    """同一时刻连发"""
    rl = RateLimiter(max_requests=6, time_window=60)
    results = [rl.request("u", now=100.0) for _ in range(10)]
    ok = sum(1 for a, _ in results if a)
    no = sum(1 for a, _ in results if not a)
    assert ok == 6 and no == 4
    print(f"✅ test_rapid_burst (allowed={ok}, rejected={no})")


# ═══════════════════════════════════════════════════
# 群组限制测试
# ═══════════════════════════════════════════════════

def test_group_limit():
    """群组自定义限制生效"""
    rl = RateLimiter(max_requests=6, time_window=60, group_limits={"group_A": 2})
    # group_A 的用户限额为 2
    assert rl.request("u1", group_id="group_A", now=0.0)[0] is True
    assert rl.request("u1", group_id="group_A", now=1.0)[0] is True
    assert rl.request("u1", group_id="group_A", now=2.0)[0] is False  # 第 3 次被拒
    print("✅ test_group_limit")


def test_group_default_fallback():
    """未配置的群组使用全局默认"""
    rl = RateLimiter(max_requests=3, time_window=60, group_limits={"group_A": 7})
    # group_B 没有自定义，使用默认 3
    for i in range(3):
        rl.request("u", group_id="group_B", now=float(i))
    assert rl.request("u", group_id="group_B", now=3.0)[0] is False
    print("✅ test_group_default_fallback")


def test_group_higher_limit():
    """群组限制可以比默认更高"""
    rl = RateLimiter(max_requests=3, time_window=60, group_limits={"vip_group": 7})
    for i in range(7):
        allowed, _ = rl.request("u", group_id="vip_group", now=float(i))
        assert allowed, f"vip_group 第 {i+1} 次应该被允许"
    assert rl.request("u", group_id="vip_group", now=7.0)[0] is False
    print("✅ test_group_higher_limit (7次OK, 第8次拒绝)")


def test_private_msg_no_group():
    """私聊消息（无群组ID）使用全局默认"""
    rl = RateLimiter(max_requests=3, time_window=60, group_limits={"g": 100})
    for i in range(3):
        rl.request("u", group_id=None, now=float(i))
    assert rl.request("u", group_id=None, now=3.0)[0] is False
    print("✅ test_private_msg_no_group")


# ═══════════════════════════════════════════════════
# 用户自定义限制测试
# ═══════════════════════════════════════════════════

def test_user_limit():
    """用户自定义限制生效"""
    rl = RateLimiter(max_requests=6, time_window=60, user_limits={"slow_user": 2})
    assert rl.request("slow_user", now=0.0)[0] is True
    assert rl.request("slow_user", now=1.0)[0] is True
    assert rl.request("slow_user", now=2.0)[0] is False
    print("✅ test_user_limit")


def test_user_limit_overrides_group():
    """用户限制优先级高于群组限制"""
    rl = RateLimiter(
        max_requests=10,
        time_window=60,
        group_limits={"group_X": 7},
        user_limits={"special_user": 4},
    )
    # special_user 在 group_X 里，但用户级限制 4 > 群组级 7
    for i in range(4):
        allowed, _ = rl.request("special_user", group_id="group_X", now=float(i))
        assert allowed, f"第 {i+1} 次应该被允许"
    assert rl.request("special_user", group_id="group_X", now=4.0)[0] is False
    print("✅ test_user_limit_overrides_group (4次OK, 第5次拒绝)")


def test_user_limit_overrides_default():
    """用户限制优先级高于全局默认"""
    rl = RateLimiter(max_requests=10, time_window=60, user_limits={"tight_user": 3})
    for i in range(3):
        rl.request("tight_user", now=float(i))
    assert rl.request("tight_user", now=3.0)[0] is False
    # 普通用户仍然按 10 次来
    for i in range(10):
        rl.request("normal", now=float(i))
    assert rl.request("normal", now=10.0)[0] is False
    print("✅ test_user_limit_overrides_default")


# ═══════════════════════════════════════════════════
# 混合优先级综合测试
# ═══════════════════════════════════════════════════

def test_full_priority_chain():
    """
    综合测试优先级链: 白名单 > 用户自定义 > 群组自定义 > 全局默认

    场景: 全局默认=6, group_A=7, user_X=4, vip 在白名单
    """
    rl = RateLimiter(
        max_requests=6,
        time_window=60,
        group_limits={"group_A": 7},
        user_limits={"user_X": 4},
        whitelist=["vip"],
    )

    resolved = rl.resolve_max_requests("random", None)
    assert resolved == 6, f"普通用户/无群: expected 6, got {resolved}"

    resolved = rl.resolve_max_requests("random", "group_A")
    assert resolved == 7, f"普通用户/group_A: expected 7, got {resolved}"

    resolved = rl.resolve_max_requests("user_X", "group_A")
    assert resolved == 4, f"user_X/group_A: expected 4, got {resolved}"

    resolved = rl.resolve_max_requests("user_X", None)
    assert resolved == 4, f"user_X/无群: expected 4, got {resolved}"

    # 白名单用户无论如何都放行
    for i in range(20):
        assert rl.request("vip", group_id="group_A", now=float(i))[0] is True

    # user_X 在 group_A 中被限制为 4
    for i in range(4):
        assert rl.request("user_X", group_id="group_A", now=float(i))[0] is True
    assert rl.request("user_X", group_id="group_A", now=4.0)[0] is False

    print("✅ test_full_priority_chain (全链路验证通过)")


if __name__ == "__main__":
    print("=" * 55)
    print("🧪 AstrBot Rate Limit Plugin - 单元测试 v1.1")
    print("=" * 55)

    section = lambda title: print(f"\n── {title} ──")

    section("基础测试")
    test_basic_allow()
    test_exceed_limit()
    test_window_expiry()
    test_users_isolated()
    test_whitelist()
    test_cooldown_accuracy()
    test_rapid_burst()

    section("群组限制测试")
    test_group_limit()
    test_group_default_fallback()
    test_group_higher_limit()
    test_private_msg_no_group()

    section("用户自定义限制测试")
    test_user_limit()
    test_user_limit_overrides_group()
    test_user_limit_overrides_default()

    section("混合优先级综合测试")
    test_full_priority_chain()

    print("\n" + "=" * 55)
    print("🎉 全部 15 个测试通过！")
    print("=" * 55)
