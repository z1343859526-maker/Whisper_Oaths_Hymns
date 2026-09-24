"""NPC 决策：组装"有限视角快照" -> 调 LLM -> 输出 decision JSON。

有限视角（§8.6 ①感知）：NPC 只能看到自己所在/可见的环境卡 + 最近的世界痕迹
+ 自己召回的记忆 + 自己的关系，**看不到**别人的秘密/意图 —— 这是与 fate.py
（全知仲裁）的本质区别。

decision JSON 约定见 P4 笔记 §8.3：action.type 八类枚举
（move / use_item / give_item / speak / observe / wait / interact / trigger_event）。
「规则定方向，LLM 定表达」：type/target 是结构化字段（引擎用），
detail/speech 是自然语言（旁白/玩家看）。
"""
import inspect
import json
import time

from . import db
from . import debug_trace
from . import recorder
from . import world_pack
from . import scheduler
from . import mind_engine
from . import mental
from .config import LLM_JSON_MODE
from .context_builder import compose_mental_system
from .spatial import build_perception_snapshot   # 知识边界：NPC 只感知它当下所在场景（而非全世界环境卡）
from .llm import DeepSeekClient
from .memory import recall_memories

_client = DeepSeekClient()  # 模块级单例：复用连接，避免每 tick 每 NPC 都新建客户端

ACTION_TYPES = (
    "move", "use_item", "give_item", "speak",
    "observe", "wait", "interact", "trigger_event",
    "converse",
)

# 解析失败时的兜底意图：唯一来源，避免解析器与兜底构造两处各写一遍字面量而漂移
FALLBACK_INTENT = "不知所措，原地等待"

_DEFAULT_WORLD = "golden"
_DEFAULT_WORLD_NAME = "黄金乡谋杀案"

# {客户端类型: 是否支持 json_mode/tag 关键字参数}——按类型只探测一次，避免每 tick 反射
_CHAT_KW_SUPPORT: dict = {}


def _chat(messages: list, json_mode: bool = False, tag: str = "") -> str:
    """调用决策 LLM，并按客户端能力决定是否带上 json_mode / tag。

    为什么要探测签名：chat 新增了 json_mode（JSON 输出模式）与 tag（trace 观测标签）
    两个关键字参数，但项目里多个验证脚本用 `class FakeLLM: def chat(self, messages)`
    替换了 agent._client。直接传新参会在 stub 上抛 TypeError，被 decide 的 except 捕获后
    误报成"AI 未响应（TypeError）"——把"模型没回"和"我们参数不对"两种故障混为一谈。
    """
    key = type(_client)
    supported = _CHAT_KW_SUPPORT.get(key)
    if supported is None:
        try:
            params = inspect.signature(_client.chat).parameters
            supported = ("json_mode" in params) or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
        except (TypeError, ValueError):  # 取不到签名（装饰器/C 实现）→ 保守：不放新参
            supported = False
        _CHAT_KW_SUPPORT[key] = supported
    if supported:
        return _client.chat(messages, json_mode=json_mode, tag=tag)
    return _client.chat(messages)


# 重试提示：不重复整个角色提示（messages 原样保留），只补一句"上次没写完"
_RETRY_HINT = ("你上一次的输出没有写完，JSON 未闭合、无法解析。请**重新**只输出一个完整"
               "闭合的 JSON 对象：不要复述上文、不要任何解释文字、不要 markdown 代码块。")


def _retry_once(messages: list, npc_id: str, tick: int):
    """决策 JSON 解析失败后重试一次；重试也失败（或调用异常）返回 None。

    为什么值得多花一次调用：坏输出是"**信息丢失**"（模型提前 end_turn），不是"格式可修"，
    解析技巧救不回来；而重试只是把同一次请求再发一遍，代价仅这一 tick 多一次 LLM 调用。
    """
    try:
        return _chat(messages + [{"role": "user", "content": _RETRY_HINT}],
                     json_mode=LLM_JSON_MODE, tag=f"{npc_id}@t{tick}#retry")
    except Exception as e:  # noqa: BLE001  重试再失败就认了，交给上层兜底 wait
        debug_trace.record("plan_retry_fail", npc_id=f"{npc_id}@t{tick}",
                           error="%s: %s" % (type(e).__name__, e))
        return None


