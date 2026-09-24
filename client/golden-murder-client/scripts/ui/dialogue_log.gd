extends VBoxContainer
## DialogueLog —— 对话记录区（右区）渲染组件
## 职责单一：只负责"把一条消息 / 分隔线写进对话流 + 平滑滚动到最佳阅读位"。
## 与对话状态机（main.gd）解耦：main.gd 只调用 add_message / add_separator / insert_before_spacer。
## 对应设计文档 scenes/ui/dialogue_log.tscn 的脚本；这是一个可复用 UI 零件。

## 对话记录自动滚动时，让"最新消息底部"停在可视区的这个比例处（0.5=中间，越大小越靠下）。
const SCROLL_READ_POS := 0.8
## 对话记录自动滚动的动画时长（秒）：改大更慢更流畅，改小更快。
const SCROLL_DURATION := 0.6
## 对话流默认字号（统一口径，避免散落的 magic number）
const FONT_SIZE_TEXT := 22
const FONT_SIZE_TITLE := 20

## 滚动容器的引用（本组件的父节点，即 DialogueScroll）
@onready var _scroll: ScrollContainer = get_parent()
## 对话记录滚动动画的 Tween 引用（每次滚动复用，避免叠加）
var _scroll_tween: Tween
## 内容底部空白垫片：让最新消息底部停在可视区中间、下方留白
var _scroll_spacer: Control

func _ready() -> void:
	_init_scroll_spacer()
	# 让 LogVBox 至少撑满 ScrollContainer 视口高 + 内容底部对齐：
	# 这样即使内容不足一屏，最新消息也会停在 SCROLL_READ_POS 阅读位，而不是从顶部排。
	size_flags_vertical = Control.SIZE_EXPAND_FILL
	alignment = BoxContainer.ALIGNMENT_END
	_scroll.resized.connect(_update_scroll_spacer)

## ---------- 对外接口 ----------
## 追加一条消息（NPC 靠左 / 玩家靠右）
func add_message(speaker: String, text: String, is_player: bool) -> void:
	var lbl := Label.new()
	lbl.add_theme_font_size_override("font_size", FONT_SIZE_TEXT)
	lbl.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	if is_player:
		lbl.text = "[我] %s" % text
		lbl.horizontal_alignment = HORIZONTAL_ALIGNMENT_RIGHT
	else:
		lbl.text = "[%s] %s" % [speaker, text]
		lbl.horizontal_alignment = HORIZONTAL_ALIGNMENT_LEFT
	insert_before_spacer(lbl)
	## 等这一帧布局完成后，再平滑滚动到底部（此时垫片会把文本顶到中间）
	call_deferred("_scroll_to_read_position")

## 插入一条分隔线（横向淡白细线，可选居中标题）
func add_separator(title: String = "") -> void:
	var sep := ColorRect.new()
	sep.custom_minimum_size = Vector2(0, 2)
	sep.color = Color(1, 1, 1, 0.25)
	insert_before_spacer(sep)
	if not title.is_empty():
		var lbl := Label.new()
		lbl.text = title
		lbl.add_theme_font_size_override("font_size", FONT_SIZE_TITLE)
		lbl.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
		lbl.add_theme_color_override("font_color", Color(1, 1, 1, 0.6))
		insert_before_spacer(lbl)

## 把任意节点插到滚动垫片之前（保证垫片始终是最后一个子节点）
func insert_before_spacer(node: Node) -> void:
	add_child(node)
	if is_instance_valid(_scroll_spacer) and _scroll_spacer.get_parent() == self:
		move_child(node, _scroll_spacer.get_index())

## ---------- 内部：垫片与滚动 ----------
func _init_scroll_spacer() -> void:
	_scroll_spacer = Control.new()
	_scroll_spacer.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	add_child(_scroll_spacer)

func _update_scroll_spacer() -> void:
	## 垫片高度 = 可视区高度 × (1 − SCROLL_READ_POS)，即让最新消息底部停在此比例处
	_scroll_spacer.custom_minimum_size = Vector2(0, _scroll.size.y * (1.0 - SCROLL_READ_POS))

## 平滑滚动到底部：垫片占据下方空白，最新文本底部恰好停在可视区 SCROLL_READ_POS 处。
func _scroll_to_read_position() -> void:
	await _smooth_scroll_to_bottom()

## 场景切换后平滑滚动到底部：为了能看到最后追加的"与XX交谈"入口按钮。
func scroll_to_entry_bottom() -> void:
	await _smooth_scroll_to_bottom()

## 唯一实现：等一帧布局稳定后，平滑滚动到滚动条最大值（即到底部）。
## 场景描述、玩家动作、入口按钮共用这一个滚动逻辑，避免重复。
func _smooth_scroll_to_bottom() -> void:
	# 等两帧，确保 VBox 子节点与 ScrollContainer 的尺寸都完成布局后再读滚动条最大值
	await get_tree().process_frame
	await get_tree().process_frame
	_update_scroll_spacer()
	var vbar: ScrollBar = _scroll.get_v_scroll_bar()
	var maxv: float = vbar.max_value
	if maxv <= 0.0:
		# 内容不足一屏：无需滚动，align=END 已让最新消息停在 SCROLL_READ_POS 阅读位
		_scroll.scroll_vertical = 0
		return
	if _scroll_tween and _scroll_tween.is_valid():
		_scroll_tween.kill()
	var from: float = _scroll.scroll_vertical
	_scroll_tween = create_tween()
	_scroll_tween.tween_method(
		func(val: float): _scroll.scroll_vertical = int(val),
		from, maxv, SCROLL_DURATION
	).set_trans(Tween.TRANS_CUBIC).set_ease(Tween.EASE_IN_OUT)
