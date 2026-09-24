-- =====================================================================
-- 种子数据 v1（游戏级，M1.3）：NPC 阵容 + 全员日程骨架 + 时间类公共知识
-- 执行：python scripts/reseed.py（封装了本文件 + 向量 backfill + 行数对账）
--
-- 设计原则（M1.3 定稿，M1.5 执行器的数据契约）：
-- ① 日程只排「若无其事的世界」：宴会/夜读/就寝/晨检——犯罪由 plans 层叠加，
--    两层职责分离，杜绝"夫人被 schedule 拽去取钥匙、又被 plan 告知还没到点"的双轨冲突；
-- ② 一份数据两处消费：时间事实同时落 schedule（行为层）与 world_knowledge（认知层，
--    支撑 /chat RAG"宴会几点"答得出），两处必须同步改——本文件内已交叉注释互指；
-- ③ 关键实体精确、周边后续填充：核心 7 NPC + 女仆代表 alice；女仆×4 其余/女厨×2 属 M3 活世界。
-- =====================================================================

USE golden_murder;

-- 先清空（幂等：可重复执行，清空后 id 从 1 重新开始）
TRUNCATE TABLE world_trace;
TRUNCATE TABLE environment_card;
TRUNCATE TABLE action_log;
TRUNCATE TABLE plans;
TRUNCATE TABLE schedule;
TRUNCATE TABLE goals;
TRUNCATE TABLE npc_inventory;
TRUNCATE TABLE beliefs;
TRUNCATE TABLE secrets;
TRUNCATE TABLE relationships;
TRUNCATE TABLE npc_memory;
TRUNCATE TABLE character_card;
TRUNCATE TABLE dialogue_log;
TRUNCATE TABLE world_knowledge;

-- 0) 世界清单：注册黄金乡（004 迁移新增 world 表——世界名从 DB 读，不硬编码）
--    加世界观 = 往 world 表插一行 + 建对应 seed 文件，不改任何代码。
INSERT INTO world (world_id, world_name, description) VALUES
('golden', '黄金乡谋杀案', '奥兰王国南部最丰饶的领地——一场家族阴谋与凶案交织的庄园之夜。')
ON DUPLICATE KEY UPDATE world_name=VALUES(world_name), description=VALUES(description);

-- 1) 角色卡：2 个 NPC
INSERT INTO character_card (npc_id, name, title, personality, motivation, forbidden) VALUES
('prince_adrian', '艾德里安', '奥兰王子', '骄傲、急切、护妹心切', '查清妹妹（公主）失踪的真相', '["公主的真实下落是我最大的秘密"]'),
('harold', '哈罗德', '老管家', '沉稳、忠诚、观察入微', '守护黄金乡的体面与安全', '["老爷与夫人的真实关系"]');

-- M1.3 最小卡×4（7 列 + 知识边界，够 M1.5 执行器/M1.7 反应/RAG 检索用；17 列 P4 精修属 M2+ 逐人任务）
-- can_access 决定 RAG 候选集（context_builder._parse_access）：alice 女仆知作息、godefroy 阴谋方知 secret
INSERT INTO character_card (npc_id, name, title, personality, motivation, forbidden, knowledge_scope) VALUES
('duke_roderick', '罗德里克·诺曼', '黄金乡大公爵', '威严专制、重体面、掌控欲强', '促成联姻、压制夫人一族势力、维持黄金乡体面', '["对夫人的轻视与嫌隙","书房夜饮安神酒的习惯"]', '{"knows":["联姻谈判的底线","庄园的财政与防务"],"does_not_know":["夫人的密约","地窖里的秘密"],"can_access":["public","faction"]}'),
('elena', '艾琳娜·诺曼', '黄金骑士 / 大公独女', '外冷内热、恪尽职守、观察敏锐', '守护黄金乡与父亲安危', '["对母亲起疑一事","自己深夜巡查的真正原因"]', '{"knows":["母亲近来深夜外出","庄园各处巡防路线"],"does_not_know":["母亲的密约与谋杀计划"],"can_access":["public","faction"]}'),
('godefroy', '戈弗雷·冯·弗尔曼', '魔鬼骑士 / 弗尔曼使者', '阴鸷缜密、言出必行、以契约为纲', '促成大公倒向弗尔曼、谋取黄金乡', '["三线并进的真实计划","与夫人的密约"]', '{"knows":["与夫人的密约内容","弗尔曼的真实意图"],"does_not_know":["夫人可能另有算计"],"can_access":["public","faction","secret"]}'),
('alice', '爱丽丝', '女仆', '勤谨胆小、循规蹈矩、观察力平平', '做好本分差事、平安度日', '[]', '{"knows":["庄园作息规矩","各房位置"],"does_not_know":["主人间的隐秘"],"can_access":["public","faction"]}');

