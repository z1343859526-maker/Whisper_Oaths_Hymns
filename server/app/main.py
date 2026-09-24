"""Web 入口层：定义 FastAPI 应用、路由与请求/响应模型。

职责：
- 接收客户端（Godot / /docs 测试页）发来的 HTTP 请求；
- 把请求体交给 Pydantic 校验，交给 context_builder 组装上下文，再交给下层 DeepSeekClient 处理；
- 把结果封装成 JSON 响应返回给前端。

设计约束：保持"薄入口"——路由函数只做「接请求 → 调下层 → 回结果」，
不在这一层写业务逻辑，保证上层像 llm.py 一样面向接口、职责单一。
"""
import json
import logging

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse   # ← 新增：流式响应工具

from pydantic import BaseModel

from . import db
from . import sessions
from . import mind_engine   # 心智引擎（v0.3）：有 mental_model 的角色走"理解→管线→生成"
from . import world as world_mod      # 在线世界时钟：/chat 后自动推 tick、/world/step 手动推
from . import recorder                # 上帝视角记录器（/debug/overview 数据源）
from . import world_pack               # 世界模组包（说书人/词典/词表/模板）
from . import npc_loader               # NPC 模组加载器：一个 NPC=一个定义文件，自动注册 AI 卡
from . import debug_trace   # 调试追踪：记录发给AI的话/返回/解析/失败，GET /debug/trace 可查
from . import intent as intent_mod     # 意图识别：对话路径也判定"这句实为行动"（边说话边做行动）
from . import spatial as spatial_mod   # 空间可达性：方案①玩家移动"位置延后落地"的can_reach校验
from .llm import DeepSeekClient
from .context_builder import build_messages, run_environment   # P4-A：按 npc_id 组装角色人设；npc_id='' 走环境运行时入口
from .memory import remember_conversation   # P4-B：对话后写回记忆
from .relationship import adjust_relationship   # P4-C：对话后更新关系
from .spatial import get_held_items, build_perception_snapshot   # 背包数据源 + 场景动态感知快照
from . import conversation as conversation_mod   # 对话会话状态机 + 仲裁（v0.4）：邀请/应答/轮次/结束
from . import agent as agent_mod                  # reject_and_advance：被拒后按优先级递推意图


def _run_mind_pipeline(npc_id: str, session_id: str, world_id: str, message: str) -> dict or None:
    """心智管线（LLM①意图理解 → 引擎走链 → 中间态）。

    只对配置了 mental_model 的角色生效（测试世界）；黄金乡等未迁移角色返回 None，
    走旧四段拼接——两条路线互不干扰。任何失败都降级，不挡对话主链路。
    LLM 预算：本函数 1 次（理解），回复生成另有 1 次，合计 ≤2（v4 铁律）。
    """
    try:
        if not npc_id or not mind_engine.has_mental_model(npc_id):
            return None
        u_raw = llm.chat(mind_engine.build_understanding_messages(npc_id, message, world_id))
        understanding = mind_engine.parse_understanding(u_raw)
        return mind_engine.process_dialogue(npc_id, session_id, world_id, message, understanding)
    except Exception as e:  # noqa: BLE001
        logger.warning("心智管线失败，降级为基础拼接：%s", e)
        return None


# 日志器：模块级 logger，用 __name__ 命名（这里是 app.main），
# 方便在多文件项目里按模块追踪错误来源
logger = logging.getLogger(__name__)

# FastAPI 应用实例：title/description 会显示在 /docs 自动文档页上
app = FastAPI(
    title="黄金乡谋杀案 · AI NPC 后端",
    description="AI NPC 对话与行为系统技术原型",
    version="0.1.0",
)

# 依赖实例：LLM 客户端在整个服务生命周期内复用同一份连接配置
llm = DeepSeekClient()


class ChatRequest(BaseModel):
    """聊天请求体：POST /chat 时前端必须提交的结构。

    字段名 message 由本模型定义——前端只能改它的值，不能改字段名。
    """

    message: str
    npc_id: str = ""   # P4-A：这句话是哪个 NPC 说的（"" = 环境直接行动，走旁白）
    session_id: str = ""   # M1.1：会话标识（/session/start 获得）；缺省落 adhoc 兼容旧前端
    world_id: str = "golden"   # 世界维度：不同世界不同 RAG 库（golden=黄金乡 / test=测试世界）
    scene: str = ""   # 客户端当前房间 id（= GameState.location_id）：环境观察/行动用，消除场景同步时序问题


class SessionLocationRequest(BaseModel):
    """玩家移动同步请求体：POST /session/location 时提交。

    客户端地图移动成功后调用，把新位置写进后端 game_state['scene']，
    让后端旁白读到的"你所在"与客户端实际位置一致。
    """
    session_id: str = ""   # 会话标识（缺省落 adhoc，与 /chat 兜底一致）
    scene: str             # 目标场景 id（= 客户端 location_id，测试世界为 room_1/2/3）
    world_id: str = "golden"   # 世界维度（写游戏状态用；目前 scene 命名空间按世界区分）


class SessionMoveRequest(BaseModel):
    """点地图移动请求体：POST /session/move 时提交。

    与 SessionLocationRequest（只写 scene）不同：target 需校验可达性，移动会写世界痕迹并
    推进一个世界 tick（方向1：移动即行动、即流逝，与"输入移动"体验一致）。
    """
    session_id: str = ""   # 会话标识（缺省落 adhoc）
    world_id: str = "test"   # 世界维度
    target: str = ""   # 目标场景 id（= 客户端 location_id，如 room_2）


class ConversationAcceptRequest(BaseModel):
    """玩家响应"某 NPC 想跟你对话"的邀请。

    /conversation/accept 请求体：玩家选同意/拒绝后提交。
    """
    session_id: str = ""
    world_id: str = "test"
    accept: bool = True        # True=同意对话；False=拒绝
    npc_id: str = ""           # 发起对话邀请的 NPC id（必填，定位用）
    scene: str = ""            # 玩家当前场景（对话发生地，可选，缺省用邀请里的场景）
    max_rounds: int = 5        # 本场对话轮数上限（可选，缺省 5；供前端/配置动态调整）


class ConversationTurnRequest(BaseModel):
    """对话进行中玩家回一句话。

    /conversation/turn 请求体：会话期间前端逐句提交。
    """
    session_id: str = ""
    world_id: str = "test"
    message: str = ""          # 玩家回的话
    npc_id: str = ""           # 当前对话对象（校验用，可选）


class ConversationStartRequest(BaseModel):
    """玩家【主动】发起与某 NPC 的对话。

    /conversation/start 请求体：玩家点「与X交谈」入口时提交。
    与 /conversation/accept（NPC 邀你→你裁决）方向相反：这里是你找对方。
    """
    session_id: str = ""
    world_id: str = "test"
    npc_id: str = ""           # 你想对话的 NPC id（必填）
    scene: str = ""            # 玩家当前场景（对话发生地，可选，缺省取 NPC 所在）
    max_rounds: int = 5        # 本场对话轮数上限（可选，缺省 5；供前端/配置动态调整）


