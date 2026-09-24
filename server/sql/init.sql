-- =====================================================================
-- 《黄金乡谋杀案》数据库初始化脚本  init.sql
-- 库名：golden_murder    引擎：InnoDB    字符集：utf8mb4
-- 表：character_card / world_knowledge / dialogue_log / npc_memory / game_state / relationships
--     secrets / beliefs / npc_inventory / goals / schedule / action_log（P4 心智模型扩展）
--     environment_card / world_trace（P4 涌现引擎：环境卡 + 世界痕迹）
-- 执行：在 server/ 目录下  mysql -uroot -p  进入后  source sql/init.sql;
-- =====================================================================

CREATE DATABASE IF NOT EXISTS golden_murder
    DEFAULT CHARACTER SET utf8mb4
    COLLATE utf8mb4_unicode_ci;

USE golden_murder;

-- 先删旧表（幂等：脚本可重复执行；当前表为空，无数据损失）
DROP TABLE IF EXISTS environment_entity;
DROP TABLE IF EXISTS world_trace;
DROP TABLE IF EXISTS environment_card;
DROP TABLE IF EXISTS world;
DROP TABLE IF EXISTS action_log;
DROP TABLE IF EXISTS plans;
DROP TABLE IF EXISTS schedule;
DROP TABLE IF EXISTS goals;
DROP TABLE IF EXISTS npc_inventory;
DROP TABLE IF EXISTS beliefs;
DROP TABLE IF EXISTS secrets;
DROP TABLE IF EXISTS relationships;
DROP TABLE IF EXISTS game_state;
DROP TABLE IF EXISTS npc_memory;
DROP TABLE IF EXISTS dialogue_log;
DROP TABLE IF EXISTS world_knowledge;
DROP TABLE IF EXISTS character_card;

