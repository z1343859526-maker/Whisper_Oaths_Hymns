extends Control
## MainMenu —— 游戏入口（开始画面）
## 职责：展示标题，提供"剧情模式 / 测试模式"两个入口，并把玩家送到主游戏场景。
## 说明：真正的主游戏场景是 main.tscn（UI 不变）；本场景只负责"放进哪个世界"，
##       通过设置 GameState.world_id 后 change_scene 切走。主菜单本身无游戏逻辑。

## 剧情模式入口（黄金乡，正式剧情）
@onready var story_btn: Button = $Center/VBox/Buttons/StoryBtn
## 测试模式入口（测试世界，引擎自检台）
@onready var test_btn: Button = $Center/VBox/Buttons/TestBtn
## 底部提示 Label（剧情模式未实装时用它提示）
@onready var notice_label: Label = $Notice

func _ready() -> void:
	story_btn.pressed.connect(_on_story_pressed)
	test_btn.pressed.connect(_on_test_pressed)
	notice_label.text = ""

## 剧情模式：黄金乡版本内容尚未实装，先给出占位提示，不进入。
func _on_story_pressed() -> void:
	notice_label.text = "剧情模式尚未实装：这是开发期占位入口，请先体验【测试模式】验证引擎。"

## 测试模式：写入世界 id，先切到【待机加载界面】（loading.tscn）——
## 由它负责"展示世界介绍 + 等待后端就绪 + 进度条"，就绪后自动切入主游戏场景 main.tscn。
func _on_test_pressed() -> void:
	GameState.world_id = "test"
	get_tree().change_scene_to_file("res://loading.tscn")
