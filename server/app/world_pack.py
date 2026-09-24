"""世界模组包（World Pack）加载器：随模组（world_id）变化的配置与引擎默认分离。

架构定位（模组化裁决 A6）：
- **引擎不变的**：五阶段 tick、心智管线、grounding、仲裁、执行器——代码；
- **随模组变化的**：人物/环境/物品/秘密（DB seed）+ **说书人语气、意图动词词典、
  情绪词表、提示词模板**（本模块管理的 worlds/<world_id>/ 目录）。

目录结构（全部可选，缺省回退引擎内置默认——"没配也能跑"）：
  server/worlds/<world_id>/
    manifest.json     模组元信息：name/version/author/description/entry（开场剧情页）
    storyteller.json  AI 说书人：{"system": "旁白人设与文风", "tone": "基调说明"}
    prompts.json      提示词模板覆盖：{"role_lock": "...", "decision_guide": "...", ...}
    lexicon.json      语言词典：{"write_verbs": [...], "dialogue_verbs": [...],
                                  "position_verbs": [...], "observe_verbs": [...]}
    emotions.json     情绪词表覆盖：{"fear": ["惊恐万分", "紧张不安", "隐隐发慌"], ...}
                      （情绪族的 PAD 基向量是心理学常量不随模组变，只有"词"变）

设计原则：
- 零迁移：文件系统配置，不占 DB 列；
- 引擎默认 = 现有内置文本原样抽出（改模组不需要改代码，改代码不影响模组）；
- 进程内缓存（模组文件在运行期视为只读；改配置重启生效——与 seed 语义一致）。
"""
import json
import threading
from pathlib import Path

_WORLDS_ROOT = Path(__file__).resolve().parents[1] / "worlds"
_cache: dict = {}
_lock = threading.Lock()


def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load(world_id: str) -> dict:
    """读某世界的模组配置（缺目录/缺文件 → 空 dict，引擎走内置默认）。"""
    wid = str(world_id or "").strip()
    if not wid:
        return {}
    with _lock:
        if wid not in _cache:
            root = _WORLDS_ROOT / wid
            pack = {}
            if root.is_dir():
                for name in ("manifest", "storyteller", "prompts", "lexicon", "emotions"):
                    data = _load_json(root / f"{name}.json")
                    if data:
                        pack[name] = data
            _cache[wid] = pack
        return _cache[wid]


def manifest(world_id: str) -> dict:
    """模组元信息：{name, version, author, description, entry, initial_scene}。"""
    m = load(world_id).get("manifest") or {}
    return {"name": m.get("name", ""), "version": m.get("version", ""),
            "author": m.get("author", ""), "description": m.get("description", ""),
            "entry": m.get("entry", ""),
            "initial_scene": initial_scene(world_id)}


# ---------------------------------------------------------------------------
# 接入点 0：玩家开局初始场景（09-09 用户需求——不再写死，随模组自动更换）
# ---------------------------------------------------------------------------
_DEFAULT_INITIAL_SCENE = "gate"


def initial_scene(world_id: str) -> str:
    """玩家开局（一局轮回开始）所在的场景。

    09-09 之前写死在 sessions.INITIAL_STATE['scene']='gate'，导致测试世界（只有
    room_1/2/3、无 gate）开局落在不存在的场景。改为读模组 manifest.json 的
    initial_scene 字段：不同模组 = 不同初始场景，加模组不改代码。
    缺省回退"gate"（默认模组/未配置时的引擎兜底，兼容旧调用）。
    """
    m = load(world_id).get("manifest") or {}
    sc = str(m.get("initial_scene", "")).strip()
    return sc or _DEFAULT_INITIAL_SCENE


# ---------------------------------------------------------------------------
# 接入点 1：AI 说书人（旁白 system——世界文风的灵魂）
# ---------------------------------------------------------------------------
_DEFAULT_STORYTELLER = (
    "你是一个文字冒险游戏的旁白/世界叙事者。用简体中文、第二人称客观描述玩家"
    "所处这个世界对玩家行动的反应，不要替任何角色说话，不要打破第四面墙。"
)