def _decide_raw_with_retry(messages: list, npc_id: str, tick: int) -> str:
    """调用模型拿决策原始输出；**解析失败则重试一次**，返回最终应采用的那份 raw。

    为什么重试（09-10 实测根因）：本模型会**自己写到一半就 end_turn**（finish_reason=stop，
    不是被 max_tokens 截断）→ JSON 未闭合。这是"信息真丢了"，任何解析技巧都救不回来
    （括号平衡抠取也抠不出完整对象）。坏输出概率约 3%~10%，重试代价只是这一 tick 多一次
    调用，远比"整轮退化成空 wait 且没有 AI 思考"划算。
    """
    raw = _chat(messages, json_mode=LLM_JSON_MODE, tag=f"{npc_id}@t{tick}")
    if not _is_fallback_plan(_parse_plan(raw, npc_id=f"{npc_id}@t{tick}")):
        return raw
    retry_raw = _retry_once(messages, npc_id, tick)
    if retry_raw is not None and not _is_fallback_plan(
            _parse_plan(retry_raw, npc_id=f"{npc_id}@t{tick}#retry")):
        debug_trace.record("plan_retry_ok", npc_id=f"{npc_id}@t{tick}", raw=retry_raw[:200],
                           error="首次输出 JSON 未闭合，重试一次后解析成功")
        return retry_raw
    return raw   # 重试也没救 → 沿用首次输出（_parse_plan 已留证）


def _system_prompt(world_id: str = _DEFAULT_WORLD) -> str:
    """按世界组装「角色决策器」system 提示。

    为什么把世界名做成参数而不是写死：不同世界的承载引擎不同（黄金乡/测试世界），
    NPC 决策时若被套进错误的世界叙事，会"自称黄金乡人"而 OOC。用 world_id 隔离，
    与 context_builder._knowledge_block 的 RAG 世界隔离保持同一维度。
    缺省 golden 供 simulate 等旧调用向后兼容（未显式传世界时不因语义变化而崩）。
    """
    world_name = db.get_world_name(world_id) or _DEFAULT_WORLD_NAME
    return (
        f"你是《{world_name}》涌现叙事引擎里的「角色决策器」。你要扮演某个 NPC，"
        "根据它当下的有限视角，决定它在这 10 分钟（1 tick）里想做的一件事。\n\n"
        "一次给出【按优先级从高到低排列的多个候选行动】，引擎会依次尝试：第 0 项是这回合"
        "最想做的；若它因故被拒绝（例如对方不想跟你对话），会自动改用下一个。\n\n"
        "只能输出一个 JSON 对象，不要输出任何其它文字、不要加 markdown 代码块。格式：\n"
        '{"reasoning":"先简短写下你此刻的内部思考：想做什么、为何、若被拒会退到哪一步（写给系统看，'
        '不要写成对玩家说的话）",'
        '"plan":[{"intent":"内部动机","honest":true,"plan_step":true,'
        '"action":{"type":"动作类型","target":"对象id",'
        '"location":"发生地","detail":"补充细节"},"speech":null},{"intent":..,"honest":..,"action":{..},"speech":..}]}\n\n'
        "plan 至少 1 项、最多 3 项；第 0 项是最想做的，越靠后越是退而求其次（可把 wait 放最后作兜底）。\n"
        + world_pack.prompt(world_id, "action_types_guide")
    )


def _held_of(npc_id: str, world_id: str) -> list:
    """查某 NPC 身上带着什么（随身背包）：空间骨架里 mode=held && holder=npc_id 的实体。

    用户第3点：NPC 自主行动判定必须知道「自己身上有什么」——不然怎么判断"能不能用这件东西/
    该不该把东西给人"。与玩家背包同源 get_held_items（走 where 动态归属），保证两端一致。
    无空间骨架的世界安静返回空列表（不阻塞决策）。
    """
    try:
        from . import spatial as _sp
        return _sp.get_held_items(world_id, npc_id)
    except Exception:  # noqa: BLE001
        return []


