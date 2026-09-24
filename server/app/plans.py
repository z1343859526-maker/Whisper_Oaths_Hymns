"""计划：BDI 意图层（M1.2，涌现引擎的行为层心脏）。

BDI 三层在 NPC 系统里的落位：
- Belief    信念：npc_memory / beliefs / world_knowledge（他以为世界是什么样）
- Desire    愿望：goals 表（他想要什么——一行人话，无执行语义）
- Intention 意图：本模块 plans 表（他承诺怎么做——机器可执行的多步骤路径）

steps JSON 结构契约（M1.4 校验器 / M1.5 执行器 / M1.6 重规划器的共同接口）：

[
  {
    "step": 1,                    # 步骤编号，从 1 起连续递增
    "action_type": "use_item",    # 与 agent.ACTION_TYPES 八类一致（verify_m1_2 有一致性断言）
    "target": "cellar_key",       # 作用对象 id（物品 env_id / 人物 npc_id / 地点 env_id）
    "scene": "isabella_room",     # 执行地点 env_id（move 类填目标地点）
    "time_window": [36, 40],      # tick 闭区间；18:00=tick0，10 分钟/tick（与 scheduler 一致）
    "preconditions": [            # 全部满足才执行（AND 语义）
      {"check": "env_state", "target": "cellar_key", "key": "state", "expected": "in_place"}
    ],
    "effects": [                  # 执行成功后对世界的改变（M1.5 执行器按 set 枚举落库）
      {"set": "env_state", "target": "cellar_key", "key": "state", "value": "held"},
      {"set": "npc_status", "target": "duke_roderick", "key": "dead",
       "value": true, "delay_tick": 6}
    ],
    "note": "从床头花盆下取出地窖钥匙"      # 可选：人话备注（旁白 / 审计）
  }
]

check 枚举（前置条件怎么验，M1.5 实现读取）：
- env_state  环境卡状态    必填 target(env_id) / key / expected
- has_item   持有物品      必填 item；可选 holder（缺省=计划主人，读物品卡 state.holder）
- npc_alive  NPC 存活      必填 target(npc_id) / expected
- npc_status NPC 状态      必填 target(npc_id) / key / expected

set 枚举（效果怎么落库，存储约定）：
- env_state  写 environment_card.state[target].key = value
             （物品转移也走这里：改物品卡的 state/holder——与 fate._apply_use_item 语义对齐）
- npc_status 写 game_state['npc_status:{target}']（dict 合并），支持 delay_tick 延迟生效
             （M1.5 执行器维护 pending 队列：到点才落库，世界线因此有"深夜下毒、凌晨身亡"）

status 状态机：active → blocked（前置不满足）→ replanning（M1.6 LLM 重规划中）
    → active（换路径成功，旧行置 replaced）/ abandoned（彻底无路，合法世界线）
    active → completed（全部步骤执行完）
    replaced 仅由重规划产生：与 abandoned 必须区分——世界线审计里
    "夫人换了个杀法"和"夫人不杀了"是两条完全不同的故事（M1.13 分叉统计的原料）。
"""
import json
import logging

from . import db

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 枚举常量：单一事实源。M1.4 校验器 / M1.5 执行器 / M1.6 重规划器一律 import 这里，
# 禁止散落魔法字符串——枚举改名只改一处，漂移由 verify_m1_2 的一致性断言兜住。
# ---------------------------------------------------------------------------
PLAN_STATUS = ("active", "blocked", "replanning", "abandoned", "completed", "replaced")
STICKINESS = ("high", "normal", "low")        # high=换手段不换目标 / normal=可推迟 / low=可放弃
PLAN_SOURCE = ("handwritten", "llm")           # 策划手写 / LLM 重规划生成
CHECK_TYPES = ("env_state", "has_item", "npc_alive", "npc_status")
EFFECT_SETS = ("env_state", "npc_status")

# 与 agent.ACTION_TYPES 八类保持一致（move/use_item/give_item/speak/observe/wait/
# interact/trigger_event，v0.4 新增 converse 第 9 类）。不直接 import agent：那会连带
# 实例化 LLM 客户端，让"纯数据层"背上网络依赖；一致性改由 verify_m1_2 的断言保证
# （契约测试：双向校验 PLAN_ACTION_TYPES ⊆ agent.ACTION_TYPES 且逐项相等，防断更）。
PLAN_ACTION_TYPES = (
    "move", "use_item", "give_item", "speak",
    "observe", "wait", "interact", "trigger_event",
    "converse",
)

MAX_TICK = 99  # 一天一夜 0~99，与 scheduler 基准一致


class PlanValidationError(ValueError):
    """计划结构不合法（schema 校验失败）。errors 属性携带全部错误描述，一次报全。"""

    def __init__(self, errors):
        self.errors = errors
        super().__init__("; ".join(errors))