@app.post("/session/move")
def session_move(req: SessionMoveRequest):
    """点地图移动（方向1）：把"移动"做成一次完整的世界行动。

    背景：原 /session/location 只写 game_state['scene']，不做校验/不写痕迹/不推 tick——
    导致"点地图后旁白看到房间2，但环境执行器读 get_player_scene 仍是旧房间、动作落在
    房间1、NPC 也感知玩家在旧处"。本接口按 environment._exec_move 语义做：

      ① 校验可达（空间连通 + 门开没开）→ 不可达返回 blocked；
      ② 移动即行动 → 推进一个世界 tick（world_mod.advance_one）——**玩家位置延后落地**；
      ③ 结算完成后才写 scene + 世界痕迹（"从X移动到Y"）。

    ⚠️ 09-10 方案①（设计决定）核心：**玩家位置延后落地**。旧实现是先"写 player scene=dest"
    再 advance_one，导致这格结算期间 `db.get_player_scene` 已是 dest —— room_2 的 NPC
    （如 test_woman）会"看见玩家来了房间二"并想搭话，room_1 的 test_man 会说"你去了房间二"
    （预知玩家移动）。改为：先校验（不落地）→ 推 tick（结算期间玩家仍在旧场景，NPC 决策/
    导演聚合读到的是旧场景，不预知）→ 结算完成后才落地玩家位置到 dest + 写移动痕迹。

    Returns:
        {"session_id","moved","scene","message","tick"}。
    """
    session_id = req.session_id.strip() or "adhoc"
    sessions.ensure_session(session_id)
    dest = req.target.strip()
    world_id = req.world_id
    old_scene = db.get_player_scene(session_id) or ""
    cur_tick = int(db.get_game_state_map(session_id).get("current_tick", 0) or 0)

    # ① 校验可达（只校验，不落地玩家位置——落地在结算完成后做，见 ③）
    if not dest:
        return {"session_id": session_id, "moved": False, "scene": old_scene,
                "message": "你要去哪？", "tick": cur_tick}

    # ⓪ 世界正等人裁决对话邀请 → 移动【整段不做】：不登记意图（登记了也没人消费）、不推 tick、
    #    也【不落地位置】。旧行为是照常 append 意图 + advance_one（一撞挂起态就空返回）+
    #    照常写 scene —— 结果"地点后端已变、世界那一格却没结算"，前端既无到达画面又无提示，
    #    玩家只看到"卡住"（2026-09-10 实测）。这里改为明确回报挂起原因，交前端弹邀请。
    _paused = world_mod.pending_offer_fields(session_id)
    if _paused:
        return {"session_id": session_id, "moved": False, "scene": old_scene,
                "message": _paused["notice"], "tick": cur_tick, **_paused}

    if old_scene == dest:
        return {"session_id": session_id, "moved": False, "scene": old_scene,
                "message": "你已经在" + dest, "tick": cur_tick}
    if not spatial_mod.can_reach(old_scene, dest, world_id):
        # 与原 _exec_move 一致的"路不通"痕迹：空间逻辑可被交互感知确认
        db.add_world_trace(session_id, cur_tick, "player", "move", dest, old_scene,
                           f"试图从 {old_scene} 走向 {dest}，但路不通（门关着或不相连）")
        return {"session_id": session_id, "moved": False, "scene": old_scene,
                "message": "你过不去——这条路不通。", "tick": cur_tick}

    # ② 方案①：先推 tick —— 玩家位置仍是 old_scene，NPC 决策/导演聚合读 get_player_scene
    #    得到旧场景，不会预知"玩家已到 dest"（这是预知破窗的根因）。阻塞至本 tick 结算完成。
    # 09-10 A（设计决定）：把"移动"登记成玩家意图进池，导演那一格才正确识别"玩家正离开
    #    old_scene 前往 dest"，而不是把玩家判成"原地等待(wait)"。位置延后落地，故 scene=old_scene；
    #    advance_one 内部会 drain_player_intents 取出，导演/意图链都能看到玩家"move→dest"。
    db.append_player_intent(session_id, {
        "intent": {"verb": "move", "target": {"id": dest}},
        "text": f"移动到{dest}",
        "scene": old_scene,
    })
    # 09-10：advance_one 的返回值必须接住——None=没抢到锁（上一格还在推）/ paused=本格刚
    # 产生"邀请玩家对话"。两者都意味着"这一格没结算"，此时【位置不能落地】（否则又回到
    # "地点已变、世界没走"的错位），改为把原因透传前端（前端据此弹邀请或提示稍后再试）。
    _timing = world_mod.world_timing_fields(world_mod.advance_one(session_id, world_id))
    if _timing.get("world_paused") or _timing.get("world_skipped"):
        return {"session_id": session_id, "moved": False, "scene": old_scene,
                "message": _timing.get("notice", ""), "tick": cur_tick, **_timing}

    # ③ 结算完成后才落地玩家位置 + 写移动痕迹（"移动在 tick 内结算、位置延后落地"）。
    new_tick = int(db.get_game_state_map(session_id).get("current_tick", 0) or 0)
    db.upsert_game_state(session_id, "scene", dest)
    db.add_world_trace(session_id, new_tick, "player", "move", dest, old_scene,
                       f"从 {old_scene} 移动到 {dest}")
    return {"session_id": session_id, "moved": True, "scene": dest,
            "message": f"你来到了{dest}。", "tick": new_tick}


@app.get("/")
def root():
    """根路径：给前端/调试用的欢迎信息。

    Returns:
        服务名与运行状态，由 FastAPI 自动序列化为 JSON。
    """
    return {"service": "golden-murder-backend", "status": "running"}


@app.get("/health")
def health():
    """健康检查：上线时探活用，返回服务是否正常。

    Returns:
        固定返回 {"status": "ok"}，用于确认服务已就绪。
    """
    return {"status": "ok"}


@app.post("/session/start")
def session_start(world_id: str = "test"):
    """开局：创建新会话（M1.1，一局轮回的开始）。

    world_id：本局所在世界。后端据此复位该世界环境卡到出厂（M1.10，09-08 需求
    "每次游戏重启自动初始化"）——避免上一局物品状态（如刀被拿成 held）污染本局。

    Returns:
        {"session_id": ..., "state": {current_tick, round_no, scene, action_points, status}}。
        前端此后每次 /chat 带上这个 session_id，记忆/关系/日志按会话隔离。
    """
    return sessions.start_session(world_id)


@app.get("/session/state")
def session_state(session_id: str):
    """查会话当前状态（时钟/轮回数/行动点/场景）。

    只读不创建：不存在的会话返回 404——把前端漏调 /session/start 的 bug
    暴露出来，而不是静默新建一个空会话掩盖它。
    """
    info = sessions.get_session(session_id)
    if info is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return info


