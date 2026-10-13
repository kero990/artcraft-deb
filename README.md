# artcraft-deb

为 ArtCraft 全家桶（[getartcraft.com/apps](https://getartcraft.com/apps)）编译 **deepin 格式 deb** 的自动化仓库。
12 个 app 全部是 Rust workspace，统一分成 `crates/*` + `apps/<app>` + `apps/<app>-cli` + `apps/<app>-web`，
所以上游仓库结构、官方 deb 命名（`<app>-<版本>-linux-{x86_64,aarch64}.deb`）、app id（`ai.storyteller.<app>`）
都高度一致，本仓库因此做到**一份流程脚本 + 12 份配置**。

| app | 上游仓库 | 官网页面 |
|---|---|---|
| photocraft | [storytold/photocraft](https://github.com/storytold/photocraft) | <https://getartcraft.com/apps/photocraft> |
| vectorcraft | [storytold/vectorcraft](https://github.com/storytold/vectorcraft) | <https://getartcraft.com/apps/vectorcraft> |
| filmcraft | [storytold/filmcraft](https://github.com/storytold/filmcraft) | <https://getartcraft.com/apps/filmcraft> |
| lightcraft | [storytold/lightcraft](https://github.com/storytold/lightcraft) | <https://getartcraft.com/apps/lightcraft> |
| pdfcraft | [storytold/pdfcraft](https://github.com/storytold/pdfcraft) | <https://getartcraft.com/apps/pdfcraft> |
| effectcraft | [storytold/effectcraft](https://github.com/storytold/effectcraft) | <https://getartcraft.com/apps/effectcraft> |
| designcraft | [storytold/designcraft](https://github.com/storytold/designcraft) | <https://getartcraft.com/apps/designcraft> |
| wordcraft | [storytold/wordcraft](https://github.com/storytold/wordcraft) | <https://getartcraft.com/apps/wordcraft> |
| soundcraft | [storytold/soundcraft](https://github.com/storytold/soundcraft) | <https://getartcraft.com/apps/soundcraft> |
| deckcraft | [storytold/deckcraft](https://github.com/storytold/deckcraft) | <https://getartcraft.com/apps/deckcraft> |
| cadcraft | [storytold/cadcraft](https://github.com/storytold/cadcraft) | <https://getartcraft.com/apps/cadcraft> |
| gridcraft | [storytold/gridcraft](https://github.com/storytold/gridcraft) | <https://getartcraft.com/apps/gridcraft> |

## 为什么要重编

上游官方 deb 基于 Ubuntu 22.04 (glibc 2.35) 编译，在 UOS / Deepin V20 (glibc 2.28) 上直接起不来。
本仓库在 `python:3.11-buster`（glibc 2.28）容器里从源码重编，并在打包前用 `objdump -T` 校验二进制
需要的最高 `GLIBC_*` 符号，超了就直接判失败。

amd64 用 `ubuntu-latest`，arm64 用 `ubuntu-22.04-arm`，两个架构都原生编译（不做交叉编译）。

## 每日更新（update.yml）

```
schedule (每天 UTC 02:23)
   │
   ├─ check    比对上游 latest release 与上次编译记录（滚动 release 里的 versions.json）
   │           在 job summary 里输出对照表：上次编译 / 上游最新 / 状态
   │
   ├─ build    只对“有更新”的 app 跑矩阵（复用 build-app.yml，amd64 + arm64）
   │           没更新的 app 不进矩阵，直接沿用 release 里已有的 deb
   │
   └─ publish  把新 deb 合并进唯一的滚动 latest release：
               · 有更新的 app：删旧 deb 资产 → 上传新 deb → 更新 versions.json
               · 无更新的 app：资产原样保留，不重复上传
               · 删除除 latest 之外的所有历史 release 及其 tag
               · 本次有更新的 deb 额外上传到 pCloud WebDAV
```

也就是说仓库里**永远只有一个 release**（tag `latest`），里面永远是 12 个 app × 2 架构的最新 deb，
外加一个 `versions.json` 记录每个 app 当前打包的上游版本，方便任何地方做版本对比。

手动跑 `update.yml` 时可以：

- `force = true`：忽略版本状态，12 个 app 全部重编
- `apps = photocraft,gridcraft`：只处理指定的 app

## 单个 app 的编译（12 个薄封装）

每个 app 都有一个同名 workflow（`photocraft.yml`、`vectorcraft.yml` …），
都是对 `.github/workflows/build-app.yml` 的薄封装，用于**单独重编某一个 app**：

- `version`：留空取上游最新 release，也可以填 `0.9.0` 之类的版本覆盖值
- `publish`：是否把结果合并进滚动 latest release（默认是）

## 目录结构

```
apps/<app>.yml          每个 app 的打包配置（名称、仓库、appid、section、特性、
                        额外依赖、deepin 权限、deb 描述）
templates/              deepin 控制文件模板
  control.in            deb control（占位符 {{...}}）
  info.in               /opt/apps/<appid>/info（deepin 权限）
  postinst.in           把 <app>-cli 链接进 /usr/bin
  postrm.in             卸载时清理
scripts/
  appcfg.py             读取 apps/<app>.yml，导出 shell 变量 / JSON
  check-updates.py      版本比对，输出构建矩阵 + 对照表
  build-app.sh          容器内：取源码 → cargo build → strip → glibc 校验
  package-app.sh        组装 /opt/apps 布局 → 渲染模板 → dpkg-deb
  render-template.py    模板渲染
  publish-release.py    维护唯一的滚动 latest release + 清理历史 release
.github/workflows/
  update.yml            每日检查 → 构建有更新的 → 发布
  build-app.yml         可复用的单 app 编译流程（workflow_call + workflow_dispatch）
  <app>.yml             12 个手动触发的薄封装
```

## 打包布局（deepin 格式）

```
/opt/apps/ai.storyteller.<app>/
├── info                                 # deepin 应用信息（版本、架构、权限）
├── files/<app>, files/<app>-cli         # 二进制
└── entries/
    ├── applications/ai.storyteller.<app>.desktop
    └── icons/hicolor/…/apps/ai.storyteller.<app>.png
/usr/bin/<app>-cli                       # postinst 创建的启动脚本，postrm 删除
```

桌面文件与图标取自上游同版本的官方 deb（架构无关），取不到时退回源码树；
`Exec`/`TryExec` 一律改写成 `/opt/apps/...` 下的绝对路径，否则 deepin 启动器起不来。

## 新增一个 app

上游出新 app 时只需两步：

1. `apps/<新app>.yml` 抄一份最近的配置，改 `name/repo/appid/slug/homepage/section/description`
   （`cargo_pkgs`、`binaries`、`cli` 默认就是 `<app>` / `<app>-cli`，一般不用写）
2. `.github/workflows/<新app>.yml` 抄一份薄封装，把 `app:` 改掉

`check-updates.py`、`appcfg.py` 都是扫描 `apps/*.yml` 的，改完即可生效。

## 依赖 Secrets

| secret | 用途 |
|---|---|
| `WEBDAV_USERNAME` / `WEBDAV_PASSWORD` | pCloud WebDAV（`bxb100/action-upload-webdav`） |

这两个 secret 没配时流程不会失败，只会在发布作业里提示“跳过 WebDAV 上传”，
deb 仍然会在 `latest` release 里。`GITHUB_TOKEN` 由 Actions 自动提供，权限只要
`contents: write`（创建/编辑 release）即可。

## 本地调试

```bash
# 看某个 app 的完整配置
python3 scripts/appcfg.py photocraft --json

# 只做版本比对（需要 GH_TOKEN 避免限流）
GH_TOKEN=xxx python3 scripts/check-updates.py --state versions.json --out matrix.json

# 在 Debian 10 容器里完整跑一次（本仓库脚本和容器环境一致，可直接跑）
docker run --rm -it -v "$PWD":/w -w /w python:3.11-buster bash -c '
  sed -i -e "s|deb.debian.org|archive.debian.org|g" -e "/buster-updates/d" /etc/apt/sources.list
  apt-get update && apt-get install -y build-essential pkg-config binutils file dpkg-dev \
    curl git ca-certificates python3-yaml libxkbcommon-dev libwayland-dev libx11-dev \
    libxrandr-dev libxi-dev libgl1-mesa-dev
  curl --proto =https --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal
  PATH=$HOME/.cargo/bin:$PATH ./scripts/build-app.sh photocraft 0.8.2 amd64 x86_64
'
```
