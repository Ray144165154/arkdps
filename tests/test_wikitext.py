"""wikitext 解析测试：模板切分、字段取值、展平。

``arkdps.importers.wikitext`` 是导入器的第一道关：PRTS 页面上的数据全都写在
``{{模板|字段=值}}`` 里，这一层取错一段文本**不会报错**，只会让后面的 DPS
悄悄偏低（丢一个天赋、少一条攻击加成）。所以下面每个用例盯住的都是
"取出来的到底是哪一段字符"，而不是"有没有取到东西"。

可核对性：所有输入都写成字面量，期望值也全部写出来。读的人应该能盯着输入串
自己数出 ``|`` 与 ``=`` 的位置，判断期望值对不对，而不需要相信作者。

带「回归」字样的用例都是真实踩过的坑，docstring 里说明坑在哪。

### 一处曾经的源码缺陷与一处文档失实

写本文件时发现两个问题，都不在这里"就地改掉"而是先记录下来，
再由源码侧处理，最后把断言改成普通断言：

1. ``flatten`` 里 ``<ref>…</ref>`` 整块删除是**死代码**（前面的
   ``<[^>]+>`` 已经把标签删了），脚注正文会漏进技能描述。
   → **已修源码**（把整块删除挪到删标签之前），用例
   :meth:`TestFlatten.test_reference_block_content_is_removed` 现在锁死它。
2. ``split_templates`` 的 docstring 写"顶层模板"，实现却把嵌套模板也返回。
   查过调用方后确认**实现是对的、文档是错的**（``prts.py`` 三处都按模板名
   精确过滤），于是**改文档**，用例改成如实记录
   :meth:`TestSplitTemplates.test_nested_templates_are_also_returned`。

### 一处有意留空的地方

``_template_display_value`` 在"只有命名参数、没有位置参数"时
（例如 ``{{修正|原文=我方单位}}``）会返回原文 ``'原文=我方单位'``。
这算不算泄漏，"没有位置参数时该显示什么"文档没定义，语料里也没有实例，
属于**歧义而非确证缺陷**——按本项目"宁可标出来也不猜"的原则留空不测。
"""

from __future__ import annotations

import unittest

from fixtures import operator  # noqa: F401  （导入以设定 sys.path）
from fixtures import multi_talent_wikitext, sample_wikitext

from arkdps.importers import wikitext
from arkdps.importers.wikitext import (
    _template_display_value,
    clean_value,
    flatten,
    split_templates,
    strip_templates,
    template_fields,
)


