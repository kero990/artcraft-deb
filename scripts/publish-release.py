#!/usr/bin/env python3
"""发布：把本次编译产物合并进唯一的滚动 latest release，并清理历史 release。

用法:
  publish-release.py --artifacts DIR [--changed-dir DIR] [--tag latest] [--dry-run]

做法（对应需求“每日只保留一个最新 release”）:
  1. 读取 latest release 里的 versions.json，它就是“上次编译的版本”记录
  2. 本次新编译的 deb 从 --artifacts 收进来；没有新 deb 的 app 沿用 release 里
     已有的资产（这就是“没更新的用上次编译的缓存”）
  3. 每次发布都重建 release：先建临时 tag latest-next 传完全部资产，再删掉旧的
     latest 并把 latest-next 改名过来。GitHub 列表页显示的是 release 的发布时间，
     原地换资产不会刷新它，页面上的日期会永远停在首次发布那天。
     不能先删后传：上传 24 个资产要几分钟，中途失败 latest 就没了。
  4. 删除除 --tag 之外的所有 release 及其 tag，保证仓库里永远只有一个最新 release
  5. 新增的 deb 复制到 --changed-dir，供后续 WebDAV 上传（未更新的不重复上传）

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
RELEASE_TITLE = "ArtCraft deepin/deb 最新构建"


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
    parser.add_argument("--carry-dir", default="carry",
                        help="沿用旧 release 资产的临时下载目录")
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
    to_upload, missing, carry = [], [], []

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
            to_upload += [fresh[a] for a in ARCHES]
        elif fresh:
            missing.append("%s（只编译了 %s）" % (app, "/".join(sorted(fresh))))
        elif entry and all(entry.get("debs", {}).get(a) in asset_names for a in ARCHES):
            # 没更新的 app：它在旧 release 里的 deb 要原样搬到新 release，
            # 否则删旧 release 时会把这些资产一起带走（日常只更新 1-2 个 app
            # 时，其余二十几个 deb 就这么丢过一次）。
            carry += [entry["debs"][a] for a in ARCHES]
        elif not entry or not all(entry.get("debs", {}).get(a) in asset_names for a in ARCHES):
            missing.append("%s（release 里没有完整产物）" % app)
        state["apps"][app] = entry

    if args.dry_run:
        print("dry-run: 重建 %s release" % args.tag)
        print("dry-run: 本次新编译并上传 %s" % [os.path.basename(p) for p in to_upload])
        print("dry-run: 原样沿用 %s" % sorted(set(carry)))
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0

    # 一个产物都没有、而且还没有 release：直接跳过，绝不能建出一个空的 latest。
    if not to_upload and not exists:
        write_summary("## 发布结果\n\n- 本次没有任何完整产物，且 release 尚不存在，跳过发布")
        return 0

    # -------------------------------------------------------------- release --
    if not to_upload:
        # 本次没有任何新的完整产物：不动 release，也不建空 release。
        note = "本次没有新的完整产物，%s release 保持原样" % args.tag
        if missing:
            note += "（%d 个 app 缺少产物，见下）" % len(missing)
        write_summary("## 发布结果\n\n- %s" % note)
        return 0

    # 每次发布都重建 release，而不是在旧 release 上换资产：GitHub 列表页显示的是
    # release 的发布时间，换资产不会刷新它，页面上的日期永远停在首次发布那天。
    # 但不能先删后传——上传 24 个资产要几分钟，中途失败 latest 就没了。所以先传到
    # 临时 tag，全部成功后再删旧的、把临时 tag 改名过来，不可用窗口只有两次 API 调用。
    #
    # 沿用的资产要趁旧 release 还在时先下载下来，重建时一并传到新 release：删旧
    # release 会把它名下的资产全带走，只传本次新编的 deb 会丢掉其余 app 的产物。
    carry_dir = os.path.join(os.path.dirname(os.path.abspath(args.artifacts)), args.carry_dir)
    carried = []
    if carry:
        shutil.rmtree(carry_dir, ignore_errors=True)
        os.makedirs(carry_dir, exist_ok=True)
        print("== 沿用旧 release 资产: %s ==" % ", ".join(sorted(set(carry))))
        gh("release", "download", args.tag, "--dir", carry_dir, "--clobber",
           *[p for name in sorted(set(carry)) for p in ("--pattern", name)])
        for name in sorted(set(carry)):
            path = os.path.join(carry_dir, name)
            if not os.path.isfile(path):
                sys.exit("ERROR: 沿用资产 %s 下载失败，中止发布（旧 release 未动）" % name)
            carried.append(path)

    state_path = os.path.join(args.artifacts, "versions.json")
    with open(state_path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    notes = markdown_body(state, missing)
    upload_tag = args.tag if not exists else args.tag + "-next"
    if upload_tag != args.tag:
        # 上次发布中途失败可能留下半成品，先清掉
        gh("release", "delete", upload_tag, "--yes", "--cleanup-tag", check=False)

    gh("release", "create", upload_tag, "--title", RELEASE_TITLE, "--notes", notes)
    os.makedirs(args.changed_dir, exist_ok=True)
    # 沿用的资产直接传；本次新编的额外复制到 changed-dir 供 WebDAV 上传
    for path in carried + to_upload:
        gh("release", "upload", upload_tag, path)
    for path in to_upload:
        shutil.copy2(path, os.path.join(args.changed_dir, os.path.basename(path)))
    gh("release", "upload", upload_tag, state_path)

    if upload_tag != args.tag:
        gh("release", "delete", args.tag, "--yes", "--cleanup-tag")
        # gh 的 flag 是 --tag（不是 --tag-name）；改名只改 release 指向，旧 git tag
        # 不会跟着删（实测残留），所以这里显式清掉，否则 tag 会越积越多。
        gh("release", "edit", upload_tag, "--tag", args.tag)
        gh("api", "-X", "DELETE",
           "repos/%s/git/refs/tags/%s" % (repo, upload_tag), check=False)

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
    if missing:
        print("warning: 以下 app 缺少完整产物: %s" % "; ".join(missing), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
