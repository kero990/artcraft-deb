#!/usr/bin/env python3
"""版本检查：比对上游 GitHub release 与上次编译记录，决定哪些 app 需要重新编译。

两种模式：

  1) 全量检查（update.yml 的 check 作业调用）
       check-updates.py --state versions.json --out matrix.json [--force] [--apps a,b]
     - 从 apps/*.yml 读出所有 app
     - 查询上游最新 release tag
     - 与 --state 记录的上次编译版本比对
     - 写出 matrix.json（GitHub Actions 矩阵格式），并把 matrix=/count= 追加到
       $GITHUB_OUTPUT、把对比表格写到 $GITHUB_STEP_SUMMARY

  2) 单个 app（build-app.yml 的 resolve 作业调用）
       check-updates.py --app photocraft [--version 1.2.3]
     - 优先使用 --version 覆盖值，否则取上游最新 release
     - 把 version= 追加到 $GITHUB_OUTPUT，并打印到 stdout

state (versions.json) 是滚动 latest release 里的一个资产，格式：
  {"schema": 1, "apps": {"photocraft": {"version": "0.8.2", ...}}}
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from appcfg import app_names, load_app  # noqa: E402

API = "https://api.github.com"


def http_get_json(url):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "artcraft-deb"}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def latest_release_tag(repo):
    """上游最新正式 release 的 tag（不含 draft/prerelease）。返回 (tag, 错误信息)。"""
    try:
        data = http_get_json("%s/repos/%s/releases/latest" % (API, repo))
    except Exception as exc:  # 网络/限流问题：跳过该 app，不中断整轮检查
        return None, "%s" % exc
    tag = (data.get("tag_name") or "").lstrip("v")
    if not tag:
        return None, "上游还没有发布 release"
    return tag, None


def append_github_output(key, value):
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        if "\n" in value:
            fh.write("%s<<__EOF__\n%s\n__EOF__\n" % (key, value))
        else:
            fh.write("%s=%s\n" % (key, value))


def write_summary(markdown):
    print(markdown)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(markdown + "\n")


def full_check(args):
    state = {}
    if args.state and os.path.isfile(args.state):
        try:
            state = json.load(open(args.state, encoding="utf-8"))
        except Exception as exc:
            print("warning: 解析 state 失败（%s），按全新构建处理" % exc, file=sys.stderr)
    built = (state.get("apps") or {}) if isinstance(state, dict) else {}

    wanted = None
    if args.apps:
        wanted = {a.strip() for a in args.apps.split(",") if a.strip()}
        unknown = wanted - set(app_names())
        if unknown:
            sys.exit("ERROR: 未知 app: %s" % ", ".join(sorted(unknown)))

    include, rows = [], []
    for app in app_names():
        if wanted and app not in wanted:
            continue
        cfg = load_app(app)
        upstream, err = latest_release_tag(cfg["repo"])
        prev = (built.get(app) or {}).get("version")
        if err:
            # 查询失败不影响其他 app，也不改写它上次编译的记录。
            print("warning: %s 查询失败（%s）" % (cfg["repo"], err), file=sys.stderr)
            status, version = "⚠️ 查询失败", prev
        elif args.force:
            status, version = "🔁 强制重建", upstream
        elif prev is None:
            status, version = "🆕 首次编译", upstream
        elif prev != upstream:
            status, version = "⬆️ 有更新", upstream
        else:
            status, version = "✅ 已是最新", prev
        if version and status in ("🔁 强制重建", "🆕 首次编译", "⬆️ 有更新"):
            include.append({"app": app, "version": version})
        rows.append((app, cfg["name"], prev or "-", upstream or "-", status))

    matrix = {"include": include} if include else {"include": []}
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(matrix, fh, ensure_ascii=False)

    lines = ["## ArtCraft 版本检查", ""]
    lines.append("| app | 名称 | 上次编译 | 上游最新 | 状态 |")
    lines.append("|---|---|---|---|---|")
    for app, name, prev, up, status in rows:
        lines.append("| %s | %s | %s | %s | %s |" % (app, name, prev, up, status))
    lines.append("")
    lines.append("**需要编译: %d 个**" % len(include))
    if include:
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(matrix, ensure_ascii=False, indent=2))
        lines.append("```")
    write_summary("\n".join(lines))

    append_github_output("matrix", json.dumps(matrix, ensure_ascii=False))
    append_github_output("count", str(len(include)))
    append_github_output("apps", ",".join(item["app"] for item in include))
    return 0


def single(args):
    cfg = load_app(args.app)
    version, err = latest_release_tag(cfg["repo"])
    if args.version and args.version.strip():
        version, err = args.version.strip(), None
    if not version:
        sys.exit("ERROR: %s 无法确定版本（%s）" % (args.app, err or "未知原因"))
    print("%s -> %s" % (args.app, version))
    append_github_output("version", version)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--state", help="上次编译记录 versions.json")
    parser.add_argument("--out", help="输出矩阵文件")
    parser.add_argument("--force", action="store_true", help="忽略状态，全部重建")
    parser.add_argument("--apps", help="只处理这些 app（逗号分隔）")
    parser.add_argument("--app", help="只解析单个 app 的版本")
    parser.add_argument("--version", help="配合 --app：版本覆盖值")
    args = parser.parse_args(argv)
    return single(args) if args.app else full_check(args)


if __name__ == "__main__":
    sys.exit(main())