class TestSplitTemplates(unittest.TestCase):
    """``split_templates``：把页面切成 ``[(模板名, 模板体)]``。

    为什么值得单独测：它是所有上游数据的第一道筛子。它少切一个模板，
    「资质凭证」「属性」「天赋列表3」里的数据就凭空消失；
    它多切/切错一段，字段名就会串到别的模板上去。
    """

    def test_two_sibling_templates(self):
        """``{{A}}{{B}}`` 是两个模板，不是一个——配对不能跨过 ``}}{{``。"""
        self.assertEqual(split_templates("{{A}}{{B}}"), [("A", "A"), ("B", "B")])
        self.assertEqual(
            split_templates("{{A|1}}{{B|2}}{{C|3}}"),
            [("A", "A|1"), ("B", "B|2"), ("C", "C|3")],
        )

    def test_nested_template_body_is_complete(self):
        """回归：嵌套模板必须按**花括号深度**配对，否则外层模板体会被截断。

        输入 ``{{属性|攻击力={{color|#00B0FF|+50}}}}`` 里有 4 个 ``}}``：
        如果按"第一个 ``}}`` 就算结束"来切，外层的模板体会变成
        ``属性|攻击力={{color|#00B0FF|+50``——内层 ``}}`` 被当成了外层结尾。
        正确的期望值是外层模板体**一字不差**地带着整个内层模板一起返回。

        文档字符串明确写了这里要的是"顶层模板"，所以下面只检查第一个条目
        （即最外层模板）的内容；"嵌套模板会不会被额外返回"见
        :meth:`test_nested_templates_are_not_returned_separately`。
        """
        cases = [
            (
                "{{属性|攻击力={{color|#00B0FF|+50}}}}",
                ("属性", "属性|攻击力={{color|#00B0FF|+50}}"),
            ),
            (
                "{{技能\n|技能专精3描述=攻击力提高至{{color|#0098DC|290%}}\n}}",
                ("技能", "技能\n|技能专精3描述=攻击力提高至{{color|#0098DC|290%}}\n"),
            ),
        ]
        for text, expected_first in cases:
            with self.subTest(text=text):
                self.assertEqual(split_templates(text)[0], expected_first)

    def test_nested_templates_are_also_returned(self):
        """``split_templates`` 返回的是**所有**模板，嵌套的那些也算在内。

        这一条曾经和 docstring 不一致（文档写"顶层"，实现却从外层模板
        内部继续扫描）。查过两个调用方后确认**行为是对的、文档是错的**：

            prts.py 三处调用都按模板名精确过滤
            （``if template_name == name`` / ``in names`` / ``!= "天赋列表3"``），

        所以内层模板（名字是 ``color`` / ``*`` 之类）不会被误当成顶层模板，
        而"把所有模板都列出来"对调用方更有用。

        于是这里锁定的契约是：**外层模板体一字不差地包含嵌套模板，
        内层模板也会单独出现一条**。
        """
        result = split_templates("{{属性|攻击力={{color|#00B0FF|+50}}}}")

        self.assertEqual(result[0][0], "属性")
        self.assertEqual(result[0][1], "属性|攻击力={{color|#00B0FF|+50}}")

        self.assertIn(
            ("color", "color|#00B0FF|+50"), result,
            "内层模板也会作为独立条目返回——调用方必须自己按模板名过滤",
        )

    def test_prose_between_templates_is_ignored(self):
        """模板之间的散文属于正文，不该混进模板条目里。"""
        self.assertEqual(
            split_templates("前面 {{A|1}} 中间 {{B|2}} 后面"),
            [("A", "A|1"), ("B", "B|2")],
        )

    def test_template_name_is_stripped(self):
        """回归：PRTS 的模板名后面紧跟换行，名字要 ``strip`` 后才能比对。

        ``{{ 属性\\n|...}}`` 这种写法在真实页面上很常见；名字带空格或换行
        就匹配不上 ``== "属性"``，整块属性数据会静默消失。
        模板体保持原样（不 strip），因为字段切分依赖里面的 ``\\n|``。
        """
        self.assertEqual(
            split_templates("{{ 属性\n|攻击速度=1.3s\n}}"),
            [("属性", " 属性\n|攻击速度=1.3s\n")],
        )

    def test_unbalanced_tail_does_not_destroy_earlier_templates(self):
        """回归：括号不配对的**尾部**不能把前面的模板一起吞掉。

        页面被截断时（例如抓取中断）最后一个模板可能只有 ``{{`` 没有 ``}}``。
        这时前面完整的模板必须照常返回——否则一次截断会让整页数据全丢。
        """
        self.assertEqual(
            split_templates("{{A|1}} {{B|2}} {{C"),
            [("A", "A|1"), ("B", "B|2")],
        )

    def test_unbalanced_braces_never_raise(self):
        """回归：花括号数量对不上的各种输入都不能抛异常（栈只能少不能溢）。

        导入器面对的是"网页文本"，脏数据是常态；这里只要求"不崩、返回列表"。
        """
        broken = [
            "",
            "{{",
            "{{{{",
            "{{A|1",
            "}}}}",
            "}{{",
            "{{A|1}} {{B",
            "文字 }} {{A|1}}",
        ]
        for text in broken:
            with self.subTest(text=text):
                result = split_templates(text)
                self.assertIsInstance(result, list)
                for name, body in result:
                    self.assertIsInstance(name, str)
                    self.assertIsInstance(body, str)
        self.assertEqual(split_templates("文字 }} {{A|1}}"), [("A", "A|1")])
        self.assertEqual(split_templates("{{A|1}} {{B"), [("A", "A|1")])

    def test_broken_open_brace_before_a_template_drops_the_rest(self):
        """**按设计**：多出来的 ``{{`` 在有效模板前面时，后面的部分整体放弃。

        源码里对应 ``if end is None:  # 括号不配对，放弃剩余部分 / break``。
        这不是缺陷、是被写进 docstring 的取舍（继续往下切反而会切出更离谱的
        字段），所以这里如实断言，免得以后有人"顺手"改成别的行为。
        """
        self.assertEqual(split_templates("{{C {{A|1}}"), [])

    def test_single_braces_are_not_templates(self):
        """``{A}``（单括号）和孤立的 ``}}`` 都不是模板。"""
        self.assertEqual(split_templates("{A} {B}}"), [])


