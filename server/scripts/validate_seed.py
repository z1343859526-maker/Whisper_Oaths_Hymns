"""轮回初始世界状态质检（M1.4）：计划可达性 + 引用完整性，悬空即红灯拒绝入库。

用法（server/ 目录下）：
    python scripts/validate_seed.py

做什么（三层递进，纯程序读库、零 LLM）：
  0. 结构层    —— 复用 app/plans.py validate_plan_schema（字段/枚举/编号）；
  1. 引用完整性 —— 计划每步引用的 scene/target/状态键，在世界里必须真实存在；
  2. 可达性    —— 从初始世界出发把效果逐步应用，评估每步前置是否可满足。
                  某步 blocked = 链条断裂 = 这条计划线在无干预下走不完。

设计原则（关键）：
  - 本文件不写死任何实体/人物名——每次跑都从库里现读（build_world_view），
    内容整版换血后照样工作，这正是"质检随内容走、代码不随内容走"；
  - 校验规则全部来自契约（plans.py 的 steps 结构 + 表结构），不来自剧情；
  - validate_plan(world, plan) 是纯内存函数——M1.6 换入 LLM 新生成计划
    即反向校验器（防幻觉引用），同一入口。
"""
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]   # server/
sys.path.insert(0, str(BASE))

from app import db, plans  # noqa: E402

PASS, FAIL = "[OK]", "[FAIL]"


def _loads(raw, where, fallback=None):
    """JSON 列读出是字符串 → dict/list；坏 JSON 报错不中断。"""
    if raw is None:
        return fallback
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (ValueError, TypeError) as exc:
            print(f"  {FAIL} {where} 不是合法 JSON：{exc}")
            return fallback
    return raw


# ---------------------------------------------------------------------------
# 1. 世界视图加载：从库动态建"存在清单"。内容换血后重跑即自动重建。
# ---------------------------------------------------------------------------
def build_world_view():
    """读库建世界视图。

    Returns:
        dict: {"npc_ids": set, "items": {env_id: state}, "locations": {env_id: state},
               "plans": [plan dict]}（plans 取 seed 会话 active 计划 = 会话克隆后会执行的）
    """
    npc_ids = {r[0] for r in db.execute_query(
        "SELECT npc_id FROM character_card WHERE is_active=1")}
    items, locations = {}, {}
    for env_id, kind, state_raw in db.execute_query(
            "SELECT env_id, kind, state FROM environment_card"):
        st = _loads(state_raw, f"environment_card.{env_id}.state", fallback={}) or {}
        if kind == "location":
            locations[env_id] = st
        elif kind == "item":
            items[env_id] = st
    # 用公开 DAO：'seed' 会话 active 计划 = clone_seed_plans 会复制给每轮回的那批
    seed_plans = plans.get_active_plans("seed")
    return {"npc_ids": npc_ids, "items": items, "locations": locations,
            "plans": seed_plans}


def _env_kind(world, name):
    """实体名在世界里的种类：npc / item / location / None。"""
    if name in world["npc_ids"]:
        return "npc"
    if name in world["items"]:
        return "item"
    if name in world["locations"]:
        return "location"
    return None


# ---------------------------------------------------------------------------
# 2. 引用完整性：计划引用的场景/对象/状态键必须存在于世界视图。
#    env_id/npc_id 靠字符串一致关联——这层就是逐名核对"在不在清单里"。
# ---------------------------------------------------------------------------
def check_references(world, plan):
    """校验一个计划的引用完整性，返回错误行列表（空 = 引用全部闭合）。"""
    errs = []
    tag = f"{plan['npc_id']}/{plan['goal_id'] or plan['goal']}"
    for st in plan["steps"]:
        where = f"{tag} step{st.get('step')}"
        scene = st.get("scene")
        if scene and scene not in world["locations"]:
            errs.append(f"{where}.scene：'{scene}' 不是任何地点"
                        f"（现有：{sorted(world['locations'])})")
        tgt = st.get("target")
        if tgt and _env_kind(world, tgt) is None:
            errs.append(f"{where}.target：'{tgt}' 在世界里不存在"
                        f"（NPC {sorted(world['npc_ids'])} / 物品"
                        f" {sorted(world['items'])} / 地点 {sorted(world['locations'])})")
        for j, pre in enumerate(st.get("preconditions", []) or []):
            _check_ref_block(world, errs, f"{where}.preconditions[{j}]", pre)
        for j, eff in enumerate(st.get("effects", []) or []):
            _check_ref_block(world, errs, f"{where}.effects[{j}]", eff)
    return errs


def _check_ref_block(world, errs, where, block):
    """单个前置/效果块：check/set 的目标与状态键全部核一遍存在性。"""
    kind = block.get("check") or block.get("set")
    if kind == "env_state":
        t = block.get("target")
        k = _env_kind(world, t) if t else None
        if k not in ("item", "location"):
            errs.append(f"{where}.target：'{t}' 不是物品/地点（env_state 只能写环境卡）")
        else:
            state = world["items"].get(t) or world["locations"].get(t)
            key = block.get("key")
            if key and key not in state:
                errs.append(f"{where}.key：'{t}' 的 state 没有键 '{key}'"
                            f"（现有：{sorted(state)})")
    elif kind == "has_item":
        item = block.get("item")
        if item and item not in world["items"]:
            errs.append(f"{where}.item：'{item}' 不是任何物品"
                        f"（现有：{sorted(world['items'])})")
    elif kind in ("npc_status", "npc_alive"):
        t = block.get("target")
        if t and t not in world["npc_ids"]:
            errs.append(f"{where}.target：'{t}' 不是任何 NPC"
                        f"（现有：{sorted(world['npc_ids'])})")


