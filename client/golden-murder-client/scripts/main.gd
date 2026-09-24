extends Control
## Main —— 单屏主场景控制（呈现世界 / 承载对话 / 同步状态）
## 本文件是 Godot 端"三件事"的落点：把 UI 与 GameState / Clock / ApiClient 连起来。
## 注意：现在后端(P4)未通，对话回复用"本地演示"顶替，保证整条交互链路可跑；
##       P4 接上 ApiClient 后，把 send_chat 的实现替换即可。

## ---------- 节点引用（严格对应 main.tscn 的节点路径） ----------
@onready var time_label: Label = $TimeLabel
@onready var loc_label: Label = $LocLabel
@onready var left_vbox: VBoxContainer = $LeftPanel/LeftVBox
@onready var name_label: Label = $NameCard/NameLabel
@onready var status_label: Label = $StatusCard/StatusLabel
@onready var status_card: PanelContainer = $StatusCard
@onready var dialogue_log: VBoxContainer = $RightPanel/Container/RightVBox/DialogueScroll/LogVBox
@onready var option_box: VBoxContainer = $RightPanel/Container/RightVBox/OptionBox
@onready var timer_bar: ProgressBar = $TimerBar
@onready var input: LineEdit = $InputBox/Input
@onready var send_btn: Button = $SendBtn
@onready var interact_btn: Button = $InteractBtn
@onready var bag_btn: Button = $NavBar/ActionBar/BagBtn
@onready var map_btn: Button = $NavBar/ActionBar/MapBtn
@onready var log_btn: Button = $NavBar/ActionBar/LogBtn
@onready var link_btn: Button = $NavBar/ActionBar/LinkBtn
@onready var ap_label: Label = $APLabel
@onready var ap_bar: ProgressBar = $APBar

## ---------- 数据 ----------
## 地点表（按 world_id 从对应目录运行时加载，方便调参）
var locations: Dictionary = {}
## 人物卡表（按 world_id 从对应目录运行时加载）。台词/状态/选项全部由人物卡提供，不再写死在脚本里。
var npcs: Dictionary = {}

## 数据目录按世界分流（黄金乡 = 正式剧情 / 测试世界 = 引擎自检台）
const DIR_GOLDEN_LOCATIONS := "res://data/locations"
const DIR_GOLDEN_NPCS := "res://data/npcs"
const DIR_TEST_LOCATIONS := "res://data/test_locations"
const DIR_TEST_NPCS := "res://data/test_npcs"

## 世界 → 数据目录映射：main 场景启动时按 GameState.world_id 取定当前世界用哪套目录。
func _world_dirs() -> Dictionary:
	match GameState.world_id:
		"test":
			return {"locations": DIR_TEST_LOCATIONS, "npcs": DIR_TEST_NPCS}
		_:
			return {"locations": DIR_GOLDEN_LOCATIONS, "npcs": DIR_GOLDEN_NPCS}

## 单轮对话倒计时总时长（秒）
const DIALOGUE_SECONDS: float = 60.0

## ---------- UI 常量（统一字号口径，避免散落的 magic number） ----------
const FONT_SIZE_ACTION: int = 22   # 按钮/条目字号
const FONT_SIZE_LEFT: int = 24     # 左面板正文字号

## ---------- 运行时状态 ----------
var current_npc: String = ""       # 当前对话的 NPC id（"" 表示不在对话中）
var in_dialogue: bool = false
## 本轮对话还剩下的秒数（从 DIALOGUE_SECONDS 递减到 0，驱动进度条从左到右消失）
var countdown_remaining: float = 0.0
## 倒计时是否正在走（用 _process 逐帧连续递减，而非 Timer 滴答）
var countdown_running: bool = false
var active_tab: String = "scene"   # scene/backpack/map/log/link
## D（全同步时序）：世界结算期间锁住输入/移动（等全部 NPC 动完才放行下一发）
var _busy: bool = false
## 09-10：即时提示轮询是否在跑（防重复起多个 await 循环 / 防重复请求）
var _notice_polling: bool = false
## 09-08 问题2：略过时间后需以"停留"视角重新描述场景。_on_skip 置 true，
## _on_world_updates 拿到世界变化后重述一次场景并复位。
var _skip_reflow_scene: bool = false
## 09-08 会话补拉：_inspect_mode 控制 _on_scene_inspect 是否注入旁白。
## "full"=完整展示（分隔符+旁白+入口）；"entries_only"=仅刷新交谈入口（会话建立后补拉时不重复旁白）。
var _inspect_mode: String = "full"
## 当前场景已生成的交谈入口按钮（_clear_scene_entries 清旧，避免补拉/重看后入口堆积）。
var _scene_entries: Array = []
## 09-09 问题3/4：略过需以"停留"视角重述场景（其中走 LLM，30s+）。此标志表示"本次略过的
## 忙碌状态要一直保持到场景重述完整落地（_on_scene_inspect 的 full 渲染）才解除"——否则
## world_step_done 一返回就解锁（截图时刻3），而场景内容要等 LLM 才出现（截图时刻4），
## 造成"解锁过早、界面先亮内容后到"。true 时 world_step_done 不解锁，等重述完再解锁。
var _skip_reflow_busy: bool = false
## ---------- 对话系统 v0.4 会话状态（区别于上面的"浏览对话"in_dialogue）----------
## 后端多轮对话会话进行中：玩家每句经 /conversation/turn，NPC 流式回一句，至多 10 句/1 tick。
## 进入会话已耗 1 tick；会话期间 world 被 active_conv 挂起，不再额外推进。
var _conv_active: bool = false
var _conv_npc: String = ""
## ---------- 对话结束回场景汇合（09-09 第4步：等"场景重述 + 那一个tick世界增量"都到位才解锁）----------
## 后台进程B在对话期间结算「那一个tick」。对话结束回场景时，除重述场景外，还要把该tick的世界变化
## 增量（/world/updates）放进右框；若该tick后台还没结算完，前端保持忙碌并轮询，两者都到位才解锁。
var _end_conv_pending: bool = false      # 对话结束回场景流程进行中
var _end_conv_scene_done: bool = false   # 场景重述（/scene/inspect）已完成
var _end_conv_world_done: bool = false   # 那一个tick世界增量（/world/updates）已到位
var _end_conv_target_tick: int = -1      # 对话前玩家已见的tick（判断后台该tick是否已算完）
var _end_conv_polling: bool = false      # 防止世界增量轮询定时器叠加
## 发起对话那一刻的 tick（后端 /conversation/start 或 /conversation/accept 返回的 conv_tick）。
## 结束对话回场景拉 /world/updates 时以 _conv_start_tick-1 为 since 基准，只拉【这一格】的世界
## 增量（治「A/B/C 堆叠」——对话前多个 tick 的痕迹不再一次性混入右框）。
var _conv_start_tick: int = -1
## 后端已预生成的"结束对话回场景"专属转场旁白（空=需前端兜底现调 /scene/inspect view=conv_end）。
var _conv_end_text: String = ""
## ---------- NPC 对话邀请：内嵌按钮的持有引用（09-10 用户拍板改版）----------
## 为什么用【变量引用】而不是按名字查找节点：程序动态建的 UI，一旦按 get_node("XXX") 反查，
## 就会踩 queue_free() 的坑——queue_free 是【延迟】释放，同一帧内"先建后建"会撞名，
## Godot 自动给新节点改名（@XXX@2），之后按原名再也找不到它 → 覆盖层永久留在屏幕上。
## 现场症状正是"点了婉拒，这个页面还在上面"。改成引用持有后，这类静默 bug 从根上消失。
## 09-10 用户拍板改版（多人邀请）：待裁决的可能是【一组】邀请——同一 tick 里好几个 NPC 都想跟
## 玩家说话。前端呈现为"一条邀请提示 + N 个（名字：第一句话）选项 + 底部一个「婉拒对话」"。
## _pending_invite_key = 本组邀请的【邀请人 id 集合指纹】，作去重锚点：同一个邀请会从三条来路
## 到达前端（/chat 响应、/world/updates 轮询、结算后补查）。旧版只按"第一个发起人 id"去重，
## 多人邀请时 offer 顺序一变就会重复渲染出好几组按钮。
var _pending_invite_key: String = ""     # 当前待裁决【整组】邀请的指纹（去重锚点，空=无待裁决）
var _pending_invite_npc: String = ""     # 本组第一个邀请人（记 _conv_npc / 文案兜底用）
var _pending_invite_row: Node = null     # 内嵌按钮那一块（裁决后整块摘掉，消息本身留在记录里）
## 选项按钮里第一句话的截断长度：Godot Button 会按文本撑高，N 个长句会把右框顶满，
## 故按钮内只留前 24 个字（悬停 tooltip 给全文；真正选中的那位，进对话时会展示完整第一句）。
const INVITE_PREVIEW_CHARS := 24
## ---------- 世界结算提示（09-08 用户拍板·同步回归）----------
## 后端 /chat 已改回【同步 advance_one】：阻塞至本 tick 全部 NPC 行动+反应结算完成才返回。
## 前端玩家提交行动后显示"命运的齿轮开始转动"，同步等待；后端返回时若走了导演判定
## （多人同场景），再补"命运的齿轮在咬合中再次转动"并与导演叙述一起展示——纯同步、直觉、
## 不重复触发。旧版"异步+轮询"已否决删除（会导致重复触发/割裂）。

