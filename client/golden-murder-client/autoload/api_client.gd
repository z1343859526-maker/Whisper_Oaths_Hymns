extends Node
## ApiClient —— 后端通信（autoload 单例）
## 职责：连到 FastAPI 后端，把「玩家输入 + 该 NPC 感知的世界状态」发过去，拿回 NPC 回复。
## 这是"LLM 与游戏引擎集成、实时感知状态"的客户端一侧。
## 目前只定接口签名（形状），P4 才填实现：a) HTTPRequest POST /chat；b) WebSocketPeer 实时对话 + 主动推送。

## 后端地址（本地 localhost 即在线架构，无需部署）
const BASE_URL := "http://127.0.0.1:8000"

## 是否已连接（P4 用 WebSocketPeer 时才为 true）
var connected := false

## 请求超时（秒）：超过仍未返回视为失败。
## 注意：必须 ≥ 后端 LLM 上限 + 世界 tick 同步结算时长。后端 DeepSeekClient 超时 30s，
## 「对象观察/旁白」首次生成要 LLM 写长描述（实测约 24s）；多人同场景导演判定再叠加
## 多个 NPC 并行 LLM。09-08 已改回【同步 advance_one】阻塞到本 tick 全部结算完——
## 故超时放宽到 600s（10min 兜底），宁可等也不掐断导致假超时出兜底话。
const TIMEOUT_SECONDS := 600.0
## 后端不可用时的兜底回复（避免请求失败时游戏裸奔）
const FALLBACK_REPLY := "……（此人沉默地打量着你。）"

## ---------- 后端守护：开游戏自动拉起后端 ----------
## 启动脚本（客户端专用，无 pause，窗口关闭即结束；手动开发仍可用 start_server.bat）。
## 按仓库布局相对定位：server/ 与 client/ 平级。编辑器内运行 res:// 即工程目录；
## 导出发布版不含 server/，届时请手动启动 server/start_server.bat。
func _backend_bat_path() -> String:
	return ProjectSettings.globalize_path("res://").path_join("../../server/launch_backend.bat")
## 就绪等待上限：最多探测次数；每次间隔秒（09-09 设计决定：加载界面到 75% 才首次探测，
## 之后每 0.6s 探测一次，确认全部信息就绪即快速补完）
const BACKEND_PROBE_MAX := 20
const BACKEND_PROBE_INTERVAL := 0.6

## P3-A 第②步：回复通过这两个信号交回给界面（异步，不能同步 return）
signal reply_received(npc_id: String, reply_text: String)   # 正常拿到 AI 回复
signal reply_fallback(npc_id: String, reply_text: String)   # 失败，已回退到兜底话

## 09-10 修永久死锁：/chat 响应里带回"世界被对话邀请挂起 / 上一格还在结算"的字段
## （world_paused / world_skipped / pending_offer / notice）时发出，界面据此弹邀请或明确提示。
## 背景：advance_one 的挂起态过去被 /chat 直接丢掉，前端只收到一句"命运的齿轮开始转动"，
## 而世界其实已冻结在那一格 → 玩家永久卡住且毫无提醒（2026-09-10 实测）。
signal world_paused_received(payload: Dictionary)

## 是否已有请求在途（防玩家连点发送导致回复错位）
var _requesting := false

## ---------- 后端守护运行时状态 ----------
var _backend_ready := false   # 是否已探测到后端就绪（幂等：避免重复探测）
var _backend_pid: int = 0      # 被拉起进程的 PID（0 = 尚未拉起，供退出时 taskkill）
var _probe_count := 0          # 已轮询探测次数（达 BACKEND_PROBE_MAX 即放弃，避免无限等）

## 请求一个 NPC 对玩家本轮输入/行动的回复（P3-A：真正 POST /chat）。
## 注意：这是异步的——发出去就返回，结果经 reply_received / reply_fallback 信号交回，
## 所以签名是 void + 信号，而不是"返回 String"。
## @param npc_id    正在对话的 NPC id
## @param text      玩家输入（说的话 / 做的事）
## @param _context  游戏状态快照（GameState.snapshot_for_npc 的产物），P4 用于"感知世界"，
##                  P3-A 暂未用——用 _ 前缀标示"故意不用"，消除 UNUSED_PARAMETER 警告
func request_npc_reply(npc_id: String, text: String, _context: Dictionary = {}) -> void:
	if _requesting:
		return                       # 上一个请求还没回，忽略本次，避免两条回复错位
	_requesting = true

	var http := HTTPRequest.new()
	http.timeout = TIMEOUT_SECONDS
	add_child(http)
	# 结果回来时调 _on_chat_done；把 http 和 npc_id 一并绑过去（bind 追加在信号参数之后）
	http.request_completed.connect(_on_chat_done.bind(http, npc_id))

	# P4-A：告诉后端这句话是"哪个 NPC"在回应，后端按它加载角色卡拼人设。
	# world_id：世界维度（不同世界不同 RAG 库）——从 GameState.world_id 读，
	#           让后端只检索当前世界的知识（测试世界不再串到黄金乡）。
	# session_id：游戏启动时 POST /session/start 获得的会话（缺省空则后端兜底 adhoc）。
	# 让记忆/关系/玩家位置按会话隔离（M1.1）。
	# scene：把客户端当前房间一并传给后端。环境观察/行动靠它定位"你在哪个场景找东西"，
	# 不依赖后端 sync_location（时序/会话不一致会导致后端 scene 为空→找不到椅子）。
	var payload := {"message": text, "npc_id": npc_id, "session_id": GameState.session_id, "world_id": GameState.world_id, "scene": GameState.location_id}
	var headers := ["Content-Type: application/json"]   # 告诉后端"发的是 JSON"
	var err := http.request(BASE_URL + "/chat", headers, HTTPClient.METHOD_POST, JSON.stringify(payload))
	if err != OK:                                       # 请求根本发不出去（如 URL 错）
		_requesting = false
		reply_fallback.emit(npc_id, FALLBACK_REPLY)
		http.queue_free()

