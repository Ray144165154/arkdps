"""游戏机制常量。

这些数字是**游戏规则**，不是魔法数字——集中放在这里，可以改、可以测、
可以在不同版本的游戏之间切换（例如某天官方改了最低伤害系数）。

刻意遵守的一条纪律：本模块只放**全游戏通用**的机制。
任何与具体干员有关的数值（攻击力、技防、技能参数）都属于**数据**，
放在 ``data/`` 里，不属于引擎。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["GameRules", "DEFAULT_RULES"]


@dataclass(frozen=True, slots=True)
class GameRules:
    """全游戏通用的机制参数。

    :param fps: 游戏逻辑帧率。攻击间隔按帧结算时要用到。
    :param min_damage_ratio: 最低伤害系数——物理/法术伤害不会低于攻击力的这个比例。
    :param auto_sp_per_second: 「自动回复」类技能每秒回复的技力。
    :param frame_rounding: 是否把攻击间隔取整到整数帧。

        关闭时攻击间隔是连续的浮点数；开启时按游戏实际的帧结算取整。
        两者差别很小，但需要精确到帧时（例如比较两个干员的攻速阈值）
        就必须开启。
    """

    fps: float = 30.0
    min_damage_ratio: float = 0.05
    auto_sp_per_second: float = 1.0
    frame_rounding: bool = False

    def round_interval(self, seconds: float) -> float:
        """按规则决定是否把攻击间隔取整到帧。"""
        if not self.frame_rounding or self.fps <= 0:
            return seconds
        # 游戏里的间隔按帧数结算，最少 1 帧
        frames = max(1, round(seconds * self.fps))
        return frames / self.fps

    def __post_init__(self) -> None:
        if self.fps <= 0:
            raise ValueError("fps 必须为正数")
        if not 0 <= self.min_damage_ratio <= 1:
            raise ValueError("min_damage_ratio 应在 [0, 1] 区间内")
        if self.auto_sp_per_second < 0:
            raise ValueError("auto_sp_per_second 不能为负")


#: 默认规则。与当前游戏版本一致。
DEFAULT_RULES = GameRules()
