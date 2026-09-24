"""意图识别层：把玩家一句自然语言翻译成结构化 Intent，分流到对话/环境管线。

为什么需要它（环境管线的第一环）：
  玩家不论对环境采取行动、还是对话，都在同一个输入框（/chat）里发言。
  后端必须先判断"这句是对话还是环境行为"，才能决定走哪条管线——
  别把复杂度推给前端（前端永远是薄薄一层：一句话 + 会话/世界 id）。

核心洞察（与需求对齐）：「做什么」与「会不会改变世界」是两个正交维度。
  例：『观察』可以是只读（站着看），也可以是写入（凿墙偷窥）——
  所以不能只按动词分类，必须用两组维度：
    domain       : dialogue(对NPC) / spatial(对场景+实体) / self(自身移动)
    side_effect  : read_only(只读感知) / mutating(会写回状态/痕迹)

算法路径：规则优先 + LLM 兜底（分层，沿用项目一贯作风）
  ① 规则层：零成本、可审计、覆盖高频确定动词（看=只读；拿/凿=写入；说=对话）
  ② LLM 兜底：仅规则判不出/冲突（如"看"+"凿"）才调一次，输出结构化 Intent JSON
  ③ 两者产出同一 Intent 结构 → 下游（空间查询/感知/执行器）无感知

四条铁律（承接 resolve() 理念）：
  - LLM 只输出"意图结构"，不摸坐标/不写数字（坐标翻译交给 resolve()）
  - 全走规则或全走 LLM 都不可取：规则快/可审计，LLM 兜底自由语义
  - 规则判出的高频句可缓存（本层暂不做，产出统一 Intent 后优化随时可加）
  - Intent 结构稳定（domain/side_effect/target/verb），下游解耦
"""
import json
import logging
import re

from . import spatial as spatial_mod
from . import debug_trace
from .llm import DeepSeekClient

logger = logging.getLogger(__name__)

# 模块级 LLM 单例：复用连接，避免每次兜底都新建客户端（与 agent 同款）
_client = None
def _llm() -> DeepSeekClient:
    global _client
    if _client is None:
        _client = DeepSeekClient()
    return _client


# ---------------------------------------------------------------------------
# ① 规则词典（零成本分类）
#   - 强对话动词：几乎必然是"跟人说话"
#   - 强位置动词：自身移动（去/走到/前往）
#   - 强观察动词：只读感知（默认 side_effect=read_only）
#   - 强写入动词：会改变世界（拿/放/凿/刻/藏/开门...），mutating
#   - 覆盖规则不出来的词，留给 LLM 兜底
# ---------------------------------------------------------------------------
_DIALOGUE_VERBS = ("问", "说", "打听", "告诉", "召唤", "召见", "询问", "交谈",
                   "聊聊", "对话", "交代", "汇报", "质问", "试探", "称呼", "跟")
_POSITION_VERBS = ("去", "走到", "前往", "移动", "来到", "抵达", "进入", "离开", "到", "回", "动身")
_OBSERVE_VERBS = ("看", "观察", "望", "环顾", "端详", "打量", "四下", "审视", "查看",
                  "听着", "听", "打量", "张望", "沉思", "回想", "抬头", "低头")
_WRITE_VERBS = ("拿", "拿起", "取", "放", "放回", "藏", "藏起", "凿", "刻", "撬", "开",
                "关", "关门", "开门", "移动", "搬", "挪", "插", "藏匿", "推", "拉", "拆",
                "点燃", "熄灭", "破坏", "按住", "堵住", "捡起", "捡", "掏出", "藏好")
# 攻击/伤害类动词（对 NPC/目标造成伤害）：让"用刀偷袭/刺中要害/攻击/杀掉/捅死"
# 这类行动被识别为对目标（通常是 NPC）的 mutating 意图，而非被误判成"说话"。
# 优先级最高（写世界 > 对话）：攻击若被当对话，NPC 只会回一句台词（如"偷袭不是我的风格"），
# 而世界毫无变化——这正是测试中"NPC 被我刺中要害却不死、只是聊了句"的根因。
_ATTACK_VERBS = ("偷袭", "攻击", "刺杀", "刺中", "刺", "捅", "砍", "劈", "杀", "打死",
                 "击中", "重击", "挥刀", "打向", "扑向", "击倒", "干掉", "扎", "戳")
# 攻击/搜尸对象判定：含这些"指代某个人"的词 → 判为对 NPC 的攻击/搜身（而非物品）。
# 从5个硬编码扩充为口语指代（B1 语义指代消解第一步）：覆盖"那个男的/那位先生/
# 这男人/这家伙"等直觉性说法——这些都不在预设 ID 名字里，规则层靠口语指代兜住"打的是人"。
# 精确"到底是谁"仍由 _llm_classify 的在场角色消解/执行器在场唯一NPC兜底完成（见 B2）。
_NPC_PRONOUNS = ("他", "她", "那人", "男人", "女人", "女的", "男的", "那人", "那位",
                 "这位", "先生", "女士", "小姐", "小哥", "姑娘", "家伙", "老头", "老婆婆",
                 "这男人", "这女人", "那个人", "那个男的", "那个女的", "这个男的", "这个女的")
# 攻击句内的"给予"语境排除词：出现这些词说明是"给/交给/递给"而非攻击（如"把刀给她防身"）。
_ATTACK_GIVE_BLOCK = ("给", "交给", "递给", "递", "送", "给她", "给他", "借", "归还", "使用")
# 搜尸/摸尸动词：对尸体"搜身/搜刮/摸尸/检查他身上"。识别为 mutating（转移物品改世界），
# op=search。玩家从死者身上拿走随身物品——这是"死人→尸体→可搜身"功能的核心交互。
_SEARCH_VERBS = ("搜身", "搜刮", "搜尸", "摸尸", "摸他身上", "翻他身上", "检查他身上",
                 "翻找尸体", "翻尸体", "剥", "找找身上", "看看他身上", "搜死", "搜搜",
                 "搜一下", "搜一搜", "搜了搜")
# 搜尸判定的"搜"前缀：单独"搜"字太宽（搜索房间），需配合指代 NPC（含代词/人名）才判搜尸。
# 但常见口语"搜搜他身上/搜一下尸体"已入 _SEARCH_VERBS；下面用 _SEARCH_PRONOUN 补"搜尸体/搜他"。
_SEARCH_STRONG = ("搜尸", "搜身", "搜刮", "摸尸", "搜死", "搜尸体")
# 造物/制作类动词：让玩家"凭行动创造物体"（做一根绳子/把椅子腿改成匕首/织一块布…）。
# 这是"AI自由度造物"的入口——具体造什么由 LLM 生成规格（名称/描述），程序落库成可寻址实体。
# 只做"从无到有/改造出新物"的意图占位，真正的语义规格在环境执行器 (_exec_create) 用 LLM 补齐。
_CREATE_VERBS = ("做一根", "做一把", "打造", "制作", "制造", "合成", "组装", "织", "编",
                 "造一个", "造一根", "捏一个", "造", "熔", "锻", "搓", "削成", "改成", "弄成")
# 冲突前缀：出现"听/从...听到"、"观察后"等，可能只读；但"凿/刻/挖"等强写入词
# 若与观察词同现（"凿洞偷窥"），标 mutating——写入词优先级高于观察词。

