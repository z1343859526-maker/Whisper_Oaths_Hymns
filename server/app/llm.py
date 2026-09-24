"""模型适配层：定义统一的 LLM 客户端接口与 DeepSeek 实现。

调用方只依赖 LLMClient 抽象基类；换模型时新增子类即可，
无需改动业务代码 —— 面向接口编程 + 依赖倒置。

并行调用（世界时序 v2）：同一 API key 天然支持多路并发请求（服务端按请求计费，
不按连接）。chat_many 用线程池把同 tick 多个 NPC 的决策调用并发出去——
LLM 延迟是 IO 等待，线程池即可吃满，不需要 asyncio 改造全链路。
OpenAI 客户端线程安全（每次调用独立 HTTP 请求），模块级单例可被多线程共享。
"""
import time
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Iterator

from openai import OpenAI

from .config import (LLM_API_KEY, LLM_BASE_URL, LLM_MODEL,
                     LLM_MODEL_PARSE, LLM_PROVIDER, LLM_PARSE_TIMEOUT,
                     LLM_PARSE_TEMPERATURE, LLM_JSON_MODE_PARSE)
from . import debug_trace

# 并发上限：DeepSeek 免费/低档 key 有 RPM 限制，默认 4 路并发是保守安全值；
# 提升配额后调大即可（环境变量化留给 M5 成本治理）。

# ---------------------------------------------------------------------------
# 模型路由（09-10 用户拍板）：让"调用哪个模型、用什么状态调"变成显式参数
# ---------------------------------------------------------------------------
# 「不深度思考」这件事：提示词只能"请求"（软约束，模型可以不听），
# 真正的开关在【模型选择 + API 参数】上，且各家参数名不同——下表把它收敛成一处。
# 用哪个由 LLM_PROVIDER 决定；thinking_off=True 时合并进请求 kwargs。
_PROVIDER_PARAMS = {
    # DeepSeek：没有参数开关，"换模型名"就是开关（deepseek-chat 非推理 / deepseek-reasoner 推理）
    "deepseek": {},
    # OpenAI o 系 / GPT-5：用推理档位降级
    "openai": {"reasoning_effort": "minimal"},
    # 通义千问（DashScope 兼容模式）
    "qwen": {"extra_body": {"enable_thinking": False}},
    "dashscope": {"extra_body": {"enable_thinking": False}},
    # 智谱 GLM
    "zhipu": {"extra_body": {"thinking": {"type": "disabled"}}},
    "glm": {"extra_body": {"thinking": {"type": "disabled"}}},
}

# JSON 模式是否被当前端点接受：默认信配置，一旦实测报错就永久降级（只踩一次坑）。
_json_mode_supported = True

# 客户端实例缓存：key=(模型, 超时)。OpenAI 客户端线程安全（每次调用独立 HTTP 请求），
# 可按模型复用实例——避免每次解析都新建客户端（连接复用的意义在这里）。
_CLIENTS: dict = {}


def get_client(model: str = "", timeout: float = 30.0) -> "DeepSeekClient":
    """按 (模型, 超时) 取客户端（模型路由的落地点：不同用途→不同模型）。

    为什么要按模型缓存而不是共用一个：模型名和超时都是**客户端级**参数，
    解析要"快模型 + 短超时"，叙事要"主模型 + 长超时"，一个客户端表达不了。
    """
    key = (str(model or LLM_MODEL), float(timeout))
    client = _CLIENTS.get(key)
    if client is None:
        client = DeepSeekClient(model=key[0], timeout=key[1])
        _CLIENTS[key] = client
    return client


def _is_json_mode_unsupported(err: Exception) -> bool:
    """判断异常是不是"这个端点不认 response_format"。"""
    msg = str(err).lower()
    return "response_format" in msg or "json_object" in msg


