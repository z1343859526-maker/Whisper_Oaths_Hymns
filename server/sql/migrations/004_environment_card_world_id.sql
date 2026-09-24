-- =====================================================================
-- 004_environment_card_world_id.sql
-- 目标：给 environment_card 增加世界维度 world_id + 出厂状态 initial_state，
--       并新建 world 表（世界清单，世界名从 DB 读、不再硬编码）。
--
-- 背景/为什么改（M1.5 泛化：测试世界 = 数据更少的正式世界，同一套引擎）：
--   character_card(003) / world_knowledge(002) 已有 world_id，唯独 environment_card
--   没有——测试世界的 knife/room_1 与黄金乡的 cellar_key/study 全混在同一张表，
--   db.get_environment_cards() 全量返回，测试角色的 LLM 决策会"看到"黄金乡地点
--   （跨世界环境泄露）。这与"换任何世界观都能运行"的泛化目标是冲突的。
--
--   加 world_id 后，环境卡与角色/知识三处对齐：世界 = 角色池 + 知识库 + 环境卡。
--
--   同时加 initial_state（出厂状态）：原 simulate._reset_test_world_environment 硬编码
--   room_1/knife/chair 的初始 state，等于把测试世界的环境"写死"在代码里。改由
--   environment_card 自己记录初始状态，reset 环境卡时从 initial_state 恢复——
--   任何世界都通用，不再依赖代码里列 id。
--
-- 与 002/003 的关联：002=知识按世界隔离、003=角色按世界隔离、004=环境按世界隔离。
-- 三者共同构成"M1.5 世界作为数据驱动维度"的根基。
--
-- 执行（在 server/ 目录）：python scripts/run_migration.py sql/migrations/004_environment_card_world_id.sql
-- =====================================================================

ALTER TABLE environment_card
ADD COLUMN world_id VARCHAR(32) NOT NULL DEFAULT 'golden' COMMENT '所属世界：golden=黄金乡 / test=测试世界' AFTER env_id;

-- 出厂状态：环境卡"轮回初始"时的 state（供模拟/轮回开始时 reset 环境卡用）。
-- 允许 NULL：未设定出厂状态的卡（如旧数据）reset 时跳过，不强行覆盖。
ALTER TABLE environment_card
ADD COLUMN initial_state JSON DEFAULT NULL COMMENT '出厂状态（seed 写入时的 state 快照，供环境卡 reset）' AFTER state;

-- 存量数据（此前环境卡都是黄金乡）统一标为 golden（与 003 同款处理，保证迁移后语义正确）
UPDATE environment_card SET world_id = 'golden' WHERE world_id = 'golden' OR world_id = '';

-- ---------------------------------------------------------------------
-- 新建 world 表：世界清单（世界名等元数据从 DB 读，代码不再硬编码 _WORLD_NAMES）
-- 目标：加世界观 = 往本表插一行 + 建对应 seed 文件，不改任何代码。
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS world (
    id          INT UNSIGNED AUTO_INCREMENT PRIMARY KEY COMMENT '自增主键',
    world_id    VARCHAR(32)  NOT NULL UNIQUE COMMENT '世界标识：golden=黄金乡 / test=测试世界',
    world_name  VARCHAR(64)  NOT NULL COMMENT '世界显示名（替代代码里硬编码的 _WORLD_NAMES）',
    description TEXT COMMENT '世界一句话描述',
    is_active   TINYINT      NOT NULL DEFAULT 1 COMMENT '是否启用 1=启用 0=停用',
    created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='世界清单：世界元数据（RAG/角色/环境隔离的顶层维度）';