# 意图识别兜底的 system 提示：让 LLM 当"意图解析器"，只输出 JSON
_INTENT_SYSTEM = (
    "你是文字冒险游戏的「意图解析器」。把玩家的一句话解析成结构化意图 JSON，"
    "只输出一个 JSON 对象，不要任何其他文字、不要 markdown 代码块。\n"
    '格式：{"domain":"spatial|dialogue|self","side_effect":"read_only|mutating",'
    '"verb":"动词","target":{"type":"scene|entity|npc|self","id":"对象id或空","hint":"自然语言定位提示"},'
    '"spatial":{"op":"pick|place|tip_over|move_body|move_self|look|open|close|disassemble|search|create|attack",'
    '"item":"对象id|空","dest_scene":"目标场景id|空","orientation":0或90,'
    '"ref_anchor":"参照锚点id|空","rel":["方位词"],"part":"部件名|空"}}\n\n'
    "判定规则：\n"
    "- domain：对场景/物品/环境的动作=spatial；跟NPC说话/打听=dialogue；自身移动=自self。\n"
    "- side_effect：会不会改变世界状态（拿/放/凿/刻/开门放走/藏=mutating；只看/观察/环顾=read_only）。\n"
    "  注意：'观察'也可能改变世界（如凿墙偷窥），此时 side_effect=mutating。\n"
    "- target：能确定对象id就填id（用提供的实体/场景清单），不确定就id空串、hint填原文方位描述。\n"
    "- verb：就是玩家做的那个动作，如'拿起'、'观察'、'凿'。\n"
    "- spatial.op：空间操作。pick=拿取；place=放置到某处；tip_over=放倒；"
    "move_body=把【物品】搬到某场景；move_self=自身移动到某场景；look=观察；open/close=开关门；"
    "disassemble=从物体上拆下部件（如拆椅子腿，造出独立部件实体）；"
    "search=搜身/搜尸（从尸体身上取物）；create=造物；attack=攻击/伤害某人。\n"
    "- spatial.part：要拆的那部分（如'腿'），disassemble 时填。\n"
    "- spatial.item：操作对象id（拿/放/倒的是哪个物）；dest_scene：目标场景id；"
    "ref_anchor：相对方位参照的锚点id；rel：相对方位词数组（如['左','墙','角']）；"
    "orientation：姿态角（放倒=90，直立=0）。\n\n"
    "例：'我拿起那把刀'→{\"domain\":\"spatial\",\"side_effect\":\"mutating\",\"verb\":\"拿起\","
    '"target":{"type":"entity","id":"knife","hint":"那把刀"},'
    '"spatial":{"op":"pick","item":"knife","dest_scene":"","orientation":0,"ref_anchor":"","rel":[]}}；'
    "'我看这个房间'→{\"domain\":\"spatial\",\"side_effect\":\"read_only\",\"verb\":\"观察\","
    '"target":{"type":"scene","id":"","hint":"这个房间"},'
    '"spatial":{"op":"look","item":"","dest_scene":"","orientation":0,"ref_anchor":"","rel":[]}}'
)


def _contains(text: str, verbs: tuple) -> bool:
    """粗略判断文本是否包含任一动词（子串匹配，够用即可，中文无分词）。"""
    return any(v in text for v in verbs)


class Intent:
    """结构化意图（意图识别层的统一产出，下游无感知）。

    domain        : spatial / dialogue / self
    side_effect   : read_only / mutating（对 dialogue 通常 read_only；self 通常 mutating=位置变化）
    target        : {"type": scene|entity|npc|self, "id": str, "hint": str}
    verb          : 动作动词（原话）
    spatial       : 空间操作详情（环境管线的核心解构，目标①"语义识别调动对应效果"）：
                      {"op": "pick|place|tip_over|move_body|move_self|look|open|close",
                       "item": "<env_id>|",            # 操作对象（拿/放/倒的是哪个物）
                       "dest_scene": "<scene>|",        # 目标场景（搬到/放到哪）
                       "position": [x,y,z]|None,        # 目标坐标（place 时由程序算）
                       "orientation": 0|90|None,        # 姿态角（放倒=90）
                       "ref_anchor": "<env_id>|",       # 相对方位的参照锚点
                       "rel": ["left","wall","front"],  # 相对方位词列表
                       "part": "<部件名>"}              # 拆下的部件（disassemble 用）
    """

    def __init__(self, domain, side_effect, target, verb, spatial=None):
        self.domain = domain
        self.side_effect = side_effect
        self.target = target
        self.verb = verb
        self.spatial = spatial or {}

    def to_dict(self) -> dict:
        return {
            "domain": self.domain,
            "side_effect": self.side_effect,
            "verb": self.verb,
            "target": self.target,
            "spatial": self.spatial,
        }

    def __repr__(self):
        return (f"Intent(domain={self.domain}, side_effect={self.side_effect}, "
                f"verb={self.verb}, target={self.target}, spatial={self.spatial})")


def _npc_target(text: str, world_id: str = "test", scene_id: str = "") -> dict:
    """把玩家文本里"指代的 NPC"定位成 target dict（type=npc, id=npc_id）。

    复用攻击/搜尸共用的指称消解：① 明确 NPC 名（中文名→npc_id）→ 直接用 id；
    ② 否则含"他/她/人"等代词 → id 留空（执行器再用 hint + 在场唯一 NPC 兜底）。
    Returns: {"type":"npc","id":..., "hint":text}；无任何 NPC 指向线索时返回 None。
    """
    from . import world_pack
    npc_hit = world_pack.npc_name_to_id(text, world_id)
    if npc_hit:
        return {"type": "npc", "id": npc_hit, "hint": text}
    # "尸体/遗体"是泛指的指尸体对象（在场那具）——id 留空，执行器用"在场唯一死亡NPC"兜底。
    if any(p in text for p in ("尸体", "遗体") + _NPC_PRONOUNS):
        # 路线B精确消解：先用确定性语义对齐（名字/性别特征 vs 候选NPC），
        # 命中直接填 id（换世界通用，因为候选/名字从 DB 读，零硬编码）。填不出留空交执行器/LLM。
        return {"type": "npc", "id": _disambiguate_npc_target(text, world_id, scene_id),
                "hint": text}
    return None


def _disambiguate_npc_target(text: str, world_id: str = "test", scene_id: str = "") -> str:
    """确定性语义指代消解（B2 路线B的前置，零 LLM、数据驱动）：

    玩家用口语指代某 NPC（"那个男的/那位先生/这个女人"），但 id 里没有明确名字。
    npc_name_to_id 用"名字子串∈text"匹配——玩家未必说出完整名字（说他"男的"而非"测试男"），
    所以这里额外按【特征词】对齐名字，命中最吻合的一个返回 npc_id。

    特征来源（全部从 DB 读，换世界通用）：
      - character_card.name（显示名，如"测试男/测试女"）
      - 名字里的性别字（男/女）或头衔字（先生/女士/管家/骑士…）
    Rules:
      ① 名字子串直接命中玩家原文 → 立即定（明确点名）；
      ② 玩家 hint 含"女"特征（女的/女人/女士/小姐）→ 只留名字带"女"的候选；
         含"男"特征（男的/男人/先生/家伙）→ 只留名字带"男"的候选；
      ③ 过滤后唯一候选 → 返回其 npc_id；
      ④ 仍多候选（如多个男 NPC）→ 返回空串，交执行器"在场唯一"兜底或 LLM 再判，
         不做错误解（宁可不消解，也不瞎指定）。
    Returns: npc_id 或 ""。
    """
    from . import db, world_pack
    # 候选池 = 该世界已启用的 NPC（复用 get_all_npc_ids 的 world_id 隔离，防跨世界指代）
    # + 逐入补名字（npc_id_to_name 读 character_card.name）。
    try:
        npc_ids = db.get_all_npc_ids(world_id)
        rows = [(n, world_pack.npc_id_to_name(n, world_id)) for n in npc_ids]
    except Exception:  # noqa: BLE001  查不到就不做解（安全回退）
        return ""

    feats = _gender_hint(text)          # ("male"/"female"/None) 玩家指代的性别偏好
    # 候选：能提供特征对齐的 NPC。优先名字子串直接命中（"测试男"/"男"在名字里），
    # 再按性别特征过滤——"那个男的"没有名字子串，靠性别对齐到"测试男"。
    cands = []
    for npc_id, name in rows:
        name = str(name or "")
        if name and name in text:
            return npc_id               # 明确点名，直接定
        g = _name_gender(name)          # 该 NPC 名字的性别线索（"测试男"→male）
        if feats and g and g != feats:
            continue                     # 性别冲突，排除
        cands.append((npc_id, name, g))

    if len(cands) == 1:
        return cands[0][0]               # 唯一契合（如"男"只在"测试男"里，女在"测试女"里）
    # 多个同性别候选（如两个男 NPC 都在）：交执行器按在场唯一兜底 / LLM 再判，
    # 这里不做错误解，返回空串让上层去精配。
    return ""


