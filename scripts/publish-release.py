#!/usr/bin/env python3
"""发布：把本次编译产物合并进唯一的滚动 latest release，并清理历史 release。

用法:
  publish-release.py --artifacts DIR [--changed-dir DIR] [--tag latest] [--dry-run]

做法（对应需求“每日只保留一个最新 release”）:
  1. 读取 latest release 里的 versions.json，它就是“上次编译的版本”记录
  2. 本次新编译的 deb 从 --artifacts 收进来；没有新 deb 的 app 沿用 release 里
     已有的资产（这就是“没更新的用上次编译的缓存”）
  3. 有更新的 app：先删掉它在 release 里的旧 deb，再上传新 deb，并更新记录
  4. 重写 versions.json 资产与 release 说明（版本对照表）
  5. 删除除 --tag 之外的所有 release 及其 tag，保证仓库里永远只有一个最新 release
  6. 新增的 deb 复制到 --changed-dir，供后续 WebDAV 上传（未更新的不重复上传）

需要环境: GH_TOKEN、GITHUB_REPOSITORY
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from appcfg import app_names, load_app  # noqa: E402

DEB_RE = re.compile(r"^(?P<slug>.+)_(?P<version>[^_]+)_(?P<arch>amd64|arm64)\.deb$")
ARCHES = ("amd64", "arm64")


def sh(*args, check=True, capture=True):
    proc = subprocess.run(args, check=False, text=True,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE if capture else None)
    if check and proc.returncode != 0:
        sys.exit("ERROR: %s 失败 (%d)\n%s" % (" ".join(args), proc.returncode, proc.stderr or ""))
    return (proc.stdout or "").strip()


def gh(*args, check=True, capture=True):
    return sh("gh", *args, check=check, capture=capture)


def release_info(tag):
    """返回 (是否存在, assets 列表)。"""
    out = gh("release", "view", tag, "--json", "tagName,assets", check=False)
    if not out:
        return False, []
    data = json.loads(out)
    return True, data.get("assets", [])


def download_state(tag, dest):
    gh("release", "download", tag, "--pattern", "versions.json", "--dir", dest, "--clobber",
       check=False)
    path = os.path.join(dest, "versions.json")
    if os.path.isfile(path):
        try:
            return json.load(open(path, encoding="utf-8"))
        except Exception as exc:
            print("warning: versions.json 解析失败（%s），按空记录处理" % exc, file=sys.stderr)
    return {}


def new_debs(artifacts):
    """扫描本次构建产物，返回 {slug: {arch: filepath}}（只收两边架构都齐全的）。"""
    found = {}
    if not os.path.isdir(artifacts):
        return found
    for path in sorted(glob.glob(os.path.join(artifacts, "**", "*.deb"), recursive=True)):
        m = DEB_RE.match(os.path.basename(path))
        if not m:
            print("warning: 忽略无法识别的 deb: %s" % path, file=sys.stderr)
            continue
        found.setdefault(m.group("slug"), {})[m.group("arch")] = path
    return found


def markdown_body(state, missing):
    lines = ["# ArtCraft deepin/deb 最新构建", ""]
    lines.append("在 Debian 10 (glibc 2.28) 容器内从上游源码编译，可运行于 UOS / Deepin V20。")
    lines.append("amd64 与 arm64 双架构；每日检查上游 release，只重新编译有更新的 app。")
    lines.append("")
    lines.append("| app | 版本 | 说明 |")
    lines.append("|---|---|---|")
    for app in app_names():
        entry = state.get("apps", {}).get(app)
        note = "✅" if entry else "❌ 未构建"
        lines.append("| %s | %s | %s |" % (app, entry["version"] if entry else "-", note))
    if missing:
        lines.append("")
        lines.append("> 注意: 以下 app 缺少完整产物，沿用旧版本: %s" % ", ".join(sorted(missing)))
    lines.append("")
    lines.append("版本记录见 versions.json 资产。")
    return "\n".join(lines)


def write_summary(text, extra=""):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text + "\n" + extra + "\n")
    print(text + ("\n" + extra if extra else ""))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", default="build-artifacts",
                        help="本次构建产物目录（递归扫描 *.deb）")
    parser.add_argument("--changed-dir", default="publish",
                        help="本次有更新的 deb 输出目录（供 WebDAV 上传）")
    parser.add_argument("--tag", default="latest")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        sys.exit("ERROR: 缺少 GITHUB_REPOSITORY")
    if not os.environ.get("GH_TOKEN"):
        sys.exit("ERROR: 缺少 GH_TOKEN")

    configs = {app: load_app(app) for app in app_names()}
    built = new_debs(args.artifacts)

    exists, assets = release_info(args.tag)
    asset_names = {a["name"] for a in assets}
    prev = download_state(args.tag, args.artifacts) if exists else {}
    prev_apps = prev.get("apps", {}) if isinstance(prev, dict) else {}

    state = {"schema": 1, "tag": args.tag, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "apps": {}}
    to_delete_assets, to_upload, missing = [], [], []

    for app, cfg in sorted(configs.items()):
        slug = cfg["slug"]
        entry = dict(prev_apps.get(app) or {})
        fresh = built.get(slug, {})
        if len(fresh) == len(ARCHES):
            versions = {arch: DEB_RE.match(os.path.basename(p)).group("version") for arch, p in fresh.items()}
            if len(set(versions.values())) != 1:
                sys.exit("ERROR: %s 两个架构的版本不一致: %s" % (app, versions))
            version = versions["amd64"]
            entry.update({
                "version": version,
                "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "source": "https://github.com/%s/releases/tag/v%s" % (cfg["repo"], version),
                "debs": {arch: os.path.basename(p) for arch, p in fresh.items()},
            })
            for name in asset_names:
                m = DEB_RE.match(name)
                if m and m.group("slug") == slug:
                    to_delete_assets.append(name)
            to_upload += [fresh[a] for a in ARCHES]
        elif fresh:
            missing.append("%s（只编译了 %s）" % (app, "/".join(sorted(fresh))))
        elif not entry or not all(entry.get("debs", {}).get(a) in asset_names for a in ARCHES):
            if not exists:
                missing.append("%s（release 里还没有产物）" % app)
        state["apps"][app] = entry

    if args.dry_run:
        print("dry-run: 将删除资产 %s" % to_delete_assets)
        print("dry-run: 将上传资产 %s" % [os.path.basename(p) for p in to_upload])
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0

    # -------------------------------------------------------------- release --
    if not exists:
        gh("release", "create", args.tag, "--title", "ArtCraft deepin/deb 最新构建",
           "--notes", markdown_body(state, missing))
    else:
        gh("release", "edit", args.tag, "--title", "ArtCraft deepin/deb 最新构建",
           "--notes", markdown_body(state, missing))

    for name in to_delete_assets:
        gh("release", "delete-asset", args.tag, name, "--yes", check=False)

    os.makedirs(args.changed_dir, exist_ok=True)
    for path in to_upload:
        gh("release", "upload", args.tag, path, "--clobber")
        shutil.copy2(path, os.path.join(args.changed_dir, os.path.basename(path)))

    state_path = os.path.join(args.artifacts, "versions.json")
    with open(state_path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    gh("release", "delete-asset", args.tag, "versions.json", "--yes", check=False)
    gh("release", "upload", args.tag, state_path, "--clobber")

    # ------------------------------------------------- 清理历史 release/tag --
    listing = gh("release", "list", "--limit", "100", "--json", "tagName,isDraft", check=False)
    removed = []
    for item in (json.loads(listing) if listing else []):
        if item["tagName"] == args.tag:
            continue
        gh("release", "delete", item["tagName"], "--yes", "--cleanup-tag", check=False)
        removed.append(item["tagName"])

    built_apps = [a for a, e in state["apps"].items() if e]
    summary = [
        "## 发布结果",
        "",
        "- release tag: `%s`" % args.tag,
        "- 本次新编译: %s" % (", ".join(sorted(built)) or "无"),
        "- 已删除的历史 release: %s" % (", ".join(removed) or "无"),
        "- 已记录的 app 数: %d / %d" % (len(built_apps), len(configs)),
    ]
    if missing:
        summary.append("- ⚠️ 缺少完整产物: %s" % "; ".join(missing))
    write_summary("\n".join(summary))
    if missing and not built_apps:
        sys.exit("ERROR: 没有任何 app 的完整产物，release 保持原样")
    return 0


if __name__ == "__main__":
    sys.exit(main())
