# arkdps

[![CI](https://github.com/Ray144165154/arkdps/actions/workflows/ci.yml/badge.svg)](https://github.com/Ray144165154/arkdps/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![零依赖](https://img.shields.io/badge/运行时依赖-0-brightgreen)](tools/check_zero_deps.py)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**数据驱动的明日方舟 DPS / DPH 计算引擎。**

引擎里**没有任何干员数值**。它只知道"一个攻击力 X、攻击间隔 Y、带这些技能参数的东西"
能打多少伤害；干员数据是**输入**，从 JSON 读进来。加干员、改数据都不用碰代码。

```console
$ arkdps calc 银灰 --def 800 --hp 20000
银灰  6★  近卫 / 领主
面板攻击 789    攻击间隔 1.30s    伤害类型 物理
天赋 攻击力+10%    特性 （无）
目标 防御 800    法抗 0    生命 20000

技能          攻击力   间隔     DPH  技能期 DPS  循环 DPS  覆盖率    击杀
------------  ------  -----  ------  ----------  --------  ------  ------
（常态）       867.9  1.300    67.9        52.2      52.2       —       —
强力击·γ型    2516.9  1.300  1716.9      1320.7     686.5   50.0%   29.1s
雪境生存法则   867.9  1.300    67.9        52.2      52.2  100.0%  382.9s
真银斩        2445.9  1.300  1645.9      1266.1     355.7   25.0%    9.4s

说明
  · 强力击·γ型：瞬发技能：按「2 次攻击中有 1 次被强化」计算
  · 雪境生存法则：形态切换类技能：已按「开启后一直保持该形态」计算，……
  · 真银斩：同时攻击 6 个目标，总 DPS 为上表数值 × 6

⚠ 特性有 1 项条件效果**未计入**（随战场情况变化）
```

---

## 它和"手填数值的计算器"有什么不同

| | 常见做法 | 这个项目 |
|---|---|---|
| 加一个干员 | 改代码、加一行常量 | 丢一份 JSON 进 `data/`，代码不动 |
| 技能描述 | 人工读、人工换算成倍率 | 自动解析中文描述 |
| 遇到没见过的写法 | 加一条新规则 | 按**概念**匹配，不按句子 |
| 认不出来时 | 套一个"看起来合理"的默认值 | **标出来让人看**，不猜 |
| 算出来的数 | 只有一个结果 | 保留中间量，可逐项核对 |

最后两条是这个项目真正的重点。

### 数据从哪来

`arkdps import` 从 [PRTS wiki](https://prts.wiki) 抓全部 460 个干员的页面，
解析中文技能描述，产出**带来源标注的草稿**：

- 有一条**词表**把「攻击力/攻击/自身攻击」归到同一个概念，把
  「提高至」和「提高」分成两种关系（一个乘算、一个加算）
- 用**花括号配对**而不是正则解析 wiki 模板（模板会嵌套）
- 认出来的每条都记录"哪句话变成了哪个字段"
- 认不出来的连**原文**一起记下来，并评估"它会不会影响 DPS"

实测结果是 1005 个技能里 **66% 完全解析**，21% 需要人看一眼——
这个数字是**报出来的**，不是藏起来的。

```console
$ arkdps coverage
解析覆盖率
========================================================
  干员草稿      460
  技能总数      1005
  完全解析        659  (66%)
  有无关的未知    130  (13%)
  需人工复核      216  (21%)

最常出现的「可能影响 DPS 但没解析出来」片段（前 5）
--------------------------------------------------------
   3  下次攻击会把目标往攻击方向较大力地推开
   2  第一天赋效果提升至3倍
   2  第二次及以后使用时能力加成变为最初的两倍
   2  天赋的发动概率提升至100%
```

---

## 快速开始

零运行时依赖，clone 下来直接跑，不需要装任何东西：

```bash
git clone https://github.com/Ray144165154/arkdps
cd arkdps
python -m arkdps list                        # 有哪些干员
python -m arkdps calc 银灰 --def 800         # 核心命令
```

也可以装成命令行工具：

```bash
pip install .
arkdps calc 能天使 --def 400 --hp 20000
```

### 常用参数

```bash
arkdps calc 银灰 --def 800 --res 0 --hp 20000   # 目标防御 / 法抗 / 生命
arkdps calc 银灰 --targets 6                    # 强制指定同时攻击目标数
arkdps calc 银灰 --extra-sp 0.5                 # 队友提供的额外技力回复
arkdps calc 银灰 --hits-taken 2                 # 受击回复技能假设的每秒受击次数
arkdps calc 银灰 --first-open                   # 按首次开技能算（计入初始技力）
arkdps calc 银灰 --json                         # 机器可读输出
```

### 其他子命令

```bash
arkdps show 能天使                    # 看原始数据、解析结果、卡在哪
arkdps compare 银灰 能天使 史尔特尔    # 同一条件横比
arkdps coverage                       # 全局解析覆盖率
arkdps import --class 近卫             # 重新导入某个职业
```

---

## 为什么公式要单独写文档

因为"算出来是多少"必须能**核对**，不能只靠相信。

👉 **[docs/FORMULAS.md](docs/FORMULAS.md)** —— 每条公式、每个中间量、
以及一个可以口算验证的例子。

几个真实踩过的坑（都写进文档了）：

- **百分比加算、倍率乘算**。「攻击力+100%」和「攻击力提高至110%」同时存在时，
  正确结果是 `1 + 100%` 再 `× 110% = 2.2 倍`，不是加在一起。
- **有效法抗可以为负**。法抗被削成负数时，法术伤害**高于**攻击力——
  这是机制，不是 bug，所以代码里刻意没有做下限截断。
- **阻回**。技能持续期间技力停止回复，充能时间是**整段**的
  `技耗 ÷ 回复速度`，不是"在循环里平摊"。写错了短持续技能会被严重高估。
- **四种技能形态**。持续 / 瞬发 / 无限持续 / 形态切换，混在一起算会得到
  荒谬的结果——瞬发技能按"持续 0 秒"算会得出**无穷大** DPS。
- **DPH ≠ DPS**。慢速高伤（攻击力 2000、间隔 2.0s）DPH 高一倍，
  DPS 却低一半。打高防目标前者更优。

---

## 诚实是有代价的，也是要花钱实现的

这个项目里最难的部分不是公式，而是**判断"什么时候不该给数"**：

- 「怪娃娃周围**敌人的**攻击力和防御力-50%」说的是减**敌人**的攻，
  套到干员自己身上会让 DPS 直接砍半。引擎**不套用**，只报出来。
- 「可以进行远程攻击，**但此时**攻击力降低至80%」只在远程攻击时生效。
  当成常驻会算出**恒定偏低 20%** 的 DPS，而且从结果上看不出哪里错了。
  所以带条件的子句单独存放，**默认不参与计算**。
- 「攻击范围扩大**且**攻击力+50%」里「攻击范围」更长，最长词匹配会赢，
  整个攻击力加成会消失。所以要在连接词处切开，并且对复合子句报警。

数据文件里的每一个 `_unparsed`、`_conditional`、`_needs_review` 都是这套东西的产物：

```json
{
  "_confidence": "partial",
  "_parsed": [
    { "text": "攻击力+200%", "attribute": "atk", "field": "atk_pct", "value": 200.0 }
  ],
  "_unparsed": [
    { "text": "第一天赋效果提升至3倍", "kind": "unknown", "affects_damage": true }
  ]
}
```

---

## 项目结构

```
arkdps/
├── arkdps/
│   ├── rules.py         机制常量（帧率、最低伤害系数、技力回复）
│   ├── model.py         数据模型（Effects / Skill / Operator / Enemy）
│   ├── combat.py        攻击力与攻击间隔的合成
│   ├── damage.py        三条伤害公式
│   ├── rotation.py      技能循环求解 → DPS / DPH / 覆盖率
│   ├── lexicon.py       词表：说法 → 概念
│   ├── recognizer.py    中文描述 → 语义三元组
│   ├── mapping.py       语义三元组 → 引擎字段
│   ├── loader.py        JSON 草稿 → 数据模型
│   ├── cli.py           命令行
│   └── importers/
│       ├── wikitext.py  wiki 模板解析
│       ├── prts.py      PRTS 页面 → 草稿
│       └── bulk.py      全量导入
├── data/
│   ├── operators/       460 份干员草稿（已提交，clone 下来就能算）
│   └── _coverage.json   解析覆盖率报告
├── docs/
│   ├── FORMULAS.md      公式文档
│   └── DATA-FORMAT.md   草稿数据格式
├── tests/               547 个用例
└── tools/
    ├── check_zero_deps.py  零依赖承诺的自动检查
    ├── ci_smoke.py         命令行端到端冒烟测试
    └── import_all.py       全量导入入口
```

分层是刻意的：**采集/IO 层**（`importers`）与**纯推理层**（`recognizer` / `mapping` /
`combat` / `damage` / `rotation`）完全分开。纯推理层不碰文件、不碰网络、不依赖时间，
所以可以精确测试——547 个用例全部离线，不用网络。

---

## 测试

```bash
python run_tests.py              # 全部
python run_tests.py -v           # 列出每个用例
python run_tests.py test_rotation
```

547 个用例，覆盖三层：

- **纯计算层**：伤害公式、攻击力合成、四种技能循环。期望值全部可以口算核对。
- **语义层**：中文描述识别、字段映射。每个「回归」标记的用例都对应一个真实踩过的坑。
- **端到端**：从 JSON 草稿一路算到 DPS，外加拿**全部 460 份真实草稿**跑一遍的健壮性检查。

CI 在 Linux / Windows / macOS × Python 3.10–3.13 上跑，
另外检查零依赖承诺、打包配置完整性、以及数据完整性。

### 写测试时挖出来的 bug

这些错误**都不会报错**，只会让数字悄悄错掉：

| 表现 | 原因 |
|---|---|
| 减攻变加攻 | `攻击力-30%` 的负号已在数值里，映射层又减了一次（双重取负） |
| 整条效果消失 | 词表漏了「缩短」，`攻击间隔缩短0.22` 映射不出来 |
| **DPS 砍半** | 「**敌人的**攻击力-50%」被当成干员自己的减攻 |
| 少一星 | 稀有度 `0` 是合法的 1 星，被 `value or -1` 换成了 `-1` |
| 面板攻击力为空 | PRTS 按精英阶段分档存面板值，1–3★ 没有 `精英2` 那一档 |
| **总 DPS 翻倍** | 「能够阻挡两个**敌人**」被当成"同时攻击 2 个目标"（72 个干员中招） |
| 定身变自动回复 | `str(ChargeType.HIT)` 在 Python 3.12 得到 `"ChargeType.HIT"`，被兜底静默吞掉 |

---

## 已知限制

诚实列出来比假装没有好。以下情况引擎**不**处理，或只做近似：

- 「第一天赋效果提升至N倍」「攻击变为群体攻击」「攻击装有N发子弹」——认不出来，标为待复核
- 浮游单元 / 召唤物的输出——只算干员本体
- 多段攻击中**各段倍率不同**——按各段相同处理
- 链式跳跃（每跳伤害递减）——用单一目标数表达不了
- 条件效果——不参与计算，单独列出
- 敌人属性被技能改变——不套用到干员身上，标为待确认

完整列表见 [docs/FORMULAS.md 第 11 节](docs/FORMULAS.md#11-建模边界)。

---

## English summary

**arkdps** is a data-driven DPS/DPH calculator for Arknights. The engine contains
**no operator data at all** — it only implements the damage formulas, attack-interval
composition, and skill-rotation solving, and reads operators from JSON.

It ships with an importer that parses Chinese skill descriptions from the PRTS wiki into
engine parameters using a **concept lexicon** rather than sentence-by-sentence rules, and
emits drafts annotated with `_source` / `_parsed` / `_unparsed` / `_confidence`.
Of 1005 imported skills, 66% parse exactly and 21% are **reported** as needing human
review — the number is surfaced, not hidden.

Design principle: **a silent error is worse than missing data.** When a clause cannot be
resolved, the calculator flags it instead of guessing a plausible default.

- Zero runtime dependencies (stdlib only, enforced by [`tools/check_zero_deps.py`](tools/check_zero_deps.py))
- 547 offline tests, CI on Linux / Windows / macOS × Python 3.10–3.13
- Formulas documented and hand-checkable: [docs/FORMULAS.md](docs/FORMULAS.md)

---

## License

MIT —— 见 [LICENSE](LICENSE)。

本项目是一个**计算工具**，与鹰角网络无关联。干员数据取自
[PRTS wiki](https://prts.wiki)，版权归原作者所有。
