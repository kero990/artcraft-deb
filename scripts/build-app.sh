#!/usr/bin/env bash
# 在 Debian 10 (glibc 2.28) 容器内编译单个 ArtCraft app，并打出 deepin 格式的 deb。
#
# 用法: build-app.sh <app> <version> <deb_arch> <asset_arch>
#   <app>         apps/<app>.yml 里的名字
#   <version>     要打包的上游版本（不带 v）
#   <deb_arch>    deb 架构名: amd64 / arm64
#   <asset_arch>  上游发布资产使用的架构名: x86_64 / aarch64
#
# 环境变量:
#   WORK          工作目录，默认 $GITHUB_WORKSPACE 或 $PWD
#   OUT_DIR       产物目录，默认 $WORK/out
#   DEB_CACHE_DIR 已编译 deb 的缓存目录；命中 (app, version, arch) 直接复用，跳过编译
#   SKIP_DEB_CACHE=1  忽略缓存，强制重新编译
#
# 为什么在 Debian 10 里编译: 上游用 Ubuntu 22.04 (glibc 2.35) 出的官方 deb 在
# UOS / Deepin V20 (glibc 2.28) 上起不来，所以这里从源码重编。
set -euo pipefail

APP="${1:?用法: build-app.sh <app> <version> <deb_arch> <asset_arch>}"
VERSION="${2:?缺少 version}"
DEB_ARCH="${3:?缺少 deb_arch}"
ASSET_ARCH="${4:?缺少 asset_arch}"

WORK="${GITHUB_WORKSPACE:-$PWD}"
OUT_DIR="${OUT_DIR:-$WORK/out}"
STEPS="$WORK/.build"
mkdir -p "$STEPS" "$OUT_DIR"

python3 "$WORK/scripts/appcfg.py" "$APP" --env "$STEPS/app.env"
# shellcheck disable=SC1090
. "$STEPS/app.env"

FILENAME="${APP_SLUG}_${VERSION}_${DEB_ARCH}.deb"

# ---------------------------------------------------------------- 命中缓存 --
if [ "${SKIP_DEB_CACHE:-0}" != "1" ] && [ -n "${DEB_CACHE_DIR:-}" ] && [ -f "$DEB_CACHE_DIR/$FILENAME" ]; then
  echo "== 缓存命中: $FILENAME，跳过编译 =="
  cp -a "$DEB_CACHE_DIR/$FILENAME" "$OUT_DIR/$FILENAME"
  ls -lh "$OUT_DIR/$FILENAME"
  exit 0
fi

# ------------------------------------------------------------------ 取源码 --
SRC="$WORK/src"
echo "== 克隆 ${APP_REPO} @ v${VERSION} =="
rm -rf "$SRC"
if git clone --depth 1 --branch "v${VERSION}" "https://github.com/${APP_REPO}.git" "$SRC"; then
  echo "已检出 tag v${VERSION}"
else
  echo "tag v${VERSION} 不存在，改用默认分支"
  rm -rf "$SRC"
  git clone --depth 1 "https://github.com/${APP_REPO}.git" "$SRC"
fi
grep -m1 '^version' "$SRC/Cargo.toml" || true
git -C "$SRC" log -1 --format='source: %h %s'

# ------------------------------------------------------------------ 编译 --
# 上游各 app 的官方构建命令是 `cargo build --release --locked -p <app> -p <app>-cli`
# （见各自 packaging/linux/package.sh）。配置里的 features 逐个校验，某个 tag 的
# manifest 还没有该特性时降级跳过，而不是让整次构建失败。
FILTERED=()
SKIP_FEATURES=""
want_features=0
for tok in $APP_CARGO_ARGS; do
  if [ "$want_features" = 1 ]; then
    kept=""
    for feat in $(echo "$tok" | tr ',' ' '); do
      pkg="${feat%%/*}"; name="${feat##*/}"
      if grep -qE "^${name}[[:space:]]*[:=]" "$SRC/apps/$pkg/Cargo.toml" 2>/dev/null \
         || grep -qE "^${name}[[:space:]]*[:=]" "$SRC/apps/${APP_SLUG}-cli/Cargo.toml" 2>/dev/null; then
        kept="${kept:+$kept,}$feat"
      else
        SKIP_FEATURES="${SKIP_FEATURES:+$SKIP_FEATURES,}$feat"
      fi
    done
    if [ -n "$kept" ]; then FILTERED+=(--features "$kept"); fi
    want_features=0
    continue
  fi
  FILTERED+=("$tok")
  [ "$tok" = "--features" ] && want_features=1
done
if [ -n "$SKIP_FEATURES" ]; then
  echo "warning: 这个版本没有特性 $SKIP_FEATURES，已跳过"
fi
echo "cargo build --release --locked ${FILTERED[*]}"

export PATH="$HOME/.cargo/bin:$PATH"
rustc --version
cargo --version
(cd "$SRC" && cargo build --release --locked "${FILTERED[@]}")

mkdir -p "$SRC/target/release"
for bin in $APP_BINARIES; do
  strip "$SRC/target/release/$bin" || true
  ls -lh "$SRC/target/release/$bin"
done
echo "== 最高的 GLIBC 符号（必须 <= 2.28）=="
{ objdump -T "$SRC/target/release/${APP_SLUG}" | grep -o 'GLIBC_[0-9.]*' | sort -uV | tail -3; } || true

# ------------------------------------------------------------------ 打包 --
"$WORK/scripts/package-app.sh" "$APP" "$VERSION" "$DEB_ARCH" "$ASSET_ARCH"

# 命中条件满足时把 deb 存进缓存，供下次“无更新则复用”的流程使用。
if [ -n "${DEB_CACHE_DIR:-}" ]; then
  mkdir -p "$DEB_CACHE_DIR"
  cp -a "$OUT_DIR/$FILENAME" "$DEB_CACHE_DIR/$FILENAME"
  echo "已写入缓存: $DEB_CACHE_DIR/$FILENAME"
fi