## request_completed 回调：result=0 才是成功；否则超时/连不上 → 回退兜底
func _on_chat_done(result: int, code: int, _headers: PackedStringArray, body: PackedByteArray, http: HTTPRequest, npc_id: String) -> void:
	_requesting = false
	http.queue_free()                       # 用完即毁，避免请求节点堆积
	if result != HTTPRequest.RESULT_SUCCESS:
		# 可观测：把失败原因打到 Godot 输出，而不是只给统一兜底话（result≠0=超时/连不上）
		push_warning("[ApiClient] /chat 失败（result=%s code=%s）→ 回退兜底话。" % [result, code])
		reply_fallback.emit(npc_id, FALLBACK_REPLY)
		return
	var raw := body.get_string_from_utf8()
	# 09-10 修永久死锁（透传链）：先看响应里有没有"世界被挂起/没推进"的字段——有就先抛给
	# 界面（弹邀请或明确提示），再照常走回复。否则前端只看到"命运的齿轮开始转动"，
	# 世界却已冻结在那一格，玩家完全无从判断发生了什么。
	var _data: Variant = JSON.parse_string(raw)
	if _data is Dictionary and (_data.has("world_paused") or _data.has("world_skipped")):
		world_paused_received.emit(_data)
	var reply := _parse_reply(raw)
	if reply.is_empty():
		reply_fallback.emit(npc_id, FALLBACK_REPLY)
	else:
		reply_received.emit(npc_id, reply)   # 把 AI 回复抛给界面

## 布尔安全收敛：后端 JSON 字段类型不定（null/字符串/数字都可能），
## GDScript 的 bool() 构造对部分类型直接崩（Invalid call 'bool' constructor）。
## autoload 无法调用 main.gd 的 _b，这里独立实现一份（规则一致）。
func _as_bool(v) -> bool:
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

## 解析后端 JSON {"reply": "..."} → 取文本；解析失败返回空串（交由上层回退）
func _parse_reply(json_text: String) -> String:
	var data: Variant = JSON.parse_string(json_text)
	if data is Dictionary and data.has("reply"):
		return String(data["reply"]).strip_edges()
	return ""

## ---------- 会话（M1.1）：开局建立，此后请求都带 session_id ----------
## 开局建立会话：POST /session/start，把返回的 session_id 存进 GameState。
## 此后 /chat 与 /session/location 都带它，记忆/关系/玩家位置按会话隔离。
## 空 session_id 时后端会自动兜底 adhoc，不阻塞游戏。
func start_session() -> void:
	var http := HTTPRequest.new()
	http.timeout = TIMEOUT_SECONDS
	add_child(http)
	http.request_completed.connect(_on_session_started.bind(http))
	# M1.10（09-08 需求"每次游戏重启自动初始化"）：带上当前世界，后端据此复位
	# 该世界环境卡到出厂——否则上一局拿过/挪过的物品状态（如开局背刀、房间却空无一物）
	# 会污染本局。
	http.request(BASE_URL + "/session/start?world_id=" + GameState.world_id, [], HTTPClient.METHOD_POST)

func _on_session_started(result: int, code: int, _headers: PackedStringArray, body: PackedByteArray, http: HTTPRequest) -> void:
	http.queue_free()
	if result != HTTPRequest.RESULT_SUCCESS or code != 200:
		return   # 后端未就绪等：保持 session_id 为空，后端兜底 adhoc，不阻塞游戏
	var data: Variant = JSON.parse_string(body.get_string_from_utf8())
	if data is Dictionary and data.has("session_id"):
		GameState.session_id = String(data["session_id"])
		print("[ApiClient] 会话已建立：", GameState.session_id)
		session_created.emit()   # 通知界面：session_id 已就绪 → 界面重新拉当前场景补"交谈"入口
		# ✨ 关键：会话建立后立刻同步开局位置。后端初始 scene="gate"（黄金乡的门，对测试世界无效），
		# 若玩家开局不先移动就做环境行动/对话，旁白"你所在"会错。这里把后端 scene 拉齐到客户端起点
		# （测试世界=room_1），此后玩家再地图移动/环境移动都会继续同步，两侧始终一致。
		sync_location(GameState.location_id)

## ---------- 地图移动同步（需修：客户端只改本地 location_id 不通知后端） ----------
## 玩家在地图上移动成功后调用，把新位置写回后端 game_state['scene']，
## 此后旁白读 get_player_scene 就与客户端一致（修"你所在"错）。
## scene = 客户端 location_id。
func sync_location(scene: String) -> void:
	var http := HTTPRequest.new()
	http.timeout = TIMEOUT_SECONDS
	add_child(http)
	http.request_completed.connect(_on_sync_location_done.bind(http, scene))
	var payload := {"session_id": GameState.session_id, "scene": scene, "world_id": GameState.world_id}
	var headers := ["Content-Type: application/json"]
	http.request(BASE_URL + "/session/location", headers, HTTPClient.METHOD_POST, JSON.stringify(payload))