# ---------------------------------------------------------------------------
# schema 结构校验（纯结构，零 DB 访问；引用存在性/可达性校验属 M1.4 validate_seed）
# 为什么"收集全部错误"而不是第一条就抛：策划修数据时一次看到所有问题，
# 改一遍再验，而不是改一处跑一次（fail-fast 对机器友好，对人不友好）。
# ---------------------------------------------------------------------------
def _check_precondition(idx: int, pre: dict, errors: list):
    where = f"preconditions[{idx}]"
    check = pre.get("check")
    if check not in CHECK_TYPES:
        errors.append(f"{where}.check 必须是 {CHECK_TYPES} 之一，收到 {check!r}")
        return
    if check == "env_state" or check == "npc_status":
        for field in ("target", "key", "expected"):
            if field not in pre:
                errors.append(f"{where}: check={check} 缺必填字段 {field}")
    elif check == "has_item":
        if "item" not in pre:
            errors.append(f"{where}: check=has_item 缺必填字段 item")
    elif check == "npc_alive":
        for field in ("target", "expected"):
            if field not in pre:
                errors.append(f"{where}: check=npc_alive 缺必填字段 {field}")


def _check_effect(idx: int, eff: dict, errors: list):
    where = f"effects[{idx}]"
    setter = eff.get("set")
    if setter not in EFFECT_SETS:
        errors.append(f"{where}.set 必须是 {EFFECT_SETS} 之一，收到 {setter!r}")
        return
    for field in ("target", "key", "value"):
        if field not in eff:
            errors.append(f"{where}: set={setter} 缺必填字段 {field}")
    if "delay_tick" in eff:
        dt = eff["delay_tick"]
        if not isinstance(dt, int) or isinstance(dt, bool) or dt < 1:
            errors.append(f"{where}: delay_tick 必须是 >=1 的整数（到点前效果不生效），收到 {dt!r}")


def validate_plan_schema(plan: dict) -> list:
    """校验一个 plan dict 的结构合法性，返回错误列表（空列表 = 合法）。

    只管结构（字段齐全/枚举合法/编号连续），不管引用（target 的 env_id 是否
    真实存在、计划链是否可达）——引用校验是 M1.4 validate_seed 的职责，
    届时它会在本函数之后追加引用层检查（结构 → 引用 → 可达，三层递进）。
    """
    errors = []
    if not plan.get("npc_id"):
        errors.append("顶层缺必填字段 npc_id")
    if not plan.get("goal"):
        errors.append("顶层缺必填字段 goal")
    if plan.get("stickiness", "normal") not in STICKINESS:
        errors.append(f"stickiness 必须是 {STICKINESS} 之一，收到 {plan.get('stickiness')!r}")
    if plan.get("source", "handwritten") not in PLAN_SOURCE:
        errors.append(f"source 必须是 {PLAN_SOURCE} 之一，收到 {plan.get('source')!r}")
    if plan.get("status", "active") not in PLAN_STATUS:
        errors.append(f"status 必须是 {PLAN_STATUS} 之一，收到 {plan.get('status')!r}")

    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        return errors + ["steps 必须是非空数组"]

    for i, st in enumerate(steps):
        where = f"steps[{i}]"
        if not isinstance(st, dict):
            errors.append(f"{where} 必须是对象")
            continue
        if st.get("step") != i + 1:
            errors.append(f"{where}.step 应为 {i + 1}（从 1 起连续递增），收到 {st.get('step')!r}")
        if st.get("action_type") not in PLAN_ACTION_TYPES:
            errors.append(f"{where}.action_type 必须是八类之一 {PLAN_ACTION_TYPES}，收到 {st.get('action_type')!r}")
        tw = st.get("time_window")
        if (not isinstance(tw, list) or len(tw) != 2
                or not all(isinstance(t, int) and not isinstance(t, bool) for t in tw)):
            errors.append(f"{where}.time_window 必须是 [start, end] 两个整数，收到 {tw!r}")
        elif tw[0] > tw[1]:
            errors.append(f"{where}.time_window 起点不能晚于终点，收到 {tw}")
        elif tw[0] < 0 or tw[1] > MAX_TICK:
            errors.append(f"{where}.time_window 超出 0~{MAX_TICK} 范围，收到 {tw}")
        for j, pre in enumerate(st.get("preconditions", []) or []):
            _check_precondition(j, pre, errors)
        for j, eff in enumerate(st.get("effects", []) or []):
            _check_effect(j, eff, errors)
    return errors


