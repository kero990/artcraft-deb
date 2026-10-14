#!/usr/bin/env bash
# 把 $SRC/target/release 的二进制打包成 deepin 格式 deb（/opt/apps/<appid> 布局）。
#
# 用法: package-app.sh <app> <version> <deb_arch> <asset_arch>
# 桌面文件与图标取自上游同版本的官方 deb（架构无关），取不到时退回源码树；
# Exec/TryExec 必须改写成 /opt/apps 下的绝对路径，否则 deepin 启动器起不来。
set -euo pipefail

APP="${1:?用法: package-app.sh <app> <version> <deb_arch> <asset_arch>}"
VERSION="${2:?缺少 version}"
DEB_ARCH="${3:?缺少 deb_arch}"
ASSET_ARCH="${4:?缺少 asset_arch}"

WORK="${GITHUB_WORKSPACE:-$PWD}"
OUT_DIR="${OUT_DIR:-$WORK/out}"
STEPS="$WORK/.build"

if [ -z "${APP_ID:-}" ]; then
  python3 "$WORK/scripts/appcfg.py" "$APP" --env "$STEPS/app.env"
  # shellcheck disable=SC1090
  . "$STEPS/app.env"
fi

SRC="${SRC:-$WORK/src}"
PKG_ROOT="$WORK/pkg"
APPID_DIR="$PKG_ROOT/opt/apps/$APP_ID"
UP_DEB="$WORK/upstream.deb"
UP_DIR="$WORK/upstream"

rm -rf "$PKG_ROOT" "$UP_DIR"
mkdir -p "$OUT_DIR" \
         "$APPID_DIR/files" \
         "$APPID_DIR/entries/applications" \
         "$APPID_DIR/entries/icons/hicolor" \
         "$PKG_ROOT/DEBIAN"

# ------------------------------------------------------------------ 1) 二进制
for bin in $APP_BINARIES; do
  [ -f "$SRC/target/release/$bin" ] || { echo "ERROR: 找不到 $SRC/target/release/$bin" >&2; exit 1; }
  install -Dm755 "$SRC/target/release/$bin" "$APPID_DIR/files/$bin"
done

# 在这个容器里编译的意义就是拿到 glibc 2.28 的二进制：如果出现了比容器 glibc
# 更新的 GLIBC_* 符号，在 UOS/Deepin V20 上只会表现为 "version `GLIBC_2.xx' not
# found"，所以在这里就直接判失败。
SYS_GLIBC="$(ldd --version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+$' || true)"
for bin in $APP_BINARIES; do
  target="$APPID_DIR/files/$bin"
  MAX_GLIBC="$(objdump -T "$target" | grep -o 'GLIBC_[0-9.]*' | sed 's/^GLIBC_//' | sort -uV | tail -1 || true)"
  if [ -z "$MAX_GLIBC" ] || [ -z "$SYS_GLIBC" ]; then
    echo "跳过 $bin 的 glibc 检查（max='$MAX_GLIBC' host='$SYS_GLIBC'）"
    continue
  fi
  BIGGEST="$(printf '%s\n%s\n' "$SYS_GLIBC" "$MAX_GLIBC" | sort -uV | tail -1)"
  if [ "$BIGGEST" != "$SYS_GLIBC" ]; then
    echo "ERROR: $bin 需要 GLIBC_$MAX_GLIBC，但容器只有 $SYS_GLIBC" >&2
    exit 1
  fi
  echo "$bin: 最高 GLIBC_$MAX_GLIBC（容器 glibc $SYS_GLIBC）"
done

# ------------------------------------------------------------ 2) 桌面项与图标
fetch_upstream_deb() {
  local arch="$1"
  local url="https://github.com/${APP_REPO}/releases/download/v${VERSION}/${APP_SLUG}-${VERSION}-linux-${arch}.deb"
  echo "== 取上游官方 deb 的桌面/图标资源: $url"
  if curl -fL --retry 3 -o "$UP_DEB" "$url"; then
    rm -rf "$UP_DIR"
    dpkg-deb -x "$UP_DEB" "$UP_DIR"
    return 0
  fi
  return 1
}