func _ready() -> void:
	# 起点地点：读 GameState.initial_scene（09-09 模组化，不再按 world_id 写死）。
	# loading 已把 /world/info 的 initial_scene 回填到这里；此处仅做兜底（若为空保持当前值）。
	if String(GameState.initial_scene).strip_edges() != "":
		GameState.location_id = GameState.initial_scene
	load_locations()
	load_npcs()
	# 绑定信号（与状态单例解耦）
	GameState.location_changed.connect(_on_location_changed)
	GameState.action_points_changed.connect(_on_action_points_changed)
	GameState.items_changed.connect(_on_items_changed)   # 背包持有物变化时刷新
	send_btn.pressed.connect(_on_send)
	input.text_submitted.connect(func(_t): _on_send())
	interact_btn.pressed.connect(_on_interact_btn)
	interact_btn.text = "略过"
	bag_btn.pressed.connect(_on_tab.bind("backpack"))
	map_btn.pressed.connect(_on_tab.bind("map"))
	log_btn.pressed.connect(_on_tab.bind("log"))
	# 调试面板（原"联络"占位 tab 改造）：上帝视角逐 tick 看 NPC 的得知/提示词/行动/输出/环境影响/耗时
	link_btn.text = "调试"
	link_btn.pressed.connect(_on_tab.bind("link"))
	ApiClient.world_updates_received.connect(_on_world_updates)
	ApiClient.debug_overview_received.connect(_on_debug_overview)
	# 09-10：后端"正在忙什么"的即时提示（如解析层触发重试的"事情比想象中复杂……"）
	ApiClient.notices_received.connect(_on_notices_received)
	# A：进房间拉动态感知（替代本地静态 desc）；D：手动推 tick 结算后解锁输入/移动
	ApiClient.scene_inspect_received.connect(_on_scene_inspect)
	# 会话建立成功（真实 session_id 就绪）→ _on_session_created 补拉当前场景。
	ApiClient.session_created.connect(_on_session_created)
	# 玩家【主动】发起对话：成功→进会话；被拒→显示婉拒（均来自 /conversation/start）。
	ApiClient.conversation_started.connect(_on_conversation_started)
	ApiClient.conversation_start_rejected.connect(_on_conversation_start_rejected)
	ApiClient.world_step_done.connect(func():
		# 09-09 问题3/4：略过引发的场景重述在 tick 结算"之后"才做（/scene/inspect 走 LLM 30s+）。
		# 若此刻在等待重述（_skip_reflow_busy），先不解锁——否则世界一结算完界面就先亮（时刻3），
		# 而重述内容要等 LLM 才出现（时刻4），造成"解锁过早、内容后到"。等重述落地再解锁。
		if not _skip_reflow_busy:
			_set_busy(false)
		_query_conversation_invite()   # 手动推/略过结算完，查是否有 NPC 想邀玩家对话
	)
	# 09-09 问题3/4 兜底：重述场景的 /scene/inspect 若失败/超时（不再回调 _on_scene_inspect），
	# 且此刻在等待重述，也要解锁，避免略过的忙碌永久卡住（异常路径不阻塞玩家）。
	ApiClient.scene_inspect_failed.connect(func():
		if _skip_reflow_busy:
			_skip_reflow_busy = false
			_set_busy(false)
			_query_conversation_invite()
	)
	# D：手动/略过推 tick 后，若后端"没推进"（达时间上限/上一 tick 仍结算）→ 明确告知，
	# 避免"扣了行动点却没动静"的错觉（问题3）。
	ApiClient.world_step_advanced.connect(func(advanced: bool, _cur_tick: int):
		if not advanced:
			add_message("系统", "这一刻世界似乎没有变化……", false)
	)
	# 方向1（点地图移动）：收到 /session/move 结算结果 → 切地点 + 拉世界变化 + 解锁
	ApiClient.move_received.connect(_on_move_received)
	# 初始化界面：先展示场景（分隔符+场景描述+人物入口），再补一段序言开场白
	_refresh_topbar()
	_refresh_actions()
	show_location(GameState.location_id)
	# 09-09：进入本场景时由 _show_scene_in_location 统一兜底——它内部已是"缓存优先（用
	# loading 就绪的完整场景数据一次成型渲染）→ 会话就绪则发起 /scene/inspect → 会话未就绪
	# 则只落分隔符、由下方 ensure_backend_ready 建会话后经 session_created 信号兜底"。因此
	# 这里不再主动调 _on_session_created（避免与缓存/请求时序重复）。
	# 09-09：开场白（/world/info 的 entry）已移到【待机加载界面 loading.tscn】中间显示，
	# 不再注入正式游戏右侧聊天框。此处仅保留【幂等】的后端兜底探测/建会话：
	# loading 场景完成后 _backend_ready 已为 true，这里会短路；万一直接进入本场景
	# （跳过 loading），也能自动拉起后端、建会话，不裸奔。
	ApiClient.ensure_backend_ready()   # 幂等：已就绪则直接返回，不重复探测/重启
	# P3-A 第③步：连接后端回复信号，收到 AI 回复就调用 _on_ai_reply（成功/兜底都走它）
	ApiClient.reply_received.connect(_on_ai_reply)
	ApiClient.reply_fallback.connect(_on_ai_reply)
	# 对话系统 v0.4：邀请 → 同意/拒绝 → 多轮逐句 → 结束
	ApiClient.conversation_invite_received.connect(_on_conversation_invite)
	# 09-10 修永久死锁：/chat 响应里带回"世界被对话邀请挂起 / 上一格还在结算"→ 弹邀请/明确提示
	ApiClient.world_paused_received.connect(_on_world_paused)
	ApiClient.conversation_accepted.connect(_on_conversation_accepted)
	ApiClient.conversation_declined.connect(_on_conversation_declined)
	ApiClient.conversation_turn_received.connect(_on_conversation_turn_received)
	ApiClient.conversation_ended.connect(_on_conversation_ended)

	# 左面板加滚动（用户反馈：调试面板内容超出面板底部且无法上下滑动）——
	# 把 LeftVBox 装进 ScrollContainer：纵向可滑、横向禁用（强制子节点宽度=面板宽，
	# 自动换行的 Label 才能正确折行不溢出）。
	var left_scroll := ScrollContainer.new()
	left_scroll.name = "LeftScroll"
	left_scroll.horizontal_scroll_mode = ScrollContainer.SCROLL_MODE_DISABLED
	left_scroll.vertical_scroll_mode = ScrollContainer.SCROLL_MODE_SHOW_ALWAYS
	left_scroll.size_flags_vertical = Control.SIZE_EXPAND_FILL
	left_vbox.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	left_vbox.size_flags_vertical = Control.SIZE_SHRINK_BEGIN
	var left_container := left_vbox.get_parent()
	left_container.remove_child(left_vbox)
	left_container.add_child(left_scroll)
	left_scroll.add_child(left_vbox)

	
## 加载地点卡：扫描当前世界的数据目录，一个地点一个 .json 文件。
## 文件名（不含扩展名）即该地点的 id。
func load_locations() -> void:
	locations = _load_data_dir(_world_dirs()["locations"])

## 通用：扫描一个数据目录，把 dir 下所有 .json 读成 {文件名(去后缀): dict}，键为实体 id。
## 这样"新增一个实体" = 在目录里新建一个 .json，无需改代码、无需改其它数据。
func _load_data_dir(dir_path: String) -> Dictionary:
	var result: Dictionary = {}
	var dir := DirAccess.open(dir_path)
	if dir == null:
		push_error("无法打开目录 " + dir_path + "：" + str(DirAccess.get_open_error()))
		return result
	dir.list_dir_begin()
	var fname := dir.get_next()
	while fname != "":
		if not dir.current_is_dir() and fname.ends_with(".json"):
			var id := fname.get_basename()
			var file := FileAccess.open(dir_path + "/" + fname, FileAccess.READ)
			if file == null:
				push_error("无法读取 " + dir_path + "/" + fname + "：" + str(FileAccess.get_open_error()))
			else:
				var json_data: Variant = JSON.parse_string(file.get_as_text())
				if json_data is Dictionary:
					result[id] = json_data
				else:
					push_error(dir_path + "/" + fname + " 解析失败")
		fname = dir.get_next()
	dir.list_dir_end()
	return result

## 加载人物卡：扫描当前世界的数据目录，一个 NPC 一个 .json 文件。
## 文件名（不含扩展名）即该 NPC 的 id，字典键为 id，值为该卡数据。
func load_npcs() -> void:
	npcs = _load_data_dir(_world_dirs()["npcs"])

## 取人物卡指定 NPC 的中文名（容错：人物卡缺失或没有 name 时回退为 id 本身）
func _npc_name(npc_id: String) -> String:
	if npcs.has(npc_id):
		return npcs[npc_id].get("name", npc_id)
	return npc_id

## ---------- 顶栏 / 行动点刷新 ----------
func _refresh_topbar() -> void:
	# 测试世界：时间对玩家始终未知（后端 tick 数据存在，仅用于世界判定——用户裁决）；
	# 黄金乡沿用时段显示
	if GameState.world_id == "test":
		time_label.text = "时间：未知"
	else:
		time_label.text = "时间：" + Clock.current_slot()
	
	if locations.has(GameState.location_id):
		var loc: Dictionary = locations[GameState.location_id]
		loc_label.text = "地点：" + loc.get("name", "")

func _refresh_actions() -> void:
	ap_label.text = str(GameState.action_points)
	ap_bar.value = GameState.action_points

func _on_action_points_changed(points: int) -> void:
	ap_label.text = str(points)
	ap_bar.value = points

func _on_items_changed(_items: Array[String]) -> void:
	## 背包持有物变化（拿刀/放下）时刷新。若正处于背包页，重绘即可看到最新持有物。
	## 注意只重绘不请求（避免 request_inventory → set_items → 本回调 → 再请求的死循环）。
	if active_tab == "backpack":
		_render_current_tab()

## ---------- 地点显示 ----------
func show_location(loc_id: String) -> void:
	## 只负责"显示"，不触发状态变更（避免 switch_location 的 location_changed 信号自激循环）。
	## 切换地点统一由 GameState.switch_location() 触发信号 → _on_location_changed → 这里显示。
	_refresh_topbar()
	# 保持当前 tab 并重绘左边栏：地图点击后仍是"地图"tab，仅更新可达按钮状态（不退回 scene）
	_render_current_tab()
	# 每个地点的"场景展示"统一走 _show_scene_in_location（分隔符 + 场景描述 + 人物入口）
	_show_scene_in_location(loc_id)

## 在地点进入/切换、以及退出对话返回时，往右对话流里写入"分隔符 + 场景描述 + 人物入口"。
## 这样所有场景描述前都有一条分隔线，把上一段对话与当前场景清晰地隔开。
## view（09-08 问题1/2）：移动进入=arriving（"你刚走进来"）；对话结束/略过=lingering（停留重看）。
func _show_scene_in_location(loc_id: String, view: String = "arriving") -> void:
	# 场景描述用分隔符包住（上方一条 → 描述 → 下方一条），视觉上自成一块
	_add_separator("—— %s ——" % _loc_name(loc_id))
	if not locations.has(loc_id):
		add_message("旁白", "（未知地点）", false)
		return
	# 09-08 完整场景一次成型：会话未就绪（开局，GameState.session_id 为空）时不发 adhoc 请求，
	# 避免"adhoc 旧数据/迟到响应"与真实会话竞态交织（上次出现"有入口无描述"就是这个原因）。
	# 先只落分隔符；等会话就绪后由 _on_session_created 用真实会话 full 完整渲染一次。
	if String(GameState.session_id) == "":
		return
	# 09-09（用户需求"场景信息都显示出来才进页面"）：loading 阶段已把首个 /scene/inspect
	# 完整响应缓存到 GameState.pending_scene_inspect。优先用它一次成型渲染（旁白+入口），
	# 避免进入场景时只有分隔线、异步请求回来才补的空窗。用过即清空，下次切地点走正常请求。
	if not GameState.pending_scene_inspect.is_empty():
		var payload: Dictionary = GameState.pending_scene_inspect
		GameState.pending_scene_inspect = {}
		_inspect_mode = "full"   # 确保注入旁白（缓存是完整响应）
		_on_scene_inspect(payload)
		return
	# 不再用本地静态 loc.desc（如"角落有一把刀"——刀被拿走仍旧，会骗玩家）。
	# 场景描述统一由 /scene/inspect 返回：narrative(文学旁白) + snapshot(事实), 动态反映现状。
	# 场景人物入口改为动态（问题4）：也由 /scene/inspect 的"此刻在场 NPC"生成交谈按钮。
	ApiClient.request_scene_inspect(loc_id, view)

## 取地点名（做容错：未知 id 回退到 id 本身）
func _loc_name(loc_id: String) -> String:
	if locations.has(loc_id):
		return locations[loc_id]["name"]
	return loc_id

func _on_location_changed(location_id: String) -> void:
	show_location(location_id)

## 会话建立成功（真实 session_id 就绪 + 已同步位置）后调用：完整渲染当前场景一次。
## 开局 _show_scene_in_location 因 session 为空只落了分隔符（未请求），这里是第一次真正拉取
## 场景数据——/scene/inspect 一次响应即完整包（文学旁白+事实+入口），等它到齐一次性展示。
func _on_session_created() -> void:
	_inspect_mode = "full"
	# 09-10：玩家初始场景=initial_scene(room_1)，玩家【开局就在此】，并非"移动进来"，
	# 故用 opening（开局场景预设文案，秒回）而非 arriving（"你踏进…"入场口吻，那会误导为"到达"）。
	# 移动进入仍由移动流走 arriving；停留/略过走 lingering；结束对话返回走 conv_end。
	ApiClient.request_scene_inspect(GameState.location_id, "opening")

## 清除上一轮生成的交谈入口按钮（挂对话流、滚动垫片之前；补拉/重看同一场景时先清旧的）。
func _clear_scene_entries() -> void:
	for btn in _scene_entries:
		if is_instance_valid(btn):
			btn.queue_free()
	_scene_entries.clear()

## 09-08【死人入口修复】刷新当前场景的交谈入口（只重建按钮，不重复旁白）。
## 用法：世界有变化（NPC 死亡/进入/离开）后置 _inspect_mode="entries_only" 再重拉 /scene/inspect；
## 后端 scene_inspect 已过滤死亡 NPC（main.py），_on_scene_inspect 据重建入口 → 死人按钮消失。
func _refresh_scene_entries() -> void:
	_inspect_mode = "entries_only"
	ApiClient.request_scene_inspect(GameState.location_id)

## ---------- 左面板（背包 / 地图 / 日志 / 联络 的内容容器） ----------
func _clear_left() -> void:
	for child in left_vbox.get_children():
		child.queue_free()

func _add_left_label(text: String) -> void:
	var lbl := Label.new()
	lbl.add_theme_font_size_override("font_size", FONT_SIZE_LEFT)
	lbl.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	lbl.text = text
	left_vbox.add_child(lbl)

## ---------- 对话系统 v0.4：邀请 → 同意/拒绝 → 多轮逐句 → 结束 ----------
## 区别于上面的"浏览对话"（_enter_dialogue，人物卡台词+倒计时）：
## 这里是**后端驱动的多轮对话会话**——玩家每句经 /conversation/turn，NPC 流式回一句，
## 至多 10 句、只耗进入对话那 1 tick。会话期间 world 被 active_conv 挂起（不额外推进）。