def _gender_hint(text: str) -> str or None:
    """玩家指代的性别偏好：含女特征→female；含男特征→male；都无→None。"""
    if any(w in text for w in ("女的", "女人", "女士", "小姐", "姑娘", "妹", "sister")):
        return "female"
    if any(w in text for w in ("男的", "男人", "先生", "小哥", "家伙", "老头", "爷爷")):
        return "male"
    return None


def _name_gender(name: str) -> str or None:
    """从 NPC 显示名推断性别线索：名字含"女"→female；含"男/先生/骑士/管家/公爵/王子"
    等男性称谓→male；否则 None（中性/未知，不强判）。"""
    if any(w in name for w in ("女", "小姐", "夫人", "女士", "公主")):
        return "female"
    if any(w in name for w in ("男", "先生", "骑士", "管家", "公爵", "王子", "勋爵")):
        return "male"
    return None


def _rule_classify(text: str, scene_id: str, world_id: str = "test") -> Intent:
    """规则层：用动词词典零成本分类。命中即返回 Intent，未命中返回 None 交给 LLM。

    scene_id：玩家当前所在场景（self 型 target 用它当目的地 hint 的默认上下文）。
    所有命中分支都填充 spatial（空间操作详情），保证规则与 LLM 产出同一结构。
    """
    # 攻击/写入等词典所需的 world_pack 词包导入提到函数开头（攻击分支在其后）
    from . import world_pack

    # 先判"搜尸/摸尸"——对尸体的搜身（转移其随身物品 op=search，mutating）。
    # 放在攻击之前：搜身只对"死者"，攻击只对"活人"，语义互斥；命中即定 op=search。
    # 判定放宽两种形态：①明确搜引词（搜身/搜刮/搜尸/摸尸/搜搜…）；②"搜尸体/搜他/搜她"
    # （弱"搜"+强对象——含"尸体"或指代 NPC，才判搜尸；避免"搜房间"误判）。
    _search_hit = _contains(text, _SEARCH_VERBS) or (
        _contains(text, ("搜",)) and _contains(text, _SEARCH_STRONG + ("尸体", "遗体")))
    if _search_hit:
        tgt = _npc_target(text, world_id, scene_id)
        if tgt is not None:
            return Intent("spatial", "mutating", tgt, _first_match(text, _SEARCH_VERBS) or "搜身",
                          {"op": "search", "item": "", "dest_scene": scene_id or "",
                           "position": None, "orientation": 0, "ref_anchor": "", "rel": [],
                           "part": ""})

    # 先判"攻击"——对目标的伤害意图，优先级最高（若被当对话，NPC 只回台词、世界不变）
    # 排除"给予"语境（把刀给她防身 / 交给他用）——那是 give，不是攻击。
    if _contains(text, _ATTACK_VERBS) and not any(g in text for g in _ATTACK_GIVE_BLOCK):
        # 攻击对象：明确 NPC 名/代词 → target.type=npc；否则推 entity（交 resolve_target 精配）。
        # 注意：匹配到中文名后要解析成 **npc_id**（get_all_npc_ids/npc_status 用的是 id，如
        # test_man），否则死亡状态写错键、_decide_targets 的 dead 过滤会失效（NPC"复活"）。
        tgt = _npc_target(text, world_id, scene_id)
        if tgt is not None:
            return Intent("spatial", "mutating", tgt, _first_match(text, _ATTACK_VERBS),
                          {"op": "attack", "item": "", "dest_scene": scene_id or "",
                           "position": None, "orientation": 0, "ref_anchor": "", "rel": [],
                           "part": ""})
        return Intent("spatial", "mutating", _guess_target(text), _first_match(text, _ATTACK_VERBS),
                      {"op": "attack", "item": "", "dest_scene": scene_id or "",
                       "position": None, "orientation": 0, "ref_anchor": "", "rel": [], "part": ""})

    # 先判"造物"（AI自由度）：做/制作/打造/织/合成/削成… 从无到有造出新物。
    # 这些动词不在 write_verbs 里，需独立判定（写世界+mutating）。规格由执行器 LLM 补齐。
    if _contains(text, _CREATE_VERBS):
        dest = _extract_dest(text, world_id)
        _create_verb = _first_match(text, _CREATE_VERBS) or "造物"
        return Intent("spatial", "mutating", _guess_target(text), _create_verb,
                      {"op": "create", "item": "", "dest_scene": dest,
                       "position": None, "orientation": 0, "ref_anchor": "", "rel": [],
                       "part": _infer_part(text)})

    # 先判"写入"——写入词优先级最高（"凿洞偷窥"虽含'看'，但'凿'标 mutating）
    if _contains(text, world_pack.lexicon(world_id, "write_verbs")):
        op, dest = _infer_write_op(text)
        return Intent("spatial", "mutating", _guess_target(text), _first_match(text, _WRITE_VERBS),
                      _mk_spatial(op, dest, text))

    # 对话
    if _contains(text, world_pack.lexicon(world_id, "dialogue_verbs")):
        return Intent("dialogue", "read_only", {"type": "npc", "id": "", "hint": text}, _first_match(text, _DIALOGUE_VERBS))

    # 位置移动（去/走到/前往）→ move_self
    if _contains(text, world_pack.lexicon(world_id, "position_verbs")):
        dest = _extract_dest(text, world_id)
        return Intent("self", "mutating", {"type": "self", "id": "", "hint": text}, _first_match(text, _POSITION_VERBS),
                      _mk_spatial("move_self", dest, text))

    # 观察（只读）
    if _contains(text, world_pack.lexicon(world_id, "observe_verbs")):
        # 观察默认只读；但若同句含"爬上/俯身/凑近/掀开/翻"等不在此词典的写入词，
        # 规则判不出，留 LLM 兜底更稳妥——这里保守返回只读，可被 LLM 覆盖
        return Intent("spatial", "read_only", {"type": "scene", "id": scene_id or "", "hint": text}, _first_match(text, _OBSERVE_VERBS),
                      _mk_spatial("look", scene_id, text))

    return None  # 规则没覆盖到，交给 LLM


