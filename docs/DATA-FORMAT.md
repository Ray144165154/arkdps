# 草稿数据格式

`data/operators/*.json` 里的每个文件是一个干员。这些文件由
`arkdps import` 从 PRTS 生成，也可以**手写**——引擎不关心你是怎么来的。

字段分两类：

- **不带下划线**的字段是引擎真正读的（`arkdps/loader.py`）
- **带下划线**的字段是解析过程的记录，引擎会忽略它们

设计上的一条规矩：**带下划线的字段永远不会影响计算结果**。
所以你可以放心删掉它们，也可以照着补。

---

## 一个完整的例子

```json
{
  "_schema": "arkdps/operator/1",
  "_source": { "wiki": "PRTS", "page": "银灰" },

  "name": "银灰",
  "class": "近卫",
  "branch": "领主",
  "rarity": 6.0,
  "trait_text": "可以进行远程攻击，但此时攻击力降低至80%",

  "attack_interval": 1.3,
  "atk": 713.0,
  "atk_trust": 50.0,
  "atk_potential": 26.0,

  "trait": {},
  "_trait_parsed": [],
  "_trait_conditional": { "atk_mult": 0.8 },
  "_trait_conditions": [
    {
      "text": "但此时攻击力降低至80%",
      "condition": "但此时",
      "fields": { "atk_mult": 0.8 },
      "reason": "含条件标记「但此时」，是否生效取决于战场情况"
    }
  ],

  "talent": { "atk_pct": 10.0 },
  "_talent_meta": [
    { "name": "领袖", "condition": "精英2", "text": "攻击力+10%" }
  ],
  "_talent_parsed": [
    {
      "text": "攻击力+10%",
      "attribute": "atk",
      "relation": "increase",
      "value": 10.0,
      "field": "atk_pct"
    }
  ],

  "skills": [
    {
      "name": "真银斩",
      "charge": "auto",
      "trigger": "手动触发",
      "sp_cost": 90.0,
      "init_sp": 0.0,
      "duration": 30.0,
      "effects": { "atk_pct": 200.0, "targets": 6 },
      "_source": "攻击力+200%，同时攻击至多6个目标",
      "_parsed": [
        { "text": "攻击力+200%", "attribute": "atk", "relation": "increase",
          "value": 200.0, "field": "atk_pct" },
        { "text": "同时攻击至多6个目标", "attribute": "targets", "relation": "max_of",
          "value": 6.0, "field": "targets" }
      ],
      "_confidence": "exact"
    }
  ],

  "_warnings": []
}
```

---

## 引擎读的字段（不带下划线）

### 干员级

| 字段 | 类型 | 说明 |
|---|---|---|
| `name` | 字符串 | 干员名。`--data` 查找时会用它匹配 |
| `class` | 字符串 | 职业（近卫 / 狙击 / …） |
| `branch` | 字符串 | 分支（领主 / 速射手 / …） |
| `rarity` | 数字 | 星级，**1 起算** |
| `trait_text` | 字符串 | 特性原文，只用于显示 |
| `attack_interval` | 数字 | 基础攻击间隔（秒） |
| `atk` | 数字 | 精英满级面板攻击力 |
| `atk_trust` | 数字 | 信赖加成（**加**在面板上） |
| `atk_potential` | 数字 | 潜能加成（**加**在面板上） |
| `damage_type` | 字符串 | `physical` / `arts` / `true`，缺省 `physical` |
| `hits` | 数字 | 基础段数，缺省 1 |
| `targets` | 整数 | 基础目标数，缺省 1 |
| `trait` | 对象 | 特性的效果字段（见下） |
| `talent` | 对象 | 天赋的效果字段（多个天赋会合并） |
| `skills` | 数组 | 技能列表 |

`atk` / `atk_trust` / `atk_potential` 是**分开**的，因为前两个是加在面板上的，
而技能里的「攻击力+200%」是乘在**总和**上的。混在一起会算错。

### 技能级

| 字段 | 类型 | 说明 |
|---|---|---|
| `name` | 字符串 | 技能名 |
| `charge` | 字符串 | `auto` / `attack` / `hit` / `passive`。不认识的值**兜底成 `auto`** |
| `trigger` | 字符串 | 「自动触发」「手动触发」，只用于显示 |
| `sp_cost` | 数字 | 技力消耗 |
| `init_sp` | 数字 | 初始技力（只在 `--first-open` 时计入） |
| `duration` | 数字 | 持续时间（秒）。`0` 表示**瞬发** |
| `infinite_duration` | 布尔 | 持续时间无限 |
| `stance` | 布尔 | 形态切换类技能 |
| `effects` | 对象 | 效果字段（见下） |

`duration: 0` 与 `infinite_duration: true` 是**两件完全不同的事**：
前者是"只强化一次攻击"，后者是"开了就不会关"。混了会得出无穷大 DPS。

### 效果字段（`effects` / `trait` / `talent` 共用）

数值叠加规则：**百分比加算，倍率乘算**。