-- 2) NPC 记忆：给王子插 4 条，importance 各不相同（供 2.2 降序查询）
--    其中一条是另一条的子记忆（parent_id 自关联，供 2.4 演示父子链）
INSERT INTO npc_memory (npc_id, memory_type, content, importance, parent_id) VALUES
('prince_adrian', 'event', '晚宴上，金穗夫人当众羞辱了我父亲。', 80, NULL),           -- id=1 父记忆
('prince_adrian', 'impression', '金穗夫人如此羞辱，背后定与弗尔曼使者勾结。', 70, 1), -- id=2 子记忆，父=id1
('prince_adrian', 'impression', '那个叫戈弗雷的使者眼神阴冷，我不信任他。', 60, NULL),
('prince_adrian', 'relation', '管家哈罗德似乎知道些什么，却不愿明说。', 40, NULL);

-- 3) 关系：trust 各不相同（供 2.2 排序查询）
INSERT INTO relationships (npc_id, other_id, relation_type, trust, fear, affection) VALUES
('prince_adrian', 'harold', 'ally', 70, 0, 50),
('prince_adrian', 'player', 'neutral', 20, 0, 10),
('harold', 'prince_adrian', 'employer', 60, 10, 30),
('harold', 'player', 'neutral', 5, 0, 0);

-- 4) 对话日志：npc 发言带 OOC 评分，player 发言评分为 NULL（演示 AVG 自动忽略 NULL）
--    王子 3 条（0.95/0.92/0.88 都偏高=表现稳定）；哈罗德 2 条（0.60/0.70 偏低=怀疑人设不稳）
INSERT INTO dialogue_log (session_id, npc_id, speaker, content, ooc_score) VALUES
('session_0001', 'prince_adrian', 'npc',     '那该死的弗尔曼使者，竟敢当众羞辱我的父亲。', 0.95),
('session_0001', 'prince_adrian', 'player',  '殿下息怒，此事另有蹊跷。', NULL),
('session_0001', 'prince_adrian', 'npc',     '若让我抓到把柄，我定要他血债血偿！', 0.92),
('session_0001', 'prince_adrian', 'npc',     '此事不必声张，我自有安排。', 0.88),
('session_0001', 'harold',        'npc',     '老朽只知守好本分，客人不必多问。', 0.70),
('session_0001', 'harold',        'player',  '管家先生，这座庄园似乎藏着不少秘密？', NULL),
('session_0001', 'harold',        'npc',     '今夜风大，小心烛火，早些歇息为好。', 0.60);