def _mk_spatial(op, dest, text) -> dict:
    """构造一个统一的 spatial 解构 dict（规则层与 LLM 层共用同一结构）。

    item/position 先留空：目标物 id 由环境执行器 resolve_target 解析，
    坐标由执行器调 spatial.resolve 根据 rel 方位词计算（程序算坐标，LLM 只给语义）。
    """
    return {
        "op": op,
        "item": "",                       # 操作对象 env_id（执行器解析）
        "dest_scene": dest,               # 目标场景（搬到/放到哪）
        "position": None,                 # 目标坐标（执行器算）
        "orientation": _infer_orientation(text),   # 姿态角（放倒=90）
        "ref_anchor": "",                 # 相对方位参照锚点（执行器解析）
        "rel": _extract_rel(text),        # 相对方位词列表
        "part": _infer_part(text),        # 要拆的部件（disassemble 时用）
    }


def _infer_write_op(text, scene_id="", world_id: str = "test") -> tuple:
    """从写入类文本推断空间操作 op + 目标场景 dest_scene（目标①：语义→对应效果）。

    优先级：放倒>跨场景移动(move_body)>放置(place)>开/关>拿起(pick)。
    例："把椅子搬到房间一"→("move_body","room_1")；"把椅子放倒"→("tip_over","")。
    scene_id 保留给将来需要"当前场景"作默认目的地时用。
    """
    # 造物（AI自由度）：玩家凭行动创造/改造出新物体（做/打造/织/合成/改成…）——
    # 强语义"从无到有"，优先于拆（拆是"从有拆出",造是"从无生出"）。规格由执行器 LLM 补齐。
    if any(k in text for k in _CREATE_VERBS):
        return "create", _extract_dest(text, world_id)
    # 制造误区："做成/做到"不是造物（如"把他做成"→不做造物）——排除"成"结尾的非造物：
    # 上面已命中造物词，这里不用再排；"改成/弄成/削成"已显式列入 _CREATE_VERBS。

    # 拆/卸：把部件从物体上拆下来（造出独立部件实体）——强语义，优先于放置/移动
    if any(k in text for k in ("拆下", "拆掉", "卸下", "拆")):
        return "disassemble", _extract_dest(text, world_id)

    dest = _extract_dest(text, world_id)
    has_dest = bool(dest)
    has_pose = any(k in text for k in ("倒", "角", "墙"))
    # 复合：搬到某地并放倒/放到墙角 → place（一次写 dest+position+orientation，最贴合"搬走并放倒放墙角"）
    if has_dest and has_pose:
        return "place", dest
    if has_dest:
        return "move_body", dest
    if any(k in text for k in ("放倒", "翻倒", "弄倒", "推倒", "扳倒", "倒放")):
        return "tip_over", dest
    if any(k in text for k in ("开", "打开", "开放")):
        return "open", None
    if any(k in text for k in ("关上", "关门", "关闭")):
        return "close", None
    if any(k in text for k in ("放", "放置", "摆", "搁")):
        return "place", scene_id or ""
    if any(k in text for k in ("凿", "刻", "挖", "撬", "破坏", "砸", "烧", "画")):
        return "other", dest
    return "pick", None


def _extract_dest(text, world_id: str = "test") -> str:
    """从文本提取目的地场景 id（数据驱动：room 实体候选 + grounding 数字对齐打分）。

    替代旧"房间一→room_1"映射表——映射表只对测试世界成立，换世界即失效
    （grounding.resolve_scene：候选来自 environment_entity 的 type=room 行，
    换世界=换数据不改代码）。"""
    from . import grounding
    from . import db as _db
    try:
        rooms = [{"env_id": r[0], "name": r[2] or r[0]}
                 for r in _db.get_environment_entities(None, world_id) if r[3] == "room"]
    except Exception:
        rooms = []
    return grounding.resolve_scene(text, rooms)


def _infer_orientation(text) -> int:
    """从文本推断姿态角：含"放倒/翻倒"等 → 90°（倒放），否则 0°（直立）。"""
    return 90 if any(k in text for k in ("放倒", "翻倒", "弄倒", "推倒", "扳倒", "倒放")) else 0


def _infer_part(text) -> str:
    """推断要拆/要动的部件（disassemble 目标）。测试世界椅子只有腿，默认"腿"。"""
    for w in ("腿", "脚", "扶手", "靠背", "背板"):
        if w in text:
            return w
    return "腿"


def _extract_rel(text) -> list:
    """提取文本里的相对方位词（供执行器 translate rel→position）。"""
    words = []
    for w in ("左", "右", "东", "西", "北", "南", "前", "后", "墙", "角", "边", "旁", "附近"):
        if w in text:
            words.append(w)
    return words


def _guess_target(text: str) -> dict:
    """写入类动作的目标：尽力猜实体/场景。当前轻量版返回 (entity, hint=原文)，
    精确对象对齐交给 resolve()；这里 target.type 开放，resolve 再收口。"""
    return {"type": "entity", "id": "", "hint": text}


def _first_match(text: str, verbs: tuple) -> str:
    """返回文本中第一个命中的动词（作 verb 字段）。"""
    for v in verbs:
        if v in text:
            return v
    return ""


def _llm_classify(text: str, scene_id: str, world_id: str, entity_names: str) -> Intent:
    """LLM 兜底：规则判不出/冲突时调一次，输出结构化 Intent JSON（映射回 Intent 对象）。

    entity_names：当前世界可引用实体/场景清单（人名/物名/房名），喂给 LLM 好让它填 id。
    注意：LLM 只输出"意图结构"，不摸坐标；id 填的是我们在清单里给的业务 id。
    """
    user_prompt = (
        f"玩家当前在场景：{scene_id or '未知'}。\n"
        f"本世界可引用的场景/实体/角色清单：{entity_names}。\n"
        f"玩家输入：{text}\n"
        "请把这条输入解析成意图 JSON。"
    )
    messages = [
        {"role": "system", "content": _INTENT_SYSTEM},
        {"role": "user", "content": user_prompt},
    ]
    try:
        raw = _llm().chat(messages)
    except Exception as e:
        logger.error("意图识别 LLM 兜底失败: %s", e)
        debug_trace.record("intent_llm_fail", user_text=text, error=e)
        # 兜不住就退化为"环境只读观察"：宁可不执行（只读），也不要误执行写入
        return Intent("spatial", "read_only", {"type": "scene", "id": scene_id or "", "hint": text}, "观察")

    return _parse_intent_json(raw, text, scene_id)


def _parse_intent_json(raw: str, text: str, scene_id: str) -> Intent:
    """把 LLM 返回的字符串解析成 Intent；解析失败则安全降级为只读观察。"""
    raw = raw.strip()
    # 去掉可能的 markdown 代码块围栏
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", raw).strip()
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("意图识别 LLM 返回非合法 JSON: %r", raw[:200])
        debug_trace.record("intent_parse_fail", user_text=text, raw=raw,
                           error="意图 LLM 返回非合法 JSON")
        return Intent("spatial", "read_only", {"type": "scene", "id": scene_id or "", "hint": text}, "观察")

    domain = str(data.get("domain", "spatial")).lower()
    if domain not in ("spatial", "dialogue", "self"):
        domain = "spatial"
    side_effect = str(data.get("side_effect", "read_only")).lower()
    if side_effect not in ("read_only", "mutating"):
        side_effect = "read_only"
    target = data.get("target") or {}
    if not isinstance(target, dict):
        target = {}
    spatial_raw = data.get("spatial") or {}
    if not isinstance(spatial_raw, dict):
        spatial_raw = {}
    intent = Intent(domain, side_effect, {
        "type": str(target.get("type", "entity")),
        "id": str(target.get("id", "")),
        "hint": str(target.get("hint", text)),
    }, str(data.get("verb", "")) or "", {
        "op": str(spatial_raw.get("op", "look")),
        "item": str(spatial_raw.get("item", "")),
        "dest_scene": str(spatial_raw.get("dest_scene", "")),
        "position": spatial_raw.get("position"),
        "orientation": spatial_raw.get("orientation"),
        "ref_anchor": str(spatial_raw.get("ref_anchor", "")),
        "rel": list(spatial_raw.get("rel") or []),
        "part": str(spatial_raw.get("part", "")),
    })
    debug_trace.record("intent_parsed", user_text=text, raw=raw, parsed=intent.to_dict())
    return intent


