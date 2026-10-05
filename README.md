# AutoDL Aoao

轻量的 AutoDL 空卡监控工具。为指定实例或其宿主机定时检查空闲 GPU，满足条件时通过 163 邮箱提醒。**v0.4.2 提供可选自动抢卡开机、紧凑浅色客户端、Windows 托盘后台运行、Chrome 登录检测和命令行。**

源码支持 **Windows、macOS、Linux，Python 3.10 及以上**。监控核心只使用 Python 标准库，桌面界面使用 Tkinter；Chrome 登录连接额外使用一个小型 WebSocket 库。桌面下载包自带这些运行环境，无需安装 Python。项目不包含个人账号、机器 ID 或邮箱凭据。

## 能做什么

- 按实例 ID、实例完整名称监控，支持同时配置多个目标。
- 桌面界面中填写连接设置、选择账号已有实例，查看状态与活动记录。
- 点击“打开 Chrome”，在专用窗口登录 AutoDL，再点击“检测登录”自动读取并验证登录信息，无需手动复制 Token。
- 按当前账号已有实例的宿主机 ID 监控指定空卡数量。
- 默认每 60 秒检查；按所需 GPU 数量判断，支持多卡实例。
- 163 SMTP 加密发信，也可以配置其他支持 SSL / STARTTLS 的邮箱。
- 持续空闲只发一次；空卡被占后再次释放会重新提醒，默认两次成功邮件至少相隔 5 分钟。
- 提醒状态跨重启保存；临时网络故障自动退避重试，登录失效停止并提示。
- 默认只查询和提醒。可勾选自动开机，成功启动一台已有实例后停止抢卡；不创建、克隆或关机，不修改实例规格与计费方式。

**支持范围：** 查询当前账号的普通容器实例。机器模式从这些实例中读取宿主机数据；不是查询整个算力市场任意一台尚未关联实例的机器。Pro、弹性部署的官方开发者 API 使用不同接口，本版本不兼容。

## 桌面客户端快速开始