func _on_sync_location_done(result: int, code: int, _headers: PackedStringArray, _body: PackedByteArray, http: HTTPRequest, scene: String) -> void:
	http.queue_free()
	if result == HTTPRequest.RESULT_SUCCESS and code == 200:
		print("[ApiClient] 玩家位置已同步到后端：", scene)
	else:
		push_warning("[ApiClient] 玩家位置同步失败（后端未就绪？地址错？）：", scene)

## ---------- 背包数据源（需修：背包要根据玩家身上物品动态更新，而非本地静态数组） ----------
## 拉取玩家当前手上持有物（GET /session/inventory），刷新 GameState.items。
## 调用时机：做完"拿/放"环境行动后、进入背包页时。
func request_inventory() -> void:
	var http := HTTPRequest.new()
	http.timeout = TIMEOUT_SECONDS
	add_child(http)
	http.request_completed.connect(_on_inventory_done.bind(http))
	var url := "%s/session/inventory?session_id=%s&world_id=%s" % [BASE_URL, GameState.session_id, GameState.world_id]
	http.request(url, [], HTTPClient.METHOD_GET)

func _on_inventory_done(result: int, code: int, _headers: PackedStringArray, body: PackedByteArray, http: HTTPRequest) -> void:
	http.queue_free()
	if result != HTTPRequest.RESULT_SUCCESS or code != 200:
		return   # 拿不到就保持现有背包（不阻塞，下次再刷）
	var data: Variant = JSON.parse_string(body.get_string_from_utf8())
	if data is Dictionary and data.has("names"):
		GameState.set_items(data["names"])   # 更新背包 + 广播 items_changed

## 占位：接收后端 NPC 主动推送（主动消息栏用）。
func _on_server_push(_payload: Dictionary) -> void:
	# TODO(P4): 解析后端主动消息 → 交给主动消息栏
	pass

## 最小探测：向后端 /health 发一个 GET，把返回体通过 _on_ping_done 打印出来。
## 目的是验证"Godot → 后端"这条管道是否真的通（第①步验收点）。
func ping() -> void:
	var http := HTTPRequest.new()          # 每次请求建一个独立节点，用完即弃
	add_child(http)
	# 收到结果后调 _on_ping_done，并把 http 传回去以便清理
	http.request_completed.connect(_on_ping_done.bind(http))
	var err := http.request(BASE_URL + "/health", [], HTTPClient.METHOD_GET)
	if err != OK:
		push_error("请求发起失败（err=%s）" % error_string(err))
		http.queue_free()

## request_completed 信号回调：result=0 才算成功，body 是 PackedByteArray(bytes)
func _on_ping_done(result: int, _code: int, _headers: PackedStringArray, body: PackedByteArray, http: HTTPRequest) -> void:
	http.queue_free()                      # 用完销毁该请求节点，避免堆积
	if result != HTTPRequest.RESULT_SUCCESS:
		push_error("请求失败（result=%s）" % result)
		return
	var text := body.get_string_from_utf8() # bytes → 字符串
	print("[ApiClient] /health 返回：", text)

## ---------- 后端守护：开游戏自动拉起后端 ----------
## 入口（main.gd 的 _ready() 调用）：先探测 /health，通了直接用；不通则拉起后端进程，再轮询等待。
## 幂等：已就绪就直接返回，不重复探测/重启。后端起不来也不阻塞游戏——对话走兜底话。
## 09-09 需求：把"后台是否打开"纳入加载页检验进度。这里发 backend_status 状态词，
## 加载页据此显示"正在检查后端/后端已就绪/正在启动后端/后端未能就绪"等明确提示，
## 而不是进度条默默停在某处、玩家不知道后台到底开没开、到哪一步。
signal backend_status(text: String)
func ensure_backend_ready() -> void:
	if _backend_ready:
		return
	# 09-09 需求"每次打开若监测到没关就先关再开"：进游戏先清掉上轮可能残留的后端
	# （无论它是不是上次客户端拉的——残留/手动起的都可能是旧代码，且占着 8000 会让复用
	# 了旧进程）。先按端口杀掉，再重新探测+拉起，保证本局一定跑在【当前】代码上。
	# 已就绪（复用中）则不动，避免每次进入都重启后端打断进行中的会话。
	_kill_backend_tree()
	_backend_ready = false
	_backend_pid = 0
	_probe_count = 0
	_probe_backend()

## 探测一次 /health：发一个独立 HTTP 请求，结果在 _on_probe_done 里决策（成功=就绪/失败=拉起+轮询）。
func _probe_backend() -> void:
	backend_status.emit("正在检查后端……")
	var http := HTTPRequest.new()
	http.timeout = 2.0   # 探测用短超时：连不上时快速失败，立刻转去拉起/重试，而非干等 15 秒
	add_child(http)
	http.request_completed.connect(_on_probe_done.bind(http))
	http.request(BASE_URL + "/health", [], HTTPClient.METHOD_GET)