# ---------------------------------------------------------------------------
# 默认落点补全器（你新增需求：玩家只说"放房间1"没说放哪 → 也要给出合适落点）
#   职责 = 语义决策（挑放哪个锚点旁边），坐标由 spatial 算。铁律：LLM 不摸坐标。
#   触发：op 是 place/move_body，且却没给 position/ref_anchor，且 hint 无方位词。
#   流程：程序筛该场景锚点候选 → LLM 挑一个合理锚点 → 填入 intent.spatial.ref_anchor
#         → 执行器用 spatial.near_position(ref_anchor) 算坐标。LLM 不可用则程序兜底。
# ---------------------------------------------------------------------------
_PLACEMENT_SYSTEM = (
    "你是文字冒险游戏的「摆放落点决策器」。玩家要把一件东西放到某个房间，但没说具体位置。"
    "你需要从给出的锚点候选里，挑一个最符合常理、最合适的落点（放在它旁边）。"
    "只输出一个锚点的 env_id，不要任何其他文字、不要 markdown 代码块。\n"
    "候选格式：env_id(名称，锚点标签)\n"
)


def _has_placement_hint(hint: str) -> bool:
    """player 是否已给出"落点线索"（方位词/参照物名）。给了就不触发默认落点补全。"""
    if not hint:
        return False
    for w in ("左", "右", "东", "西", "北", "南", "前", "后", "墙", "角", "旁",
              "边", "附近", "中央", "床", "桌", "门", "窗"):
        if w in hint:
            return True
    return False


def _pick_anchor_by_llm(cands: list, hint: str, dest: str) -> str:
    """让 LLM 从锚点候选里挑一个"放哪个旁边最合理"（只挑语义，不摸坐标）。"""
    lines = [f"{c['env_id']}({c['name']}，{c['anchor_label']})" for c in cands]
    user = (
        f"目标场景：{dest}\n"
        f"锚点候选：{'；'.join(lines)}\n"
        f"玩家原话：{hint or '（没说落点，请给我一个合理的）'}\n"
        "请挑一个落点锚点 env_id。"
    )
    try:
        raw = _llm().chat([
            {"role": "system", "content": _PLACEMENT_SYSTEM},
            {"role": "user", "content": user},
        ]).strip()
        raw = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", raw).strip()
    except Exception as e:
        logger.error("默认落点 LLM 决策失败: %s", e)
        return ""
    for c in cands:
        if c["env_id"] in raw:
            return c["env_id"]
    m = re.search(r"[A-Za-z_][\w]*", raw)
    return m.group(0) if m else ""


def fill_default_placement(intent: Intent, world_id: str = "test") -> Intent:
    """默认落点补全：给"只说了房间没说话给哪"的放置，挑一个合理的锚点落点。

    就地补全 intent.spatial.ref_anchor（不新建对象），下游执行器据此算坐标。
    返回原 Intent。
    """
    sp = intent.spatial or {}
    op = sp.get("op")
    # 只有"会落到具体地点"的操作才需要默认落点（拿取 tip_over 不需要）
    if op not in ("place", "move_body"):
        return intent
    dest = sp.get("dest_scene") or ""
    if not dest:
        return intent
    if sp.get("ref_anchor") or sp.get("position"):
        return intent  # 玩家已明确落点，不走默认
    hint = intent.target.get("hint", "")
    if _has_placement_hint(hint):
        return intent  # 已有方位线索，交给 resolve 传统路径
    try:
        cands = spatial_mod.anchor_candidates(dest, world_id)
    except Exception:
        cands = []
    if not cands:
        sp["ref_anchor"] = ""  # 无锚点：执行器退回场景中央
        return intent
    chosen = _pick_anchor_by_llm(cands, hint, dest)
    if not chosen:
        chosen, _pos = spatial_mod.pick_default_anchor(dest, world_id)
    sp["ref_anchor"] = chosen
    return intent


def classify(text: str, scene_id: str = "", world_id: str = "test", entity_names: str = "") -> Intent:
    """意图识别统一入口：规则优先 + LLM 兜底，返回结构化 Intent。

    Args:
        text: 玩家输入文本。
        scene_id: 玩家当前所在场景（self 型/观察型 target 默认上下文）。
        world_id: 世界维度（供 LLM 兜底提示）。
        entity_names: 本世界可引用实体/场景/角色清单（供 LLM 填 id）——由调用方
            从 environment_entity + character_card 拼好后传入，省略则 LLM 不好填 id。
    Returns:
        Intent 对象。
    """
    if not text or not text.strip():
        return Intent("spatial", "read_only", {"type": "scene", "id": scene_id or "", "hint": ""}, "")

    rule = _rule_classify(text, scene_id, world_id)
    if rule is not None:
        return rule

    return _llm_classify(text, scene_id, world_id, entity_names or text)


def classify_rule_only(text: str, scene_id: str = "", world_id: str = "test") -> Intent or None:
    """只跑规则层、零 LLM 的意图判定。

    用途：对话路径（npc_id 非空）在拼人设前先做一次轻量规则判定——若玩家这句
    **其实是行动**（对场景/NPC 的 mutating 动作，如"用刀偷袭他/搬椅子/开门"），
    即便正处在"与某 NPC 交谈"状态，也应先登记行动意图（随 tick 由场景导演裁决），
    而不只是当作一句台词回给 NPC。零 LLM：命中规则返回 Intent，未命中返回 None
    （交给对话管线正常处理为台词），绝不触发 _llm_classify（避免对话路径双倍成本）。
    """
    if not text or not text.strip():
        return None
    return _rule_classify(text, scene_id, world_id)


# ===========================================================================
# 多意图解析（09-10 用户拍板）：玩家一句话 = 【有序的多步意图】
# ===========================================================================
# 为什么必须做（问题现场）：
#   玩家输入「我拿上椅子去测试房间三追赶这个男人攻击他」，旧实现里含"攻击"→
#   _rule_classify 子串命中就【直接 return 一个 attack Intent】，
#   `_llm_classify` 一次都不会被调用 —— 于是"拿椅子/去房间三"整段被吞掉，
#   玩家看到的是"攻击"导致的空动作，还以为"AI 理解不了我"。
#   真相是：那句话压根没送到 AI 面前，规则层只会返回【一个】意图。
# 解法（分层 + 成本纪律，每层只在前一层不够用时才付代价）：
#   ① 规则分句多意图（零 LLM）：玩家自己写了"我拿起刀，然后攻击他" → 直接拆两条
#   ② 规则单意图（零 LLM）：不跨类别的简单句（"我拿起刀"）→ 规则层直出
#   ③ LLM 多意图（1 次调用，解析模型=非推理）：跨类别的复合句 → 交解析模型
#   ④ 程序校验（零成本 0 延迟 100% 确定）：op 闭集 / id 真实 / 目的地真实 / 类型匹配
#   ⑤ 再解析一次（第 2 次调用）：校验失败 → 把"错在哪 + 候选清单"回喂让模型修正，
#      同时立刻推一条提示给前端（让玩家知道自己在等什么）
#   ⑥ 收口：仍没落地的写进 unresolved 明确回报，绝不静默丢弃

