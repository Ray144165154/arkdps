"""数据载入层（:mod:`arkdps.loader`）测试。

草稿 JSON 是半自动生成、再手工修订的，字段随时可能少一个、多一个，或者被写成
``null``。载入层要做的就是把这些"不干净"的输入稳定地变成引擎对象，并且
**不改变语义**。

下面三条最容易写错，而且都属于"错了不报错、只会静默算错"的类型，所以各自
单独测：

  * 缺 ``atk_mult`` 只能当 ``1.0``（乘法的单位元）——当成 ``0.0`` 会把一切伤害清零
  * 缺 ``hits_override`` 必须是 ``None``（"用默认段数"），不能是 ``0.0``（"零段"）
  * 引擎不认识的键要**忽略**掉，而不是让整份草稿载入失败

真实草稿（``data/operators``）只用来核对**结构不变量**（名字非空、间隔为正、
技能能按名字查回来），不核对游戏数值——数值随版本漂，写死只会让测试变脆。
"""

import dataclasses
import json
import os
import shutil
import tempfile
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path

from fixtures import operator  # noqa: F401  —— 它会把项目根插进 sys.path

from arkdps import loader
from arkdps.model import ChargeType, DamageType, Effects, Operator, Skill

ROOT = Path(__file__).resolve().parent.parent
#: 真实草稿目录；缺失时相关用例整类跳过（不联网、不假装数据存在）。
DATA_OPERATORS = ROOT / "data" / "operators"


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------

def _probe_writable(directory: Path) -> bool:
    """真的往目录里写一个文件试试——"能建目录"不等于"能写文件"。"""
    probe = directory / ".write-probe"
    try:
        probe.write_text("x", encoding="utf-8")
    except OSError:
        return False
    try:
        probe.unlink()
    except OSError:
        pass
    return True


def _dispose(path: Path) -> None:
    """删掉手工建的临时目录；删不掉就算了。

    这台机器上临时目录常常删不掉（``PermissionError``），而
    ``TemporaryDirectory(ignore_cleanup_errors=True)`` 挡不住这种错——
    它发生在 ``tempfile`` 内部的 ``_resetperms`` 里，绕过了忽略分支。
    所以这里自己接管清理，绝不让"清理失败"变成"测试失败"。
    """
    shutil.rmtree(path, ignore_errors=True)


def _dispose_tempfile(tmp: tempfile.TemporaryDirectory) -> None:
    """删掉 ``TemporaryDirectory``；删不掉就算了，并摘掉 finalizer。

    自己接管清理之后要顺手 ``detach()``：否则析构时 ``tempfile`` 会再删一次，
    删不掉就往 stderr 打一行 "Exception ignored"，把测试输出弄脏。
    """
    try:
        tmp.cleanup()
    except OSError:
        pass
    finalizer = getattr(tmp, "_finalizer", None)  # 私有属性，仅用于抑制析构告警
    if finalizer is not None:
        finalizer.detach()


def _make_fallback_temp_dir() -> Path:
    """自己建一个**确实能写文件**的临时目录（``tempfile`` 用不了时的退路）。

    ``tempfile`` 按 **0o700** 建目录，而本机的沙箱把写操作交给另一个身份执行，
    0o700 会被直接拒掉（``PermissionError [Errno 13]``）——建得出来，却连
    ``chmod`` 补救都不许。同一位置用 ``os.makedirs``（默认 **0o777**）建出来的
    目录写起来毫无问题，差别就在权限位。

    先试系统临时区，再退回项目根目录；两处都不行才跳过——与其让文件级用例
    "假装通过"，不如明确跳过。
    """
    last_error: OSError | None = None
    for base in (Path(tempfile.gettempdir()), ROOT):
        path = base / f"arkdps-test-{uuid.uuid4().hex[:8]}"
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as exc:
            last_error = exc
            continue
        if _probe_writable(path):
            return path
        _dispose(path)
    raise unittest.SkipTest(f"当前环境没有可写的临时目录（{last_error}）")


@contextmanager
def _temp_dir():
    """临时目录上下文：优先 ``tempfile.TemporaryDirectory``，**清理失败也不让测试失败**。

    先用标准做法建目录（正常机器上就该这么写）；但本机上它的目录写不进去
    （0o700 + 沙箱，见 :func:`_make_fallback_temp_dir`），探测不过就换成手工建的
    目录。这样"用 tempfile"和"真的能跑"两件事都成立。
    """
    tmp: tempfile.TemporaryDirectory | None = None
    try:
        tmp = tempfile.TemporaryDirectory()
    except OSError:
        tmp = None
    if tmp is not None:
        path = Path(tmp.name)
        if _probe_writable(path):
            try:
                yield path
            finally:
                _dispose_tempfile(tmp)  # 夹住整个 with 块，不然会被析构提前删掉
            return
        _dispose_tempfile(tmp)

    path = _make_fallback_temp_dir()
    try:
        yield path
    finally:
        _dispose(path)


def _write_draft(directory: Path, filename: str, data: object) -> Path:
    """把一份草稿写成 UTF-8 JSON（``ensure_ascii=False``，中文保持原样）。"""
    path = directory / filename
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _assert_fields(case: unittest.TestCase, obj: object, expected: dict) -> None:
    """逐字段断言；用 ``subTest`` 让失败信息直接指出是哪个字段不对。"""
    for field_name, want in expected.items():
        with case.subTest(field=field_name):
            case.assertEqual(getattr(obj, field_name), want)


def _field_names(cls: type) -> set[str]:
    """dataclass 的字段名集合——用来核对"测试是否覆盖了模型的每个字段"。"""
    return {f.name for f in dataclasses.fields(cls)}


# --------------------------------------------------------------------------
# Effects：默认值与全字段往返
# --------------------------------------------------------------------------

#: :class:`Effects` 每个字段的默认值，照 ``model.py`` 手抄，方便口算核对。
_EFFECT_DEFAULTS = {
    "atk_pct": 0.0,
    "atk_mult": 1.0,
    "aspd": 0.0,
    "interval_mult": 1.0,
    "interval_flat": 0.0,
    "hits_mult": 1.0,
    "hits_add": 0.0,
    "hits_override": None,
    "targets": 1,
    "targets_scope": None,
    "damage_type": None,
    "def_ignore_pct": 0.0,
    "def_ignore": 0.0,
    "res_ignore_pct": 0.0,
    "res_ignore": 0.0,
    "bonus_arts_pct": 0.0,
    "bonus_true_pct": 0.0,
    "dot_pct": 0.0,
    "attacks": True,
}

#: 一份把所有字段都写满的 Effects 草稿，以及它应当载入成什么。
_EFFECT_FULL = {
    "atk_pct": 100.0,
    "atk_mult": 1.5,
    "aspd": 25.0,
    "interval_mult": 0.8,
    "interval_flat": -0.2,
    "hits_mult": 2.0,
    "hits_add": 1.0,
    "hits_override": 3.0,
    "targets": 4,
    "targets_scope": "阻挡的所有敌人",
    "damage_type": "arts",
    "def_ignore_pct": 50.0,
    "def_ignore": 200.0,
    "res_ignore_pct": 10.0,
    "res_ignore": 5.0,
    "bonus_arts_pct": 30.0,
    "bonus_true_pct": 40.0,
    "dot_pct": 60.0,
    "attacks": False,
}