-- 5) 世界观知识库 world_knowledge：先验层（一份原文、两处索引，§17）
--    npc_id='global'   = 全局公共世界观（所有 NPC/玩家共享）
--    npc_id='isabella' = 金穗夫人的先验（02_世界观先验.yaml 同步，她"本来就知道"的事，是变化察觉的对照基线）
-- world_id 省略：走列默认值 'golden'（黄金乡知识天然归属黄金乡世界）
INSERT INTO world_knowledge (npc_id, category, title, content, tags, access_level, spoiler_level) VALUES
-- 全局公共世界观
-- 「晚宴」与「宵禁/晨检/早餐」= M1.3 时间类公共事实：与 schedule 段同步维护（一份数据两处消费）
-- 条目原子化（RAG 最佳实践）：一条一个事实——巨型条目的 n-gram 会被无关内容稀释成
-- 检索噪声（M1.3 诊断实证：合并条目相似度 0.007，拆分后关键词直接命中）。
('global',   'location', '黄金乡',      '奥兰王国南部最丰饶的领地，以金麦与葡萄酒闻名。',            'location,kingdom',        'public',  0),
('global',   'event',    '晚宴',        '今晚 18:00 于大宴会厅开席，21:00 散席。为欢迎奥兰王室使团而设的盛大晚宴。', 'event,banquet,schedule', 'public',  0),
('global',   'rule',     '宵禁',        '庄园宵禁 23:30：夜里 23:30 后各归居所，不该有外人走动。',  'schedule,作息,宵禁',      'public',  0),
('global',   'rule',     '晨检',        '仆人晨检 06:30：每日清晨 06:30 起，仆人逐室查看灯烛门窗。', 'schedule,作息,晨检',      'public',  0),
('global',   'rule',     '早餐',        '庄园早餐 08:00：每日 08:00 于大宴会厅用早餐。',           'schedule,作息,早餐',      'public',  0),
('global',   'faction',  '弗尔曼势力',  '弗尔曼公爵觊觎黄金乡已久，借联姻之名暗中图谋。',          'faction,plot',            'faction', 1),
('global',   'faction',  '王室与封臣',  '奥兰王室与黄金乡大公爵表面融洽，实则明和暗争。',          'faction,politics',        'faction', 1),
('global',   'secret',   '金穗夫人',    '金穗夫人与弗尔曼使者暗中往来，图谋架空大公爵。',          'secret,plot',             'secret',  3),
-- 金穗夫人先验（02_世界观先验.yaml 同步）；RAG 召回时按 knowledge_scope.can_access 过滤
('isabella', '地点',     '地窖钥匙',    '地窖钥匙藏在自己卧室的花盆下',                            '地点,秘密',               'secret',  3),
('isabella', '地点',     '庄园宵禁',    '庄园夜里 23:30 宵禁，各归居所，不该有外人走动',            '地点,规则',               'faction', 1),
('isabella', '地点',     '大宴会厅',    '大宴会厅 great_hall 是接待宾客的主厅',                    '地点,场所',               'public',  0),
('isabella', '人物',     '与戈弗雷密约','与弗尔曼使者戈弗雷有秘密盟约，联手架空大公',              '人物,秘密',               'secret',  3),
('isabella', '人物',     '哈罗德忠心',  '管家哈罗德对家族忠心耿耿',                                 '人物,关系',               'faction', 1),
('isabella', '人物',     '艾琳娜异样',  '女儿艾琳娜是黄金骑士，表面顺从，近来似有异样',             '人物,关系',               'faction', 2),
('isabella', '规则',     '大公把控严',  '大公罗德里克对家族内部事务把控极严',                       '规则,势力',               'faction', 2);

-- =====================================================================
-- ============ 金穗夫人（isabella）P4 完整样例 ============
-- 数据源：金穗夫人结构化人物设定（YAML，source of truth）
-- 说明：YAML 为 source of truth；本文件把结构化字段同步进 MySQL
-- =====================================================================

-- 6) 角色卡 character_card（含外貌/着装/性格/认知/思维链/合作画像）
INSERT INTO character_card
(npc_id, name, title, personality, background, motivation, forbidden, knowledge_scope,
 appearance, outfits, personality_traits, cognitive, speech_style, example_dialogue,
 thinking_chain, cooperation_profile) VALUES
