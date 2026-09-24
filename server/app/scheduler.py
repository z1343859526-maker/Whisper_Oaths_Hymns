"""世界调度器：维护世界时钟 tick，并查询"某 NPC 在某个 tick 的既定排班"。

tick 约定（P4 笔记 §8.2）：
- 一天 24h = 144 tick，每 tick = 10 分钟；
- tick 0 = 第一天 18:00（玩家抵达），tick 99 = 次日 10:30（灭口坏结局锚点）；
- 一轮轮回的可行动窗口 = tick 0 ~ 99，约 100 个行动点。

职责边界：
- 本模块只做「时钟换算」+「排班查询」；
- NPC 决策（agent.py）、碰撞仲裁（fate.py）另属他模块。
"""
from . import db

TICK_MINUTES = 10                 # 每个 tick 的物理分钟数
START_HOUR, START_MIN = 18, 0     # tick 0 对应的墙上时间 18:00
MAX_TICK = 99                     # 次日 10:30（坏结局锚点），超过即本轮终止


def tick_to_clock(tick: int) -> str:
    """tick -> 'HH:MM' 墙上时钟。

    为什么对 24h 取模：tick 会跨零点（18:00 -> 次日 06:00），
    分钟总数超过 1440 后要绕回当天，取模即"回卷"。
    """
    total = START_HOUR * 60 + START_MIN + tick * TICK_MINUTES
    total %= 24 * 60
    return f"{total // 60:02d}:{total % 60:02d}"


def clock_to_tick(hhmm: str) -> int:
    """'HH:MM' -> tick。

    为什么 < 18:00 要 +24h：时间线从 18:00 起步、跨到次日清晨，
    06:00 相对起点已过 12 小时，须按"次日"累加才能得到正数分钟差。
    整数除法 // 自然向下取整，让 23:45 这类非整格时间落到前一格 tick。
    """
    h, m = hhmm.split(":")
    minutes = int(h) * 60 + int(m) - (START_HOUR * 60 + START_MIN)
    if minutes < 0:
        minutes += 24 * 60
    return minutes // TICK_MINUTES


def scheduled_actions(npc_id: str, tick: int) -> list[dict]:
    """查某 NPC 在当前 tick 命中的既定排班（可能 0 或多条）。

    为什么可能 0 条：绝大多数 tick 里 NPC 没有精确到分钟的计划，
    此时由 agent.py 按人设/目标自由决策；排班只是"计划基线"。
    """
    rows = db.get_schedule(npc_id)
    hits = []
    for time, location, action, intent in rows:
        if clock_to_tick(time) == tick:
            hits.append({
                "time": time,
                "location": location,
                "action": action,
                "intent": intent,
            })
    return hits


if __name__ == "__main__":
    # 演示：时钟换算 + 金穗夫人在 tick 34(23:40) 取钥匙的排班命中
    print("时钟换算检查：")
    for t in (0, 6, 34, 36, 72, 99):
        print(f"  tick {t:>3} -> {tick_to_clock(t)}")

    print("\n金穗夫人排班命中（tick 34 应命中 23:40 取钥匙）：")
    for row in scheduled_actions("isabella", 34):
        print(f"  {row['time']} @ {row['location']}：{row['action']}（{row['intent']}）")
