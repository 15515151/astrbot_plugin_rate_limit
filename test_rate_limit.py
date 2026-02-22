"""
独立单元测试 - 不依赖 AstrBot 运行时。
覆盖：基础限频、窗口过期、用户隔离、白名单、群组每用户限制、
      用户自定义限制、优先级链、群组总量限制。
运行: python test_rate_limit.py
"""
import time
from collections import defaultdict, deque
from typing import Tuple


class RateLimiter:
    """从 main.py 提取的纯逻辑，用于独立测试。"""

    def __init__(self, max_requests=6, time_window=60, default_group_total=0,
                 group_limits=None, group_total_limits=None,
                 user_limits=None, whitelist=None):
        self.max_requests = max_requests
        self.time_window = time_window
        self.default_group_total = default_group_total
        self.group_limits: dict = group_limits or {}
        self.group_total_limits: dict = group_total_limits or {}
        self.user_limits: dict = user_limits or {}
        self.whitelist: list = whitelist or []
        self._user_records: dict[str, deque] = defaultdict(deque)
        self._group_records: dict[str, deque] = defaultdict(deque)

    def resolve_max_requests(self, user_id: str, group_id: str | None) -> int:
        if user_id in self.user_limits:
            return int(self.user_limits[user_id])
        if group_id and group_id in self.group_limits:
            return int(self.group_limits[group_id])
        return self.max_requests

    def _resolve_group_total(self, group_id: str) -> int:
        if group_id in self.group_total_limits:
            return self.group_total_limits[group_id]
        return self.default_group_total

    @staticmethod
    def _sw_check(records: deque, max_req: int, time_window: int,
                  now: float) -> Tuple[bool, float]:
        if max_req <= 0:
            return False, 0.0
        window_start = now - time_window
        while records and records[0] <= window_start:
            records.popleft()
        if len(records) >= max_req:
            cooldown = records[0] - window_start
            return False, round(cooldown, 1)
        return True, 0.0

    def request(self, user_id: str, group_id: str | None = None,
                now: float = None) -> Tuple[bool, float, str]:
        """模拟完整请求流程。返回 (allowed, cooldown, reason)。"""
        if now is None:
            now = time.time()
        if user_id in self.whitelist:
            return True, 0.0, "whitelist"

        # 用户级检查
        max_req = self.resolve_max_requests(user_id, group_id)
        user_records = self._user_records[user_id]
        allowed, cd = self._sw_check(user_records, max_req, self.time_window, now)
        if not allowed:
            return False, cd, "user_limit"

        # 群组总量检查
        group_max = self._resolve_group_total(group_id) if group_id else 0
        if group_id and group_max > 0:
            g_records = self._group_records[group_id]
            g_allowed, g_cd = self._sw_check(g_records, group_max, self.time_window, now)
            if not g_allowed:
                return False, g_cd, "group_total"

        # 都通过，记录
        user_records.append(now)
        if group_id and group_max > 0:
            self._group_records[group_id].append(now)

        return True, 0.0, "ok"


# ═══════════════════════════════════════════════════
# 基础测试
# ═══════════════════════════════════════════════════

def test_basic_allow():
    rl = RateLimiter(max_requests=3, time_window=60)
    for i in range(3):
        ok, _, _ = rl.request("u", now=100.0 + i)
        assert ok
    print("OK test_basic_allow")


def test_max_req_zero():
    """max_req=0 should always deny without IndexError"""
    rl = RateLimiter(max_requests=0, time_window=60)
    ok, _, reason = rl.request("u", now=0.0)
    assert not ok and reason == "user_limit"
    print("OK test_max_req_zero")


def test_exceed_limit():
    rl = RateLimiter(max_requests=3, time_window=60)
    for i in range(3):
        rl.request("u", now=100.0 + i)
    ok, cd, reason = rl.request("u", now=103.0)
    assert not ok and cd > 0 and reason == "user_limit"
    print(f"✅ test_exceed_limit (cd={cd}s)")


def test_window_expiry():
    rl = RateLimiter(max_requests=2, time_window=10)
    rl.request("u", now=0.0)
    rl.request("u", now=1.0)
    assert rl.request("u", now=5.0)[0] is False
    assert rl.request("u", now=11.0)[0] is True
    print("✅ test_window_expiry")


def test_users_isolated():
    rl = RateLimiter(max_requests=1, time_window=60)
    rl.request("a", now=0.0)
    assert rl.request("a", now=1.0)[0] is False
    assert rl.request("b", now=1.0)[0] is True
    print("✅ test_users_isolated")