_EFFECT_FULL_EXPECTED = dict(_EFFECT_FULL, damage_type=DamageType.ARTS)


class TestEffectsDefaults(unittest.TestCase):
    """空草稿必须等于"裸的" :class:`Effects` 默认值。

    这条之所以重要：载入层如果给某个字段填了别的值（哪怕只是把 ``hits_override``
    填成 0.0），伤害会静默算错，而且不会报任何错。所以这里既比整体相等，
    也逐个字段核对。
    """

    def test_empty_dict_is_bare_defaults(self):
        """``{}`` 与 ``Effects()`` 必须完全相等——载入层不该"顺手"填值。"""
        self.assertEqual(loader.effects_from_dict({}), Effects())

    def test_none_is_bare_defaults(self):
        """草稿里没写 ``trait``/``talent``/``effects`` 时传进来的就是 ``None``。"""
        self.assertEqual(loader.effects_from_dict(None), Effects())

    def test_every_default_value(self):
        """逐个字段核对默认值；三个倍率字段是 1.0，其余按 model.py 的声明。"""
        _assert_fields(self, loader.effects_from_dict({}), _EFFECT_DEFAULTS)

    def test_default_table_covers_every_model_field(self):
        """默认值表必须覆盖 :class:`Effects` 的每一个字段。

        模型以后新增字段时这条会失败，提醒把新字段也纳入载入测试——
        比"忘了测"要好。
        """
        self.assertEqual(set(_EFFECT_DEFAULTS), _field_names(Effects))


class TestEffectsRoundTrip(unittest.TestCase):
    """写满所有字段的草稿必须原样载入——不能有字段被静默丢弃或串位。"""

    def test_all_fields_round_trip(self):
        """一次性设置全部 19 个字段，逐个核对载入结果。"""
        _assert_fields(self, loader.effects_from_dict(_EFFECT_FULL), _EFFECT_FULL_EXPECTED)

    def test_full_draft_covers_every_model_field(self):
        """全字段草稿的键集合必须等于模型字段集合（防止漏测某个字段）。"""
        self.assertEqual(set(_EFFECT_FULL), _field_names(Effects))

    def test_explicit_zeros_are_kept(self):
        """加算类字段显式写 0.0 要保留成 0.0，不能被当成"没写"而回退默认值。"""
        loaded = loader.effects_from_dict({"atk_pct": 0.0, "hits_add": 0.0})
        self.assertEqual(loaded.atk_pct, 0.0)
        self.assertEqual(loaded.hits_add, 0.0)

    def test_explicit_false_attacks_is_kept(self):
        """``attacks: false`` 是"这个技能不能普攻"，语义极重，不能被 ``or`` 吃掉。"""
        self.assertIs(loader.effects_from_dict({"attacks": False}).attacks, False)


# --------------------------------------------------------------------------
# Effects：类型与枚举
# --------------------------------------------------------------------------

class TestEffectsCoercion(unittest.TestCase):
    """枚举与数值的类型转换。

    ``damage_type`` / ``charge`` 在草稿里是字符串，在模型里是枚举。
    转换规则必须与源码一致：**认识的值精确映射，不认识的值按源码的处理方式走**。
    """

    def test_damage_type_strings(self):
        """三个伤害类型字符串各自映射到对的枚举成员。"""
        expected = {
            "physical": DamageType.PHYSICAL,
            "arts": DamageType.ARTS,
            "true": DamageType.TRUE,
        }
        for raw, want in expected.items():
            with self.subTest(damage_type=raw):
                self.assertIs(
                    loader.effects_from_dict({"damage_type": raw}).damage_type, want
                )

    def test_damage_type_enum_member_is_accepted(self):
        """直接传枚举成员也应该能用（枚举构造器见到同类成员会直接返回它）。"""
        self.assertIs(
            loader.effects_from_dict({"damage_type": DamageType.TRUE}).damage_type,
            DamageType.TRUE,
        )

    def test_unknown_damage_type_raises_value_error(self):
        """不认识的伤害类型**故意不吞**：源码里没有 try/except，直接抛 ValueError。

        这与 ``charge`` 的处理方式不同（那里会退化成 AUTO）。伤害类型猜错会让
        整套公式用错，宁可在载入期就炸掉，所以这里断言异常而不是断言回退。
        """
        for raw in ("magic", "ARTS", "Physical", "物理", ""):
            with self.subTest(damage_type=raw):
                with self.assertRaises(ValueError):
                    loader.effects_from_dict({"damage_type": raw})

    def test_null_damage_type_stays_none(self):
        """``damage_type: null`` 表示"本组修正不改伤害类型"，保留 None。

        注意这与 :func:`operator_from_dict` 不同：干员级别的 damage_type
        缺省会退化成 PHYSICAL（见 TestOperatorFromDict）。
        """
        self.assertIsNone(loader.effects_from_dict({"damage_type": None}).damage_type)

    def test_number_like_fields_become_floats(self):
        """整数字面量要变成 float——否则引擎里 ``int/float`` 混算出精度问题很难查。"""
        numeric = [
            "atk_pct", "atk_mult", "aspd", "interval_mult", "interval_flat",
            "hits_mult", "hits_add", "def_ignore_pct", "def_ignore",
            "res_ignore_pct", "res_ignore", "bonus_arts_pct", "bonus_true_pct",
            "dot_pct",
        ]
        for name in numeric:
            with self.subTest(field=name):
                value = getattr(loader.effects_from_dict({name: 7}), name)
                self.assertEqual(value, 7.0)
                self.assertIsInstance(value, float)

    def test_targets_is_int(self):
        """``targets`` 是计数，必须是 int：字符串 "3" 和浮点 3.0 都要落到 3。"""
        for raw in (3, "3", 3.0):
            with self.subTest(targets=raw):
                value = loader.effects_from_dict({"targets": raw}).targets
                self.assertEqual(value, 3)
                self.assertIsInstance(value, int)

    def test_targets_scope_is_str(self):
        """``targets_scope`` 是非数值范围（"阻挡的所有敌人"），统一转成字符串。"""
        self.assertEqual(
            loader.effects_from_dict({"targets_scope": 5}).targets_scope, "5"
        )

    def test_attacks_is_bool(self):
        """``attacks`` 一律转 bool；0/1 也要落到 False/True。"""
        for raw, want in ((True, True), (False, False), (0, False), (1, True)):
            with self.subTest(attacks=raw):
                self.assertIs(
                    loader.effects_from_dict({"attacks": raw}).attacks, want
                )


# --------------------------------------------------------------------------
# 三个容易静默算错的字段
# --------------------------------------------------------------------------