## 探测结果回调：result=0 且 HTTP 200 才视为"后端就绪"；否则驱动"拉起进程 + 隔秒轮询"。
func _on_probe_done(result: int, code: int, _headers: PackedStringArray, _body: PackedByteArray, http: HTTPRequest) -> void:
	http.queue_free()
	if result == HTTPRequest.RESULT_SUCCESS and code == 200:
		_backend_ready = true
		backend_status.emit("后端已就绪")
		print("[ApiClient] 后端已就绪：", BASE_URL)
		start_session()   # 后端可用后立即建会话，让后续 /chat、/session/location 都带 session_id
		return
	# 未就绪：只在第一次失败时拉起后端（_backend_pid != 0 表示已拉过，避免重复弹窗）
	if _backend_pid == 0:
		_spawn_backend()
	# 轮询计数：达上限仍未就绪则放弃，游戏继续（对话将命中兜底话），下次 _ensure 不会再来烦。
	_probe_count += 1
	if _probe_count >= BACKEND_PROBE_MAX:
		push_warning("[ApiClient] 后端启动超时（%d 次探测未通），对话将回退兜底话。" % BACKEND_PROBE_MAX)
		backend_status.emit("后端未能就绪，将以简化模式进入")
		return
	get_tree().create_timer(BACKEND_PROBE_INTERVAL).timeout.connect(_probe_backend)

## 拉起后端：用 cmd 新开一个终端窗口运行 launch_backend.bat，并记下 PID 供退出时关闭。
## 用 OS.create_process（非阻塞，游戏继续），open_console=true 让控制台窗口可见（方案A：便于看日志）。
func _spawn_backend() -> void:
	var bat := _backend_bat_path()
	if not FileAccess.file_exists(bat):
		push_warning("[ApiClient] 后端启动脚本不存在：%s" % bat)
		backend_status.emit("后端启动脚本缺失")
		return
	backend_status.emit("后端未运行，正在启动……")
	_backend_pid = OS.create_process("cmd.exe", ["/c", bat], true)
	if _backend_pid == 0:
		push_warning("[ApiClient] 后端进程拉起失败（pid=0）。")
		backend_status.emit("后端启动失败")
	else:
		print("[ApiClient] 已拉起后端进程（pid=%s），等待就绪……" % _backend_pid)

## ---------- 游戏关闭 → 自动关掉被拉起的后端窗口 ----------
## 用 taskkill 结束该进程树（/T 连同 uvicorn --reload 的子进程一并结束），窗口随之关闭。
##
## 09-09 修复"自动关闭不彻底"：旧实现只杀 _backend_pid（客户端【自己拉起】并记录的 PID），
## 且一旦游戏复用了【残留/外部】的后端（_backend_pid==0，如手动 start_server.bat 起的旧进程）
## 就直接 return 不关——导致退出后旧后端仍占 8000。现在改为按【端口 8000】定位并杀整棵进程树，
## 无论后端是客户端拉的还是残留的旧进程，都能清干净；uvicorn --reload 的 reloader/worker
## 子进程也会被 /T 连根拔掉，不再留孤儿占端口。也兜底杀一次 _backend_pid（兼容旧记录）。
func _exit_tree() -> void:
	print("[ApiClient] 游戏退出，清理后端进程……")
	_kill_backend_tree()
	if _backend_pid != 0:
		OS.execute("taskkill", ["/PID", str(_backend_pid), "/T", "/F"])
		_backend_pid = 0


## 按端口 8000 定位监听进程，并杀其整棵进程树（/T 连 reloader+worker 子进程一并结束）。
## 为什么用端口而非 PID：PID 会随每次拉起变化（残留/复用/新起都不同），端口才是稳定的后端占位。
## Godot 4 的 OS.execute(path, args, output) 把标准输出写进 output【Array】（引用传递），返回 exit code。
## ⚠️ 易错：output 参数必须是 Array，传 PackedStringArray 会导致输出捕获不到 → PID 解析不到 → 杀不掉。
## 返回杀掉了多少监听进程（0 = 端口空闲，无需清理）。
func _kill_backend_tree() -> int:
	var killed := 0
	# 用 Array（不是 PackedStringArray）承接 netstat 标准输出，每行一个元素。
	var lines: Array = []
	OS.execute("netstat", ["-ano"], lines)
	var pids := {}
	for line in lines:
		var l := String(line).strip_edges()
		if l.find(":8000") == -1 or l.find("LISTENING") == -1:
			continue
		var tok := l.split(" ")
		for i in range(tok.size() - 1, -1, -1):
			var t := tok[i].strip_edges()
			if t != "" and t.is_valid_int():
				pids[t] = true   # 有多个 8000 连接行，去重 PID
				break
	for pid in pids:
		OS.execute("taskkill", ["/PID", pid, "/T", "/F"])
		killed += 1
		print("[ApiClient] 已关闭后端进程树（pid=%s）" % pid)
	return killed