def storyteller_system(world_id: str) -> str:
    """说书人 system prompt（模组可覆盖；缺省=引擎内置旁白）。"""
    st = load(world_id).get("storyteller") or {}
    text = str(st.get("system", "")).strip()
    return text or _DEFAULT_STORYTELLER


# ---------------------------------------------------------------------------
# 接入点 2：意图动词词典（快路径规则——语言/世界风格变化时模组可换）
# ---------------------------------------------------------------------------
_DEFAULT_LEXICON = {
    "write_verbs": ["拿", "拿起", "取", "放", "放回", "藏", "藏起", "凿", "刻", "撬",
                    "开", "关", "关门", "开门", "移动", "搬", "挪", "插", "藏匿", "推",
                    "拉", "拆", "点燃", "熄灭", "破坏", "按住", "堵住", "捡起", "捡",
                    "掏出", "藏好"],
    "dialogue_verbs": ["问", "说", "打听", "告诉", "召唤", "召见", "询问", "交谈"],
    "position_verbs": ["去", "走到", "前往", "移动", "来到", "抵达", "进入", "离开", "到", "回", "动身"],
    "observe_verbs": ["看", "观察", "望", "环顾", "端详", "打量", "四下", "审视", "查看"],
}


def lexicon(world_id: str, group: str) -> tuple:
    """动词词典组（模组 lexicon.json 可覆盖；缺省=引擎内置中文动词）。

    group="npc_names" 是**运行时注入**组：从 character_card 拉本世界当前 NPC 的名字
    （中文名），供攻击/对话识别在玩家文本里匹配到 NPC 名。
    """
    if group == "npc_names":
        from . import db
        names = []
        try:
            rows = db.execute_query(
                "SELECT npc_id, name FROM character_card WHERE is_active=1")
            for _npc_id, name in rows:
                if name:
                    names.append(str(name))
        except Exception:  # noqa: BLE001  读不到角色卡时静态兜底
            pass
        return tuple(names)

    lex = load(world_id).get("lexicon") or {}
    merged = list(_DEFAULT_LEXICON.get(group, []))
    override = lex.get(group)
    if isinstance(override, list) and override:
        merged = [str(v) for v in override]  # 显式覆盖（换语言/换风格）
    extra = lex.get(f"{group}_extra")
    if isinstance(extra, list):
        merged += [str(v) for v in extra]   # 追加（只加不减）
    return tuple(merged)


def npc_name_to_id(text: str, world_id: str = "test") -> str:
    """在文本里匹配 NPC（中文名 或 id），命中则返回其 **npc_id**（如 test_man）。

    为什么必须转成 npc_id：npc_status/npc_pos 的键、_decide_targets 的 dead 过滤都按
    npc_id（get_all_npc_ids）对齐——若只返回中文名（"测试男"），死亡状态会写进
    npc_status:测试男 这个键，而引擎读的是 npc_status:test_man，导致死亡过滤失效（复活）。
    """
    from . import db
    try:
        rows = db.execute_query(
            "SELECT npc_id, name FROM character_card WHERE is_active=1")
    except Exception:  # noqa: BLE001
        return ""
    for npc_id, name in rows:
        if name and str(name) in text:
            return str(npc_id)
        if npc_id and str(npc_id) in text:
            return str(npc_id)
    return ""


def npc_id_to_name(npc_id: str, world_id: str = "test") -> str:
    """npc_id → 中文名（旁白显示用，避免裸吐 test_man）。读不到返回空串。"""
    from . import db
    try:
        rows = db.execute_query(
            "SELECT name FROM character_card WHERE npc_id=%s AND is_active=1",
            (npc_id,))
    except Exception:  # noqa: BLE001
        return ""
    return str(rows[0][0]) if rows else ""


# ---------------------------------------------------------------------------
# 接入点 3：情绪词表（OCC 族的"词"随模组文风变化；PAD 基向量不变）
# ---------------------------------------------------------------------------
_DEFAULT_EMOTION_WORDS = {
    "anger": ["盛怒", "恼火", "有些烦躁"],
    "fear": ["惊恐", "紧张不安", "隐隐发慌"],
    "sadness": ["悲痛欲绝", "闷闷不乐", "有些低落"],
    "disgust": ["深恶痛绝", "心存鄙夷", "略感反感"],
    "joy": ["狂喜", "劲头十足", "颇感振奋"],
    "calm": ["如释重负", "平静", "略感轻松"],
    "liking": ["满心欢喜", "颇有好感", "略生好感"],
}