从 [GitHub Releases](https://github.com/2274168514/AutoDL-Aoao/releases/latest) 下载对应系统的完整压缩包。

### macOS：下载后打开应用

- Apple 芯片（M 系列）：选择文件名以 `macos-arm64.zip` 结尾的版本。
- Intel 芯片：选择文件名以 `macos-x64.zip` 结尾的版本。
- 完整解压，将其中的 `AutoDL Aoao.app` 拖到“应用程序”，再双击打开。无需 Python；首次使用需要在本机重新连接 Chrome 和填写邮箱，Windows 的凭据不会上传或同步。

可在苹果菜单的“关于本机”中查看芯片。Mac 版当前没有 Apple Developer ID 签名或公证；如果 macOS 阻止打开，请核对下载来源，再按系统提示到“系统设置 → 隐私与安全”允许打开这个应用，参见 [Apple 官方说明](https://support.apple.com/102445)。

### Windows x64：克隆后双击运行

本仓库根目录附带已构建的 Windows x64 客户端。**克隆仓库，或下载仓库 ZIP 后完整解压，双击根目录的 `AutoDL Aoao.exe` 即可打开**，无需安装 Python、pip 或构建工具。

```text
autodl-aoao/
├── AutoDL Aoao.exe
├── _internal/
├── licenses/
├── src/                     # 源码，普通使用无需操作
├── README.md
└── LICENSE
```

这是免安装版。**EXE 与 `_internal` 必须放在同一个目录，不要只下载或移动 EXE，也不要直接在压缩包内运行。** `licenses` 保留运行环境的许可说明。程序不会安装系统服务，也不会设置开机启动。

只想使用客户端，也可在 Releases 下载以 `windows-x64.zip` 结尾的桌面包，完整解压后运行其中的 EXE。`dist/` 是本地构建输出，不纳入 Git 仓库。macOS/Linux 无法运行 Windows EXE；Mac 请下载对应系统的包，Linux 按下文使用源码运行。

首次打开只显示界面，**不会自动联网、发送邮件或开始监控**。使用步骤：

1. 点击“打开 Chrome”，在专用窗口亲自完成 AutoDL 登录，再回到客户端点击“检测登录”。已登录时可以直接检测，无需重新打开窗口；验证成功后会显示实例列表。
2. 选择需要监控的实例并添加。也可以手工输入实例 ID、完整名称或机器 ID；机器目标必须指定需要的 GPU 数量。监控中也能追加目标，新增目标自动保存，并在当前轮次结束后的检查中生效。
3. 填写发件邮箱及 163 SMTP 授权码。授权码字段旁的“获取授权码”可打开网易邮箱，按底部提示进入设置获取。默认发给自己，其他收件人和 SMTP 参数放在“更多”中。
4. 可主动发送测试邮件确认邮箱配置，监控期间也能发送。点击“发给自己”或“改为发给自己”会将后续邮件的收件人设为当前发件邮箱；点击本身只更改设置。“更多”也保留只检查一次的入口，不会发送邮件。
5. 如需抢卡，勾选“自动开机”，再点击“开始监控”。未勾选时只提醒；勾选后只在本轮生效，启动一台就停止抢卡。默认每 60 秒检查，持续空闲只提醒一次。Chrome 操作期间主按钮变为“取消操作”。

登录信息和 SMTP 授权码默认只留在当前窗口；勾选“记住凭据”后才按下述方式保存。专用 Chrome 窗口可以在连接成功后关闭，监控不依赖 Chrome 持续运行。Chrome 自己保存的网页登录状态与客户端的“记住凭据”是两回事。

打开 Chrome、检测登录均不需要先填写邮箱；发送测试邮件也不需要先连接 AutoDL。手工 Token、子账号与 AppVersion 收在高级设置中。SMTP 授权码的获取方法见下文。

Windows 上点击右上角 **×** 会隐藏到系统托盘，当前监控继续运行。点击托盘中的 Labubu 图标可恢复窗口；图标也可能位于任务栏右侧的隐藏图标区域。右键图标选择“退出”，或进入“更多设置”选择“退出程序”，才会停止监控、等待正在进行的查询或邮件结束后完全退出。恢复隐藏窗口会继续原来的监控；完全退出后重新启动，需要主动点击“开始监控”。

托盘使用 Windows 系统接口，无额外运行依赖。macOS/Linux 暂用最小化到 Dock/任务栏的方式保留恢复入口；Windows 托盘不可用时也会使用最小化或保留窗口的后备方式。托盘注册失败不会把程序隐藏到无法找回的位置。

运行中追加目标和切换收件人保留原有提醒记录及冷却时间，无需重启监控。当前一轮已经开始的邮件仍使用该轮的收件设置。选择实例、发送测试邮件使用独立的短操作，不中断监控；短操作进行时会防止重复点击。其他配置与移除目标需停止监控后调整。

### 自动抢卡开机

开关默认关闭。每次需要抢卡时，先在停止监控的状态下勾选“自动开机”，再点击“开始监控”。打开客户端或保存设置不会自动开机；停止监控、重新打开客户端后需要重新勾选。

- 按目标列表顺序检查，只对当前主账号中明确关机、GPU 信息完整且空卡足够的普通容器实例发起开机。机器模式、子账号和 Pro 实例暂不支持自动开机；正在运行或启停中的实例不会被再次启动。
- 使用实例原有 GPU 数量和计费方式，按 AutoDL 原有规则产生费用，参见[官方计费说明](https://www.autodl.com/docs/price/)。不会新建实例、变更套餐或替你关机。
- 请求受理后继续查状态，确认以 GPU 模式运行才显示“已抢到”并发送“已自动开机”邮件。成功一台后本轮不再启动其他实例，普通监控继续；即使你随后手动关机，也不会在本轮重新开机。
- 如果请求超时或结果无法确定，显示“待核实”，只继续查询，不重复提交，也不会转而启动另一台。开机记录在发送请求前保存到本地，重启也保留未决记录；确认结果前请到 AutoDL 控制台查看实际状态。
- 新增实例、测试邮件和修改收件人仍可在监控期间使用，不会重新开启已完成的抢卡轮次。抢卡使用当前检查间隔，没有高频请求循环，空卡不保证一定抢到。

命令行配置也可显式设置 `"auto_start": true` 后运行 `run`；默认示例为 `false`。`list`、`check`、`validate` 和 `test-email` 都不会开机。命令行重启沿用已有抢卡轮次，桌面客户端的新一轮明确勾选可以重新授权，但不会清除上次结果未明的请求。

### 桌面配置与凭据保存

桌面配置保存在当前用户的配置目录，与程序文件分开，也与命令行的 `config.json`、`.env` 分开。改名为 AutoDL Aoao 后沿用原有配置目录，已有登录信息、邮箱设置和提醒记录继续使用：

| 系统 | 默认配置目录 |
|---|---|
| Windows | `%LOCALAPPDATA%\AutoDLGPUWatch` |
| macOS | `~/Library/Application Support/AutoDLGPUWatch` |
| Linux | `$XDG_CONFIG_HOME/AutoDLGPUWatch`，未设置时为 `~/.config/AutoDLGPUWatch` |

“记住凭据”**默认关闭**。关闭时，Token 与授权码只留在当前窗口内，保存设置不会把它们写入普通配置。取消勾选并再次保存，会移除先前保存的凭据文件。

### Chrome 连接如何工作

需要本机安装 Google Chrome。客户端使用上述配置目录下的 `chrome-session` 作为专用浏览器资料目录，第一次需要重新登录；**不会复用或扫描日常 Chrome 的默认资料目录**。这是 Chrome 当前支持的隔离连接方式，参见 [Chrome 远程调试目录限制说明](https://developer.chrome.com/blog/remote-debugging-port)。

“打开 Chrome”只负责准备专用窗口；“检测登录”短时检查现有窗口，不另开窗口。检测时程序连接本机回环地址上的临时调试端口，只从 `https://www.autodl.com` 页面读取 `localStorage` 中的 `token`，然后向 AutoDL 验证并查询实例。它不读取浏览器密码库、Cookies 或其他网站的登录信息，不自动输入密码、处理验证码或创建实例。获取完成后会断开调试连接；Chrome 专用窗口由你自行关闭。

登录失效时点击“打开 Chrome”，在专用窗口重新登录后再点“检测登录”。未开窗口、未登录、页面加载中和 API 验证失败会给出具体提示。未安装 Chrome 时，可安装 Chrome 后重试，也可以展开手工连接输入 Token。专用浏览器资料可能含网页登录状态，不应分享；它在用户配置目录，不在发布包内。

主动开启后，Windows 使用当前账户的 **DPAPI 加密**保存凭据；macOS / Linux 使用权限为 **0600 的本地明文文件**，界面会明确标注。后者限制文件访问权限，但不对文件内容加密。配置目录可能包含已保存的凭据，请勿作为客户端分发包的一部分分享。Windows 凭据文件不能作为可跨电脑、跨账户使用的配置备份。

### 从 Python 源码打开桌面界面

下载源码并进入项目目录，先安装桌面连接依赖，再运行：

```sh
python -m pip install ".[desktop]"
python run.py gui
```

macOS / Linux 通常使用：

```sh
python3 -m pip install '.[desktop]'
python3 run.py gui
```

需要 Python 3.10 及以上，并带有 Tk 支持。Windows 与 macOS 的 Python 安装应包含 Tkinter；Linux 发行版可能需要单独安装对应的 Tk 包，例如 Debian / Ubuntu 的 `python3-tk`。可用 `python -m tkinter`（或 `python3 -m tkinter`）检查当前解释器是否能打开 Tk 窗口。

也可选择安装项目入口：

```sh
python -m pip install ".[desktop]"
autodl-aoao-gui
```

桌面自动连接使用 `websocket-client`，桌面包已包含它。命令行与手工 Token 路径仍无第三方运行依赖。安装源码可能需要下载构建工具 setuptools。

## 命令行快速开始

下载源码并进入项目目录。Windows 使用 `python`；macOS / Linux 通常使用 `python3`。下面以 `python` 为例。

```sh
python run.py init
```

编辑生成的 `config.json`，最少修改三个位置：

```json
{
  "targets": [
    {"kind": "instance", "value": "你的实例ID或完整名称", "min_gpus": null}
  ],
  "email": {
    "sender": "你的邮箱@163.com",
    "recipients": ["接收提醒的邮箱@example.com"]
  }
}
```

以上是可直接使用的最小配置。完整配置见 [config.example.json](config.example.json)。`min_gpus: null` 表示使用该实例原本配置的 GPU 数量。建议优先填写实例 ID，避免重名；程序遇到名称歧义会报告未知，不会猜测。

在本地终端输入凭据，输入过程不回显：

```sh
python run.py secrets
```

它会创建 `.env`，保存 `AUTODL_TOKEN` 和 `SMTP_PASSWORD`。文件不会被自动加载，运行时明确使用 `--env-file .env`。已有文件不会被覆盖。

```sh
# 只检查配置，不联网
python run.py validate

# 只读列出实例、名称及宿主机 ID，不发邮件
python run.py --env-file .env list

# 实际查询一次，不发邮件、不修改提醒状态
python run.py --env-file .env check

# 主动发送一封测试邮件
python run.py --env-file .env test-email

# 开始监控，第一次发现足够空卡就发信
python run.py --env-file .env run
```

按 `Ctrl+C` 停止。所有全局参数（`--config`、`--env-file`）放在子命令前面。

macOS 示例：

```sh
python3 run.py --env-file .env run
```

也可安装命令行入口：

```sh
python -m pip install .
autodl-aoao --env-file .env run
```

直接用 `run.py` 无需安装依赖。`pip install .` 可能需要下载构建工具 setuptools。

## 凭据怎么获取

### AutoDL

本版本使用 **AutoDL 网页登录 Token**。自行登录 AutoDL 后，在浏览器开发者工具的“应用 / 存储 → 本地存储 → https://www.autodl.com”中查找 `token`，仅在自己电脑上填入。它不是控制台另外提供的“开发者 Token”。

账号退出登录、令牌过期或平台修改接口时，可能需要更新令牌。子账号填写 `autodl.sub_account`；若平台要求 AppVersion，可在 `autodl.app_version` 填入当前网页对应版本。

### 163 邮箱

在自己的 163 网页邮箱中开启 SMTP 服务，获取**客户端授权码**。`SMTP_PASSWORD` 填授权码，不是邮箱网页登录密码。

默认服务器为 `smtp.163.com`，端口 `465`，`security` 为 `ssl`。发件邮箱和登录邮箱相同；接收提醒的地址可以是任意邮箱，也可以与发件邮箱相同。

其他邮箱可配置 `host`、`port` 和 `security: "starttls"`。两种模式都验证服务器证书，不支持未加密 SMTP。

`.env` 是**本地明文文件**，不是保险箱；macOS / Linux 创建时权限为仅当前用户可读写，Windows 使用所在目录的权限。不要分享、上传或提交它。也可以完全不用凭据文件，通过系统环境变量提供两个值；已有环境变量优先于 `--env-file`。

## 配置说明

| 配置 | 默认值 | 用途 |
|---|---|---|
| `targets` | 必填 | 一个或多个目标 |
| `poll_seconds` | 60 | 正常查询间隔，最小 15 秒 |
| `timeout_seconds` | 20 | 单次 AutoDL 请求超时 |
| `cooldown_seconds` | 300 | 同一目标两次成功提醒的最小间隔 |
| `state_file` | `state.json` | 提醒状态，路径相对配置文件所在目录 |
| `autodl.token_env` | `AUTODL_TOKEN` | 存放网页登录令牌的环境变量名 |
| `autodl.sub_account` | 空 | 子账号名称 |
| `autodl.app_version` | 空 | 可选的网页 AppVersion |
| `email.password_env` | `SMTP_PASSWORD` | 存放 SMTP 授权码的环境变量名 |
| `email.timeout` | 20 | SMTP 超时秒数 |

宿主机模式示例：

```json
{"kind": "machine", "value": "通过list命令得到的machine_id", "min_gpus": 2}
```

机器模式必须显式填写需要的卡数。若账号没有该机器上的实例，或同一机器的数据不一致，结果为“未知”。

实例模式在“已关机”或“运行中且无卡模式”时判断是否足够开机；已经带 GPU 运行的实例不会发送空卡提醒。宿主机模式直接检查关联实例返回的宿主机空闲数。

## 原理与边界

程序在每轮查询 AutoDL 实例列表，匹配目标，再比较 `gpu_idle_num` 和所需卡数。普通账号请求 `https://www.autodl.com/api/v1/instance`，子账号请求 `/api/v1/sub_user/instance`。分页读取，错误及缺失数据不会当作“没有空卡”或“空卡已释放”。

这是网页使用的接口，**不是有稳定性承诺的官方开发者 API**。项目参考了公开项目对接口字段的说明并独立实现：

- [AutoDL GPU Availability Monitor 的接口实现](https://github.com/Kenny-Huang-mz/autodl-gpu-availability-monitor/blob/main/background.js)
- [AutoDL 对实例和宿主机的说明](https://www.autodl.com/docs/env/)

不需要浏览器保持打开，但电脑和程序必须保持运行、有网络。睡眠、关机或终止进程时不检测；轮询间隙释放又被抢走的卡可能检测不到。提醒不代表 GPU 已被预留。

持续空闲不会每轮发邮件，重启也不会重复提醒。检测到不足后重新进入等待；再次足够但仍在冷却期内，会等冷却结束并确认仍足够后发送。网络错误、字段缺失、目标未找到不会重置去重状态。

同一轮多个目标可用会合并一封邮件。SMTP 报错会重试；部分收件人已收信而其他收件人失败时，会报告失败，重试可能让已接收的人收到重复邮件。SMTP 和本地状态不是同一个事务，因此程序在邮件已被服务器接收、但状态尚未落盘的极短时间内意外中断，也可能在重启后重复提醒，不能承诺严格“恰好一次”。

登录失效会停止监控并显示原因。状态文件损坏时会保留备份并停止，避免悄悄重置后重复提醒；检查备份后可修复或手动移走状态文件。重复运行同一状态文件会被文件锁阻止。

接口请求拒绝重定向，避免把认证头带往其他站点。邮件仅发往配置的收件人。日志不输出 Token、SMTP 授权码或服务器原始错误正文。

## 后台运行

先在前台完成 `check` 和 `test-email`。长期运行时可交给 Windows 任务计划程序、macOS launchd 或 Linux systemd 管理；Python 路径、`--config` 和 `--env-file` 均使用绝对路径。项目不会自行安装系统服务或修改开机启动。

退出码：`0` 正常退出；`2` 配置、认证、邮件或本地运行错误；`check` 返回 `3` 表示至少一个目标状态未知。无空卡属于正常状态。

## 开发与验证

源码位于 `src/autodl_gpu_watch`：`gui.py` 负责 Tkinter 界面，`desktop.py` 负责后台任务与桌面配置，`api.py` 负责查询与评估，`monitor.py` 负责轮询/状态/文件锁，`notifier.py` 负责邮件，`config.py` 和 `cli.py` 负责配置和命令行。桌面网络工作在后台线程运行，界面通过事件队列更新；运行中的任务使用启动时的配置。

源码包仅包含代码、说明和示例配置。桌面包仅包含应用运行文件和允许分发的说明，不包含真实 `.env`、`config.json`、桌面配置、凭据、提醒状态或日志。

核心逻辑、Chrome 连接协议和桌面交互已进行离线验证。0.3.2 紧凑界面通过三种缩放比例、两种窗口尺寸的布局检查，以及操作切换、弹窗生命周期和凭据处理检查。**已用本机已登录的专用 Chrome 验证登录检测、真实 AutoDL 请求及实例读取；163 SMTP 投递仍待使用者确认。** Windows x64、macOS arm64/x64 的 GitHub 原生构建及压缩包检查已通过，Mac 应用额外通过签名完整性与架构检查；Mac 图形交互和真实账号行为仍需用户本地确认。构建成功不代表已验证邮件送达或其他账号、系统与网络环境。

具体记录与资源采样见 [VALIDATION.md](VALIDATION.md)。GitHub Actions 在提交后执行 Windows / macOS / Linux、Python 3.10 / 3.14 安装与配置检查；各次结果可在仓库 Actions 页面查看。

生成不含凭据的源码压缩包：

```sh
python tools/package.py
```

该脚本生成的是开发用源码包，不包含 EXE 和运行环境；普通 Windows 用户使用完整仓库或桌面分发包即可。

### 构建桌面分发包

图标原图保存在 `assets/labubu.jpg`。Windows EXE 使用 `assets/app.ico`，macOS 使用 `assets/app.icns`，窗口图标使用随 Python 包分发的 `src/autodl_gpu_watch/assets/app.png`。这些资源已生成，正常构建和运行不需要 Pillow。更换原图后可用 `python -m pip install ".[icons]"` 安装图标生成工具依赖，再运行 `python tools/make_icon.py` 重新生成。

在目标系统上，使用带 Tk 的 Python 安装构建依赖后执行：

```sh
python -m pip install PyInstaller==6.18.0 ".[desktop]"
python tools/build_desktop.py
```

PyInstaller 只用于构建，不是应用的运行时依赖。默认输出到 `dist/desktop/`：

- Windows：`AutoDL Aoao/AutoDL Aoao.exe` 及配套 `_internal` 目录，另生成完整 ZIP。
- macOS：包含 `AutoDL Aoao.app` 的应用目录与 ZIP。
- Linux：包含可执行文件和运行库的应用目录与 `.tar.gz`，保留可执行权限。

构建使用文件夹模式，不是启动时临时解压的单文件模式。脚本不会覆盖已有输出；再次构建时可指定新目录：

```sh
python tools/build_desktop.py --output-dir dist/desktop-next
```

首次准备可直接运行的 Git 仓库时，将经过验证的 Windows 分发包同步到项目根目录：

```sh
python tools/prepare_portable.py --from-dir "dist/release-0.4.2/AutoDL Aoao"
```

同步工具只复制 `AutoDL Aoao.exe`、`_internal/` 和 `licenses/`，不会复制个人配置。根目录已有这些文件时会拒绝覆盖；更新客户端应先退出正在运行的根目录版本，再由维护者替换整套运行文件。提交仓库时必须同时包含这三个路径，不能只提交 EXE。`.gitattributes` 保持运行文件的原始字节；`dist/`、构建缓存和个人配置保持忽略。

源代码修改后需要重新构建并更新根目录客户端，避免仓库中的 EXE 落后于源码。仓库使用者直接启动 EXE，无需运行上述维护命令。

脚本会一并收集当前 Python 运行环境的原始许可证。如果所用 Python 发行版没有安装该文件，构建会提示使用 `--python-license` 指定与该运行环境对应的原始许可证文件。

Windows 包在 Windows 构建，macOS 包在 macOS 构建，Linux 包在 Linux 构建；不支持在一个系统上直接交叉生成其他系统的客户端。打包架构跟随构建机器。`desktop-build.yml` 可手动构建；推送与项目版本一致的 `v` 标签时，在各系统的原生 runner 构建并发布 GitHub Release，提供 Windows x64、macOS arm64/x64 下载包与源码 ZIP。Linux 可使用源码或在本机打包。

## 许可

[MIT](LICENSE)。第三方非官方工具，与 AutoDL、网易无隶属关系。
