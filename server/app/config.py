import os                       # 系统工具，用来读环境变量
from dotenv import load_dotenv  # 导入能读 .env 的工具

load_dotenv()                   # ① 读取 .env 文件

LLM_API_KEY = os.getenv("LLM_API_KEY")    # ② 拿出叫这个名字的钥匙，存进变量
LLM_BASE_URL = os.getenv("LLM_BASE_URL")
LLM_MODEL = os.getenv("LLM_MODEL")

# JSON 输出模式（response_format={"type":"json_object"}）：默认关闭。
# 不是所有模型/中转都支持该参数（本项目的 LLM_MODEL 是实验性视觉模型），
# 打开前先实测：server/scripts/probe_llm_json_mode.py 会真调一次看接口是否接受。
LLM_JSON_MODE = os.getenv("LLM_JSON_MODE", "").strip().lower() in ("1", "true", "yes", "on")

# ===== 模型路由（09-10 用户拍板）：一次"用途" = 一个模型 + 一套调用状态 =====
# 为什么要把"解析"和"叙事"分开：这是两个目标相反的任务——
#   叙事（旁白/NPC 台词）要"最聪明、最有文采"，慢一点无所谓；
#   解析（把玩家一句话拆成结构化意图）要"最快、最稳、可重复"，绝不能创作。
# 用同一个模型两件事都做不好，所以按【用途】路由到不同模型（业界叫 model routing）。
# 更深一层：模型"要不要深度思考"不是靠提示词求来的，而是靠【选模型 + API 参数】控制的
# （见 llm._PROVIDER_PARAMS：DeepSeek 靠换模型名，OpenAI o 系靠 reasoning_effort，
#   Qwen/GLM 靠 extra_body 开关）。选对模型才是"不深度思考"的正解。
LLM_PROVIDER = (os.getenv("LLM_PROVIDER") or "deepseek").strip().lower()

# 解析模型：DeepSeek 官方【非推理】模型 deepseek-chat（推理版是 deepseek-reasoner）。
# 语义解析只需要"读懂 + 填槽"，不需要思考链——非推理模型快、稳、便宜，
# 而且不会把预算耗在隐藏推理上（本项目中转的推理模型曾把 token 吃光导致空返回）。
LLM_MODEL_PARSE = (os.getenv("LLM_MODEL_PARSE") or "deepseek-chat").strip()

# 解析调用超时（秒）：比叙事短。解析卡在"玩家按下回车之后"，宁可降级回规则结果，
# 也不让玩家为一个填槽动作干等（叙事客户端超时 30s，解析给 20s）。
LLM_PARSE_TIMEOUT = float(os.getenv("LLM_PARSE_TIMEOUT") or 20)

# 解析采样温度：0 = 尽量确定性。解析是"分类+填槽"不是创作，抖动越小越好复现。
LLM_PARSE_TEMPERATURE = float(os.getenv("LLM_PARSE_TEMPERATURE") or 0)

# 解析 JSON 模式：deepseek-chat 官方支持 response_format={"type":"json_object"}，默认开。
# 若中转不支持，llm.chat 会关掉它重试一次并【永久降级】（只踩一次坑，不反复失败）。
LLM_JSON_MODE_PARSE = (os.getenv("LLM_JSON_MODE_PARSE") or "1").strip().lower() in (
    "1", "true", "yes", "on")

# ===== MySQL（长期记忆存储）=====
MYSQL_HOST = os.getenv("MYSQL_HOST")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", 3306))  # int()：pymysql 要的是数字端口，不是字符串
MYSQL_USER = os.getenv("MYSQL_USER")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD")
MYSQL_DB = os.getenv("MYSQL_DB")

# ===== Redis（缓存 / 会话记忆）=====
REDIS_HOST = os.getenv("REDIS_HOST")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
REDIS_DB = int(os.getenv("REDIS_DB", 0))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD")  # 本地无密码则为空串，连接时转 None