class TestTemplateFields(unittest.TestCase):
    """``template_fields``：把模板体切成 ``{字段名: 值}``。

    为什么值得单独测：字段名一旦切错，取到的值就是**另一个字段**的值——
    类型正确、数值离谱，是最难发现的一类错误。
    """

    def setUp(self):
        """把反复用到的模板体放在这里。

        这些字符串会被好几个用例共用，放一处是为了让读者**只核对一次**
        就够；每个用例的 docstring 里再指出它关心的那一段。
        """
        # 与 PRTS「属性」模板同构：第一行是模板名，之后每行一个字段
        self.stats_body = (
            "属性\n"
            "|攻击速度=1.3s\n"
            "|精英2_满级_攻击=713\n"
            "|信赖加成_攻击=50\n"
        )
        # 字段值里带嵌套模板，且嵌套模板自己也有 pipe
        self.charinfo_body = (
            "CharinfoV2\n"
            "|干员名=测试干员\n"
            "|特性=可以攻击{{修正|可部署单位|原文=我方单位|group=注}}\n"
            "|职业=近卫\n"
        )

    def test_operator_stats_body(self):
        """常规多行模板：三个字段的值就是 ``=`` 后面的原文。"""
        self.assertEqual(
            template_fields(self.stats_body),
            {"攻击速度": "1.3s", "精英2_满级_攻击": "713", "信赖加成_攻击": "50"},
        )

    def test_template_name_line_is_not_a_field(self):
        """模板名那一行不是字段（它没有 ``=``），不能出现在结果里。"""
        fields = template_fields(self.stats_body)
        self.assertNotIn("属性", fields)
        self.assertEqual(len(fields), 3)

    def test_value_containing_pipes_stays_one_field(self):
        """回归：值里的 ``|``（来自嵌套模板）不能把字段切成几段。

        值 ``可以攻击{{修正|可部署单位|原文=我方单位|group=注}}`` 里有 3 个
        ``|``。按 ``\\n|`` 切分时它们都属于同一个字段，所以「特性」的完整值
        就是上面那一段原文——也正是这个原因，源码才用 ``\\n\\s*\\|`` 而不是
        ``|`` 做分隔符。
        """
        self.assertEqual(
            template_fields(self.charinfo_body),
            {
                "干员名": "测试干员",
                "特性": "可以攻击{{修正|可部署单位|原文=我方单位|group=注}}",
                "职业": "近卫",
            },
        )

    def test_whitespace_around_key_and_value_is_trimmed(self):
        """``= `` 后面的空格、行尾空格都不该进到值里，否则 ``_to_float`` 要多吃字符。"""
        self.assertEqual(
            template_fields("属性\n|攻击速度= 1.3s \n|精英2_满级_攻击=713\n"),
            {"攻击速度": "1.3s", "精英2_满级_攻击": "713"},
        )

    def test_key_without_equals_is_ignored(self):
        """没有 ``=`` 的行（位置参数）不是字段，直接跳过、也不影响别的字段。"""
        self.assertEqual(template_fields("T\n|裸位置参数\n|a=1\n"), {"a": "1"})

    def test_positional_only_single_line_body_yields_no_fields(self):
        """**按设计**：单行模板体（如 ``属性|攻击速度=1.3s``）取不到字段。

        源码用 ``\\n|`` 切分，是为了让值里的 ``|`` 不切碎字段；代价就是
        参数全部写在一行、中间只有 ``|`` 的模板（``{{*|12|+12}}`` 这类）
        本来就不该走这个函数——它们的内容该由 :func:`strip_templates` /
        :func:`_template_display_value` 取。这里如实记录这个取舍。
        """
        self.assertEqual(template_fields("属性|攻击速度=1.3s"), {})