class TestMultiplicativeIdentity(unittest.TestCase):
    """倍率字段缺失时必须是 ``1.0``（乘法单位元），**绝对不能**是 ``0.0``。

    这是全项目最危险的一类错误：``atk_mult`` 为 0 时，任何攻击力都会被乘成 0，
    DPS 直接变成 0，而且整条链路不会报任何异常。
    """

    def test_absent_atk_mult_is_one_not_zero(self):
        """没写 ``atk_mult`` → 1.0；并且明确不等于 0.0。"""
        loaded = loader.effects_from_dict({})
        self.assertEqual(loaded.atk_mult, 1.0)
        self.assertNotEqual(loaded.atk_mult, 0.0)

    def test_absent_atk_mult_keeps_damage_unchanged(self):
        """语义核对：1000 攻击力乘上"没写 atk_mult"的倍率，仍然是 1000。"""
        self.assertAlmostEqual(1000.0 * loader.effects_from_dict({}).atk_mult, 1000.0)

    def test_explicit_atk_mult_is_kept(self):
        """写了 2.0 就是 2.0（"攻击力提高至 200%"）。"""
        self.assertEqual(loader.effects_from_dict({"atk_mult": 2.0}).atk_mult, 2.0)

    def test_null_atk_mult_falls_back_to_one(self):
        """``atk_mult: null`` 与"没写"等价——源码显式跳过 None 值。"""
        self.assertEqual(loader.effects_from_dict({"atk_mult": None}).atk_mult, 1.0)

    def test_all_multiplier_fields_default_to_one(self):
        """三个倍率字段（攻击力/间隔/段数）默认都必须是 1.0。"""
        loaded = loader.effects_from_dict({})
        for name in ("atk_mult", "interval_mult", "hits_mult"):
            with self.subTest(field=name):
                self.assertEqual(getattr(loaded, name), 1.0)


class TestHitsOverrideOptional(unittest.TestCase):
    """``hits_override`` 是真正可选的：``None`` 与 ``0.0`` 语义完全不同。

    ``None`` = "用干员/技能默认的段数"；``0.0`` = "一段都不打"（明确写死的零）。
    载入层用的是 ``float(value)`` 而不是 ``value or ...``，所以显式的 0.0 能活下来。
    """

    def test_absent_is_none(self):
        """没写 → None（不是 0.0，也不是 1.0）。"""
        self.assertIsNone(loader.effects_from_dict({}).hits_override)

    def test_null_is_none(self):
        """``hits_override: null`` → None。"""
        self.assertIsNone(loader.effects_from_dict({"hits_override": None}).hits_override)

    def test_present_number_is_kept(self):
        """写了 2.0 → 2.0，并且是 float。"""
        value = loader.effects_from_dict({"hits_override": 2.0}).hits_override
        self.assertEqual(value, 2.0)
        self.assertIsInstance(value, float)

    def test_explicit_zero_is_preserved_and_is_not_none(self):
        """显式的 0.0 必须保留成 0.0，且**不能**被当成"没写"。"""
        value = loader.effects_from_dict({"hits_override": 0.0}).hits_override
        self.assertEqual(value, 0.0)
        self.assertIsNotNone(value)
        self.assertNotEqual(value, None)

    def test_string_number_is_coerced(self):
        """草稿里偶尔会把数字写成字符串，照样要能转。"""
        self.assertEqual(
            loader.effects_from_dict({"hits_override": "3"}).hits_override, 3.0
        )


# --------------------------------------------------------------------------
# 未知键
# --------------------------------------------------------------------------

class TestUnknownKeys(unittest.TestCase):
    """不认识的键必须被**忽略**，载入照常成功。

    草稿里混着大量给人看的审计字段（``_source`` / ``_parsed`` / ``_unparsed`` /
    ``_conditional`` …）以及引擎不认识的扩展字段。载入层只挑 :data:`_EFFECT_FIELDS`
    里的名字，过滤发生在类型转换**之前**，所以连"值是没法转成 float 的垃圾"
    都不会炸。
    """

    def test_audit_keys_are_ignored(self):
        """下划线开头的审计字段一个都不进引擎对象。"""
        data = {
            "_schema": "arkdps/operator/1",
            "_source": {"wiki": "PRTS", "page": "12F"},
            "_parsed": [{"text": "攻击力+10%"}],
            "_unparsed": [{"text": "停止攻击", "affects_damage": True}],
            "_conditional": [{"text": "仅远程"}],
            "_warnings": ["没取到精英2攻击"],
            "_confidence": "low",
        }
        self.assertEqual(loader.effects_from_dict(data), Effects())

    def test_extension_keys_are_ignored(self):
        """扩展字段被忽略，但同一份字典里认识的字段照常生效。"""
        loaded = loader.effects_from_dict({"atk_pct": 10.0, "bogus": 999.0, "name": "x"})
        self.assertEqual(loaded, Effects(atk_pct=10.0))

    def test_junk_value_on_unknown_key_survives(self):
        """不认识的键连值都不该碰：值是中文，压根不该进 float() 那一支。"""
        loaded = loader.effects_from_dict({"atk_pct": 10.0, "bogus": "不是数字"})
        self.assertEqual(loaded, Effects(atk_pct=10.0))

    def test_ignored_keys_do_not_become_attributes(self):
        """忽略要彻底：被丢掉的键不能变成对象属性（slots dataclass 也不允许）。"""
        loaded = loader.effects_from_dict({"bogus": 1.0})
        self.assertFalse(hasattr(loaded, "bogus"))

    def test_known_field_with_bad_value_still_raises(self):
        """反过来：**认识**的字段给了转不了的值，就该抛错——沉默才会害人。"""
        with self.assertRaises(ValueError):
            loader.effects_from_dict({"atk_pct": "abc"})

    def test_skill_level_extra_keys_are_ignored(self):
        """技能字典里的未知键同样被忽略（``_talent_meta`` 这类原样带过来也不会炸）。"""
        skill = loader.skill_from_dict(
            {"name": "技能", "_talent_meta": [{"name": "天赋"}], "_unknown": 1}
        )
        self.assertEqual(skill.name, "技能")

    def test_operator_level_extra_keys_are_ignored(self):
        """干员字典里的未知键同样被忽略。"""
        op = loader.operator_from_dict({"name": "干员", "_warnings": ["x"], "_junk": 1})
        self.assertEqual(op.name, "干员")


# --------------------------------------------------------------------------
# Skill
# --------------------------------------------------------------------------

_SKILL_DEFAULTS = {
    "name": "未命名技能",
    "charge": ChargeType.AUTO,
    "sp_cost": 0.0,
    "init_sp": 0.0,
    "duration": 0.0,
    "infinite_duration": False,
    "stance": False,
    "effects": Effects(),
    "source": "",
    "confidence": "exact",
    "unparsed": [],
    "note": "",
}

#: 模型的字段名 → 草稿里的键名。``source``/``confidence``/``unparsed`` 在草稿里
#: 是下划线开头的审计字段，载入时映回公开字段名。
_SKILL_KEY_MAP = {
    "name": "name",
    "charge": "charge",
    "sp_cost": "sp_cost",
    "init_sp": "init_sp",
    "duration": "duration",
    "infinite_duration": "infinite_duration",
    "stance": "stance",
    "effects": "effects",
    "source": "_source",
    "confidence": "_confidence",
    "unparsed": "_unparsed",
    "note": "note",
}

