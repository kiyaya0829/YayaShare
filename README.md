# YayaShare 🐾

在同一局域网里，把文字、剪贴板和文件从 MacBook 发到 Windows，也能反向发送。
无需账号、服务器或云存储。Python + PySide6，适合学习和继续维护。

## 下载与使用

在本仓库 **Releases** 下载对应 ZIP：

- **MacBook Air M3**：`YayaShare-macOS-arm64.zip`，解压后把 `YayaShare.app` 拖入「应用程序」。最低 macOS 13。
- **Windows x64 / R9000P**：`YayaShare-Windows-x64.zip`，完整解压后运行里面的 `YayaShare.exe`。保留同目录的 `_internal` 文件夹；不要单独移动 exe。推荐 Windows 11。
- 若尚无 Release：进入 **Actions → Build applications → 成功的运行 → Artifacts** 下载构建产物（需登录 GitHub）。Artifact 外层 ZIP 内还有应用 ZIP。

当前构建尚未使用 Apple Developer ID 公证或 Windows Authenticode 签名，系统可能提示无法验证开发者。只运行你确认来源和校验值的应用，不必关闭系统整体安全保护。组织电脑请遵循管理员要求。

1. 两台电脑连接同一 Wi-Fi / 局域网，并打开 YayaShare。允许应用在**专用网络**访问网络；TCP `45873` 用于加密传输，UDP `45874` 用于自动发现。
2. 在一台电脑点击 **生成我的配对码**，将完整配对码通过你信任的渠道带到另一台电脑。
3. 另一台电脑选中发现的设备，点击 **配对 / 手动连接**，粘贴配对码。只需配对一次，两边都会保存信任关系。
4. 选择已信任的设备，输入文字，或先点击 **读取剪贴板**，再点击 **发送文字**。
5. 点击 **选择文件并发送** 或 **选择文件夹并发送**。文字最大 64 KiB，文件或整个文件夹最大 2 GiB。先计算 SHA-256，再流式传输；进度条在开始发送后更新。
6. 在 **最近接收** 查看文字、文件和文件夹位置，或点击 **打开接收文件夹**。应用不会自动运行文件。

### v1.1：自动剪贴板与文件夹

两台电脑均升级到 v1.1，并分别开启 **Clipboard Sync**。在任意一台复制新文本，另一台即可直接粘贴。开关每次启动默认关闭，开启时已有的剪贴板不会发送；开启后的新文本会同步到**所有已配对设备**。仅同步最多 64 KiB 的文本；图片、文件列表和带文件 URL 的剪贴板不会同步。自动剪贴板内容不写入接收历史或磁盘。

接收和发送均受开关控制，关闭会清空排队更新；已进入网络传输的更新不能撤回。自动同步使用独立后台任务，Qt 主线程处理系统剪贴板，通过变化信号和 400ms 轮询检测后台复制。应用需保持运行，关闭窗口会退出；本版不含托盘常驻。离线设备会显示失败，不会反复重发旧剪贴板。同步期间复制的敏感文本也会传给可信设备，请按需要开关。

更新带唯一 ID，最近 512 个 ID 去重；收到内容先更新本地来源状态，再写系统剪贴板并读取规范化后的文本作为基准，避免回环。双方同时复制时可能各自收到对方最后一个更新；本版不保证分布式全局排序，但不会无限互传。最多缓存 64 个远端更新，仅保留最新待发送本地更新。

文件夹保留相对层级、中文名和空目录，每批放在独立随机接收目录。最多 2048 项、32 层、相对路径最多 512 UTF-8 字节。清理不兼容的文件名（例如 `CON.txt` → `_CON.txt`）；清理后重名、大小写/Unicode 重名会拒绝并提示先改名。拒绝绝对路径、上级跳转、链接与特殊文件；Windows 接收路径过长时提示缩短层级。所有文件逐个校验，完整成功后才显示接收记录；中断或校验失败会清理整个临时批次。

正式图标使用用户提供的原图，保持比例及完整画面，补白转换为 PNG、Windows ICO 与 macOS ICNS。窗口、任务栏/Dock 与打包应用使用相同图案。

配对码为 **64 个字符**，不是易猜的六位数字：它同时携带证书指纹和 128 位随机秘密，5 分钟过期，成功使用一次即失效。自动发现信息本身不可信，发送前始终校验证书。生成新配对码会立即替换旧码。

### 找不到另一台电脑？

- 自动发现大约需要 3–12 秒；电脑应保持唤醒、应用应保持打开。
- 在对方系统的 Wi-Fi 网络详情中找到局域网 IPv4（例如 `192.168.1.20`），点击 **配对 / 手动连接** 输入 IP 和端口。
- 已信任设备换 IP 后，可选择该设备，手动连接时填写新 IP、留空配对码以更新地址。
- 访客 Wi-Fi / 校园网可能启用设备隔离，同一 Wi-Fi 名称不代表设备能互通；可换自己的路由器或热点。
- 检查 VPN 和防火墙；macOS 在「系统设置 → 隐私与安全性 → 本地网络」中允许 YayaShare。无需配置路由器端口转发，不建议暴露到公网。
- `地址已被使用` 表示可能已运行另一个实例。一个数据目录只允许一个实例。

## 数据位置与信任

| 系统 | 数据目录 |
| --- | --- |
| macOS | `~/Library/Application Support/YayaShare` |
| Windows | `%LOCALAPPDATA%\YayaShare` |
| Linux（源码运行） | `~/.local/share/YayaShare` |