def build_snapshot(npc_id: str, tick: int, session_id: str, world_id: str = _DEFAULT_WORLD) -> dict:
    """组装某 NPC 在 tick 时刻的「有限视角快照」（结构化 dict）。

    有限视角的「有限」体现在：只召回自己的记忆 + 自己与玩家的关系 + 自己的排班；
    环境与痕迹先全量读进来（MVP 不按 perception 细筛，M5 联调时收紧）。
    world_id：世界隔离维度——环境卡按世界过滤（004 迁移后），
    测试世界的 NPC 绝不会看到黄金乡的地窖/书房（换世界观环境不串台）。
    """
    card = db.get_character_card(npc_id)
    memories = [m["text"] for m in recall_memories(npc_id, min_importance=4, session_id=session_id)][:8]
    rel = db.get_relationship(npc_id, "player", session_id)
    plan = scheduler.scheduled_actions(npc_id, tick)
    envs = db.get_environment_cards(world_id)
    traces = db.get_recent_traces(session_id, tick, limit=20)
    return {
        "card": card,
        "memories": memories,
        "relationship": rel,
        "plan": plan,
        "environment": envs,
        "traces": traces,
        "session_id": session_id,
        # 知识边界（问题2/3）：NPC 只感知自己当下所在场景；玩家是否在场作为"环境一部分"注入
        "npc_scene": db.get_npc_pos(session_id, npc_id) or "",
        "player_scene": db.get_player_scene(session_id) or "",
        # 背包/随身物品（第3点）：NPC 决策要知道自己身上有什么
        "held": _held_of(npc_id, world_id),
        "world_id": world_id,
    }


def _merge_timeline_and_memory(timeline: list, memories: list, max_lines: int = 8) -> list:
    """痕迹 + 重要记忆去重合并（P0-c 方案C）：两者可能记同一件事，去重后排序再截断。

    简化的文本级去重（不依赖结构化 id）：痕迹行和记忆行若互相包含（重合度高）视为同一条，
    记忆行优先级更高（它是"记得住的长期认知"）。结果保序：先重要记忆、再痕迹补足。
    """
    merged = []
    seen = []
    for m in memories or []:
        mtxt = str(m).strip()
        if not mtxt:
            continue
        # 与已并入的任一条重合则跳过（去重）
        if any(mtxt in s or s in mtxt for s in seen):
            continue
        merged.append(mtxt)
        seen.append(mtxt)
    for t in timeline or []:
        ttxt = str(t).strip()
        if not ttxt:
            continue
        if any(ttxt in s or s in ttxt for s in seen):
            continue
        merged.append(ttxt)
        seen.append(ttxt)
    return merged[:max_lines]


