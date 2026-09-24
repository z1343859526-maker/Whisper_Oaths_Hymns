-- =====================================================================
-- 测试世界 · 空间骨架 seed（environment_entity）
-- 执行（server/ 目录下）：mysql -uroot -p golden_murder < sql/seed_test_spatial.sql
--
-- 与 seed_test_world.sql 的关系：那边灌 environment_card（厚描述/状态/感知），
-- 本文件灌 environment_entity（空间骨架：坐标/尺寸/朝向/连通性/锚点）。
-- 两表 env_id 对齐（room_1/room_2/room_3/knife/chair 共用业务 id）。
--
-- 【坐标约定（测试世界，自编，非剧情模式 3D 建模）】
--   · 原点：房间二（room_2，中央房间）几何中心，Y 轴朝北、X 轴朝东、Z 轴向上（米）。
--   · 3 个房间排成东西向直线：room_1(西) -- room_2(中，连接驿站) -- room_3(东)。
--   · 连通性：room_1↔room_2、room_2↔room_3；room_1↔room_3 断开（验证不可达规则）。
--   · 刀(knife)在 room_3 角落；椅子(chair)在 room_2 中央（与 seed_test_world 的
--     current_place 完全一致，保证空间数据与环境卡状态不矛盾）。
--
-- ⚠️ JSON 列（size/bounds/frame/connected_to）必须写成 JSON 字符串（单引号），
--    不能写裸数组字面量（MySQL 会把 [..] 当成数组构造语法而报错）。
--
-- 幂等：ON DUPLICATE KEY UPDATE（uk_world_env=(world_id,env_id) 命中即刷新空间数据）。
--   ⚠️ 只刷空间骨架，不碰 environment_card（状态由 seed_test_world + 引擎维护）。
-- =====================================================================

USE golden_murder;

-- 房间（type=room；bounds=净空 AABB；connected_to=连通性；frame=方位参考系）：
--   房间二居中 (0,0)：bounds x[-5,5] y[-4,4]；房间一在其西 x[-14,-4]；房间三在其东 x[4,14]
INSERT INTO environment_entity
(env_id, world_id, scene, name, type, position, size, orientation, bounds, frame, connected_to, is_anchor, anchor_label, source)
VALUES
('room_1', 'test', 'room_1', '测试房间一', 'room',
 NULL, '[10.0,8.0,3.0]', 0,
 '{"x_min":-14.0,"x_max":-4.0,"y_min":-4.0,"y_max":4.0,"z_min":0.0,"z_max":3.0}',
 '{"north":"-y","door":"east","windows":[]}', '["room_2"]', 0, '', 'seed'),
('room_2', 'test', 'room_2', '测试房间二', 'room',
 NULL, '[10.0,8.0,3.0]', 0,
 '{"x_min":-4.0,"x_max":4.0,"y_min":-4.0,"y_max":4.0,"z_min":0.0,"z_max":3.0}',
 '{"north":"-y","door":"east,west","windows":[]}', '["room_1","room_3"]', 0, '', 'seed'),
('room_3', 'test', 'room_3', '测试房间三', 'room',
 NULL, '[10.0,8.0,3.0]', 0,
 '{"x_min":4.0,"x_max":14.0,"y_min":-4.0,"y_max":4.0,"z_min":0.0,"z_max":3.0}',
 '{"north":"-y","door":"west","windows":[]}', '["room_2"]', 0, '', 'seed')
ON DUPLICATE KEY UPDATE
  name=VALUES(name), scene=VALUES(scene), type=VALUES(type), position=VALUES(position),
  size=VALUES(size), orientation=VALUES(orientation), bounds=VALUES(bounds),
  frame=VALUES(frame), connected_to=VALUES(connected_to), is_anchor=VALUES(is_anchor),
  source=VALUES(source);

-- 实体：刀(key_item，room_3 东北角，锚点) + 椅子(furniture， room_2 中央，锚点)
INSERT INTO environment_entity
(env_id, world_id, scene, name, type, position, size, orientation, bounds, frame, connected_to, is_anchor, anchor_label, source)
VALUES
('knife', 'test', 'room_3', '一把刀', 'key_item',
 '[12.0,3.0,0.75]', '[0.08,0.60,0.04]', 90, NULL, NULL, NULL, 1, '房间三角落', 'seed'),
('chair', 'test', 'room_2', '一把椅子', 'furniture',
 '[0.0,0.0,0.45]', '[0.60,0.60,0.90]', 0, NULL, NULL, NULL, 1, '房间二中央', 'seed')
ON DUPLICATE KEY UPDATE
  name=VALUES(name), scene=VALUES(scene), type=VALUES(type), position=VALUES(position),
  size=VALUES(size), orientation=VALUES(orientation), is_anchor=VALUES(is_anchor),
  anchor_label=VALUES(anchor_label), source=VALUES(source);
