-- =====================================================================
-- 006_mental_model.sql
-- 目标：心智引擎重构（v0.2）的数据层地基——
--   ① character_card 新增 mental_model JSON 列：结构化承载全部心智参数
--     （kernel / perception / emotion / planning / expression / depth 六块），
--     替代散落的旧 cognitive + personality_traits（旧列保留为迁移源，新代码不读）；
--   ② 新建 npc_mental_state 表：运行时心智热态（情绪 PAD 向量 / 主情绪词 / 信念 /
--     察觉事项 / 工作记忆 / 上一帧观测），按 session 隔离，纯 code 读写——
--     心理引擎铁律"LLM 不持有数值"，这些数值永远只以"状态词"进 prompt。
--
-- 背景/为什么改（心智引擎重构文档 §1.2 P2/P3 核实）：
--   character_card 已有 cognitive/personality_traits/thinking_chain 列且黄金乡
--   isabella 已灌值（seed.sql），但 db.get_character_card 只 SELECT 8 列，
--   全部代码零引用这些列——"心智数据存在但完全没被消费"。
--   且旧参数分层混乱：sensitivity 与 neuroticism 重复、rational_ratio 跨子系统、
--   impulse_threshold 语义倒置。重构后统一进 mental_model（参数含义见 character_card 各列注释）。
--
--   npc_mental_state 解决"运行时无情绪存储"：此前情绪/信念/工作记忆在运行时
--   没有任何落点（beliefs 表无 DAO、Redis 未接入、无内存态）。MVP 不引入 Redis
--   （重构文档 §10.3 决策），本表 + 进程内缓存即可；UNIQUE(session,npc) 保证
--   每 NPC 每会话只有一份热态（§3.2 全局统一状态，不各写各的副本）。
--
-- 与既有迁移的关联：001=会话隔离、002/003/004=三维 world_id 隔离。
-- 本迁移不碰任何旧列/旧表结构（红线：不推翻表，只新增列/表）。
--
-- 执行（在 server/ 目录）：python scripts/run_migration.py sql/migrations/006_mental_model.sql
-- =====================================================================

-- ---------------------------------------------------------------------
-- ① character_card.mental_model：心智参数的唯一入口（新角色一律灌这里）
--    结构契约（六块，缺块时引擎按默认值兜底）：
--    {
--      "kernel":     {"identity","long_term_motivation","value_priority[]",
--                     "hard_limits[]","core_beliefs[]","motive_profile{achievement,affiliation,power}"},
--      "perception": {"attentiveness","suspicion","min_acceptance","priors[]"},
--      "emotion":    {"emotional_reactivity","rational_bias","appraisal_style","decay_rate"},
--      "planning":   {"planning_depth","tenacity","flexibility","self_control"},
--      "expression": {"impulsivity","composure","sociability","verbal_style"},
--      "depth":      {"perception","emotion","planning","expression"}
--    }
--    尺度：特质/倾向 0~100；系数 0~1。参数手册：构造指南 P 章。
-- ---------------------------------------------------------------------
ALTER TABLE character_card
ADD COLUMN mental_model JSON DEFAULT NULL COMMENT '心智模型(v2 主参数结构)：kernel/perception/emotion/planning/expression/depth 六块，替代旧 cognitive+personality_traits（旧列废弃为迁移源，构造指南 P 章）' AFTER thinking_chain;

-- ---------------------------------------------------------------------
-- ② npc_mental_state：运行时心智热态（每 NPC 每会话一份）
--    与 character_card.mental_model 的分工：那边是"这个人生来什么样"（冷/静态），
--    这边是"这个人此刻心里怎么样"（热/动态）。
--    纯 code 读写；LLM 只通过"状态词映射"（mental.py 词映射函数）见到它的影子。
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS npc_mental_state (
    id               BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY COMMENT '自增主键',
    session_id       VARCHAR(64)  NOT NULL COMMENT '会话/轮回标识（与 npc_memory/relationships 同一隔离维度）',
    npc_id           VARCHAR(64)  NOT NULL COMMENT 'NPC 业务标识',
    emotion          JSON         DEFAULT NULL COMMENT 'PAD 情绪向量 {"valence":-1~1,"arousal":0~1,"dominance":0~1}（Russell 1980 / Mehrabian）',
    emotion_word     VARCHAR(32)  NOT NULL DEFAULT '' COMMENT '主情绪词（词映射产物，进 prompt L4 块，数字不进 prompt）',
    emotion_intensity DECIMAL(4,3) DEFAULT NULL COMMENT '情绪强度 0~1（词档位：微/中/强）',
    beliefs          JSON         DEFAULT NULL COMMENT '信念列表 [{"topic","confidence":0~100,"state":"firm/doubt/shaken/betrayed","is_core":bool}]',
    noticed          JSON         DEFAULT NULL COMMENT '最近察觉事项（detect_change 输出，prompt L3 块）',
    working_memory   JSON         DEFAULT NULL COMMENT '工作记忆（文字条目列表，会话内滚动）',
    last_observation JSON         DEFAULT NULL COMMENT '上一帧观测快照（detect_change 的先验来源之一，§10.3 债务项）',
    updated_tick     INT          NOT NULL DEFAULT 0 COMMENT '最近更新的游戏 tick（0=开局未更新）',
    created_at       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    UNIQUE KEY uk_session_npc (session_id, npc_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='运行时心智热态：情绪/信念/工作记忆（纯 code 读写，LLM 只见状态词，按会话隔离）';
