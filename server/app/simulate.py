"""后台模拟：命令行跑通一整轮（tick 0 -> 99），打印每 NPC 每 tick 的行为。

M1.5 起为「计划执行 + 例外唤醒」双引擎：
- 计划执行（plan_executor，零 LLM、确定性、可回放）：有 active 计划的 NPC
  按 BDI 计划自转——这是"涌动引擎"确定性的一半，也是本文件的自检主线
  （test_man 按 7 步猎杀链推进：逐室→取刀→杀女）。
- 例外唤醒（agent.decide，LLM）：排班命中且【无 active 计划】的 NPC 才动脑；
  排班驱动原本是 MVP 基线，现在降级为"计划未覆盖角色的兜底"。

为什么保留例外唤醒：完整涌现是每 NPC 每 tick 都决策，但那是 8 NPC × 100 tick
= 800 次 LLM 调用，又慢又贵；计划内走确定性路径，计划外才按 schedule 唤醒，
兼顾"涌现感"与"成本可控"。

用法（在 server/ 目录下）：
    python -m app.simulate                        # 自动创建新会话跑整轮（黄金乡）
    python -m app.simulate --world test           # 按测试世界跑（M1.5 主线）
    python -m app.simulate --session sess_xxx     # 指定会话（复用/回归）
    python -m app.simulate --max-tick 40          # 只跑到 tick 40
"""
import json
import sys

from . import scheduler
from . import db
from . import sessions
from . import world as world_mod
from .llm import DeepSeekClient


def run_simulation(session_id=None, max_tick=None, world_id="golden"):
    """跑一整轮模拟，返回整轮的痕迹总数。

    世界时序 v2（每 tick 五阶段，顺序固定保证可回放）：
      ① 计划执行（确定性，零 LLM）：有 active 计划的 NPC 落效果——
         决策者本 tick 将看到这些效果（别人的行动改变世界 → 变化察觉）；
      ② 决策阶段（并行 LLM）：排班命中且无 active 计划且存活的 NPC 走 agent.decide。
         并发执行（llm.chat_many 线程池语义，同 key 多路并发）；决策阶段只读
         世界状态，彼此的决策互相不可见（快照语义，防止同 tick 串话）；
      ③ 仲裁落库（确定性顺序）：decisions 按 agent id 排序后交 fate——
         同地点多人行动 → 碰撞痕迹（fate 已有规则版）；同物争抢 → 后写覆盖（MVP）；
      ④ 自我记忆：agent.decide 内部已写（我做了/我说了/谎言追踪/说漏嘴检测）；
      ⑤ 时间推进：current_tick 落库（game_state），/chat 与下一 tick 感知共用此时钟。

    Args:
        session_id: 会话标识（M1.1）。None = 自动创建新会话；
            传入已存在的会话则复用（痕迹/记忆继续累积在该会话下）。
        max_tick: 终止 tick（None = 跑满 0~99 一整轮）。
        world_id: 世界维度（golden=黄金乡 / test=测试世界）。决定角色池/知识/决策
            挂在哪个世界叙事下 —— 测试世界模拟须传 "test"，否则 OOC。
    """
    end = scheduler.MAX_TICK if max_tick is None else max_tick
    if session_id:
        sessions.ensure_session(session_id)
    else:
        session_id = sessions.start_session()["session_id"]
    # 环境卡复位：从本世界的 initial_state 恢复出厂状态（轮回=完整重置，二次模拟不塌）。
    # M1.5 泛化：任何世界都走 db.reset_environment_cards，不再硬编码 room_1/knife/chair。
    db.reset_environment_cards(world_id)

    npcs = db.get_all_npc_ids(world_id)  # 按世界过滤角色池（003 迁移后可隔离）
    llm_client = DeepSeekClient()  # 重规划用（有受阻/从零起才真正调用；预算：每事件 ≤1 次）
    print(f"会话：{session_id}")
    print(f"世界：{world_id}　参与模拟的 NPC：{npcs}")
    print(f"时间范围：{scheduler.tick_to_clock(0)} -> {scheduler.tick_to_clock(end)}")
    total_traces = 0

    for tick in range(0, end + 1):
        clock = scheduler.tick_to_clock(tick)
        # 五阶段全量（与在线驱动共用同一实现——world.step_world_tick）：
        # 计划执行 → 重规划 → 并行决策 → fate 仲裁 → 时钟推进（recorder 自动开启）
        # plan 模式（离线确定性回放/自检）：计划机械执行 + 排班例外决策——
        # 与在线 live 模式（全 NPC 每 tick LLM 决策）是两种运行形态
        summary = world_mod.step_world_tick(session_id, tick, world_id,
                                            llm_client=llm_client, npcs=npcs, mode="plan")
        for r in summary.get("replans", []):
            tag = "重规划" if r.get("phase") == "replan" else "从零起规划"
            print(f"  [T6][{r.get('npc')}] {tag}：{'成功' if r.get('ok') else '未成'}——{r.get('reason', '')}")

        traces = (summary.get("recorder", {}).get("world", {}) or {}).get("traces", [])
        if not traces:
            continue
        total_traces += len(traces)

        print(f"\n=== {clock} (tick {tick}) ===")

        for t in traces:
            line = f"  [{t['actor']}] {t['type']} -> {t['target']} @ {t['location']}"
            if t.get("detail"):
                line += f"：{t['detail']}"
            print(line)

    # 收官：世界时钟停在最后 tick
    db.upsert_game_state(session_id, "current_tick", end)
    print(f"\n模拟结束，会话 {session_id} 共产生 {total_traces} 条世界痕迹。")
    # 打印关键世界状态（收官可见性：谁在哪、谁死了、关键物状态）
    _print_world_state(session_id, npcs, world_id)
    return total_traces


def _print_world_state(session_id, npcs, world_id="golden"):
    """模拟收尾打印关键世界状态（收官可见性：谁在哪、谁死了、关键物状态）。

    M1.5 泛化：不再硬编码 knife/玩家文案——遍历本世界全部环境卡与角色状态，
    任何世界都能打印（黄金乡无 knife 也不会报错/误导）。玩家状态由 M1.9 判定器消费，
    这里只如实展示本世界的 NPC 位置/存活与关键物（kind=item）的关键状态位。
    """
    print("\n—— 关键世界状态 ——")
    for npc in npcs:
        pos = db.get_npc_pos(session_id, npc)
        status = db.get_npc_status(session_id, npc)
        live = "存活" if not status.get("dead") else "已死"
        print(f"  {npc}：位置 {pos or '未知'}　生命= {live}")
    # 遍历本世界物品类环境卡，打印各关键物状态位（含 holder/state 等）
    for env_id, kind, name, _d, state_raw, _p in db.get_environment_cards(world_id):
        if kind != "item":
            continue
        st = json.loads(state_raw or "{}")
        # 只展示几类通用状态位：state/holder/current_place/stained（有则显示）
        shown = {k: st[k] for k in ("state", "holder", "current_place", "stained", "poisoned") if k in st}
        print(f"  {env_id}：{shown or st or '(无状态)'}")


if __name__ == "__main__":
    max_tick = None
    session_id = None
    world_id = "golden"
    if "--max-tick" in sys.argv:
        idx = sys.argv.index("--max-tick")
        max_tick = int(sys.argv[idx + 1])
    if "--session" in sys.argv:
        idx = sys.argv.index("--session")
        session_id = sys.argv[idx + 1]
    if "--world" in sys.argv:
        idx = sys.argv.index("--world")
        world_id = sys.argv[idx + 1]
    run_simulation(session_id=session_id, max_tick=max_tick, world_id=world_id)