class TestTemplateDisplayValue(unittest.TestCase):
    """``_template_display_value``：从一个模板的内部文本里取"显示出来的内容"。

    为什么值得单独测：不同 wiki 模板的约定不一样，取错一段就会把
    ``group=注`` 这种**参数名**当成内容写进中文描述，再喂给识别器。
    这是本项目踩过的坑，也是本文件里最重要的回归点。
    """

    def test_without_named_parameter_the_last_segment_wins(self):
        """**分支一**：没有命名参数时，显示内容是**最后一段**。

        ``color`` 模板的约定是"前几段是参数（颜色等），最后一段才是正文"：
        ``{{color|#0098DC|290%}}`` 要的是 ``290%``；
        ``{{color|#0098DC|#FF0000|290%}}`` 同样要 ``290%``（多一段颜色也不变）。
        """
        cases = [
            ("color|#0098DC|290%", "290%"),
            ("color|#0098DC|#FF0000|290%", "290%"),
            ("color|#0098DC|攻击力提升", "攻击力提升"),
        ]
        for inner, expected in cases:
            with self.subTest(inner=inner):
                self.assertEqual(_template_display_value(inner), expected)

    def test_named_parameter_makes_the_first_positional_win(self):
        """**分支二（回归）**：只要出现命名参数（``k=v``），内容就是**第一个位置参数**。

        修的是这个坑：``{{修正|可部署单位|原文=我方单位|group=注}}`` 里
        位置参数 ``可部署单位`` 才是正文，``原文`` / ``group`` 是给编辑器看的。
        旧实现一律取最后一段，于是 ``group=注`` 被当成正文塞进中文描述——
        输出变成「可以攻击group=注」，识别器只会把它当废话丢掉。
        两条 ``group`` 在末尾的输入都必须取到 ``可部署单位``。
        """
        cases = [
            "修正|可部署单位|原文=我方单位|group=注",
            "修正|可部署单位|原文=我方单位",
            "修正|可部署单位|group=注",
        ]
        for inner in cases:
            with self.subTest(inner=inner):
                value = _template_display_value(inner)
                self.assertEqual(value, "可部署单位")
                self.assertNotIn("=", value)

    def test_diff_idiom_returns_the_displayed_value(self):
        """回归：wiki 的「diff」写法 ``{{*|旧值|新值}}`` 要取到**新值**。

        天赋里写 ``攻击速度{{*|12|+12}}``，``12`` 是原始值、``+12`` 才是页面上
        显示给玩家看的那一段。取错成 ``12`` 不会报错，只是 DPS 里少了一个 ``+``
        （正值可以靠数字对上，负值 ``-12`` 就会把符号弄反）。
        """
        self.assertEqual(_template_display_value("*|12|+12"), "+12")
        self.assertEqual(_template_display_value("*|6|+6"), "+6")
        self.assertEqual(_template_display_value("*|12|-12"), "-12")

    def test_single_segment_has_no_display_value(self):
        """只有模板名、没有内容的输入返回空串（不能把模板名当内容）。"""
        self.assertEqual(_template_display_value("coloronly"), "")
        self.assertEqual(_template_display_value(""), "")
        self.assertEqual(_template_display_value("color|"), "")


class TestStripTemplates(unittest.TestCase):
    """``strip_templates``：把模板换成它显示的内容，从内到外反复展开。

    为什么值得单独测：它是"剥离外壳"这一步的实现，剥多剥少都会直接
    改变进入识别器的中文句子。
    """

    def test_prose_around_template_survives(self):
        """回归：模板两侧的散文必须原样保留，只换掉模板本身。

        ``攻击力{{*|12|+12}}提升`` 里 ``攻击力`` 与 ``提升`` 是正文，
        只把 ``{{*|12|+12}}`` 换成 ``+12``，结果恰好是通顺的「攻击力+12提升」。
        """
        self.assertEqual(strip_templates("攻击力{{*|12|+12}}提升"), "攻击力+12提升")

    def test_multiple_templates_in_one_pass(self):
        """一行里多个模板都要处理，且各自按自己的约定取值。"""
        self.assertEqual(
            strip_templates("A{{*|1|+1}}B{{color|#000|二}}C"),
            "A+1B二C",
        )

    def test_nested_templates_expand_inside_out(self):
        """嵌套模板要**从内到外**展开：正则是 ``\\{\\{([^{}]*)\\}\\}``，只吃得下最内层。

        第一轮把 ``{{b|290%}}``（最内层，内部没有花括号）换成 ``290%``，
        字符串变成 ``攻击{{color|#0098DC|是290%哦}}``；第二轮再换外层。
        结果 ``攻击是290%哦`` 正好说明展开顺序是对的。
        """
        self.assertEqual(
            strip_templates("攻击{{color|#0098DC|是{{b|290%}}哦}}"),
            "攻击是290%哦",
        )

    def test_unbalanced_braces_are_left_alone(self):
        """回归：括号不配对的模板不匹配、不处理，但**绝不能崩**，也不能吃掉正文。"""
        self.assertEqual(strip_templates("攻击力{{A|1 提升"), "攻击力{{A|1 提升")
        self.assertEqual(strip_templates("攻击力{{A|1</ref"), "攻击力{{A|1</ref")


