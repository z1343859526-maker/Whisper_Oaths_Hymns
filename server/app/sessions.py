"""会话管理：游戏会话的创建与查询（M1.1，轮回机制的数据地基）。

一个会话 = 一局轮回。会话是所有运行时数据的隔离单位：
- 记忆（npc_memory）：先验('seed') + 本轮(session_id) 双可见，见 memory.recall_memories；
- 关系（relationships）：会话创建时从 seed 复制初始行，此后 UPDATE 只动本会话行；
- 计划（plans，M1.2）：会话创建时从 seed 复制出厂计划，重规划/推进只动本会话行；
- 状态（game_state）：KV 式，每会话一行组（current_tick/round_no/action_points/...）；
- 痕迹/对话日志（world_trace/dialogue_log）：按 session_id 落库，天然隔离。

轮回语义预留（M1.10 实现 reset 时收口）：
- reset = 创建新会话（round_no = 旧会话 +1），旧会话 status 置 'archived'；
- 多轮数据并存留档，M1.13 统计回归的 20 轮原料由此而来。

已知限制（D-01，见开发日志）：environment_card 仍是全局单例，
多活跃会话共享环境状态——单机单会话产品形态 + 串行回归下可接受，
M1.10 reset 实现环境卡 seed 重灌时一并处理。
"""
import json
import logging
import time
import uuid
from typing import Optional

from . import db
from . import world_pack

logger = logging.getLogger(__name__)

# 会话初始状态（KV）：开局时刻的世界参数。
# scene 不写死（09-09 需求：随模组自动更换），由 initial_state(world_id) 从
# 模组 manifest.json 的 initial_scene 读（test→room_1，golden→gate），加模组不改代码。
_INITIAL_STATE_BASE = {
    "current_tick": 0,     # 18:00，与 scheduler 的 tick 基准一致
    "round_no": 1,         # 轮回数：M1.10 reset 时由旧会话传承 +1
    "action_points": 100,  # 行动点 ≈ 可用 tick 数（tick 0~99 一天一夜）
    "status": "active",    # active / ended / archived
    # 玩家运行时属性：observation 观察力（0~100），影响"对象级观察"能展开多深的细节。
    "player_attrs": {"perception": 60},
}


def _initial_state(world_id: str = "test") -> dict:
    """构建该世界的会话初始状态：静态参数 + 模组决定的初始场景。"""
    return dict(_INITIAL_STATE_BASE, scene=world_pack.initial_scene(world_id))


def generate_session_id() -> str:
    """生成会话 id：sess_{时间戳}_{随机6位}。

    为什么不用纯 uuid：日志/调试时要肉眼分辨"这是哪一局"，
    可读前缀的成本为零；后接随机段保证全局唯一。
    """
    return f"sess_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"


def _active_plan_first_scene(session_id, npc_id):
    """从 NPC 当前激活计划的第一步取 scene，当作它的初始位置来源。

    计划就是在哪个场景发动的：test_man 第 1 步 wait scene=room_1 → 开局就在房间一守候。
    无计划/无 scene 返回 ""（该 NPC 位置仍未知 = 不在场，观察会读空场景）。
    """
    rows = db.execute_query(
        "SELECT steps FROM plans WHERE session_id=%s AND npc_id=%s AND status='active' "
        "ORDER BY id LIMIT 1",
        (session_id, npc_id),
    )
    if not rows or not rows[0][0]:
        return ""
    try:
        steps = rows[0][0]
        steps = json.loads(steps) if isinstance(steps, str) else steps
    except (ValueError, TypeError):
        return ""
    if isinstance(steps, list) and steps and isinstance(steps[0], dict):
        return (steps[0].get("scene") or "").strip()
    return ""


def _init_npc_positions(session_id):
    """开局给每个激活 NPC 设初始位置（轮回初始世界状态的一部分）。

    为什么放在会话初始化：npc_pos 是运行时世界状态，必须在开局那一帧写对——否则
    「NPC 明明在场景却观察不到」（观察物定位靠 npc_pos==scene 判断在场）。此前只有
    plan_executor 的 move 动作才写 npc_pos，导致开局所有 NPC 位置为 None。

    来源先用「激活计划第一步 scene」兜底（有计划的 test_man 开局即定位到出生场景，
    观察立即可见）。无计划 NPC（如 test_woman）暂无位置，属"位置未知"；后续加
    character_card.initial_scene 字段承载"出生地"语义时一并补上。
    """
    count = 0
    for npc_id in db.get_all_npc_ids(None):
        scene = _active_plan_first_scene(session_id, npc_id)
        if not scene:
            # 008 迁移：无计划 NPC 用角色卡出生地兜底（test_woman 此前位置恒 None，
            # 观察"空无一人"、自身决策不知所在——出生地是角色卡静态属性，不该由计划兼任）
            scene = db.get_initial_scene(npc_id)
        if scene:
            db.set_npc_pos(session_id, npc_id, scene)
            # 足迹系统：出生场景即已知（开局就"看得见"自己所在）
            try:
                from . import mind_engine
                mind_engine.note_visited(session_id, npc_id, scene)
            except Exception:  # noqa: BLE001
                pass
            count += 1
    return count