# 空间操作 op 闭集（09-10 补 attack/search）：执行器 environment._exec_mutate 早就实现了
# attack/search，但旧提示词闭集里漏了它们 —— 规则层能出 attack，一旦走 LLM 兜底，
# LLM 不知道有这两个选项，只能把"攻击"编成别的动作。闭集不全 = 闭集失效。
_OP_CLOSED = ("pick", "place", "tip_over", "move_body", "move_self", "look",
              "open", "close", "disassemble", "search", "create", "attack", "other")
# 只作用于【物品】的 op（目标是 NPC 就是语义错配）
_OPS_NEED_ITEM = ("pick", "place", "tip_over", "move_body", "disassemble")
# 只作用于【人】的 op（目标是物品就是语义错配）
_OPS_NEED_NPC = ("attack", "search")
# 必须给出目标场景的 op
_OPS_NEED_DEST = ("move_self", "move_body", "place")

# 多意图分句：玩家自己用标点/关联词断开的地方，就是"多步动作"的天然边界。
# 注意：故意【不】把单字"再"当分隔（"再拿一把刀"是一个动作，切开反而错）。
_SPLIT_RE = re.compile(r"[，,；;。！!？?\n]+|然后|接着|随后|之后|并且|同时|最后|又")

_INTENT_SYSTEM_MULTI = (
    "你是文字冒险游戏的「意图解析器」。玩家的一句话可能包含【多个先后动作】\n"
    "（例：'我拿上椅子去房间三攻击那个男人' = 拿椅子 → 去房间三 → 攻击那个人）。\n"
    "把它解析成【有序】意图数组，只输出一个 JSON 对象，不要任何其他文字、不要 markdown 代码块。\n"
    '格式：{"intents":[{"domain":"spatial|dialogue|self",'
    '"side_effect":"read_only|mutating","verb":"动词",'
    '"target":{"type":"scene|entity|npc|self","id":"清单里的id或空","hint":"玩家原文说法"},'
    '"spatial":{"op":"' + "|".join(_OP_CLOSED) + '","item":"","dest_scene":"","orientation":0,'
    '"ref_anchor":"","rel":[],"part":""}}],"unresolved":["没读懂/没把握的部分"]}\n'
    "铁律：\n"
    "- intents 必须按玩家说话的【先后顺序】排列，一个动作一个对象；"
    "同一个动作不要拆成两条（'搬起来放到墙角'是【一个】place，不是两个）。\n"
    "- spatial.op 只能取上面列出的闭集，不许自创。\n"
    "- target.id / dest_scene 只填【清单里给出】的 id；清单里找不到就留空，"
    "把玩家的原话写进 hint。绝对不许编造清单外的 id。\n"
    "- move_body 只能搬【物品】；人不能用 move_body。"
    "自身移动到某场景用 move_self（target.id 与 dest_scene 都填那个房间 id）。\n"
    "- 『追赶/跟着/追着某人』不要用 move_body（那是搬物品），也不要硬编一个攻击："
    "能判断 TA 在哪个房间就写成 move_self 去那个房间，判断不了就写进 unresolved。\n"
    "- 『追赶』本身【不要】作为一条 op=other 的意图出现（引擎没有'追逐'这个动作）；"
    "op=other 只用于对【环境/物件】的粗糙改写（凿/刻/挖/砸/烧/画），不是对人。\n"
    "- target.type 必须填对：人=npc、物品=entity、房间=scene。"
    "id 填不出可以留空（把原话写进 hint），但 type 不许填错。\n"
    "- 攻击/伤害某人用 attack（target.type=npc）。搜身/搜尸用 search（target.type=npc）。\n"
    "- 读懂但当前可能做不到（对象不在场、路不通）→ 照实解析出来，"
    "可行性由程序判定，你不要替程序判。\n"
    "- 完全没读懂的部分写进 unresolved（一句话说明），不要硬凑成意图。\n"
)

# 再解析一次时用的 user 模板（把"错在哪 + 候选清单"回喂给模型修正）
_INTENT_REPAIR_TEMPLATE = (
    "【上一次解析没通过程序校验】请修正后重新输出完整的意图数组 JSON。\n"
    "上一次你的输出：{last}\n"
    "校验发现的问题：\n{problems}\n"
    "可引用清单（只能用这些 id）：{entities}\n"
    "要求：逐条修掉上面列出的问题；拿不准的那条宁可删掉并写进 unresolved，也不许编造 id。\n"
    "玩家原话：{text}\n"
)

# 是否启用"再解析一次"（用户拍板：允许一次）。关掉即退回"校验不过就明确回报"。
_REPAIR_ENABLED = True


class ParseResult:
    """一次玩家输入的解析产物：有序多意图 + 可见的失败。

    intents    : list[Intent]，有序；可能为空（整句没读懂）
    source     : 解析来源，用于观测（rule / rule_multi / llm_multi /
                 llm_multi_repair / rule_fallback / readonly_fallback / llm_fail）
    notices    : 需要推给前端的即时提示（如"事情比想象中复杂……"）
    unresolved : 读懂但没能落地的部分——【明确回报，不静默丢弃】
    repaired   : 是否触发过"再解析一次"
    raw        : 最后一次 LLM 原始输出（排障用）
    """

    def __init__(self, intents=None, source: str = "", notices=None,
                 unresolved=None, repaired: bool = False, raw: str = ""):
        self.intents = list(intents or [])
        self.source = str(source or "")
        self.notices = list(notices or [])
        self.unresolved = list(unresolved or [])
        self.repaired = bool(repaired)
        self.raw = str(raw or "")

    @property
    def primary(self) -> Intent or None:
        """主意图（第一条）。给只想要"一个意图"的旧调用方用。"""
        return self.intents[0] if self.intents else None

    @property
    def has_mutating(self) -> bool:
        """本句是否含会改变世界的动作（决定走"登记进池"还是"当场执行"）。"""
        return any(i.side_effect == "mutating" for i in self.intents)

    def to_dict(self) -> dict:
        return {"intents": [i.to_dict() for i in self.intents], "source": self.source,
                "notices": self.notices, "unresolved": self.unresolved,
                "repaired": self.repaired}


def _safe_readonly(scene_id: str, text: str) -> Intent:
    """安全兜底意图：宁可不执行（只读观察），也不要误执行写入。"""
    return Intent("spatial", "read_only",
                  {"type": "scene", "id": scene_id or "", "hint": text}, "观察")


def _verb_categories(text: str, world_id: str = "test") -> set:
    """统计文本里命中的【动作类别】（用于判断"这是不是一句复合句"）。

    为什么分类别、不数动词个数：'搬起来放到墙角'命中多个写入词，但它是【一个】动作
    （place），不该当复合句。只有【跨类别】才说明玩家在做两件不同的事。
    """
    from . import world_pack
    cats = set()
    if _contains(text, _ATTACK_VERBS) and not any(g in text for g in _ATTACK_GIVE_BLOCK):
        cats.add("attack")
    if _contains(text, _SEARCH_VERBS) or (
            _contains(text, ("搜",)) and _contains(text, _SEARCH_STRONG + ("尸体", "遗体"))):
        cats.add("search")
    if _contains(text, _CREATE_VERBS):
        cats.add("create")
    if _contains(text, world_pack.lexicon(world_id, "write_verbs")):
        cats.add("write")
    if _contains(text, world_pack.lexicon(world_id, "dialogue_verbs")):
        cats.add("dialogue")
    if _contains(text, world_pack.lexicon(world_id, "position_verbs")):
        cats.add("position")
    if _contains(text, world_pack.lexicon(world_id, "observe_verbs")):
        cats.add("observe")
    return cats