@app.post("/session/location")
def session_location(req: SessionLocationRequest):
    """玩家在地图上移动后，把新位置同步回后端（M1.7 场景权威的两端对齐第一步）。

    Godot 端地图点击只改本地 location_id、不通知后端，导致后端旁白读 get_player_scene
    得到旧位置（"你所在"错）。此接口把新位置写进 game_state['scene']，此后 /chat 的
    环境路径（_environment_messages）读到的"你所在"就与客户端一致。

    scene 键与客户端 location_id 对齐：测试世界为 room_1/room_2/room_3（与空间骨架表
    environment_entity.scene 同值），黄金乡为 front_yard/hall 等。

    Returns:
        {"session_id", "scene", "state"}，state 为主要游戏状态一键取回（含更新后的 scene）。
    """
    session_id = req.session_id.strip() or "adhoc"
    sessions.ensure_session(session_id)
    scene = req.scene.strip()
    if scene:
        db.upsert_game_state(session_id, "scene", scene)
    info = sessions.get_session(session_id)
    return {"session_id": session_id, "scene": scene, "state": info["state"] if info else {}}


@app.get("/session/notices")
def session_notices(session_id: str = ""):
    """即时提示轮询端点（09-10 新增）：取走后端"正在忙什么"的提示并清空池。

    为什么需要它：玩家自由输入触发的解析层"再解析一次"发生在 /chat 这个【同步阻塞】
    请求内部——请求没返回，前端就拿不到中途进度，只能干等（那一格世界结算还要几十秒）。
    后端在触发重试时立刻把提示（如"事情比想象中复杂……"）推进提示池，前端在忙碌期间
    轮询本端点，就能【在等待中】看到进度，而不是盯着"命运的齿轮"怀疑卡死。

    排干式语义（取走即清空）：同一条提示不会重复显示，前端无需去重。
    """
    sid = session_id.strip() or "adhoc"
    return {"session_id": sid, "notices": db.take_notices(sid)}


@app.get("/world/updates")
def world_updates(session_id: str, world_id: str = "test", since_tick: int = -1, limit: int = 60,
                  until_tick: int = -1):
    """世界变化提示（玩家挂机/时间流逝后的场景更新数据源）。

    玩家在原地略过时间（NPC 在跑 tick）期间，环境可能变化：有人进入、物品被拿、
    门被开关……前端在玩家回到界面/每次交互前调本接口，把 since_tick 之后的
    变化作为提示直接更新场景信息。

    Args:
        since_tick: 客户端上次已见的 tick。前端默认传 -1（首次前游标为 -1）。
        -1 表示「全量可见痕迹」（tick > -1），不与 current 换算——
        否则 since_tick=-1 被换算成 current 后，get_traces_since 查 tick>current，
        恰好把「本 tick（tick == current）的痕迹」排除掉，导致异步结算后即使
        current_tick 已前进、痕迹却始终读不出来（前端卡在"结果即将揭晓"）。
        修正为直接透传 since_tick 作为 since。
    Returns:
        {"current_tick", "traces": [{tick,actor,action_type,target,location,detail}],
         "env_changes": [{env_id,name,state}], "npc_positions": {npc: pos}}
    """
    sessions.ensure_session(session_id.strip() or "adhoc")
    gs = db.get_game_state_map(session_id)
    current = int(gs.get("current_tick", 0) or 0)
    since = since_tick if since_tick >= 0 else -1
    # 09-08 可见性修复：玩家只应知道【自己所在场景】里发生且他能感知的事（认知局限）。
    # 旧 get_traces_since 全场景透传 → 玩家在房间三却看到房间二的导演/齿轮。
    # 世界照常后台运转并记录，只是不把别处的事报给玩家。
    player_scene = db.get_player_scene(session_id)
    if player_scene:
        # 09-10 方向2 闭环：until_tick 作为上界（仅取 since_tick < tick <= until_tick），
        # 结束对话回场景只拉「发起对话那一格」的增量，掐断 A/B/C 多 tick 痕迹堆叠；<0 = 不限上界。
        traces = db.get_scene_traces_since(
            session_id, player_scene, "player", since, limit=limit,
            until_tick=(until_tick if until_tick >= 0 else None))
    else:
        traces = []

    env_changes = []
    for env_id, _kind, name, _desc, state_raw, _p in db.get_environment_cards(world_id):
        try:
            st = json.loads(state_raw) if isinstance(state_raw, str) else (state_raw or {})
        except ValueError:
            st = {}
        env_changes.append({"env_id": env_id, "name": name, "state": st})
    npc_positions = {npc: (db.get_npc_pos(session_id, npc) or "")
                     for npc in db.get_all_npc_ids(world_id)}
    # 09-10 修永久死锁（兜底通道）：把"待玩家裁决的对话邀请"一并带回。前端每次拉世界增量
    # 都能发现挂起态——即使它错过了 /chat 或 /session/move 响应里的挂起字段，也不会再
    # 静默冻结在一个看不见的邀请上。
    pending_offer = (gs.get(world_mod.PENDING_CONV_OFFER) or {}).get("invites") or []
    # 09-10：把【玩家权威位置】一并带回。文字输入触发的移动（"去房间二"）由后端执行器落地
    # （director._execute_player_intent_steps），但客户端只在【点地图】时才主动 switch_location
    # → 否则顶栏/地图/交谈入口仍停在旧房间，表现为"后端到了、界面原地不动"。
    # 前端拿它与本地 location_id 比对，不一致才切（幂等，不影响点地图路径）。
    return {"session_id": session_id, "current_tick": current, "since_tick": since,
            "player_scene": player_scene,
            "traces": [{"tick": t[0], "actor": t[1], "action_type": t[2],
                        "target": t[3], "location": t[4], "detail": t[5]} for t in traces],
            "env_changes": env_changes, "npc_positions": npc_positions,
            "pending_offer": pending_offer}


class WorldStepRequest(BaseModel):
    """POST /world/step 请求体：手动推进一个世界 tick（调试/手动模式）。"""
    session_id: str = ""
    world_id: str = "test"


@app.post("/world/step")
def world_step(req: WorldStepRequest):
    """手动推进一个 tick：跑完五阶段（计划→重规划→并行决策→仲裁→时钟）并返回摘要。

    与 /chat 的自动推进共用 world.advance_one（会话级互斥）；这里是同步版——
    调试面板"推进下一 tick"按钮的后端。"""
    session_id = req.session_id.strip() or "adhoc"
    sessions.ensure_session(session_id)
    summary = world_mod.advance_one(session_id, req.world_id)
    if summary is None:
        return {"advanced": False, "reason": "上一 tick 仍在推进或已达时间上限",
                "current_tick": db.get_game_state_map(session_id).get("current_tick", 0)}
    if summary.get("paused"):
        # 对话系统挂起：会话进行中（in_conversation）/ 有待玩家裁决的邀请（awaiting_conversation）
        # → 世界不推进，前端据此弹"XX 想跟你对话"询问或保持对话会话。
        return {"advanced": False,
                "in_conversation": bool(summary.get("in_conversation")),
                "awaiting_conversation": bool(summary.get("awaiting_conversation")),
                "offer": summary.get("pending_offer") or [],
                "current_tick": int(db.get_game_state_map(session_id).get("current_tick", 0) or 0)}
    rec = summary.get("recorder") or {}
    return {"advanced": True, "tick": summary["tick"], "decisions": summary.get("decisions", 0),
            "traces": summary.get("traces", 0),
            "replans": summary.get("replans", []),
            "npcs": {nid: {"action_tags": r.get("action_tags"),
                           "learned": r.get("learned"),
                           "decide_ms": r.get("decide_ms")}
                     for nid, r in (rec.get("npcs") or {}).items()}}


