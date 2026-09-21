"""测试夹具：合成干员、技能、敌人。

刻意**不依赖网络与真实数据**：

  * 联网测试会因 PRTS 改版而莫名其妙地失败
  * 真实数据的期望值需要人工查证，改起来慢
  * 合成夹具能把"想测什么"写清楚

数值都取得很整齐（1000 攻击、1.0 间隔、0 防御），是为了让期望值可以
**口算核对**。测试里出现 ``assertAlmostEqual(x, 1250.0)`` 时，
读的人应该能自己推出来，而不是只能相信作者。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from arkdps.model import (  # noqa: E402
    ChargeType,
    DamageType,
    Effects,
    Enemy,
    Operator,
    Skill,
)

__all__ = [
    "effects",
    "skill",
    "operator",
    "enemy",
    "temp_dir",
    "write_draft",
    "sample_wikitext",
    "multi_talent_wikitext",
    "conditional_wikitext",
]


# --------------------------------------------------------------------------
# 临时目录
# --------------------------------------------------------------------------

@contextmanager
def temp_dir():
    """给一个**确实能写文件**的临时目录。

    不能直接用 ``tempfile.TemporaryDirectory`` / ``mkdtemp``：它们按
    **0o700** 建目录，而本机的沙箱把写操作交给另一个身份执行，
    0o700 会被直接拒掉（``PermissionError``）——连 ``chmod`` 补救都被拒。
    用 ``os.makedirs`` 建的目录是 **0o777**，写起来毫无问题。

    清理失败**不让测试失败**：这台机器上临时目录常常删不掉。
    """
    path = Path(tempfile.gettempdir()) / f"arkdps-test-{uuid.uuid4().hex[:8]}"
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:  # 系统临时区不行就退回项目根
        path = ROOT / f"arkdps-test-{uuid.uuid4().hex[:8]}"
        os.makedirs(path, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def write_draft(directory, filename: str, data: dict):
    """把一份草稿字典写成 JSON 文件，返回文件路径。"""
    import json

    path = Path(directory) / filename
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def effects(**kwargs) -> Effects:
    """构造 Effects，只写关心的字段。"""
    return Effects(**kwargs)


def skill(
    name: str = "测试技能",
    *,
    charge: ChargeType | str = ChargeType.AUTO,
    sp_cost: float = 30.0,
    init_sp: float = 0.0,
    duration: float = 10.0,
    infinite_duration: bool = False,
    stance: bool = False,
    **effect_fields,
) -> Skill:
    """构造技能。``effect_fields`` 直接传给 :class:`Effects`。"""
    if isinstance(charge, str):
        charge = ChargeType(charge)
    return Skill(
        name=name,
        charge=charge,
        sp_cost=sp_cost,
        init_sp=init_sp,
        duration=duration,
        infinite_duration=infinite_duration,
        stance=stance,
        effects=effects(**effect_fields),
    )


def operator(
    name: str = "测试干员",
    *,
    atk: float = 1000.0,
    interval: float = 1.0,
    atk_trust: float = 0.0,
    atk_potential: float = 0.0,
    trait: Effects | None = None,
    talent: Effects | None = None,
    skills: list[Skill] | None = None,
    damage_type: DamageType = DamageType.PHYSICAL,
    hits: float = 1.0,
    targets: int = 1,
) -> Operator:
    """构造干员。"""
    return Operator(
        name=name,
        atk=atk,
        interval=interval,
        atk_trust=atk_trust,
        atk_potential=atk_potential,
        trait=trait or Effects(),
        talent=talent or Effects(),
        skills=skills or [],
        damage_type=damage_type,
        hits=hits,
        targets=targets,
    )


def enemy(
    *,
    defense: float = 0.0,
    res: float = 0.0,
    hp: float = float("inf"),
    name: str = "测试目标",
) -> Enemy:
    """构造敌人。"""
    return Enemy(name=name, defense=defense, res=res, hp=hp)


# --------------------------------------------------------------------------
# 合成 wikitext（导入器测试用，避免联网）
# --------------------------------------------------------------------------

def sample_wikitext(
    *,
    name: str = "测试干员",
    attack: int = 700,
    attack_key: str = "精英2_满级_攻击",
    interval: str = "1.3s",
    rarity: int = 5,
    trait: str = "",
    talent_blocks: str = "",
    skills: str = "",
) -> str:
    """拼一份与 PRTS 结构一致的干员页 wikitext。

    只要结构和字段名对得上，导入器就会像处理真实页面一样处理它——
    于是导入器的测试可以完全离线。

    :param attack_key: 面板攻击力挂在哪一档。PRTS 按精英阶段分档存，
        1~3 星干员没有 ``精英2_满级_攻击`` 这个字段——改这个参数
        就能造出低星干员那种页面。
    """
    return f"""{{{{干员页面名|{name}|TestOperator}}}}
==干员信息==
{{{{CharinfoV2
|干员名={name}
|干员id=char_test_001
|稀有度={rarity}
|职业=近卫
|分支=领主
|特性={trait}
}}}}
==属性==
{{{{属性
|攻击速度={interval}
|{attack_key}={attack}
|信赖加成_攻击=50
|精英2_满级_生命上限=2500
}}}}
==天赋==
{talent_blocks}
==技能==
{skills}
"""


def multi_talent_wikitext() -> str:
    """**两个** ``天赋列表3`` 模板——对应两个天赋。

    回归测试用：早先的实现处理完第一个模板就返回了，
    结果像能天使这样的双天赋干员会丢掉第二个天赋，
    而 DPS 会悄悄偏低。
    """
    first = """{{天赋列表3
|天赋1=快速弹匣
|天赋1条件=精英1
|天赋1效果=攻击速度{{*|6|+6}}
|天赋2=快速弹匣
|天赋2条件=精英2
|天赋2效果=攻击速度{{*|12|+12}}
}}"""
    second = """{{天赋列表3
|天赋1=天使的祝福
|天赋1条件=精英2
|天赋1效果=攻击力{{*|6%|+6%}}，生命上限{{*|10%|+10%}}
}}"""
    return sample_wikitext(talent_blocks=first + "\n" + second)


def conditional_wikitext() -> str:
    """含**条件效果**的特性文本。

    回归测试用：「但此时」说明这个 80% 只在远程攻击时生效，
    无条件套用会让 DPS 恒定偏低 20%。
    """
    return sample_wikitext(trait="可以进行远程攻击，但此时攻击力降低至80%")