def _needs_multi_parse(text: str, world_id: str = "test") -> bool:
    """是否值得交给 LLM 做多意图解析（成本闸门）。

    规则层只能返回【一个】Intent，所以跨类别的句子必然有一半被吞掉；
    但只在真跨类别时才交 LLM——简单动作仍零成本走规则。
    特例：含写入词时，"位置类别"常只是写入的介词（搬到/放到/搬到房间一），
    去掉它，避免把【单动作】误判成复合句（否则"我把椅子搬到房间一放倒"会白白多调一次）。
    """
    cats = _verb_categories(text, world_id)
    if "write" in cats:
        cats = cats - {"position"}
    return len(cats) >= 2


def _rule_multi(text: str, scene_id: str, world_id: str = "test") -> list:
    """规则分句 → 多意图（零 LLM）。

    只在"玩家自己用了标点/关联词断开"时生效（'我拿起刀，然后攻击他'）。
    要求【每个片段都能被规则层判定】才认这个结果；有片段判不出就整体返回 [] 交 LLM
    ——宁可多花一次调用，也不要"半条多意图"（漏掉一句比慢一点更伤体验）。
    """
    parts = [p.strip() for p in _SPLIT_RE.split(str(text or "")) if p and p.strip()]
    if len(parts) < 2:
        return []
    out = []
    for p in parts:
        if len(p) < 2:
            return []          # 片段太短（分隔词残留）→ 不可信
        it = _rule_classify(p, scene_id, world_id)
        if it is None:
            return []
        out.append(it)
    return out


def _room_candidates(world_id: str = "test") -> list:
    """本世界的房间候选 [{env_id,name}]（数据驱动，换世界不改代码）。"""
    from . import db as _db
    try:
        return [{"env_id": r[0], "name": r[2] or r[0]}
                for r in _db.get_environment_entities(None, world_id) if r[3] == "room"]
    except Exception:  # noqa: BLE001
        return []


def _known_target_ids(world_id: str = "test") -> set:
    """本世界合法 target.id 集合（环境卡 + 空间实体 + NPC）——校验 LLM 有没有编造 id。"""
    from . import db as _db
    ids = set()
    try:
        for row in _db.get_environment_cards(world_id):
            ids.add(str(row[0]))
    except Exception:  # noqa: BLE001
        pass
    try:
        for e in _db.get_environment_entities(None, world_id):
            ids.add(str(e[0]))
    except Exception:  # noqa: BLE001
        pass
    try:
        for n in _db.get_all_npc_ids(world_id):
            ids.add(str(n))
    except Exception:  # noqa: BLE001
        pass
    return ids


def _intent_from_dict(data: dict, text: str, scene_id: str) -> Intent:
    """把一个意图 JSON 对象映射成 Intent（容错：缺字段用安全默认）。"""
    domain = str(data.get("domain", "spatial")).lower()
    if domain not in ("spatial", "dialogue", "self"):
        domain = "spatial"
    side_effect = str(data.get("side_effect", "read_only")).lower()
    if side_effect not in ("read_only", "mutating"):
        side_effect = "read_only"
    target = data.get("target") or {}
    if not isinstance(target, dict):
        target = {}
    spatial_raw = data.get("spatial") or {}
    if not isinstance(spatial_raw, dict):
        spatial_raw = {}
    return Intent(domain, side_effect, {
        "type": str(target.get("type", "entity")),
        "id": str(target.get("id", "")),
        "hint": str(target.get("hint", text)),
    }, str(data.get("verb", "")) or "", {
        "op": str(spatial_raw.get("op", "look")),
        "item": str(spatial_raw.get("item", "")),
        "dest_scene": str(spatial_raw.get("dest_scene", "")),
        "position": spatial_raw.get("position"),
        "orientation": spatial_raw.get("orientation"),
        "ref_anchor": str(spatial_raw.get("ref_anchor", "")),
        "rel": list(spatial_raw.get("rel") or []),
        "part": str(spatial_raw.get("part", "")),
    })


