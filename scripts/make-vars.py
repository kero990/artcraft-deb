#!/usr/bin/env python3
"""生成 templates/*.in 渲染用的 vars.json。

读取 appcfg.py 导出的环境变量（APP_*）以及打包脚本设置的 PACKAGE_* / INSTALLED_SIZE。
用法: make-vars.py <输出的 vars.json>
"""
import json
import os
import sys


def main(argv=None):
    if len(sys.argv) != 2:
        sys.exit("用法: make-vars.py <输出的 vars.json>")
    desc_path = os.environ["APP_DESCRIPTION_FILE"]
    desc = open(desc_path, encoding="utf-8").read().strip().splitlines()
    if not desc:
        sys.exit("ERROR: %s 是空的" % desc_path)
    cli = os.environ.get("APP_CLI", "")
    app_id = os.environ["APP_ID"]
    if cli:
        install_note = ("程序本体安装在 /opt/apps/%s 下，%s 由 postinst 脚本链接到 /usr/bin，"
                        "卸载时由 postrm 脚本清理。" % (app_id, cli))
    else:
        install_note = "程序本体安装在 /opt/apps/%s 下。" % app_id

    vars_ = {
        "APP_ID": app_id,
        "NAME": os.environ["APP_NAME"],
        "VERSION": os.environ["PACKAGE_VERSION"],
        "ARCH": os.environ["PACKAGE_ARCH"],
        "SIZE": os.environ.get("INSTALLED_SIZE", "0"),
        "SECTION": os.environ["APP_SECTION"],
        "HOMEPAGE": os.environ["APP_HOMEPAGE"],
        "SUMMARY": desc[0].strip(),
        "DESCRIPTION": "\n".join(desc[1:]),
        "INSTALL_NOTE": install_note,
        "CLI": cli,
        "PERMISSIONS": json.load(open(os.environ["APP_PERMISSIONS_FILE"], encoding="utf-8")),
    }
    with open(sys.argv[1], "w", encoding="utf-8") as fh:
        json.dump(vars_, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