## 世界结算完成后调用：查是否有 NPC 想邀玩家对话（offer 非空则前端弹询问）。
func _query_conversation_invite() -> void:
	ApiClient.request_conversation_invite()

## 收到"待玩家裁决的对话邀请"：在右侧对话流里渲染【整组】邀请——
## "「房间」有人向你发来对话邀请：" + N 个（名字：第一句话）选项 + 底部一个「婉拒对话」。
func _on_conversation_invite(offer: Array) -> void:
	if offer.is_empty():
		return
	# 只保留有发起人的条目（脏数据直接丢，不让空 id 挤进选项）
	var invs: Array = []
	for raw in offer:
		if raw is Dictionary and _s(raw.get("initiator", "")) != "":
			invs.append(raw)
	if invs.is_empty():
		return
	# 去重：同一个邀请会从三条来路到达前端（/chat 响应、/world/updates 轮询、结算后补查），
	# 每条都渲染一次就会变成"三组按钮"。锚点是【整组邀请人 id 排序后的指纹】——
	# 只要本组按钮还在（玩家尚未裁决），同一组的重复邀请一律丢弃。
	var key := _invite_key(invs)
	if key != "" and key == _pending_invite_key and is_instance_valid(_pending_invite_row):
		return
	_conv_npc = _s(invs[0].get("initiator", ""))
	_show_invite_inline(invs)

## 整组邀请的指纹：邀请人 id 排序后拼接——与 offer 里的顺序无关，顺序变了仍算同一组。
func _invite_key(invs: Array) -> String:
	var ids: Array[String] = []
	for inv in invs:
		if inv is Dictionary:
			ids.append(_s(inv.get("initiator", "")))
	ids.sort()
	return "|".join(ids)

## 09-10 修永久死锁：任何世界响应带回"世界被挂起 / 没推进"时，统一在这里收口。
## 触发来源有三条（互为兜底，保证至少一条能到玩家眼前）：
##   ① /chat 与 /session/move 响应里的 world_paused + pending_offer（本次新增的透传链）；
##   ② /world/updates 的 pending_offer 字段（每次拉世界增量都能发现）；
##   ③ 世界结算后主动查 /conversation/invite 补一次（旧链路，保留）。
## 旧故障：① 被调用点丢掉、③ 的参数传错（session_id 塞进 body，后端只查到 adhoc）→ 三条全断，
## 于是 tick 因"NPC 邀请玩家对话"挂起后世界永久冻结，玩家只看到一行"另有对话邀请待你处理"
## 的拒绝理由，既没有可点按钮、也没有任何"世界停住了"的说明。
## Returns: true = 本次响应确实带挂起态（调用方可据此跳过"世界已推进"的处理）。
func _handle_world_pause(payload: Dictionary) -> bool:
	if payload.is_empty():
		return false
	var paused := _b(payload.get("world_paused", false))
	var offer_raw = payload.get("pending_offer", [])
	var offer: Array = offer_raw if offer_raw is Array else []
	if not paused and offer.is_empty():
		return false
	if not offer.is_empty():
		# 明细已在手 → 直接渲染邀请（右框内嵌按钮），省一次网络往返。挂起提示语由各调用点
		# 自己的文案承载（/chat 的 reply、/session/move 的 message 都是这句 notice），
		# 这里不再重复打一遍。重复到达的同一邀请由 _on_conversation_invite 去重。
		_on_conversation_invite(offer)
	else:
		# 没有邀请明细（如"上一格还在结算"= world_skipped，或旧后端缺字段）→ 必须把原因说清楚，
		# 否则玩家又回到"点了没反应"的困惑；同时补查一次，拿到明细后再弹。
		var notice := _s(payload.get("notice", ""))
		if notice != "":
			add_message("系统", notice, false)
		_query_conversation_invite()
	_set_busy(false)                       # 世界没动，界面不能停在"等待结算"的忙碌态
	return true

## /chat 响应自带的挂起态（api_client.world_paused_received）→ 交给统一收口处理。
func _on_world_paused(payload: Dictionary) -> void:
	_handle_world_pause(payload)

## 把【整组】"有人想与你对话"渲染进右侧对话流 + 紧跟一列内嵌选项。
## 09-10 用户拍板改版（多人邀请）：一条邀请提示 → 每个邀请人一个选项（名字：TA 打算说的第一句话）
## → 底部一个「婉拒对话」。选某人 = 跟 TA 聊、其余人【后端自动婉拒】；点底部 = 全都不聊。
## 不再用全屏模态遮罩，理由有二——
##   ① 体验：模态会挡住整个界面、把玩家从上下文里拽出来；而邀请本质是一条【消息】，
##      放进记录流既能停留回看，又不打断"读场景 → 决定要不要聊"的自然节奏。
##   ② 工程：模态要靠"建覆盖层 + 按名字查找 + queue_free 收起"，queue_free 是延迟释放，
##      同一帧内重复创建会撞名改名，之后再也找不到新节点 → 覆盖层永久留在屏幕上。
##      内嵌方案用【变量引用】持有按钮块，裁决时直接摘引用，不存在查找失败的可能。
func _show_invite_inline(invs: Array) -> void:
	_dismiss_invite_modal()      # 先清掉上一组（幂等；同时清历史遗留的旧覆盖层）
	# 邀请提示带上房间名，"在哪有人想跟你说话"一目了然（inv.scene 是后端给的房间 id）
	var scene_id := ""
	for inv in invs:
		if inv is Dictionary and _s(inv.get("scene", "")) != "":
			scene_id = _s(inv.get("scene", ""))
			break
	var where := _loc_name(scene_id) if scene_id != "" else _loc_name(GameState.location_id)
	add_message("系统", "「%s」有人向你发来对话邀请：" % where, false)
	# 按钮块（临时交互件，裁决后整块摘掉；上面那条提示消息留在记录里）
	# 用 VBox（一人一行）而不是 HBox：选项含第一句话，横排会互相挤压到看不清。
	var box := VBoxContainer.new()
	box.add_theme_constant_override("separation", 6)
	for inv in invs:
		var npc_id := _s(inv.get("initiator", ""))
		var speaker := _invite_speaker_name(npc_id)
		var speech := _s(inv.get("speech", ""))
		var btn := Button.new()
		btn.text = _invite_button_text(speaker, speech)
		# 悬停看全文：按钮内是截断预览，被截掉的完整第一句必须还能读到
		btn.tooltip_text = "「%s」：%s" % [speaker, speech if speech != "" else "……"]
		btn.alignment = HORIZONTAL_ALIGNMENT_LEFT
		btn.size_flags_horizontal = Control.SIZE_EXPAND_FILL
		btn.add_theme_font_size_override("font_size", FONT_SIZE_ACTION)
		btn.pressed.connect(_accept_invite.bind(npc_id, true))
		box.add_child(btn)
	# 底部：婉拒对话（npc_id="" 是"整组婉拒"的哨兵值，见 _accept_invite）
	var decline_btn := Button.new()
	decline_btn.text = "婉拒对话"
	decline_btn.alignment = HORIZONTAL_ALIGNMENT_LEFT
	decline_btn.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	decline_btn.add_theme_font_size_override("font_size", FONT_SIZE_ACTION)
	decline_btn.pressed.connect(_accept_invite.bind("", false))
	box.add_child(decline_btn)
	_insert_before_spacer(box)
	_pending_invite_key = _invite_key(invs)
	_pending_invite_npc = _s((invs[0] as Dictionary).get("initiator", "")) if not invs.is_empty() else ""
	_pending_invite_row = box
	call_deferred("_scroll_to_entry_bottom")   # 滚到底，确保玩家看得见这组选项

## 邀请人显示名：人物卡缺失 / 卡里 name 是空串时退回 id 或"有人"（绝不能渲染成空白）。
func _invite_speaker_name(npc_id: String) -> String:
	var speaker := _s(_npc_name(npc_id))
	if speaker.strip_edges() == "":
		speaker = npc_id if npc_id != "" else "有人"
	return speaker

## 选项按钮文案：「名字」：第一句话（超长截断，全文在 tooltip 里——见 INVITE_PREVIEW_CHARS）。
func _invite_button_text(speaker: String, speech: String) -> String:
	if speech.strip_edges() == "":
		return "「%s」：（欲言又止）" % speaker
	var line := speech
	if line.length() > INVITE_PREVIEW_CHARS:
		line = line.substr(0, INVITE_PREVIEW_CHARS) + "…"
	return "「%s」：%s" % [speaker, line]

## 点某个邀请人 = 跟 TA 聊（其余人由后端自动婉拒）；点「婉拒对话」（npc_id=""）= 全都不聊。
## 先摘按钮（同步生效，玩家立刻看到"已裁决"），再向后端裁决。
## 摘按钮【不依赖 queue_free 的延迟】：整块从引用上摘掉并 queue_free，玩家不可能再点第二次；
## 后端结果回来由 accepted/declined 回调续接（进会话 / 记一行婉拒）。
func _accept_invite(npc_id: String, accept: bool) -> void:
	# 幂等：整组邀请只能被裁决一次（按钮块摘掉后再收到任何回调一律忽略）
	if _pending_invite_key == "" or not is_instance_valid(_pending_invite_row):
		return
	if accept and npc_id == "":
		return   # "同意"必须指明具体对象
	_dismiss_invite_modal()
	ApiClient.request_conversation_accept(npc_id, accept)

## 收起邀请交互件：摘掉内嵌按钮块 + 清掉任何残留的旧全屏覆盖层。
## 幂等、可重复调用（也用于"后端先推送了裁决结果"时的防御性清理）。
func _dismiss_invite_modal() -> void:
	if is_instance_valid(_pending_invite_row):
		_pending_invite_row.queue_free()
	_pending_invite_row = null
	_pending_invite_key = ""
	_pending_invite_npc = ""
	# 兼容历史遗留：改版前建过名为 InviteOverlay 的全屏覆盖层，旧实例/热重载可能还挂在树上。
	# 前缀匹配（Godot 撞名会改名为 @InviteOverlay@N），一次清干净。
	for child in get_children():
		if child is Control and String(child.name).begins_with("InviteOverlay"):
			child.queue_free()

## 进入"对话会话"UI：切到会话模式、建分隔符、展示 AI 动态简介（+可选 first_line）。
## 齿轮提示由调用方在合适时机先行展示（NPC 邀请在同意时、玩家主动发起在点交谈时），避免重复。
func _enter_conv_session(npc_id: String, intro: String, first_line: String) -> void:
	if _conv_active:
		_set_busy(false)
		return
	_conv_active = true
	_conv_npc = npc_id
	current_npc = npc_id
	in_dialogue = false   # 会话模式独立，不复用"浏览对话"的倒计时/选项
	name_label.text = _npc_name(npc_id)
	status_label.text = ""
	status_card.visible = false
	status_label.visible = false
	interact_btn.text = "结束对话"
	_clear_options()
	_add_separator("—— 与「%s」的对话 ——" % _npc_name(npc_id))
	if intro != "":
		add_message("系统", intro, false)
	if first_line != "":
		add_message(name_label.text, first_line, false)
	_set_busy(false)

## 玩家同意 NPC 的邀请 → 齿轮 → AI 动态简介 → 开始对话。
func _on_conversation_accepted(payload: Dictionary) -> void:
	if _conv_active:
		_set_busy(false)
		return
	_dismiss_invite_modal()   # 防御性收起（正常路径已在 _accept_invite 摘掉按钮）
	var npc_id := _s(payload.get("initiator", _conv_npc))
	# 记录发起对话那一刻的 tick（结束回场景做 since 基准）。
	_conv_start_tick = int(payload.get("conv_tick", _conv_start_tick))
	# 多人邀请（09-10 用户拍板）：选了一人 → 其余人由【后端自动婉拒】。这里显式说出来，
	# 否则玩家不知道"其他人怎么了"（他们会按各自的下一个打算继续行动，不再干等）。
	var others_raw: Variant = payload.get("rejected_others", [])
	var others: Array = others_raw if others_raw is Array else []
	var others_names: Array[String] = []
	for x in others:
		var on := _invite_speaker_name(_s(x))
		if on != "" and not others_names.has(on):
			others_names.append(on)
	if not others_names.is_empty():
		add_message("系统", "你选择了与「%s」交谈；%s 的邀谈，只能留到下次了。"
			% [_invite_speaker_name(npc_id), "、".join(others_names)], false)
	# 时序（第2点）：同意即视为"本 tick 选择对话"——先齿轮，再进会话
	add_message("旁白", "命运的齿轮开始转动，周围的声响仿佛都慢了下来……", false)
	_enter_conv_session(npc_id, _s(payload.get("intro", "")), _s(payload.get("first_line", "")))

