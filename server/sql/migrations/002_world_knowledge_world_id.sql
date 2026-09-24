-- =====================================================================
-- 002_world_knowledge_world_id.sql
-- 目标：给 world_knowledge 增加世界维度 world_id，实现「不同世界不同 RAG 库」。
--
-- 背景/为什么改：原表里 npc_id='global' 的公共世界观（黄金乡、晚宴、宵禁…）
-- 是跨世界共享的。测试角色 can_access=["public","faction"] 检索时命中这些
-- 黄金乡知识，导致测试世界对话串乡（测试女开口就是"黄金乡晚宴"）。
-- 加 world_id 后，检索按「当前世界 + 有权访问」过滤：测试世界只查 test 的知识，
-- 黄金乡只查 golden 的知识，物理上同一张表、逻辑上各世界独立一个 RAG 库。
--
-- 执行（在 server/ 目录）：python scripts/run_migration.py sql/migrations/002_world_knowledge_world_id.sql
-- 说明：本脚本只 ALTER ADD 一列，幂等性由 run_migration 依赖
--（重复执行会因列已存在报错，按"失败即停"处理，无需二次执行）。
-- =====================================================================

ALTER TABLE world_knowledge
ADD COLUMN world_id VARCHAR(32) NOT NULL DEFAULT 'golden' COMMENT '所属世界：golden=黄金乡 / test=测试世界' AFTER npc_id;

-- 存量数据（此前全是黄金乡）统一标为 golden，保证迁移后检索语义正确
UPDATE world_knowledge SET world_id = 'golden' WHERE world_id = 'golden' OR world_id = '';