def _snapshot_to_prompt(npc_id: str, tick: int, snap: dict, mental_ctx: dict or None = None) -> str:
    """把有限视角快照转成给 LLM 的 user 文本。

    mental_ctx 非空（该角色配了 mental_model）→ 人设/关系/计划段由心智拼接
    （compose_mental_system，状态词口径）承担；本函数只补时间/环境/痕迹等世界事实。
    旧路径保持原样（trust=75 裸数值注入的问题只存在于旧路径，心智路径不产生数字）。
    """
    clock = scheduler.tick_to_clock(tick)
    lines = [f"【当前时间】{clock}（tick {tick}）"]

    if mental_ctx is not None:
        card = snap.get("card")
        lines.append(compose_mental_system(mental_ctx, card,
                                           world_id=snap.get("world_id", "golden"),
                                           include_knowledge=False))
        # 决策引导（用户裁决"方法总比困难多"+ 思维链差异化）：
        # 带入身份想办法，不限于清单动作；链上有"未来推演"环节的角色先推演后果再选
        lines.append(world_pack.prompt(snap.get("world_id", "golden"), "decision_guide"))
        mm = mental_ctx.get("mm") or {}
        chain = mm.get("thinking_chain") or []
        if "未来推演" in chain:
            lines.append(world_pack.prompt(snap.get("world_id", "golden"), "future_inference"))
        if "情绪调节" in chain:
            lines.append(world_pack.prompt(snap.get("world_id", "golden"), "emotion_regulation"))
    else:
        card = snap["card"]
        if card:
            name, title = card[1], card[2]
            personality, motivation = card[3], card[5]
            lines.append(f"【你扮演】{name}" + (f"（{title}）" if title else ""))
            if personality:
                lines.append(f"性格：{personality}")
            if motivation:
                lines.append(f"动机：{motivation}")
        else:
            lines.append(f"【你扮演】{npc_id}")

        if snap["plan"]:
            lines.append("【你的既定计划/排班】")
            for p in snap["plan"]:
                lines.append(f"- {p['time']} 在 {p['location']}：{p['action']}（意图：{p['intent']}）")

        if snap["memories"]:
            lines.append("【你记得的事】")
            for m in snap["memories"]:
                lines.append(f"- {m}")

        if snap["relationship"]:
            trust, fear, affection, rel_type, notes = snap["relationship"]
            lines.append(f"【你对玩家的关系】trust={trust}, affection={affection}, fear={fear}（{rel_type}）")
            if notes:
                lines.append(f"（备注：{notes}）")

    # C（用户裁决 09-08）：只注入「自己作为 actor 的世界痕迹」——它自己做过/说过的事，
    # 绝不给别的 NPC 的痕迹（他人没经历的事不是它的知识）。不做全量痕迹灌注。
    # P0-c（方案C·时间线收敛）：不再机械灌注前 8 条，改用"高信息痕迹收敛 + 记忆表兜底"：
    #   · own_traces 先经 converge_own_timeline 剔噪/同类去重/按信息权重截断（控 token）；
    #   · 兜底叠加 recall_memories(importance>=6) 的"重要长期记忆"（如 t5 藏钥匙、t11 杀人），
    #     痕迹与记忆可能记同一件事 → 需去重合并（见 _merge_timeline_and_memory）。
    own_traces = [t for t in (snap.get("traces") or []) if str(t[1]) == str(npc_id)]
    timeline_lines = mental.converge_own_timeline(own_traces, max_lines=6)
    try:
        important_mems = [m["text"] for m in
                          recall_memories(npc_id, min_importance=6, session_id=snap.get("session_id", ""))]
    except Exception:  # noqa: BLE001  记忆召回失败降级（只用痕迹段）
        important_mems = []
    merged = _merge_timeline_and_memory(timeline_lines, important_mems, max_lines=8)
    if merged:
        lines.append("【你做过/说过的事（近期记录）】")
        for m in merged:
            lines.append(f"- {m}")

    # 知识边界（问题2）：NPC 只感知"它当下所在场景"——不再灌注全世界环境卡、不再给全部痕迹。
    # 感知快照按当前动态归属裁剪（被拿走的物品不再显示原位），本场景最近痕迹也并入"你注意到"。
    npc_scene = snap.get("npc_scene") or ""
    if npc_scene:
        lines.append(build_perception_snapshot(npc_scene, session_id=snap.get("session_id"),
                                               world_id=snap.get("world_id", "golden"),
                                               observer=npc_id))
        # 空间认知（用户裁决）：①现在能去哪（连通+门开——当前可行动范围）；
        # ②去过的房间（足迹=它的世界认知——开局不认识没去过的房间，去过了才进记忆）
        try:
            from . import spatial
            reachable = [c for c in spatial.connected_scenes(npc_scene, snap.get("world_id", "golden"))
                         if spatial.can_reach(npc_scene, c, snap.get("world_id", "golden"))]
            if reachable:
                lines.append("【现在能去的房间】" + "、".join(reachable))
            from . import mind_engine
            visited = mind_engine.get_visited(snap.get("session_id", ""), npc_id)
            if visited:
                lines.append("【你去过的房间】" + "、".join(visited)
                             + "（它们的详细样子在你的记忆里）")
        except Exception:  # noqa: BLE001  无空间骨架的世界安静跳过
            pass
    # 玩家是环境的一部分（问题3）：告诉 NPC 玩家在不在场（同场景=能看见；否则不知道其在哪）。
    player_scene = snap.get("player_scene") or ""
    if npc_scene and player_scene and player_scene == npc_scene:
        lines.append("【在场的角色】玩家就在你身边（与你同在%s）。" % npc_scene)
    elif npc_scene:
        lines.append("【关于玩家】玩家不在这里；你此刻不知道玩家在哪。")

    # 背包/随身物品（第3点）：让 NPC 知道自己身上带着什么——心智路径与旧路径都注入。
    # 有东西在手（刀/信/钥匙）会直接影响"能不能用/该不该给人"的决策，AI 必须知道。
    held = snap.get("held") or []
    if held:
        held_txt = []
        for h in held:
            h_name = (h.get("name") if isinstance(h, dict) else str(h)) or "未知物件"
            held_txt.append("「%s」" % h_name)
        lines.append("【你身上带着的东西】" + "、".join(held_txt))

    lines.append("\n请决定你在这 10 分钟里做什么，输出 JSON。")
    return "\n".join(lines)


