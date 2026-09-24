-- =====================================================================
-- 迁移 001：会话隔离（M1.1）
-- 目的：给 npc_memory / relationships 装上会话维度，消除轮回/多会话状态串台（R8）
--
-- 语义约定（见 开发日志_NPC系统.md §3-D3）：
--   session_id = 'seed'  → 先验数据（NPC 出生自带：种子记忆 / 初始关系），
--                          轮回后仍然生效，任意会话可读；
--   session_id = 会话id  → 运行时数据（本轮产生的记忆 / 关系变动），会话间互不可见。
--
-- 存量数据处理：ALTER 加列带 DEFAULT 'seed'，现有行自动成为先验数据。
-- 注意：开发库中曾手工写入的测试记忆也会因此被标为先验——属已知脏数据，
--       游戏级 seed（M1.3）交付时以全量重灌为准。
--
-- 执行（server/ 目录）：mysql -uroot -p golden_murder < sql/migrations/001_session_isolation.sql
-- 幂等性：本脚本不可重复执行（ALTER 无 IF NOT EXISTS，MySQL 8.0 前）；
--         重复执行会报 Duplicate column，属预期报错，忽略即可。
-- =====================================================================

USE golden_murder;

-- 1) npc_memory：标记式隔离（INSERT-only 表，加列标记即可）
ALTER TABLE npc_memory
    ADD COLUMN session_id VARCHAR(64) NOT NULL DEFAULT 'seed'
        COMMENT '归属会话：seed=先验记忆（轮回保留），否则=运行时记忆（会话隔离）'
        AFTER npc_id,
    ADD KEY idx_npc_session (npc_id, session_id);

-- 2) relationships：复制式隔离（UPDATE 累加表，会话创建时从 seed 复制初始行）
--    原唯一键 (npc_id, other_id) 会阻止"同一对关系多会话并存"，必须换成含 session 的键。
ALTER TABLE relationships
    ADD COLUMN session_id VARCHAR(64) NOT NULL DEFAULT 'seed'
        COMMENT '归属会话：seed=初始关系（复制模板），否则=该会话的关系现状'
        AFTER id,
    DROP INDEX uk_pair,
    ADD UNIQUE KEY uk_session_pair (session_id, npc_id, other_id);

-- 验证：两表列结构应含 session_id
-- SHOW COLUMNS FROM npc_memory LIKE 'session_id';
-- SHOW COLUMNS FROM relationships LIKE 'session_id';
