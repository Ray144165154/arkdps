"""命令行接口。

五个子命令::

    arkdps list                      列出已导入的干员
    arkdps show 银灰                 看一个干员的原始数据与解析情况
    arkdps calc 银灰 --def 800       算 DPS / DPH（核心命令）
    arkdps compare 银灰 能天使        按同一条件横比多个干员
    arkdps coverage                  解析覆盖率报告
    arkdps import --class 近卫        从 PRTS 导入草稿

不带子命令时打印帮助。

**数据是输入，不是代码**：引擎里没有任何干员数值，全部从 ``data/operators/``
的 JSON 读进来。所以换数据、加干员都不用改这个文件。
"""

from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from pathlib import Path

from . import __version__
from .importers.bulk import DEFAULT_CACHE_DIR, DEFAULT_DATA_DIR, run_import, safe_filename
from .loader import load_operator, load_operators
from .model import DamageType, Effects, Enemy, Operator
from .rotation import RotationResult, solve_operator

__all__ = ["main", "build_parser"]

#: 伤害类型 → 中文
_DAMAGE_TYPE_CN = {
    DamageType.PHYSICAL: "物理",
    DamageType.ARTS: "法术",
    DamageType.TRUE: "真实",
}


# --------------------------------------------------------------------------
# 输出
# --------------------------------------------------------------------------

