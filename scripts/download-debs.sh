#!/usr/bin/env bash
# 下载 latest release 里的全部 deb，供手动上传 deepin 应用商店。
#
# 商店没有 CI 上传接口，只能手动传：这个脚本把 release 里的 deb 一次性拉到
# 本地目录，文件名形如 <slug>_<version>_<arch>.deb，按架构分批上传即可。
#
# 不用 GitHub API：仓库只有一个滚动 latest release，直接抓 release 页面的
# expanded_assets 拿资产清单（页面里自带 sha256 和大小），再走镜像下载。
# 国内直连 api.github.com 基本不通，github.com 页面和镜像都还能用。
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
#   REPO    目标仓库，默认 kero990/artcraft-deb
#   TAG     release tag，默认 latest
#   MIRROR  镜像前缀，如 https://ghfast.top/ ；留空则按内置列表逐个试，
#           最后回退直连。注意镜像前缀结尾的 / 不能省。
set -euo pipefail

REPO="${REPO:-kero990/artcraft-deb}"
TAG="${TAG:-latest}"
# 12 个 app × 2 架构（amd64 / arm64）；对不上就提醒，免得漏传商店
EXPECTED="${EXPECTED:-24}"
PAGE="https://github.com/$REPO/releases/expanded_assets/$TAG"

# 两组候选，用途不同：
#   PAGE_CANDIDATES  抓资产清单页（很小，github.com 直连一般还通，MIRROR 挂了也能回退）
#   FILE_CANDIDATES  下载 deb（大文件，国内必须走镜像；指定了 MIRROR 就只信它）
DEFAULT_MIRRORS=(
  "https://ghfast.top/"
  "https://ghproxy.net/"
  "https://gh-proxy.com/"
)
PAGE_CANDIDATES=(${DEFAULT_MIRRORS[@]+"${DEFAULT_MIRRORS[@]}"} "")
if [ -n "${MIRROR:-}" ]; then
  FILE_CANDIDATES=("$MIRROR")
  # 指定的镜像若不代理页面，清单页仍可回退直连拿
  PAGE_CANDIDATES=("$MIRROR" "")
else
  FILE_CANDIDATES=(${DEFAULT_MIRRORS[@]+"${DEFAULT_MIRRORS[@]}"} "")
fi

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

command -v curl >/dev/null || { echo "ERROR: 需要 curl" >&2; exit 1; }
command -v python3 >/dev/null || { echo "ERROR: 需要 python3" >&2; exit 1; }

# 依次试镜像和直连，返回第一个能给出资产清单的来源。空串前缀 = 直连。
fetch_assets() {
  local prefix html
  for prefix in "${PAGE_CANDIDATES[@]}"; do
    if html="$(curl -fsSL -m 30 "$prefix$PAGE" 2>/dev/null)"; then
      if printf '%s' "$html" | grep -q 'releases/download/'; then
        printf '%s' "$html"
        return 0
      fi
    fi
  done
  return 1
}

echo "== 读取 $REPO release '$TAG' 的资产清单 =="
HTML=""
for attempt in 1 2 3; do
  if HTML="$(fetch_assets)"; then break; fi
  echo "   清单页抓取失败，5 秒后重试（第 $attempt 次）" >&2
  sleep 5
done
if [ -z "$HTML" ]; then
  echo "ERROR: 抓不到 $PAGE （镜像和直连都失败）" >&2
  exit 1
fi

# 从页面解析 name<TAB>sha256<TAB>size —— 页面里就带 sha256，不用调 API。
# 只挑 .deb，versions.json 不要。HTML 经环境变量传给 heredoc，避免引号打架。
mapfile -t ROWS < <(EA_HTML="$HTML" python3 - <<'PY'
import os, re
h = os.environ["EA_HTML"]
for m in re.finditer(r"<li[^>]*>.*?</li>", h, re.S):
    t = m.group(0)
    href = re.search(r'href="[^"]*/releases/download/[^/"]+/([^"]+)"', t)
    if not href or not href.group(1).endswith(".deb"):
        continue
    sha = re.search(r"sha256:([0-9a-f]{64})", t)
    size = re.search(r">([\d.]+ [KMG]?B)<", t)
    print("\t".join([href.group(1), sha.group(1) if sha else "",
                     size.group(1) if size else ""]))
PY
)

if [ "${#ROWS[@]}" -eq 0 ]; then
  echo "ERROR: release '$TAG' 里没有 deb 资产" >&2
  exit 1
fi

mkdir -p "$OUT"
total=0 got=0
for row in ${ROWS[@]+"${ROWS[@]}"}; do
  IFS=$'\t' read -r name sha size <<<"$row"
  if [ -n "$ONLY" ] && [[ "$name" != *"$ONLY"* ]]; then continue; fi
  total=$((total + 1))
  dest="$OUT/$name"

  # 已存在且 sha256 对得上就跳过：重跑、断点续传都靠这条。
  if [ -f "$dest" ] && [ -n "$sha" ] \
     && [ "$(sha256sum "$dest" | awk '{print $1}')" = "$sha" ]; then
    echo "跳过（已存在且校验通过）: $name"
    got=$((got + 1))
    continue
  fi

  if [ "$DRY" = 1 ]; then
    echo "[dry-run] $name${size:+ ($size)}"
    got=$((got + 1))
    continue
  fi

  # 逐个试镜像，第一个下成的就算数；都失败才回退直连。
  url="https://github.com/$REPO/releases/download/$TAG/$name"
  ok=0
  for prefix in "${FILE_CANDIDATES[@]}"; do
    if [ -n "$prefix" ]; then echo "下载 $name ... （镜像 $prefix）"
    else echo "下载 $name ... （直连）"; fi
    if curl -fL --retry 2 --retry-delay 2 -m 900 -sS --progress-bar \
         -o "$dest.part" "$prefix$url"; then
      mv "$dest.part" "$dest"
      ok=1
      break
    fi
    rm -f "$dest.part"
  done
  if [ "$ok" != 1 ]; then
    echo "ERROR: $name 所有镜像和直连都失败" >&2
    exit 1
  fi

  # 校验：大小不符或 sha256 对不上，说明下载损坏或镜像被动手脚。
  if [ -n "$sha" ]; then
    if [ "$(sha256sum "$dest" | awk '{print $1}')" != "$sha" ]; then
      echo "ERROR: $name sha256 校验失败，已删除" >&2
      rm -f "$dest"
      exit 1
    fi
    echo "   sha256 校验通过"
  fi
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