接收文件位于数据目录的 `Received/<随机批次>/<安全文件名>`。同名文件分别保存，不覆盖已有文件。历史只保留最近 100 条，**不会因此删除已接收文件**。文本历史、信任令牌和证书保存在本机，不上传云端；本地数据未进行静态加密，请保护你的系统账户和磁盘。

点击 **取消信任** 会阻止该设备后续发送；正在传输的连接可能继续完成。重新连接需重新配对。删除整个数据目录会重置身份与信任，也会删除其中接收文件，操作前请自行备份。

## 从源码运行

需要 Python 3.11+；项目构建与测试使用 Python 3.13。

```bash
git clone https://github.com/kiyaya0829/YayaShare.git
cd YayaShare
python -m venv .venv
```

macOS / Linux：

```bash
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m yayashare
```

Windows PowerShell（无需修改脚本执行策略）：

```powershell
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe -m yayashare
```

测试：

```bash
python -m pytest -q
python -m yayashare --smoke-test --no-discovery --port 0 --data-dir ./build/smoke-data
```

可使用不同 `--data-dir` 和 `--port` 在一台机器运行两个实例，并手动连接 `127.0.0.1` 验证双向传输。自动测试用隔离临时目录，不接触日常接收数据。

## 项目结构

```text
src/yayashare/
  __main__.py     启动参数、单实例锁
  gui.py          Qt Widgets 界面与后台任务
  discovery.py    UDP 设备发现（不参与身份认证）
  security.py     TLS 自签证书、指纹固定、一次性配对码
  protocol.py     JSON 消息帧、可信认证、文件流式传输
  storage.py      原子保存状态、历史记录、安全文件名
tests/           本机双端 TLS 集成测试、异常输入、GUI 测试
scripts/         打包与应用入口
.github/workflows/build.yml  双平台测试、打包、Release 草稿
```

## 协议与基础安全

- 传输层使用 Python 标准库 `socket`、`ssl`、`threading`，避免额外服务器框架；证书生成依赖 `cryptography`，网卡广播地址使用已有的 QtNetwork 查询。
- TCP 连接首先完成 TLS，校验配对码或已保存的 SHA-256 证书指纹，之后才能发送秘密或内容。
- 消息为 4 字节大端长度 + UTF-8 JSON，版本仍为 `1`；保留 `text` / `file` / `pair`，新增 `folder` 和认证后的 `capabilities`。自动剪贴板先确认 `clipboard-text-v1` 能力，再发送 `text` + `purpose=clipboard` + `mime=text/plain` + `update_id`，避免向旧版发送自动文本。手动文字和单文件继续兼容旧版。
- 文件使用 JSON 元信息 → ready 确认 → 原始字节流 → 最终确认；SHA-256 必须匹配后才记录成功。文件夹发送含路径、类型、大小和 SHA-256 的清单，ready 后按清单顺序发送文件字节，最后整体确认。剪贴板确认表示进入内存队列，随后由 GUI 写入系统剪贴板。
- 配对秘密只短期存于内存；信任令牌随机生成。只接受已信任设备发送。配对请求携带发起方证书指纹，从而建立双向信任。
- 拒绝过大的消息与文件，最多 8 条入站连接，有连接、读取和文件总时限。传输使用固定大小缓冲区。
- 路径被降为文件名，清理 Windows 特殊字符、保留名、控制字符和尾部点空格，限制 UTF-8 文件名长度。接收方创建随机子目录，以独占方式写临时文件，成功后改名；中断或校验失败会清理临时文件。
- UDP 广播只含设备名、随机 ID、证书指纹与端口，局域网其他人能看见这些发现信息。没有遥测。
- MVP 不包含断点续传、传输取消、单次接收确认、静态数据加密或专业安全审计。可信设备可在应用打开时发送内容，因此仅与自己的可信设备配对。

## 构建与 Release

```bash
python -m pip install -e '.[dev]'
python scripts/build.py
```

打包必须在目标系统进行。macOS arm64 使用 Apple Silicon Python；Windows x64 使用 x64 Python。
结果在 `dist/`，含应用 ZIP 和 SHA-256 校验文件。采用 PyInstaller onedir，便于排错和替换动态库。

GitHub Actions 使用 `macos-14`（arm64）和 `windows-2022`（x64），会验证实际 Python 架构、运行测试、打包，并启动打包后的应用进行 smoke test。

- 推送 `main` 或手动运行 workflow：生成可下载 Artifacts。
- 推送 `v*` 标签：双平台成功后自动创建 **Release 草稿**，附 ZIP 与校验文件。检查后在 GitHub 发布草稿即可公开下载。

```bash
git tag v0.1.0
git push origin v0.1.0
```

构建默认没有商用代码签名证书。未来可在仓库 Secrets 中接入签名与公证流程。

官方参考：[GitHub runner 规格](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)、[PyInstaller 打包说明](https://pyinstaller.org/en/stable/usage.html)。

## 下一步

- 拖放、多文件队列
- 二维码配对，减少手动复制长配对码
- 取消传输、断点续传、接收确认与磁盘配额
- 托盘常驻、可选开机启动、图片剪贴板同步
- mDNS / 多网卡发现、IPv6、设备重命名
- Apple 公证与 Windows 签名、自动更新

## 许可证

本项目源码使用 [MIT License](LICENSE)。打包应用包含独立许可的 Python、Qt / PySide6、cryptography 等组件，见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 及应用包中的 `licenses/`。