# ---------------------------------------------------------------------------
# 数据访问（DAO）：SQL 全部参数化；写操作只动本会话的行（R8 状态串扰防线）。
# ---------------------------------------------------------------------------
def _row_to_plan(row) -> dict:
    """结果行 → plan dict（steps 反序列化为 list，其余原样）。"""
    return {
        "id": row[0], "session_id": row[1], "npc_id": row[2], "goal_id": row[3],
        "goal": row[4], "stickiness": row[5], "status": row[6],
        "current_step": row[7], "version": row[8],
        "steps": json.loads(row[9]) if row[9] else [],
        "source": row[10], "blocked_reason": row[11],
    }


_PLAN_COLS = ("id, session_id, npc_id, goal_id, goal, stickiness, status, "
              "current_step, version, steps, source, blocked_reason")


def create_plan(session_id, npc_id, goal, steps,
                stickiness="normal", source="handwritten", goal_id="", version=1) -> int:
    """插入一条计划（schema 校验失败抛 PlanValidationError），返回新行 id。

    入库守卫：结构非法的计划在这里就被拒——"数据错了不报错、安静地错着"
    是种子数据最阴险的 bug（M1.1 开发日志的教训），入口把关优于事后排查。
    """
    plan = {"npc_id": npc_id, "goal": goal, "steps": steps,
            "stickiness": stickiness, "source": source, "version": version}
    errors = validate_plan_schema(plan)
    if errors:
        raise PlanValidationError(errors)
    sql = (
        "INSERT INTO plans "
        "(session_id, npc_id, goal_id, goal, stickiness, status, current_step, version, steps, source) "
        "VALUES (%s, %s, %s, %s, %s, 'active', 1, %s, %s, %s)"
    )
    conn = db.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, (session_id, npc_id, goal_id, goal,
                                 stickiness, version,
                                 json.dumps(steps, ensure_ascii=False), source))
            new_id = cursor.lastrowid
        conn.commit()
        logger.info("计划创建：%s/%s v%s（%d 步，source=%s）",
                    session_id, npc_id, version, len(steps), source)
        return new_id
    finally:
        conn.close()


def get_active_plans(session_id) -> list:
    """查某会话全部 active 计划（M1.5 tick 引擎每轮的入口查询）。"""
    sql = f"SELECT {_PLAN_COLS} FROM plans WHERE session_id=%s AND status='active'"
    return [_row_to_plan(r) for r in db.execute_query(sql, (session_id,))]


def get_active_plan(session_id, npc_id):
    """查某会话某 NPC 的 active 计划；无则 None。"""
    sql = (f"SELECT {_PLAN_COLS} FROM plans "
           "WHERE session_id=%s AND npc_id=%s AND status='active' LIMIT 1")
    rows = db.execute_query(sql, (session_id, npc_id))
    return _row_to_plan(rows[0]) if rows else None


def get_plan_history(session_id, npc_id) -> list:
    """查某 NPC 在某会话的全部计划版本（按 version 升序）。

    版本链 = 世界线分叉审计的原料：M1.13 统计"同干预 5 轮 ≥2 种重规划路径"
    就是数这棵链的形状；M1.2 先把链留好，统计是水到渠成。
    """
    sql = (f"SELECT {_PLAN_COLS} FROM plans "
           "WHERE session_id=%s AND npc_id=%s ORDER BY version")
    return [_row_to_plan(r) for r in db.execute_query(sql, (session_id, npc_id))]


def advance_step(session_id, plan_id) -> dict:
    """推进计划的 current_step；末步执行完则置 completed。返回更新后的 plan。

    为什么末步在函数里判定而不是留给调用方：状态机迁移规则（active→completed）
    收口在 DAO 一处，M1.5 执行器只管"执行成功就 advance"，不背状态机细节。

    ⚠️ SET 子句顺序不可调换（M1.2 调试中踩过的真实坑）：
    MySQL 的 UPDATE SET 从左到右求值，后面的 SET 能看到前面 SET 的新值
    （与标准 SQL"全语句读旧值"不同）。status 的 completed 判定必须读
    current_step 的旧值——所以 status 在前、current_step 推进在后。
    顺序反了会导致：推进到末步的那一次提前置 completed（off-by-one），
    下一次调用 WHERE status='active' 匹配不到而误报"计划不存在"。
    """
    sql = (
        "UPDATE plans SET "
        "status = CASE WHEN current_step >= JSON_LENGTH(steps) THEN 'completed' ELSE status END, "
        "current_step = CASE WHEN current_step < JSON_LENGTH(steps) THEN current_step + 1 "
        "                    ELSE current_step END "
        "WHERE session_id=%s AND id=%s AND status='active'"
    )
    conn = db.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, (session_id, plan_id))
            if cursor.rowcount == 0:
                raise ValueError(f"计划 {plan_id} 不存在或不处于 active 状态，无法推进")
        conn.commit()
    finally:
        conn.close()
    rows = db.execute_query(f"SELECT {_PLAN_COLS} FROM plans WHERE id=%s", (plan_id,))
    return _row_to_plan(rows[0])


