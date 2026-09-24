extends Node
## GameState —— 全局世界状态（autoload 单例，跨界面常驻）
## 类比后端 main.py 里的 llm=DeepSeekClient() 单例思想：全局只有一份，人人都能访问。
## 字段对齐游戏机制：地点 / 行动点 / 背包 / 线索 / 声望。（时段由 Clock 单例管理）

## 地点 / 行动点变化时通知 UI（解耦：状态变化 → UI 自己刷新）
signal location_changed(location_id: String)
signal action_points_changed(points: int)
signal items_changed(items: Array[String])   # 背包道具变化时通知 UI（如拿刀/放下后）

## 当前世界 id（决定加载哪一套数据目录）：
##   "golden" = 黄金乡（正式剧情，data/locations/ + data/npcs/）
##   "test"   = 测试世界（data/test_locations/ + data/test_npcs/）
## 由主菜单在选择"模式"时写入，main.gd 据此切换数据源。
var world_id: String = "golden"

## 后端会话 id：游戏启动时经 POST /session/start 获取（M1.1）。此后 /chat、
## /session/location 都带它，让记忆/关系/玩家位置按会话隔离。
## 空串 = 会话尚未建立（启动瞬间），后端会自动兜底 adhoc，不阻塞游戏。
var session_id: String = ""

## 当前地点 id（对应对应世界数据目录下文件名，如黄金乡 "front_yard" / 测试世界 "room_1"）
var location_id: String = "front_yard"

## 开局初始场景（09-09 模组化：不再用 world_id 写死判断）。
## 由 loading 的 /world/info 响应（initial_scene 字段）写入；在此仅给黄金乡的保守默认，
## 避免 `/world/info` 尚未返回时就用错地点发 /scene/inspect。换模组=改 manifest.json，不改代码。
var initial_scene: String = "front_yard"
## 行动点：第 1 天约 25 点（从下午开始）→ 第 2 天 100 点。每做一件事耗 1 点。
var action_points: int = 25
## 背包道具（可从 NPC 交互获得，可作证据/特殊使用）
var items: Array[String] = []
## 线索（玩家推理拼图）
var clues: Array[String] = []
## 声望（中立=0，倾向某一方会变）
var reputation: int = 0

## 世界时钟（在线驱动）：玩家已见到的后端 tick（拉世界变化增量的游标）与当前 tick。
## 玩家交互后后端自动 +1 并驱动所有 NPC；前端用 last_seen_tick 拉 /world/updates
## 增量，把"挂机期间世界发生了什么"渲染成提示。
var last_seen_tick: int = -1
var current_tick: int = 0

## 开局场景数据缓存（09-09 需求"场景信息都显示出来才进页面"）。
## loading 界面在等待后端就绪时，会先拉一次 /scene/inspect 并把首个完整响应存到这里；
## main 场景进入时优先用这份缓存立即渲染（旁白/入口一次成型），而不是等异步请求回来才补——
## 否则会出现"进页面只有分隔线、旁白后补、入口后冒"的空窗。用过即清空，避免后续误用旧地点。
var pending_scene_inspect: Dictionary = {}

## 切换地点：改 location_id 并广播（单屏机制下只是改状态 + 刷新界面，不是换场景）
func switch_location(new_id: String) -> void:
	location_id = new_id
	location_changed.emit(location_id)

## 消耗行动点：够则扣并返回 true，不够返回 false（调用方据此提示"行动点不足"）
func spend_action(points: int = 1) -> bool:
	if action_points >= points:
		action_points -= points
		action_points_changed.emit(action_points)
		return true
	return false

## 收集道具（去重）
func add_item(item: String) -> void:
	if item not in items:
		items.append(item)

## 用后端返回的持有物整体替换本地背包（背包系统的数据源在后端，客户端只做展示层）。
## 后端 GET /session/inventory 返回"玩家身上当前拿着哪些物品"，这里按名字刷新。
## item_names: 后端 items[].name 列表（如 ["一把刀"]）。去重 + 排序 + 广播。
func set_items(item_names: Array) -> void:
	var seen: Array[String] = []
	for n in item_names:
		var s := String(n).strip_edges()
		if s != "" and s not in seen:
			seen.append(s)
	seen.sort()
	items = seen
	items_changed.emit(items)

## 记录线索（去重）
func add_clue(clue: String) -> void:
	if clue not in clues:
		clues.append(clue)

## ⚠️ Belief 原则（设计文档第6章）：NPC 非全知，只感知"他该知道/能看到"的事，
## 绝不能拿到全知视角（否则=剧透=OOC）。P4 后端据此组装上下文，避免 NPC 说漏不该说的。
## present_npc_ids：当前地点在场的其他 NPC id（由 main.gd 从地点卡查得后传入，避免本单例依赖数据层）。
## 返回：某个 NPC 感知范围内的世界状态快照。
func snapshot_for_npc(npc_id: String, present_npc_ids: Array = []) -> Dictionary:
	return {
		"player_id": "companion",             # 玩家身份：王子随从
		"npc_id": npc_id,                     # 当前对话的 NPC
		"location_id": location_id,           # 玩家当前地点
		"time_slot": Clock.current_slot(),    # 当前时段（时钟单例）
		"action_points": action_points,       # 玩家剩余行动点（NPC 能感知玩家"忙不忙/急不急"）
		"present_npc_ids": present_npc_ids,   # 此时此地在场的 NPC
		"player_items": items.duplicate(),    # 玩家携带的道具（NPC 可见部分）
		"player_clues": clues.duplicate(),    # 玩家已掌握的线索（NPC 可见部分）
		"reputation": reputation,             # 玩家声望（NPC 对玩家的立场/好感）
	}