_SKILL_FULL = {
    "name": "黄昏",
    "charge": "attack",
    "sp_cost": 70,
    "init_sp": 30,
    "duration": 20,
    "infinite_duration": False,
    "stance": False,
    "effects": {"atk_pct": 100.0, "damage_type": "arts"},
    "_source": "PRTS/示例干员",
    "_confidence": "manual-verified",
    "_unparsed": [{"text": "攻击范围扩大", "affects_damage": False}],
    "note": "人工核对过",
}

_SKILL_FULL_EXPECTED = {
    "name": "黄昏",
    "charge": ChargeType.ATTACK,
    "sp_cost": 70.0,
    "init_sp": 30.0,
    "duration": 20.0,
    "infinite_duration": False,
    "stance": False,
    "effects": Effects(atk_pct=100.0, damage_type=DamageType.ARTS),
    "source": "PRTS/示例干员",
    "confidence": "manual-verified",
    "unparsed": [{"text": "攻击范围扩大", "affects_damage": False}],
    "note": "人工核对过",
}


class TestSkillDefaults(unittest.TestCase):
    """``skill_from_dict({})`` 也必须是一份可用的技能，而不是半成品。

    技能是列表元素，载入层没有"必填校验"；缺字段时它靠 ``or`` 链兜底，
    所以每个兜底值都要对得上，否则默认技能会带着意外的技力/持续时间进引擎。
    """

    def test_empty_dict_defaults(self):
        """空字典 → 占位名字 + 全部默认值。"""
        _assert_fields(self, loader.skill_from_dict({}), _SKILL_DEFAULTS)

    def test_default_skill_is_instant(self):
        """没有持续时间的默认技能是瞬发技能（``is_instant`` 为真）。"""
        self.assertTrue(loader.skill_from_dict({}).is_instant)

    def test_default_effects_equal_bare_effects(self):
        """没写 ``effects`` 时，技能带的是一组没有任何修正的 Effects。"""
        self.assertEqual(loader.skill_from_dict({}).effects, Effects())

    def test_unparsed_list_is_not_shared_between_skills(self):
        """两个技能不能共享同一个 ``unparsed`` 列表。

        共享可变默认值是经典事故：给 A 技能追加一条未解析片段，
        B 技能会跟着多一条，而两者看起来毫无关系。
        """
        first = loader.skill_from_dict({})
        second = loader.skill_from_dict({})
        self.assertIsNot(first.unparsed, second.unparsed)


class TestSkillRoundTrip(unittest.TestCase):
    """技能的全部字段都要能载入——包括那几个映射自下划线键的审计字段。"""

    def test_all_fields_round_trip(self):
        """写满所有字段，逐个核对（``source`` ← ``_source`` 等映射见草稿）。"""
        _assert_fields(
            self, loader.skill_from_dict(_SKILL_FULL), _SKILL_FULL_EXPECTED
        )

    def test_key_map_covers_every_model_field(self):
        """映射表必须覆盖 :class:`Skill` 的每个字段，否则有字段是漏测的。"""
        self.assertEqual(set(_SKILL_KEY_MAP), _field_names(Skill))

    def test_unparsed_is_copied_not_aliased(self):
        """``unparsed`` 是拷贝：之后往技能的清单里追加，不该改到原草稿字典。"""
        data = {"_unparsed": [{"text": "a"}]}
        skill = loader.skill_from_dict(data)
        self.assertEqual(skill.unparsed, data["_unparsed"])
        self.assertIsNot(skill.unparsed, data["_unparsed"])


class TestSkillCharge(unittest.TestCase):
    """技力回复方式的枚举转换，以及不认识的值怎么兜底。

    ``charge`` 决定"技能多久能开一次"，直接乘在 DPS 上；猜错会得到完全
    不同的结论，所以这里把每个合法值都过一遍，并单独钉住兜底行为。
    """

    def test_known_charge_strings(self):
        """四个合法字符串各自映射到对的枚举成员。"""
        expected = {
            "auto": ChargeType.AUTO,
            "attack": ChargeType.ATTACK,
            "hit": ChargeType.HIT,
            "passive": ChargeType.PASSIVE,
        }
        for raw, want in expected.items():
            with self.subTest(charge=raw):
                self.assertIs(loader.skill_from_dict({"charge": raw}).charge, want)

    def test_charge_enum_value_string_is_accepted(self):
        """传枚举的 ``.value``（"hit"）必须被接受。"""
        for member in ChargeType:
            with self.subTest(charge=member.value):
                self.assertEqual(
                    loader.skill_from_dict({"charge": member.value}).charge, member
                )

    def test_charge_enum_member_is_accepted(self):
        """回归：直接传 :class:`ChargeType` 成员，必须等同于传它的字符串值。

        源码原本写的是 ``str(data.get("charge") or "auto")``，而
        ``class ChargeType(str, Enum)`` 在 Python 3.12 上 ``str()`` 得到的是
        ``"ChargeType.HIT"`` 而不是 ``"hit"``；于是 ``ChargeType(...)`` 抛
        ValueError 被兜底吞掉，**受击回复被静默当成自动回复**——
        而那些技能的开技能频率会算得完全不对，却不会有任何报错。

        传枚举成员本来是最自然的写法，结果反而坏掉，所以这条必须钉死。
        """
        for member in ChargeType:
            with self.subTest(charge=member.name):
                self.assertIs(
                    loader.skill_from_dict({"charge": member}).charge, member
                )

    def test_unknown_charge_falls_back_to_auto(self):
        """不认识的值（含空串、``None``）按源码兜底成 AUTO，且不抛异常。

        注意这是**有意为之的宽容**：与其让一份草稿整个载入失败，不如按最常见的
        自动回复处理——代价是错的技力回复不会报错，所以调用方要配合
        ``confidence``/``note`` 一起看。
        """
        for raw in ("bogus", "", None, "AUTO!", 0):
            with self.subTest(charge=raw):
                self.assertIs(
                    loader.skill_from_dict({"charge": raw}).charge, ChargeType.AUTO
                )