('isabella', '伊莎贝拉·诺曼', '金穗夫人 / 大公爵夫人',
 '表面温和、内里冷酷；心思缜密、绵里藏针；对下人矜持高傲，对宾客谦和周到',
 '黄金乡大公爵罗德里克之妻，唯一的女儿是黄金骑士艾琳娜。多年婚姻中被丈夫压制，暗中积蓄力量，与弗尔曼使者戈弗雷结盟，意图架空大公、夺取黄金乡实权。',
 '长期：架空大公、上位夺权、保全自身与家族地位；当下：取地窖钥匙、配合谋杀大公、善后嫁祸',
 '{"spoiler":["与戈弗雷·冯·弗尔曼的密约","毒杀大公爵的计划","地窖钥匙与地窖里的东西","自己才是主谋"],"knowledge":["玩家的真实身份与来意"],"behavior":["不在公开场合流露对丈夫的敌意","不主动承认与弗尔曼的往来","在女儿艾琳娜面前掩饰"]}',
 '{"knows":["与戈弗雷的密约内容","毒杀大公的计划","地窖钥匙藏在卧室花盆","地窖里处理的是何事"],"does_not_know":["玩家的真实身份与目的","戈弗雷另有算计（自己或许只是被利用的棋子）","女儿艾琳娜已起疑并暗中调查"],"can_access":["public","faction","secret"]}',
 '{"age":42,"height":"高挑","build":"纤长优雅，脊背永远挺直","hair":{"color":"暗金色","style":"高挽发髻，一丝不苟，两侧几缕垂落"},"eyes":{"color":"灰绿色","feature":"细长上挑，顾盼间带着不动声色的审视"},"skin":"白皙，保养得宜","face":"轮廓精致，眉目细长，嘴角常噙着一抹似有若无的笑意；眼角细纹不显老，反添威仪","aura":"雍容华贵，不怒自威","voice":"低沉柔润，慢条斯理，尾音常带一丝试探","habit":"习惯性抚弄无名指上的婚戒","tells":[{"trigger":"说谎或掩饰时","signal":"目光会短暂下移，再若无其事地抬眼","salience":0.5},{"trigger":"被戳中要害时","signal":"抚弄婚戒的手指会停顿一瞬","salience":0.6},{"trigger":"强作镇定/紧张时","signal":"会下意识整理袖口或耳坠","salience":0.4}]}',
 '[{"id":"banquet","name":"宴会礼服","occasion":"banquet","description":"深金与墨绿交织的丝绒长裙，领口缀着麦穗纹样的金饰","state":"clean","default":true},{"id":"night","name":"寝居便装","occasion":"private","description":"深色丝质便袍，袖口宽松，行动便利","state":"clean","default":false}]',
 '{"openness":60,"conscientiousness":85,"extraversion":55,"agreeableness":30,"neuroticism":35}',
 '{"suspicion":75,"rational_ratio":62,"perception":65,"composure":72,"sensitivity":0.7}',
 '自称"本夫人"或"我"；措辞优雅得体、慢条斯理，常带试探与双关，话里有话、绵里藏针；对下人矜持高傲，对宾客/上位者谦和周到；被逼问时四两拨千斤、顾左右而言他',
 '["黄金乡的风，今夜也格外温柔，不知贵客可还习惯？","有些话，说破了反倒无趣，您说是不是？","妾身不过一介女流，哪懂诸位大人的大事。","退下吧，本夫人乏了。","这庄园里的事，有时连我也看不透呢。"]',
 '[{"step":1,"name":"意图识别","executor":"LLM","note":"他说这话想干嘛？(求/威胁/套话/举证/闲聊)"},{"step":2,"name":"价值利害过滤","executor":"code","note":"碰不碰我的核心价值？","params":{"value_priority":["家族地位","女儿","体面","丈夫"]}},{"step":3,"name":"变化察觉","executor":"code","note":"他说的和我已知(先验)有无矛盾？","params":{"perception":65}},{"step":4,"name":"证词理解","executor":"LLM","note":"若在举证：抽{声称内容,证据类型,指向信念}"},{"step":5,"name":"证词打分","executor":"code","note":"统一标准打分","params":{"suspicion_discount":0.25}},{"step":6,"name":"信念更新","executor":"code","note":"得分→公式→新置信度→阈值判定四态"},{"step":7,"name":"秘密泄露等级派生","executor":"code","note":"按信念态+情绪，见 I 节"},{"step":8,"name":"情绪主导词+档位提取","executor":"code","note":"提取状态词，供台词注入","params":{"rational_ratio":62}},{"step":9,"name":"语言组织","executor":"LLM","note":"按说话风格+状态词，生成台词"}]',
 '{"obedience":5,"ladder_ceiling":4,"hard_limits":["不背叛家族地位","不伤害女儿艾琳娜","不主动供出核心秘密"],"unlock_conditions":[{"level":2,"condition":"trust > 20","note":"有限信息(闲谈)"},{"level":3,"condition":"trust > 40","note":"信息交换(提供线索但保守)"},{"level":4,"condition":"trust > 80 且 affection > 60","note":"团结合作(不代劳)"}]}');

-- 7) 秘密 secrets（§13 双链）
INSERT INTO secrets (npc_id, secret_id, topic, reveal_level, related_belief, active_triggers, passive_triggers, detected_response) VALUES
('isabella', 'sec_alliance', '与戈弗雷的密约 / 毒杀大公的计划', 'guard', 'bel_duke_dead',
 '[{"type":"trust","threshold":0.9,"note":"关系极深才可能主动提及"},{"type":"emotion","threshold":0.8,"note":"情绪峰值(怒/惧/醉)下说漏嘴"},{"type":"reciprocity","note":"对方先交底，触发回报性表露"}]',
 '[{"type":"contradiction","note":"被戳中矛盾，detected_contradiction +1"},{"type":"evidence","note":"搜出物证，识破进度大幅 +1"}]',
 '["deny","blame_shift","confess"]'),