## 玩家【主动】发起对话成功 → 进入会话。09-10 用户拍板：被拒不扣点、无齿轮；
## 【愿意进入对话那一刻】才真正扣行动点 + 齿轮（后端愿意才推 1 格世界，此刻才算消耗）。
func _on_conversation_started(payload: Dictionary) -> void:
	var npc_id := _s(payload.get("npc_id", ""))
	if npc_id == "":
		npc_id = _s(payload.get("initiator", ""))
	# 记录发起对话那一刻的 tick（结束回场景用 _conv_start_tick-1 拉 /world/updates 只显这一格）。
	_conv_start_tick = int(payload.get("conv_tick", _conv_start_tick))
	# 愿意→扣行动点（后端已推 1 tick）。_on_talk_pressed 已预检 >0，这里几乎必然成功；兜底以防万一。
	if not GameState.spend_action():
		add_message("系统", "行动点不足，无法交谈。", false)
		_set_busy(false)
		return
	add_message("旁白", "你走向「%s」——命运的齿轮开始转动……" % _npc_name(npc_id), false)
	_enter_conv_session(npc_id, _s(payload.get("intro", "")), _s(payload.get("first_line", "")))

## 玩家主动发起对话被拒 → 显示婉拒，世界不动、可重选其它行动。
## 09-10 用户拍板：被拒【不扣行动点、不打齿轮】——后端被拒不推世界、没进入对话，点保留。
func _on_conversation_start_rejected(reason: String) -> void:
	_set_busy(false)
	if reason != "":
		add_message("旁白", reason, false)

## 玩家点「与X交谈」入口 → 主动发起对话。09-10 用户拍板（被拒不扣点）：
## 这里【只预检行动点 + 进入忙碌】，不扣点、不打齿轮——后端愿不愿由 /conversation/start 判定，
## 愿→_on_conversation_started 才扣点+齿轮进会话；拒→_on_conversation_start_rejected 婉拒（点保留）。
func _on_talk_pressed(npc_id: String) -> void:
	if _busy or _conv_active:
		return
	current_npc = npc_id   # 先记下对象，便于文案与后续归属
	if GameState.action_points <= 0:
		add_message("系统", "行动点不足，无法交谈。", false)
		return
	_set_busy(true)
	add_message("旁白", "你走近「%s」，开口想与TA交谈……" % _npc_name(npc_id), false)
	ApiClient.request_conversation_start(npc_id)

## 玩家拒绝 → 后端已按优先级递推该 NPC 本 tick 的下一步，并把【被挂起的那一格】真正 resume 结算。
## 09-10 用户现场修正（"婉拒后我自己的行动没继续"）：拒绝 = 让世界继续走完那一格，
## 你输入的行动（如"去房间二"）也在那一格里被结算落地（后端已修：导演分支现在也执行玩家的
## 机械动作）。但"结算掉"不等于"送到了玩家眼前"——这一格的产物要经 /world/updates 才会渲染。
## 所以这里必须补一次 request_world_updates()：否则玩家只看到一行"你婉拒了…"，
## 屏幕再无变化，体感就是"我的行动没执行"。
func _on_conversation_declined(npc_id: String, payload: Dictionary = {}) -> void:
	_dismiss_invite_modal()   # 防御性收起（正常路径已在 _accept_invite 摘掉按钮）
	# 婉拒文案（09-10 多人邀请改版）：后端返回 declined_all = 本组【全部】被婉拒的邀请人。
	# 旧文案只写"你婉拒了与「张三」的对话"——多人邀请时会让玩家以为还有人在等他裁决。
	var names: Array[String] = []
	var declined_raw: Variant = payload.get("declined_all", [])
	var declined: Array = declined_raw if declined_raw is Array else []
	for x in declined:
		var n := _invite_speaker_name(_s(x))
		if n != "" and not names.has(n):
			names.append(n)
	if names.size() > 1:
		add_message("系统", "你婉拒了这几位的邀谈：%s。" % "、".join(names), false)
	elif names.size() == 1:
		add_message("系统", "你婉拒了与「%s」的对话。" % names[0], false)
	elif npc_id != "":
		add_message("系统", "你婉拒了与「%s」的对话。" % _npc_name(npc_id), false)
	else:
		add_message("系统", "你婉拒了这次的对话邀请。", false)
	# ① 若"恢复这一格"的过程中【又】有人想跟你说话（payload 带挂起态）→ 继续裁决，不丢链
	if _handle_world_pause(payload):
		return
	# ② 正常结算：解锁并补拉本格产物（导演叙述 + 结果痕迹，含玩家自己那一步行动的结果）
	_set_busy(false)
	ApiClient.request_world_updates()
	_query_conversation_invite()   # 兜底再查一次（兼容 payload 缺时序字段的旧链路）

## 玩家回一句后的 NPC 下一句（over=true 表示后端已判定会话结束）。
func _on_conversation_turn_received(reply: String, over: bool) -> void:
	if not _conv_active:
		_set_busy(false)
		return
	if reply != "":
		add_message(name_label.text, reply, false)
	_set_busy(false)
	if over:
		_end_conversation()

## 主动结束对话（后端已清会话）。conv_end_text = 后端已预生成的专属转场旁白（空=兜底现调）。
func _on_conversation_ended(_ended: bool, conv_end_text: String) -> void:
	_conv_end_text = conv_end_text   # 供 _end_conversation 渲染专属转场旁白（缺失则兜底 /scene/inspect）
	_end_conversation()

## 收尾：清会话状态，回到当前场景（停留视角重看）。
## 09-09 用户拍板：结束对话后回到场景界面，若需重新生成场景叙述（/scene/inspect 走 LLM，异步），
## 忙碌必须维持到场景信息【完整生成完毕】才解锁——否则玩家在"场景还没生成、按键已亮"时就操作，
## 与"移动/略过"提前解锁同病。故复用 _skip_reflow_busy 门控：结束对话需重述场景时置位，
## 等 _on_scene_inspect 渲染完旁白+交谈入口再复位解锁；仅退出对话面板（_end_interact）不重述才立即放行。
func _end_conversation() -> void:
	_conv_active = false
	_conv_npc = ""
	if in_dialogue:
		_end_interact()
		_set_busy(false)   # 仅退对话面板、不回场景：无场景重述，立即解锁
	else:
		# 09-10：结束对话即回到环境交互模式。会话对话 in_dialogue=false，_end_conversation 不走
		# _end_interact 分支，原代码从未把 interact_btn 文本复位 → 结束时仍停在"结束对话"。
		# 这里第一时间把右下角按钮切回"略过"（其余交互输入框保持"输入行动/对话"，由 _set_busy 解锁）。
		interact_btn.text = "略过"
		# 09-09 第4步（对话结束回场景汇合）：除重述场景外，还要把"发起对话那一个tick"后台结算的
		# 世界变化（/world/updates 增量）放右框。若后台那一个tick还没算完，则忙碌等待并轮询。
		# 两个都到位（场景重述 + 世界增量）才解锁——否则"场景先亮、世界变化后到"再次提前解锁。
		_skip_reflow_busy = true
		_end_conv_pending = true
		_end_conv_scene_done = false
		_end_conv_world_done = false
		# 基准 = 发起对话那一格的前一格（只拉【这一格】的世界增量，治「A/B/C 堆叠」——
		# 对话前多个 tick 的痕迹不再随 last_seen_tick 一次性混入右框）。
		_end_conv_target_tick = _conv_start_tick - 1 if _conv_start_tick >= 0 else GameState.last_seen_tick
		# 转场导语：后端 /scene/inspect view=conv_end 已【优先读缓存】返回预生成的"你和XX结束了对话，
		# 注意力回到现实"专属旁白（发起对话那格结算完成时已落库，秒出、且不重复现调 LLM）；缓存缺失时
		# 该端点兜底现调。此处由 _show_scene_in_location 拉回的同一份 narrative 统一渲染，不再手动 add。
		# 场景重述：对话结束回场景用 conv_end 视角（转场语气，非"你刚走进来"入场口吻）。
		_show_scene_in_location(GameState.location_id, "conv_end")
		# 拉后台那一个tick的世界增量；若后台该tick尚未结算完（current_tick<=_end_conv_target_tick），
		# 则由 _on_world_updates 触发轮询，直到拿到 > 基准tick 的增量才标记 world_done。
		# since=_end_conv_target_tick(=conv_tick-1)，until=_conv_start_tick(=conv_tick)：
		# 只拉【发起对话那一格】的世界增量，彻底掐断 A/B/C 多 tick 痕迹堆叠。
		ApiClient.request_world_updates(_end_conv_target_tick, _conv_start_tick)

## 对话结束回场景：场景重述 + 后台那一个tick世界增量【都到位】→ 真正解锁并复位汇合状态。
## 解锁点必须是"界面信息完全就绪"这一时刻（否则又出现"先解锁、内容后到"）。
func _finish_end_conv_merge() -> void:
	_end_conv_pending = false
	_end_conv_scene_done = false
	_end_conv_world_done = false
	_end_conv_polling = false
	if _skip_reflow_busy:
		_skip_reflow_busy = false
		_set_busy(false)
		_query_conversation_invite()   # 汇合完成，再查是否有 NPC 想邀玩家对话

## 后台那一个tick尚未结算完时轮询：每 0.6s 再拉一次 /world/updates，直到 current_tick 超过基准。
## 用 _end_conv_polling 防叠加（已有排队的轮询时不再重复起定时器）。
func _poll_end_conv_world() -> void:
	if not _end_conv_pending:
		return
	if _end_conv_polling:
		return
	_end_conv_polling = true
	await get_tree().create_timer(0.6).timeout
	_end_conv_polling = false   # 复位，供下一次再触发
	if _end_conv_pending:
		ApiClient.request_world_updates()

## ---------- 对话 ----------
func _enter_dialogue(npc_id: String) -> void:
	## 点击 NPC 入口仅进入对话浏览，不消耗行动点；真正对话（发送消息）才会扣行动点。
	current_npc = npc_id
	in_dialogue = true
	name_label.text = _npc_name(npc_id)
	status_label.text = _npc_status(npc_id)
	status_card.visible = not status_label.text.is_empty()
	status_label.visible = status_card.visible
	interact_btn.text = "结束"
	_clear_options()
	# 开场白用"中下位置"滚动，方便玩家阅读；后续玩家自己发送的消息仍滚到底
	add_message(name_label.text, _npc_opening(npc_id), false)
	_start_countdown()

## 从人物卡读 NPC 状态文案（P4 由后端信念/状态给，这里先由角色卡承载）
func _npc_status(npc_id: String) -> String:
	if npcs.has(npc_id):
		return npcs[npc_id].get("status", "")
	return ""

## 从人物卡读 NPC 开场白（P4 由后端生成）
func _npc_opening(npc_id: String) -> String:
	if npcs.has(npc_id):
		return npcs[npc_id].get("opening", "（此人打量着你，沉默不语。）")
	return "（此人打量着你，沉默不语。）"

## D：世界结算锁。锁住输入框/发送/略过/地图移动，等本 tick 全部 NPC 动完再放行。
func _set_busy(locked: bool) -> void:
	_busy = locked
	# 09-10：忙碌期间轮询"后台正在忙什么"的提示——玩家输入被解析层"再解析一次"时，
	# 后端会推"事情比想象中复杂……"，让玩家知道自己在等什么，而不是怀疑卡死。
	if locked:
		_start_notice_poll()
	input.editable = not locked
	send_btn.disabled = locked
	interact_btn.disabled = locked
	# 09-09 问题2：忙碌状态同时锁住"与X交谈"场景入口按钮——之前只锁了输入框/发送/略过/地图，
	# 漏了这些动态生成的交谈按钮，导致世界结算期间那几个"与X交谈"仍可点、悬停仍有反应。
	# 现在统一置灰（disabled 后悬停/点击均无反应），重述/行动完成解锁后自动恢复可点。
	for btn in _scene_entries:
		if is_instance_valid(btn):
			btn.disabled = locked
	if active_tab == "map":
		_render_current_tab()   # 重绘地图按钮（busy 时可达地点也置灰，移动被禁止）