class TestSkillDuration(unittest.TestCase):
    """持续时间与"无限持续"是两套语义，必须分得清。

    持续时间决定技能覆盖率，覆盖率直接乘在 DPS 上；把无限技能当成瞬发（或反之）
    会得到量级上的错误。
    """

    def test_missing_duration_is_zero_and_not_infinite(self):
        """没写 duration → 0.0，且不是无限。"""
        for data in ({}, {"duration": None}, {"duration": 0}):
            with self.subTest(data=data):
                skill = loader.skill_from_dict(data)
                self.assertEqual(skill.duration, 0.0)
                self.assertFalse(skill.infinite_duration)

    def test_numeric_duration_is_kept(self):
        """写了 12.5 就是 12.5（字符串数字也能转）。"""
        for raw in (12.5, "12.5"):
            with self.subTest(duration=raw):
                self.assertEqual(loader.skill_from_dict({"duration": raw}).duration, 12.5)

    def test_infinite_flag_forces_infinite_duration(self):
        """``infinite_duration`` 为真时，duration 一律归一成 ``inf``。

        归一很重要：下游只要看 duration 就能知道技能不会结束，
        不必再分两处判断标志位。
        """
        skill = loader.skill_from_dict({"duration": 5, "infinite_duration": True})
        self.assertTrue(skill.infinite_duration)
        self.assertEqual(skill.duration, float("inf"))

    def test_infinite_duration_value_implies_flag(self):
        """草稿里直接写 ``Infinity`` 也要等价于"无限持续"。"""
        skill = loader.skill_from_dict({"duration": float("inf")})
        self.assertTrue(skill.infinite_duration)
        self.assertEqual(skill.duration, float("inf"))

    def test_is_instant_boundaries(self):
        """瞬发的判定：有持续时间 → 否；无限 → 否；形态切换 → 否。"""
        cases = {
            "无持续时间": ({}, True),
            "有持续时间": ({"duration": 10}, False),
            "无限持续": ({"infinite_duration": True}, False),
            "形态切换": ({"stance": True}, False),
        }
        for label, (data, want) in cases.items():
            with self.subTest(case=label):
                self.assertIs(loader.skill_from_dict(data).is_instant, want)


class TestSkillNameAndReview(unittest.TestCase):
    """名字兜底与"需要复核"的判定。

    这两个字段本身不影响伤害，但它们是**调用方判断结果可不可信的唯一线索**：
    名字为空会让人无法在报告里认出技能；``needs_review`` 算错会让未解析的片段
    被当成已经算进去了。
    """

    def test_name_fallback(self):
        """名字缺失/为空/为 None 都退化成占位名，而不是空字符串。"""
        for data in ({}, {"name": ""}, {"name": None}):
            with self.subTest(data=data):
                self.assertEqual(loader.skill_from_dict(data).name, "未命名技能")

    def test_name_is_kept(self):
        """正常名字原样保留（中文不转义）。"""
        self.assertEqual(loader.skill_from_dict({"name": "电流翻涌"}).name, "电流翻涌")

    def test_needs_review_when_unparsed_affects_damage(self):
        """未解析片段没有明确说"不影响伤害"时，一律按"需要复核"处理。"""
        cases = {
            "无片段": ({}, False),
            "明确影响伤害": ({"_unparsed": [{"affects_damage": True}]}, True),
            "明确不影响伤害": ({"_unparsed": [{"affects_damage": False}]}, False),
            "没写这个字段": ({"_unparsed": [{"text": "某段没解析"}]}, True),
            "一条不影响一条没说": (
                {"_unparsed": [{"affects_damage": False}, {"text": "x"}]},
                True,
            ),
            "空清单": ({"_unparsed": []}, False),
            "清单是 null": ({"_unparsed": None}, False),
        }
        for label, (data, want) in cases.items():
            with self.subTest(case=label):
                self.assertIs(loader.skill_from_dict(data).needs_review, want)


# --------------------------------------------------------------------------
# Operator
# --------------------------------------------------------------------------

_OPERATOR_DEFAULTS = {
    "name": "未命名干员",
    "atk": 0.0,
    "interval": 1.0,
    "class_name": "",
    "branch": "",
    "rarity": None,
    "atk_trust": 0.0,
    "atk_potential": 0.0,
    "trait": Effects(),
    "talent": Effects(),
    "damage_type": DamageType.PHYSICAL,
    "hits": 1.0,
    "targets": 1,
    "skills": [],
    "trait_text": "",
    "source": "",
}

#: 干员模型的字段名 → 草稿键名（``note`` 是载入时算出来的，草稿里没有对应键）。
_OPERATOR_KEY_MAP = {
    "name": "name",
    "atk": "atk",
    "interval": "attack_interval",
    "class_name": "class",
    "branch": "branch",
    "rarity": "rarity",
    "atk_trust": "atk_trust",
    "atk_potential": "atk_potential",
    "trait": "trait",
    "talent": "talent",
    "damage_type": "damage_type",
    "hits": "hits",
    "targets": "targets",
    "skills": "skills",
    "trait_text": "trait_text",
    "source": "_source.page",
}

#: 载入时推导、草稿里没有对应键的字段。
_OPERATOR_DERIVED = {"note"}


class TestOperatorDefaults(unittest.TestCase):
    """空草稿的干员也要是一份"能算"的干员：间隔 1.0（不为 0）、段数 1。"""

    def test_empty_dict_defaults(self):
        """逐个字段核对空草稿的载入结果。"""
        _assert_fields(self, loader.operator_from_dict({}), _OPERATOR_DEFAULTS)

    def test_interval_falls_back_to_one(self):
        """间隔缺失/为 None/为 0 都退化成 1.0——0 会让 ``1/interval`` 直接除零。"""
        for data in ({}, {"attack_interval": None}, {"attack_interval": 0}):
            with self.subTest(data=data):
                self.assertEqual(loader.operator_from_dict(data).interval, 1.0)

    def test_name_falls_back_to_placeholder(self):
        """没有名字时会写成占位名，方便在结果里一眼看出是坏数据。"""
        for data in ({}, {"name": ""}, {"name": None}):
            with self.subTest(data=data):
                self.assertEqual(loader.operator_from_dict(data).name, "未命名干员")

    def test_hits_and_targets_fall_back(self):
        """段数与目标数的 0/None/缺失都退化成 1（"至少打一下、至少打一个"）。"""
        for data in ({}, {"hits": 0, "targets": 0}, {"hits": None, "targets": None}):
            with self.subTest(data=data):
                op = loader.operator_from_dict(data)
                self.assertEqual(op.hits, 1.0)
                self.assertEqual(op.targets, 1)

    def test_key_map_covers_every_model_field(self):
        """键映射表 + 推导字段 必须覆盖 :class:`Operator` 的每个字段。"""
        self.assertEqual(
            set(_OPERATOR_KEY_MAP) | _OPERATOR_DERIVED, _field_names(Operator)
        )


