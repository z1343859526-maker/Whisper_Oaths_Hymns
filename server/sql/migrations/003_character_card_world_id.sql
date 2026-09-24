-- =====================================================================
-- 003_character_card_world_id.sql
-- 目标：给 character_card 增加世界维度 world_id，实现「不同世界不同 NPC」。
--
-- 背景/为什么改：world_knowledge 已有 world_id（002），但角色卡没有。
-- 导致 db.get_all_npc_ids() 把黄金乡 NPC 和测试 NPC 混在一起返回——
-- 跑测试世界模拟时，会遍历到黄金乡角色（他们只有黄金乡排班），
-- 这些角色在测试世界里既不该出现、也不该被调度（否则 OOC）。
-- 加 world_id 后，按「世界」过滤 NPC：测试世界只遍历 test_man/test_woman，
-- 黄金乡只遍历王族一干人——物理上同一张表、逻辑上各世界独立角色池。
--
-- 与 002 的关联：002 解决了"知识与世界隔离"，本迁移解决"角色与世界隔离"。
-- 二者共同构成 M1.3 环境确定性 / M1.5 世界隔离的基础：世界=角色池+知识库+环境。
--
-- 执行（在 server/ 目录）：python scripts/run_migration.py sql/migrations/003_character_card_world_id.sql
-- 说明：只 ALTER ADD 一列，幂等性由 run_migration 依赖（重复执行因列已存在报错，
-- 按"失败即停"处理，无需二次执行）。
-- =====================================================================

ALTER TABLE character_card
ADD COLUMN world_id VARCHAR(32) NOT NULL DEFAULT 'golden' COMMENT '所属世界：golden=黄金乡 / test=测试世界' AFTER npc_id;

-- 存量数据（此前全是黄金乡角色）统一标为 golden，保证迁移后角色池语义正确
UPDATE character_card SET world_id = 'golden' WHERE world_id = 'golden' OR world_id = '';