## ---------- 即时提示轮询（09-10）----------
## 玩家自由输入的解析层可能触发"再解析一次"，但这件事发生在 /chat 这个【同步阻塞】请求内部：
## 请求没返回，前端就拿不到任何中途进度，而这一格世界结算还要等几十秒。
## 所以后端把提示入池、前端在忙碌期间轮询取走——玩家全程看得见"机器在忙什么"。
func _start_notice_poll() -> void:
	if _notice_polling:
		return                      # 已在轮询，不重复起循环
	_notice_polling = true
	while _busy and _notice_polling:
		ApiClient.request_notices()
		await get_tree().create_timer(0.7).timeout
	_notice_polling = false

## 收到后端推来的"后台进度提示"→ 显示成系统消息。
## 后端是排干式（取走即清空），这里直接逐条显示、无需去重。
func _on_notices_received(notices: Array) -> void:
	for n in notices:
		if not (n is Dictionary):
			continue
		var text := String(n.get("text", "")).strip_edges()
		if text != "":
			add_message("系统", text, false)

## A/问题4：进房间拉到的动态感知（文学叙事 + 事实快照）+ 此刻在场 NPC。
## 09-08 叙述编排修正（用户拍板）：文学叙述由后端 narrate_scene 生成（已融合"这里的人在
## 这一 tick 做了什么" + 玩家刚走进来的有限视角），一次呈现；这里只做背景性拼接，不再出现
## 破墙的"test_man: xxx"零散痕迹。快照按行拆，去掉机械标签前缀，改成自然叙述句。
func _on_scene_inspect(payload: Dictionary) -> void:
	# _inspect_mode: full=完整展示(旁白+入口)；entries_only=仅刷新入口(会话建立后补拉)
	# 09-08：丢弃"旧会话/adhoc"的迟到旁白响应。开局 `_ready` 的 show_location 用空 session_id
	# 落到 adhoc 旧会话（test_man 在上次残留位置，场景读不到他）。会话建立后补拉的是真实会话；
	# 若 adhoc 的慢响应（LLM narrative）晚到，丢弃以免用旧/错数据覆盖真实会话的正确结果。
	var cur_sid := String(GameState.session_id)
	if cur_sid != "" and String(payload.get("session_id", "")) != cur_sid:
		return
	var full: bool = _inspect_mode == "full"
	var narr := String(payload.get("narrative", "")).strip_edges()
	var snap := String(payload.get("snapshot", "")).strip_edges()
	if full:
		if narr != "":
			add_message("旁白", narr, false)
		if snap != "":
			# 快照形如：【你所在】X / 【这里的样子】… / 【这里有】A；B / 【你注意到】…
			# 09-08 用户拍板：旁白只呈现文学叙事（narr）+ 事实【这里有】；机械标签
			# 【你所在】【这里的样子】【你注意到】及括号差异注释（departed 标注）一律
			# 不再作为旁白原文显示——它们会让旁白"系统化/出戏"，场景描述由文学叙事承载。
			for line in snap.split("\n"):
				var l := line.strip_edges()
				if l.is_empty():
					continue
				if l.begins_with("【这里有】"):
					var body := l.substr(5)
					if body.contains("；"):
						# 人/物可能混在一行：把"【这里有】"整行作为"你看得到"概述自然给出
						add_message("旁白", "你看到：" + body, false)
					else:
						add_message("旁白", "这里有：" + body, false)
				# 其余行（【你所在】【这里的样子】【你注意到】等标签，或括号注释）：跳过，不显示。
			# 不再兜底原样显示 snap——避免机械标签漏出；场景描述全靠上方文学叙事 narr，
			# 若 narr 也为空且无【这里有】，走下方 narr=="" and snap=="" 的本地静态回退。
		if narr == "" and snap == "":
			# 后端异常（空快照+空叙事）→ 回退本地静态 desc，保证进房不空白
			var loc_id := String(GameState.location_id)
			if locations.has(loc_id):
				add_message("旁白", String(locations[loc_id].get("desc", "")), false)
	# 场景内可互动的 NPC → 以"入口选项"列出（动态：只列此刻在该场景的存活 NPC）。
	# 先清旧入口再生成：会话建立后补拉/重看同一场景时，避免入口按钮堆积。
	_clear_scene_entries()
	var present: Array = payload.get("npcs", [])
	for n in present:
		var n_d: Dictionary = n
		var nid := String(n_d.get("id", ""))
		if nid == "":
			continue
		var btn := Button.new()
		btn.text = "与「%s」交谈" % String(n_d.get("name", nid))
		btn.add_theme_font_size_override("font_size", FONT_SIZE_ACTION)
		btn.pressed.connect(_on_talk_pressed.bind(nid))
		_insert_before_spacer(btn)
		_scene_entries.append(btn)
	call_deferred("_scroll_to_entry_bottom")
	_inspect_mode = "full"   # 复位：下次按默认完整展示
	# 09-09 问题3/4：略过/移动触发的场景重述已完整落地（旁白+交谈入口渲染完，即截图时刻4）→
	# 此刻才是忙碌状态的真实结束点。解锁并把控制权交还玩家（重述里已等完 LLM，不再闪烁）。
	if _end_conv_pending:
		# 对话结束回场景：场景重述已渲染完。若后台那一个tick的世界增量也已到位 → 两就绪，解锁；
		# 否则只标 scene_done，等 _on_world_updates 判定增量到位后再统一解锁（避免"场景先亮、世界后到"）。
		_end_conv_scene_done = true
		if _end_conv_world_done:
			_finish_end_conv_merge()
	else:
		if _skip_reflow_busy:
			_skip_reflow_busy = false
			_set_busy(false)
			_query_conversation_invite()   # 重述完，再查是否有 NPC 想邀玩家对话

func _on_send() -> void:
	if _busy:
		return   # 世界结算中，禁发新消息（等全部 NPC 动完再放行）
	var text := input.text.strip_edges()
	input.text = ""
	if text.is_empty():
		return
	if _conv_active:
		# 对话会话：不再扣行动点（进入对话已耗 1 tick），发后端 turn 流式生成 NPC 下一句
		add_message("我", text, true)
		_set_busy(true)
		ApiClient.request_conversation_turn(text)
		return
	if not GameState.spend_action():
		add_message("系统", "行动点不足。", false)
		return
	add_message("我", text, true)
	_set_busy(true)   # 本次交互期间锁住输入/移动，直到 AI 回复回来（后端已同步推完 tick）
	# P3-A：非对话（环境直接行动）—— 无目标 NPC，npc_id 传空串，由世界/旁白响应。
	# 09-08 用户拍板：废弃"浏览对话"（预设选项+倒计时），对话统一走会话（_conv_active）。
	ApiClient.request_npc_reply("", text, {})
	# 第一齿轮（09-08 用户拍板）：只有当前房间还有其他行动者（可能多人同场景、走导演判定）
	# 才显示"齿轮开始转动"（等所有角色决策完）。单人场景确定性执行，无需等待提示。
	if _present_npcs().size() > 0:
		add_message("旁白", "你按下了行动——命运的齿轮开始转动……", false)

## P3-A 第③步：收到后端 AI 回复（成功或兜底都走这里）。
## 分两类：npc_id 为空 = 环境行动（由旁白响应）；否则为某 NPC 对话（需比对仍是当前对话对象）。
func _on_ai_reply(npc_id: String, reply: String) -> void:
	if npc_id.is_empty():
		add_message("旁白", reply, false)     # 环境行动：由旁白显示行动结果
		ApiClient.request_inventory()         # 拿/放物品在此路径发生 → 刷新背包
		# 后端已改回【同步 advance_one】：响应到达 = 本 tick 全部 NPC 行动+反应已结算完。
		# 拉一次 /world/updates 取导演叙述/各人后果（含 phase 第二齿轮标记），渲染后解锁。
		ApiClient.request_world_updates()
		_set_busy(false)
		_query_conversation_invite()   # 世界结算完，查是否有 NPC 想邀玩家对话
		return
	if npc_id != current_npc or not in_dialogue:
		_set_busy(false)                      # 回复过期（放弃），防死锁
		return                      # 对话已结束或换人了，丢弃这次回复
	add_message(name_label.text, reply, false)
	_reset_countdown()
	# 对话也是交互：后端已同步结算完 → 拉一次世界变化做提示，然后解锁
	ApiClient.request_world_updates()
	_set_busy(false)
	_query_conversation_invite()   # 本 tick 结束后查是否有 NPC 想邀玩家对话

## ---------- 选项（动态生成，点选即当作发送） ----------
## 从人物卡 option_rules 读取：依次匹配关键词，命中即用其 choices；空 match 的规则作兜底。
func _show_options_for(text: String, npc_id: String) -> void:
	_clear_options()
	var keywords: Array = _match_option_keywords(text, npc_id)
	for kw in keywords:
		var opt := Button.new()
		opt.text = "「%s」" % kw
		opt.add_theme_font_size_override("font_size", FONT_SIZE_ACTION)
		opt.pressed.connect(_on_option_pressed.bind(kw))
		option_box.add_child(opt)

## 按玩家输入文本，匹配人物卡的 option_rules，返回应命中的选项数组（逐条规则依次匹配）
func _match_option_keywords(text: String, npc_id: String) -> Array:
	var rules: Array = npcs.get(npc_id, {}).get("option_rules", [])
	var fallback: Array = ["继续刚才的话题", "换一个话题", "沉默以对"]
	for rule in rules:
		var match_keys: Array = rule.get("match", [])
		# 空 match = 兜底规则：无论输入什么都不会命中关键词，直接采用
		if match_keys.is_empty():
			return rule.get("choices", fallback)
		for key in match_keys:
			if text.contains(key):
				return rule.get("choices", fallback)
	return fallback

func _on_option_pressed(text: String) -> void:
	input.text = text
	_on_send()

func _clear_options() -> void:
	for child in option_box.get_children():
		child.queue_free()

## 追加一条对话消息（NPC 靠左 / 玩家靠右）—— 委托给对话流组件。
func add_message(speaker: String, text: String, is_player: bool) -> void:
	dialogue_log.add_message(speaker, text, is_player)

## 在对话流里插入一条分隔线（横向淡白细线，可选居中标题）—— 委托给对话流组件。
func _add_separator(title: String = "") -> void:
	dialogue_log.add_separator(title)

## 把任意节点插到对话流滚动垫片之前 —— 委托给对话流组件。
func _insert_before_spacer(node: Node) -> void:
	dialogue_log.insert_before_spacer(node)

## 场景切换后平滑滚动到底部（看到"与XX交谈"入口）—— 委托给对话流组件。
func _scroll_to_entry_bottom() -> void:
	dialogue_log.scroll_to_entry_bottom()

## ---------- 倒计时（60s，逐帧连续递减，未确定则自动发出 / 静默） ----------
func _process(delta: float) -> void:
	## 只有对话进行中且倒计时在走，才持续递减 —— 用 delta（每帧真实时间差）实现干净平滑的线性收缩
	if not countdown_running or not in_dialogue:
		return
	countdown_remaining -= delta
	_apply_countdown_ui()
	if countdown_remaining <= 0.0:
		countdown_remaining = 0.0
		_apply_countdown_ui()
		countdown_running = false
		_on_countdown_finish()

func _start_countdown() -> void:
	countdown_remaining = DIALOGUE_SECONDS
	countdown_running = true
	_apply_countdown_ui()

func _reset_countdown() -> void:
	countdown_remaining = DIALOGUE_SECONDS
	countdown_running = true
	_apply_countdown_ui()

func _apply_countdown_ui() -> void:
	## 用独立的剩余秒数驱动进度条：从 60 逐帧平滑递减到 0。
	## 每帧直接写 value（ProgressBar.value 是 float），进度条即连续平滑收缩。
	timer_bar.value = countdown_remaining

func _on_countdown_finish() -> void:
	if not in_dialogue:
		return
	var pending := input.text.strip_edges()
	if pending.is_empty():
		# 什么都没写 → 玩家静默看着 NPC，结束本段互动
		add_message(name_label.text, "……你默默看着对方，一言不发。", false)
		_end_interact()
	else:
		# 按未写完的内容自动发出（_on_send 里会自行 _reset_countdown 开启下一轮）
		input.text = pending
		_on_send()