class TestOperatorRoundTrip(unittest.TestCase):
    """干员的每个字段都要能载入，并且面板攻击力按"加算"进 ``base_atk``。"""

    def test_all_settable_fields_round_trip(self):
        """写满所有可设置字段，逐个核对。"""
        data = {
            "name": "测试干员",
            "atk": 700,
            "attack_interval": 1.3,
            "class": "近卫",
            "branch": "领主",
            "rarity": 6,
            "atk_trust": 50,
            "atk_potential": 30,
            "trait": {"atk_pct": 10.0},
            "talent": {"aspd": 6.0},
            "damage_type": "arts",
            "hits": 2,
            "targets": 3,
            "skills": [{"name": "技能一", "charge": "attack"}],
            "trait_text": "攻击造成法术伤害",
            "_source": {"page": "测试干员"},
        }
        expected = {
            "name": "测试干员",
            "atk": 700.0,
            "interval": 1.3,
            "class_name": "近卫",
            "branch": "领主",
            "rarity": 6,
            "atk_trust": 50.0,
            "atk_potential": 30.0,
            "trait": Effects(atk_pct=10.0),
            "talent": Effects(aspd=6.0),
            "damage_type": DamageType.ARTS,
            "hits": 2.0,
            "targets": 3,
            "trait_text": "攻击造成法术伤害",
            "source": "测试干员",
        }
        _assert_fields(self, loader.operator_from_dict(data), expected)

    def test_base_atk_sums_panel_trust_and_potential(self):
        """``base_atk`` = 面板 + 信赖 + 潜能 = 700 + 50 + 30 = 780（加算，非百分比）。"""
        op = loader.operator_from_dict(
            {"atk": 700, "atk_trust": 50, "atk_potential": 30}
        )
        self.assertEqual(op.base_atk, 780.0)

    def test_null_atk_becomes_zero(self):
        """草稿里 ``atk: null``（导入器取不到面板攻击力时就是这么写的）要变成 0.0。

        载入层不在这里报错——它靠 ``_warnings`` 与 ``note`` 提示人来修。
        """
        self.assertEqual(loader.operator_from_dict({"atk": None}).atk, 0.0)

    def test_rarity_is_passed_through(self):
        """稀有度只是展示信息，原样透传（不强制转 float）。"""
        self.assertIsNone(loader.operator_from_dict({}).rarity)
        self.assertEqual(loader.operator_from_dict({"rarity": 6}).rarity, 6)

    def test_wrong_key_name_is_silently_ignored(self):
        """字段名写错（``attack`` 而不是 ``atk``）不会报错，只会静默落到 0.0。

        这是"未知键被忽略"的另一面：草稿生成器必须写对键名，
        所以真实草稿的 ``_warnings`` 才那么重要。
        """
        self.assertEqual(loader.operator_from_dict({"attack": 700}).atk, 0.0)


class TestOperatorDamageType(unittest.TestCase):
    """干员级别的伤害类型：缺省必须落到物理，且能改成法术/真实。"""

    def test_default_is_physical(self):
        """没写/写成 null/写成空串 → 物理（干员的三选一字段没有"未指定"这一档）。"""
        for data in ({}, {"damage_type": None}, {"damage_type": ""}):
            with self.subTest(data=data):
                self.assertIs(
                    loader.operator_from_dict(data).damage_type, DamageType.PHYSICAL
                )

    def test_string_is_coerced(self):
        """字符串照样映射到枚举。"""
        self.assertIs(
            loader.operator_from_dict({"damage_type": "true"}).damage_type,
            DamageType.TRUE,
        )

    def test_enum_member_is_accepted(self):
        """直接传枚举成员也可以（枚举构造器见到同类成员直接返回）。"""
        self.assertIs(
            loader.operator_from_dict({"damage_type": DamageType.ARTS}).damage_type,
            DamageType.ARTS,
        )


class TestOperatorSkills(unittest.TestCase):
    """技能列表的构造与按名字查找。

    ``Operator.skill(name)`` 是调用方唯一的取技能入口（命令行、批量对比都走它），
    所以"声明的技能一定能查回来"是必须钉住的不变量。
    """

    def test_skills_are_loaded_in_order(self):
        """技能按草稿顺序载入，名字可枚举。"""
        op = loader.operator_from_dict(
            {"skills": [{"name": "技能一"}, {"name": "技能二"}]}
        )
        self.assertEqual(op.skill_names, ["技能一", "技能二"])

    def test_skill_lookup_by_name(self):
        """每个声明的技能都能按名字查回来。"""
        op = loader.operator_from_dict(
            {"skills": [{"name": "技能一"}, {"name": "技能二"}]}
        )
        for name in ("技能一", "技能二"):
            with self.subTest(name=name):
                skill = op.skill(name)
                self.assertIsNotNone(skill)
                self.assertEqual(skill.name, name)

    def test_unknown_skill_name_returns_none(self):
        """查不到就返回 None——调用方据此决定是否回退到普攻。"""
        op = loader.operator_from_dict({"skills": [{"name": "技能一"}]})
        self.assertIsNone(op.skill("不存在的技能"))

    def test_no_skills_is_empty_list(self):
        """没有技能（或 skills 写成 null）时是空列表，不是 None。"""
        for data in ({}, {"skills": None}, {"skills": []}):
            with self.subTest(data=data):
                op = loader.operator_from_dict(data)
                self.assertEqual(op.skills, [])
                self.assertEqual(op.skill_names, [])

    def test_skill_effects_are_loaded(self):
        """技能的 effects 子字典要真的进到技能对象里。"""
        op = loader.operator_from_dict(
            {"skills": [{"name": "技能一", "effects": {"atk_pct": 50.0}}]}
        )
        skill = op.skill("技能一")
        self.assertIsNotNone(skill)
        self.assertEqual(skill.effects, Effects(atk_pct=50.0))


class TestOperatorNote(unittest.TestCase):
    """``note`` 是"这份数据可不可信"的汇总，必须如实反映未解析片段。

    载入层的设计立场是"引擎不假装数据是完整的"，note 就是这条立场的出口：
    数错了会让用户以为结果可信。
    """

    def test_clean_data_has_no_note(self):
        """干净数据（没有未解析片段）不该留下任何提示。"""
        self.assertEqual(loader.operator_from_dict({"skills": [{"name": "技能一"}]}).note, "")

    def test_note_counts_skills_needing_review(self):
        """两个技能里有一个含未解析片段 → note 里出现 "1/2"。"""
        op = loader.operator_from_dict(
            {
                "skills": [
                    {"name": "技能一", "_unparsed": [{"text": "x"}]},
                    {"name": "技能二"},
                ]
            }
        )
        self.assertIn("1/2", op.note)

    def test_note_for_trait_needs_review(self):
        """特性含未解析片段 → note 里点出来。"""
        op = loader.operator_from_dict({"_trait_needs_review": True})
        self.assertIn("特性", op.note)

    def test_note_for_conditional_trait_counts_items(self):
        """条件特性要报出**条数**，让人知道有多少效果没计入。"""
        op = loader.operator_from_dict({"_trait_conditional": [{"text": "a"}, {"text": "b"}]})
        self.assertIn("2", op.note)

    def test_empty_conditional_list_produces_no_note(self):
        """空清单不算"有条件特性"，不该留提示。"""
        self.assertEqual(loader.operator_from_dict({"_trait_conditional": []}).note, "")

    def test_note_parts_are_joined(self):
        """多条提示同时命中时要合并成一条 note（用「；」连接），不能只剩最后一条。"""
        op = loader.operator_from_dict(
            {
                "skills": [{"name": "技能一", "_unparsed": [{"text": "x"}]}],
                "_trait_needs_review": True,
                "_trait_conditional": [{"text": "a"}],
            }
        )
        self.assertIn("；", op.note)
        self.assertIn("1/1", op.note)
        self.assertIn("特性", op.note)