-- ---------------------------------------------------------------------
-- 0) world 世界清单：所有世界的一等公民（M1.5 泛化，004 迁移后随 init 建成）
--    世界名/描述从 DB 读，代码不硬编码 _WORLD_NAMES；加世界观 = 插一行 + 建 seed 文件。
--    顶层维度：角色(character_card.world_id)/知识(world_knowledge.world_id)/环境(environment_card.world_id)
--    都挂在某个 world 下，世界齐全才谈"按世界隔离"。
-- ---------------------------------------------------------------------
CREATE TABLE world (
    id          INT UNSIGNED AUTO_INCREMENT PRIMARY KEY COMMENT '自增主键',
    world_id    VARCHAR(32)  NOT NULL UNIQUE COMMENT '世界标识：golden=黄金乡 / test=测试世界',
    world_name  VARCHAR(64)  NOT NULL COMMENT '世界显示名（替代代码硬编码的 _WORLD_NAMES）',
    description TEXT COMMENT '世界一句话描述',
    is_active   TINYINT      NOT NULL DEFAULT 1 COMMENT '是否启用 1=启用 0=停用',
    created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='世界清单：世界元数据（RAG/角色/环境隔离的顶层维度）';

-- ---------------------------------------------------------------------
-- 1) character_card 角色卡：NPC 是谁（人设 + 禁区，防 OOC 第一道防线）
-- ---------------------------------------------------------------------
CREATE TABLE character_card (
    id          INT UNSIGNED  AUTO_INCREMENT PRIMARY KEY COMMENT '自增主键',
    npc_id      VARCHAR(64)   NOT NULL COMMENT 'NPC 业务标识，如 prince_adrian',
    world_id    VARCHAR(32)   NOT NULL DEFAULT 'golden' COMMENT '所属世界：golden=黄金乡 / test=测试世界（003 迁移：角色按世界隔离）',
    name        VARCHAR(64)   NOT NULL        COMMENT '显示名',
    title       VARCHAR(128)  NOT NULL DEFAULT '' COMMENT '头衔/身份',
    personality TEXT                          COMMENT '性格特点',
    background  TEXT                          COMMENT '背景故事',
    motivation  TEXT                          COMMENT '核心动机',
    forbidden   TEXT                          COMMENT '禁区：不能说破的秘密（JSON 数组）',
    knowledge_scope JSON                      COMMENT '知识边界：{"knows":[...],"does_not_know":[...]}',
    initial_scene VARCHAR(64)                  COMMENT '出生地/开局所在场景 id（008 迁移；NULL=未设定，沿用按计划第一步定位的旧行为）',
    appearance   JSON                         COMMENT '外貌(静态)：含 tells 非言语破绽(§14)',
    outfits      JSON                         COMMENT '着装(半静态)：occasion 场合 + state 状态(§14)',
    personality_traits JSON                   COMMENT 'OCEAN 大五人格 0~100(底层驱动参数)',
    cognitive    JSON                         COMMENT '认知参数：suspicion/rational_ratio/perception/composure/sensitivity',
    speech_style TEXT                         COMMENT '说话风格(纯生成字段，无判断公式)',
    example_dialogue JSON                     COMMENT '示范台词(锁定台词风格)',
    thinking_chain JSON                       COMMENT '思维链配置：步骤序列 + executor + params(§10)（废弃中，见 mental_model）',
    mental_model JSON                          COMMENT '心智模型(v2 主参数结构)：kernel/perception/emotion/planning/expression/depth 六块，替代旧 cognitive+personality_traits（旧列废弃为迁移源，构造指南 P 章）（006 迁移）',
    cooperation_profile JSON                  COMMENT '合作画像：obedience/ladder_ceiling/hard_limits/unlock_conditions(§15)',
    is_active   TINYINT       NOT NULL DEFAULT 1 COMMENT '是否启用 1=启用 0=停用',
    created_at  DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at  DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='角色卡：NPC 人设与禁区';

-- ---------------------------------------------------------------------
-- 2) world_knowledge 世界观知识库：世界长啥样（先验层，RAG 检索源）
--    npc_id='global'  = 全局公共世界观；npc_id=具体NPC(如 isabella) = 该 NPC 的先验(§2/§12/§17)
--    一份原文、两处索引：source of truth 在版本化 YAML，本表同步结构化字段，再向量化进 RAG
-- ---------------------------------------------------------------------
CREATE TABLE world_knowledge (
    id         INT UNSIGNED AUTO_INCREMENT PRIMARY KEY COMMENT '自增主键',
    world_id   VARCHAR(32)  NOT NULL DEFAULT 'golden' COMMENT '所属世界：golden=黄金乡 / test=测试世界（不同世界不同 RAG 库）',
    npc_id     VARCHAR(64)  NOT NULL DEFAULT 'global' COMMENT '归属：global=全局公共世界观，或某 NPC 的 npc_id（如 isabella）',
    category   VARCHAR(32)  NOT NULL COMMENT '类别：character/location/event/rule/rumor',
    title      VARCHAR(255) NOT NULL COMMENT '条目标题',
    content    TEXT         NOT NULL COMMENT '知识正文',
    tags       VARCHAR(255) NOT NULL DEFAULT '' COMMENT '标签，逗号分隔',
    access_level  VARCHAR(16) NOT NULL DEFAULT 'public' COMMENT '权限：public/faction/secret（谁有权知道）',
    spoiler_level TINYINT     NOT NULL DEFAULT 0 COMMENT '剧透等级：0=无剧透，1~3 逐步剧透',
    embedding  JSON         COMMENT '向量（P4 用 numpy 生成后写入，供余弦相似度比对）',
    created_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='世界观知识库：RAG 检索源';

-- ---------------------------------------------------------------------
-- 3) dialogue_log 对话日志：大家说过什么（流水，可观测 + 评测语料）
-- ---------------------------------------------------------------------
CREATE TABLE dialogue_log (
    id         BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY COMMENT '自增主键（量大用 BIGINT）',
    session_id VARCHAR(64)  NOT NULL COMMENT '一轮会话标识',
    npc_id     VARCHAR(64)  NOT NULL COMMENT '对话的 NPC',
    speaker    VARCHAR(16)  NOT NULL COMMENT '说话者：player/npc/system',
    content    TEXT         NOT NULL COMMENT '对话内容',
    ooc_score  DECIMAL(3,2) COMMENT 'OOC 一致性评分（P5 写入）',
    created_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '发生时间',
    KEY idx_npc (npc_id),
    KEY idx_session (session_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='对话日志：逐条流水';

-- ---------------------------------------------------------------------
-- 4) npc_memory NPC 长期记忆：这个 NPC 记住什么（单轮时间线演化）
-- ---------------------------------------------------------------------
CREATE TABLE npc_memory (
    id             BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY COMMENT '自增主键',
    npc_id         VARCHAR(64)  NOT NULL COMMENT '记忆归属的 NPC',
    session_id     VARCHAR(64)  NOT NULL DEFAULT 'seed' COMMENT '归属会话：seed=先验记忆（轮回保留），否则=运行时记忆（会话隔离）',
    memory_type    VARCHAR(32)  NOT NULL COMMENT '类型：event/impression/relation',
    content        TEXT         NOT NULL COMMENT '记忆内容',
    importance     TINYINT      NOT NULL DEFAULT 0 COMMENT '重要性 0~100，召回排序权重',
    related_entity VARCHAR(64)  NOT NULL DEFAULT '' COMMENT '关联实体 id（人/地/物）',
    summary        VARCHAR(255) NOT NULL DEFAULT '' COMMENT '一句话摘要（prompt 先放摘要，命中才放原文，省 token）',
    embedding      JSON         COMMENT '记忆向量（P4 用 numpy 生成，低于 importance 阈值留空）',
    parent_id      BIGINT UNSIGNED DEFAULT NULL COMMENT '父记忆 id（自关联，2.4 用）',
    created_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '产生时间',
    KEY idx_npc_time (npc_id, created_at),
    KEY idx_npc_session (npc_id, session_id),
    KEY idx_parent (parent_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='NPC 长期记忆：单轮时间线演化（M1.1 起按会话隔离，先验=seed）';

-- ---------------------------------------------------------------------
-- 5) game_state 游戏状态：世界现在什么状态（KV 式快照，按会话隔离）
-- ---------------------------------------------------------------------
CREATE TABLE game_state (
    id          INT UNSIGNED AUTO_INCREMENT PRIMARY KEY COMMENT '自增主键',
    session_id  VARCHAR(64) NOT NULL COMMENT '会话/轮回标识',
    state_key   VARCHAR(64) NOT NULL COMMENT '状态键：location/time_slot/action_points',
    state_value JSON        COMMENT '状态值（JSON）',
    updated_at  DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    UNIQUE KEY uk_session_key (session_id, state_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='游戏状态：世界快照（KV 式）';

-- ---------------------------------------------------------------------
-- 6) relationships 关系：NPC 对玩家/其他 NPC 的好感/信任/恐惧（结构化数值）
-- ---------------------------------------------------------------------
CREATE TABLE relationships (
    id                  INT UNSIGNED AUTO_INCREMENT PRIMARY KEY COMMENT '自增主键',
    session_id          VARCHAR(64) NOT NULL DEFAULT 'seed' COMMENT '归属会话：seed=初始关系（复制模板），否则=该会话的关系现状',
    npc_id              VARCHAR(64) NOT NULL COMMENT 'NPC 一侧',
    other_id            VARCHAR(64) NOT NULL COMMENT '对方：player 或另一 NPC 的 id',
    relation_type       VARCHAR(32) NOT NULL DEFAULT 'neutral' COMMENT '关系定性：ally/enemy/employer/family/neutral',
    trust               INT NOT NULL DEFAULT 0 COMMENT '信任 0~100',
    fear                INT NOT NULL DEFAULT 0 COMMENT '恐惧 0~100',
    affection           INT NOT NULL DEFAULT 0 COMMENT '好感 0~100',
    tags                VARCHAR(255) NOT NULL DEFAULT '' COMMENT '标签，逗号分隔',
    notes               TEXT COMMENT '短备注（最近互动印象）',
    last_interaction_at DATETIME COMMENT '最近互动时间',
    created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    UNIQUE KEY uk_session_pair (session_id, npc_id, other_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='关系：结构化好感/信任/恐惧（M1.1 起会话创建时从 seed 复制，UPDATE 只动本会话行）';

-- ---------------------------------------------------------------------
-- 7) secrets 秘密与泄露等级（§13 双链：主动链=意愿，被动链=识破进度）
-- ---------------------------------------------------------------------
CREATE TABLE secrets (
    id                INT UNSIGNED AUTO_INCREMENT PRIMARY KEY COMMENT '自增主键',
    npc_id            VARCHAR(64)  NOT NULL COMMENT '秘密归属的 NPC',
    secret_id         VARCHAR(64)  NOT NULL COMMENT '业务标识，如 sec_alliance',
    topic             VARCHAR(255) NOT NULL COMMENT '秘密主题',
    reveal_level      VARCHAR(16)  NOT NULL DEFAULT 'guard' COMMENT '泄露等级：guard/hint/half/reveal',
    related_belief    VARCHAR(64)  DEFAULT NULL COMMENT '关联信念 id',
    active_triggers   JSON         COMMENT '主动链触发(trust/emotion/reciprocity 阈值)',
    passive_triggers  JSON         COMMENT '被动链触发(contradiction/evidence)',
    detected_response JSON         COMMENT '被识破后的反应策略数组',
    created_at        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    UNIQUE KEY uk_npc_secret (npc_id, secret_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='秘密：泄露等级 + 双链触发';

-- ---------------------------------------------------------------------
-- 8) beliefs 信念：NPC 对命题的采信程度（驱动秘密泄露/行动）
-- ---------------------------------------------------------------------
CREATE TABLE beliefs (
    id         INT UNSIGNED AUTO_INCREMENT PRIMARY KEY COMMENT '自增主键',
    npc_id     VARCHAR(64)  NOT NULL COMMENT '信念归属的 NPC',
    belief_id  VARCHAR(64)  NOT NULL COMMENT '业务标识，如 bel_duke_dead',
    topic      VARCHAR(255) NOT NULL COMMENT '命题/主题',
    confidence TINYINT      NOT NULL DEFAULT 50 COMMENT '置信度 0~100',
    state      VARCHAR(16)  NOT NULL DEFAULT 'firm' COMMENT '四态：firm/doubt/shaken/betrayed',
    is_core    TINYINT      NOT NULL DEFAULT 0 COMMENT '是否核心信念(难被动摇)',
    created_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    UNIQUE KEY uk_npc_belief (npc_id, belief_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='信念：置信度 + 四态';

-- ---------------------------------------------------------------------
-- 9) npc_inventory 随身物品（§14 动态层：可搜身/偷/掉落/上交，物证线索来源）
-- ---------------------------------------------------------------------
CREATE TABLE npc_inventory (
    id          INT UNSIGNED AUTO_INCREMENT PRIMARY KEY COMMENT '自增主键',
    npc_id      VARCHAR(64)  NOT NULL COMMENT '物品归属的 NPC',
    item_id     VARCHAR(64)  NOT NULL COMMENT '业务标识，如 inv_kerchief',
    name        VARCHAR(64)  NOT NULL COMMENT '物品名',
    clue        TINYINT      NOT NULL DEFAULT 0 COMMENT '是否线索物证',
    clue_target VARCHAR(64)  DEFAULT NULL COMMENT '线索指向(势力/人物/地点)',
    note        TEXT         COMMENT '备注(观察/推理要点)',
    created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    UNIQUE KEY uk_npc_item (npc_id, item_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='随身物品：线索与状态';

-- ---------------------------------------------------------------------
-- 10) goals 目标（§4 行动策略：长期动机 + 短期目标 + 计划）
-- ---------------------------------------------------------------------
CREATE TABLE goals (
    id       INT UNSIGNED AUTO_INCREMENT PRIMARY KEY COMMENT '自增主键',
    npc_id   VARCHAR(64)  NOT NULL COMMENT '目标归属的 NPC',
    goal_id  VARCHAR(64)  NOT NULL COMMENT '业务标识，如 poison_duke',
    priority TINYINT      NOT NULL DEFAULT 50 COMMENT '优先级 0~100',
    type     VARCHAR(16)  NOT NULL DEFAULT 'short' COMMENT 'long/short',
    plan     JSON         COMMENT '计划步骤数组',
    note     TEXT         COMMENT '备注',
    created_at DATETIME   NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    UNIQUE KEY uk_npc_goal (npc_id, goal_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='目标：优先级 + 计划';

-- ---------------------------------------------------------------------
-- 10.5) plans 计划（M1.2 BDI 意图层：行为层心脏，涌现引擎的执行单元）
--   与 goals 的分工：goals = Desire（想什么，一行人话，无执行语义）；
--                    plans = Intention（承诺怎么做，机器可执行的多步骤路径）。
--   steps 结构契约见 app/plans.py 模块注释（M1.4 校验 / M1.5 执行 / M1.6 重规划的共同接口）。
--   session_id='seed' = 出厂计划模板（轮回保留），开局复制成会话运行副本。
--   status 含 replaced：被重规划"换路径"的旧计划——与 abandoned"放弃目标"必须区分，
--   世界线审计里"夫人换了个杀法"和"夫人不杀了"是两条不同的故事（M1.13 分叉统计的原料）。
-- ---------------------------------------------------------------------
CREATE TABLE plans (
    id             BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY COMMENT '自增主键',
    session_id     VARCHAR(64)  NOT NULL DEFAULT 'seed' COMMENT '归属会话：seed=出厂模板（轮回保留），否则=该会话的运行副本',
    npc_id         VARCHAR(64)  NOT NULL COMMENT '计划归属的 NPC',
    goal_id        VARCHAR(64)  NOT NULL DEFAULT '' COMMENT '关联 goals.goal_id（Desire→Intention 可追溯）',
    goal           VARCHAR(255) NOT NULL COMMENT '目标一句话（LLM 重规划的新计划可能无 goal_id，此列必填）',
    stickiness     VARCHAR(16)  NOT NULL DEFAULT 'normal' COMMENT '目标粘性：high=受阻换手段不换目标 / normal=可推迟 / low=可放弃',
    status         VARCHAR(16)  NOT NULL DEFAULT 'active' COMMENT '状态机：active/blocked/replanning/abandoned/completed/replaced',
    current_step   TINYINT      NOT NULL DEFAULT 1 COMMENT '当前步骤编号（1-based，与 steps[].step 对齐；全步完成置 completed）',
    version        INT          NOT NULL DEFAULT 1 COMMENT '重规划版本号（1=开局原版，每次重规划 +1）',
    steps          JSON         NOT NULL COMMENT '步骤数组（结构契约见 app/plans.py；MySQL JSON 类型保证语法合法）',
    source         VARCHAR(16)  NOT NULL DEFAULT 'handwritten' COMMENT '来源：handwritten=策划手写 / llm=重规划生成',
    blocked_reason VARCHAR(512) DEFAULT NULL COMMENT '最近一次 blocked 的原因（重规划 prompt 与审计输入）',
    created_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    UNIQUE KEY uk_plan_version (session_id, npc_id, goal_id, version),
    KEY idx_session_status (session_id, status),
    KEY idx_npc (npc_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='计划：BDI 意图层（愿望在 goals 表，承诺路径在本表）';

-- ---------------------------------------------------------------------
-- 11) schedule 排班（§4 时间线原计划基线，可被玩家打断）
-- ---------------------------------------------------------------------
CREATE TABLE schedule (
    id       INT UNSIGNED AUTO_INCREMENT PRIMARY KEY COMMENT '自增主键',
    npc_id   VARCHAR(64)  NOT NULL COMMENT '排班归属的 NPC',
    time     VARCHAR(8)   NOT NULL COMMENT '时间点 HH:MM',
    location VARCHAR(64)  NOT NULL COMMENT '地点标识',
    action   VARCHAR(128) NOT NULL COMMENT '动作',
    intent   VARCHAR(255) NOT NULL DEFAULT '' COMMENT '自主意图(§16 self 型)',
    created_at DATETIME   NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    KEY idx_npc_time (npc_id, time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='排班：NPC 时间线原计划';

-- ---------------------------------------------------------------------
-- 12) action_log 行动日志（§16 意图来源 + §5 命运判定，支持回放与回归）
-- ---------------------------------------------------------------------
CREATE TABLE action_log (
    id                BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY COMMENT '自增主键',
    npc_id            VARCHAR(64)  NOT NULL COMMENT '行动归属的 NPC',
    action_intent     VARCHAR(255) NOT NULL COMMENT '行动意图描述',
    intent_source     VARCHAR(16)  NOT NULL DEFAULT 'self' COMMENT '意图来源：self/other',
    commander         VARCHAR(64)  DEFAULT NULL COMMENT '外部指令下达者(仅 other)',
    compliance_result VARCHAR(32)  DEFAULT NULL COMMENT '服从/拒绝/讨价还价(仅 other)',
    adjudication_result JSON       COMMENT '命运判定结果(§5)',
    world_delta       JSON         COMMENT '世界状态变更 delta',
    seed              VARCHAR(64)  DEFAULT NULL COMMENT '抽样种子(可回放)',
    created_at        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '发生时间',
    KEY idx_npc_time (npc_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='行动日志：意图来源 + 命运判定';

-- ---------------------------------------------------------------------
-- 13) environment_card 环境卡：地点/关键物"现在是什么样、谁能感知"
--     环境像 NPC 一样有"卡"：Canon 厚描述 + 可变状态 + 感知规则（§8.5）
--     与 world_trace 的分工：本表=当前状态(可被覆盖更新)；world_trace=历史流水(只追加)
-- ---------------------------------------------------------------------
CREATE TABLE environment_card (
    id            INT UNSIGNED AUTO_INCREMENT PRIMARY KEY COMMENT '自增主键',
    env_id        VARCHAR(64)  NOT NULL COMMENT '业务标识：cellar / cellar_key / study（同一 env_id 可在不同世界共存）',
    world_id      VARCHAR(32)  NOT NULL DEFAULT 'golden' COMMENT '所属世界：golden=黄金乡 / test=测试世界（004 迁移：环境卡按世界隔离）',
    kind          VARCHAR(16)  NOT NULL COMMENT '类别：location(地点) / item(关键物)',
    name          VARCHAR(64)  NOT NULL COMMENT '显示名',
    description   TEXT         COMMENT '厚描述(Canon，供旁白/AI 生成细节)',
    state         JSON         COMMENT '可变状态：{"where":"...","holder":"...","locked":true,...}',
    initial_state JSON         COMMENT '出厂状态快照（004 迁移新列：环境卡 reset 时从它恢复，模拟起局回出厂）',
    perception    JSON         COMMENT '感知规则：谁/什么条件下能看到它(access_level/spoiler_level)',
    created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    UNIQUE KEY uk_world_env (world_id, env_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='环境卡：地点/关键物的厚描述 + 可变状态（按世界隔离）';

-- ---------------------------------------------------------------------
-- 14) world_trace 世界痕迹：谁何时在哪做了什么（追加式流水，只增不删）
--     回答"发生过什么、谁可能知道"；供回溯审计 + 有限视角感知 + 旁白生成（§8.5）
-- ---------------------------------------------------------------------
CREATE TABLE world_trace (
    id          BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY COMMENT '自增主键(流水量大用 BIGINT)',
    session_id  VARCHAR(64)  NOT NULL COMMENT '会话/轮回标识',
    tick        INT          NOT NULL COMMENT '时间步(0=18:00，每 tick=10 分钟)',
    actor       VARCHAR(64)  NOT NULL COMMENT '行动者：player 或 npc_id',
    action_type VARCHAR(32)  NOT NULL COMMENT '动作类型：move/use_item/speak/observe/...',
    target      VARCHAR(64)  NOT NULL DEFAULT '' COMMENT '作用对象 id',
    location    VARCHAR(64)  NOT NULL DEFAULT '' COMMENT '发生地 id',
    detail      TEXT         COMMENT '发生了什么(旁白/审计用)',
    visible_to  JSON         COMMENT '谁能感知到(NULL=全可见)',
    created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '发生时间',
    KEY idx_session_tick (session_id, tick)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='世界痕迹：追加式行动流水';

-- ---------------------------------------------------------------------
-- 15) environment_entity 环境实体：权威空间骨架（P3 空间模型落表）
--     每个"占空间的东西"（房间/家具/关键物/门窗墙）一行：坐标/尺寸/朝向/连通性。
--     与 environment_card 的分工：本表=静态骨架(它本来长什么样/在哪/连通谁)；
--     environment_card.state=动态可变状态(钥匙被拿走/门打开)。契合知识/状态分水岭。
--     查询(纯 SQL，程序粗筛，不用 LLM)：
--       entities_in(scene)=SELECT ... WHERE scene=%s；connected_to(room)=SELECT connected_to；resolve()靠 position/size/orientation/frame
-- ---------------------------------------------------------------------
CREATE TABLE environment_entity (
    id          INT UNSIGNED AUTO_INCREMENT PRIMARY KEY COMMENT '自增主键',
    env_id      VARCHAR(64)  NOT NULL COMMENT '业务标识：room_1 / knife / chair（与 environment_card 对齐；纯空间实体可不对应环境卡）',
    world_id    VARCHAR(32)  NOT NULL DEFAULT 'golden' COMMENT '所属世界：golden=黄金乡 / test=测试世界（按世界隔离，测试世界不串乡）',
    scene       VARCHAR(64)  NOT NULL COMMENT '所属场景 id：room_1/room_2/room_3；房间自身 scene=自身',
    name        VARCHAR(64)  NOT NULL COMMENT '中文显示名：测试房间一 / 一把刀',
    type        VARCHAR(16)  NOT NULL COMMENT '类别：room / building(门窗墙) / furniture(家具) / key_item(关键物)',
    position    JSON         COMMENT '底部中心坐标 [x,y,z]（米，相对约定原点）；房间为包围盒中心可空',
    size        JSON         COMMENT '包围盒尺寸 [x,y,z]（米）；房间=净空尺寸，实体=自身尺寸',
    orientation FLOAT        NOT NULL DEFAULT 0 COMMENT '朝向角（度，绕全局 Z 轴，0°=朝北+Y）；房间可无视',
    bounds      JSON         COMMENT '房间空间外接矩形 AABB：{"x_min","x_max","y_min","y_max","z_min","z_max"}（仅 type=room 用）',
    frame       JSON         COMMENT '方位参考系：{"north","door","windows"...}（仅 type=room 用）',
    connected_to JSON        COMMENT '房间连通性：["room_2","room_3"]（仅 type=room 用；实体为空）',
    is_anchor   TINYINT      NOT NULL DEFAULT 0 COMMENT '是否锚点（床/壁炉/桌子等大件可作参照物）',
    anchor_label VARCHAR(64) NOT NULL DEFAULT '' COMMENT '锚点标签：床头 / 壁炉旁（供 resolve() ①锚点命中）',
    source      VARCHAR(16)  NOT NULL DEFAULT 'seed' COMMENT '来源：seed=作者拍的 / inferred=运行中合理化具现（三层判定）',
    created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    UNIQUE KEY uk_world_env (world_id, env_id),
    KEY idx_scene (world_id, scene)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='环境实体：权威空间骨架（坐标/尺寸/朝向/连通性/锚点）';

-- ---------------------------------------------------------------------
-- 16) npc_mental_state 运行时心智热态（006 迁移，心智引擎重构 v0.2）：
--     情绪 PAD 向量 / 主情绪词 / 信念 / 察觉事项 / 工作记忆 / 上一帧观测。
--     与 character_card.mental_model 的分工：那边是"生来什么样"（冷/静态），
--     这边是"此刻心里怎么样"（热/动态）。纯 code 读写，LLM 只见状态词。
--     MVP 不引入 Redis：本表 + 进程内缓存即可（重构文档 §10.3 决策）。
--     15) environment_entity（P3 空间模型）由并行空间管线工作线新增。
-- ---------------------------------------------------------------------
CREATE TABLE npc_mental_state (
    id               BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY COMMENT '自增主键',
    session_id       VARCHAR(64)  NOT NULL COMMENT '会话/轮回标识（与 npc_memory/relationships 同一隔离维度）',
    npc_id           VARCHAR(64)  NOT NULL COMMENT 'NPC 业务标识',
    emotion          JSON         DEFAULT NULL COMMENT 'PAD 情绪向量 {"valence":-1~1,"arousal":0~1,"dominance":0~1}（Russell 1980 / Mehrabian）',
    emotion_word     VARCHAR(32)  NOT NULL DEFAULT '' COMMENT '主情绪词（词映射产物，进 prompt L4 块，数字不进 prompt）',
    emotion_intensity DECIMAL(4,3) DEFAULT NULL COMMENT '情绪强度 0~1（词档位：微/中/强）',
    beliefs          JSON         DEFAULT NULL COMMENT '信念列表 [{"topic","confidence":0~100,"state":"firm/doubt/shaken/betrayed","is_core":bool}]',
    noticed          JSON         DEFAULT NULL COMMENT '最近察觉事项（detect_change 输出，prompt L3 块）',
    working_memory   JSON         DEFAULT NULL COMMENT '工作记忆（文字条目列表，会话内滚动）',
    last_observation JSON         DEFAULT NULL COMMENT '上一帧观测快照（detect_change 的先验来源之一）',
    updated_tick     INT          NOT NULL DEFAULT 0 COMMENT '最近更新的游戏 tick（0=开局未更新）',
    created_at       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    UNIQUE KEY uk_session_npc (session_id, npc_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='运行时心智热态：情绪/信念/工作记忆（纯 code 读写，LLM 只见状态词，按会话隔离）';