## ---------- 底部按钮：略过 / 结束（同一按钮，随状态切换语义） ----------
## 对话中 = 结束对话；非对话 = 略过跳过时段。
func _on_interact_btn() -> void:
	if _conv_active:
		# 对话会话中：点这键 = 主动结束对话（后端清会话，world 恢复推进）
		ApiClient.request_conversation_end()
	elif in_dialogue:
		_end_interact()
	else:
		_on_skip()

func _on_skip() -> void:
	if _busy:
		return   # 世界结算中，禁再略过
	if not GameState.spend_action():
		add_message("系统", "行动点不足，无法再略过。", false)
		return
	Clock.advance_slot()
	_refresh_topbar()
	_set_busy(true)   # 略过也推进世界 → 锁输入，直到 /world/step 结算（world_step_done）或本地立即解锁
	if GameState.world_id == "test":
		add_message("系统", "你略过了片刻。周围的世界在继续运转……", false)
		# 略过 = 交互：显式推进一个世界 tick（POST /world/step），随后经
		# _on_world_updates 把"这一刻世界里发生了什么"渲染成提示。
		# 09-08 问题2：略过即"原地待了一会"，世界变化后应以"停留"视角重新描述场景。
		_skip_reflow_scene = true
		# 09-09 问题3/4：本次略过的忙碌要维持到"场景重述完整落地"（时刻4）才解除，
		# 而不是 world_step_done 一返回（时刻3）就亮——重述里走 LLM，30s+。
		_skip_reflow_busy = true
		ApiClient.request_world_step()
	else:
		add_message("系统", "你略过了片刻，时间悄然流逝（%s）。" % Clock.current_slot(), false)
		_set_busy(false)   # 黄金乡本地时钟推进，无后端世界结算，立即放行
	interact_btn.text = "略过"

func _end_interact() -> void:
	in_dialogue = false
	current_npc = ""
	name_label.text = "人物"
	status_label.text = ""
	status_card.visible = false
	status_label.visible = false
	interact_btn.text = "略过"
	_clear_options()
	countdown_running = false
	timer_bar.value = 0.0
	# 退出对话后，回到当前场景的"分隔符 + 场景描述 + 人物入口"状态。
	# 玩家本就待在此地（未移动），是"停留后重看"→ 用 lingering 视角（非"刚走进来"）。
	_show_scene_in_location(GameState.location_id, "lingering")

## ---------- 左区切换栏：背包 / 地图 / 日志 / 联络 ----------
func _on_tab(tab: String) -> void:
	active_tab = tab
	if tab == "backpack":
		ApiClient.request_inventory()   # 进入背包页先向后端拉一次当前持有物（数据源在后端）
	_render_current_tab()

## 按当前 active_tab 重绘左边栏（scene tab 不需要左边栏）。
## 地图点击移动后，show_location 也会调用它，从而保持"地图"tab 并仅更新可达按钮状态。
## ⚠️ 注意：此处只负责"渲染"，不再发网络请求——否则 items_changed → 重绘 → 再请求会死循环。
func _render_current_tab() -> void:
	_clear_left()
	match active_tab:
		"backpack":
			_add_left_label(_show_backpack())
		"map":
			_add_left_label("点击下方地点移动（遵守楼层规则，不可跳层）：")
			_show_map_buttons()
		"log":
			_add_left_label(_show_log())
		"link":
			_render_debug_panel()

func _show_backpack() -> String:
	if GameState.items.is_empty():
		return "背包空空：\n（道具可从与 NPC 的交互中获得）"
	var s := "背包：\n"
	for it in GameState.items:
		s += "- %s\n" % it
	return s

func _show_log() -> String:
	var s := "日志：\n·人物：\n"
	for key in npcs.keys():
		s += "  - %s\n" % npcs[key].get("name", key)
	s += "·已掌握线索：\n"
	if GameState.clues.is_empty():
		s += "  （暂无）\n"
	else:
		for c in GameState.clues:
			s += "  - %s\n" % c
	return s

## 推导区域分组顺序：黄金乡遵循习惯性的 外部→一层→二层；
## 测试世界等其它世界自动按其数据里出现的区域排（先出现的在前）。
## 实现：先按"惯例顺序"走一遍（出现在数据里的才保留），再补上数据里
## 出现但不在惯例列表内的兜底区域，保证任何世界都能完整分组显示。
func _derive_region_order() -> Array:
	var convention: Array = ["外部", "一层", "二层"]
	var region_order: Array = []
	for region in convention:
		if _world_has_region(region) and region not in region_order:
			region_order.append(region)
	# 兜底：出现但不在惯例列表里的区域（如测试世界的"房间"），按出现顺序追加
	for id in locations.keys():
		var region: String = locations[id].get("region", "")
		if region != "" and region not in region_order:
			region_order.append(region)
	return region_order

## 当前世界数据里是否存在某区域（用于惯例顺序过滤）
func _world_has_region(region: String) -> bool:
	for id in locations.keys():
		if locations[id].get("region", "") == region:
			return true
	return false

func _show_map_buttons() -> void:
	## 地图展示所有地点的全貌，按"区域"分组显示。
	## 区域顺序从数据推导（黄金乡惯例：外部→一层→二层；测试世界自动按其数据里出现的区域排）。
	## 只有"直接相连"的地点才可点按移动，其余显示但置灰不可达（遵守连接规则，不可跳星/跳层）。
	var cur_id: String = GameState.location_id
	var region_order: Array = _derive_region_order()
	for region in region_order:
		var region_locs: Array[String] = []
		for id in locations.keys():
			if locations[id].get("region", "") == region:
				region_locs.append(id)
		if region_locs.is_empty():
			continue
		_add_left_label("【%s】" % region)
		for id in region_locs:
			left_vbox.add_child(_make_map_button(cur_id, id))

## 生成单个地点的移动按钮：当前=置灰标记；直接相连=可点按；否则=置灰不可达。
func _make_map_button(cur_id: String, id: String) -> Button:
	var target: Dictionary = locations[id]
	var btn := Button.new()
	btn.text = target["name"]
	btn.add_theme_font_size_override("font_size", FONT_SIZE_ACTION)
	if id == cur_id:
		btn.text += "（当前）"
		btn.disabled = true
	elif _can_move(cur_id, id):
		btn.disabled = _busy   # 世界结算中，可达地点也置灰（移动被禁止）
		btn.pressed.connect(_on_move.bind(id))
	else:
		# 不可达：仅置灰（disabled），不加文字标注，靠灰色传递状态
		btn.disabled = true
	return btn

## 楼层/连接规则：能否从 from_id 一步移动到 to_id。
## 规则由各地点卡的 connect 数组定义（二楼房间只能经回廊到一层，不可跳层）。
## 抽成独立函数：连接判定与按钮渲染解耦，P4 若要加"时间/行动点门槛"只需改这里。
func _can_move(from_id: String, to_id: String) -> bool:
	var loc: Dictionary = locations.get(from_id, {})
	var conn: Array = loc.get("connect", [])
	return to_id in conn

## 当前地点在场的 NPC id 列表（P4 做状态快照用：让该 NPC 感知"此刻还有谁在"）。
func _present_npcs() -> Array:
	var cur_id: String = GameState.location_id
	var loc: Dictionary = locations.get(cur_id, {})
	return loc.get("npc_ids", [])

func _on_move(target: String) -> void:
	if _busy:
		return   # 世界结算中，禁止移动
	# 防御性校验：即便按钮被绕过直接调用，也要遵守楼层/连接规则
	if not _can_move(GameState.location_id, target):
		_add_left_label("无法从这里到达「%s」。" % target)
		return
	if not GameState.spend_action():
		_add_left_label("行动点不足，无法移动。")
		return
	# 移动即离开对话对象身边：结束对话状态（修"换房间还在跟旧 NPC 对话"）
	if in_dialogue:
		_end_interact()
		add_message("系统", "你离开了原来的房间，对话结束了。", false)
	# 方向1：点地图移动 = 一次完整世界行动。进入忙碌态，交由后端 /session/move 做
	# 校验可达 → 写 game_state['scene'] + 世界痕迹 → 推进一个世界 tick；全部完成才返回，
	# 客户端据此切地点 + 拉世界变化 + 解锁（与"输入移动/略过"同一条时序，杜绝场景错位）。
	_set_busy(true)
	# 地图移动 = 一次完整世界行动：后端 /session/move 会同步推进一个世界 tick（AI 反应），
	# 等待期给玩家"命运的齿轮开始转动"提示（与 _on_send 的行动提示一致，09-08 用户要求）。
	add_message("旁白", "你向「%s」走去——命运的齿轮开始转动……" % _loc_name(target), false)
	ApiClient.request_move(target)

## 方向1：/session/move 结算完成回调。移动成功才切地点（避免"显示到达、后端却未移动"）。
## 09-08 修正乱序/重复：不再在这里 request_world_updates——它会在回调里又触发一次
## _show_scene_in_location，与 switch_location 触发的场景叙述叠加，导致"测试房间二"重复、
## NPC 动作痕迹冒出在描述之前。只切 location，完整场景叙述（标题→文学描述→你所在/这里有→
## 这里有的人→你有注意到这 tick 人做的事→交谈入口）统一由 _show_scene_in_location 生成一次。
func _on_move_received(payload: Dictionary) -> void:
	# 09-10：世界挂起（等玩家裁决对话邀请）时后端返回 moved=false + world_paused，这里统一
	# 收口把邀请弹出来 / 明确提示，别让玩家以为"点了地图没反应"。
	_handle_world_pause(payload)
	var moved := _b(payload.get("moved", false))
	# 移动即离开对话现场：进行中的对话会话/浏览就此结束。
	if moved and (_conv_active or in_dialogue):
		if _conv_active:
			ApiClient.request_conversation_end()
		else:
			_end_interact()
	if moved:
		GameState.switch_location(String(payload.get("scene", GameState.location_id)))
		# 09-09 问题1：move 后同步 current_tick 给调试面板。后端 /session/move 确实推了 tick
		# （实测 current_tick 1→2→3），但 move 回调既不拉 /world/updates 也不拉 /debug/overview，
		# 前端 GameState.current_tick 恒停 0，调试面板"当前 0"。这里补拉 debug_overview：
		# 其回调无条件更新 current_tick，且只在"调试"页重绘——不重复渲染场景、不冒多余痕迹旁白，
		# 从而不触发 09-08 那次"move 后场景叙述叠加重复"的问题。
		ApiClient.request_debug_overview()
		# 09-09 用户拍板：移动也是完整世界行动，场景叙述（/scene/inspect 走 LLM）在切地点后
		# 异步才返回。若此刻就 _set_busy(false)，玩家会在"标题已出现、叙述未生成"时就解锁，
		# 产生"标题一出现就能继续行动、内容后到"的错乱（与略过同病）。故移动成功也置
		# _skip_reflow_busy：忙碌维持到 _on_scene_inspect 把本场景完整叙述+入口渲染完才解锁。
		_skip_reflow_busy = true
		# 09-10（用户拍板·口径统一）：移动的角色按【移动前】的场景参与推演，该格导演
		# player_view 落在出发地，玩家已不在那里、不会收到。到达画面改由【目标场景自己的】
		# 导演调用产出 arrival_view（独立字段，落 location=目标场景）：这里补拉一次
		# /world/updates（player_scene 此刻已=目标场景），正好把 arrival_view 与本格其他人的
		# 真实痕迹（NPC 移动/观察等）一起显示，不重复场景。
		ApiClient.request_world_updates()
	else:
		add_message("系统", String(payload.get("message", "移动失败，请重试。")), false)
		_set_busy(false)   # 移动失败：无场景重述，立即解锁
	_query_conversation_invite()   # 移动结算完，查是否有 NPC 想邀玩家对话


## 安全转字符串：LLM 输出的 JSON 字段类型不定（null/数字/数组/字典都可能），
## GDScript 的 String() 构造对部分类型直接崩（Invalid call 'String' constructor）。
func _s(v) -> String:
	if v == null:
		return ""
	if v is String:
		return v
	if v is float or v is int or v is bool:
		return str(v)
	if v is Array:
		var parts: Array[String] = []
		for x in v:
			parts.append(_s(x))
		return "、".join(parts)
	if v is Dictionary:
		var kv: Array[String] = []
		for k in v:
			kv.append("%s=%s" % [str(k), _s(v[k])])
		return "{" + "，".join(kv) + "}"
	return str(v)