def _parse_intent_list(raw: str, text: str, scene_id: str) -> tuple:
    """把 LLM 返回的多意图 JSON 解析成 (list[Intent], unresolved)。

    容错形态：标准 {"intents":[...]} / 裸数组 [...] / 退化单对象 {...}。
    解析不了就返回空列表——由调用方决定退回规则结果（绝不在这里假装成功）。
    """
    raw = str(raw or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", raw).strip()
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("多意图 LLM 返回非合法 JSON: %r", raw[:200])
        return [], ["解析器返回的不是合法 JSON"]
    unresolved = []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("intents")
        if not isinstance(items, list):
            # 退化成单对象（模型没听懂"数组"要求）
            items = [data] if (data.get("op") or data.get("domain")
                               or data.get("verb")) else []
        unresolved = [str(u) for u in (data.get("unresolved") or []) if str(u).strip()]
    else:
        return [], ["解析器返回的结构不认识"]
    intents = [_intent_from_dict(x, text, scene_id) for x in items if isinstance(x, dict)]
    return intents, unresolved


def _validate_intents(intents: list, world_id: str, scene_id: str) -> tuple:
    """程序校验（零成本、零延迟、100% 确定）：把 LLM 输出的"越界/编造"挑出来。

    这是『开集判定 + 闭集效果』里的【闭集闸门】：LLM 可以自由裁"玩家在做什么"，
    但落到世界上的动作必须是一个合法 op、指向一个真实存在的对象、去一个真实存在的房间。
    校验时会把编造的 id 就地清空（保留 hint，给执行器/兜底留机会）。
    Returns:
        (ok_list, problems)：problems 会原样喂给"再解析一次"，让模型知道错在哪。
    """
    rooms = {str(c["env_id"]) for c in _room_candidates(world_id)}
    known = _known_target_ids(world_id)
    ok, problems = [], []
    for idx, it in enumerate(intents, 1):
        sp = it.spatial or {}
        tgt = it.target or {}
        op = str(sp.get("op") or "")
        dest = str(sp.get("dest_scene") or "")
        tid = str(tgt.get("id") or "")
        ttype = str(tgt.get("type") or "")
        bad = []
        if it.domain not in ("spatial", "dialogue", "self"):
            bad.append("domain 非法(%s)" % it.domain)
        if it.side_effect not in ("read_only", "mutating"):
            bad.append("side_effect 非法(%s)" % it.side_effect)
        if op not in _OP_CLOSED:
            bad.append("op 越界(%s)" % op)
        if dest and rooms and dest not in rooms:
            bad.append("dest_scene 不是本世界真实房间(%s)" % dest)
            sp["dest_scene"] = ""      # 清掉编造的目的地
        if tid and known and tid not in known:
            bad.append("target.id 不在清单里(%s)" % tid)
            tgt["id"] = ""             # 清掉编造的 id，保留 hint 交执行器定位
        if op in _OPS_NEED_NPC and ttype and ttype != "npc":
            bad.append("%s 的目标应当是人，但 type=%s" % (op, ttype))
        if op in _OPS_NEED_ITEM and ttype == "npc":
            bad.append("%s 只能作用于物品，但目标是 NPC" % op)
        # other = "对环境的粗糙改写"（凿/刻/挖/砸/烧/画）。指着人就说明模型把
        # "追赶/招惹/捉弄某人"这类它没有对应动作的话硬塞进来了——宁可让它重解析，
        # 也不要让一个语义不明的写入落到世界状态上（攻击有专门的 attack op）。
        if op == "other" and ttype == "npc":
            bad.append("other 是对环境的改写，不能以人为目标（对人请用 attack）")
        if op == "move_self" and not dest:
            bad.append("move_self 缺 dest_scene")
        if bad:
            problems.append("第%d条（%s）：%s" % (idx, it.verb or op or "?", "；".join(bad)))
            continue
        ok.append(it)
    return ok, problems


def _llm_multi_messages(text: str, scene_id: str, world_id: str, entity_names: str,
                        rule_hint=None, repair: dict = None) -> list:
    """拼多意图解析的 messages（首次 / 再解析共用）。

    只喂"清单与闭集"，【不喂剧情、不喂人设】——解析器看不到剧情，就天然不可能剧透；
    而且 prompt 极短 → 又快又便宜（这是解析层能压到 1~2s 的关键）。
    """
    if repair:
        return [
            {"role": "system", "content": _INTENT_SYSTEM_MULTI},
            {"role": "user", "content": _INTENT_REPAIR_TEMPLATE.format(
                last=str(repair.get("last") or "")[:1500],
                problems="\n".join(repair.get("problems") or []) or "（未指出具体问题）",
                entities=entity_names or "（无清单）",
                text=text)},
        ]
    rooms = "、".join("%s(%s)" % (c["env_id"], c["name"]) for c in _room_candidates(world_id))
    user = ("玩家当前在场景：%s\n" % (scene_id or "未知")
            + "可引用清单：%s\n" % (entity_names or "（无清单）")
            + "可去的房间（dest_scene 只能填这些）：%s\n" % (rooms or "（无）"))
    if rule_hint is not None:
        user += ("规则层初步判定（仅供参考；与你的理解冲突时以你为准）：op=%s、动作=%s、目标=%s\n"
                 % ((rule_hint.spatial or {}).get("op"), rule_hint.verb,
                    (rule_hint.target or {}).get("id") or (rule_hint.target or {}).get("hint")))
    user += "玩家输入：%s\n请把这条输入解析成有序的意图数组 JSON。" % text
    return [
        {"role": "system", "content": _INTENT_SYSTEM_MULTI},
        {"role": "user", "content": user},
    ]


def _llm_parse_multi(text: str, scene_id: str, world_id: str, entity_names: str,
                     rule_hint=None, repair: dict = None) -> ParseResult:
    """调解析模型做多意图解析（模型路由：非推理模型 + 温度 0 + JSON 模式 + 短超时）。

    永远不抛异常：失败返回空 intents 的 ParseResult，由调用方退回规则结果。
    """
    from .llm import parse_chat
    tag = "intent_repair" if repair else "intent_multi"
    msgs = _llm_multi_messages(text, scene_id, world_id, entity_names,
                               rule_hint=rule_hint, repair=repair)
    try:
        raw = parse_chat(msgs, tag=tag)
    except Exception as e:  # noqa: BLE001
        logger.error("多意图 LLM 解析失败(%s): %s", tag, e)
        debug_trace.record("intent_multi_fail", user_text=text, error=e)
        return ParseResult([], "llm_repair_fail" if repair else "llm_fail")
    intents, unresolved = _parse_intent_list(raw, text, scene_id)
    debug_trace.record("intent_multi_parsed", user_text=text, raw=raw,
                       parsed=[i.to_dict() for i in intents], tag=tag)
    return ParseResult(intents, "llm_multi_repair" if repair else "llm_multi",
                       unresolved=unresolved, raw=raw)


def parse_player_input(text: str, scene_id: str = "", world_id: str = "test",
                       entity_names: str = "", on_notice=None) -> ParseResult:
    """【多意图统一入口】玩家一句话 → 有序的多个结构化 Intent（含"再解析一次"闭环）。

    分层见本段开头注释；成本纪律：简单句零 LLM，只有跨类别复合句才付一次解析调用。
    Args:
        on_notice: 可选回调 fn(str)。"再解析一次"被触发时【立即】调用，
                   供上层把提示推给前端（让玩家知道自己在等什么）。
    Returns:
        ParseResult（intents/source/notices/unresolved/repaired/raw）。
    """
    text = str(text or "")
    if not text.strip():
        return ParseResult([_safe_readonly(scene_id, "")], "empty")

    # ① 规则分句多意图（零 LLM）——玩家自己断了句，最可信也最便宜
    seg = _rule_multi(text, scene_id, world_id)
    if len(seg) >= 2:
        return ParseResult(seg, "rule_multi")

    # ② 规则单意图（不跨类别时才信它；跨类别必然要在③交 LLM）
    rule_one = _rule_classify(text, scene_id, world_id)
    if rule_one is not None and not _needs_multi_parse(text, world_id):
        return ParseResult([rule_one], "rule")

    # ③ LLM 多意图解析
    res = _llm_parse_multi(text, scene_id, world_id, entity_names, rule_hint=rule_one)
    if not res.intents:
        # 解析模型没给出任何意图 → 退回规则结果（有就用），实在没有才安全只读
        if rule_one is not None:
            res.intents = [rule_one]
            res.source = "rule_fallback"
        else:
            res.intents = [_safe_readonly(scene_id, text)]
            res.source = "llm_fail_fallback"
            res.unresolved = res.unresolved or ["没能理解这条输入"]
        return res

    # ④ 程序校验（闭集闸门）
    ok, problems = _validate_intents(res.intents, world_id, scene_id)

    # ⑤ 再解析一次（用户拍板：允许一次；触发时立刻给前端反馈）
    notices, repaired = [], False
    if problems and _REPAIR_ENABLED:
        notice = world_pack_notice(world_id)
        notices.append(notice)
        repaired = True
        if callable(on_notice):
            try:
                on_notice(notice)
            except Exception:  # noqa: BLE001  提示推送失败绝不能拖累解析
                logger.warning("解析提示推送失败", exc_info=True)
        res2 = _llm_parse_multi(text, scene_id, world_id, entity_names, rule_hint=rule_one,
                                repair={"last": res.raw, "problems": problems})
        if res2.intents:
            ok2, problems2 = _validate_intents(res2.intents, world_id, scene_id)
            # 只有当"第二次确实更好"才采纳（不比第一次差就行，避免修坏了）
            if len(ok2) >= len(ok):
                ok, problems, res = ok2, problems2, res2

    # ⑥ 收口：校验后一条不剩 → 规则结果兜底 → 再不行安全只读 + 明确回报
    if not ok:
        if rule_one is not None:
            ok = [rule_one]
            res.source += "+rule_fallback"
        else:
            ok = [_safe_readonly(scene_id, text)]
            res.source += "+readonly_fallback"
    res.intents = ok
    res.notices = notices
    res.repaired = repaired
    res.unresolved = list(res.unresolved) + problems
    return res


def world_pack_notice(world_id: str, key: str = "notice_reparse") -> str:
    """取一条"给玩家看的提示"文案（模组 prompts.json 可覆盖；缺省=引擎内置）。"""
    from . import world_pack
    try:
        return world_pack.prompt(world_id, key) or "事情比想象中复杂……"
    except Exception:  # noqa: BLE001
        return "事情比想象中复杂……"