## ---------- 在线世界时钟（OL-6/7）：世界变化拉取 + 调试面板数据源 ----------
## 玩家每次交互后，后端会自动推一个 tick（所有 NPC 在其中运转）。
## 这里提供两个拉取器：
##   request_world_updates —— 拉取"上次已见 tick 之后"的世界变化（痕迹增量），
##                            结果经 world_updates_received 信号交回界面做提示；
##   request_debug_overview —— 拉取上帝视角 recorder（调试面板逐 tick 展示）。
signal world_updates_received(payload: Dictionary)
signal debug_overview_received(payload: Dictionary)
## A（环境描述动态化）：进房间拉"此刻实际状态"的快照（替代本地静态 desc）
signal scene_inspect_received(payload: Dictionary)
## 09-09：/scene/inspect 拉取失败/超时（不再回调 scene_inspect_received）时发出，供界面
## 解除"等待重述"的忙碌锁，避免略过重述场景失败时忙碌永久卡住（防死锁兜底）。
signal scene_inspect_failed
## D（全同步时序）：/world/step 手动推 tick 已结算（世界已推完）→ 客户端解锁输入/移动
signal world_step_done
## 会话建立成功（拿到真实 session_id 并已同步位置）后发出：界面借此重新拉当前场景，
## 解决"开局 request_scene_inspect 用了空 session_id → 后端 adhoc 无 npc_pos → 无交谈入口"。
signal session_created
## 方向1（点地图移动）：POST /session/move 结算完成 → 客户端据此切地点 + 解锁
signal move_received(payload: Dictionary)

## 09-10 新增：即时提示（"机器在忙什么"的侧信道）。
## 背景：玩家自由输入的解析层可能触发"再解析一次"，但这件事发生在 /chat 这个【同步阻塞】
## 请求内部——请求没返回，前端拿不到任何中途进度，而这一格世界结算还要等几十秒。
## 后端把提示（如"事情比想象中复杂……"）推进提示池，前端在忙碌期间轮询本端点取走显示。
signal notices_received(notices: Array)

## 世界变化拉取：GET /world/updates?since_tick=上次已见。玩家回到界面/每次交互后调用——
## "挂机期间有人进入/物品变动/门被开关"都从这里变成前端提示（需求）。
## since_tick 可选：显式传入则用它（异步轮询时传"出发时的 tick"，绕过 last_seen_tick
## 被 `_on_world_updates` 提前推进导致的游标跳变——否则轮询第二个增量会被跳过）。
## 不传则默认用 GameState.last_seen_tick。
func request_world_updates(since_tick: int = -999, until_tick: int = -999) -> void:
	var http := HTTPRequest.new()
	http.timeout = 30.0
	add_child(http)
	http.request_completed.connect(_on_world_updates_done.bind(http))
	since_tick = GameState.last_seen_tick if since_tick == -999 else since_tick
	# until_tick 可选上界（09-10 方向2 闭环）：结束对话回场景只拉「发起对话那一格」的增量，
	# 掐断 A/B/C 多 tick 堆叠；<0 表示不限上界（全局默认）。
	var url := "%s/world/updates?session_id=%s&world_id=%s&since_tick=%d" % [
		BASE_URL, GameState.session_id, GameState.world_id, since_tick]
	if until_tick >= 0:
		url += "&until_tick=%d" % until_tick
	http.request(url, [], HTTPClient.METHOD_GET)

func _on_world_updates_done(result: int, code: int, _headers: PackedStringArray, body: PackedByteArray, http: HTTPRequest) -> void:
	http.queue_free()
	if result != HTTPRequest.RESULT_SUCCESS or code != 200:
		return   # 拉不到就下次再拉（不阻塞）
	var data: Variant = JSON.parse_string(body.get_string_from_utf8())
	if data is Dictionary:
		world_updates_received.emit(data)

## 即时提示轮询（09-10）：GET /session/notices —— 取走后端"正在忙什么"的提示并清空池。
## 排干式语义：后端取走即清空，前端直接显示、无需去重。
## 超时给 10s（提示端点不调 LLM，很快）；失败静默——提示只影响体验，绝不能拖累主流程。
func request_notices() -> void:
	var http := HTTPRequest.new()
	http.timeout = 10.0
	add_child(http)
	http.request_completed.connect(_on_notices_done.bind(http))
	var url := "%s/session/notices?session_id=%s" % [BASE_URL, GameState.session_id]
	http.request(url, [], HTTPClient.METHOD_GET)

func _on_notices_done(result: int, code: int, _headers: PackedStringArray, body: PackedByteArray, http: HTTPRequest) -> void:
	http.queue_free()
	if result != HTTPRequest.RESULT_SUCCESS or code != 200:
		return
	var data: Variant = JSON.parse_string(body.get_string_from_utf8())
	if data is Dictionary:
		notices_received.emit(data.get("notices", []))

## 调试面板数据源：GET /debug/overview?tick=当前（缺省=后端当前 tick）。
## 返回 recorder 全量（每 NPC 得知/提示词/行动标签/AI输出/环境影响/LLM耗时）+ 世界状态。
func request_debug_overview(tick: int = -1) -> void:
	var http := HTTPRequest.new()
	http.timeout = 30.0
	add_child(http)
	http.request_completed.connect(_on_debug_overview_done.bind(http))
	var url := "%s/debug/overview?session_id=%s&world_id=%s" % [BASE_URL, GameState.session_id, GameState.world_id]
	if tick >= 0:
		url += "&tick=%d" % tick
	http.request(url, [], HTTPClient.METHOD_GET)

func _on_debug_overview_done(result: int, code: int, _headers: PackedStringArray, body: PackedByteArray, http: HTTPRequest) -> void:
	http.queue_free()
	if result != HTTPRequest.RESULT_SUCCESS or code != 200:
		return
	var data: Variant = JSON.parse_string(body.get_string_from_utf8())
	if data is Dictionary:
		debug_overview_received.emit(data)