## 浮点安全显示（保留两位；null → "-"）
func _f(v) -> String:
	if v == null:
		return "-"
	return "%.2f" % float(v)

## 布尔安全收敛：LLM/后端 JSON 字段类型不定（null/字符串/数字都可能），
## GDScript 的 bool() 构造对部分类型直接崩（Invalid call 'bool' constructor），
## 与 _s() 对 String() 崩同类。这里统一收敛成 bool：
##   - null → false；bool → 原样；String("true"/"1"/"yes"/"真") → true；
##   - int/float → 非 0；Array/Dictionary → 非空则 true。其余未知 → false。
func _b(v) -> bool:
	if v == null:
		return false
	if v is bool:
		return v
	if v is String:
		var low: String = v.strip_edges().to_lower()
		return low in ["true", "1", "yes", "y", "真", "是"]
	if v is int or v is float:
		return v != 0
	if v is Array or v is Dictionary:
		return not v.is_empty()
	return false

## 字典安全收敛：调试面板展示的是「跨 tick 历史」数据，字段类型在不同后端版本/不同
## 来源间不定（null/Array/Dictionary 都可能）。GDScript 把值赋给 :Dictionary 变量时，
## 若值是 null 或 Array 会直接崩（"Trying to assign value of type 'X' to a variable of
## type 'Dictionary'"）。这里统一收敛成 dict：Dictionary→原样；null/Array/其它→{}。
func _d(v) -> Dictionary:
	if v is Dictionary:
		return v
	return {}

## 分隔线追加（无返回值辅助）
func _lines_separator(lines: Array[String]) -> void:
	lines.append("──────────────")

## ---------- 在线世界时钟的前端（调试面板 + 世界变化提示） ----------
## 调试面板（上帝视角，用户要求的辅助测试功能）：逐 tick 展示每个 NPC 的
## ①得知了什么 ②给 AI 的提示词 ③实际行动(多选标签) ④AI 输出 ⑤环境影响 ⑥LLM 耗时。
var _debug_tick: int = -1   # 调试面板正在看的 tick（-1=跟随当前）

func _render_debug_panel() -> void:
	var view_tick := _debug_tick if _debug_tick >= 0 else GameState.current_tick
	_add_left_label("【上帝视角·世界运转】正在看 tick %d（后台数据，玩家不可见）
点\"略过\"或对话一次 = 世界推一格。" % view_tick)
	_add_debug_nav_button()
	_add_debug_step_button()
	ApiClient.request_debug_overview(view_tick)

func _add_debug_nav_button() -> void:
	var nav := HBoxContainer.new()
	var prev := Button.new()
	prev.text = "◀ 上一刻"
	prev.pressed.connect(func():
		if _debug_tick < 0:
			_debug_tick = GameState.current_tick
		if _debug_tick > 0:
			_debug_tick -= 1
		_render_debug_panel()
	)
	var next := Button.new()
	next.text = "下一刻 ▶"
	next.pressed.connect(func():
		if _debug_tick >= 0 and _debug_tick < GameState.current_tick:
			_debug_tick += 1
		elif _debug_tick < 0:
			pass   # 已在跟随当前
		_render_debug_panel()
	)
	nav.add_child(prev)
	nav.add_child(next)
	left_vbox.add_child(nav)

func _add_debug_step_button() -> void:
	var btn := Button.new()
	btn.text = "▶ 推进下一刻（手动推 tick）"
	btn.pressed.connect(func():
		_add_left_label("世界运转中……")
		ApiClient.request_world_step()
	)
	left_vbox.add_child(btn)

## 调试面板折叠块（问题1结构化排版）：子标题 + 可点击展开的详情区，长内容默认收起
func _debug_header(text: String) -> Label:
	var lbl := Label.new()
	lbl.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	lbl.text = text
	lbl.add_theme_font_size_override("font_size", FONT_SIZE_LEFT)
	return lbl

func _debug_section(title: String, content: String, collapsed := true) -> VBoxContainer:
	var box := VBoxContainer.new()
	box.add_theme_constant_override("separation", 2)
	var hdr := Button.new()
	hdr.flat = true
	hdr.alignment = HORIZONTAL_ALIGNMENT_LEFT
	hdr.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	hdr.text = ("> " if collapsed else "▼ ") + title
	hdr.add_theme_font_size_override("font_size", FONT_SIZE_LEFT)
	# 正文用 MarginContainer 左缩进，视觉上缩在标题下方（文档式）；填满宽度才能正确折行不溢出
	var body := MarginContainer.new()
	body.add_theme_constant_override("margin_left", 14)
	body.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	var detail := Label.new()
	detail.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	detail.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	detail.text = content
	detail.add_theme_font_size_override("font_size", FONT_SIZE_LEFT)
	body.add_child(detail)
	body.visible = not collapsed
	hdr.pressed.connect(func():
		body.visible = not body.visible
		hdr.text = ("▼ " if body.visible else "> ") + title
	)
	box.add_child(hdr)
	box.add_child(body)
	return box

func _debug_prompt_text(prompt_list: Array) -> String:
	var parts: Array[String] = []
	for m in prompt_list:
		var m_d: Dictionary = m
		parts.append("[%s]\n%s" % [_s(m_d.get("role", "")), _s(m_d.get("content", ""))])
	return "\n\n".join(parts)

## /debug/overview 返回 → 渲染调试面板正文
func _on_debug_overview(payload: Dictionary) -> void:
	GameState.current_tick = int(payload.get("current_tick", GameState.current_tick))
	if _debug_tick < 0:
		_debug_tick = int(payload.get("tick", GameState.current_tick))
	if active_tab != "link":
		return   # 不在调试页就不重绘（数据照收，切页时重拉）
	_clear_left()
	var rec: Dictionary = payload.get("recorder", {})
	var tick := int(payload.get("tick", 0))
	# 历史档案：每 tick 永久累积——点编号回看那一 tick 全量记录
	var ticks_avail: Array = payload.get("ticks_available", [])
	if not ticks_avail.is_empty():
		left_vbox.add_child(_debug_header("── 历史档案（点编号回看任意 tick）──"))
		var nav := HBoxContainer.new()
		for t in ticks_avail:
			var t_tick := int(t)
			var tb := Button.new()
			tb.text = str(t_tick)
			if t_tick == tick:
				tb.disabled = true   # 正在看的置灰
			tb.pressed.connect(func():
				_debug_tick = t_tick
				ApiClient.request_debug_overview(t_tick)
			)
			nav.add_child(tb)
		left_vbox.add_child(nav)
	left_vbox.add_child(_debug_header("【上帝视角·按房间】每个空间发生了什么（玩家不可见的后台数据，tick %d / 当前 %d）" % [tick, GameState.current_tick]))
	var envs: Dictionary = {}
	for e in (payload.get("environments", []) as Array):
		var e_d: Dictionary = _d(e)
		envs[_s(e_d.get("env_id", ""))] = _s(e_d.get("name", ""))
	var events: Dictionary = payload.get("events", {})
	if events.is_empty():
		left_vbox.add_child(_debug_header("（本 tick 无空间事件记录）"))
	for sc in events:
		var ev: Dictionary = _d(events[sc])
		var sc_name: String = envs.get(sc, sc)
		var card := VBoxContainer.new()
		card.add_child(_debug_header("位置：%s" % sc_name))
		var kind := _s(ev.get("kind", "empty"))
		if kind == "empty":
			card.add_child(_debug_section("无事发生", "这里空无一人，本 tick 没有动静。", false))
		else:
			for p in (ev.get("people", []) as Array):
				var p_d: Dictionary = _d(p)
				var who := _s(p_d.get("id", ""))
				if _s(p_d.get("kind", "")) == "player":
					who += "（你）"
				var act: Dictionary = _d(p_d.get("action"))
				var act_target := _s(act.get("target", ""))
				if act_target == "":
					act_target = _s(act.get("detail", ""))
				if act_target == "":
					act_target = "—"
				var eff: Dictionary = _d(p_d.get("env_effects"))
				var eff_txt := ""
				for se in (eff.get("self", []) as Array):
					eff_txt += "· 对自身：%s\n" % _s(se)
				for ee in (eff.get("env", []) as Array):
					eff_txt += "· 对环境：%s\n" % _s(ee)
				if eff_txt == "":
					eff_txt = "（无明显可被玩家识别的影响）"
				card.add_child(_debug_section("%s：%s → %s" % [who, _s(act.get("type", "—")), act_target], eff_txt, false))
			var dir_d: Dictionary = _d(ev.get("director"))
			if not dir_d.is_empty():
				var dir_txt := ""
				if _s(dir_d.get("narrative", "")) != "":
					dir_txt += "综述：%s\n" % _s(dir_d.get("narrative", ""))
				for oc in (dir_d.get("outcomes", []) as Array):
					dir_txt += "- %s\n" % _s(oc)
				for ef in (dir_d.get("effects", []) as Array):
					var ef_d: Dictionary = ef
					dir_txt += "- 对 %s：%s\n" % [_s(ef_d.get("target", "")), _s(ef_d.get("effect", ""))]
				if _s(dir_d.get("reasoning", "")) != "":
					dir_txt += "思考：%s\n" % _s(dir_d.get("reasoning", ""))
				card.add_child(_debug_section("导演结果", dir_txt))
		left_vbox.add_child(card)

	# ---------- 【导演系统·处理流程】（第6点：紧随上帝视角，备注房间，含流程+AI 思考）----------
	left_vbox.add_child(_debug_header("【导演系统·处理流程】"))
	var any_dir := false
	for sc in events:
		var ev: Dictionary = _d(events[sc])
		var dir_d: Dictionary = _d(ev.get("director"))
		if dir_d.is_empty():
			continue
		any_dir = true
		var sc_name: String = envs.get(sc, sc)
		var card := VBoxContainer.new()
		card.add_child(_debug_header("◆ 场景「%s」" % sc_name))
		var people_arr: Array = ev.get("people", [])
		var parts: Array[String] = []
		for p in people_arr:
			parts.append(_s((p as Dictionary).get("id", "")))
		card.add_child(_debug_section("参与者", "、".join(parts), false))
		if _s(dir_d.get("narrative", "")) != "":
			card.add_child(_debug_section("裁定结果", _s(dir_d.get("narrative", "")), false))
		var outcomes: Array = dir_d.get("outcomes", [])
		if not outcomes.is_empty():
			var ol: Array[String] = []
			for o in outcomes:
				ol.append("- %s" % _s(o))
			card.add_child(_debug_section("各角色走向", "\n".join(ol)))
		var effects: Array = dir_d.get("effects", [])
		if not effects.is_empty():
			var el: Array[String] = []
			for ef in effects:
				var ef_d: Dictionary = ef
				el.append("- 对 %s：%s" % [_s(ef_d.get("target", "")), _s(ef_d.get("effect", ""))])
			card.add_child(_debug_section("生效效果", "\n".join(el)))
		if _s(dir_d.get("reasoning", "")) != "":
			card.add_child(_debug_section("AI 思考", _s(dir_d.get("reasoning", "")), false))
		left_vbox.add_child(card)
	if not any_dir:
		left_vbox.add_child(_debug_header("（本 tick 无冲突，未触发导演系统）"))

	# ---------- 【逐 NPC 详情】（第4点：@房间 + 多意图权重列表 + AI 思考 + 心智 + 提示词）----------
	left_vbox.add_child(_debug_header("【逐 NPC 详情】"))
	var npcs_all: Dictionary = payload.get("npcs", {})
	var npcs_rec: Dictionary = rec.get("npcs", {})
	for npc_id in npcs_all:
		var d: Dictionary = _d(npcs_all[npc_id])
		var fs: Dictionary = _d(d.get("full_state"))
		var st_d: Dictionary = _d(d.get("status"))
		var live_tag := ""
		if _b(st_d.get("dead", false)):
			live_tag = " ☠已死"
		var card := VBoxContainer.new()
		card.add_child(_debug_header("● %s @ %s%s" % [npc_id, _s(d.get("pos", "未知")), live_tag]))
		var r: Dictionary = _d(npcs_rec.get(npc_id))
		var out: Dictionary = _d(r.get("output"))
		if not out.is_empty():
			# 多意图权重列表（从高到低，标注当前生效项）
			var plan_l: Array = out.get("plan", [])
			if not plan_l.is_empty():
				var cur := int(out.get("_plan_index", 0))
				var pl: Array[String] = []
				for i in plan_l.size():
					var p_d: Dictionary = _d(plan_l[i])
					var mark := "◆" if i == cur else "  "
					var a_d: Dictionary = _d(p_d.get("action"))
					var sp := _s(p_d.get("speech"))
					pl.append("%s 第%d意图：%s｜%s→%s｜%s" % [mark, i + 1, _s(p_d.get("intent", "")),
						_s(a_d.get("type", "")), _s(a_d.get("target", "")), sp if sp != "" else "（不语）"])
				card.add_child(_debug_section("意图权重列表（从高到低）", "\n".join(pl)))
			if _s(out.get("reasoning", "")) != "":
				card.add_child(_debug_section("AI 思考", _s(out.get("reasoning", "")), false))
			var o_action: Dictionary = _d(out.get("action"))
			var detail_part := ""
			if _s(o_action.get("detail", "")) != "":
				detail_part = "（" + _s(o_action.get("detail", "")) + "）"
			card.add_child(_debug_section("本次行动",
				"%s → %s%s\n决策耗时：%s ms" % [_s(o_action.get("type", "")), _s(o_action.get("target", "")),
					detail_part, str(r.get("decide_ms", "-"))], false))
		var learned: Dictionary = _d(r.get("learned"))
		if not learned.is_empty():
			var lm := ""
			var noticed: Array = learned.get("noticed", [])
			if not noticed.is_empty():
				lm += "；".join(PackedStringArray(noticed))
			if _s(learned.get("emotion", "")) != "":
				lm += (("情绪：" if lm != "" else "情绪：") + _s(learned.get("emotion", "")))
			if lm != "":
				card.add_child(_debug_section("得知了什么", lm))
		var env_list: Array = r.get("env", [])
		if not env_list.is_empty():
			var el: Array[String] = []
			for e in env_list:
				var e_d: Dictionary = _d(e)
				var msg := _s(e_d.get("message", ""))
				if msg == "" and String(e_d.get("type", "")) == "reaction":
					msg = _s(e_d.get("narrative", "世界作出了回应"))
				var msg_part := ("：" + msg) if msg != "" else ""
				el.append("%s%s（%s）" % [_s(e_d.get("type", "")), msg_part, _s(e_d.get("outcome", ""))])
			card.add_child(_debug_section("环境影响", "\n".join(el)))
		if not fs.is_empty():
			var e: Dictionary = _d(fs.get("emotion"))
			card.add_child(_debug_section("情绪",
				"%s（tick %s）\n效价：%s\n唤醒：%s\n支配：%s\n强度：%s" % [
				_s(e.get("word", "平静")), str(e.get("updated_tick", "-")),
				_f(e.get("valence")), _f(e.get("arousal")),
				_f(e.get("dominance")), _f(e.get("intensity"))], false))
			var plan_b: Dictionary = _d(fs.get("plan"))
			card.add_child(_debug_section("计划",
				"目标：%s\n状态：%s\n进度：%s\n下一步：%s" % [
				_s(plan_b.get("goal", "无")), _s(plan_b.get("status", "")),
				_s(plan_b.get("progress", "")), _s(plan_b.get("next_step", ""))], false))
			var beliefs: Array = fs.get("beliefs", [])
			if not beliefs.is_empty():
				var bl: Array[String] = []
				for b in beliefs:
					var b_d: Dictionary = b
					bl.append("%s（%s）" % [_s(b_d.get("topic", "")), _s(b_d.get("state", ""))])
				card.add_child(_debug_section("信念", "\n".join(bl)))
			var wm: Array = fs.get("working_memory", [])
			if not wm.is_empty():
				card.add_child(_debug_section("工作记忆", "\n".join(PackedStringArray(wm))))
			var secrets: Array = fs.get("secrets", [])
			if not secrets.is_empty():
				var sl: Array[String] = []
				for s in secrets:
					var s_d: Dictionary = s
					sl.append("%s（口径：%s｜识破%s）" % [
						_s(s_d.get("topic", "")), _s(s_d.get("口径", "")), str(s_d.get("识破进度", 0))])
				card.add_child(_debug_section("秘密", "\n".join(sl)))
			var rel_b: Dictionary = fs.get("relationship_to_player", {})
			if not rel_b.is_empty():
				card.add_child(_debug_section("对玩家",
					"信任：%s\n恐惧：%s\n好感：%s" % [
					str(rel_b.get("trust", "-")), str(rel_b.get("fear", "-")), str(rel_b.get("affection", "-"))], false))
			var held: Array = fs.get("held_items", [])
			if not held.is_empty():
				card.add_child(_debug_section("携带", "\n".join(PackedStringArray(held))))
		var prompt_list: Array = r.get("prompt", [])
		if not prompt_list.is_empty():
			card.add_child(_debug_section("给 AI 的提示词", _debug_prompt_text(prompt_list)))
		for err in (r.get("errors", []) as Array):
			var err_d: Dictionary = err
			card.add_child(_debug_section("⚠ 未收到 AI 回复（%s）" % _s(err_d.get("phase", "")),
				_s(err_d.get("error", ""))))
		left_vbox.add_child(card)

	# 世界历史痕迹（09-09 问题4）：现在是「累计」到本 tick 的所有轮次痕迹（后端已把
	# tick<=t 的痕迹正序返回、每项带 tick 字段），供用户跨轮分析——不再是"只显示本轮"。
	# 每条加 tick 前缀；按 tick 分组聚合，一个 tick 一行标题，更易阅读。
	var traces_all: Array = payload.get("traces", [])
	if not traces_all.is_empty():
		var lines: Array[String] = []
		var last_tick: int = -1
		for x in traces_all:  # 后端已按 tick 正序返回
			var x_d: Dictionary = x
			var tk := int(x_d.get("tick", -1))
			if tk != last_tick:
				if last_tick != -1:
					lines.append("")
				last_tick = tk
				lines.append("〔tick %d〕" % tk)
			lines.append("  %s %s %s@%s：%s" % [_s(x_d.get("actor", "")), _s(x_d.get("type", "")),
				_s(x_d.get("target", "")), _s(x_d.get("location", "")), _s(x_d.get("detail", ""))])
		left_vbox.add_child(_debug_section("世界历史痕迹（累计到本 tick）", "\n".join(lines)))
	_add_debug_step_button()