# ---------------------------------------------------------------------------
# 3. 可达性：把效果按序应用到世界副本，评估每步前置能否满足。
#    blocked 后停止（链条断裂，后续不执行——与引擎挂起语义一致）。
# ---------------------------------------------------------------------------
def reachability(world, plan):
    """推演一个计划的可达性，返回错误行列表（空 = 全链可达）。

    世界模型（粗粒度、只读数据、不碰引擎时序）：
      - env_state 直接读环境/物品卡的 state 键；
      - item 持有：state['holder']；use_item 的效果会写 holder；
      - npc_alive：从推演副本 alive 字典读；set npc_status dead=true → alive=False。
      - effect 应用顺序 = steps 顺序；precondition 满足才应用该步效果。
    """
    errs = []
    tag = f"{plan['npc_id']}/{plan['goal_id'] or plan['goal']}"
    sim_items = {k: dict(v) for k, v in world["items"].items()}
    sim_locs = {k: dict(v) for k, v in world["locations"].items()}
    alive = {nid: True for nid in world["npc_ids"]}

    def env_state_of(target):
        return sim_items.get(target) or sim_locs.get(target)

    for st in plan["steps"]:
        where = f"{tag} step{st.get('step')}"
        # --- 评估全部前置 ---
        blocked_reason = None
        for pre in st.get("preconditions", []) or []:
            if not _pre_satisfied(pre, plan["npc_id"], env_state_of, alive):
                blocked_reason = f"{where} 前置不满足：{pre}"
                break
        if blocked_reason:
            errs.append(blocked_reason + " —— 链条在此断裂，该步及后续无干预下不会发生")
            break  # 与引擎一致：blocked 挂起，不再推演后续步
        # --- 应用效果 ---
        for eff in st.get("effects", []) or []:
            if eff.get("set") == "env_state":
                state = env_state_of(eff.get("target"))
                if state is not None:
                    state[eff["key"]] = eff["value"]
            elif eff.get("set") == "npc_status":
                t = eff.get("target")
                if eff.get("key") == "dead" and eff.get("value") is True:
                    alive[t] = False
    return errs


def _pre_satisfied(pre, npc_id, env_state_of, alive):
    """单条前置在推演世界里是否满足。"""
    kind = pre.get("check")
    if kind == "env_state":
        state = env_state_of(pre.get("target"))
        return bool(state and state.get(pre.get("key")) == pre.get("expected"))
    if kind == "has_item":
        state = env_state_of(pre.get("item"))
        return bool(state and state.get("holder") == npc_id)
    if kind == "npc_alive":
        return alive.get(pre.get("target")) is pre.get("expected")
    if kind == "npc_status":
        # npc_status 效果暂只模拟 dead；其余 key 视为无法预知 → 保守通过（留给 M1.5）
        if pre.get("key") == "dead":
            return (alive.get(pre.get("target")) is not pre.get("expected"))
        return True  # 未知 key 不阻塞（无数据可证伪，不误报）
    return True


def validate_plan(world, plan):
    """单计划完整校验（结构 → 引用 → 可达），返回 (errs, ok)。M1.6 反向校验复用此入口。"""
    errs = []
    for msg in plans.validate_plan_schema(plan):
        errs.append(f"[E] schema: {msg}")
    if errs:
        return errs, False
    ref_errs = check_references(world, plan)
    errs += [f"[R] {e}" for e in ref_errs]
    if ref_errs:
        return errs, False
    errs += [f"[X] {e}" for e in reachability(world, plan)]
    return errs, not errs


# ---------------------------------------------------------------------------
# main：加载世界 → 逐一校验 seed 计划 → 汇总输出 + 退出码
# ---------------------------------------------------------------------------
def main():
    print("读库构建世界视图 ……")
    world = build_world_view()
    n_npc, n_item, n_loc = (len(world["npc_ids"]), len(world["items"]),
                            len(world["locations"]))
    print(f"  世界视图：NPC {n_npc} · 物品 {n_item} · 地点 {n_loc} · 计划 {len(world['plans'])}")
    print(f"  名单：NPC {sorted(world['npc_ids'])}")
    print(f"        物品 {sorted(world['items'])}")
    print(f"        地点 {sorted(world['locations'])}")

    print("\n逐计划校验（结构 → 引用 → 可达）：")
    all_ok = True
    for p in world["plans"]:
        label = f"{p['npc_id']}/{p['goal_id'] or p['goal']} v{p['version']}"
        errs, ok = validate_plan(world, p)
        if ok:
            print(f"  {PASS} {label}")
        else:
            all_ok = False
            print(f"  {FAIL} {label}")
            for e in errs:
                print(f"        {e}")
    if not world["plans"]:
        print("  （seed 会话没有计划，跳过——内容换血后此处会提示建计划）")

    print()
    if all_ok:
        print("全部通过：初始世界状态可支撑全部种子计划（结构 + 引用 + 可达）。")
        return 0
    print("校验未通过——见上方 [E]/[R]/[X] 明细，修正 seed 数据后重跑本脚本。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
