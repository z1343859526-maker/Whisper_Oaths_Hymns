-- =====================================================================
-- 007 观察扩展：environment_card 增加 detail 层（观测级极详厚描述）
--
-- 为什么需要它（对象级观察的地基）：
--   environment_card.description 是"简述"（供场景感知/旁白粗读），但玩家"仔细观察"
--   一个物体时要的"细节"（材质/尺寸/做工/磨损/气味/能藏什么）并不在 description 里。
--   若让 LLM 凭空补，会幻觉且两次不一致。因此加一层独立、更深的 detail 文本——
--   "独立信息存储"里存足足够详细的细节，观察时注入给 LLM，让"细节有据、前后一致"。
--
--   observation 的深度分层（本次实现）：
--     · description = 简述（默认可见）；
--     · detail     = 深层细节（受玩家【观察力 perception】门槛保护——低观察力看不到
--                    detail 层，高观察力才能展开）→ 这正是"信息厚度受观察力影响"。
--
-- 用法：python scripts/run_migration.py sql/migrations/007_observation_detail.sql
--   （与 001~006 同款跑法；重复执行会因列已存在报错，符合"失败即停"约定。）
-- =====================================================================
ALTER TABLE environment_card
    ADD COLUMN detail TEXT COMMENT '观测细节层：极详厚描述（独立于 description 简述，供对象级观察/观察力分层展开）' AFTER description;
