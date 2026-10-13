#!/usr/bin/env python3
"""读取 apps/<app>.yml，把打包配置暴露给 CI 和本地脚本。

用法:
  appcfg.py --list                       列出所有 app
  appcfg.py <app> --json                 输出合并默认值后的完整配置 (JSON)
  appcfg.py <app> --env <file>           写出可被 `source` 的 shell 变量文件
                                        （同时写入描述/权限两个附属文件）
  appcfg.py <app> --cargo-packages       输出 cargo 包参数，如 "-p x -p x-cli"

配置项 (apps/<app>.yml) 与默认值:
  name          显示名              必填
  repo          上游 GitHub 仓库     必填，如 storytold/photocraft
  appid         deepin 应用 ID      必填，如 ai.storyteller.photocraft
  slug          包名/二进制基名      必填
  homepage      主页地址            默认 https://getartcraft.com/apps/<slug>
  section       deb 分类            默认 graphics
  features      cargo --features     默认 []
  extra_deps    额外的 apt 包        默认 []
  permissions   deepin info 权限覆盖  默认见 DEFAULT_PERMISSIONS
  description   deb 长描述          必填
  cargo_pkgs    要编译的 cargo 包    默认 [slug, slug-cli]
  binaries      装进 files/ 的二进制 默认 [slug, slug-cli]
  cli           链接到 /usr/bin 的工具 默认 slug-cli（设为 null 表示不装）
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import sys

APPS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "apps")

DEFAULT_PERMISSIONS = {
    "autostart": False,
    "notification": False,
    "trayicon": False,
    "clipboard": True,
    "account": False,
    "bluetooth": False,
    "camera": False,
    "audio_record": False,
    "installed_apps": True,
}

REQUIRED = ("name", "repo", "appid", "slug", "description")


def app_names():
    if not os.path.isdir(APPS_DIR):
        return []
    return sorted(f[:-4] for f in os.listdir(APPS_DIR) if f.endswith(".yml"))


def _scalar(text):
    t = text.strip()
    if t == "{}":
        return {}
    if t.startswith("[") and t.endswith("]"):
        inner = t[1:-1].strip()
        return [item.strip().strip("\"'") for item in inner.split(",")] if inner else []
    if t in ("true", "True"):
        return True
    if t in ("false", "False"):
        return False
    if t in ("null", "~", ""):
        return None
    return t.strip("\"'")


def parse_app_config(fp):
    """解析 apps/<app>.yml 用到的 YAML 子集，不依赖 PyYAML。

    为什么不依赖 PyYAML：CI 容器 python:3.11-buster 里的 python3 是
    /usr/local/bin/python3（3.11），而 apt 装的 python3-yaml 只给系统 python3.7
    用，装了也 import 不到（首次全量跑 12 个作业全死在这上面）。

    支持的语法（也就是 apps/*.yml 实际用到的全部语法）：
      - 顶层 `key: value`、`key: []`、`key: {}`
      - `key: |` 块标量（后续 2 空格缩进的行整体作为值）
      - 2 空格缩进的二级映射（permissions）
      - 整行注释与空行
    """
    lines = fp.read().splitlines()
    data, i, n = {}, 0, len(lines)
    while i < n:
        raw = lines[i]
        i += 1
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw[:1] in (" ", "\t"):
            sys.exit("ERROR: apps/*.yml 第 %d 行缩进意外: %r" % (i, raw))
        if ":" not in raw:
            sys.exit("ERROR: apps/*.yml 第 %d 行不是 key: value: %r" % (i, raw))
        key, _, rest = raw.partition(":")
        key, rest = key.strip(), rest.strip()
        if rest == "|":                      # 块标量
            block = []
            while i < n:
                nxt = lines[i]
                if not nxt.strip():
                    block.append("")
                elif nxt.startswith("  "):
                    block.append(nxt[2:])
                else:
                    break
                i += 1
            while block and not block[-1]:
                block.pop()
            data[key] = "\n".join(block) + "\n"
        elif rest == "":                     # 二级映射
            sub = {}
            while i < n:
                nxt = lines[i]
                if not nxt.strip() or nxt.lstrip().startswith("#"):
                    i += 1
                    continue
                if not nxt.startswith("  "):
                    break
                if nxt.strip() == "{}":      # `permissions: {}`（空映射）
                    i += 1
                    break
                sk, _, sv = nxt.strip().partition(":")
                sub[sk.strip()] = _scalar(sv)
                i += 1
            data[key] = sub
        else:
            data[key] = _scalar(rest)
    return data


def load_app(app):
    path = os.path.join(APPS_DIR, app + ".yml")
    if not os.path.isfile(path):
        sys.exit("ERROR: 未找到 app 配置 %s（可用: %s）" % (path, ", ".join(app_names())))
    cfg = parse_app_config(open(path, encoding="utf-8"))
    cfg.setdefault("slug", app)
    cfg.setdefault("name", cfg["slug"].capitalize())
    cfg.setdefault("homepage", "https://getartcraft.com/apps/%s" % cfg["slug"])
    cfg.setdefault("section", "graphics")
    cfg.setdefault("features", [])
    cfg.setdefault("extra_deps", [])
    cfg.setdefault("cargo_pkgs", [cfg["slug"], cfg["slug"] + "-cli"])
    cfg.setdefault("binaries", list(cfg["cargo_pkgs"]))
    if "cli" not in cfg:
        cfg["cli"] = cfg["slug"] + "-cli"
    perms = dict(DEFAULT_PERMISSIONS)
    perms.update(cfg.get("permissions") or {})
    cfg["permissions"] = perms
    missing = [k for k in REQUIRED if not cfg.get(k)]
    if missing:
        sys.exit("ERROR: apps/%s.yml 缺少字段: %s" % (app, ", ".join(missing)))
    return cfg


def cargo_packages(cfg):
    args = []
    for pkg in cfg["cargo_pkgs"]:
        args += ["-p", pkg]
    if cfg["features"]:
        args += ["--features", ",".join(cfg["features"])]
    return args


def write_env(cfg, env_path):
    """写出 shell 变量文件；多行内容（描述、权限）写到附属文件里。"""
    base = os.path.dirname(os.path.abspath(env_path)) or "."
    os.makedirs(base, exist_ok=True)
    desc_path = os.path.join(base, "app-description.txt")
    perm_path = os.path.join(base, "app-permissions.json")
    open(desc_path, "w", encoding="utf-8").write(cfg["description"].rstrip("\n") + "\n")
    open(perm_path, "w", encoding="utf-8").write(
        json.dumps(cfg["permissions"], ensure_ascii=False, indent=2) + "\n")

    def q(value):
        return shlex.quote(str(value))

    lines = [
        "APP_SLUG=%s" % q(cfg["slug"]),
        "APP_NAME=%s" % q(cfg["name"]),
        "APP_ID=%s" % q(cfg["appid"]),
        "APP_REPO=%s" % q(cfg["repo"]),
        "APP_HOMEPAGE=%s" % q(cfg["homepage"]),
        "APP_SECTION=%s" % q(cfg["section"]),
        "APP_CARGO_ARGS=%s" % q(" ".join(cargo_packages(cfg))),
        "APP_BINARIES=%s" % q(" ".join(cfg["binaries"])),
        "APP_CLI=%s" % q(cfg["cli"] or ""),
        "APP_EXTRA_DEPS=%s" % q(" ".join(cfg["extra_deps"])),
        "APP_DESCRIPTION_FILE=%s" % q(desc_path),
        "APP_PERMISSIONS_FILE=%s" % q(perm_path),
    ]
    with open(env_path, "w", encoding="utf-8") as fh:
        fh.write("# 由 scripts/appcfg.py 生成，请勿手改。\n")
        # 必须 export：这些值要传给后续的 python / dpkg-deb 子进程。
        fh.write("\n".join("export " + line for line in lines) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("app", nargs="?", help="apps/<app>.yml 中的 app 名")
    parser.add_argument("--list", action="store_true", help="列出所有 app")
    parser.add_argument("--json", action="store_true", help="输出完整配置 JSON")
    parser.add_argument("--env", metavar="FILE", help="写出 shell 变量文件")
    parser.add_argument("--cargo-packages", action="store_true", help="输出 cargo 包参数")
    args = parser.parse_args(argv)

    if args.list:
        print("\n".join(app_names()))
        return 0
    if not args.app:
        parser.error("需要 app 名，或使用 --list")
    cfg = load_app(args.app)
    if args.json:
        print(json.dumps(cfg, ensure_ascii=False, indent=2))
    elif args.env:
        write_env(cfg, args.env)
        print("wrote %s" % args.env)
    elif args.cargo_packages:
        print(" ".join(cargo_packages(cfg)))
    else:
        parser.error("请指定 --list / --json / --env / --cargo-packages")
    return 0


if __name__ == "__main__":
    sys.exit(main())
