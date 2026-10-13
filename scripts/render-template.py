#!/usr/bin/env python3
"""模板渲染：把 templates/*.in 里的 {{KEY}} 换成实际值。

用法:
  render-template.py <template> <output> --vars-file vars.json [--indent DESCRIPTION]

规则:
  - {{KEY}} 全量替换；未知 KEY 直接报错（避免把占位符漏进 deb）
  - vars.json 里值为 dict 的键（如 permissions）会被渲染成缩进后的 JSON 片段
  - --indent 指定的键（deb 长描述）会逐行加一个空格，空行渲染成 " ."
"""
from __future__ import annotations

import argparse
import json
import re
import sys

PLACEHOLDER = re.compile(r"\{\{([A-Z0-9_]+)\}\}")


def render_value(key, value, indent_keys):
    if isinstance(value, dict):
        inner = json.dumps(value, ensure_ascii=False, indent=4).splitlines()[1:-1]
        return "\n".join("        " + line.strip() for line in inner)
    text = str(value)
    if key in indent_keys:
        lines = [ln.rstrip() for ln in text.splitlines()]
        while lines and not lines[-1]:
            lines.pop()
        return "\n".join((" ." if not ln.strip() else " " + ln) for ln in lines)
    return text


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("template")
    parser.add_argument("output")
    parser.add_argument("--vars-file", required=True)
    parser.add_argument("--indent", action="append", default=[],
                        help="需要整体缩进的键（可多次，如 --indent DESCRIPTION）")
    args = parser.parse_args(argv)

    vars_ = json.load(open(args.vars_file, encoding="utf-8"))
    text = open(args.template, encoding="utf-8").read()

    unknown = [m.group(1) for m in PLACEHOLDER.finditer(text) if m.group(1) not in vars_]
    if unknown:
        sys.exit("ERROR: 模板 %s 缺少变量: %s" % (args.template, ", ".join(sorted(set(unknown)))))

    def sub(match):
        return render_value(match.group(1), vars_[match.group(1)], args.indent)

    out = PLACEHOLDER.sub(sub, text)
    with open(args.output, "w", encoding="utf-8") as fh:
        fh.write(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