## 手动推 tick（调试面板"推进下一刻"按钮）：POST /world/step。
func request_world_step() -> void:
	var http := HTTPRequest.new()
	http.timeout = 60.0
	add_child(http)
	http.request_completed.connect(_on_world_step_done.bind(http))
	var payload := {"session_id": GameState.session_id, "world_id": GameState.world_id}
	var headers := ["Content-Type: application/json"]
	http.request(BASE_URL + "/world/step", headers, HTTPClient.METHOD_POST, JSON.stringify(payload))

signal world_step_advanced(advanced: bool, current_tick: int)

func _on_world_step_done(_result: int, _code: int, _headers: PackedStringArray, body: PackedByteArray, http: HTTPRequest) -> void:
	http.queue_free()
	# 解析 /world/step 返回体：advanced 是否真的推进了一格、current_tick 最新值。
	# 此前忽略返回体 → 略过后世界虽推进但 current_tick 没进 GameState，游标滞后，
	# _on_world_updates 的 fresh 检测不到增量痕迹 → "时间消耗了但没反应"。
	var advanced := false
	var new_tick := int(GameState.current_tick)
	var data: Variant = JSON.parse_string(body.get_string_from_utf8())
	if data is Dictionary:
		advanced = _as_bool(data.get("advanced", false))
		new_tick = int(data.get("current_tick", GameState.current_tick))
		GameState.current_tick = new_tick
	# 后端 /world/step 同步跑完本 tick 才返回 → 到这里即"世界已结算"，解锁客户端输入/移动
	world_step_done.emit()
	world_step_advanced.emit(advanced, new_tick)
	# 推完立刻刷新调试面板与场景（世界变化提示）
	request_debug_overview()
	request_world_updates()

## 方向1：点地图移动 = 一次完整世界行动（POST /session/move）。
## 后端执行 environment._exec_move 语义——校验可达、写 game_state['scene'] + 世界痕迹、
## 再推进一个世界 tick（与 /chat、/world/step 同一推进入口）。全部完成才返回。
## 结果经 move_received 回传：客户端据此切地点 + 拉世界变化 + 解锁。杜绝"看到房间2、
## 执行器/NPC 却读到旧房间"的场景错位。
func request_move(target: String) -> void:
	var http := HTTPRequest.new()
	http.timeout = TIMEOUT_SECONDS
	add_child(http)
	http.request_completed.connect(_on_move_done.bind(http))
	var payload := {"session_id": GameState.session_id, "world_id": GameState.world_id, "target": target}
	var headers := ["Content-Type: application/json"]
	http.request(BASE_URL + "/session/move", headers, HTTPClient.METHOD_POST, JSON.stringify(payload))

func _on_move_done(result: int, code: int, _headers: PackedStringArray, body: PackedByteArray, http: HTTPRequest) -> void:
	http.queue_free()
	if result != HTTPRequest.RESULT_SUCCESS or code != 200:
		move_received.emit({})   # 请求失败：按"移动失败"处理（解锁 + 提示）
		return
	var data: Variant = JSON.parse_string(body.get_string_from_utf8())
	move_received.emit(data if data is Dictionary else {})

## A：进房间拉当前场景动态感知（文学叙事 narrative + 事实快照 snapshot + 在场 NPC）。
## 结果经 scene_inspect_received 交回界面，反映"此刻实际状态"，替代本地静态 desc。
## 超时放大：narrative 需调 LLM 生成，可能 30s+；宁等不掐（超时回退静态 desc）。
func request_scene_inspect(scene: String, view: String = "arriving") -> void:
	## view（09-08 问题1/2）：arriving=刚进入（移动过来）；lingering=停留后重看（对话后/略过）。
	## 透传给后端 narrate_scene，决定文学旁白用"你刚走进来"还是"你在此逗留片刻"视角。
	var http := HTTPRequest.new()
	http.timeout = 60.0
	add_child(http)
	http.request_completed.connect(_on_scene_inspect_done.bind(http))
	var url := "%s/scene/inspect?session_id=%s&scene=%s&world_id=%s&view=%s" % [
		BASE_URL, GameState.session_id, scene, GameState.world_id, view]
	http.request(url, [], HTTPClient.METHOD_GET)

func _on_scene_inspect_done(result: int, code: int, _headers: PackedStringArray, body: PackedByteArray, http: HTTPRequest) -> void:
	http.queue_free()
	if result != HTTPRequest.RESULT_SUCCESS or code != 200:
		# 09-09：失败/超时不再是"静默返回"。发失败信号，让界面（若在"等待略过重述"）解除忙碌锁，
		# 否则略过后若 /scene/inspect 拉不到，忙碌会永久卡住（原本解锁点在它的成功回调）。
		scene_inspect_failed.emit()
		return   # 拉不到就保持本地静态 desc兜底（不阻塞，下次进房间再拉）
	var data: Variant = JSON.parse_string(body.get_string_from_utf8())
	if data is Dictionary:
		scene_inspect_received.emit(data)

## 模组元信息（开场白/名称）：GET /world/info。开场白不再硬编码在客户端——
## 换模组（world_id）= 换一套开场与文风，客户端零改动。
signal world_info_received(payload: Dictionary)

func request_world_info() -> void:
	var http := HTTPRequest.new()
	http.timeout = 15.0
	add_child(http)
	http.request_completed.connect(_on_world_info_done.bind(http))
	var url := "%s/world/info?world_id=%s" % [BASE_URL, GameState.world_id]
	http.request(url, [], HTTPClient.METHOD_GET)