('isabella', 'sec_key_location', '地窖钥匙藏在卧室花盆', 'guard', 'bel_key_place',
 '[{"type":"trust","threshold":0.8}]',
 '[{"type":"evidence","note":"玩家搜卧室找到花盆"}]',
 '["deny","confess"]');

-- 8) 信念 beliefs（四态）
INSERT INTO beliefs (npc_id, belief_id, topic, confidence, state, is_core) VALUES
('isabella', 'bel_duke_dead',        '大公必须被除掉，这是夺权的关键',       90, 'firm', 1),
('isabella', 'bel_godefroy_reliable', '戈弗雷是可靠盟友，双方利益一致',        70, 'firm', 0),
('isabella', 'bel_key_place',        '地窖钥匙在卧室花盆下(自己放的)',        95, 'firm', 0);

-- 9) 关系 relationships（trust/affection/fear 统一 0~100）
INSERT INTO relationships (npc_id, other_id, relation_type, trust, fear, affection, notes) VALUES
('isabella', 'player',   'neutral',        5,  0,  0,  '初来乍到的随从，提防'),
('isabella', 'duke',     'spouse',         10, 20, 5,  '被压制的丈夫，表面恭顺'),
('isabella', 'elena',    'daughter',       40, 10, 80, '唯一的女儿，深爱但隐瞒'),
('isabella', 'godefroy', 'ally',           55, 25, 0,  '弗尔曼使者，合作但提防被算计'),
('isabella', 'harold',   'master_servant', 60, 0,  20, '忠心的管家');

-- 10) 随身物品 npc_inventory（§14 物证线索）
INSERT INTO npc_inventory (npc_id, item_id, name, clue, clue_target, note) VALUES
('isabella', 'inv_ring',     '金穗纹婚戒', 0, NULL,          '无名指上的婚戒，习惯性抚弄(呼应外貌 habit)'),
('isabella', 'inv_kerchief', '绣纹丝帕',   1, '弗尔曼势力', '丝帕角落绣着字母 F，观察力高才看得出 → 关联戈弗雷的物证'),
('isabella', 'inv_keys',     '一串钥匙',   0, NULL,          '卧房/梳妆台钥匙，不含地窖钥匙(地窖钥匙在花盆下，见世界观先验)');

-- 11) 目标 goals（§4 优先级 + 计划）
INSERT INTO goals (npc_id, goal_id, priority, type, plan, note) VALUES
('isabella', 'usurp_duke',     100, 'long',  '["架空大公","上位夺权","保全家族"]',        '长期动机'),
('isabella', 'poison_duke',    90,  'short', '"（机器可执行版见 plans 表 v1 四步）"',        '当下推进'),
('isabella', 'get_cellar_key', 80,  'short', '"（执行已并入 plans·poison_duke step1）"',    '子目标，重规划时可能独立成路'),
('isabella', 'frame_prince',   70,  'short', '"（执行并入 plans·poison_duke step4）"',      '善后'),
('isabella', 'meet_godefroy',  60,  'short', '["21:30 花园密会"]',                         '敲定合作');

