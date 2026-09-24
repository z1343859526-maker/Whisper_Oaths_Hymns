-- =====================================================================
-- 005_environment_entity.sql
-- 目标：新建 environment_entity 表——「权威实体清单 / 空间骨架」。
--
-- 背景/为什么改（环境管线 P3 空间模型落表）：
--   environment_card 只存"厚描述 + 可变状态"，回答不了空间问题：
--   「床边有什么」「我能看见什么」「这房间连接到哪些房间」。
--   第六章定稿：要有"粗糙实体模型"——实体的存在记录、分布、坐标、尺寸、
--   朝向、方位基准，密度足以支撑"空间查询 + 视线判定 + 方位解读"。
--
--   本表是**空间骨架**（静态出厂信息：它本来长什么样、在哪、多大、朝哪、连通谁），
--   与 environment_card 的 state（动态可变：钥匙被拿走、门打开）职责分离。
--   静态骨架 vs 动态状态，契合「知识(静态)/状态(动态)」分水岭。
--
-- 与 environment_card 的关系：
--   - environment_card = 环境卡（厚描述 + 可变状态 + 感知规则），来源已有；
--   - environment_entity = 每个"占空间的东西"（房间/家具/关键物）一行空间数据；
--   - 两者用 env_id 对齐（room_1 房间卡 & room_1 房间实体是同一 env_id，kind 不同）。
--   但允许 environment_entity 有 environment_card 里没有的纯空间实体（如墙/门窗/building），
--   所以本表不设外键，envi_id 只是业务关联，不强约束。
--
-- 查询能力（纯 SQL，程序粗筛，不用 LLM）：
--   ① entities_in(scene)  = SELECT ... WHERE scene=%s（该房间有什么）
--   ② connected_to(room)  = SELECT connected_to WHERE env_id=%s（能从哪到哪）
--   ③ 空间定位 resolve()  靠 position/size/orientation/bounds/frame 做区域查询
--
-- 执行（在 server/ 目录）：python scripts/run_migration.py sql/migrations/005_environment_entity.sql
-- =====================================================================

CREATE TABLE IF NOT EXISTS environment_entity (
    id          INT UNSIGNED AUTO_INCREMENT PRIMARY KEY COMMENT '自增主键',
    env_id      VARCHAR(64)  NOT NULL COMMENT '业务标识：room_1 / knife / chair（与 environment_card 对齐；墙面等纯空间实体可不对应环境卡）',
    world_id    VARCHAR(32)  NOT NULL DEFAULT 'golden' COMMENT '所属世界：golden=黄金乡 / test=测试世界（按世界隔离，测试世界不串乡）',
    scene       VARCHAR(64)  NOT NULL COMMENT '所属场景 id：room_1/room_2/room_3；房间自身的 scene=自身（如 room_1 属 room_1）',
    name        VARCHAR(64)  NOT NULL COMMENT '中文显示名：如 测试房间一 / 一把刀',
    type        VARCHAR(16)  NOT NULL COMMENT '类别：room(房间) / building(门窗墙等构件) / furniture(家具) / key_item(关键物)',
    position    JSON         COMMENT '底部中心坐标 [x,y,z]（米，相对约定原点），房间为包围盒中心可空',
    size        JSON         COMMENT '包围盒尺寸 [x,y,z]（米）；房间为净空尺寸，实体为自身尺寸',
    orientation FLOAT        NOT NULL DEFAULT 0 COMMENT '朝向角（度，绕全局 Z 轴，0°=朝北+Y）；房间可无视',
    bounds      JSON         COMMENT '房间空间外接矩形 AABB：{"x_min","x_max","y_min","y_max","z_min","z_max"}（仅 type=room 用）',
    frame       JSON         COMMENT '方位参考系：{"north","door","windows"...} 门/窗映射到局部轴（仅 type=room 用）',
    connected_to JSON        COMMENT '房间连通性：["room_2","room_3"]（仅 type=room 用；实体为空）',
    is_anchor   TINYINT      NOT NULL DEFAULT 0 COMMENT '是否锚点（床/壁炉/桌子等大件可作参照物：玩家说"去床边"）',
    anchor_label VARCHAR(64) NOT NULL DEFAULT '' COMMENT '锚点标签：如 床头 / 壁炉旁（供 resolve() ①锚点命中）',
    source      VARCHAR(16)  NOT NULL DEFAULT 'seed' COMMENT '来源：seed=作者拍的客观存在 / inferred=运行中合理化具现（三层判定）',
    created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    UNIQUE KEY uk_world_env (world_id, env_id),
    KEY idx_scene (world_id, scene)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='环境实体：权威空间骨架（坐标/尺寸/朝向/连通性/锚点），与环境卡职责分离';