func _on_world_info_done(result: int, code: int, _headers: PackedStringArray, body: PackedByteArray, http: HTTPRequest) -> void:
	http.queue_free()
	if result != HTTPRequest.RESULT_SUCCESS or code != 200:
		return
	var data: Variant = JSON.parse_string(body.get_string_from_utf8())
	if data is Dictionary:
		world_info_received.emit(data)

## ---------- 对话系统 v0.4：邀请 → 同意/拒绝 → 多轮逐句 → 结束 ----------
## 对话邀请产生于世界 tick 暂停（后端 game_state 的 pending_conv_offer）——某本 tick NPC
## 想"跟玩家说话"，后端把世界停在"待玩家裁决"，前端据此弹"XX 想跟你对话"。
## 触发方式（重要）：/chat、/session/move 的 advance_one 返回值被丢弃、不透传 offer，
## 所以前端在【每次世界结算完成后】主动拉一次 /conversation/invite 查询是否有待裁决邀请。
## 会话期间 world.advance_one 被 active_conv 挂起（不额外推进 tick），只耗进入对话那 1 tick。

## 查询到"待玩家裁决的对话邀请"（offer 非空 => 弹确认询问）
signal conversation_invite_received(offer: Array)
## 玩家同意对话（后端已建 active_conv，payload 含 initiator / first_line）
signal conversation_accepted(payload: Dictionary)
## 玩家拒绝对话（后端已按优先级递推该 NPC 下一步，world 恢复）。
## 09-10：payload 透传后端世界时序字段——
##   · 正常结算 → 几乎空 dict（只剩 accepted/declined）：此时那一格已被真正结算，前端须补拉
##     一次 /world/updates 才能拿回本格产物（导演叙述 + 结果痕迹，含玩家自己那一步行动）；
##   · 恢复后【又】产生新邀请 → 含 world_paused / pending_offer：前端继续裁决，不丢链。
signal conversation_declined(npc_id: String, payload: Dictionary)
## 对话进行中玩家回一句 → 后端流式生成 NPC 下一句（HTTPRequest 拿完整句后整体展示；
## over=true 表示后端已判定会话结束/无话可接，前端据此收尾）
signal conversation_turn_received(reply: String, over: bool)
## 主动结束对话（后端已清会话，world 恢复推进）。conv_end_text 为后端已预生成的"结束对话
## 回场景"专属转场旁白（发起对话那格结算完成时已落库；空串=预生成失败/未完成，前端兜底现调
## /scene/inspect view=conv_end）。
signal conversation_ended(ended: bool, conv_end_text: String)
## 玩家【主动】发起对话成功（后端已建会话，payload 含 initiator/intro/first_line）
signal conversation_started(payload: Dictionary)
## 玩家主动发起对话被拒（对方不愿 / 目标已死等），reason 为婉拒文案；世界不推、可重选
signal conversation_start_rejected(reason: String)

## 查询当前是否有待玩家裁决的对话邀请。前端每次世界结算完成后调用。
func request_conversation_invite() -> void:
	var http := HTTPRequest.new()
	http.timeout = TIMEOUT_SECONDS
	add_child(http)
	http.request_completed.connect(_on_conversation_invite.bind(http))
	# 后端 /conversation/invite 是 POST（FastAPI @app.post）——之前这里误用 GET 导致 405，
	# 前端"查询 NPC 是否主动邀你"一直失败、邀请从不弹出。改回 POST 才接入。
	# ⚠️ 09-10 修（"邀请永不弹、世界卡死"的真凶）：后端的 session_id / world_id 声明是
	# 【查询参数】（def conversation_invite(session_id: str = "", ...)），旧代码却把它们塞进
	# JSON body、URL 上一个都不带 → 后端读到 session_id="" → 回落去查 adhoc 会话 → 不管真实
	# 会话里有没有待裁决邀请，这里【永远返回空】→ 前端永远不弹邀请 → tick 因邀请挂起后世界
	# 永久冻结且毫无提示。改为与 /conversation/end 一致的 query string。
	var url := "%s/conversation/invite?session_id=%s&world_id=%s" % [
		BASE_URL, GameState.session_id, GameState.world_id]
	http.request(url, [], HTTPClient.METHOD_POST)

func _on_conversation_invite(result: int, code: int, _headers: PackedStringArray, body: PackedByteArray, http: HTTPRequest) -> void:
	http.queue_free()
	if result != HTTPRequest.RESULT_SUCCESS or code != 200:
		return   # 拿不到就不查（不阻塞，下次世界结算后再查）
	var data: Variant = JSON.parse_string(body.get_string_from_utf8())
	if data is Dictionary:
		conversation_invite_received.emit(data.get("offer", []))

## 玩家裁决对话邀请：accept=true 同意 / false 拒绝。
## scene 用客户端当前地点，让后端把对话锚定到此刻玩家所在场景。
func request_conversation_accept(npc_id: String, accept: bool) -> void:
	var http := HTTPRequest.new()
	http.timeout = TIMEOUT_SECONDS
	add_child(http)
	http.request_completed.connect(_on_conversation_accept.bind(http, npc_id, accept))
	var payload := {"session_id": GameState.session_id, "world_id": GameState.world_id,
		"accept": accept, "npc_id": npc_id, "scene": GameState.location_id}
	var headers := ["Content-Type: application/json"]
	http.request(BASE_URL + "/conversation/accept", headers, HTTPClient.METHOD_POST, JSON.stringify(payload))