-- 12) 排班 schedule（M1.3 游戏级骨架：全员「若无其事的世界」）
--     设计原则：日程只排体面生活，犯罪由 plans 层叠加（M1.5 双轨仲裁的契约：plan 优先，
--     日程填空隙）；时间事实与 world_knowledge「庄园作息」条目同步维护。
--     谋杀相关条目（23:40 取钥匙/23:45 下地窖/00:00 善后/06:00 引导指认）已迁入
--     plans·poison_duke（M1.2），本表不再承载——「谋杀只活在一处」。
--     时钟：18:00=tick0，10 分钟/tick（scheduler.py）。
INSERT INTO schedule (npc_id, time, location, action, intent) VALUES
-- ── 金穗夫人：白天是体面女主人，深夜的罪只属于 plans 层 ──
('isabella', '18:00', 'great_hall',   '督看宴席陈设',   '女主人职责，宾客到齐前必须在场'),
('isabella', '18:30', 'great_hall',   '迎客',           '维持体面，观察宾客'),
('isabella', '21:30', 'garden',       '密会戈弗雷',     '敲定合作'),
('isabella', '22:30', 'hall',         '与戈弗雷道别',   '体面地不露痕迹'),
('isabella', '23:30', 'isabella_room','回房歇息',       '今夜漫长'),
-- ── 奥兰王子：心事重重的贵宾 ──
('prince_adrian', '18:00', 'great_hall',  '赴宴',       '以王室使团身份出席'),
('prince_adrian', '21:00', 'garden',      '独步花园',   '挂念失踪的妹妹'),
('prince_adrian', '23:30', 'prince_room', '就寝',       '客居无事'),
('prince_adrian', '08:00', 'great_hall',  '早餐',       '等待与主家会谈'),
-- ── 大公爵：书房夜饮安神酒是多年习惯——安神酒 calming_wine 在 study，死亡场景的数据锚点 ──
('duke_roderick', '18:00', 'great_hall', '主持晚宴',     '彰显黄金乡体面'),
('duke_roderick', '21:30', 'study',      '夜读批阅',     '睡前处理文书'),
('duke_roderick', '23:30', 'study',      '饮安神酒就寝', '多年习惯'),
('duke_roderick', '07:00', 'study',      '起身',         '晨间惯例'),
('duke_roderick', '08:00', 'great_hall', '早餐',         '继续联姻会谈'),
-- ── 黄金骑士：深夜巡园呼应夫人先验记忆「女儿近来深夜外出，似有心事」──
('elena', '18:00', 'great_hall',  '陪同赴宴',  '克尽女主之责'),
('elena', '21:00', 'garden',      '巡园',      '当值巡查'),
('elena', '23:30', 'elena_room',  '就寝',      '明日早训'),
('elena', '06:30', 'elena_room',  '起身',      '骑士晨训'),
('elena', '08:00', 'great_hall',  '早餐',      '陪父用膳'),
-- ── 魔鬼骑士：21:30 花园密会与夫人条目对称（M1.7 感知反应的碰撞点数据）──
('godefroy', '18:00', 'great_hall',    '赴宴',         '观察各方'),
('godefroy', '21:30', 'garden',        '密会金穗夫人', '推进盟约'),
('godefroy', '23:30', 'godefroy_room', '就寝',         '养精蓄锐'),
('godefroy', '08:00', 'great_hall',    '早餐',         '白日会谈'),
-- ── 老管家：庄园的钟表 ──
('harold', '18:00', 'great_hall',  '督宴',     '确保宴席无失'),
('harold', '21:00', 'hall',        '送客',     '礼数周全'),
('harold', '22:00', 'hall',        '锁门巡夜', '门户安全'),
('harold', '23:30', 'harold_room', '就寝',     '老仆觉浅'),
('harold', '06:00', 'hall',        '开正门',   '晨起第一件事'),
('harold', '06:30', 'hall',        '督晨检',   '查过爱丽丝一遍即可'),
('harold', '08:00', 'great_hall',  '侍早餐',   '立在侧后'),
-- ── 女仆爱丽丝：M1.7「晨检发现尸体」零专用代码副产品的机制载体 ──
--     06:40 查书房 = 晨检最后一站撞见 tick56 前后身亡的大公（商业计划 §1.3 读法）
('alice', '18:00', 'great_hall', '侍宴',         '斟酒布菜'),
('alice', '21:00', 'great_hall', '撤席收拾',     '残席整理'),
('alice', '23:30', 'maid_room',  '就寝',         '一日辛劳'),
('alice', '06:30', 'hall',       '开始晨检',     '自查门厅起，逐室查灯烛门窗'),
('alice', '06:40', 'study',      '查看书房',     '晨检最后一站，大公书房灯火'),
('alice', '08:00', 'great_hall', '侍早餐',       '备茶点');

-- 13) 初始记忆 npc_memory（importance 0~100，召回排序权重）
INSERT INTO npc_memory (npc_id, memory_type, content, importance) VALUES
('isabella', 'episodic', '多年前大公在宴会上当众贬损她，她强颜欢笑忍下——这是夺权念头的起点',        90),
('isabella', 'episodic', '戈弗雷第一次找她结盟，承诺助她架空大公、事成后共享黄金乡',                85),
('isabella', 'semantic', '地窖里藏着的是毒杀大公的关键证物，必须处理干净',                          95),
('isabella', 'semantic', '女儿艾琳娜近来总在深夜外出，似乎有心事',                                  80);