def test_whitelist():
    rl = RateLimiter(max_requests=1, time_window=60, whitelist=["vip"])
    rl.request("normal", now=0.0)
    assert rl.request("normal", now=1.0)[0] is False
    for i in range(20):
        ok, _, reason = rl.request("vip", now=float(i))
        assert ok and reason == "whitelist"
    print("✅ test_whitelist")


def test_cooldown_accuracy():
    rl = RateLimiter(max_requests=3, time_window=60)
    rl.request("u", now=10.0)
    rl.request("u", now=20.0)
    rl.request("u", now=30.0)
    _, cd, _ = rl.request("u", now=50.0)
    assert cd == 20.0
    print(f"✅ test_cooldown_accuracy (cd={cd}s)")


def test_rapid_burst():
    rl = RateLimiter(max_requests=6, time_window=60)
    results = [rl.request("u", now=100.0) for _ in range(10)]
    ok = sum(1 for a, _, _ in results if a)
    no = sum(1 for a, _, _ in results if not a)
    assert ok == 6 and no == 4
    print(f"✅ test_rapid_burst (ok={ok}, no={no})")


# ═══════════════════════════════════════════════════
# 群组每用户限制
# ═══════════════════════════════════════════════════

def test_group_per_user_limit():
    rl = RateLimiter(max_requests=6, time_window=60, group_limits={"gA": 2})
    assert rl.request("u", group_id="gA", now=0.0)[0] is True
    assert rl.request("u", group_id="gA", now=1.0)[0] is True
    assert rl.request("u", group_id="gA", now=2.0)[0] is False
    print("✅ test_group_per_user_limit")


def test_group_default_fallback():
    rl = RateLimiter(max_requests=3, time_window=60, group_limits={"gA": 7})
    for i in range(3):
        rl.request("u", group_id="gB", now=float(i))
    assert rl.request("u", group_id="gB", now=3.0)[0] is False
    print("✅ test_group_default_fallback")


def test_group_higher_limit():
    rl = RateLimiter(max_requests=3, time_window=60, group_limits={"vip": 7})
    for i in range(7):
        assert rl.request("u", group_id="vip", now=float(i))[0] is True
    assert rl.request("u", group_id="vip", now=7.0)[0] is False
    print("✅ test_group_higher_limit")


# ═══════════════════════════════════════════════════
# 用户自定义限制 + 优先级
# ═══════════════════════════════════════════════════

def test_user_limit():
    rl = RateLimiter(max_requests=6, time_window=60, user_limits={"slow": 2})
    assert rl.request("slow", now=0.0)[0] is True
    assert rl.request("slow", now=1.0)[0] is True
    assert rl.request("slow", now=2.0)[0] is False
    print("✅ test_user_limit")


def test_user_overrides_group():
    rl = RateLimiter(max_requests=10, time_window=60,
                     group_limits={"gX": 7}, user_limits={"sp": 4})
    for i in range(4):
        assert rl.request("sp", group_id="gX", now=float(i))[0] is True
    assert rl.request("sp", group_id="gX", now=4.0)[0] is False
    print("✅ test_user_overrides_group")


def test_full_priority_chain():
    rl = RateLimiter(max_requests=6, time_window=60,
                     group_limits={"gA": 7}, user_limits={"uX": 4},
                     whitelist=["vip"])
    assert rl.resolve_max_requests("rand", None) == 6
    assert rl.resolve_max_requests("rand", "gA") == 7
    assert rl.resolve_max_requests("uX", "gA") == 4
    for i in range(20):
        assert rl.request("vip", group_id="gA", now=float(i))[0] is True
    for i in range(4):
        assert rl.request("uX", group_id="gA", now=float(i))[0] is True
    assert rl.request("uX", group_id="gA", now=4.0)[0] is False
    print("✅ test_full_priority_chain")


# ═══════════════════════════════════════════════════
# 群组总量限制（新功能）
# ═══════════════════════════════════════════════════

def test_group_total_basic():
    """群组总量限制：全群共享计数器"""
    rl = RateLimiter(max_requests=10, time_window=60,
                     group_total_limits={"g1": 5})
    # 5 个不同用户各请求一次，全部 OK
    for i in range(5):
        ok, _, _ = rl.request(f"user_{i}", group_id="g1", now=float(i))
        assert ok, f"user_{i} 应该被允许"
    # 第 6 个用户被群总量拒绝
    ok, cd, reason = rl.request("user_5", group_id="g1", now=5.0)
    assert not ok and reason == "group_total"
    print(f"✅ test_group_total_basic (5人各1次OK, 第6人被拒, cd={cd}s)")


