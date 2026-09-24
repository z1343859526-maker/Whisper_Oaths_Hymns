extends Control
## Loading —— 待机开场画面（黑屏 + 世界介绍 + "世界加载中..." + 时间线）
##
## 职责：主菜单选完模式后进入，负责把"世界介绍 + 后端就绪 + 场景数据就绪"这步串起来，再切进正式游戏。
##
## 时序（设计决定 09-09，v2 修正）：
##   ① 0 → 25%：纯动画填充，约 4s（预测"等待后端自己就绪"的时间，此时不探测）；
##   ② 到 25% 才发起第一次后端探测（不是一进游戏就探测，避免空等/慢超时）；
##   ③ 探测后每 0.6s 一次（ApiClient 内轮询），确认会话建立（session_created）→ 快进到 50%；
##   ④ 会话建立后再拉一次 /scene/inspect（view=opening）拿场景数据（旁白+入口），1s 从 50% 跑到 75%；
##   ⑤ 确认会话 + 场景数据【都就绪】→ 1s 从 75% 跑到 100%；
##   ⑥ 100% → 切入 main.tscn，关掉本黑屏。
##
## 世界介绍：从 /world/info 拉 entry，显示在屏幕【中间】（不再进正式游戏右侧聊天框）。
##   失败用本地兜底，绝不让加载界面空着。
##
## 为什么 v2 改（09-09）：①原来进度到了 75% 才第一次探测，等待时间浪费；
## ②初始场景现由后端 view=opening 返回预设描述（不调 LLM），/scene/inspect 秒回，
## 无需再等 LLM 文学旁白 30s+；把"场景数据"也算进就绪条件并缓存到
## GameState.pending_scene_inspect，让 main 进入时立即用缓存渲染，做到"场景信息都显示出来了才进页面"。

## 各阶段时长 / 触发点
const FILL_DURATION := 4.0      # 0 → 25% 历时（秒）
const STAGE_DURATION := 1.0     # 每个"快进段"（25→50 / 50→75 / 75→100）历时（秒）
const PROBE_TRIGGER := 0.25     # 进度到达此值才发起第一次后端探测
const SESSION_PROGRESS := 0.50  # 会话建立后进度
const SCENE_PROGRESS := 0.75    # 场景数据就绪后进度
## 场景数据等待兜底（秒）：/scene/inspect(opening) 走预设描述应秒回；万一后端异常/超时，
## _on_scene_inspect 不被触发（api_client 非 200 直接 return）。若不兜底，scene 一直没回 → 卡死。
## 设上限强制放行：宁可进入后由 main 再兜底拉一次，也不无限卡加载界面。
const SCENE_WAIT_TIMEOUT := 30.0

@onready var intro_label: Label = $Intro
@onready var progress_bar: ProgressBar = $Bottom/ProgressBar
@onready var loading_label: Label = $Bottom/LoadingLabel

## 进度 0~1
var _progress: float = 0.0
## 阶段：fill(0→25%) / session(25→50%) / scene(50→75%) / snap(75→100%) / done
var _phase: String = "fill"
## 是否已发起第一次探测（只触发一次，避免重复拉起）
var _probe_started: bool = false
## 会话已建立（后端已就绪 + session_id 拿到）
var _session_ready: bool = false
## 场景数据已就绪（首个 /scene/inspect 完整响应已到达并缓存）
var _scene_ready: bool = false
## 是否已发起过场景拉取（只触发一次）
var _scene_requested: bool = false
## 场景拉取等待计时（秒）：发起 scene 请求后累计；达 SCENE_WAIT_TIMEOUT 未回则强制放行。
var _scene_wait: float = 0.0
## 后端就绪状态提示文案（探测中/已就绪/启动中/失败），经 backend_status 信号更新。
var _backend_msg: String = ""


func _ready() -> void:
	progress_bar.max_value = 100.0
	progress_bar.value = 0.0
	loading_label.text = "世界加载中..."
	# 起点地点：读 GameState.initial_scene（模组化，不再按 world_id 写死）。
	# loading 先于 main 运行，这里用 GameState.initial_scene 对齐地点，loading 拉 /scene/inspect
	# 时才不会用错房间。真正的 initial_scene 来自 /world/info（异步），回填见 _on_world_info。
	GameState.location_id = GameState.initial_scene
	# 世界介绍：从 /world/info 拉 entry；先给本地兜底，命中后覆盖。
	var fallback := "世界正在苏醒……"
	if GameState.world_id == "test":
		fallback = "这是引擎自检台：3 个房间、2 名测试角色，用来验证计划驱动与环境反应。可自由走动、尝试对话。"
	ApiClient.world_info_received.connect(_on_world_info.bind(fallback))
	ApiClient.request_world_info()
	# 会话建立信号（后端探测成功 → start_session → session_created）。
	ApiClient.session_created.connect(_on_session_ready)
	# 场景数据就绪信号（/scene/inspect 返回）——首个匹配当前会话的完整响应 → 缓存给 main。
	ApiClient.scene_inspect_received.connect(_on_scene_inspect)
	# 09-09 需求：把"后台是否打开"纳入加载进度。后端探测的每一步（检查/就绪/启动/失败）
	# 都经此信号回传，落到 loading_label 让玩家看清楚后台到底开没开、开到哪一步。
	ApiClient.backend_status.connect(_on_backend_status)
	# 会话可能已建立（重进/先前进程已就绪）→ 直接标记就绪。
	if String(GameState.session_id) != "":
		_session_ready = true