class TestOperatorSource(unittest.TestCase):
    """``source`` 取自 ``_source.page``——报告里"这条数据从哪来"要靠它。"""

    def test_source_from_page(self):
        """``_source.page`` 里有页面名时，原样取出来。"""
        op = loader.operator_from_dict({"_source": {"page": "12F"}})
        self.assertEqual(op.source, "12F")

    def test_source_missing_is_empty(self):
        """没有 ``_source``（或 page 为空/为 null）时是空字符串，不是 "None"。"""
        for data in ({}, {"_source": {}}, {"_source": {"page": None}}):
            with self.subTest(data=data):
                self.assertEqual(loader.operator_from_dict(data).source, "")


# --------------------------------------------------------------------------
# Enemy
# --------------------------------------------------------------------------

class TestEnemyFromDict(unittest.TestCase):
    """受击目标的载入：默认必须是一个"能算"的靶子，而不是 0 防御 0 血的怪。"""

    def test_empty_dict_defaults(self):
        """空字典 → 占位名 + 0 防御 + 0 法抗 + 无限血 + 非精英。"""
        enemy = loader.enemy_from_dict({})
        _assert_fields(
            self,
            enemy,
            {
                "name": "测试目标",
                "defense": 0.0,
                "res": 0.0,
                "hp": float("inf"),
                "is_elite": False,
            },
        )

    def test_all_fields_round_trip(self):
        """写满所有字段，逐个核对。"""
        enemy = loader.enemy_from_dict(
            {"name": "重装防御者", "defense": 800, "res": 30, "hp": 20000, "is_elite": True}
        )
        _assert_fields(
            self,
            enemy,
            {
                "name": "重装防御者",
                "defense": 800.0,
                "res": 30.0,
                "hp": 20000.0,
                "is_elite": True,
            },
        )

    def test_missing_hp_is_infinite(self):
        """血量缺失/为 null → ``inf``（"打不死的靶子"，用于只看 DPS 场景）。"""
        for data in ({}, {"hp": None}):
            with self.subTest(data=data):
                self.assertEqual(loader.enemy_from_dict(data).hp, float("inf"))

    def test_explicit_zero_hp_is_kept(self):
        """显式的 ``hp: 0`` 要保留成 0.0——源码用的是 ``is not None`` 判断。

        这正是 ``hits_override`` 的同款区别：**None 是"没写"，0 是"写了零"**。
        两者被混起来会得到完全相反的结果。
        """
        hp = loader.enemy_from_dict({"hp": 0}).hp
        self.assertEqual(hp, 0.0)
        self.assertNotEqual(hp, float("inf"))

    def test_def_alias_is_accepted(self):
        """防御力支持简写键 ``def``（草稿里两种写法都出现过）。"""
        self.assertEqual(loader.enemy_from_dict({"def": 500}).defense, 500.0)
        self.assertEqual(loader.enemy_from_dict({"defense": 800}).defense, 800.0)

    def test_is_elite_is_bool(self):
        """精英标记一律转 bool（部分天赋只对精英/领袖生效）。"""
        for raw, want in ((True, True), (False, False), (0, False), (1, True)):
            with self.subTest(is_elite=raw):
                self.assertIs(loader.enemy_from_dict({"is_elite": raw}).is_elite, want)


# --------------------------------------------------------------------------
# 文件级接口
# --------------------------------------------------------------------------

class TestLoadOperatorFile(unittest.TestCase):
    """``load_operator`` 读单个文件——这一层才真正碰到磁盘与编码。

    草稿里全是中文（干员名、技能名、特性文本），编码写错会变成乱码或直接
    抛 ``UnicodeDecodeError``，所以这里必须真的写一份文件到磁盘上再读回来。
    """

    def test_loads_draft_from_path_and_str(self):
        """同一份草稿，用 ``Path`` 和用字符串路径载入结果必须一致。"""
        with _temp_dir() as tmp:
            path = _write_draft(
                tmp,
                "测试干员.json",
                {
                    "name": "测试干员",
                    "atk": 700,
                    "attack_interval": 1.3,
                    "skills": [{"name": "技能一", "charge": "attack"}],
                },
            )
            from_path = loader.load_operator(path)
            from_str = loader.load_operator(str(path))
        self.assertEqual(from_path, from_str)
        self.assertEqual(from_path.name, "测试干员")
        self.assertEqual(from_path.atk, 700.0)
        self.assertEqual(from_path.interval, 1.3)
        self.assertIsNotNone(from_path.skill("技能一"))

    def test_utf8_is_used(self):
        """UTF-8 读盘：中文名字不能被解成乱码。"""
        with _temp_dir() as tmp:
            path = _write_draft(tmp, "draft.json", {"name": "史尔特尔"})
            op = loader.load_operator(path)
        self.assertEqual(op.name, "史尔特尔")

    def test_missing_file_raises(self):
        """文件不存在时直接抛 ``FileNotFoundError``——比返回一个空干员安全得多。"""
        with _temp_dir() as tmp:
            with self.assertRaises(FileNotFoundError):
                loader.load_operator(tmp / "不存在.json")


class TestLoadOperatorsDirectory(unittest.TestCase):
    """``load_operators`` 扫整个目录：顺序、宽容度、边界情况。

    批量入口要同时满足两件事：**顺序稳定**（报告可复现）和**坏文件不拖垮整批**
    （400+ 份草稿里有一份手写出错，不该让全部结果消失）。
    """

    def test_matches_individual_loads(self):
        """批量载入的结果与逐个 ``load_operator`` 完全一致（含字段值）。

        比较时必须**对齐顺序**：``load_operators`` 按干员名排序
        （``sorted(..., key=op.name)``，见 ``loader.py``），而逐个载入保持
        传入顺序。这里 ``乙`` 的码位比 ``甲`` 小，所以批量结果的第一个是
        ``乙``——直接拿两个列表 ``assertEqual`` 会误报成"内容不一致"。
        """
        drafts = {
            "a.json": {"name": "甲", "atk": 100, "attack_interval": 1.0},
            "b.json": {"name": "乙", "atk": 200, "attack_interval": 2.0},
        }
        with _temp_dir() as tmp:
            paths = {name: _write_draft(tmp, name, data) for name, data in drafts.items()}
            batch = loader.load_operators(tmp)
            individually = [loader.load_operator(p) for p in (paths["a.json"], paths["b.json"])]
        self.assertEqual(batch, sorted(individually, key=lambda op: op.name))

    def test_sorted_by_operator_name_not_file_name(self):
        """按**干员名**排序，而不是按文件名排序（报告顺序要跟文件命名无关）。"""
        with _temp_dir() as tmp:
            _write_draft(tmp, "z.json", {"name": "AAA"})
            _write_draft(tmp, "a.json", {"name": "ZZZ"})
            ops = loader.load_operators(tmp)
        self.assertEqual([op.name for op in ops], ["AAA", "ZZZ"])

    def test_non_json_files_are_ignored(self):
        """只认 ``*.json``：说明文档、缓存文件都不该变成干员。"""
        with _temp_dir() as tmp:
            _write_draft(tmp, "good.json", {"name": "甲"})
            _write_draft(tmp, "readme.txt", {"name": "不是草稿"})
            (tmp / "notes.md").write_text("说明", encoding="utf-8")
            ops = loader.load_operators(tmp)
        self.assertEqual([op.name for op in ops], ["甲"])

    def test_broken_files_are_skipped(self):
        """坏草稿（JSON 语法错误 / 顶层不是字典）被跳过，好草稿照常载入。

        这是源码里 ``except Exception: continue`` 的可见行为：宁可少一份数据，
        也不要让整批载入失败。
        """
        with _temp_dir() as tmp:
            _write_draft(tmp, "good.json", {"name": "甲"})
            (tmp / "broken.json").write_text("{不是合法 JSON", encoding="utf-8")
            _write_draft(tmp, "list.json", [{"name": "顶层是数组"}])
            ops = loader.load_operators(tmp)
        self.assertEqual([op.name for op in ops], ["甲"])

    def test_does_not_descend_into_subdirectories(self):
        """只扫一层：子目录里的 json 不算干员草稿。"""
        with _temp_dir() as tmp:
            _write_draft(tmp, "good.json", {"name": "甲"})
            sub = tmp / "sub"
            sub.mkdir()
            _write_draft(sub, "nested.json", {"name": "子目录里的"})
            ops = loader.load_operators(tmp)
        self.assertEqual([op.name for op in ops], ["甲"])

    def test_empty_directory_returns_empty_list(self):
        """空目录 → 空列表（不是异常）。"""
        with _temp_dir() as tmp:
            self.assertEqual(loader.load_operators(tmp), [])

    def test_missing_directory_returns_empty_list(self):
        """目录不存在 → 空列表。批量入口要能被"数据还没生成"的场景安全调用。"""
        with _temp_dir() as tmp:
            self.assertEqual(loader.load_operators(tmp / "不存在的目录"), [])

    def test_file_path_returns_empty_list(self):
        """传进来的是文件而不是目录 → 空列表，而不是抛 ``NotADirectoryError``。"""
        with _temp_dir() as tmp:
            path = _write_draft(tmp, "good.json", {"name": "甲"})
            self.assertEqual(loader.load_operators(path), [])