| 字段 | 类型 | 缺省 | 含义 |
|---|---|---|---|
| `atk_pct` | 数字 | 0 | 攻击力百分比（加算）。`-30` 表示降低 30% |
| `atk_mult` | 数字 | 1 | 攻击力倍率（乘算）。「提高至145%」→ `1.45` |
| `aspd` | 数字 | 0 | 攻击速度 |
| `interval_flat` | 数字 | 0 | 攻击间隔的秒数修正。`-0.22` 表示缩短 0.22 秒 |
| `interval_mult` | 数字 | 1 | 攻击间隔倍率（乘算） |
| `hits_override` | 数字或 null | null | **覆盖**段数（「变为5连射」→ `5`） |
| `hits_mult` | 数字 | 1 | 段数倍率 |
| `hits_add` | 数字 | 0 | **额外**段数 |
| `targets` | 整数 | 1 | 同时攻击目标数 |
| `targets_scope` | 字符串或 null | null | 目标数是"非固定数量"时的范围，见下 |
| `damage_type` | 字符串或 null | null | 覆盖伤害类型（只对技能有意义） |
| `def_ignore_pct` | 数字 | 0 | 无视防御百分比 |
| `def_ignore` | 数字 | 0 | 无视防御固定值 |
| `res_ignore_pct` | 数字 | 0 | 无视法抗百分比 |
| `res_ignore` | 数字 | 0 | 无视法抗固定值 |
| `bonus_arts_pct` | 数字 | 0 | 附加法术伤害（占攻击力的百分比） |
| `bonus_true_pct` | 数字 | 0 | 附加真实伤害 |
| `dot_pct` | 数字 | 0 | 持续伤害 |
| `attacks` | 布尔 | true | `false` 表示技能期间**不进行普攻** |

`hits_override` 与 `hits_add` 差一倍：前者是"一共 5 段"，后者是"再加 5 段"。
「攻击变为5连射」属于前者。

`targets_scope` 的取值：

| 值 | 含义 |
|---|---|
| `all_blocked` | 阻挡的所有敌人 |
| `all_in_range` | 范围内的所有敌人 |

这类技能打几个取决于场上有多少敌人，引擎无从得知，会按单体 1 计算**并在结果里标注**。
想覆盖它就用 `--targets N`。

---

## 解析记录（带下划线）

引擎全部忽略，但**值得读**——它们是"这个数字是怎么来的"的答案。

| 字段 | 说明 |
|---|---|
| `_schema` | 格式版本，目前是 `arkdps/operator/1` |
| `_source` | 数据来源（wiki 名 + 页面名） |
| `_warnings` | 导入时的警告。**会在载入时进入 `Operator.note`**，`arkdps list` 里显示为 ⚠ |
| `_atk_from` | 面板攻击力取自哪一档精英阶段（低星干员没有精英 2） |
| `_parsed` | 逐条审计：哪句话变成了哪个字段、值是多少 |
| `_unparsed` | 认出来（或没认出来）但**没用上**的片段 |
| `_conditional` | 带条件的字段。**默认不参与计算** |
| `_conditions` | 条件说明（原文 + 命中的条件标记） |
| `_confidence` | `exact` / `partial` / `low` |
| `_needs_review` | 是否有"可能影响 DPS 但没解析出来"的片段 |

### `_unparsed` 里每一条的形状

```json
{
  "text": "第一天赋效果提升至3倍",
  "kind": "unknown",
  "affects_damage": true,
  "reason": "找到了属性词，但没能确定数值或关系"
}
```

`kind` 有三种：

| 值 | 含义 |
|---|---|
| `unknown` | 识别器**认不出**这句话 |
| `unmapped` | 认出了属性和数值，但**映射不到任何引擎字段** |
| `enemy_effect` | 说的是**敌人**的属性变化，不能套到干员身上 |

`affects_damage` 是**风险分级**，三种取值：

| 值 | 含义 |
|---|---|
| `true` | 可能影响算出来的 DPS——**要人看** |
| `false` | 已确认与 DPS 无关（例如部署费用、攻击范围） |
| `null` | 不确定——按"有风险"处理 |

只有 `affects_damage` 不是 `false` 的片段才会被算进 `_needs_review`。
把确定无关的东西排除掉，报告才不会被噪音淹没。

### `_confidence` 是怎么算出来的

| 值 | 条件 |
|---|---|
| `exact` | 所有子句都识别成功（或判定为无关） |
| `partial` | 有认不出来的子句，但都不影响 DPS |
| `low` | 有**可能影响 DPS** 却没解析出来的子句 |

---

## 手写一份草稿

最小可用的一份——不写任何下划线字段：

```json
{
  "name": "我的干员",
  "class": "近卫",
  "attack_interval": 1.2,
  "atk": 800.0,
  "talent": { "atk_pct": 15.0 },
  "skills": [
    {
      "name": "我的技能",
      "charge": "auto",
      "sp_cost": 30.0,
      "duration": 15.0,
      "effects": { "atk_pct": 100.0, "targets": 3 }
    }
  ]
}
```

放进 `data/operators/` 就能用：

```console
$ arkdps calc 我的干员 --def 500
```

想验证手写的数对不对，照着 [docs/FORMULAS.md](FORMULAS.md) 口算一遍即可。

---

## 容错与边界

- **不认识的键**一律忽略。所以加自己的扩展字段是安全的（建议加下划线前缀）
- **缺字段**用上表的缺省值填补。`atk` 缺失会算成 0——所以导入器会为此写一条
  `_warnings`，载入时进入 `note`，在 `arkdps list` 里显示成 ⚠
- **`charge` 不认识的值**兜底成 `auto`（宽容处理，避免一份草稿整个载入失败）
- **`damage_type` 不认识的值**直接抛 `ValueError`（不吞掉）
- **顶层不是对象的 JSON**、语法错误的 JSON：`load_operators` 会**跳过该文件**并继续，
  一份坏草稿不该让整个列表消失