def emotion_words(world_id: str, family: str) -> tuple or None:
    """某情绪族的词三元组（强/中/微）；模组未覆盖返回 None（用 mental.FAMILIES 默认）。"""
    emo = load(world_id).get("emotions") or {}
    words = emo.get(family)
    if isinstance(words, list) and len(words) == 3:
        return tuple(str(w) for w in words)
    return None


# ---------------------------------------------------------------------------
# 接入点 4：提示词模板覆盖（compose 段落引导语/决策器 system 等关键文本）
# ---------------------------------------------------------------------------
_DEFAULT_PROMPTS = {
    # 角色锁定（compose L1 尾）
    "role_lock": "你必须始终以这个角色身份说话，用简体中文，绝不跳出角色，绝不承认你是 AI。",
    # 思维链脚手架引导（compose L6）
    "chain_scaffold": "你思考时的习惯顺序：{chain}。遇事按这个顺序在心里过一遍再开口或行动。",
    # 决策引导（agent decide：创造性手段）
    "decision_guide": "以你的身份去想办法——方法总比困难多：你所在的位置、身上的东西、"
                      "房间里的一切、其他人的动向，都可以成为你的手段。",
    # 链感知引导
    "future_inference": "决定前先在心里推演：这一步做完，局面会变成什么样？对你的目标有什么影响？",
    "emotion_regulation": "如果心里发慌，先稳住自己再行动——慌乱中选择容易出错。",
    # 空间快照引导（compose L3.5 前缀）
    "spatial_snapshot_prefix": "你所在",
    # 场景文学旁白引导（narrate_scene，09-09 抽离独立管理）。
    # 09-09 按场景视角拆成两个独立键：统一放在本 PROMPTS / 模组 prompts.json 体系里管理（统一），
    # 但 arriving / lingering 两个视角各自独立、可分别覆盖（分开）——改某一视角的文风/约束只动对应键，
    # 不碰另一个。五条铁律（防幻觉/防破墙/物品移动/玩家有限视角/结构以快照为准）两者语义完全一致，
    # 只是视角句不同：arriving=玩家刚跨入，lingering=玩家停留片刻后重新环顾。
    # 导演对话分析（T3，v0.4）：NPC↔NPC 交谈成立后，导演全知视角分析"讨论了什么 +
    # 对彼此认知/关系/情绪的影响"。产物 JSON 的键位结构见 director.analyze_dialogue。
    "director_dialogue_analysis": "你是叙事引擎的「对话导演」。两名 NPC 刚刚在交谈，"
                                 "请以全知视角分析这段对话产生了什么影响。只输出一个 JSON 对象，"
                                 "不要其它文字。字段：\n"
                                 '{"discussion":"两人讨论了什么（一两句，有画面感）",'
                                 '"cognition":{"<actor_id>":"对方在此次交谈中的认知变化（印象/判断，'
                                 '写成一句给该角色将来回忆的叙事）"},"relation":{"<actor_id>":"或-2到+2的数字'
                                 '（该角色对另一方的信任/好感净增：正=更亲近，负=更疏远）"},'
                                 '"emotion":{"<actor_id>":"或给该角色此刻的主导情绪（中文，如"恼火/平静/紧张"）"}}\n'
                                 "注意：actor_id 必须用参与者真实 id，一个都不能少；"
                                 "emotion 给中文词即可（引擎会反查成数值）；"
                                 "relation 若不变给 0。",
    # 角色决策器·动作类型 + plan_step 语义（P2 收口）：agent._system_prompt 不再内联这些，
    # 改走 world_pack.prompt(world_id, "action_types_guide")，模组 prompts.json 可覆盖。
    "action_types_guide": "动作类型只能从这 9 类里选，target/location 填对应 id，没有就填空字符串：\n"
                          "- move：移动到某地点，target=地点id\n"
                          "- use_item：使用/拿取物品，target=物品id\n"
                          "- give_item：把物品给某人，target=物品id\n"
                          "- speak：与某人说话，target=人物id，speech 填台词（注意：说话是一次对话邀请，对方可能拒绝）\n"
                          "- observe：观察某地/物/人，target=对象id\n"
                          "- wait：原地等待/休息，target 填空串\n"
                          "- interact：其他交互（锁门/藏物/下毒等），target=对象，detail 说明动作\n"
                          "- trigger_event：触发剧情事件，target=事件id\n"
                          "- converse：与他人交谈（对话成立时引擎会改写你的行动，target=交谈对象id）\n\n"
                          "重要：intent 是内部动机（写给日志/命运分析器看，别直白暴露给玩家）；\n"
                          "speech 是对外台词（符合人设的口吻），不说话就填 null；\n"
                          "honest 自报这句话是否说的是实话——若 speech 掩盖/歪曲了 intent，填 false（引擎会替你记住真话）；\n"
                          "plan_step 标记这一步是否是你【既定计划的推进项】：若你此刻是在按既定计划推进关键一步，"
                          "填 true（引擎判定你'有事要办'，连续被打断时更倾向于拒绝闲聊）；临时起意/观察/等待填 false。\n"
                          "建议 plan 里放差异较大的行动，让被拒后仍有余地。",
    "scene_narrate_arriving": "根据下面「当前真实状态」，用一两句有画面感的文学旁白，描述玩家"
                              "刚踏进这个场景看到的景象。要求：只描述现状，不要罗列事实清单或标签，"
                              "不要替任何人说话；物品移动/不见了要自然体现；只写玩家视角能看到的动作与方位，"
                              "他不在场/没看到的不要写；墙/门/窗/家具一律以快照【这里的样子】为准，"
                              "快照里没有的元素绝不凭空添加（例如无窗就不能提到'窗'）。简笔白描，一两句，不要再展开。",
    "scene_narrate_lingering": "根据下面「当前真实状态」，用一两句有画面感的文学旁白，描述玩家"
                               "在这个场景停留片刻、重新环顾时看到的景象。要求：只描述现状，不要罗列事实清单或标签，"
                               "不要替任何人说话；物品移动/不见了要自然体现；只写玩家视角能看到的动作与方位，"
                               "他不在场/没看到的不要写；墙/门/窗/家具一律以快照【这里的样子】为准，"
                               "快照里没有的元素绝不凭空添加（例如无窗就不能提到'窗'）。简笔白描，一两句，不要再展开。",
    "scene_narrate_conv_end": "根据下面「当前真实状态」，用一两句有画面感的文学旁白，描述【玩家刚结束"
                              "一段对话、注意力重新回到现实】时看到的景象。要求：视角句要体现『你和{partner}结束了"
                              "对话，注意力重新回到现实』的转场感（描述当下场景及稍早那格里发生的事），不要出现"
                              "'你刚走进来'的入场口吻；只描述现状，不要罗列事实清单或标签，不要替任何人说话；"
                              "物品移动/不见了要自然体现；只写玩家视角能看到的动作与方位，他不在场/没看到的不要写；"
                              "墙/门/窗/家具一律以快照【这里的样子】为准，快照里没有的元素绝不凭空添加。"
                              "简笔白描，一两句，不要再展开。{partner} 是刚结束对话的另一方名字。",
    # 解析再试提示（09-10）：玩家那句话太复杂、解析器第一次没通过程序校验时，
    # 立刻推给前端的一句提示——让玩家知道"系统正在重试、不是卡住了"。
    # 语气要短、要有画面感，不要说技术名词（玩家不知道什么是"解析"）。
    "notice_reparse": "事情比想象中复杂……",
}


def prompt(world_id: str, key: str) -> str:
    """提示词模板（模组 prompts.json 可覆盖；缺省=引擎内置文本）。"""
    pr = load(world_id).get("prompts") or {}
    text = pr.get(key)
    if isinstance(text, str) and text.strip():
        return text
    return _DEFAULT_PROMPTS.get(key, "")


def available_worlds() -> list:
    """列出有模组目录的世界（供启动器/主菜单展示）。"""
    out = []
    if _WORLDS_ROOT.is_dir():
        for d in sorted(_WORLDS_ROOT.iterdir()):
            if d.is_dir() and (d / "manifest.json").exists():
                out.append(d.name)
    return out
