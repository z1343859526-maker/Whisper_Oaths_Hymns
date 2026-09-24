"""调试追踪：记录 /chat 关键链路（发给 AI 的话 / AI 返回 / 解析结果 / 失败原因），
供 GET /debug/trace 实时查看。保障「可观测性 / 工程化」。

背景：此前前端在后端失败/超时/解析失败时，一律回退统一兜底话
（"……此人沉默地打量着你。"），用户无从判断是哪一步失败。本模块把每次 LLM 调用
（输入/输出/耗时/异常）与关键解析（意图 JSON 解析等）记进内存环形缓冲，最近 N 条可查。

设计：
- 内存 ring（deque maxlen=200）+ 线程锁，够调试用，不做持久化；
  「回放/审计」需求出现时再落 SQLite。
- record() 全程 try/except 包裹：记录动作绝不能带崩主链路。
- 每条同时用 logger.info 打一行摘要到后端控制台（uvicorn --reload 窗口），
  满足"实时看到"；完整 sent/raw 存 ring 供 /debug/trace 查询。
"""
import logging
import threading
import time
from collections import deque

logger = logging.getLogger(__name__)

_RING: "deque[dict]" = deque(maxlen=200)
_LOCK = threading.Lock()


def record(stage: str, *, session_id: str = "", npc_id: str = "", user_text: str = "",
           sent=None, raw=None, parsed=None, error=None, ms: float = None,
           **extra) -> None:
    """追加一条链路记录。sent/raw/parsed/error 均可空，按 stage 需要传。

    stage 取值：
      llm           LLM 对话调用（发给 AI 的话 sent + 返回 raw + 耗时 ms）
      llm_stream    LLM 流式调用（同上）
      intent_parsed 意图 JSON 解析成功（parsed=意图结构）
      intent_parse_fail 意图 JSON 解析失败（error=原因）
      intent_llm_fail  意图识别 LLM 调用失败（error=原因）
      intent_multi_parsed 多意图解析（parsed=有序意图数组，extra.tag 区分首次/修复轮）
      player_notice 推给玩家的即时提示（raw=文案）
    extra：可选的附加观测字段（如 model=本次用的模型名、tag=调用用途）。
           09-10 新增——模型路由之后"这次用了哪个模型"必须可观测，
           否则线上看不出"解析走的是不是非推理小模型"。
    """
    try:
        entry = {
            "ts": time.strftime("%H:%M:%S"),
            "stage": stage,
            "session_id": session_id,
            "npc_id": npc_id,
            "user_text": (user_text or "")[:200],
            "sent": _clip(sent),
            "raw": _clip(raw),
            "parsed": _clip(parsed),
            "error": (error[:400] if isinstance(error, str) else str(error)[:400]) if error else "",
            "ms": round(ms, 1) if ms is not None else None,
        }
        for k, v in (extra or {}).items():
            entry[k] = _clip(v) if isinstance(v, (list, dict)) else v
        with _LOCK:
            _RING.append(entry)
        logger.info("[trace] %(stage)s npc=%(npc_id)s in=%(user_text)s ms=%(ms)s err=%(error)s "
                    "raw=%(raw_raw)s",
                    {"stage": stage, "npc_id": npc_id, "user_text": user_text[:40],
                     "ms": entry["ms"], "error": entry["error"][:120],
                     "raw_raw": _clip(raw, 80)})
    except Exception:  # noqa: BLE001  记录失败不能影响主链路
        pass


def _clip(v, n: int = 600) -> str:
    """把发给 AI 的 messages / AI 返回 / 解析对象压成可读单行字符串（截断）。"""
    try:
        if v is None:
            return ""
        if isinstance(v, (list, dict)):
            import json
            s = json.dumps(v, ensure_ascii=False)
        else:
            s = str(v)
        return " ".join(s.split())[:n]
    except Exception:  # noqa: BLE001
        return (str(v)[:n] if v else "")


def recent(limit: int = 50) -> list:
    """取最近 limit 条记录（倒序：最新在前）。"""
    with _LOCK:
        items = list(_RING)
    return list(reversed(items[-limit:]))
