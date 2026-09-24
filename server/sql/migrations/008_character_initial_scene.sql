-- =====================================================================
-- 008_character_initial_scene.sql
-- 目标：character_card 新增 initial_scene（出生地/开局所在场景）。
--
-- 背景（环境系统文档 §8.7 已知限制的修复）：npc_pos 开局由
-- sessions._init_npc_positions 按"激活计划第一步 scene"定位——无计划 NPC
-- （如 test_woman）开局位置为 None：观察恒"空无一人"、NPC 自身决策也不知道
-- 自己在哪。计划是"意图"不该兼任"出生地"；出生地是角色卡的静态属性。
--
-- 兼容：NULL = 未设定（沿用旧行为：按计划第一步定位，仍无则位置未知）。
-- 执行（server/ 目录）：python scripts/run_migration.py sql/migrations/008_character_initial_scene.sql
-- =====================================================================

ALTER TABLE character_card
ADD COLUMN initial_scene VARCHAR(64) DEFAULT NULL COMMENT '出生地/开局所在场景 id（如 room_1；NULL=未设定，沿用按计划第一步定位的旧行为）' AFTER knowledge_scope;