def test_group_total_mixed_users():
    """单用户未超限但群总量超限"""
    rl = RateLimiter(max_requests=10, time_window=60,
                     group_total_limits={"g1": 3})
    # user_a 发 2 次（个人限额 10，未超）
    rl.request("user_a", group_id="g1", now=0.0)
    rl.request("user_a", group_id="g1", now=1.0)
    # user_b 发 1 次
    rl.request("user_b", group_id="g1", now=2.0)
    # 群总量已满 3 次，user_c 虽然个人从没发过也被拒
    ok, _, reason = rl.request("user_c", group_id="g1", now=3.0)
    assert not ok and reason == "group_total"
    print("✅ test_group_total_mixed_users")


def test_group_total_window_expiry():
    """群组总量窗口过期后恢复"""
    rl = RateLimiter(max_requests=10, time_window=10,
                     group_total_limits={"g1": 2})
    rl.request("a", group_id="g1", now=0.0)
    rl.request("b", group_id="g1", now=1.0)
    assert rl.request("c", group_id="g1", now=5.0)[0] is False
    # t=11: t=0 的记录过期，腾出名额
    ok, _, _ = rl.request("c", group_id="g1", now=11.0)
    assert ok
    print("✅ test_group_total_window_expiry")


def test_group_total_user_limit_first():
    """用户级先触发 → 不应计入群总量"""
    rl = RateLimiter(max_requests=1, time_window=60,
                     group_total_limits={"g1": 10})
    # user_a 用完个人限额 1 次
    rl.request("user_a", group_id="g1", now=0.0)
    # user_a 第 2 次被用户级拒绝
    ok, _, reason = rl.request("user_a", group_id="g1", now=1.0)
    assert not ok and reason == "user_limit"
    # 群总量应该只消耗了 1 次（被用户级拒绝的不计入），user_b 应该 OK
    ok, _, _ = rl.request("user_b", group_id="g1", now=2.0)
    assert ok
    print("✅ test_group_total_user_limit_first")


def test_group_total_whitelist_bypass():
    """白名单用户不消耗群总量"""
    rl = RateLimiter(max_requests=10, time_window=60,
                     group_total_limits={"g1": 2}, whitelist=["vip"])
    # vip 发 10 次，不消耗群总量
    for i in range(10):
        ok, _, reason = rl.request("vip", group_id="g1", now=float(i))
        assert ok and reason == "whitelist"
    # 普通用户仍然有 2 次群总量配额
    assert rl.request("u1", group_id="g1", now=10.0)[0] is True
    assert rl.request("u2", group_id="g1", now=11.0)[0] is True
    ok, _, reason = rl.request("u3", group_id="g1", now=12.0)
    assert not ok and reason == "group_total"
    print("✅ test_group_total_whitelist_bypass")


def test_group_total_different_groups():
    """不同群的总量计数隔离"""
    rl = RateLimiter(max_requests=10, time_window=60,
                     group_total_limits={"g1": 2, "g2": 3})
    # g1: 2 个不同用户各 1 次 → 满
    rl.request("g1_u1", group_id="g1", now=0.0)
    rl.request("g1_u2", group_id="g1", now=1.0)
    ok, _, reason = rl.request("g1_u3", group_id="g1", now=2.0)
    assert not ok and reason == "group_total"  # g1 满
    # g2 不受影响，用不同用户
    assert rl.request("g2_u1", group_id="g2", now=2.0)[0] is True
    assert rl.request("g2_u2", group_id="g2", now=3.0)[0] is True
    assert rl.request("g2_u3", group_id="g2", now=4.0)[0] is True
    ok2, _, reason2 = rl.request("g2_u4", group_id="g2", now=5.0)
    assert not ok2 and reason2 == "group_total"  # g2 满
    print("✅ test_group_total_different_groups")