DESKTOP_SRC=""
ICONS_SRC=""
if fetch_upstream_deb "$ASSET_ARCH" || fetch_upstream_deb "$([ "$ASSET_ARCH" = x86_64 ] && echo aarch64 || echo x86_64)"; then
  DESKTOP_SRC="$UP_DIR/usr/share/applications/$APP_ID.desktop"
  ICONS_SRC="$UP_DIR/usr/share/icons/hicolor"
fi
if [ ! -f "$DESKTOP_SRC" ]; then
  echo "warning: 上游 deb 不可用，退回源码树里的资源"
  DESKTOP_SRC="$SRC/packaging/linux/$APP_ID.desktop"
  ICONS_SRC="$SRC/assets/app-icon/hicolor"
fi
[ -f "$DESKTOP_SRC" ] || { echo "ERROR: 找不到 desktop 文件: $DESKTOP_SRC" >&2; exit 1; }
[ -d "$ICONS_SRC" ] || { echo "ERROR: 找不到图标目录: $ICONS_SRC" >&2; exit 1; }
cat "$DESKTOP_SRC"

sed -E "s#^Exec=.*#Exec=/opt/apps/$APP_ID/files/$APP_SLUG %F#; s#^TryExec=.*#TryExec=/opt/apps/$APP_ID/files/$APP_SLUG#; s#^Icon=.*#Icon=$APP_ID#" \
  "$DESKTOP_SRC" > "$APPID_DIR/entries/applications/$APP_ID.desktop"
# deepin 应用商店对图标尺寸有校验，上游 hicolor 里 16x16→512x512 加 scalable
# 的全套并非都能通过，所以只装 256x256 这一版。desktop 里 Icon=$APP_ID 由
# 图标主题按尺寸逐级回退，最终落在 256x256 上，缺其他尺寸不影响显示。
ICON_SRC="$ICONS_SRC/256x256/apps/$APP_ID.png"
[ -f "$ICON_SRC" ] || { echo "ERROR: 缺少 256x256 图标: $ICON_SRC" >&2; exit 1; }
mkdir -p "$APPID_DIR/entries/icons/hicolor/256x256/apps"
cp -a "$ICON_SRC" "$APPID_DIR/entries/icons/hicolor/256x256/apps/$APP_ID.png"
find "$APPID_DIR/entries/icons" -type f \( -name "*.png" -o -name "*.svg" \) | sort

# ------------------------------------------------- 3) deepin info + deb control
VARS="$STEPS/vars.json"
export PACKAGE_VERSION="$VERSION" PACKAGE_ARCH="$DEB_ARCH"
RENDER="$WORK/scripts/render-template.py"

python3 "$WORK/scripts/make-vars.py" "$VARS"
python3 "$RENDER" "$WORK/templates/info.in" "$APPID_DIR/info" --vars-file "$VARS"
if [ -n "$APP_CLI" ]; then
  python3 "$RENDER" "$WORK/templates/postinst.in" "$PKG_ROOT/DEBIAN/postinst" --vars-file "$VARS"
  python3 "$RENDER" "$WORK/templates/postrm.in" "$PKG_ROOT/DEBIAN/postrm" --vars-file "$VARS"
  chmod 755 "$PKG_ROOT/DEBIAN/postinst" "$PKG_ROOT/DEBIAN/postrm"
fi
find "$PKG_ROOT" -type d -exec chmod 755 {} \;
chmod 644 "$APPID_DIR/info" "$APPID_DIR/entries/applications/$APP_ID.desktop"

# Installed-Size 必须在 info / postinst 都落地之后再量。
INSTALLED_SIZE="$(du -sk "$APPID_DIR" | awk '{print $1}')"
export INSTALLED_SIZE
python3 "$WORK/scripts/make-vars.py" "$VARS"
python3 "$RENDER" "$WORK/templates/control.in" "$PKG_ROOT/DEBIAN/control" --vars-file "$VARS" --indent DESCRIPTION
chmod 644 "$PKG_ROOT/DEBIAN/control"

# --------------------------------------------------------------------- 4) deb
FILENAME="${APP_SLUG}_${VERSION}_${DEB_ARCH}.deb"
dpkg-deb --root-owner-group -Zxz -b "$PKG_ROOT" "$OUT_DIR/$FILENAME"
echo "== control =="
dpkg-deb -I "$OUT_DIR/$FILENAME" | sed -n '1,30p'
echo "== contents =="
dpkg-deb -c "$OUT_DIR/$FILENAME"
ls -lh "$OUT_DIR/"