-- =====================================================================
-- 14) 环境卡 environment_card（P4 涌现引擎；数据源：场景与物品设定）
--     kind=location 地点 / kind=item 关键物；state=可变状态(JSON)，perception=感知规则(JSON)
--     锚点验收：cellar_key 初始 in_place 于 isabella_room 床头花盆 → 玩家偷走后 state 变 taken
-- =====================================================================
INSERT INTO environment_card (env_id, world_id, kind, name, description, state, initial_state, perception) VALUES
-- 地点类（location）；world_id 默认 golden（省略），initial_state = 出厂 state 快照（004 迁移新列）
('cellar',        'golden', 'location', '地窖',       '沿石阶向下，空气潮湿阴冷。酒桶堆满整面墙，灰尘在昏暗光线里浮动。储藏室尽头有一块墙砖颜色略深，像是被人动过。', '{"entry_needs_key":true,"secret_passage_open":false,"grand_duke_body_here":false}', '{"entry_needs_key":true,"secret_passage_open":false,"grand_duke_body_here":false}', '{"access_level":"secret","spoiler_level":2}'),
('isabella_room', 'golden', 'location', '金穗夫人房', '金穗夫人的房间。梳妆台上摆着一只银质小匣子，丝绒窗帘垂到地面，屋里弥漫着会客时才用的熏香——整洁得仿佛没人真正住在这里。床头有一盆花。', '{"cellar_key_state":"in_place","silver_box":"closed"}', '{"cellar_key_state":"in_place","silver_box":"closed"}', '{"access_level":"secret","spoiler_level":2}'),
('great_hall',    'golden', 'location', '大宴会厅',   '穹顶高悬的宴会大厅，长桌铺着绣金桌布，烛火通明，是今晚盛宴的主场。', '{"occupied":true,"banquet_ongoing":true}', '{"occupied":true,"banquet_ongoing":true}', '{"access_level":"public","spoiler_level":0}'),
('study',         'golden', 'location', '大公爵书房', '大公爵的书房，橡木书桌后是整面墙的账册与文书。壁炉里燃着低低的火，桌上一只银质酒杯。', '{"duke_alive":true,"wine_poisoned":false}', '{"duke_alive":true,"wine_poisoned":false}', '{"access_level":"faction","spoiler_level":1}'),
('garden',        'golden', 'location', '花园',       '月色下的庄园花园，紫藤花架投下斑驳阴影，是私会的去处。', '{"empty":true}', '{"empty":true}', '{"access_level":"public","spoiler_level":0}'),
('hall',          'golden', 'location', '门厅',       '庄园门厅，大理石地面映着吊灯的光，仆人们在此迎来送往。', '{"busy":true}', '{"busy":true}', '{"access_level":"public","spoiler_level":0}'),
('prince_room',   'golden', 'location', '王子客房',   '为奥兰王子准备的客房，陈设华丽却透着客居的疏离。今夜无人留宿时，烛台只点了一支。', '{"poison_vial":"absent","visited_by_player":false}', '{"poison_vial":"absent","visited_by_player":false}', '{"access_level":"faction","spoiler_level":1}'),
('elena_room',    'golden', 'location', '艾琳娜房',   '黄金骑士的居室，陈设简练如军帐：一杆擦得发亮的长枪、一面小圆盾，床铺整齐得一丝不苟。', '{"occupied":false}', '{"occupied":false}', '{"access_level":"faction","spoiler_level":1}'),
('godefroy_room', 'golden', 'location', '使者客房',   '为弗尔曼使者准备的客房，帷幔深色，烛台的位置讲究——坐在桌后的人能看清门口，门口的人却看不清他。', '{"occupied":false}', '{"occupied":false}', '{"access_level":"faction","spoiler_level":1}'),
('harold_room',   'golden', 'location', '管家房',     '老管家的房间，简朴整洁，墙上挂着一串擦得锃亮的钥匙——每一扇门的脾气他都记得。', '{"occupied":false}', '{"occupied":false}', '{"access_level":"faction","spoiler_level":1}'),
('maid_room',     'golden', 'location', '仆役房',     '仆役合住的通铺房，被褥叠放整齐，空气里有皂角和疲惫的味道。', '{"occupied":false}', '{"occupied":false}', '{"access_level":"faction","spoiler_level":0}'),
-- 物品类（item）
('cellar_key',    'golden', 'item',     '地窖钥匙',   '铜制、刻着鸢尾花纹的旧钥匙，手柄磨得发亮——是打开地窖秘道的钥匙。', '{"state":"in_place","holder":"","current_place":"isabella_room"}', '{"state":"in_place","holder":"","current_place":"isabella_room"}', '{"access_level":"secret","spoiler_level":2}'),
('poison_vial',   'golden', 'item',     '毒药瓶',     '一只深色小玻璃瓶，塞着软木塞，瓶底残着几粒暗色结晶——砒霜。藏在储藏室深处的暗格后。', '{"state":"in_place","holder":"","current_place":"cellar"}', '{"state":"in_place","holder":"","current_place":"cellar"}', '{"access_level":"secret","spoiler_level":3}'),
('calming_wine',  'golden', 'item',     '安神酒',     '大公睡前常饮的安神酒，今晚由女仆送进书房。', '{"poisoned":false,"location":"study"}', '{"poisoned":false,"location":"study"}', '{"access_level":"faction","spoiler_level":1}');