def test_group_total_no_limit_unconfigured():
    """未配置群总量限制的群不受群总量约束"""
    rl = RateLimiter(max_requests=2, time_window=60,
                     group_total_limits={"g1": 3})
    # g2 没配群总量 → 不受群总量约束，只受用户级限制 (max=2)
    assert rl.request("u0", group_id="g2", now=0.0)[0] is True   # u0 第1次
    assert rl.request("u0", group_id="g2", now=1.0)[0] is True   # u0 第2次
    assert rl.request("u0", group_id="g2", now=2.0)[0] is False  # u0 第3次 → 用户级拒绝
    # 不同用户也可以继续请求（没有群总量限制）
    assert rl.request("u1", group_id="g2", now=3.0)[0] is True
    assert rl.request("u2", group_id="g2", now=4.0)[0] is True
    print("OK test_group_total_no_limit_unconfigured")


def test_private_msg_no_group_total():
    """私聊消息不受群总量限制"""
    rl = RateLimiter(max_requests=3, time_window=60,
                     group_total_limits={"g1": 1})
    for i in range(3):
        assert rl.request("u", group_id=None, now=float(i))[0] is True
    assert rl.request("u", group_id=None, now=3.0)[0] is False  # 只受用户级限制
    print("✅ test_private_msg_no_group_total")


# ═══════════════════════════════════════════════════
# 全局默认群总量限制
# ═══════════════════════════════════════════════════

def test_default_group_total_basic():
    """default_group_total 让所有群都受群总量限制"""
    rl = RateLimiter(max_requests=10, time_window=60, default_group_total=3)
    # 任意群 g_any，3 个用户各 1 次 → 满
    assert rl.request("a", group_id="g_any", now=0.0)[0] is True
    assert rl.request("b", group_id="g_any", now=1.0)[0] is True
    assert rl.request("c", group_id="g_any", now=2.0)[0] is True
    ok, _, reason = rl.request("d", group_id="g_any", now=3.0)
    assert not ok and reason == "group_total"
    # 另一个群 g_other 也受限
    assert rl.request("x", group_id="g_other", now=0.0)[0] is True
    assert rl.request("y", group_id="g_other", now=1.0)[0] is True
    assert rl.request("z", group_id="g_other", now=2.0)[0] is True
    ok2, _, reason2 = rl.request("w", group_id="g_other", now=3.0)
    assert not ok2 and reason2 == "group_total"
    print("OK test_default_group_total_basic")


def test_default_group_total_override():
    """group_total_limits 覆盖 default_group_total"""
    rl = RateLimiter(max_requests=10, time_window=60,
                     default_group_total=3,
                     group_total_limits={"vip_g": 10})
    # 普通群受默认限制 3
    for i in range(3):
        rl.request(f"u{i}", group_id="normal_g", now=float(i))
    assert rl.request("u3", group_id="normal_g", now=3.0)[0] is False
    # vip_g 被覆盖为 10
    for i in range(10):
        ok, _, _ = rl.request(f"v{i}", group_id="vip_g", now=float(i))
        assert ok
    ok2, _, reason = rl.request("v10", group_id="vip_g", now=10.0)
    assert not ok2 and reason == "group_total"
    print("OK test_default_group_total_override")


def test_default_group_total_private_bypass():
    """私聊不受 default_group_total 影响"""
    rl = RateLimiter(max_requests=5, time_window=60, default_group_total=2)
    for i in range(5):
        assert rl.request("u", group_id=None, now=float(i))[0] is True
    assert rl.request("u", group_id=None, now=5.0)[0] is False  # 只受用户级
    print("OK test_default_group_total_private_bypass")



if __name__ == "__main__":
    print("=" * 55)
    print("Tests v1.3")
    print("=" * 55)

    s = lambda t: print(f"\n-- {t} --")

    s("basic")
    test_basic_allow()
    test_max_req_zero()
    test_exceed_limit()
    test_window_expiry()
    test_users_isolated()
    test_whitelist()
    test_cooldown_accuracy()
    test_rapid_burst()

    s("group per-user")
    test_group_per_user_limit()
    test_group_default_fallback()
    test_group_higher_limit()

    s("user override + priority")
    test_user_limit()
    test_user_overrides_group()
    test_full_priority_chain()

    s("group total (specific)")
    test_group_total_basic()
    test_group_total_mixed_users()
    test_group_total_window_expiry()
    test_group_total_user_limit_first()
    test_group_total_whitelist_bypass()
    test_group_total_different_groups()
    test_group_total_no_limit_unconfigured()
    test_private_msg_no_group_total()

    s("default group total")
    test_default_group_total_basic()
    test_default_group_total_override()
    test_default_group_total_private_bypass()

    print("\n" + "=" * 55)
    print("ALL 26 TESTS PASSED!")
    print("=" * 55)

