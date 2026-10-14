#!/usr/bin/env bash
# 下载 latest release 里的全部 deb，供手动上传 deepin 应用商店。
#
# 商店没有 CI 上传接口，只能手动传：这个脚本把 release 资产里的 deb 一次性
# 拉到本地目录，文件名形如 <slug>_<version>_<arch>.deb，直接按架构分批上传即可。
#
# 用法:
#   scripts/download-debs.sh [输出目录] [--dry-run] [--only 关键词]
#
# 例:
#   scripts/download-debs.sh                    # 全部 deb 下到 ./debs
#   scripts/download-debs.sh ~/debs             # 下到指定目录
#   scripts/download-debs.sh --only photocraft  # 只下名字含 photocraft 的
#   scripts/download-debs.sh --dry-run          # 只列出，不下
#
# 环境变量:
#   REPO     目标仓库，默认 kero990/artcraft-deb
#   TAG      release tag，默认 latest（仓库里唯一的滚动 release）
#   GH_TOKEN 可选；仓库是公开的，匿名即可，只有撞到 API 限流时才需要
set -euo pipefail

REPO="${REPO:-kero990/artcraft-deb}"
TAG="${TAG:-latest}"
# 12 个 app × 2 架构（amd64 / arm64）；对不上就提醒，免得漏传商店
EXPECTED="${EXPECTED:-24}"
API="https://api.github.com/repos/$REPO/releases/tags/$TAG"

OUT="debs" DRY=0 ONLY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --only)    shift; ONLY="${1:?--only 需要一个关键词}" ;;
    -*)        echo "未知参数: $1" >&2; exit 2 ;;
    *)         OUT="$1" ;;
  esac
  shift
done

command -v curl    >/dev/null || { echo "ERROR: 需要 curl" >&2; exit 1; }
command -v python3 >/dev/null || { echo "ERROR: 需要 python3" >&2; exit 1; }

# 公开仓库匿名即可。空数组在 set -u 下用 ${ARR[@]+...} 保护，兼容老 bash。
AUTH=()
[ -n "${GH_TOKEN:-${GITHUB_TOKEN:-}}" ] && AUTH=(-H "Authorization: Bearer ${GH_TOKEN:-$GITHUB_TOKEN}")

echo "== 读取 $REPO release '$TAG' 的资产 =="
LIST="$(curl -fsSL ${AUTH[@]+"${AUTH[@]}"} "$API")" \
  || { echo "ERROR: 取 release 资产失败（仓库/Tag 对不对？）" >&2; exit 1; }

# name<TAB>url<TAB>size<TAB>digest —— 只挑 .deb，versions.json 不要。
mapfile -t ROWS < <(printf '%s' "$LIST" | python3 -c '
import json, sys
rel = json.load(sys.stdin)
for a in rel.get("assets", []):
    n = a["name"]
    if not n.endswith(".deb"):
        continue
    print("\t".join([n, a["browser_download_url"],
                     str(a.get("size") or 0), a.get("digest") or ""]))
')

if [ "${#ROWS[@]}" -eq 0 ]; then
  echo "ERROR: release '$TAG' 里没有 deb 资产" >&2
  exit 1
fi

mkdir -p "$OUT"
total=0 got=0
for row in ${ROWS[@]+"${ROWS[@]}"}; do
  IFS=$'\t' read -r name url size digest <<<"$row"
  if [ -n "$ONLY" ] && [[ "$name" != *"$ONLY"* ]]; then continue; fi
  total=$((total + 1))
  dest="$OUT/$name"

  # 已存在且大小一致就跳过：重跑/断点续传都靠这条。
  if [ -f "$dest" ] && [ "$(stat -c%s "$dest")" = "$size" ]; then
    echo "跳过（已存在）: $name"
    got=$((got + 1))
    continue
  fi

  if [ "$DRY" = 1 ]; then
    echo "[dry-run] $name ($size bytes)"
    got=$((got + 1))
    continue
  fi

  echo "下载 $name ..."
  curl -fL --retry 3 --retry-delay 2 -sS --progress-bar \
    ${AUTH[@]+"${AUTH[@]}"} -o "$dest.part" "$url"
  mv "$dest.part" "$dest"

  # 下完就校验：大小对不上说明传一半，sha256 用 release 自带的 digest 比对。
  if [ "$(stat -c%s "$dest")" != "$size" ]; then
    echo "ERROR: $name 大小不符（期望 $size，实际 $(stat -c%s "$dest")）" >&2
    rm -f "$dest"
    exit 1
  fi
  case "$digest" in
    sha256:*)
      want="${digest#sha256:}"
      got_hash="$(sha256sum "$dest" | awk '{print $1}')"
      if [ "$got_hash" != "$want" ]; then
        echo "ERROR: $name sha256 校验失败" >&2
        rm -f "$dest"
        exit 1
      fi
      ;;
  esac
  got=$((got + 1))
done

if [ "$total" -eq 0 ]; then
  echo "没有资产匹配 --only '$ONLY'" >&2
  exit 1
fi

echo
echo "== 完成: $got/$total 个 deb 在 $OUT/ =="
if [ "$DRY" = 0 ]; then
  du -sh "$OUT" | sed 's/^/   总大小: /'
  if [ -z "$ONLY" ] && [ "$total" -ne "$EXPECTED" ]; then
    echo "警告: 预期 $EXPECTED 个（12 app × 2 架构），实际 $total 个，可能有 app 缺产物" >&2
  fi
  echo "   amd64 → 商店选 x86_64，arm64 → 商店选 arm64"
fi