class LLMClient(ABC):
    """所有模型客户端的统一接口（抽象基类）。"""

    @abstractmethod
    def chat(self, messages: list[dict], max_tokens: int = None,
             json_mode: bool = False, tag: str = "",
             model: str = None, temperature: float = None,
             thinking_off: bool = False, provider: str = None) -> str:
        """对话接口：传入消息列表，返回模型回复文本。

        Args:
            messages: OpenAI 消息格式列表，如 [{"role": "user", "content": "你好"}]。
            max_tokens: 可选输出上限（token 数）。设了会强制早停——缩短生成时间、降成本；
                不设（None）则走模型默认。用于"一两句话"这类短输出任务（如场景旁白提速）。
                ⚠️ 推理型模型上慎用：预算会被隐藏推理吃光导致 content 为空（本项目实测）。
            json_mode: True 则带 response_format={"type":"json_object"}，由服务端保证
                返回合法 JSON。要求提示词里出现 "json" 字样（DeepSeek 的硬性要求）。
                默认关闭：并非所有模型/中转都支持该参数；不支持时会自动降级重试一次。
            tag: 观测标签（如 "test_man@t12"），写进 /debug/trace——一个 tick 内多路
                NPC 并发决策时，用于区分哪条记录属于谁。
            model: 本次调用覆盖的模型名（模型路由：不传=用客户端默认模型）。
            temperature: 采样温度（不传=模型默认）。解析类任务应传 0（要确定性，不要创作）。
            thinking_off: True=显式要求"不要深度思考"，按 provider 合并对应参数
                （见 _PROVIDER_PARAMS）。
            provider: 提供方（不传=LLM_PROVIDER），决定 thinking_off 用哪套参数名。
        Returns:
            模型回复的文本内容。
        """
        raise NotImplementedError

    @abstractmethod
    def chat_stream(self, messages: list[dict]) -> Iterator[str]:
        """流式对话接口：返回一个生成器，逐段产出模型回复文本。

        Args:
            messages: OpenAI 消息格式列表。
        Yields:
            模型回复的一段文本（每次 yield 一小段，前端逐字显示）。
        """
        raise NotImplementedError

class DeepSeekClient(LLMClient):
    """DeepSeek 模型客户端实现（OpenAI 兼容）。"""

    def __init__(self, model: str = None, timeout: float = 30.0) -> None:
        # timeout=30.0：超过 30 秒未响应就抛异常，避免请求无限期挂起
        self.client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL, timeout=timeout)
        self.model = model or LLM_MODEL
        self.timeout = timeout

    def chat(self, messages: list[dict], max_tokens: int = None,
             json_mode: bool = False, tag: str = "",
             model: str = None, temperature: float = None,
             thinking_off: bool = False, provider: str = None) -> str:
        """调用 DeepSeek 对话接口并返回回复文本。

        max_tokens: 可选输出上限；为 None 时不传（模型默认）。传了会在 create 里带上，
        强制模型在此 token 内结束（配合"一两句话"类短输出任务提速）。
        json_mode: 带 response_format={"type":"json_object"}——由服务端保证返回合法 JSON。
            提示词里必须出现 "json" 字样，否则接口直接报错（DeepSeek 硬性要求）。
        tag: 观测标签，写进 /debug/trace 用于区分并发调用（见 LLMClient.chat）。
        model: 覆盖本次调用的模型（模型路由）；不传用 self.model。
        temperature: 采样温度；不传=模型默认（解析应显式传 0）。
        thinking_off: 显式关掉/降级"深度思考"，按 provider 合并参数（见 _PROVIDER_PARAMS）。
        provider: 提供方；不传=LLM_PROVIDER。
        """
        global _json_mode_supported
        use_model = str(model or self.model)
        t0 = time.perf_counter()
        try:
            kwargs = {"model": use_model, "messages": messages}
            if max_tokens is not None:
                kwargs["max_tokens"] = max_tokens
            if temperature is not None:
                kwargs["temperature"] = temperature
            if thinking_off:
                kwargs.update(_PROVIDER_PARAMS.get(str(provider or LLM_PROVIDER), {}))
            if json_mode and _json_mode_supported:
                kwargs["response_format"] = {"type": "json_object"}
            try:
                response = self.client.chat.completions.create(**kwargs)
            except Exception as e:  # noqa: BLE001
                # 端点不认 response_format → 关掉它重试一次，并【永久降级】。
                # 这样"开了 JSON 模式但中转不支持"只会失败一次，之后自动走普通模式，
                # 不靠人工改配置（工程上：可降级、可观测、不反复踩同一个坑）。
                if "response_format" in kwargs and _is_json_mode_unsupported(e):
                    _json_mode_supported = False
                    kwargs.pop("response_format", None)
                    debug_trace.record("llm_json_mode_off", raw="", error=e, npc_id=tag)
                    response = self.client.chat.completions.create(**kwargs)
                else:
                    raise
            choice = response.choices[0]
            reply = choice.message.content
            ms = (time.perf_counter() - t0) * 1000
            debug_trace.record("llm", sent=messages, raw=reply, ms=ms, npc_id=tag,
                               model=use_model)
            # finish_reason=="length" = 被 token 上限截断 → JSON 必然不闭合 → 解析必然失败。
            # 单独立一条：否则现场只能看到"兜底 wait + 无思考"，与其它解析失败混为一谈
            # （09-10 排障教训）。
            if getattr(choice, "finish_reason", "") == "length":
                debug_trace.record("llm_truncated", raw=reply, ms=ms, npc_id=tag,
                                   error="输出被 max_tokens 截断（finish_reason=length）")
            return reply
        except Exception as e:  # noqa: BLE001
            debug_trace.record("llm", sent=messages, raw="", error=e, npc_id=tag,
                               model=use_model, ms=(time.perf_counter() - t0) * 1000)
            raise

    def chat_many(self, messages_list: list, max_workers: int = 4) -> list:
        """并行调用多个对话（同 key 多路并发），返回与输入等长且同序的结果。

        Args:
            messages_list: [messages, ...]——每个元素是一次调用的消息列表。
            max_workers: 线程池并发上限（默认 4，保守适配 RPM 限制）。
        Returns:
            list[(ok: bool, reply_or_error: str)]，顺序与输入一致——
            失败不抛异常（单路失败不影响同 tick 其他 NPC 的决策），错误以 ok=False 给出。
        为什么线程池而不是 asyncio：调用方（agent.decide / simulate 主循环）是同步
        代码与 pymysql 同步 IO 混合，asyncio 化要动全链路；LLM 等待是 IO 密集，
        线程池即可并行吃满，改动面最小。
        """
        results = [None] * len(messages_list)

        def _one(i):
            try:
                return i, True, self.chat(messages_list[i])
            except Exception as e:  # noqa: BLE001
                return i, False, f"{type(e).__name__}: {e}"

        with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
            futures = [pool.submit(_one, i) for i in range(len(messages_list))]
            for fut in as_completed(futures):
                i, ok, payload = fut.result()
                results[i] = (ok, payload)
        return results

    def chat_stream(self, messages: list[dict]) -> Iterator[str]:
        """调用 DeepSeek 流式接口，逐段 yield 回复文本。"""
        t0 = time.perf_counter()
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                stream=True,
            )
            full = []
            for chunk in response:  # 循环这个流，每次拿到一小段
                # 流式下，新增文字在 delta.content 里（非流式才是 message.content）
                delta = chunk.choices[0].delta
                if delta.content:  # 跳过空片段（第一个 chunk 常常没有文字）
                    full.append(delta.content)
                    yield delta.content
            debug_trace.record("llm_stream", sent=messages, raw="".join(full),
                               ms=(time.perf_counter() - t0) * 1000)
        except Exception as e:  # noqa: BLE001
            debug_trace.record("llm_stream", sent=messages, raw="", error=e,
                               ms=(time.perf_counter() - t0) * 1000)
            raise