class TestFlatten(unittest.TestCase):
    """``flatten``：把 wikitext 剥成可读中文（喂给 :mod:`arkdps.recognizer`）。

    为什么值得单独测：识别器只认中文句子。这里少去一个 ``<br>``、多留一个
    ``'''``，子句就可能切不开，效果被整条丢掉。
    """

    def test_template_becomes_its_display_value(self):
        """模板 → 显示内容；两条分支各举一例（详见 ``_template_display_value`` 的用例）。"""
        cases = [
            ("攻击力提高至{{color|#0098DC|290%}}", "攻击力提高至290%"),
            ("攻击力{{修正|可部署单位|原文=我方单位|group=注}}", "攻击力可部署单位"),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(flatten(text), expected)

    def test_named_parameter_does_not_leak_into_text(self):
        """回归（端到端）：``flatten`` 的输出里不能出现 ``k=v`` 形式的参数名。

        这是「``group=注`` 泄漏」在最终文本上的表现，也是识别器实际看到的字符串。
        """
        text = flatten("可以攻击{{修正|可部署单位|原文=我方单位|group=注}}")
        self.assertEqual(text, "可以攻击可部署单位")
        self.assertNotIn("group", text)
        self.assertNotIn("原文", text)

    def test_br_becomes_comma(self):
        """``<br>`` 在技能/特性描述里就是分隔作用，换成逗号才能被子句切分器切开。

        ``<br/>`` 与 ``<br>`` 两种写法都要处理（大小写也不敏感）。
        """
        self.assertEqual(
            flatten("攻击力提升<br/>攻速提升<br>目标数+1"),
            "攻击力提升，攻速提升，目标数+1",
        )
        self.assertEqual(flatten("A<BR>B"), "A，B")

    def test_html_tags_bold_and_links_are_stripped(self):
        """HTML 标签、粗体/斜体标记、管道链接：只剥壳，保留文字。"""
        cases = [
            ("'''攻击力'''提升", "攻击力提升"),
            ("''斜体''字", "斜体字"),
            ("[[真银斩|技能]]提升", "技能提升"),
            ("[[技能]]提升", "技能提升"),
            ("攻击力<span>提升</span>", "攻击力提升"),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(flatten(text), expected)

    def test_reference_marker_is_removed(self):
        """``[1]`` 是引用角标，不是内容——留着它会把子句切成两半。"""
        self.assertEqual(flatten("攻击力提升[1]"), "攻击力提升")
        self.assertEqual(flatten("攻击力提升[12]，攻速提升[3]"), "攻击力提升，攻速提升")

    def test_reference_block_content_is_removed(self):
        """回归：``<ref>…</ref>`` 必须**连正文一起**删掉，不能只删标签。

        曾经的顺序是先 ``<[^>]+>`` 删所有 HTML 标签、再删参考资料块。
        第一步跑完之后 ``<ref>`` 标签本身已经不存在了，第二步的正则
        永远匹配不到（死代码），于是脚注**正文**留在了描述里。

        真实 PRTS 页面里就有这种脚注::

            <ref name="模组">游戏内描述和实际效果如此，……</ref>

        这些字会进入识别器被当成技能效果去解析。角标 ``[1]`` 被去掉了、
        脚注正文却留着，本身就说明本意是整块删掉。
        """
        self.assertEqual(
            flatten('攻击力提升<ref name="x">出处文字[1]</ref>结束'),
            "攻击力提升结束",
        )

    def test_self_closing_reference_is_removed(self):
        self.assertEqual(flatten('攻击力提升<ref name="x"/>结束'), "攻击力提升结束")

    def test_multiline_reference_block_is_removed(self):
        """脚注常跨行，所以整块匹配必须开 ``re.S``。"""
        self.assertEqual(
            flatten("攻击力提升<ref>第一行\n第二行</ref>结束"),
            "攻击力提升结束",
        )

    def test_nbsp_and_whitespace_are_normalised(self):
        """``&nbsp;`` 和换行/连续空白都要归一：识别器按逗号切子句，多余空白的意义为零。"""
        self.assertEqual(flatten("攻击力&nbsp;提升"), "攻击力 提升")
        self.assertEqual(flatten("  攻击力\n提升  "), "攻击力 提升")

    def test_unbalanced_braces_do_not_raise(self):
        """回归：括号不配对的模板在 ``flatten`` 里原样留下即可，但绝不能抛异常。

        ``strip_templates`` 用的是整对匹配，配不上的 ``{{A|1`` 直接留在文本里——
        宁可留着丑，也不能让整条描述丢掉。
        """
        self.assertEqual(flatten("攻击力{{A|1 提升"), "攻击力{{A|1 提升")


class TestCleanValue(unittest.TestCase):
    """``clean_value``：清理单个**数值**字段（如 ``攻击速度`` / ``精英2_满级_攻击``）。

    为什么值得单独测：它的输出会被 ``_to_float`` 拿去正则取数字，
    多余字符会让取值从"能解析"变成"解析出另一个数"。
    """

    def test_number_and_unit_suffix(self):
        """数值字段的两种常见形态：纯数字、带单位后缀。"""
        cases = [("  90  ", "90"), ("1.3s", "1.3s"), ("713", "713")]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(clean_value(raw), expected)

    def test_template_collapses_to_display_value(self):
        """值里带模板时换成显示内容：``{{*|12|+12}}`` → ``+12``（不是 ``12``）。"""
        self.assertEqual(clean_value("{{*|12|+12}}"), "+12")

    def test_html_tag_is_removed(self):
        """标签会被删掉、文字留下（这里 ``<br>`` 后面本来就没有文字）。"""
        self.assertEqual(clean_value("713<br>"), "713")


class TestSyntheticPageIntegration(unittest.TestCase):
    """用 ``fixtures`` 里的合成干员页做一次"页面 → 字段 → 中文"的串联合并测试。

    为什么值得单独测：前面每个函数都对，串起来仍可能不对（例如字段值里带的
    模板没被展开）。这里走的是 ``prts.py`` 完全相同的调用顺序：
    ``split_templates`` 找模板 → ``template_fields`` 取字段 → ``flatten`` 展平。
    """

    def test_charinfo_fields_and_trait(self):
        """修正模板嵌在字段值里时，取到的字段值保留原文、展平后只剩正文。"""
        page = sample_wikitext(attack=713, trait="可以攻击{{修正|可部署单位|原文=我方单位|group=注}}")

        charinfo = next(body for name, body in split_templates(page) if name == "CharinfoV2")
        fields = template_fields(charinfo)

        self.assertEqual(fields["干员名"], "测试干员")
        self.assertEqual(fields["职业"], "近卫")
        self.assertEqual(fields["特性"], "可以攻击{{修正|可部署单位|原文=我方单位|group=注}}")
        self.assertEqual(flatten(fields["特性"]), "可以攻击可部署单位")

    def test_stats_block_is_reachable(self):
        """「属性」块里的字段名与数值必须原样取到（DPS 的输入就是这几个数）。"""
        page = sample_wikitext(attack=713)
        stats = next(body for name, body in split_templates(page) if name == "属性")
        fields = template_fields(stats)

        self.assertEqual(fields["精英2_满级_攻击"], "713")
        self.assertEqual(fields["攻击速度"], "1.3s")
        self.assertEqual(fields["信赖加成_攻击"], "50")

    def test_two_talent_blocks_are_both_returned(self):
        """回归：页面上**两个** ``天赋列表3`` 都要能拿到，且天赋里的 diff 模板取到新值。

        双天赋干员（如能天使）丢掉第二个天赋，DPS 会悄悄偏低。这里同时锁住
        两件事：模板能被找全（2 个），以及 ``{{*|12|+12}}`` 展平成 ``+12``。
        """
        page = multi_talent_wikitext()
        blocks = [body for name, body in split_templates(page) if name == "天赋列表3"]
        self.assertEqual(len(blocks), 2)

        first, second = (template_fields(block) for block in blocks)
        self.assertEqual(first["天赋2"], "快速弹匣")
        self.assertEqual(flatten(first["天赋2效果"]), "攻击速度+12")
        self.assertEqual(second["天赋1"], "天使的祝福")
        self.assertEqual(flatten(second["天赋1效果"]), "攻击力+6%，生命上限+10%")


class TestPublicApi(unittest.TestCase):
    """``__all__`` 是给 ``from wikitext import *`` 用的对外契约。

    为什么值得单独测：``prts.py`` 正是从这些名字里挑函数的，某个名字从
    ``__all__`` 掉出去，导入器会在别处以 ``ImportError``/``NameError`` 的形式炸。
    """

    def test_dunder_all_exports_the_expected_names(self):
        self.assertEqual(
            sorted(wikitext.__all__),
            ["clean_value", "flatten", "split_templates", "strip_templates", "template_fields"],
        )

    def test_exported_names_exist_and_are_callable(self):
        for name in wikitext.__all__:
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(wikitext, name)))


if __name__ == "__main__":
    unittest.main()