def mark_status(session_id, plan_id, status, blocked_reason=None):
    """迁移计划状态（blocked/replanning/abandoned/...）。非 active 计划不可再迁移（防僵尸复活）。"""
    if status not in PLAN_STATUS:
        raise ValueError(f"status 必须是 {PLAN_STATUS} 之一，收到 {status!r}")
    sql = ("UPDATE plans SET status=%s, blocked_reason=%s "
           "WHERE session_id=%s AND id=%s AND status='active'")
    conn = db.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, (status, blocked_reason, session_id, plan_id))
            affected = cursor.rowcount
        conn.commit()
        return affected
    finally:
        conn.close()


def replace_plan(session_id, old_plan_id, new_steps, blocked_reason=None) -> int:
    """重规划落库（M1.6 的出口）：旧计划置 replaced，新计划以 version+1 接棒。

    事务性（db.execute_transaction）：两行必须同时成立——半程失败会让 NPC
    "无 active 计划裸奔"且无法重试自愈。goal/goal_id/stickiness 继承旧行：
    重规划换的是路径，不是目标（目标级放弃走 mark_status('abandoned')）。
    新计划 source='llm'、current_step=1 从头执行；new_steps 结构非法当场抛错，
    非法产物绝不入库（这是 M1.6 反向校验的最后一道闸，M1.4 校验器复用同一套 schema）。
    """
    rows = db.execute_query(
        f"SELECT {_PLAN_COLS} FROM plans WHERE session_id=%s AND id=%s",
        (session_id, old_plan_id))
    if not rows:
        raise ValueError(f"旧计划 {old_plan_id} 不存在")
    old = _row_to_plan(rows[0])
    errors = validate_plan_schema(
        {"npc_id": old["npc_id"], "goal": old["goal"], "steps": new_steps})
    if errors:
        raise PlanValidationError(errors)

    new_id_holder = {}
    stmts = [
        ("UPDATE plans SET status='replaced', blocked_reason=%s "
         "WHERE session_id=%s AND id=%s AND status IN ('active','blocked')",
         (blocked_reason or "重规划替换", session_id, old_plan_id)),
        ("INSERT INTO plans "
         "(session_id, npc_id, goal_id, goal, stickiness, status, current_step, version, steps, source) "
         "VALUES (%s, %s, %s, %s, %s, 'active', 1, %s, %s, 'llm')",
         (session_id, old["npc_id"], old["goal_id"], old["goal"], old["stickiness"],
          old["version"] + 1, json.dumps(new_steps, ensure_ascii=False))),
    ]
    conn = db.get_connection()
    try:
        with conn.cursor() as cursor:
            for sql, params in stmts:
                cursor.execute(sql, params)
            new_id_holder["id"] = cursor.lastrowid
        conn.commit()
    finally:
        conn.close()
    logger.info("计划重规划：%s/%s v%s → v%s（旧计划 %s 置 replaced）",
                session_id, old["npc_id"], old["version"], old["version"] + 1, old_plan_id)
    return new_id_holder["id"]


# ---------------------------------------------------------------------------
# 世界时序 v2 / T6：封锁计划查询 + 从零建计划（重规划器的数据源与出口）
# ---------------------------------------------------------------------------

def get_blocked_plans(session_id) -> list:
    """读本会话所有 blocked 计划（重规划器的输入：受阻 → resolve_plan 策略 → LLM 重规划）。"""
    rows = db.execute_query(
        f"SELECT {_PLAN_COLS} FROM plans WHERE session_id=%s AND status='blocked'",
        (session_id,))
    return [_row_to_plan(r) for r in rows]


def create_plan(session_id, npc_id, goal_id, goal, stickiness, steps, source="llm") -> int:
    """从零创建一条计划（"有 Desire 无 Intention"的 NPC 首次规划——如 test_woman）。

    与 replace_plan 的分工：replace 是"换路径"（旧计划置 replaced 接棒），
    create 是"无中生有"（此前从未有过 Intention）。同样过 schema 校验——
    非法产物绝不入库（M1.4 校验器同一套规则）。
    Returns:
        新计划 id。
    Raises:
        PlanValidationError：steps 结构非法。
    """
    errors = validate_plan_schema({"npc_id": npc_id, "goal": goal, "steps": steps})
    if errors:
        raise PlanValidationError(errors)
    conn = db.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO plans "
                "(session_id, npc_id, goal_id, goal, stickiness, status, current_step, version, steps, source) "
                "VALUES (%s, %s, %s, %s, %s, 'active', 1, 1, %s, %s)",
                (session_id, npc_id, goal_id, goal, stickiness,
                 json.dumps(steps, ensure_ascii=False), source))
            new_id = cursor.lastrowid
        conn.commit()
        return new_id
    finally:
        conn.close()