## /world/updates 返回 → 更新游标 + 只渲染"结算叙述"级提示。
## 09-08 同步回归 + 叙述编排修正（用户拍板）：
##  - phase（"命运的齿轮在咬合中再次转动……"）：第二齿轮，所有角色行动已出、进入相互判定
##  - director（导演叙述）：直接像 DM 讲"这一刻实际发生了什么"，带画面、无"导演说"前缀
##  - 【不再】逐条渲染 NPC 普通动作痕迹（observe/move/attack 的 "test_man: xxx"）——这些
##    应由进房时的文学叙述（narrate_scene 已融合人物动向+玩家有限视角）承载，逐条拼会破墙、
##    拆成两段、且冒出在场景描述之前。也不在此重复触发 _show_scene_in_location（会造成两次）。
##  环境物品变化（拿/放/开关）由 _on_ai_reply 环境路径的 request_inventory / 场景刷新负责。
## 只推进 last_seen_tick 当拿到增量；同步模式下此处即解锁完，无需轮询。
func _on_world_updates(payload: Dictionary) -> void:
	# 09-10 修永久死锁（兜底通道）：增量响应里若带 pending_offer，说明世界正等人裁决对话邀请
	# ——先把邀请弹出来，再照常渲染增量；否则玩家只看到"世界没有变化"，而它其实冻着。
	_handle_world_pause(payload)
	var _was_skip: bool = _skip_reflow_scene
	_skip_reflow_scene = false
	GameState.current_tick = int(payload.get("current_tick", GameState.current_tick))
	var traces: Array = payload.get("traces", [])
	var fresh: Array = []
	for t in traces:
		var t_d: Dictionary = t
		if int(t_d.get("tick", -1)) > GameState.last_seen_tick:
			fresh.append(t_d)
	if not fresh.is_empty():
		for t in fresh:
			var t_d: Dictionary = t
			var who := _s(t_d.get("actor", ""))
			var typ := _s(t_d.get("action_type", t_d.get("type", "")))
			var detail := _s(t_d.get("detail", ""))
			if who == "player":
				continue   # 自己的行动已在旁白/对话里反映，不重复提示
			# 09-10（对话结束回场景汇合）：这一格增量的「齿轮等待词(phase)」和「导演上帝视角
			# 叙事(director)」里含"test_robot 与 player 搭话"这类【对话进行中】的描述，与
			# conv_end 转场("话题的余音散尽")语境割裂、视为"生成两条"；且该格的场景叙事已由
			# _show_scene_in_location(..., "conv_end") 的转场旁白承接。故回场景时跳过这两类，
			# 只保留真正的世界变化（他人移动/观察等）。解锁判定(current_tick)与此无关，不受影响。
			# 09-10：独立 conv_end 文学旁白已删，对话结束回场景的叙事改由【导演 player_view】承接
			# （对话格 player_view 语义："你回过神来，注意到…"）。故回场景时【不再跳过 director】让
			# 玩家视角叙述显示；仅跳过 phase（齿轮等待词，避免插在对话结束转场中间）。
			if _end_conv_pending and typ == "phase":
				continue
			if typ == "phase":
				# 第二齿轮：所有角色行动已出、开始相互影响判定
				add_message("旁白", "—— %s ——" % detail, false)
				continue
			if typ == "director":
				# 导演叙述：后端已把 trace detail 落为【玩家视角包装 player_view】而非全知 narrative。
				# 空则跳过（玩家不可感知，防空旁白/防泄露全知）。
				if detail != "":
					add_message("旁白", detail, false)
				continue
			# 09-08【死人入口修复】：本 tick 世界有变化（人物死亡/进入/离开等）→ 刷新当前场景的
			# 交谈入口（去掉死人按钮、补齐新人）。复用 entries_only：只重建按钮，不重复旁白。
			# _was_skip 时下方已走 _show_scene_in_location 全量重述（含重建入口），此处跳过避免重复。
			if not _was_skip:
				_refresh_scene_entries()
		# 09-10 arrival_view 修复：游标必须【无条件】推进到 current_tick。原实现只在遇到"普通类型"
		# 痕迹时才推进，而移动那一格的痕迹只有两类——玩家自己的 move（actor=player）与导演的
		# arrival_view（director），两者都在上方被 continue 跳过 → 游标不动 → 下次
		# request_world_updates(since=旧游标) 会把同一条到达画面【重复显示】。故统一在此推进。
		GameState.last_seen_tick = GameState.current_tick
	# 09-10 权威位置回同步（用户现场："输入去房间二，婉拒后位置还在房间三"）：
	# 文字输入触发的移动由【后端】执行器落地（不是点地图那条 /session/move 路径），
	# 而 switch_location 只在 _on_move_received 里被调用 → 客户端 location_id 会一直停在旧房间，
	# 顶栏、地图、交谈入口全部过期（"后端到了、界面原地不动"）。
	# 这里以 /world/updates 带回的权威 player_scene 为准；不一致才切（幂等，不影响点地图路径）。
	var srv_scene := _s(payload.get("player_scene", ""))
	if srv_scene != "" and srv_scene != GameState.location_id:
		# switch_location → location_changed → show_location → 分隔符 + 场景块 + 交谈入口刷新
		GameState.switch_location(srv_scene)
	# 09-08 问题2：略过时间 = 原地待了一会儿，重新描述场景（用 lingering 停留视角，
	# 而非"你刚走进来"）。由 _on_skip 置 _skip_reflow_scene=true，这里在拿到世界变化后重述一次。
	if _was_skip:
		_show_scene_in_location(GameState.location_id, "lingering")
	# 09-09 第4步（对话结束回场景汇合）：后台那一个tick是否已结算完？判定=后端 current_tick 是否
	# 已超过"对话前基准tick"（_end_conv_target_tick）。已超过 → 该tick增量已落地，标 world_done ，
	# 若场景也渲染完则统一解锁；未超过 → 后台仍在算这个tick，保持忙碌并轮询再拉。
	if _end_conv_pending:
		var cur := int(payload.get("current_tick", 0))
		if cur > _end_conv_target_tick:
			_end_conv_world_done = true
			if _end_conv_scene_done:
				_finish_end_conv_merge()
		else:
			_poll_end_conv_world()