def _configure_output_encoding() -> None:
    """把标准输出/错误切到 UTF-8。

    Windows 上 stdout 被重定向时的默认编码是 GBK，而本工具的输出
    （包括 argparse 的帮助文本）全是中文——不重配 Microsoft 会直接抛
    ``UnicodeEncodeError``。``build_parser()`` 里也调一次，这样直接拿
    parser 打印 ``--help`` 的调用方（测试、嵌进别的程序）同样不会崩。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def _write(text: str) -> None:
    if not text:
        return
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        buffer = getattr(sys.stdout, "buffer", None)
        if buffer is None:
            sys.stdout.write(text.encode("ascii", "replace").decode("ascii"))
        else:
            buffer.write(text.encode("utf-8", "replace"))


def _error(message: str) -> None:
    print(f"错误：{message}", file=sys.stderr)


# --------------------------------------------------------------------------
# 表格对齐
# --------------------------------------------------------------------------
#
# 中日韩字符在终端里占**两格**，但 ``len()`` 只算一个。直接用 ljust 排版，
# 中文列会和英文列错开——一个满是中文的表格错位到看不清。
# 所以按显示宽度算，而不是按字符个数。

def _disp_width(text: str) -> int:
    return sum(
        2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        for ch in text
    )


def _pad(text: str, width: int, right: bool = False) -> str:
    filler = " " * max(0, width - _disp_width(text))
    return filler + text if right else text + filler


def _render_table(headers: list[str], rows: list[list[str]], right: list[int]) -> str:
    """渲染一张对齐的表。``right`` 是要右对齐的列下标。"""
    columns = len(headers)
    widths = []
    for index in range(columns):
        cells = [headers[index]] + [row[index] for row in rows]
        widths.append(max(_disp_width(cell) for cell in cells))

    lines = [
        "  ".join(
            _pad(headers[i], widths[i], right=i in right) for i in range(columns)
        )
    ]
    lines.append("  ".join("-" * widths[i] for i in range(columns)))
    for row in rows:
        lines.append(
            "  ".join(
                _pad(row[i], widths[i], right=i in right) for i in range(columns)
            )
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 人类可读的 effects
# --------------------------------------------------------------------------

#: ``(字段, 中文名, 格式)``。格式决定符号怎么读：
#:
#:   ``delta%``  增减百分比 → ``攻击力+10%``
#:   ``delta``   增减数值   → ``攻速+12``
#:   ``deltas``  增减秒数   → ``间隔-0.22s``
#:   ``times``   倍率       → ``攻击力×1.45``
#:   ``assign``  **覆盖**   → ``段数=3``
#:
#: ``assign`` 不能写成 ``段数+3``——那读起来像"再增加 3 段"，
#: 而覆盖语义是"一共 3 段"，算出来的伤害差一倍。
_EFFECT_LABELS: tuple[tuple[str, str, str], ...] = (
    ("atk_pct", "攻击力", "delta%"),
    ("atk_mult", "攻击力", "times"),
    ("aspd", "攻速", "delta"),
    ("interval_flat", "间隔", "deltas"),
    ("interval_mult", "间隔", "times"),
    ("hits_override", "段数", "assign"),
    ("hits_mult", "段数", "times"),
    ("hits_add", "额外段数", "delta"),
    ("targets", "目标数", "assign"),
    ("def_ignore_pct", "无视防御", "delta%"),
    ("def_ignore", "无视防御", "delta"),
    ("res_ignore_pct", "无视法抗", "delta%"),
    ("res_ignore", "无视法抗", "delta"),
    ("bonus_arts_pct", "附加法术", "delta%"),
    ("bonus_true_pct", "附加真实", "delta%"),
    ("dot_pct", "持续伤害", "delta%"),
)

_EFFECT_NEUTRAL: dict[str, object] = {
    "atk_pct": 0.0, "atk_mult": 1.0, "aspd": 0.0,
    "interval_flat": 0.0, "interval_mult": 1.0,
    "hits_mult": 1.0, "hits_add": 0.0, "targets": 1,
    "def_ignore_pct": 0.0, "def_ignore": 0.0,
    "res_ignore_pct": 0.0, "res_ignore": 0.0,
    "bonus_arts_pct": 0.0, "bonus_true_pct": 0.0, "dot_pct": 0.0,
}


def _format_one(label: str, value: float, kind: str) -> str:
    if kind == "times":
        return f"{label}×{value:g}"
    if kind == "assign":
        return f"{label}={value:g}"
    if kind == "delta%":
        return f"{label}{value:+g}%"
    if kind == "deltas":
        return f"{label}{value:+g}s"
    return f"{label}{value:+g}"


def format_effects(effects: Effects) -> str:
    """把一组修正写成人话，例如 ``攻击力+10%，攻速+12``。

    等于默认值的字段不显示——否则每行都拖一串 "+0%" 的噪音。
    """
    parts: list[str] = []
    for key, label, kind in _EFFECT_LABELS:
        value = getattr(effects, key, None)
        if value is None or value == _EFFECT_NEUTRAL.get(key):
            continue
        parts.append(_format_one(label, float(value), kind))

    if getattr(effects, "targets_scope", None):
        scope_cn = {
            "all_blocked": "阻挡的所有敌人",
            "all_in_range": "范围内的所有敌人",
        }.get(effects.targets_scope, effects.targets_scope)
        parts.append(f"目标={scope_cn}")
    if effects.damage_type is not None:
        parts.append(f"伤害类型={_DAMAGE_TYPE_CN.get(effects.damage_type, '?')}")
    if not effects.attacks:
        parts.append("不进行普攻")

    return "，".join(parts) if parts else "（无）"


# --------------------------------------------------------------------------
# 找数据
# --------------------------------------------------------------------------

def _find_draft(name: str, data_dir: Path | str) -> Path | None:
    """按干员名找草稿文件。

    先试文件名（快的路），再退回去逐份读 ``name`` 字段——因为
    ``GALLUS²``、``Raidian(卫戍协议)`` 这类名字经过文件名清洗后
    和用户输入对不上，只按文件名找会"找不到明明存在的干员"。
    """
    data_dir = Path(data_dir)
    direct = data_dir / f"{safe_filename(name)}.json"
    if direct.is_file():
        return direct

    wanted = name.strip().casefold()
    if not data_dir.is_dir():
        return None
    for path in sorted(data_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and str(data.get("name", "")).strip().casefold() == wanted:
            return path
    return None


def _resolve_one(name: str, data_dir: Path | str) -> Operator:
    path = _find_draft(name, data_dir)
    if path is None:
        raise FileNotFoundError(
            f"找不到干员「{name}」。先跑 `arkdps list` 看看有哪些，"
            "或用 `arkdps import` 导入数据。"
        )
    return load_operator(path)


# --------------------------------------------------------------------------
# 子命令
# --------------------------------------------------------------------------

def _cmd_list(args: argparse.Namespace) -> int:
    operators = load_operators(args.data)
    if not operators:
        _write(
            f"没有数据：{args.data}\n"
            "先跑 `arkdps import` 从 PRTS 导入干员草稿。\n"
        )
        return 1

    if args.klass:
        operators = [op for op in operators if op.class_name == args.klass]
    if args.rarity is not None:
        operators = [op for op in operators if op.rarity == args.rarity]

    if args.json:
        _write(json.dumps([
            {
                "name": op.name,
                "rarity": op.rarity,
                "class": op.class_name,
                "branch": op.branch,
                "atk": op.base_atk,
                "interval": op.interval,
                "skills": op.skill_names,
                "note": op.note,
            }
            for op in operators
        ], ensure_ascii=False, indent=1) + "\n")
        return 0

    if not operators:
        _write("没有符合条件的干员。\n")
        return 0

    rows = []
    for op in operators:
        rows.append([
            ("⚠ " if op.note else "  ") + op.name,
            f"{op.rarity:g}★" if op.rarity else "—",
            op.class_name or "—",
            f"{op.base_atk:.0f}",
            f"{op.interval:.2f}",
            str(len(op.skills)),
        ])

    _write(_render_table(
        ["干员", "稀有度", "职业", "面板攻击", "间隔", "技能数"],
        rows,
        right=[1, 3, 4, 5],
    ) + "\n")
    _write(f"\n共 {len(operators)} 个干员。「⚠」表示数据有需要注意的地方，"
           "用 `arkdps show 名字` 看详情。\n")
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    try:
        op = _resolve_one(args.name, args.data)
    except FileNotFoundError as exc:
        _error(str(exc))
        return 1

    if args.json:
        _write(json.dumps({
            "name": op.name,
            "rarity": op.rarity,
            "class": op.class_name,
            "branch": op.branch,
            "atk": op.atk,
            "atk_trust": op.atk_trust,
            "atk_potential": op.atk_potential,
            "base_atk": op.base_atk,
            "interval": op.interval,
            "damage_type": op.damage_type.value,
            "trait_text": op.trait_text,
            "trait": format_effects(op.trait),
            "talent": format_effects(op.talent),
            "note": op.note,
            "source": op.source,
            "skills": [
                {
                    "name": skill.name,
                    "charge": skill.charge.value,
                    "sp_cost": skill.sp_cost,
                    "init_sp": skill.init_sp,
                    "duration": skill.duration,
                    "infinite_duration": skill.infinite_duration,
                    "stance": skill.stance,
                    "effects": format_effects(skill.effects),
                    "confidence": skill.confidence,
                    "source": skill.source,
                    "unparsed": skill.unparsed,
                }
                for skill in op.skills
            ],
        }, ensure_ascii=False, indent=1) + "\n")
        return 0

    out: list[str] = []
    out.append(f"干员    {op.name}")
    out.append(f"稀有度  {f'{op.rarity:g} 星' if op.rarity else '未知'}")
    out.append(f"职业    {op.class_name}{' / ' + op.branch if op.branch else ''}")
    out.append(
        f"面板    攻击力 {op.atk:.0f}"
        f"（信赖 +{op.atk_trust:.0f}，潜能 +{op.atk_potential:.0f}"
        f" → {op.base_atk:.0f}）"
    )
    out.append(f"        攻击间隔 {op.interval:.2f}s    伤害类型 "
               f"{_DAMAGE_TYPE_CN.get(op.damage_type, '?')}")
    if op.trait_text:
        out.append(f"特性    {op.trait_text}")
    out.append(f"        解析为：{format_effects(op.trait)}")
    out.append(f"天赋    {format_effects(op.talent)}")

    if op.note:
        out.append("")
        out.append("⚠ 需要注意")
        for piece in op.note.split("；"):
            if piece.strip():
                out.append(f"    {piece.strip()}")

    out.append("")
    out.append("技能")
    for index, skill in enumerate(op.skills, 1):
        condition = []
        if skill.infinite_duration:
            condition.append("持续无限")
        elif skill.duration:
            condition.append(f"持续 {skill.duration:g}s")
        else:
            condition.append("瞬发")
        if skill.stance:
            condition.append("形态切换")

        out.append(
            f"  {index}. {skill.name}"
            f"    {skill.charge.value} / SP {skill.sp_cost:g}"
            f"    {'，'.join(condition)}"
        )
        out.append(f"     效果  {format_effects(skill.effects)}")
        if skill.source:
            out.append(f"     原文  {skill.source}")
        risk = [item for item in skill.unparsed
                if item.get("affects_damage") is not False]
        if risk:
            out.append(f"     ⚠ {len(risk)} 处可能影响 DPS 但没解析出来：")
            for item in risk[:5]:
                out.append(f"         · {item.get('text', '')}")
            if len(risk) > 5:
                out.append(f"         …… 还有 {len(risk) - 5} 处")

    _write("\n".join(out) + "\n")
    return 0


def _load_enemy(args: argparse.Namespace) -> Enemy:
    hp = args.hp
    return Enemy(
        name="命令行目标",
        defense=float(args.defense),
        res=float(args.res),
        hp=float("inf") if hp is None else float(hp),
    )


def _solve_kwargs(args: argparse.Namespace) -> dict:
    return {
        "target_count": args.targets,
        "extra_sp_regen": args.extra_sp,
        "hits_taken_per_second": args.hits_taken,
        "use_initial_sp": args.first_open,
    }


def _cmd_calc(args: argparse.Namespace) -> int:
    try:
        op = _resolve_one(args.name, args.data)
    except FileNotFoundError as exc:
        _error(str(exc))
        return 1

    enemy = _load_enemy(args)
    try:
        rows = solve_operator(op, enemy, **_solve_kwargs(args))
    except ValueError as exc:
        _error(f"算不出来：{exc}")
        return 1

    if args.skill:
        picked = [r for r in rows if not r.is_normal_row and r.skill == args.skill]
        if not picked:
            _error(
                f"「{op.name}」没有叫「{args.skill}」的技能。"
                f"可选：{'、'.join(op.skill_names) or '（无）'}"
            )
            return 1
        rows = [r for r in rows if r.is_normal_row] + picked

    if args.json:
        _write(json.dumps({
            "operator": op.name,
            "enemy": {
                "defense": enemy.defense,
                "res": enemy.res,
                "hp": None if enemy.hp == float("inf") else enemy.hp,
            },
            "rows": [_result_to_dict(row) for row in rows],
        }, ensure_ascii=False, indent=1) + "\n")
        return 0

    out: list[str] = []
    out.append(
        f"{op.name}"
        f"{f'  {op.rarity:g}★' if op.rarity else ''}"
        f"  {op.class_name}{' / ' + op.branch if op.branch else ''}"
    )
    out.append(
        f"面板攻击 {op.base_atk:.0f}    攻击间隔 {op.interval:.2f}s"
        f"    伤害类型 {_DAMAGE_TYPE_CN.get(op.damage_type, '?')}"
    )
    out.append(f"天赋 {format_effects(op.talent)}    特性 {format_effects(op.trait)}")
    hp_text = "∞" if enemy.hp == float("inf") else f"{enemy.hp:.0f}"
    out.append(
        f"目标 防御 {enemy.defense:.0f}    法抗 {enemy.res:.0f}    生命 {hp_text}"
    )
    if args.targets:
        out.append(f"（目标数被强制指定为 {args.targets}）")
    out.append("")
    out.append(_render_table(
        ["技能", "攻击力", "间隔", "DPH", "技能期 DPS", "循环 DPS", "覆盖率", "击杀"],
        [[
            "（常态）" if row.is_normal_row else row.skill,
            f"{row.atk:.1f}",
            f"{row.interval:.3f}",
            f"{row.dph:.1f}",
            f"{row.dps_skill:.1f}",
            f"{row.dps_cycle:.1f}",
            "—" if row.is_normal_row else f"{row.uptime * 100:.1f}%",
            "—" if row.ttk == float("inf") else f"{row.ttk:.1f}s",
        ] for row in rows],
        right=[1, 2, 3, 4, 5, 6, 7],
    ))

    notes: list[str] = []
    for row in rows:
        for note in row.notes:
            entry = f"{'' if row.is_normal_row else row.label + '：'}{note}"
            if entry not in notes:
                notes.append(entry)
    if notes:
        out.append("")
        out.append("说明")
        for note in notes:
            out.append(f"  · {note}")

    if op.note:
        out.append("")
        out.append(f"⚠ {op.note}")

    _write("\n".join(out) + "\n")
    return 0


def _result_to_dict(row: RotationResult) -> dict:
    return {
        "skill": row.skill,
        "is_normal": row.is_normal_row,
        "damage_type": row.damage_type.value,
        "targets": row.targets,
        "atk": row.atk,
        "interval": row.interval,
        "dph": row.dph,
        "dph_per_hit": row.dph_per_hit,
        "dps_normal": row.dps_normal,
        "dps_skill": row.dps_skill,
        "dps_cycle": row.dps_cycle,
        "dps_cycle_total": row.dps_cycle_total,
        "uptime": row.uptime,
        "charge_time": row.charge_time,
        "cycle_time": row.cycle_time,
        "ttk": None if row.ttk == float("inf") else row.ttk,
        "notes": row.notes,
    }


def _cmd_compare(args: argparse.Namespace) -> int:
    enemy = _load_enemy(args)
    operators = []
    for name in args.names:
        try:
            operators.append(_resolve_one(name, args.data))
        except FileNotFoundError as exc:
            _error(str(exc))
            return 1

    rows_data = []
    for op in operators:
        result = solve_operator(op, enemy, **_solve_kwargs(args))
        best = max(
            (r for r in result if not r.is_normal_row),
            key=lambda r: r.dps_cycle,
            default=None,
        )
        normal = next((r for r in result if r.is_normal_row), None)
        rows_data.append((op, best, normal))

    if args.by == "dph":
        rows_data.sort(key=lambda item: item[1].dph if item[1] else 0.0, reverse=True)
    else:
        rows_data.sort(
            key=lambda item: item[1].dps_cycle if item[1] else 0.0, reverse=True
        )

    if args.json:
        _write(json.dumps([
            {
                "operator": op.name,
                "best_skill": best.skill if best else None,
                "dph": best.dph if best else None,
                "dps_cycle": best.dps_cycle if best else None,
                "uptime": best.uptime if best else None,
                "normal_dps": normal.dps_normal if normal else None,
            }
            for op, best, normal in rows_data
        ], ensure_ascii=False, indent=1) + "\n")
        return 0

    out = [
        f"目标 防御 {enemy.defense:.0f}    法抗 {enemy.res:.0f}    "
        f"排序依据 {'DPH' if args.by == 'dph' else '循环 DPS'}",
        "",
    ]
    out.append(_render_table(
        ["干员", "最佳技能", "DPH", "循环 DPS", "覆盖率", "常态 DPS"],
        [[
            op.name,
            best.skill if best else "—",
            f"{best.dph:.1f}" if best else "—",
            f"{best.dps_cycle:.1f}" if best else "—",
            f"{best.uptime * 100:.1f}%" if best else "—",
            f"{normal.dps_normal:.1f}" if normal else "—",
        ] for op, best, normal in rows_data],
        right=[2, 3, 4, 5],
    ))
    out.append("")
    out.append("「最佳技能」按循环 DPS 选。同一个干员换技能可能 DPH 更高但 DPS 更低——")
    out.append("用 `arkdps calc 名字` 看全部技能。")
    _write("\n".join(out) + "\n")
    return 0


def _cmd_coverage(args: argparse.Namespace) -> int:
    report_path = Path(args.data).parent / "_coverage.json"
    if not report_path.is_file():
        _error(f"没有覆盖率报告：{report_path}\n先跑 `arkdps import`。")
        return 1

    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _error(f"读不了覆盖率报告：{exc}")
        return 1

    if args.json:
        _write(json.dumps(report, ensure_ascii=False, indent=1) + "\n")
        return 0

    skills = report.get("skills", 0) or 0
    confidence = report.get("confidence", {})
    exact = confidence.get("exact", 0)
    partial = confidence.get("partial", 0)
    low = confidence.get("low", 0)

    out = [
        "解析覆盖率",
        "=" * 56,
        f"  干员草稿      {report.get('operators', 0)}",
        f"  技能总数      {skills}",
    ]
    if skills:
        out.append(f"  完全解析      {exact:>5}  ({exact / skills:.0%})")
        out.append(f"  有无关的未知  {partial:>5}  ({partial / skills:.0%})")
        out.append(f"  需人工复核    {low:>5}  ({low / skills:.0%})")
    out.append(
        f"  缓存命中      {report.get('cache_hits', 0)}"
        f"   实际请求 {report.get('requests', 0)}"
    )

    top = report.get("top_unresolved_risky") or []
    if top:
        out.append("")
        out.append(f"最常出现的「可能影响 DPS 但没解析出来」片段（前 {min(args.top, len(top))}）")
        out.append("-" * 56)
        rows = [[str(count), text] for text, count in top[: args.top]]
        out.append(_render_table(["次数", "片段"], rows, right=[0]))

    out.append("")
    out.append("「需人工复核」不一定是错的——也可能是识别器**故意**不猜的地方。")
    out.append("用 `arkdps show 名字` 看某个干员具体卡在哪。")
    _write("\n".join(out) + "\n")
    return 0


def _cmd_import(args: argparse.Namespace) -> int:
    def progress(done: int, total: int, written: int, elapsed: float) -> None:
        _write(f"  {done}/{total}  已写 {written}  用时 {elapsed:.0f}s\n")

    _write("枚举干员…\n")
    try:
        report = run_import(
            class_name=args.klass,
            limit=args.limit,
            out_dir=args.data,
            cache_dir=args.cache,
            delay=args.delay,
            progress=progress,
        )
    except OSError as exc:
        _error(f"导入失败：{exc}")
        return 1

    if args.json:
        _write(json.dumps(report.as_dict(), ensure_ascii=False, indent=1) + "\n")
        return 0

    skills = report.skills
    out = [
        "",
        "=" * 56,
        "  导入完成",
        "=" * 56,
        f"  干员草稿      {report.operators}",
        f"  技能总数      {skills}",
    ]
    if skills:
        out.append(f"  完全解析      {report.confidence.get('exact', 0):>5}"
                   f"  ({report.exact_ratio:.0%})")
        out.append(f"  有无关的未知  {report.confidence.get('partial', 0):>5}"
                   f"  ({report.partial_ratio:.0%})")
        out.append(f"  需人工复核    {report.confidence.get('low', 0):>5}"
                   f"  ({report.low_ratio:.0%})")
    out.append(f"  缓存命中      {report.cache_hits}   实际请求 {report.requests}")
    out.append(f"  用时          {report.elapsed:.0f}s")
    if report.failures:
        out.append(f"  失败          {len(report.failures)}: {report.failures[:10]}")
    out.append(f"  已写入        {Path(args.data)}")
    out.append(f"  覆盖率报告    {Path(args.data).parent / '_coverage.json'}")
    _write("\n".join(out) + "\n")
    return 0


# --------------------------------------------------------------------------
# 参数
# --------------------------------------------------------------------------

def _add_data_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--data", metavar="DIR", default=str(DEFAULT_DATA_DIR),
        help=f"草稿目录（默认 {DEFAULT_DATA_DIR}）",
    )


def _add_target_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--def", dest="defense", type=float, default=0.0,
                        metavar="N", help="目标防御力（默认 0）")
    parser.add_argument("--res", dest="res", type=float, default=0.0,
                        metavar="N", help="目标法术抗性（默认 0）")
    parser.add_argument("--hp", type=float, default=None, metavar="N",
                        help="目标生命值，给了就算击杀耗时")
    parser.add_argument("--targets", type=int, default=None, metavar="N",
                        help="强制指定同时攻击目标数（默认按技能数据）")
    parser.add_argument("--extra-sp", dest="extra_sp", type=float, default=0.0,
                        metavar="N", help="队友提供的额外技力回复（点/秒）")
    parser.add_argument("--hits-taken", dest="hits_taken", type=float, default=1.0,
                        metavar="N", help="受击回复技能假设的每秒受击次数（默认 1）")
    parser.add_argument("--first-open", dest="first_open", action="store_true",
                        help="按首次开技能算（计入初始技力）")


def build_parser() -> argparse.ArgumentParser:
    _configure_output_encoding()

    parser = argparse.ArgumentParser(
        prog="arkdps",
        description="数据驱动的明日方舟 DPS / DPH 计算引擎 —— 引擎里没有干员数值，数据是输入",
        epilog=(
            "示例:\n"
            "  arkdps list                          有哪些干员\n"
            "  arkdps calc 银灰 --def 800           打 800 防御的 DPS / DPH\n"
            "  arkdps calc 银灰 --skill 真银斩 --hp 20000\n"
            "  arkdps compare 银灰 能天使 --def 800  同一条件横比\n"
            "  arkdps show 银灰                     看原始数据与解析情况\n"
            "  arkdps coverage                      解析覆盖率报告\n"
            "  arkdps import --class 近卫            从 PRTS 导入草稿\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-V", "--version", action="version",
                        version=f"arkdps {__version__}")

    sub = parser.add_subparsers(dest="command", metavar="子命令")

    p_list = sub.add_parser("list", help="列出已导入的干员")
    _add_data_arg(p_list)
    p_list.add_argument("--class", dest="klass", help="只看某个职业")
    p_list.add_argument("--rarity", type=float, default=None, help="只看某个星级")
    p_list.add_argument("-j", "--json", action="store_true", help="以 JSON 输出")
    p_list.set_defaults(func=_cmd_list)

    p_show = sub.add_parser("show", help="看一个干员的原始数据与解析情况")
    p_show.add_argument("name", help="干员名")
    _add_data_arg(p_show)
    p_show.add_argument("-j", "--json", action="store_true", help="以 JSON 输出")
    p_show.set_defaults(func=_cmd_show)

    p_calc = sub.add_parser("calc", help="算 DPS / DPH（核心命令）")
    p_calc.add_argument("name", help="干员名")
    p_calc.add_argument("--skill", help="只看某个技能")
    _add_data_arg(p_calc)
    _add_target_args(p_calc)
    p_calc.add_argument("-j", "--json", action="store_true", help="以 JSON 输出")
    p_calc.set_defaults(func=_cmd_calc)

    p_cmp = sub.add_parser("compare", help="按同一条件横比多个干员")
    p_cmp.add_argument("names", nargs="+", help="干员名，可以给多个")
    _add_data_arg(p_cmp)
    _add_target_args(p_cmp)
    p_cmp.add_argument("--by", choices=("cycle", "dph"), default="cycle",
                       help="排序依据（默认循环 DPS）")
    p_cmp.add_argument("-j", "--json", action="store_true", help="以 JSON 输出")
    p_cmp.set_defaults(func=_cmd_compare)

    p_cov = sub.add_parser("coverage", help="解析覆盖率报告")
    _add_data_arg(p_cov)
    p_cov.add_argument("--top", type=int, default=15, help="列出多少条未解析片段")
    p_cov.add_argument("-j", "--json", action="store_true", help="以 JSON 输出")
    p_cov.set_defaults(func=_cmd_coverage)

    p_imp = sub.add_parser("import", help="从 PRTS 导入干员草稿")
    _add_data_arg(p_imp)
    p_imp.add_argument("--class", dest="klass", help="只导入某个职业")
    p_imp.add_argument("--limit", type=int, default=0, help="最多导入多少个（0 = 全部）")
    p_imp.add_argument("--cache", default=str(DEFAULT_CACHE_DIR),
                       help=f"页面缓存目录（默认 {DEFAULT_CACHE_DIR}）")
    p_imp.add_argument("--delay", type=float, default=0.25,
                       help="请求间隔秒数（对 wiki 保持礼貌）")
    p_imp.add_argument("-j", "--json", action="store_true", help="以 JSON 输出")
    p_imp.set_defaults(func=_cmd_import)

    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_output_encoding()
    parser = build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "command", None):
        parser.print_help()
        return 0

    return int(args.func(args))