func _on_conversation_accept(result: int, code: int, _headers: PackedStringArray, body: PackedByteArray, http: HTTPRequest, npc_id: String, accept: bool) -> void:
	http.queue_free()
	if result != HTTPRequest.RESULT_SUCCESS or code != 200:
		# 请求失败：当成"没成功"处理。同意目标失败不置会话；拒绝失败也不置递推——交给下次重查
		# 单独的三元表达式返回值被丢弃 → Godot 报 Standalone ternary warning，改用 if/else
		if accept:
			conversation_accepted.emit({})
		else:
			conversation_declined.emit(npc_id, {})
		return
	var data: Variant = JSON.parse_string(body.get_string_from_utf8())
	if data is Dictionary:
		if _as_bool(data.get("accepted", false)):
			conversation_accepted.emit(data)
		else:
			# 09-10：整包透传（含 world_paused/pending_offer 等世界时序字段），
			# 不再只丢一个 npc_id —— 否则"恢复那一格"的结算结果前端无从得知。
			conversation_declined.emit(npc_id, data)
	else:
		conversation_declined.emit(npc_id, {})

## 对话进行中：玩家回一句 → 后端流式生成 NPC 下一句。
## 后端 /conversation/turn 是 StreamingResponse(text/plain)；HTTPRequest 不支持增量，
## 这里等整句返回后一次性交给界面（功能等价，只是不逐 token 打字机）。
func request_conversation_turn(message: String) -> void:
	var http := HTTPRequest.new()
	http.timeout = TIMEOUT_SECONDS
	add_child(http)
	http.request_completed.connect(_on_conversation_turn.bind(http))
	var payload := {"session_id": GameState.session_id, "world_id": GameState.world_id, "message": message}
	var headers := ["Content-Type: application/json"]
	http.request(BASE_URL + "/conversation/turn", headers, HTTPClient.METHOD_POST, JSON.stringify(payload))

func _on_conversation_turn(result: int, code: int, _headers: PackedStringArray, body: PackedByteArray, http: HTTPRequest) -> void:
	http.queue_free()
	var reply := ""
	if result == HTTPRequest.RESULT_SUCCESS and code == 200:
		reply = body.get_string_from_utf8().strip_edges()
	# over：回复为空 = 后端已判定会话结束（is_conversation_over 时 generate 产出空串）
	conversation_turn_received.emit(reply, reply.is_empty())

## 主动结束对话（清空后端会话，world 恢复推进）。
func request_conversation_end() -> void:
	var http := HTTPRequest.new()
	http.timeout = TIMEOUT_SECONDS
	add_child(http)
	http.request_completed.connect(_on_conversation_end.bind(http))
	var url := "%s/conversation/end?session_id=%s&world_id=%s" % [
		BASE_URL, GameState.session_id, GameState.world_id]
	http.request(url, [], HTTPClient.METHOD_POST)

func _on_conversation_end(result: int, code: int, _headers: PackedStringArray, body: PackedByteArray, http: HTTPRequest) -> void:
	http.queue_free()
	var ended := false
	var conv_end_text := ""
	if result == HTTPRequest.RESULT_SUCCESS and code == 200:
		var data: Variant = JSON.parse_string(body.get_string_from_utf8())
		ended = data is Dictionary and _as_bool(data.get("ended", false))
		if data is Dictionary:
			conv_end_text = String(data.get("conv_end_text", ""))
	conversation_ended.emit(ended, conv_end_text)

## 玩家【主动】发起对话：POST /conversation/start
## 时序：后端先判定对方愿不愿意（零 LLM）——愿则建会话+耗1tick+AI动态简介；拒则返回婉拒、不推世界。
## max_rounds：本场对话轮数上限，<=0 表示让后端用默认（5）。传了才随 payload 带上，
## 后端 /conversation/start 据此建可变轮数上限的会话。
func request_conversation_start(npc_id: String, max_rounds: int = -1) -> void:
	var http := HTTPRequest.new()
	http.timeout = TIMEOUT_SECONDS
	add_child(http)
	http.request_completed.connect(_on_conversation_start.bind(http, npc_id))
	var payload := {"session_id": GameState.session_id, "world_id": GameState.world_id,
		"npc_id": npc_id, "scene": GameState.location_id}
	if max_rounds > 0:
		payload["max_rounds"] = max_rounds
	var headers := ["Content-Type: application/json"]
	http.request(BASE_URL + "/conversation/start", headers, HTTPClient.METHOD_POST, JSON.stringify(payload))

func _on_conversation_start(result: int, code: int, _headers: PackedStringArray, body: PackedByteArray, http: HTTPRequest, _npc_id: String) -> void:
	http.queue_free()
	if result != HTTPRequest.RESULT_SUCCESS or code != 200:
		conversation_start_rejected.emit("与对方的交谈没能开始。")
		return
	var data: Variant = JSON.parse_string(body.get_string_from_utf8())
	if data is Dictionary:
		if _as_bool(data.get("accepted", false)):
			conversation_started.emit(data)
		else:
			conversation_start_rejected.emit(String(data.get("reason", "对方似乎不太想交谈。")))
	else:
		conversation_start_rejected.emit("响应异常，请重试。")
