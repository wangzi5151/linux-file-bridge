# Linux File Bridge · Linux 文件互通器

[![CI](https://github.com/wangzi5151/linux-file-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/wangzi5151/linux-file-bridge/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.8%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

一个零第三方依赖的小工具，让**手机端应用集成的 agent** 在任意一个 Ubuntu / proot / chroot / Termux / WSL 环境里，安全地读写**其他环境**和 Android 共享目录中的项目文件。

> 典型场景：PocketDev 之类的手机端 agent 运行在 Ubuntu 20.04 proot 中，项目却散落在另一个 Ubuntu 容器、Termux 的 `~/storage/shared` 或局域网机器上。为每个环境启动一个轻量 bridge，再用一个 MCP / HTTP 入口聚合，agent 就能用统一路径访问全部项目。

---

## 目录

- [为什么需要它](#为什么需要它)
- [特性](#特性)
- [架构](#架构)
- [快速开始（Android + Ubuntu proot）](#快速开始android--ubuntu-proot)
- [三种使用方式](#三种使用方式)
  - [1. MCP：给应用内 agent 用](#1-mcp给应用内-agent-用)
  - [2. HTTP Gateway：给 HTTP 工具用](#2-http-gateway给-http-工具用)
  - [3. CLI：同步和日常操作](#3-cli同步和日常操作)
- [多环境拓扑示例](#多环境拓扑示例)
- [MCP 工具](#mcp-工具)
- [CLI 速查](#cli-速查)
- [HTTP API](#http-api)
- [配置文件](#配置文件)
- [安全模型](#安全模型)
- [Android / proot / Termux 注意事项](#android--proot--termux-注意事项)
- [开发与测试](#开发与测试)
- [限制与路线图](#限制与路线图)
- [English](#english)
- [License](#license)

---

## 为什么需要它

在手机上，agent 往往只能看到自己所在的那个 proot 根文件系统：

- 另一个 Ubuntu 环境里的项目路径不可见；
- Termux 的 `/sdcard` / `~/storage/shared` 没有挂载进 Ubuntu；
- 想让 agent “读一下另一个容器里的配置” 或 “把代码同步到另一个环境”，只能手工 `adb`、`scp` 或复制粘贴；
- 手机端应用通常只支持 **MCP** 或 **HTTP**，没有现成的跨环境文件工具。

Linux File Bridge（命令名 `lfb`）只做一件事：**把“某个环境里的某个目录”暴露成一个带 token 的、路径安全的文件 API，再把这些环境聚合成一个 agent 可用的工具面。**

它不依赖 FUSE、不要求 root、不要求 systemd，也不需要安装 aiohttp/Flask 等第三方包。

## 特性

- **零第三方依赖**：只使用 Python 3.8+ 标准库，能直接在 proot Ubuntu / Termux 中运行。
- **单环境 Bridge**：`lfb serve` 暴露若干命名 root，支持列表、读、写、下载、上传、搜索、树、原子替换、Range 下载。
- **多环境 Gateway**：`lfb gateway` 用 `?env=` 或 `X-LFB-Env` 选择环境；本地环境进程内处理，远程 bridge 流式反向代理。
- **MCP stdio 服务器**：`lfb mcp` 直接作为手机应用的 MCP server，提供 11 个文件/命令工具，agent 无需自己发 HTTP。
- **路径安全**：拒绝 `..`；所有符号链接目标都必须在 root 内；逃逸链接在列表中标记 `blocked` 且不可读。
- **Token 认证**：Bearer token，常量时间比较；配置以 `0600` 保存。
- **读只读 root**：`"mode": "ro"` 的 root 在服务端强制只读。
- **默认本机监听**：默认 `127.0.0.1`，不主动暴露公网；TLS 可选。
- **CLI 互通**：`ls/cat/get/put/sync/cp/find` 可在任意两个环境之间复制、镜像和校验。
- **无守护进程依赖**：前台运行，适合 Termux、proot、`termux-services` 或自己的启动脚本。

## 架构

```text
┌────────────────────────────── Android 手机 ──────────────────────────────┐
│                                                                          │
│  ┌─────────────────────┐        MCP (stdio)       ┌───────────────────┐  │
│  │ 手机端应用 / Agent  │ ◄───────────────────────► │  lfb mcp          │  │
│  │ (PocketDev 等)      │                          │  (聚合器进程)     │  │
│  └─────────┬───────────┘                          └─────┬────────┬────┘  │
│            │ HTTP /v1/*  (可选)                          │        │       │
│            ▼                                              │        │       │
│  ┌─────────────────────┐                                 │        │       │
│  │  lfb gateway :8787  │ ────────────────────────────────┘        │       │
│  └─────────┬───────────┘        本地环境直接读写                  │       │
│            │ 反向代理 + 流式转发                                  │       │
│            ▼                                                      ▼       │
│  ┌─────────────────────┐        HTTP      ┌───────────────────────────┐  │
│  │ Ubuntu A (本环境)   │ ◄─────────────── │ lfb bridge :8790          │  │
│  │ /workspace/linux    │                  │ root: project=/workspace  │  │
│  └─────────────────────┘                  └───────────────────────────┘  │
│                                                                          │
│  ┌─────────────────────┐        HTTP      ┌───────────────────────────┐  │
│  │ Ubuntu B (另一容器) │ ◄─────────────── │ lfb bridge :8791          │  │
│  │ /workspace/other    │                  │ root: project=/workspace  │  │
│  └─────────────────────┘                  └───────────────────────────┘  │
│                                                                          │
│  ┌─────────────────────┐        HTTP      ┌───────────────────────────┐  │
│  │ Termux              │ ◄─────────────── │ lfb bridge :8792          │  │
│  │ ~/storage/shared    │                  │ root: shared=... (ro)     │  │
│  └─────────────────────┘                  └───────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────┘
```

- Android 上多个 proot 容器通常共享网络命名空间，`127.0.0.1:<port>` 可互通；如果不行，改用设备局域网 IP，并务必配合 token/TLS。
- 一个进程可以同时是“本地环境 + 聚合器”：例如在 Ubuntu A 的配置里，`ubuntu-a` 是 local，`ubuntu-b` 是 bridge；`lfb mcp` 会同时访问两者，无需先启动 gateway。
- Gateway 不是必需的；它只是给“只支持 HTTP 工具”的应用一个统一入口。

## 快速开始（Android + Ubuntu proot）

以下命令在项目根目录执行（示例路径为 `/workspace/linux`，请按实际路径替换）。

### 1. 安装

```bash
# Ubuntu 20.04 proot
sudo apt-get update && sudo apt-get install -y python3 python3-pip

# 从源码安装（推荐开发/手机场景）
python3 -m pip install --user -e .
export PATH="$HOME/.local/bin:$PATH"
lfb --version
```

> 不想安装也可以直接运行：`python3 -m lfb --help`。下面的 `lfb` 均可替换为 `python3 -m lfb`。

### 2. 为当前环境建配置

```bash
lfb init --name ubuntu-main --root project=/workspace/linux
# 输出 config 路径、root、gateway 端口和下一步提示
lfb env token ubuntu-main   # 打印这个环境的 bearer token
```

默认配置在 `~/.config/lfb/config.json`（可用 `LFB_CONFIG` 或 `--config` 覆盖），权限为 `0600`。

### 3. 启动当前环境的 bridge

```bash
lfb serve --env ubuntu-main
# lfb bridge 'ubuntu-main' listening on http://127.0.0.1:8790
```

验证：

```bash
TOKEN=$(lfb env token ubuntu-main)
curl -s -H "Authorization: Bearer $TOKEN" \
  'http://127.0.0.1:8790/v1/list?root=project&path='
```

### 4. 在另一个 Ubuntu 环境里启动第二个 bridge

进入另一个 proot / 终端，安装同一份代码后：

```bash
lfb init --name ubuntu-dev --root project=/workspace/other --gateway-port 8788
# 修改配置中的 serve.port，或启动时显式指定：
lfb serve --env ubuntu-dev --port 8791
lfb env token ubuntu-dev
```

### 5. 在 agent 所在环境聚合

回到 `ubuntu-main`，把 `ubuntu-dev` 加成一个 bridge 环境：

```bash
lfb env add ubuntu-dev \
  --url http://127.0.0.1:8791 \
  --token '<ubuntu-dev 的 token>'

lfb env list
lfb ls ubuntu-dev:project
```

现在 `lfb` 已经可以跨环境操作了。把它接到 agent：

```bash
lfb mcp
```

把 [`examples/mcp.json`](examples/mcp.json) 合并进手机应用的 MCP 配置即可（把 `command` 改成 `lfb` 的绝对路径，`args` 中的配置路径改成绝对路径）。应用中的 agent 会看到 `lfb_list_envs`、`lfb_read`、`lfb_write` 等工具。

## 三种使用方式

### 1. MCP：给应用内 agent 用

启动（应用会替你启动，通常不需要手动运行）：

```bash
lfb mcp
```

应用配置示例（`command` 用 `which lfb` 的绝对路径）：

```json
{
  "mcpServers": {
    "linux-file-bridge": {
      "command": "/usr/local/bin/lfb",
      "args": ["--config", "/root/.config/lfb/config.json", "mcp"]
    }
  }
}
```

如果 `lfb` 不在 PATH 中，可用：

```json
{
  "mcpServers": {
    "linux-file-bridge": {
      "command": "/usr/bin/python3",
      "args": ["-m", "lfb", "--config", "/root/.config/lfb/config.json", "mcp"]
    }
  }
}
```

MCP 协议细节见 [docs/API.md](docs/API.md#mcp)。服务器只把 JSON-RPC 写到 stdout，日志全部走 stderr。

### 2. HTTP Gateway：给 HTTP 工具用

```bash
lfb gateway            # 默认 127.0.0.1:8787
```

聚合后的 API 与单环境 bridge 完全一致，只是多一个环境选择器：

```bash
TOKEN=$(python3 -c 'import json; print(json.load(open("/root/.config/lfb/config.json"))["gateway"]["token"])')

curl -s -H "Authorization: Bearer $TOKEN" \
  'http://127.0.0.1:8787/v1/list?env=ubuntu-dev&root=project&path='

curl -s -H "Authorization: Bearer $TOKEN" \
  -H 'X-LFB-Env: ubuntu-dev' \
  'http://127.0.0.1:8787/v1/read?root=project&path=README.md'
```

`lfb gateway` 对 local 环境直接处理，对 bridge 环境做流式反向代理（上传/下载不会整体读进内存）。

### 3. CLI：同步和日常操作

```bash
lfb ls ubuntu-dev:project
lfb cat ubuntu-dev:project/README.md
lfb find ubuntu-dev:project --glob '*.py' --max 50
lfb get ubuntu-dev:project/src ./src -r
lfb put ./dist ubuntu-main:project/dist -r
lfb sync ubuntu-main:project ubuntu-dev:project --dry-run
lfb sync ubuntu-main:project ubuntu-dev:project --delete --allow-root-delete
```

路径语法：

```text
env:root/path
env:root            # root 本身
root/path           # 默认环境 + 指定 root
path                # 默认环境 + 默认 root
```

`sync` 是“把源目录内容镜像到目标目录”；`--delete` 会删除目标中多余的文件。目标为 root 本身时，`--delete` 必须额外加 `--allow-root-delete`，这是防止误删的保护。

## 多环境拓扑示例

### 方案 A：一个配置包含所有环境（推荐）

在 agent 所在环境维护一份配置：

```bash
lfb init --name ubuntu-main --root project=/workspace/linux
lfb env add ubuntu-dev --url http://127.0.0.1:8791 --token '...'
lfb env add termux-shared --root shared=$HOME/storage/shared --read-only
lfb mcp
```

`ubuntu-main` 和 `termux-shared` 由 `lfb` 直接读写；`ubuntu-dev` 通过 HTTP bridge。agent 只需连接一个 MCP 进程。

### 方案 B：每个环境独立配置 + Gateway

每个 Ubuntu 环境各自 `lfb init` 并 `lfb serve`；在手机某个环境里维护聚合配置并 `lfb gateway`。适合每个容器由不同人/脚本管理，或者应用只支持 HTTP 的场景。

### 方案 C：临时跨环境复制

不启动常驻服务，只在一台机器上运行 CLI：

```bash
lfb sync /path/a user@phone:/path/b   # 不支持 ssh 语法，请用下面的方式
lfb sync ubuntu-main:project ubuntu-dev:project
```

跨机器请用桥接环境（`lfb env add remote --url ...`），或先用 `get`/`put` 走现有 SSH/rsync。

## MCP 工具

| 工具 | 作用 | 关键参数 |
| --- | --- | --- |
| `lfb_list_envs` | 列出环境、root、只读/exec 状态 | — |
| `lfb_list` | 列目录；`depth>1` 时返回树 | `env`, `root`, `path`, `depth` |
| `lfb_tree` | 嵌套目录树 | `depth` |
| `lfb_stat` | 元数据、可选 SHA-256 | `hash` |
| `lfb_read` | 读 UTF-8 文本（偏移/上限） | `path`, `offset`, `limit` |
| `lfb_write` | 原子写文件；parent 自动创建 | `path`, `content`, `encoding` |
| `lfb_mkdir` | 建目录 | `path`, `parents` |
| `lfb_move` | 同环境移动/改名 | `src`, `dst` |
| `lfb_delete` | 删除文件/目录 | `recursive` |
| `lfb_search` | glob/regex/内容搜索 | `glob`, `regex`, `contains`, `exclude` |
| `lfb_exec` | 执行命令（默认关闭） | `argv` 或 `command`+`shell` |

所有工具都支持 `env`、`root`；`path` 也可以写成 `env:root/path`。二进制文件请用 HTTP `/v1/download` 或 CLI `lfb get`。

## CLI 速查

```text
lfb init        创建配置
lfb env         管理环境（list/add/remove/default/show/token）
lfb serve       单环境 HTTP bridge
lfb gateway     多环境聚合 HTTP 入口
lfb mcp         给应用 agent 的 MCP stdio 服务器
lfb health      检查环境连通性
lfb roots       列出 root
lfb ls/tree/stat/cat/find
lfb get/put     下载/上传（支持 -r）
lfb mkdir/mv/rm
lfb sync|cp     跨环境复制/镜像（--delete/--dry-run/--checksum）
lfb exec        执行命令（需配置开启）
lfb doctor      配置、权限、连通性诊断
```

`lfb exec` 的选项要放在 target 之前，命令放在 target 之后：

```bash
lfb exec --env ubuntu-main --shell project 'git status --short'
lfb exec --env ubuntu-main project -- git status --short
lfb exec --env ubuntu-main --timeout 10 project -- ls -la
```

`--shell` 需要配置里 `exec.allow_shell=true`；`exec` 整个功能需要 `exec.enabled=true`。默认关闭。

## HTTP API

所有响应使用统一信封：

```json
{"ok": true, "data": { ... }}
{"ok": false, "error": {"code": "not_found", "message": "path not found: ..."}}
```

认证：`Authorization: Bearer <token>`（也支持 `X-LFB-Token`）。`/v1/health` 不需要认证。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/v1/health` | 健康检查（无需认证） |
| GET | `/v1/roots` | 列出 root、limits、exec 状态 |
| GET | `/v1/list?root=&path=` | 目录列表 |
| GET | `/v1/stat?root=&path=&hash=1` | 文件元数据 |
| GET | `/v1/read?root=&path=&offset=&max=&binary=1` | 读文本（二进制需 `binary=1`） |
| GET | `/v1/download?root=&path=` | 原始下载，支持 `Range` |
| PUT | `/v1/write?root=&path=&overwrite=1&mkdir=1` | 原始上传（必须带 `Content-Length`） |
| POST | `/v1/mkdir` | JSON `{root,path,parents}` |
| POST | `/v1/move` | JSON `{root,src,dst,overwrite}` |
| POST/DELETE | `/v1/remove` | 删除 |
| GET | `/v1/search?root=&path=&glob=&regex=&contains=&type=&max=` | 搜索 |
| GET | `/v1/tree?root=&path=&depth=` | 目录树 |
| POST | `/v1/exec` | JSON `{root,path,argv|command,shell,timeout}` |
| GET | `/v1/envs` | 仅 gateway：列出所有环境 |

Gateway 额外支持 `?env=NAME` 和 `X-LFB-Env: NAME`；响应会回显 `X-LFB-Env`。

完整字段和示例见 [docs/API.md](docs/API.md)。

## 配置文件

默认路径：`~/.config/lfb/config.json`（`$LFB_CONFIG` 可覆盖）。最小示例：

```json
{
  "version": 1,
  "default_env": "ubuntu-main",
  "environments": {
    "ubuntu-main": {
      "kind": "local",
      "roots": {
        "project": {"path": "/workspace/linux", "mode": "rw"},
        "shared": {"path": "/root/storage/shared", "mode": "ro"}
      },
      "token": "请替换为随机 token",
      "serve": {"host": "127.0.0.1", "port": 8790},
      "limits": {"max_read": "1M", "max_upload": "512M", "max_search_results": 200},
      "exec": {"enabled": false, "allow_shell": false}
    },
    "ubuntu-dev": {
      "kind": "bridge",
      "url": "http://127.0.0.1:8791",
      "token": "另一个环境的 token",
      "timeout": 30
    }
  },
  "gateway": {
    "host": "127.0.0.1",
    "port": 8787,
    "token": "gateway token",
    "default_env": "ubuntu-main"
  }
}
```

- `kind`: `local`（直接读写本机）或 `bridge`（HTTP 客户端）。
- `roots`: 命名 root；`mode` 为 `rw` 或 `ro`；agent 只能看到这些目录。
- `read_only`: 环境级只读，等价于所有 root 只读。
- `limits`: 单次读取上限、上传上限、搜索上限；支持 `1M`/`512M` 写法。
- `exec`: 默认关闭；开启后以当前用户身份运行，**不是沙箱**。
- `serve`: 该环境 `lfb serve` 的默认 host/port，可选 `tls_cert`/`tls_key`。
- `gateway`: 聚合器监听地址和 token；`gateway.token` 为空时回退到默认环境的 token。
- `verify_tls`: bridge 默认校验；自签证书测试时可设 `false`（不推荐），或用 `--ca`。

用 `lfb doctor` 检查文件权限、root 是否存在、bridge 是否可达、token 是否缺失。

## 安全模型

请把它当成“带 token 的文件 API”，而不是 SSH 的替代品。

- **默认只监听 127.0.0.1**。需要跨设备时显式 `--host 0.0.0.0`，并务必使用 TLS + 强 token + 防火墙/VPN。
- **所有 root 之外的路径都拒绝**：`..` 直接报错；符号链接解析后必须在 root 内；逃逸链接在列表里显示为 `blocked`，且不可读、不可写。
- **Token 常量时间比较**；配置文件写入 `0600`，所在目录 `0700`。不要把配置提交到 Git。
- **原子写**：先写同目录临时文件、`fsync`、`rename`；不会出现半截文件。
- **只读 root 由服务端强制**，不是客户端的君子协定。
- **exec 默认关闭**；开启后命令以运行 `lfb` 的用户身份执行，没有容器隔离、没有 seccomp。不要对不可信客户端开放。
- **没有速率限制、没有多用户 ACL**。一个 token 等于该环境全部 root 的权限。需要多用户请每个用户单独跑一个 bridge/端口/token。
- HTTPS 使用标准库 `ssl`；证书和私钥路径在配置或命令行传入。自签证书需让客户端信任对应 CA。

更完整的威胁模型和部署建议见 [docs/SECURITY.md](docs/SECURITY.md)。

## Android / proot / Termux 注意事项

- **proot 没有 systemd**：用 `nohup`、`termux-services` 或 Termux:Boot 启动 `lfb serve`；前台运行时日志在 stderr。
- **端口**：不同 proot 进程共享 Android 网络栈，通常可 `127.0.0.1:8790/8791` 互通。绑定失败时先 `lfb doctor` 或换端口。
- **/sdcard 权限**：Android 11+ 对共享存储有限制；Termux 里先 `termux-setup-storage`，再把 `~/storage/shared` 配置成 root。Ubuntu proot 不一定能看到该路径，按实际挂载点配置。
- **PATH**：`pip install --user` 后 `~/.local/bin` 可能不在 PATH；用绝对路径或在应用 MCP 配置里写全路径。
- **Python 版本**：需要 3.8+；Ubuntu 20.04 自带的 3.8 足够。代码没有使用 3.9+ 语法。
- **不要用 `pgrep -f` 匹配自身命令来杀进程**：本项目的文档和脚本不使用这种方式；停止前台服务用 Ctrl-C，后台服务请记录 PID 或用 `termux-services`。
- **电池/休眠**：Android 可能冻结后台进程；需要常驻时给 Termux/应用加电池白名单。
- **后台启动示例**：`scripts/start-bridge.sh ubuntu-main` 用 `nohup` 启动并把 PID 写入 `~/.lfb/run/`；`scripts/stop-bridge.sh ubuntu-main` 只按 PID 文件停止，不会用 `pgrep` 搜索命令行，因此不会误杀当前终端。跨多个 proot 环境时，在每个环境分别运行。

## 开发与测试

```bash
git clone https://github.com/wangzi5151/linux-file-bridge.git
cd linux-file-bridge
python3 -m pip install -e .
make test          # 等价于 python3 -m unittest discover -v
```

测试覆盖：配置校验、路径穿越、符号链接逃逸、只读 root、原子读写、mtime 保留、Range 下载、HTTP 认证、Gateway 本地/代理、MCP 协议与工具、CLI、跨环境 sync/delete。

GitHub Actions 已启用，配置见 [`.github/workflows/ci.yml`](.github/workflows/ci.yml)：在 Python 3.8 / 3.9 / 3.10 / 3.11 / 3.12 / 3.13 上跑完整测试、CLI 冒烟、wheel/sdist 构建与干净虚拟环境安装，并启动真实 bridge 走一遍 health → list → `lfb ls` → `lfb cat` 的端到端冒烟。Fork 项目时可直接沿用；[`examples/github-actions-ci.yml`](examples/github-actions-ci.yml) 保留为精简模板。

目录结构：

```text
lfb/
  api.py        HTTP 处理器和单环境 server
  backends.py   local/bridge 统一后端
  cli.py        命令行
  client.py     流式 HTTP 客户端
  config.py     配置模型
  errors.py     错误码
  fsops.py      安全文件系统操作
  gateway.py    多环境反向代理
  mcp.py        MCP stdio 服务器
  targets.py    env:root/path 解析
  transfer.py   传输、镜像、删除
  util.py
tests/
examples/
docs/
```

欢迎 issue / PR。提交前请跑 `make test`，并保持零运行时依赖。

## 限制与路线图

当前版本（0.1.0）有意保持小：

- 不是 FUSE 挂载，没有内核级“把远程目录挂到本地”的能力；agent 通过工具/CLI 访问。
- 没有文件锁、冲突检测或版本历史；并发写以“最后写入者获胜”为准。
- 上传要求 `Content-Length`，不支持 chunked 请求体（本项目客户端始终发送长度）。
- 单个文本读取有上限（默认 1 MiB，MCP 默认 256 KiB）；大文件请分段或下载。
- 逃逸 root 的符号链接无法通过 `rm` 删除（安全优先）；需要时在宿主机处理。
- `sync` 跳过符号链接；不保留目录 mtime/ACL/xattr。
- 仅支持 Bearer token；没有 OAuth、ACL、审计日志。

可能的后续方向：可选的 audit log、文件锁/ETag 条件写、`rsync` 风格增量、MCP resources、HTTP MCP transport、Web UI。

## English

**Linux File Bridge** (`lfb`) is a tiny, dependency-free Python tool that lets an agent inside one Android Linux environment (Ubuntu proot/chroot, Termux, WSL) read and write project files in **other** environments and Android shared storage.

- Run `lfb serve` in each environment to expose named, path-confined roots over an authenticated HTTP API.
- Run `lfb gateway` to aggregate local and remote environments behind one endpoint (`?env=` / `X-LFB-Env`).
- Run `lfb mcp` to expose 11 file tools to a mobile app over MCP stdio.
- Use `lfb sync` / `lfb cp` for cross-environment mirroring, `lfb get/put` for transfers.

Security defaults: loopback bind, bearer auth, config `0600`, `..` and symlink-escape protection, read-only roots enforced server-side, command execution disabled.

Quick start:

```bash
python3 -m pip install --user -e .
lfb init --name ubuntu-main --root project="$PWD"
lfb serve --env ubuntu-main
# another terminal/app:
lfb mcp
```

Docs: [API](docs/API.md) · [Security](docs/SECURITY.md) · [Architecture](docs/ARCHITECTURE.md).

## License

MIT. See [LICENSE](LICENSE).
