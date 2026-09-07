# nc_states.py —— 帧状态常量（NewCut 自研引擎）
#
# 状态模型来自对明日方舟战斗界面右上角控制条的逆向分析：
#   右侧按钮（播放/暂停）： ❚❚ = 运行中    ▶ = 已暂停
#   左侧按钮（速度指示）：  1X▶ = 1倍速   2X▶▶ = 2倍速   置灰▶▶▶ = 暂停时禁用态（不单独成状态）
#
# NORMAL = 右侧为 ❚❚ 但左侧速度图标无法识别（或 UI 不在屏幕上）

STATE_NORMAL = 0
STATE_PAUSED = 1
STATE_SPEED_1X = 2
STATE_SPEED_2X = 3

STATE_NAMES = {
    STATE_NORMAL: "normal",
    STATE_PAUSED: "paused",
    STATE_SPEED_1X: "1x",
    STATE_SPEED_2X: "2x",
}
