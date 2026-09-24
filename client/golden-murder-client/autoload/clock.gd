extends Node
## Clock —— 客户端时钟（autoload 单例）
## 职责：推进"时段"，并管理"与 NPC 交互时的 60 秒倒计时"。
## 关键：玩家挂机不推进；交互/移动/跳过才消耗；AI 反应时倒计时暂停。

## 事件：时段推进了，UI 顶栏监听它刷新时间显示
signal time_advanced(slot: String)

## 可行时段顺序（用于循环推进）
const SLOTS: Array[String] = ["清晨", "上午", "中午", "下午", "傍晚", "午夜"]

var day: int = 1            ## 第几天（1 = 第一天下午起）
var slot_index: int = 4     ## 从"傍晚"开始（对应 SLOTS[4]）

## 占位：后续做"时段流转 + 事件表触发"。这里先用函数外壳，保证骨架可跑可扩展。
func advance_slot() -> void:
	slot_index += 1
	if slot_index >= SLOTS.size():
		slot_index = 0
		day += 1                # 跨天：时段循环回到清晨，天数+1
	time_advanced.emit(SLOTS[slot_index])

## 当前时段名
func current_slot() -> String:
	return SLOTS[slot_index]