# ---------------------------------------------------------------------------
# 对话系统 v0.4 端点：邀请 → 同意/拒绝 → 多轮逐句交替 → 结束
# 会话期间 world.advance_one 被 active_conv / pending_conv_offer 挂起（不额外推进 tick）
# ---------------------------------------------------------------------------
@app.post("/conversation/start")
def conversation_start(req: ConversationStartRequest):
    """玩家【主动】发起与某 NPC 的对话（点「与X交谈」入口）。

    时序（设计决定 09-08）：
    - 先判定对方是否愿意对话（relationship.willing_to_dialogue，零 LLM）——判定在一切之前；
    - 愿意 → 结算本 tick（玩家本 tick 选择对话，耗 1 tick）→ 建会话（NPC=发起者/先开口）
      → AI 动态简介 → 开始最多 5 轮对话（期间世界挂起）；
    - 不愿意 → 返回婉拒，不推世界、不耗 tick，玩家留在原地可重选别行动。
    """
    sid = req.session_id.strip() or "adhoc"
    sessions.ensure_session(sid)
    npc_id = req.npc_id.strip()
    world_id = req.world_id or "test"
    if not npc_id:
        return {"accepted": False, "reason": "缺少对话对象"}

    # 0. 防重复：已在进行中的对话 / 有待玩家裁决的邀请 → 先处理更急的，不叠会话
    if db.get_conversation(sid):
        return {"accepted": False, "reason": "正在进行另一场对话"}
    if db.get_game_state_map(sid).get(world_mod.PENDING_CONV_OFFER):
        return {"accepted": False, "reason": "另有对话邀请待你处理"}

    # 1. 目标存活校验
    st = db.get_npc_status(sid, npc_id) or {}
    if st.get("dead"):
        return {"accepted": False, "reason": "对方已经不在人世了。"}

    # 2. 对方愿不愿意（零 LLM，判定在一切之前、不耗世界时间）
    from .relationship import willing_to_dialogue
    # 玩家主动搭话：被邀方无"推进既定计划"这样的 LLM 意图标签（玩家对话不产 plan_step），
    # 传默认 False 兼容；是否在忙（定位/交互）已由 busy=False 显式关掉。
    willing = willing_to_dialogue(npc_id, "player", sid, busy=False, stress=0.0, plan_step=False)
    if not willing:
        return {"accepted": False, "reason": "对方似乎不太想交谈。"}

    # 3. 愿意 → 玩家本 tick 发起对话（进程A）：世界结算【这一个 tick】的完整世界（进程B）。
    #    09-09 最终定稿（并行时序）：所有 NPC 决策/导演/记忆收口是耗时任务，放进【后台
    #    线程】跑（auto_advance_async），【不阻塞】玩家立刻进入对话；玩家之后每轮
    #    /conversation/turn（进程A）与它在后台的结算（进程B）并行，互不等待。
    #    B 的结果由 auto_advance 落 recorder，先攒着；对话结束（主动 end 或到 max_rounds）
    #    再统一放给前端——见对话结束端点与前端 pull 逻辑。
    scene = (req.scene or db.get_npc_pos(sid, npc_id)
             or str(db.get_game_state_map(sid).get("scene") or "") or "")
    gs = db.get_game_state_map(sid)
    tick = int(gs.get("current_tick", 0) or 0) + 1   # 本 tick = 玩家发起对话的这一刻
    first_line = ""
    # 04-启动顺序（09-09 拍板）：【先】start_conversation 写入 active_conv，让导演/后台结算
    # 能读到"玩家 + 该NPC 正在交谈"这一事实；【再】auto_advance_async 后台结算【这一个 tick】
    # （进程B）。若不先建会话，后台结算那一个 tick 时导演读不到"玩家在对话"，其他 NPC 可能
    # 在同 tick 里邀/打扰这对人（与"对话期间不被打扰"相悖）。
    # 上一场对话遗留的 conv_end 旁白缓存已由 db.start_conversation 内部统一清掉（防串场）。
    conv = db.start_conversation(sid, npc_id, "player", scene, tick,
                                 first_line=first_line, topic="玩家主动交谈",
                                 max_rounds=req.max_rounds)
    # 进程B：后台线程结算这一个 tick（advance_one 内部 current+1，与上方 tick 同口径），
    # 不挡玩家进对话（进程A）。会话锁在 advance_one 内非阻塞获取，重复调用不叠 tick。
    # 传入 llm_client=llm（本服务全局），使后台结算与在线决策共用同一 LLM（生产=DeepSeek，
    # 测试=FakeLLM）；否则后台决策阶段会用 None 触发真实 LLM 调用、且拿不到注入的 mock。
    world_mod.auto_advance_async(sid, world_id, llm_client=llm)
    if scene:
        db.add_world_trace(sid, tick, npc_id, "speak", "player", scene,
                           "与玩家开始对话。")

    # 5. 动态简介（第2点）：AI 现写"此刻玩家眼中的 TA"
    try:
        intro = conversation_mod.intro_dynamic_npc(npc_id, sid, world_id)
    except Exception:  # noqa: BLE001  简介失败不挡进入对话
        intro = ""
    # conv_tick = 玩家发起对话结算的那一格（进程B 结算 current+1，与上方 tick 同口径）。
    # 前端拿它做 since_tick 基准：结束对话回场景只拉【这一格】的世界增量（治 A/B/C 堆叠）。
    return {"accepted": True, "npc_id": npc_id, "scene": scene,
            "first_line": first_line, "intro": intro, "conversation": conv,
            "conv_tick": tick}


@app.post("/conversation/invite")
def conversation_invite(session_id: str = "", world_id: str = "test"):
    """查询当前是否有"待玩家裁决"的对话邀请（tick 结算暂停时产生）。

    /world/advance 已把 pending_offer 放回应里；此端点作为前端主动查询的备用入口。
    """
    sid = session_id.strip() or "adhoc"
    sessions.ensure_session(sid)
    offer = db.get_game_state_map(sid).get("pending_conv_offer")
    return {"offer": (offer.get("invites") or []) if offer else []}