def start_session(world_id: str = "test") -> dict:
    """创建新会话：生成 id → 复位该世界环境卡到出厂 → 写初始 KV → 从 seed 复制初始关系。

    world_id：本局所在世界（环境卡按 world_id 隔离，005 迁移后）。
    为什么开工先复位环境卡（M1.10 语义，09-08 需求"每次游戏重启自动初始化"）：
    environment_card 是全局单例（D-01），不按会话隔离。上一局若把物品改了状态
    （如 knife 被拿成 held=player），会污染这一局——玩家开局就背着一把刀、房间却
    "空无一物"（出戏根因A）。因此"新一局"必须先把该世界环境卡从 initial_state 复位到出厂。
    复位放在显式"新会话"（start_session）而非"半永久/adhoc"（ensure_session，为保住旧前端
    连续对话不打断，不能复位），语义准确。

    Returns:
        {"session_id": str, "state": dict}，state 为 INITIAL_STATE 快照。
    """
    session_id = generate_session_id()
    try:
        reset_count = db.reset_environment_cards(world_id)
    except Exception:  # noqa: BLE001  某些世界环境表缺失不阻塞开局
        reset_count = 0
        logger.exception("开局复位环境卡失败（world=%s）——本局可能带上一局残留状态", world_id)
    # 09-09 需求：动态"谎称过"秘密（sec_lie_*）随每次开新局重置，避免上一局测试
    # 反复生成的客套谎跨局永久累积成"谎称过"清单（9/8 实测 test_man 积了 19 条）。
    # 只清 sec_lie_ 前缀（运行时谎言），保留角色卡预设核心秘密（sec_alliance 等）。
    try:
        cleared_lies = db.delete_dynamic_lies()
        if cleared_lies:
            logger.info("开局清空动态谎秘密 %d 条（sec_lie_*）", cleared_lies)
    except Exception:  # noqa: BLE001  清理失败不阻塞开局
        logger.exception("开局清空动态谎秘密失败——本局可能残留上一局谎言秘密")
    state = _initial_state(world_id)
    for key, value in state.items():
        db.upsert_game_state(session_id, key, value)
    cloned = db.clone_seed_relationships(session_id)
    cloned_plans = db.clone_seed_plans(session_id)
    init_pos = _init_npc_positions(session_id)
    logger.info("会话创建：%s（初始场景 %s；环境卡复位 %d 张；初始关系复制 %d 条，出厂计划复制 %d 份，NPC 初始定位 %d 个）",
                session_id, state.get("scene"), reset_count, cloned, cloned_plans, init_pos)
    return {"session_id": session_id, "state": state}


def _has_initialized_positions(session_id: str) -> bool:
    """判断该会话是否已初始化过 NPC 位置（任一 NPC 已定位即视为已初始化）。

    为什么用"任一已定位"：位置初始化是幂等的开局动作，一次性写全部 NPC；
    只要出现过一次，就认为本轮已铺过位置，避免每次请求都反复补写。
    用于 ensure_session 的"会话已存在但仍要补位置"判定。
    """
    for npc_id in db.get_all_npc_ids(None):
        if db.get_npc_pos(session_id, npc_id):
            return True
    return False


def ensure_session(session_id: str, world_id: str = "test") -> dict:
    """幂等确保会话存在：不存在则按新会话初始化（已存在则原样返回）。

    world_id：仅用于"新会话初始化"时决定初始场景；已存在会话时忽略（读已有状态）。

    为什么需要它：两类调用方没有显式开局动作——
    - /chat 未携带 session_id 的旧前端调用（落 'adhoc'）；
    - simulate 不指定会话时的兜底。
    行为等价于"半永久会话"：数据仍按会话记录（可观测不丢），
    只是没有隔离——旧前端的连续对话因此保住记忆/关系，不被破坏性升级。

    Returns:
        {"session_id": str, "state": dict}。
    """
    state = db.get_game_state_map(session_id)
    if state:
        # 幂等补 NPC 初始位置：会话可能早已存在（如开局的 adhoc / 旧前端），
        # 但 NPC 位置若未初始化（npc_pos 全 None），/scene/inspect 的 npcs 会为空，
        # 前端就不会出现「与 XX 交谈」入口。这里在"会话已存在"分支也补一次位置。
        if not _has_initialized_positions(session_id):
            _init_npc_positions(session_id)
        return {"session_id": session_id, "state": state}
    state = _initial_state(world_id)
    for key, value in state.items():
        db.upsert_game_state(session_id, key, value)
    cloned = db.clone_seed_relationships(session_id)
    cloned_plans = db.clone_seed_plans(session_id)
    init_pos = _init_npc_positions(session_id)
    logger.info("会话兜底初始化：%s（初始场景 %s；初始关系复制 %d 条，出厂计划复制 %d 份，NPC 初始定位 %d 个）",
                session_id, state.get("scene"), cloned, cloned_plans, init_pos)
    return {"session_id": session_id, "state": state}


def get_session(session_id: str) -> Optional[dict]:
    """查会话当前状态；不存在返回 None。

    与 ensure_session 的区别：本函数只读不创建——
    查询类端点（GET /session/state）用"404 会话不存在"暴露前端漏调
    /session/start 的 bug，而不是静默新建一个空会话掩盖它。
    """
    state = db.get_game_state_map(session_id)
    if not state:
        return None
    return {"session_id": session_id, "state": state}