## 后端状态回传：把状态词覆盖到加载文案（"世界加载中…"→"正在检查后端…"/"后端已就绪…"等）。
## 这样进度条等待后端期间，玩家能看到明确的进度说明而非静止黑屏。
func _on_backend_status(text: String) -> void:
	_backend_msg = text
	loading_label.text = text


func _on_world_info(payload: Dictionary, fallback: String) -> void:
	var entry := String(payload.get("entry", "")).strip_edges()
	intro_label.text = entry if entry != "" else fallback
	# 模组化初始场景（09-09）：以 /world/info 的 initial_scene 为准，回填到全局并同步 location_id，
	# 使 main 进入时与本次开局场景一致；读不到则保持当前值（不覆盖，保守兜底）。
	var iscene := String(payload.get("initial_scene", "")).strip_edges()
	if iscene != "":
		GameState.initial_scene = iscene
		GameState.location_id = iscene


func _on_session_ready() -> void:
	_session_ready = true


func _on_scene_inspect(payload: Dictionary) -> void:
	## 收到 /scene/inspect 结果：只认"当前会话"的响应（丢弃 adhoc/旧会话的迟到数据）。
	var cur_sid := String(GameState.session_id)
	if cur_sid != "" and String(payload.get("session_id", "")) != cur_sid:
		return
	GameState.pending_scene_inspect = payload
	_scene_ready = true


func _process(delta: float) -> void:
	match _phase:
		"fill":
			# ① 0 → 25%：纯动画填充，不探测
			_progress += delta / FILL_DURATION
			if _progress >= PROBE_TRIGGER:
				_progress = PROBE_TRIGGER
				if not _probe_started:
					_probe_started = true
					ApiClient.ensure_backend_ready()
					# 探测刚发起：把文案切到后端状态（由 _on_backend_status 更新），提示"后台在这步"
					if _backend_msg == "":
						loading_label.text = "正在检查后端……"
				# 兜底：探测走后若 session_id 已备好，主动标记就绪（防 session_created 信号已过）
				if not _session_ready and String(GameState.session_id) != "":
					_session_ready = true
				# ② 会话建立 → 切入 session 段（25→50%）
				if _session_ready:
					_phase = "session"
				else:
					# 等待后端就绪期间：进度在 25% 附近轻微摆动（真实所处的等待阶段不变，仅视觉上"呼吸"），
					# 让玩家知道卡在"后台检测"这步、系统仍在探测/拉起，而非死锁黑屏。
					# 直接微调 _progress（而非 progress_bar），让 _process 末尾的统一赋值映射到进度条。
					var pulse := 0.5 + 0.5 * sin(Time.get_ticks_msec() / 350.0)
					_progress = PROBE_TRIGGER - 0.01 + pulse * 0.02
		"session":
			# ③ 会话建立 → 25% 快进到 50%
			_progress += delta / STAGE_DURATION
			if _progress >= SESSION_PROGRESS:
				_progress = SESSION_PROGRESS
			# ④ 发起场景拉取（只一次），拿到场景数据即可进入 scene 段
			if not _scene_requested:
				_scene_requested = true
				ApiClient.request_scene_inspect(GameState.location_id, "opening")
			# 场景等待计时：LLM/后端异常时 _on_scene_inspect 不触发，超时强制放行
			_scene_wait += delta
			if _scene_ready or _scene_wait >= SCENE_WAIT_TIMEOUT:
				_phase = "scene"
		"scene":
			# ⑤ 场景数据已获取 → 50% 快进到 75%
			_progress += delta / STAGE_DURATION
			if _progress >= SCENE_PROGRESS:
				_progress = SCENE_PROGRESS
			_phase = "snap"
		"snap":
			# ⑥ 会话 + 场景都就绪 → 75% 快进到 100%
			_progress += delta / STAGE_DURATION
			if _progress >= 1.0:
				_progress = 1.0
				_enter_game()
				return
	progress_bar.value = _progress * 100.0


func _enter_game() -> void:
	_phase = "done"
	get_tree().change_scene_to_file("res://main.tscn")
