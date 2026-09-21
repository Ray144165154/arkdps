"""Wikitext 解析：从 PRTS 页面里取出结构化的模板字段。

PRTS 的干员页把数据放在**带命名参数的模板**里::

    {{属性
    |攻击速度=1.3s
    |精英2_满级_攻击=713
    |信赖加成_攻击=...
    }}

    {{技能
    |技能名=真银斩
    |技能类型1=自动回复
    |技能专精3消耗=90
    |技能专精3描述=防御力-70%，攻击力+200%，同时攻击至多6个目标
    }}

所以这一层的任务是：**按模板名找到模板、按字段名取值**，
并把描述里的嵌套模板展平成可读文本。

模块刻意不依赖任何第三方库——wikitext 的花括号配对用栈就能做，
不需要完整的解析器。
"""

from __future__ import annotations

import re

__all__ = [
    "split_templates",
    "template_fields",
    "flatten",
    "strip_templates",
    "clean_value",
]


def split_templates(text: str) -> list[tuple[str, str]]:
    """把文本里的模板切成 ``[(模板名, 模板体)]``。

    用**花括号配对**而不是正则，因为模板内部会嵌套模板::

        {{技能|技能专精3描述=攻击力提高至{{color|#0098DC|290%}}}}

    正则很难正确处理这种嵌套，深度计数则可以。

    ⚠️ 返回的是**所有**模板，**包括嵌套的那些**——上面这个例子会返回
    ``技能`` 与 ``color`` 两条，因为扫描在外层模板结束后不会跳过它的内部。

    这不是缺陷，但调用方必须自己按模板名过滤：``prts.py`` 里三处调用
    都是 ``if template_name == ...`` / ``!= ...``，所以内层模板
    （名字是 ``color`` / ``*`` 之类）不会被误当成干员页的顶层模板。
    若确实需要"只要最外层"，得自己再按花括号深度筛一次。
    """
    out: list[tuple[str, str]] = []
    i, n = 0, len(text)

    while i < n - 1:
        if text[i : i + 2] != "{{":
            i += 1
            continue

        depth, k, end = 0, i, None
        while k < n - 1:
            pair = text[k : k + 2]
            if pair == "{{":
                depth += 1
                k += 2
                continue
            if pair == "}}":
                depth -= 1
                k += 2
                if depth == 0:
                    end = k
                    break
                continue
            k += 1

        if end is None:  # 括号不配对，放弃剩余部分
            break

        inner = text[i + 2 : end - 2]
        match = re.match(r"\s*([^|\n}]+)", inner)
        out.append((match.group(1).strip() if match else "", inner))
        i += 2

    return out


def template_fields(body: str) -> dict[str, str]:
    """把模板体切成 ``{字段名: 值}``。

    值与字段名都可能跨行，所以按 ``\\n|`` 切分而不是按 ``|``——
    否则值里出现的 ``|``（例如嵌套模板 ``{{color|#0098DC|290%}}``）会把
    一个字段切成好几段。
    """
    fields: dict[str, str] = {}
    for chunk in re.split(r"\n\s*\|", "\n" + body)[1:]:
        if "=" not in chunk:
            continue
        key, _, value = chunk.partition("=")
        key = key.strip()
        if key:
            fields[key] = value.strip()
    return fields


def _template_display_value(inner: str) -> str:
    """从一个模板的内部文本里取「应该显示出来的内容」。

    不同模板的约定**不一样**，这是本项目踩过的一个坑：

    ``{{color|#0098DC|290%}}``
        没有命名参数，要的是**最后一段** → ``290%``

    ``{{修正|可部署单位|原文=我方单位|group=注}}``
        有命名参数（``k=v``），位置参数才是内容 → ``可部署单位``
        （取最后一段会得到 ``group=注`` 这种垃圾）

    ``{{*|5%|+5%}}``
        没有命名参数 → 最后一段 ``+5%``

    判据：**存在命名参数时取第一个位置参数，否则取最后一段**。
    """
    parts = [p for p in inner.split("|")]
    if len(parts) < 2:
        return ""

    body = parts[1:]  # 丢掉模板名
    positional = [p for p in body if "=" not in p]
    if len(positional) < len(body) and positional:
        return positional[0]
    return body[-1] if body else ""


def strip_templates(text: str) -> str:
    """把所有模板替换成它们「显示出来的内容」，从内到外反复展开。"""
    previous = None
    while previous != text:
        previous = text
        text = re.sub(
            r"\{\{([^{}]*)\}\}",
            lambda m: _template_display_value(m.group(1)),
            text,
        )
    return text


def flatten(text: str) -> str:
    """把 wikitext 展平成可读的中文文本。

    只做**与语义有关**的清理：

      * 模板 → 它的显示内容
      * ``<br>`` → 逗号（技能描述里它就是分隔作用）
      * ``<ref>…</ref>`` 脚注**连正文一起**删掉——那些字是给读者看的注解，
        不是技能效果的一部分
      * 其余 HTML 标签、粗体标记、管道链接 → 去掉外壳留文字
      * 参考资料角标 ``[1]`` → 去掉（是引用，不是内容）

    刻意**不做**的事：不改写措辞、不统一同义词。
    规范化是 :mod:`arkdps.recognizer` 的职责，这里只负责"把壳剥掉"。
    """
    text = re.sub(r"<br\s*/?>", "，", text, flags=re.I)
    text = strip_templates(text)

    # ⚠️ 顺序要紧：先把 <ref>…</ref> **整块**删掉，再删其它标签。
    # 反过来的话，下面的 `<[^>]+>` 会先拿走 <ref> 标签本身，只剩下光秃秃的
    # 脚注正文，整块删除的正则就永远匹配不到——脚注文字会混进技能描述，
    # 被识别器当成效果去解析。
    text = re.sub(r"<ref[^>]*>.*?</ref>", "", text, flags=re.S)
    text = re.sub(r"<ref[^>]*/>", "", text)

    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"'''?", "", text)                     # 粗体/斜体
    text = re.sub(r"\[\[([^\]|]*\|)?([^\]]*)\]\]", r"\2", text)  # [[链接|文字]]
    text = re.sub(r"\[\d+\]", "", text)                  # 引用角标
    text = text.replace("&nbsp;", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def clean_value(value: str) -> str:
    """清理单个字段值（用于数值型字段）。

    数值字段里通常没有模板，但可能有单位后缀或空白，
    例如 ``"90"``、``"30s"``、``"1.3s"``。
    """
    value = strip_templates(value)
    value = re.sub(r"<[^>]+>", "", value)
    return value.strip()