@app.post("/conversation/accept")
def conversation_accept(req: ConversationAcceptRequest):
    """玩家裁决对话邀请（09-10 设计决定：裁决粒度是【整组】，不是一条）。

    语义：
      · accept=True  → 选中 req.npc_id 建 active_conversation 进对话；【其余邀请人一并婉拒】
        （各自递推下一优先级意图 + 写受阻记忆"对方选择了与XX交谈"）。
      · accept=False → 【全部】邀请人都不进行对话（各自递推 + 记忆"对方婉拒了交谈"）。

    裁决后 resume 被挂起的 tick（本 tick 结算其余行动，只算 1 tick）。
    """
    session_id = req.session_id.strip() or "adhoc"
    sessions.ensure_session(session_id)
    offer = db.get_game_state_map(session_id).get("pending_conv_offer")
    if not offer:
        return {"accepted": False, "reason": "无待裁决的对话邀请"}
    initiator = req.npc_id or ((offer.get("invites") or [{}])[0].get("initiator"))
    if not initiator:
        return {"accepted": False, "reason": "缺少对话发起者"}
    tick = int(offer.get("tick", 0) or 0)
    # 本 tick 的【全部】邀请人：同一 tick 里可能好几个 NPC 都想跟玩家说话（player_invites 本就是
    # 多条）。前端把它们渲染成"一条邀请 + N 个（名字+第一句话）选项 + 底部婉拒对话"，
    # 所以这里的裁决必须是整组语义——只处理 req.npc_id 一个，其余人就会继续"在导演里跟玩家说话"。
    invite_ids = [str(i.get("initiator", "")) for i in (offer.get("invites") or [])]
    invite_ids = [x for x in invite_ids if x] or [str(initiator)]

    if not req.accept:
        # 婉拒 = 【全部】邀请人都不进行对话；每个邀请人各自"递推下一优先级意图 + 写受阻记忆"。
        updated = conversation_mod.decline_invites(
            session_id, tick, offer.get("decisions", []), invite_ids,
            reason="对方婉拒了交谈")
        db.upsert_game_state(session_id, "pending_conv_offer", {**offer, "decisions": updated})
        # 09-10 修"婉拒之后我自己的行动没继续、屏幕毫无变化"（实测）：
        # resume 会把【被挂起的那一格】真正结算掉——包括玩家自己那一格的行动（意图池里的
        # "去房间二" 由导演分支的执行器落地）、本格导演叙述、结果痕迹。旧代码把返回值丢掉、
        # 只回一句 declined，于是这一格的产物【既没随响应回去、前端也没人再去取】。
        # 这里按与 /chat 完全相同的口径透传世界时序字段：
        #   · 正常结算 → {}（前端据此补拉一次 /world/updates 取回本格产物）；
        #   · 恢复后【又】产生新邀请 → world_paused + pending_offer（前端继续裁决，不丢链）。
        _summary = world_mod.resume_pending_tick(session_id, req.world_id)
        # declined_all：本组邀请【全部】被婉拒（前端据此渲染"你婉拒了这几位的邀谈"，
        # 而不是只写"你婉拒了与「张三」的对话"——那会让玩家以为还有人在等他裁决）。
        return {"accepted": False, "declined": True, "initiator": initiator,
                "declined_all": invite_ids,
                **world_mod.world_timing_fields(_summary)}

    # 同意：建立对话会话（发起者 = 第一个发言者），本 tick 该 NPC 转入对话，其余照常结算
    invites = offer.get("invites", [])
    inv = next((i for i in invites if str(i.get("initiator")) == str(initiator)),
               invites[0] if invites else {})
    scene = req.scene or inv.get("scene", "") or ""
    first_line = inv.get("speech") or inv.get("first_line") or ""
    conv = db.start_conversation(session_id, initiator, "player", scene,
                                 tick, first_line=first_line, topic=str(inv.get("intent", "")),
                                 max_rounds=req.max_rounds)
    if scene:
        db.add_world_trace(session_id, tick, initiator, "speak", "player", scene,
                           f"与玩家开始对话，说：「{first_line[:40]}」" if first_line
                           else "对玩家提出对话。")
    # 该 NPC 的 speak 决策已转对话：从本 tick 结算集合移除；玩家本 tick 进入对话（意图池清空）
    decisions = [d for d in offer.get("decisions", []) if str(d.get("agent", "")) != str(initiator)]
    # 09-10 设计决定：玩家选了其中一人 → 其余邀请人【自动婉拒】（各自递推下一优先级意图）。
    # 顺序要紧：必须【先】把选中者从集合里摘掉，再对其余者收场——否则会把刚选中的人一起拒掉。
    others = [x for x in invite_ids if x != str(initiator)]
    if others:
        # 记忆里说清是"被放了鸽子"，而不是"对方不想说话"：它知道玩家去和谁聊了，
        # 下次的意愿/态度判定才有人味（reason 会写进 NPC 的 event 记忆）。
        chosen = world_pack.npc_id_to_name(initiator, req.world_id) or initiator
        decisions = conversation_mod.decline_invites(
            session_id, tick, decisions, others, reason=f"对方选择了与{chosen}交谈")
    db.upsert_game_state(session_id, "pending_conv_offer",
                         {**offer, "decisions": decisions, "player_intents": []})
    world_mod.resume_pending_tick(session_id, req.world_id)
    # 动态介绍（第2点）：同意即"本 tick 选择对话"——先齿轮→这段 AI 现写的简介→开始对话。
    try:
        intro = conversation_mod.intro_dynamic_npc(initiator, session_id, req.world_id)
    except Exception:  # noqa: BLE001  简介失败不挡进入对话
        intro = ""
    # conv_tick = 玩家同意邀请、进入对话结算的那一格（前端作为 since_tick 基准）。
    # rejected_others = 本组里被"自动婉拒"的其他人（前端文案/排障用）。
    return {"accepted": True, "initiator": initiator, "first_line": first_line,
            "intro": intro, "conversation": conv, "conv_tick": tick,
            "rejected_others": others}


@app.post("/conversation/turn")
def conversation_turn(req: ConversationTurnRequest):
    """对话进行中：玩家回一句 → 流式生成 NPC 下一句（逐句交替，至多上限轮）。

    09-09 设计决定（方案A）：turn 是【进程A】——只做"玩家这句→NPC回这句"，【不驱动世界、不等世界】。
    （旧语义"active_conv 存在 → advance_one 不推进/世界挂起"已废弃——对话期间世界照常后台推演。）
    """
    session_id = req.session_id.strip() or "adhoc"
    sessions.ensure_session(session_id)
    conv = db.get_conversation(session_id)
    if not conv:
        return {"turned": False, "reason": "无进行中的对话"}
    # 玩家这句：走心智理解（process_dialogue）+ 写进会话
    conversation_mod.player_respond(conv, req.message, session_id, req.world_id)
    db.update_conversation(session_id, conv)

    def generate():
        if conversation_mod.is_conversation_over(conv):
            db.end_conversation(session_id)
            yield ""
            return
        next_speaker = conversation_mod.npc_next_speaker(conv, "player")
        try:
            for token in conversation_mod.npc_generate_stream(conv, next_speaker, session_id,
                                                              req.world_id, llm):
                yield token
        except Exception as e:  # noqa: BLE001
            logger.error("对话流式生成失败: %s", e)
            yield "\n\n[对方陷入了沉默。]"
        finally:
            db.update_conversation(session_id, conv)
            if conversation_mod.is_conversation_over(conv):
                db.end_conversation(session_id)

    return StreamingResponse(generate(), media_type="text/plain; charset=utf-8")