# ---------------------------------------------------------------------------
# 用途入口：解析调用（模型路由的"客户侧"）
# ---------------------------------------------------------------------------
def parse_chat(messages: list[dict], tag: str = "intent_parse") -> str:
    """【解析用途】的统一调用入口——把"读懂玩家一句话"这件事的调用状态收敛在一处。

    与叙事调用的全部差异都在这里，调用方不必关心：
      · 模型：LLM_MODEL_PARSE（DeepSeek 官方非推理模型 deepseek-chat）
      · 温度：LLM_PARSE_TEMPERATURE（默认 0，要确定性不要创作）
      · JSON 模式：开（服务端保证合法 JSON，少一次解析失败）
      · 不思考：thinking_off=True（按 provider 落成各家参数）
      · 超时：LLM_PARSE_TIMEOUT（默认 20s，比叙事的 30s 短）
      · 降级：解析模型不可用（Key 无权限/模型名错）→ 退回主模型再试一次；
             仍失败则抛异常，由调用方降级到规则层结果（绝不静默）。
    """
    messages = messages or []
    client = get_client(LLM_MODEL_PARSE, LLM_PARSE_TIMEOUT)
    try:
        return client.chat(messages, tag=tag, temperature=LLM_PARSE_TEMPERATURE,
                           json_mode=LLM_JSON_MODE_PARSE, thinking_off=True)
    except Exception as e:  # noqa: BLE001
        # 解析模型不可用（如 Key 没开权限/模型名下架）→ 退回主模型再试一次。
        debug_trace.record("llm_parse_fallback_model", raw="", error=e, npc_id=tag,
                           model=LLM_MODEL_PARSE)
        main_client = get_client(LLM_MODEL, 30.0)
        return main_client.chat(messages, tag=tag, temperature=LLM_PARSE_TEMPERATURE,
                                json_mode=LLM_JSON_MODE_PARSE, thinking_off=True)