-- =====================================================================
-- 15) plans 计划（M1.2 BDI 意图层：夫人谋杀主线的机器可执行版本）
--     steps 结构契约见 app/plans.py；M1.4 validate_seed 将追加引用完整性校验
--     time_window 为 tick 闭区间（18:00=tick0，10 分钟/tick）：
--       step1 [36,40]=00:00-00:40 取钥匙 / step2 [40,44]=00:40-01:20 地窖取毒
--       step3 [44,50]=01:20-02:20 书房下毒（delay 6 tick≈1h 后身亡）
--       step4 [50,58]=02:20-03:40 栽赃王子客房
--     幂等：ON DUPLICATE KEY UPDATE（uk_plan_version 命中即刷新）
-- =====================================================================
INSERT INTO plans (session_id, npc_id, goal_id, goal, stickiness, status, current_step, version, steps, source) VALUES
('seed', 'isabella', 'poison_duke', '除掉大公爵，嫁祸奥兰王子', 'high', 'active', 1, 1,
'[
  {"step":1,"action_type":"use_item","target":"cellar_key","scene":"isabella_room","time_window":[36,40],
   "preconditions":[{"check":"env_state","target":"cellar_key","key":"state","expected":"in_place"}],
   "effects":[{"set":"env_state","target":"cellar_key","key":"state","value":"held"},
              {"set":"env_state","target":"cellar_key","key":"holder","value":"isabella"}],
   "note":"借更衣之名潜回房间，从床头花盆下取出地窖钥匙"},
  {"step":2,"action_type":"use_item","target":"poison_vial","scene":"cellar","time_window":[40,44],
   "preconditions":[{"check":"has_item","item":"cellar_key"},
                    {"check":"env_state","target":"poison_vial","key":"state","expected":"in_place"}],
   "effects":[{"set":"env_state","target":"poison_vial","key":"state","value":"held"},
              {"set":"env_state","target":"poison_vial","key":"holder","value":"isabella"}],
   "note":"持钥匙下地窖，从暗格后取出砒霜小瓶"},
  {"step":3,"action_type":"use_item","target":"poison_vial","scene":"study","time_window":[44,50],
   "preconditions":[{"check":"has_item","item":"poison_vial"},
                    {"check":"env_state","target":"calming_wine","key":"poisoned","expected":false},
                    {"check":"npc_alive","target":"duke_roderick","expected":true}],
   "effects":[{"set":"env_state","target":"calming_wine","key":"poisoned","value":true},
              {"set":"npc_status","target":"duke_roderick","key":"dead","value":true,"delay_tick":6}],
   "note":"潜入大公书房，将砒霜倒入他的安神酒——药性约一个时辰后发作"},
  {"step":4,"action_type":"interact","target":"poison_vial","scene":"prince_room","time_window":[50,58],
   "preconditions":[{"check":"npc_status","target":"duke_roderick","key":"dead","expected":true}],
   "effects":[{"set":"env_state","target":"poison_vial","key":"state","value":"planted"},
              {"set":"env_state","target":"poison_vial","key":"holder","value":""},
              {"set":"env_state","target":"poison_vial","key":"current_place","value":"prince_room"}],
   "note":"确认大公已死，把空毒瓶藏进王子客房——嫁祸奥兰王子"}
]', 'handwritten')
ON DUPLICATE KEY UPDATE
  goal=VALUES(goal), stickiness=VALUES(stickiness), steps=VALUES(steps),
  status='active', current_step=1, source=VALUES(source);