# 控制字符 → 空格 的翻译表：json 字符串里出现未转义换行/制表符时，先"洗"一遍再解析
_CTRL_TO_SPACE = str.maketrans({"\n": " ", "\r": " ", "\t": " "})


def _iter_json_objects(text: str):
    """依次产出文本里所有【括号平衡】的顶层 JSON 对象片段。

    为什么要换掉 `re.search(r"\\{.*\\}", text, re.DOTALL)`：贪婪 `.*` 从第一个 `{`
    一路吃到**最后一个** `}`，三种情况必翻车（09-10 实测复现，见 verify_agent_parse.py）：
      ① 尾部不闭合（输出被截断）→ 只能退回"最后一个内层 `}`"，抠出半截碎片；
      ② JSON 之后还有含花括号的文字 → 把尾巴一起吞进来（Extra data）；
      ③ 输出两个 JSON 对象 → 跨对象匹配。
    本函数按深度计数，遇到归零的 `}` 就产出一个完整对象；字符串内的花括号与转义引号
    都跳过。**最外层那个 `{` 若始终不闭合，就一个都不产出**——宁可交给上层兜底留证，
    也绝不把"半截碎片"或"plan 里某个子对象"当成决策对象返回。
    """
    i, n = 0, len(text)
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth, in_str, esc = 0, False, False
        for j in range(i, n):
            ch = text[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    yield text[i:j + 1]
                    i = j + 1
                    break
        else:
            return   # 这个 { 到文本末尾都没闭合 → 后面不会再有好对象了


def _extract_first_json(text: str) -> str:
    """第一个【括号平衡】的 JSON 对象片段；没有则 ""（供诊断/兼容使用）。"""
    return next(_iter_json_objects(text or ""), "")


# 决策对象的"身份键"：多个 JSON 对象并存时，用它挑出真正的那一份
_PLAN_KEYS = ("plan", "plans", "actions")
_REASONING_KEYS = ("reasoning", "think", "思考", "thought")


def _load_json_loose(text: str) -> dict:
    """容错解析：枚举所有平衡对象 → 逐个宽松解析 → 优先返回"含 plan 的那个"。

    为什么优先 plan：模型偶尔会先吐一个小对象（例如只有 reasoning 的自检），再吐真正
    的决策对象；只取"第一个对象"会拿到错的那份。plan 是决策对象的身份键，用它挑最稳。
    解析两轮：① 原样；② 控制字符替成空格。`strict=False` 治的是"字符串里有未转义
    换行/制表符"——模型写长段中文「内部思考」时很常见的毛病，标准 json.loads 直接拒绝。
    """
    parsed = []
    for frag in _iter_json_objects(text or ""):
        for attempt in (frag, frag.translate(_CTRL_TO_SPACE)):
            try:
                obj = json.loads(attempt, strict=False)
            except (ValueError, TypeError):
                continue
            if isinstance(obj, dict):
                parsed.append(obj)
                break
    if not parsed:
        return {}
    for key in _PLAN_KEYS:
        for obj in parsed:
            if obj.get(key) is not None:
                return obj
    return parsed[0]


def _json_fail_detail(text: str) -> dict:
    """解析失败时补齐诊断信息（只在失败路径调用，不拖慢正常决策）。"""
    objs = list(_iter_json_objects(text or ""))
    if not objs:
        err = "花括号未闭合——输出疑似被截断"
    else:
        err = "对象可抠出，但解析或取键失败"
        for frag in objs:
            try:
                json.loads(frag.translate(_CTRL_TO_SPACE), strict=False)
            except ValueError as e:
                err = "%s: %s" % (type(e).__name__, e)
                break
    return {"text_len": len(text or ""), "objs": len(objs), "error": err}


def _note_parse_fail(stage: str, text: str, npc_id: str = "") -> None:
    """解析失败【不再静默】：写进 /debug/trace，附 json 异常原文 + 文本尾部 300 字。

    09-10 教训：解析失败原本被 `except ValueError: data = None` 吞掉，面板上只表现为
    "兜底 wait + 无 AI 思考"，完全查不出原因（而 debug_trace 里的 raw 还被 _clip 砍到
    600 字符，连尾巴都看不到）。
    """
    try:
        detail = _json_fail_detail(text)
        debug_trace.record(stage, npc_id=npc_id, raw=(text or "")[-300:],
                           error=detail.pop("error"), parsed=detail)
    except Exception:  # noqa: BLE001  留证失败绝不能带崩决策
        pass


def _is_fallback_plan(plan: list) -> bool:
    """plan 是否为"解析失败兜底项"（唯一一条 FALLBACK_INTENT）。"""
    return bool(plan) and len(plan) == 1 and str(
        (plan[0] or {}).get("intent", "")) == FALLBACK_INTENT


def _parse_plan(raw: str, npc_id: str = "") -> list:
    """把 LLM 返回文本解析成【按优先级降序的多意图列表】；失败兜底 [wait]。

    为什么兜底：LLM 偶尔会输出"好的，我决定..."这种废话前缀，或直接吐 dict 字面量
    （单引号），json 解析会失败；兜底单个 wait 保证引擎不因一次坏输出而崩溃。
    但"容错"不等于"静默"：解析失败一律写 /debug/trace（stage=plan_parse_fail）留证。

    兼容旧格式：LLM 偶尔仍吐单个 {intent, action, speech}（无 plan 键）——此时返回
    单元素列表；plan 缺失的条目补空 action，确保下游都能解构出 action dict。
    """
    text = (raw or "").strip()
    data = _load_json_loose(text)
    if not data:
        _note_parse_fail("plan_parse_fail", text, npc_id=npc_id)
    # 键名别名：模型偶尔把 plan 写成 plans / actions
    plan = data.get("plan") or data.get("plans") or data.get("actions")
    if not isinstance(plan, list) or not plan:
        # 回退：可能是旧的单意图格式（顶层即一个 intent 对象）
        plan = [data] if (data.get("intent") is not None or data.get("action") is not None) else []

    out = []
    for it in plan:
        if not isinstance(it, dict):
            continue
        action = it.get("action")
        if not isinstance(action, dict):
            action = {}
        out.append({
            "intent": str(it.get("intent", FALLBACK_INTENT)),
            "honest": bool(it.get("honest", True)),
            "plan_step": bool(it.get("plan_step", False)),
            "action": action,
            "speech": it.get("speech"),
        })

    if not out:
        out = [{"intent": FALLBACK_INTENT, "honest": True, "plan_step": False,
                "action": {"type": "wait", "target": "", "location": "", "detail": ""},
                "speech": None}]
    return out


def _parse_reasoning(raw: str) -> str:
    """从 LLM 返回文本里抠出「内部思考过程」，失败返回 ""（不影响决策）。

    **必须与 _parse_plan 同源**（共用 _load_json_loose）：否则会出现"plan 解析成功、
    reasoning 却拿不到"这种自相矛盾的状态。09-10 那轮"AI 思考消失"正是两者一起失败
    （同一条 JSON 坏了），单看现象会误判成"模型不思考了"。

    决策需要"它为什么这么选"，/debug/overview 据此展示 AI 思考过程（第4点）。
    """
    # 复用 _load_json_loose（默认优先"含 plan 的那个对象"）→ 与 _parse_plan 拿到**同一份**
    # 决策对象，保证"plan 有、reasoning 却没有"只可能因为模型真漏了字段。
    data = _load_json_loose((raw or "").strip())
    if not isinstance(data, dict):
        return ""
    # 键名别名：模型偶尔把 reasoning 写成 think / 思考 / thought
    for key in _REASONING_KEYS:
        r = data.get(key)
        if r:
            return str(r)
    return ""


def _build_decision(npc_id: str, tick: int, plan: list, reasoning: str = "") -> dict:
    """从优先级 plan 构建带「当前生效镜像」的 decision。

    镜像字段（action/speech/intent/honest）指向 plan 当前生效项（初始 plan[0]），
    供旧结算（fate/director/environment 都只读 decision.action 单对象）零改动使用；
    新逻辑用 decision["plan"] + _plan_index 配合 active_action()/reject_and_advance()
    做「被拒→递推下一优先级意图」。

    Args:
        plan: 已规整过的 _parse_plan 输出（每项含 intent/honest/action/speech）。
    Returns:
        形如 {"agent",tick,"plan",_plan_index,:...,"intent","honest","action","speech"}。
    """
    if not plan:
        plan = [{"intent": FALLBACK_INTENT, "honest": True, "plan_step": False,
                 "action": {"type": "wait", "target": "", "location": "", "detail": ""},
                 "speech": None}]
    cur = plan[0]
    return {
        "agent": npc_id,
        "tick": tick,
        "plan": plan,
        "_plan_index": 0,
        "reasoning": reasoning,
        "intent": str(cur.get("intent", "")),
        "honest": bool(cur.get("honest", True)),
        "plan_step": bool(cur.get("plan_step", False)),
        "action": cur.get("action") or {},
        "speech": cur.get("speech"),
    }


def active_action(decision: dict) -> dict:
    """取当前生效的行动（镜像 decision["action"]，供旧结算读取，零改动复用）。"""
    return decision.get("action") or {}


def active_speech(decision: dict):
    """取当前生效的台词（镜像 decision["speech"]）。"""
    return decision.get("speech")


def reject_and_advance(decision: dict) -> dict:
    """当前意图被拒（如「对话邀请」被对方拒绝）→ 递推到下一个优先级意图。

    原地更新 decision 的镜像字段（action/speech/intent/honest/_plan_index）指向 plan 的下一项。
    若已穷尽所有候选（下一步越界）则收尾到 wait，并保持 _plan_index=len(plan)，使后续再次
    递推继续返回 wait，绝不回跳到 plan[0]、绝不抛错（容错优先，与决策兜底口径一致）。

    Returns: 更新后的 decision（原地修改并返回）。
    """
    plan = decision.get("plan") or []
    idx = int(decision.get("_plan_index", 0) or 0) + 1
    if idx >= len(plan):
        cur = {"intent": "所有打算都被打断，只能原地等待", "honest": True, "plan_step": False,
               "action": {"type": "wait", "target": "", "location": "", "detail": ""},
               "speech": None}
        idx = len(plan)  # 越界标记：已无可用候选，后续继续返回 wait
    else:
        cur = plan[idx]
    decision["_plan_index"] = idx
    decision["intent"] = str(cur.get("intent", ""))
    decision["honest"] = bool(cur.get("honest", True))
    decision["plan_step"] = bool(cur.get("plan_step", False))
    decision["action"] = cur.get("action") or {}
    decision["speech"] = cur.get("speech")
    return decision


def decide(npc_id: str, tick: int, session_id: str, world_id: str = _DEFAULT_WORLD) -> dict:
    """让 NPC 在 tick 时刻做一次决策，返回 decision dict。

    Args:
        world_id: 世界维度（决定角色决策器挂在哪个世界叙事下，默认 golden）。
            测试世界模拟时显式传 "test"，否则测试男子的决策器会自称黄金乡人（OOC）。

    Returns:
        形如 {"agent":npc_id,"tick":tick,"intent":...,"honest":...,"action":{...},"speech":...}。
        决策后自动写自我记忆（我做了什么/我说了什么/是否撒谎）——失败不影响决策本身。
    """
    snap = build_snapshot(npc_id, tick, session_id, world_id)
    # 心智路径（v0.3）：先走"环境观察→价值核对→情绪→计划对照"环节（纯 code，零 LLM），
    # 中间态注入 prompt；未配置 mental_model 的角色走旧平铺路径。
    try:
        mental_ctx = mind_engine.process_observation(npc_id, session_id, world_id, tick)
    except Exception:  # noqa: BLE001  观察失败不挡决策（旧路径兜底）
        mental_ctx = None
    # recorder ①：这一 tick 它得知了什么（察觉/情绪/工作记忆尾部）
    if mental_ctx:
        st = mental_ctx.get("state") or {}
        recorder.note_learned(session_id, npc_id, {
            "noticed": [str(n) for n in (st.get("noticed") or [])],
            "emotion": st.get("emotion_word") or "",
            "working_memory": [str(w) for w in (st.get("working_memory") or [])],
        }, tick=tick)
    user = _snapshot_to_prompt(npc_id, tick, snap, mental_ctx=mental_ctx)
    messages = [
        {"role": "system", "content": _system_prompt(world_id)},
        {"role": "user", "content": user},
    ]
    t0 = time.perf_counter()
    try:
        # tag：一个 tick 内多路 NPC 并发决策，带上 "test_man@t12" 才能在 /debug/trace
        # 里分清哪条属于谁（此前 trace 的 npc_id 恒空，只能靠读提示词猜）。
        # 解析失败会自动重试一次（见 _decide_raw_with_retry）。
        raw = _decide_raw_with_retry(messages, npc_id, tick)
    except Exception as e:  # noqa: BLE001  没收到 AI 回复：错误显式记录（用户要求），不静默
        decide_ms = (time.perf_counter() - t0) * 1000.0
        decision = _build_decision(npc_id, tick, [{
            "intent": f"AI 未响应（{type(e).__name__}），原地等待",
            "honest": True,
            "action": {"type": "wait", "target": "", "location": "", "detail": ""},
            "speech": None,
        }])
        decision["llm_error"] = str(e)[:300]
        recorder.note_error(session_id, npc_id, "decide", str(e)[:300],
                            messages=messages, ms=decide_ms, tick=tick)
        # 自我记忆已收口到世界结算后（mind_engine.record_self_action 在
        # world._settle_live_tick/_step_world_tick_inner 结算完成后按最终生效决策调用），
        # 这里不再提前写——否则记的是 plan[0] 意图，而非递推/导演后的真实动作。
        return decision
    plan = _parse_plan(raw, npc_id=f"{npc_id}@t{tick}")
    decide_ms = (time.perf_counter() - t0) * 1000.0
    reasoning = _parse_reasoning(raw)
    if not reasoning and not _is_fallback_plan(plan):
        # 另一种"AI 思考消失"：JSON 能解析、但取不到 reasoning 键（模型漏字段/改键名）。
        # 必须单独留证——否则面板上同样只是"没有思考"，与解析失败长得一模一样。
        debug_trace.record("reasoning_missing", npc_id=f"{npc_id}@t{tick}", raw=raw[:300],
                           error="JSON 可解析但取不到 reasoning/think/思考/thought 任一键")
    decision = _build_decision(npc_id, tick, plan, reasoning=reasoning)
    # recorder ②③④⑥：提示词 / 行动标签 / AI 输出 / 决策耗时
    recorder.note_prompt(session_id, npc_id, messages, decide_ms, decision, tick=tick)
    # 自我记忆已收口到世界结算后（world._settle_live_tick / _step_world_tick_inner
    # 结算完成后按最终生效决策调用），这里不再提前写——否则记的是 plan[0] 意图，
    # 而非 speak 被拒/导演裁决后的真实动作（09-09 记忆与实际执行脱节根因）。
    return decision


def decide_many(npc_ids: list, tick: int, session_id: str,
                world_id: str = _DEFAULT_WORLD, max_workers: int = 4) -> list:
    """并行决策（世界时序 v2）：同 tick 多个 NPC 的 LLM 调用并发执行。

    一致性语义：所有决策者面对的是"本 tick 计划效果已落、彼此决策未落"的世界——
    决策阶段只读不写世界状态，效果统一由 fate 在仲裁阶段按确定性顺序落库。

    Args:
        npc_ids: 本 tick 需要动脑的 NPC（调用方已按存活/排班过滤）。
        max_workers: 并发上限（透传 llm.chat_many 的线程池；默认 4 保守适配 RPM）。
    Returns:
        decisions 列表，顺序与 npc_ids 一致；单路失败兜底为 wait 决策（引擎不因
        一次坏输出崩溃——与单路 decide 的容错口径一致）。
    """
    if not npc_ids:
        return []
    if len(npc_ids) == 1:
        return [decide(npc_ids[0], tick, session_id, world_id)]
    from concurrent.futures import ThreadPoolExecutor

    def _one(npc_id):
        try:
            return decide(npc_id, tick, session_id, world_id)
        except Exception as e:  # noqa: BLE001  单路失败 → wait 兜底
            return {"agent": npc_id, "tick": tick, "intent": f"决策失败({e})，原地等待",
                    "honest": True,
                    "action": {"type": "wait", "target": "", "location": "", "detail": ""},
                    "speech": None}

    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(npc_ids)))) as pool:
        results = list(pool.map(lambda nid: _one(nid), npc_ids))
    return results


if __name__ == "__main__":
    # 演示：金穗夫人在 tick 34（23:40）的决策（会真实调一次 LLM）
    d = decide("isabella", 34, "demo_session")
    print("decision JSON：")
    print(json.dumps(d, ensure_ascii=False, indent=2))