@app.post("/conversation/end")
def conversation_end(session_id: str = "", world_id: str = "test"):
    """主动结束对话：清空会话 → advance_one 恢复推进。

    09-10（删文学旁白）：不再预生成/返回 conv_end 转场旁白（改由导演 player_view 承担）。
    conv_end_text 保留用于兼容（恒为空），前端不再消费。
    """
    sid = session_id.strip() or "adhoc"
    sessions.ensure_session(sid)
    if not db.get_conversation(sid):
        return {"ended": False, "reason": "无进行中的对话"}
    db.end_conversation(sid)
    return {"ended": True, "conv_end_text": db.get_conv_end_text(sid)}


@app.get("/world/info")
def world_info(world_id: str = "test"):
    """模组元信息（开场白/名称/描述/说书人语气）——客户端开场白与主菜单从模组读。

    换模组 = 换 world_id：人物/环境/物品在 seed，语气/词典/词表/模板在
    server/worlds/<world_id>/，客户端开场白在这里。三处都是数据，引擎零改动。"""
    m = world_pack.manifest(world_id)
    name = db.get_world_name(world_id) or m.get("name") or world_id
    return {"world_id": world_id, "name": name,
            "description": m.get("description", ""),
            "entry": m.get("entry", ""),
            "initial_scene": m.get("initial_scene", ""),
            "storyteller_tone": (world_pack.load(world_id).get("storyteller") or {}).get("tone", "")}


@app.get("/world/npcs")
def world_npcs(world_id: str = "test"):
    """返回某世界全部 NPC 定义卡（id -> dict，含展示字段 name/status/opening/replies/option_rules）。

    NPC 模组化：一个 NPC=一个定义文件（server/worlds/<world_id>/npcs/<id>.json），
    本端点直接读文件（文件即权威），不落 DB；AI 侧由 npc_loader 注册进 character_card 等表。
    首次访问若 DB 尚无该世界 NPC，自动触发一次 load，保证"放文件即生效"。"""
    npcs = npc_loader.read_world_npcs(world_id)
    if not npcs:
        npc_loader.load_world(world_id)   # 首次自动注册（幂等）
        npcs = npc_loader.read_world_npcs(world_id)
    return {"world_id": world_id, "npcs": npcs}


@app.post("/npcs/reload")
def npcs_reload(world_id: str = ""):
    """手动触发 NPC 模组注册（幂等）：world_id 空=全部世界，否则只刷该世界。"""
    if world_id:
        ok, errs = npc_loader.load_world(world_id)
    else:
        ok, errs, worlds = npc_loader.load_all()
    return {"ok": ok, "errors": errs, "world_id": world_id or "all"}


@app.on_event("startup")
def _startup_register_npcs():
    """启动时自动注册全部世界的 NPC 定义文件到 DB（幂等；DB 未就绪则静默跳过，
    可由 /npcs/reload 或 /world/npcs 首次访问触发）。"""
    try:
        npc_loader.load_all()
    except Exception as exc:  # noqa: BLE001
        logger.warning("启动时 NPC 模组注册失败（可稍后 /npcs/reload 触发）：%s", exc)


@app.get("/scene/inspect")
def scene_inspect(session_id: str, scene: str, world_id: str = "test", view: str = "arriving",
                  partner: str = ""):
    """场景感知：事实快照 + 在场 NPC（画面叙述交给导演 player_view，经 /world/updates 增量回前端）。

    - snapshot：build_perception_snapshot 事实（【你所在】【这里的样子】【这里有】【你注意到】）
      ——程序算，不靠 LLM；已注入环境卡厚描述（防幻觉"窗"）与认识分层（陌生用性别指代）；
    - narrative：恒为空（09-10 删文学旁白，不再现调 narrate_scene、不读 conv_end 缓存）；
    - npcs：此刻在该场景的存活 NPC（动态，不读本地静态 npc_ids）。
    view（历史参数，已废弃）：arriving/lingering/conv_end 视角此前交给 narrate_scene，现不再使用。
    """
    sid = session_id.strip() or "adhoc"
    sessions.ensure_session(sid)
    # 该场景当前"在场"的 NPC（存活且此刻位置==该场景）——客户端场景人物入口改用动态数据（问题4）
    present = []
    for nid in db.get_all_npc_ids(world_id):
        if db.get_npc_pos(sid, nid) != scene:
            continue
        if db.get_npc_status(sid, nid).get("dead"):
            continue
        card = db.get_character_card(nid)
        present.append({"id": nid, "name": card[1] if card else nid})
    # 09-09 预生成缓存优先：view=conv_end 时，"结束对话回场景"的专属转场旁白在【发起对话那一格】
    # 结算完成时已由后台预生成并落库（db.get_conv_end_text）。这里【优先读缓存】返回——秒出、不重复
    # 现调 LLM，且与前端 _show_scene_in_location(conv_end) 共用同一份（避免两条重复旁白）；
    # 缓存缺失（预生成失败/未完成）才兜底现调 narrate_scene。其余 view 始终现调。
    # 09-10 删文学旁白：场景叙事统一由【导演 player_view】承担（经 /world/updates 增量回前端）。
    # /scene/inspect 只返回事实快照 + 在场 NPC，不再现调 narrate_scene、不再读 conv_end 缓存。
    # 唯一例外：view=="opening"（开局）返回环境卡预设"初始文字"（_scene_description，秒回、非 LLM），
    # 作玩家开局固定开场白；其余 view 的 narrative 恒为空。
    narr = spatial_mod._scene_description(scene, world_id) if view == "opening" else ""
    return {"session_id": sid, "scene": scene, "world_id": world_id,
            "snapshot": build_perception_snapshot(scene, session_id=sid, world_id=world_id),
            "narrative": narr,
            "npcs": present}


@app.get("/debug/overview")
def debug_overview(session_id: str, world_id: str = "test", tick: int = None):
    """上帝视角聚合视图：指定 tick（缺省=当前）的 recorder 全量 + 世界即时状态。

    每 NPC：得知了什么 / 给 AI 的提示词 / 行动多选标签 / AI 输出 / 环境影响 /
    LLM 各段耗时（decide_ms / reaction_ms）。供 Godot 调试面板逐 tick 展示。"""
    sessions.ensure_session(session_id.strip() or "adhoc")
    return recorder.overview(session_id.strip() or "adhoc", world_id, tick)