# --------------------------------------------------------------------------
# 公开接口
# --------------------------------------------------------------------------

class TestPublicSurface(unittest.TestCase):
    """公开接口就是 ``__all__`` 里那 6 个函数。

    钉住 ``__all__`` 是有意义的：它是"载入层对外承诺了什么"的唯一声明，
    别处（命令行、导入器）都按它来调用。
    """

    def test_all_exports_exist_and_are_callable(self):
        """``__all__`` 里的每个名字都要真的存在且可调用。"""
        for name in loader.__all__:
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(loader, name)))

    def test_all_lists_the_six_loaders(self):
        """六个载入函数一个不多一个不少。"""
        self.assertEqual(
            set(loader.__all__),
            {
                "effects_from_dict",
                "skill_from_dict",
                "operator_from_dict",
                "load_operator",
                "load_operators",
                "enemy_from_dict",
            },
        )


# --------------------------------------------------------------------------
# 真实草稿
# --------------------------------------------------------------------------

def _real_data_available() -> bool:
    """真实草稿目录是否存在且非空。"""
    return DATA_OPERATORS.is_dir() and any(DATA_OPERATORS.glob("*.json"))


@unittest.skipUnless(_real_data_available(), f"没有可用的真实草稿：{DATA_OPERATORS}")
class TestRealDraftData(unittest.TestCase):
    """真实草稿必须能被载入层吃下去，并且满足**结构不变量**。

    这里刻意不核对任何游戏数值（攻击力、间隔、技力都会随版本和导入器改动而变），
    只核对"不管数据怎么变都该成立"的性质——这样测试不会因为一次版本更新就红掉，
    但导入器把草稿写坏时一定会红。
    """

    @classmethod
    def setUpClass(cls):
        """整目录载入一次，避免每条用例重复读几百个文件。"""
        cls.paths = sorted(DATA_OPERATORS.glob("*.json"))
        cls.operators = loader.load_operators(DATA_OPERATORS)

    def _pick_real_draft(self):
        """挑一份"面板攻击力可用"的真实草稿。

        优先 12F.json（老数据、结构简单），否则取排序后第一份 ``base_atk > 0`` 的。
        全都没有——说明数据目录是空的半成品——就跳过，而不是让测试红掉。
        """
        preferred = DATA_OPERATORS / "12F.json"
        candidates = ([preferred] if preferred.exists() else []) + [
            path for path in self.paths if path != preferred
        ]
        for path in candidates:
            op = loader.load_operator(path)
            if op.base_atk > 0:
                return path, op
        self.skipTest("真实草稿里没有面板攻击力为正的干员")

    def test_batch_is_not_empty(self):
        """目录里确实载出了干员（防止把"跳过一切"当成通过）。"""
        self.assertGreaterEqual(len(self.operators), 1)

    def test_every_operator_has_structural_invariants(self):
        """逐个干员核对不变量：名字、间隔、攻击力、技能可查回。"""
        for op in self.operators:
            with self.subTest(operator=op.name):
                self.assertTrue(op.name)
                self.assertNotEqual(op.name, "未命名干员")
                self.assertGreater(op.interval, 0.0)
                self.assertGreaterEqual(op.atk, 0.0)
                self.assertGreaterEqual(op.base_atk, op.atk)
                self.assertIsInstance(op.trait, Effects)
                self.assertIsInstance(op.talent, Effects)
                for skill in op.skills:
                    self.assertIsNotNone(op.skill(skill.name))
                    self.assertIsInstance(skill, Skill)

    def test_at_least_one_operator_has_attack_available(self):
        """至少有一份草稿取到了面板攻击力——否则整批数据的 DPS 全是 0。"""
        self.assertTrue(any(op.base_atk > 0 for op in self.operators))

    def test_at_least_one_operator_declares_skills(self):
        """至少有一份草稿带技能——否则"技能能按名字查回来"这条会变成空测。"""
        self.assertTrue(any(op.skills for op in self.operators))

    def test_single_file_load_has_hand_checkable_invariants(self):
        """单独走一遍 :func:`load_operator`，核对结构不变量。

        这条同时验证"单文件入口"和"整目录入口"用的是同一条载入路径。
        """
        path, op = self._pick_real_draft()
        self.assertTrue(op.name)
        self.assertGreater(op.interval, 0.0)
        self.assertGreater(op.base_atk, 0.0)
        for skill in op.skills:
            with self.subTest(skill=skill.name):
                self.assertIsNotNone(op.skill(skill.name))
        self.assertTrue(path.exists())

    def test_file_to_operator_from_dict_agrees(self):
        """文件入口与"先 json.loads 再 operator_from_dict"必须给出相同结果。

        取前若干份草稿就够：这条测的是两条路径的一致性，
        与具体是哪几份草稿无关。
        """
        for path in self.paths[:20]:
            with self.subTest(file=path.name):
                data = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(
                    loader.load_operator(path), loader.operator_from_dict(data)
                )


if __name__ == "__main__":
    unittest.main()