@app.get("/session/inventory")
def session_inventory(session_id: str, world_id: str = "test"):
    """背包数据源：查玩家当前身上持有的物品。

    Godot 端背包要"实际反映玩家身上的物品"，而不是本地静态数组。此接口返回该世界
    所有 mode=held 且 holder='player' 的环境实体（客户端拿刀后 knife.holder=player）。

    说明：environment_card 目前是全局单例（D-01 已知限制，多活跃会话共享环境状态），
    持有物按 world 查询；session_id 仅作语义标识（单机单会话下可接受）。

    Returns:
        {"session_id","world_id","items":[{env_id,name,type,holder}],"count","names"}。
    """
    sessions.ensure_session(session_id.strip() or "adhoc")
    items = get_held_items(world_id, holder="player")
    return {"session_id": session_id, "world_id": world_id, "items": items,
            "count": len(items), "names": [it["name"] for it in items]}


@app.get("/debug/trace")
def debug_trace_endpoint(limit: int = 50):
    """实时查看最近 N 条对话/LLM 链路调试记录（发给 AI 的话 / AI 返回 / 解析结果 / 失败原因）。

    排查用：前端失败时只给统一兜底话（"此人沉默地打量着你"），看不到是哪一步失败。——
    本端点还原每次 /chat 的 sent/raw/parsed/error/ms。GET /debug/trace?limit=50。
    """
    limit = max(1, min(int(limit), 200))
    traces = debug_trace.recent(limit)
    return {"count": len(traces), "traces": traces}


@app.post("/chat")
def chat(req: ChatRequest):
    """聊天入口：按 NPC 组装人设上下文，调用大模型，返回回复。

    Args:
        req: 请求体，Pydantic 已校验其中包含 message 与 npc_id 字段。

    Returns:
        模型回复文本，包装为 {"reply": ...} 返回。

    Raises:
        HTTPException: 大模型调用失败（网络/超时/Key 错误）时，
            返回 502 给前端，而非让服务裸奔 500。
    """
    # M1.1：会话解析——显式 session_id，或旧前端兜底 adhoc（日志告警，可观测不静默）
    session_id = req.session_id.strip() or "adhoc"
    if not req.session_id.strip():
        logger.warning("/chat 未携带 session_id，落 adhoc 会话（旧前端兼容路径）；请接入 POST /session/start")
    sessions.ensure_session(session_id)

    # 环境直接行动（npc_id=""）：观察/旁白立即响应；改变环境的行动走【登记制】——
    # 意图进池（不立即执行），随本 tick 与 NPC 行动按场景聚合裁决（场景导演）。
    if not req.npc_id:
        # 09-10 修永久死锁：世界正等人裁决对话邀请时，本句【不解析、不登记意图、不推 tick】
        # —— 旧行为是照常登记 + advance_one（一撞挂起态就空返回），意图留在池里永远没人消费，
        # 玩家也看不到任何反馈，只觉得"卡住"。这里直接把挂起态与待裁决邀请交回前端弹窗。
        _paused = world_mod.pending_offer_fields(session_id)
        if _paused:
            db.log_dialogue(session_id, "", "player", req.message)
            db.log_dialogue(session_id, "", "npc", _paused["notice"])
            return {"reply": _paused["notice"], "deferred": False, **_paused}
        out = run_environment(session_id, req.message, req.world_id,
                              scene_override=req.scene, defer_mutating=True)
        db.log_dialogue(session_id, "", "player", req.message)
        reply = out["reply"]
        deferred = bool(out.get("deferred"))
        if deferred:
            # 登记制：回复=确认（去破墙）。结算结果（导演 narrative + 各人后果）同步完成后
            # 经 /world/updates 的 traces 增量自然回来，前端据此渲染"命运的齿轮"两阶段。
            reply = ("你做出了行动——命运的齿轮开始转动，")
            debug_trace.record("chat_env_deferred", session_id=session_id,
                               user_text=req.message, parsed=out.get("intent"))
        db.log_dialogue(session_id, "", "npc", reply)
        debug_trace.record("chat_env", session_id=session_id, user_text=req.message, raw=reply)
        # 世界时序（D 裁决·同步回归 09-08 设计决定）：交互即流逝——登记的玩家意图随本
        # tick 与 NPC 行动一起结算（含场景导演）。改回【同步 advance_one】：阻塞至本 tick
        # 全部 NPC 行动+反应结算完成才返回——前端全程显示"命运的齿轮"，等所有角色决策
        # 出来再展示结果（直觉、不重复触发）。前端超时已放宽到 10min 兜底。
        # （此前用 auto_advance_async 后台线程 + 前端轮询，导致"重复触发/割裂"，该方案已否决。）
        # 09-10：必须接住 advance_one 的返回值——本格刚产生"邀请玩家对话"（paused）或没抢到
        # 会话锁（None）时，把挂起原因透传前端；否则前端只看到一句"命运的齿轮开始转动"，
        # 就永久卡在那里（实测：tick3 挂起后世界冻结在 tick2，前端毫无提醒）。
        _timing = world_mod.world_timing_fields(world_mod.advance_one(session_id, req.world_id))
        return {"reply": reply, "deferred": deferred, **_timing}

    # C（死亡人口卫 09-08）：对话对象已死 → 不再调模型演戏，直接告知无法交谈。
    # 纵深防御：即使前端入口没及时刷掉死人按钮，也绝不会"复活"（死人开口）。
    # 不推进 tick（没有真实对话）；入口清理由前端 _on_world_updates 的 entries_only 刷新负责。
    if db.get_npc_status(session_id, req.npc_id).get("dead"):
        reply = "对方已经死了，无法交谈。"
        db.log_dialogue(session_id, req.npc_id, "player", req.message)
        db.log_dialogue(session_id, req.npc_id, "npc", reply)
        debug_trace.record("chat_dead_guard", session_id=session_id,
                           npc_id=req.npc_id, user_text=req.message)
        return {"reply": reply}

    # B（设计裁决 09-08）：对话中也可能是"行动"——一边说话一边做动作（如"用刀偷袭他"）。
    # 用零 LLM 的规则层先判一次：若判定为 mutating 的空间/攻击行动，把这次行动登记进意图池，
    # 随本 tick 与 NPC 行动由场景导演统一裁决（玩家意图不再只当台词）。仍保留 NPC 对话——
    # NPC 会同时看到玩家说的话（台词）与做的动作，反应更贴合"边说话边动手"的现实。
    rule_intent = None
    try:
        rule_intent = intent_mod.classify_rule_only(req.message, scene_id=req.scene,
                                                    world_id=req.world_id)
    except Exception:  # noqa: BLE001  规则判定失败不阻塞对话
        rule_intent = None
    if rule_intent is not None and rule_intent.side_effect == "mutating":
        scene = req.scene.strip() or db.get_player_scene(session_id) or ""
        from .db import append_player_intent
        append_player_intent(session_id, {
            "intent": rule_intent.to_dict(), "text": req.message, "scene": scene,
        })
        p_tick = int(db.get_game_state_map(session_id).get("current_tick", 0) or 0)
        db.add_world_trace(session_id, p_tick, "player",
                           "interact" if rule_intent.domain == "spatial" else rule_intent.domain,
                           rule_intent.target.get("id", ""), scene,
                           f"{rule_intent.verb or '行动'}：「{req.message}」")

    # P4-A：不再是裸调，而是先让本层知道"这是哪个 NPC"，再拼人设
    # 记忆/关系召回都限定在当前会话（先验 seed + 本轮）；世界知识按 req.world_id 过滤
    # 心智路径（v0.3）：先跑管线（含 LLM① 理解），中间态传给拼接层 → L1~L7
    mental_ctx = _run_mind_pipeline(req.npc_id, session_id, req.world_id, req.message)
    messages = build_messages(req.npc_id, req.message, session_id, req.world_id,
                              mental_ctx=mental_ctx)

    # 对话日志（M1.1 补上此前一直缺失的写入）：玩家输入先落库——
    # 即使下一步 LLM 失败，"玩家问了什么"也是可观测数据
    db.log_dialogue(session_id, req.npc_id, "player", req.message)

    # A（设计裁决 09-08）：玩家说话也落世界痕迹——世界"记得"玩家说过的每句话，
    # 供其他 NPC/场景复盘（此前玩家只有物理动作进世界痕迹，说话完全不记）。
    # 记在当前 tick（advance_one 推进前），与 _exec_move 写玩家移动用同一 tick 口径。
    p_tick = int(db.get_game_state_map(session_id).get("current_tick", 0) or 0)
    db.add_world_trace(session_id, p_tick, "player", "speak", req.npc_id,
                       db.get_player_scene(session_id) or "", f"说：「{req.message}」")

    try:
        reply = llm.chat(messages)
    except Exception as e:
        # 记录原始异常，方便排查；但只把友好提示返回给前端（不泄露内部细节）
        logger.error("大模型调用失败: %s", e)
        raise HTTPException(status_code=502, detail="大模型调用失败，请稍后重试")

    db.log_dialogue(session_id, req.npc_id, "npc", reply)
    debug_trace.record("chat", session_id=session_id, npc_id=req.npc_id,
                       user_text=req.message, raw=reply)

    # P4-B 写回侧：聊完把这轮要点写进 NPC 记忆（落在当前会话，轮回不串台）
    remember_conversation(req.npc_id, req.message, reply, session_id)
    # 秘密演化（世界时序 v2）：回复是否说漏了守口如瓶的秘密 → 口径升级 + 记忆留痕
    mind_engine.on_reply(req.npc_id, session_id, reply)
    # P4-C 写回侧：根据这轮态度调整 NPC 对玩家的关系（本会话的关系行）
    adjust_relationship(req.npc_id, req.message, session_id)

    # 在线世界时钟（OL-6，D 裁决·同步回归 09-08 设计决定）：玩家交互后同步推进一个 tick
    # ——"挂机不推进、交互才流逝"。回复已生成，同步阻塞至本 tick 全部 NPC 行动+反应结算
    # 完成才返回（前端全程显示"命运的齿轮"，直觉、不重复触发）。前端超时已放宽到 10min。
    # （此前用 auto_advance_async 后台线程 + 前端轮询，导致"重复触发/割裂"，该方案已否决。）
    # 09-10：接住 advance_one 的返回值——挂起（本格产生对话邀请）/没抢到锁时把原因透传前端，
    # 别让世界静默冻结。
    _timing = world_mod.world_timing_fields(world_mod.advance_one(session_id, req.world_id))

    return {"reply": reply, **_timing}


@app.post("/chat/stream")
def chat_stream(req: ChatRequest):
    """流式聊天入口：逐字返回大模型回复。

    Args:
        req: 请求体，Pydantic 已校验其中必须包含 message 字段。

    Returns:
        StreamingResponse：一个逐段输出文本的流，前端边收边显示。
    """
    # M1.1：流式与非流式共用同一套会话/日志/写回语义
    session_id = req.session_id.strip() or "adhoc"
    if not req.session_id.strip():
        logger.warning("/chat/stream 未携带 session_id，落 adhoc 会话（旧前端兼容路径）")
    sessions.ensure_session(session_id)

    if not req.npc_id:
        # 环境直接行动：非流式统一处理（观察短路径 / 行动旁白），一次性返回。
        out = run_environment(session_id, req.message, req.world_id, scene_override=req.scene)
        db.log_dialogue(session_id, "", "player", req.message)
        db.log_dialogue(session_id, "", "npc", out["reply"])
        # 全同步世界时序（D 裁决）：环境行动也推进一个 tick（并行跑所有 NPC），阻塞至结算。
        world_mod.advance_one(session_id, req.world_id)
        return {"reply": out["reply"]}

    messages = build_messages(req.npc_id, req.message, session_id, req.world_id,
                              mental_ctx=_run_mind_pipeline(req.npc_id, session_id,
                                                            req.world_id, req.message))
    db.log_dialogue(session_id, req.npc_id, "player", req.message)

    # A（同 /chat）：玩家说话也落世界痕迹（流式路径保持一致语义）
    p_tick = int(db.get_game_state_map(session_id).get("current_tick", 0) or 0)
    db.add_world_trace(session_id, p_tick, "player", "speak", req.npc_id,
                       db.get_player_scene(session_id) or "", f"说：「{req.message}」")

    def generate():
        """生成器：逐段输出回复；结束后完整落日志/写回（finally 保证失败也留痕）。"""
        full = []
        try:
            for token in llm.chat_stream(messages):
                full.append(token)
                yield token
        except Exception as e:
            # 流式中途出错：记录原始错误，并向已收到的流里补一段友好提示
            logger.error("大模型流式调用失败: %s", e)
            fallback = "\n\n[大模型调用失败，请稍后重试]"
            full.append(fallback)
            yield fallback
        finally:
            # 流结束（无论成败）：完整回复落对话日志 + 记忆/关系写回，
            # 与非流式 /chat 行为对齐；空回复（异常早退）由写回侧的长度过滤自然跳过
            reply = "".join(full)
            if reply:
                db.log_dialogue(session_id, req.npc_id, "npc", reply)
                remember_conversation(req.npc_id, req.message, reply, session_id)
                mind_engine.on_reply(req.npc_id, session_id, reply)
                adjust_relationship(req.npc_id, req.message, session_id)
                # 全同步世界时序（D 裁决）：流结束后同步推进一个 tick（并行跑所有 NPC），
                # 阻塞至结算完成才结束 HTTP 流——客户端收到"流结束"即等于"本 tick 已结算"。
                world_mod.advance_one(session_id, req.world_id)

    # text/plain; charset=utf-8：明确编码，保证中文不乱码
    return StreamingResponse(generate(), media_type="text/plain; charset=utf-8")
