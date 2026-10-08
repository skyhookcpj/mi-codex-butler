# 管家：架构与实现原理

小爱音箱当耳朵和嘴，家里一台常开的 Linux 主机（下文统称 NAS）上的 Codex 当大脑，`mihome` CLI 经小米云控家电。
「小爱同学」照旧走原厂；喊「你好管家」才走这套系统。

---

## 1. 一张图

```
                    ┌──────────── 小爱音箱 OH2P（刷了 Open-XiaoAI 补丁）────────────┐
 「你好管家」 ──麦克风──► KWS（sherpa-onnx，只写 /tmp/open-xiaoai/kws.log）          │
                    │      │                                                          │
                    │      ▼                                                          │
                    │  butler-client（Rust）── 念「在呢」/ 只识别模式叫醒 / 念回复      │
                    │      │   ▲ 盯 /tmp/mico_aivs_lab/instruction.log 拿识别文字      │
 「客厅灯关了吗」 ─────────────► 原厂接待 mico_aivs_lab + 小米云 ASR                    │
                    └──────┼───────────────────────────────────────────────────────────┘
                           │ WebSocket ws://<NAS_IP>:4399（Open-XiaoAI 帧格式）
                           ▼
                    ┌──────────── NAS（systemd 用户服务 butler）─────────────────────┐
                    │  butler Server（Node/TS）：收一句话、管 session、调 Codex        │
                    │      │  配置页 http://<NAS_IP>:8787                             │
                    │      ▼                                                          │
                    │  codex exec / exec resume（工作区 ~/mihome-workspace）           │
                    │      │  读 AGENTS.md + .codex/skills/home-devices/SKILL.md      │
                    │      ▼                                                          │
                    │  mihome CLI（Python）──► 小米云 OAuth API ──► 灯 / 窗帘 / 空调   │
                    └─────────────────────────────────────────────────────────────────┘
```

分工的原则：**快的、跟原厂打交道的放音箱本地；慢的、要上下文的放 NAS。**
欢迎语、叫醒、切换只识别模式都是音箱上的本地命令，绕 NAS 只会多延迟；NAS 负责把多轮对话挂进同一个 Codex session。

---

## 2. 模块

| 目录 | 语言 | 跑在哪 | 作用 |
| --- | --- | --- | --- |
| `src/mihome/` | Python 3.11 | NAS（任何机器可开发） | 米家云 CLI：列设备、查/改属性、动作、场景、推送。短进程，无常驻后端 |
| `src/mihome/_miot/` | Python | — | 从 Xiaomi Miloco 裁出的云端 HTTP / OAuth / spec 子集，**沿用 Miloco 许可证，不改协议实现**（见 NOTICE） |
| `workspace/` | 文档 + 小脚本 | 拷到 NAS `~/mihome-workspace` | Codex 的工作目录：`AGENTS.md`（人设、规则、回话原则）+ 工作区级 skill `.codex/skills/`：`home-devices`、`go-to-work`、`taxi`（见 §2.4） |
| `butler/` | TypeScript（Node 22，tsx 直跑） | NAS | 管家 Server：WS 接音箱、调 Codex、配置页 |
| `speaker/client/` | Rust（tokio，armv7 glibc 2.25） | 音箱 `/data/open-xiaoai/` | butler-client：音箱端状态机 |
| `speaker/kws/` | 预编译二进制 + 配置 | 音箱 `/data/open-xiaoai/kws/` | 自定义唤醒词（Open-XiaoAI 的 sherpa-onnx 包），唤醒词拼音表 |
| `speaker/butler.sh`、`speaker/init.sh` | sh | 音箱 | 守护循环 + 开机自启 |
| `scripts/` | sh / systemd / Python | NAS | `install.sh` 装 mihome；`run-butler.sh`、`butler.service`、`install-butler-service.sh` 管 Server 服务；`gen-home-devices.py` 生成设备表初稿 |
| `tests/` | pytest | 开发机 | mihome 单测 |

### 2.1 mihome（`src/mihome/`）

| 文件 | 作用 |
| --- | --- |
| `cli.py` | click 入口，`mihome account / home / device / scene / notify` |
| `commands/device.py` | `list`（TSV）、`spec`、`control`（多台一次请求 + `--verify`）、`props`（多台一次请求）、`action` |
| `session.py` | `CloudSession`：读 `~/.mihome/config.json`，过期自动刷新 OAuth token |
| `specutil.py` | iid 解析、`parse_control_groups`（按位置切分多台参数）、`value_matches`、spec 渲染 |
| `codes.py` | 小米 spec 错误码 → 中文；`annotate_result_codes` 把部分失败反映到外层 code |
| `errors.py` | 退出码（1 参数 / 2 网络 / 3 云端业务）；**`redact` 输出前遮掉 token、授权码** |
| `config.py` | `$MIHOME_HOME`（默认 `~/.mihome`）下的 `config.json` |

批量能力（详见 `mihome <cmd> --help`）：

| 命令 | 一次请求多台 | 说明 |
| --- | --- | --- |
| `device props <did> <iid>… [<did> <iid>…]` | ✅ | |
| `device control <did> <iid> <v> [<iid> <v>] [<did> …] [--verify]` | ✅ | 云端只回「已受理」(code 1)，设备异步执行、偶尔丢；`--verify` 约 1s 回读并补发一次 |
| `device control <did> --set <iid> <v> …` | 单台多属性 | 兼容旧写法 |
| `device action` / `scene trigger` | ❌ | 云端接口本身一次一个 |

客厅 5 路灯：一次请求 0.1–0.25s，5 条命令串行约 3.3s。

### 2.2 butler Server（`butler/src/`）

| 文件 | 作用 |
| --- | --- |
| `index.ts` | 组装：读配置 → 起 WS（`SpeakerLink`）→ 起配置页 → `utterance` 事件交给 `Butler` |
| `speaker.ts` | `SpeakerLink`：WS 服务端，同一时刻只认一台音箱（新连接顶掉旧的），15s ping 心跳；连上即下发 `butler_config`、调 `get_version`；可 RPC `run_shell` |
| `protocol.ts` | 帧编解码和事件名 |
| `butler.ts` | 调度：新会话关键词 → 忙碌 → 空闲过期 → Codex 新建/续写 → 口语化回复；同一时间只跑一个 Codex。给 Codex 的提示只转交原话（「用户对着音箱说：…」），音箱在哪个房间、回复长短等规则都在工作区 `AGENTS.md` |
| `codex.ts` | 拼 `codex exec` / `exec resume` 参数，逐行吃 `--json` 的 JSONL；**拿到 `turn.completed` 立刻返回**，1.5s 后按进程组结束 codex。命令的开始 / 结束和中间的话实时回调出去（`onStep`），给演示页用 |
| `store.ts` | session 落盘 `~/.mihome/butler-session.json`；最近对话 `butler-history.jsonl`（保留 200 条） |
| `text.ts` | 新会话关键词匹配（「新对话，打开客厅灯」= 新开一轮再执行后半句）；`toSpeech` 去 markdown、截到句末 |
| `config.ts` | `~/.mihome/butler.json`；端口只认文件 / 环境变量，页面改不了 |
| `web.ts` | 配置页 API + SSE（log / state / history / live） |
| `public/index.html` | 配置页：状态、模拟一句、让音箱念、最近对话、话术与参数、日志 |
| `public/live.html` | 演示页 `/live`：全屏大字，实时显示每一轮听到什么、Codex 跑的每条命令（开始转圈、结束打勾、耗时）、中间说的话和最后的回复；拍视频、给人演示用 |
| `scripts/fake-speaker.ts` | 没音箱时假装音箱：`npm run fake-speaker -- ws://NAS:4399` |

### 2.3 butler-client（`speaker/client/src/`）

| 文件 | 作用 |
| --- | --- |
| `machine.rs` | **纯逻辑状态机**（输入 → 动作，不碰 IO），在开发机上单测 |
| `device.rs` | 原厂命令执行：TTS、静默唤醒、开关只识别模式、整套重置；重启类操作串行并协调「最早可叫醒 / 可念时刻」 |
| `tail.rs` | 轮询 tail：识别截断、替换、「清空后重写得更长」（比对文件开头） |
| `logs.rs` | 解析 `kws.log` 行、`instruction.log` 的 `RecognizeResult`；坏行跳过不崩 |
| `settings.rs` | NAS 下发的话术和参数，本地存 `/data/open-xiaoai/butler-config.json` |
| `protocol.rs` | 帧格式（与 Server 对称） |
| `main.rs` | 组装：两个 tail、250ms tick、WS 连接循环（断线 1–5s 退避重连，45s 无消息判死）、RPC |

---

### 2.4 工作区 skill（`workspace/.codex/skills/`）

skill 放在工作区级目录，Codex 在 `~/mihome-workspace` 里启动时自动加载；不装到 `~/.codex/skills`。需要访问外部服务的 skill 带一个小脚本，凭据只在脚本里读，不进模型上下文。

| skill | 触发 | 做什么 |
| --- | --- | --- |
| `home-devices` | 控制灯、窗帘、空调 | 设备表（did、iid、方向、分路）和 `mihome` 命令写法；多台写进一条 `device control` |
| `go-to-work` | 「我要上班去了」 | `briefing.py` 读 `~/.mihome/go-to-work.json`，并行：执行配置的设备动作（一条 `device control`）、查配置城市的天气（Open-Meteo，含未来 6 小时降雨）、查今天飞书日程（lark-cli，可关）；Codex 组织 250 字内的播报，最后问「要帮你叫车去公司吗」 |
| `taxi` | 「帮我叫车去公司」、对「要叫车吗」回「要」 | `taxi.py estimate / order / status / cancel` 调滴滴 MCP；固定路线家 → 公司；先报价、用户明确「确认」才下单，规则写死在脚本里（3 分钟询价有效、一次询价一单、正式环境每天最多 `daily_limit` 单、有进行中订单不能再叫）；下单后把车牌推到米家 App |

「上班 → 要 → 确认」三句话目前每句都要先喊「你好管家」（没有追问后自动收音）。

## 3. 一句话的完整旅程

以「你好管家」→「把客厅灯关了」为例（时间是 2026-10 实测量级）：

| # | 在哪 | 发生什么 | 耗时 |
| --- | --- | --- | --- |
| 1 | 音箱 KWS | 识别出唤醒词，写 `kws.log` | 实时 |
| 2 | client | 同时：念缓存的「在呢」mp3 + 以**只识别模式**重启接待；重启完 0.3s 后静默唤醒原厂接待 | ≈1s |
| 3 | 原厂 + 小米云 | 用户说话，云端**只做 ASR**（不理解、不执行、不播报），`instruction.log` 出 `is_final` 的 `RecognizeResult` | 说完后 ≈0.3s |
| 4 | client | 发 `butler_utterance`；等接待写出 `Dialog.Finish`（最多 2s），关只识别、重启接待回正常模式 | 立刻 |
| 5 | Server | 新会话关键词 / 忙碌 / 空闲过期判断；有 session 就 `codex exec resume` | ≈0.1s |
| 6 | Codex | 模型决定命令（第 1 次调用模型） | 2–7s |
| 7 | Codex → mihome | 一条 `device control … --verify` | ≈1.2s |
| 8 | Codex | 模型写口语回复（第 2 次调用模型）；长短按 `AGENTS.md` 的场景原则 | 1–8s |
| 9 | Server → client | `butler_reply`；接待还在重启的话等它做完再念 | — |
| 10 | 音箱 | `tts_play.sh` 云端合成并播放 | ≈0.3s |

**延迟的大头是第 6、8 步的云端模型往返**，取决于模型服务商（作者用的是经 ZAI 的 GLM-5.3，排队波动大；换成 flash 版没有更快）。

---

## 4. 协议

沿用 Open-XiaoAI 的 JSON 文本帧（serde 外部标签枚举），二进制音频帧不用：

```json
{ "Request":  { "id": "uuid", "command": "run_shell", "payload": "echo hi" } }
{ "Response": { "id": "uuid", "code": 0, "msg": "success", "data": {...} } }
{ "Event":    { "id": "uuid", "event": "butler_utterance", "data": { "text": "..." } } }
```

| event | 方向 | data | 说明 |
| --- | --- | --- | --- |
| `butler_utterance` | 音箱 → NAS | `{text, dialog_id}` | 只识别模式下的一句最终识别；Event.id 即这句的 id |
| `butler_reply` | NAS → 音箱 | `{reply_to, kind, text}` | `kind` ∈ reply / new_session / busy / error / timeout / cancelled；`text` 空 = 不念、只结束等待 |
| `butler_say` | NAS → 音箱 | `{text}` | 随时念一句（受理语、配置页「让音箱念」），不改状态 |
| `butler_config` | NAS → 音箱 | 话术 + 超时 + 打断时机 | 连上时、页面保存时下发 |
| `kws` | 音箱 → NAS | `{keyword}` | 只用于日志 |

RPC（NAS 调音箱）：`get_version`、`run_shell`。

---

## 5. 状态与规则

### 5.1 音箱端状态机（`machine.rs`）

```
待机 ──唤醒词──► 以只识别模式重启接待 + 念「在呢」+ 静默叫醒 ──► 听写（15s）
听写 ──最终识别有字──► 发 NAS ──► 等回复（Codex 超时 + 15s）
     ──最终识别为空──► 念「没听清」（默认空 = 静默）──► 待机
     ──超时──► 整套重置（回正常模式，也是自愈）──► 待机
     ──又喊唤醒词──► 整套重置后再以只识别模式叫醒
（出了最终识别后：等 Dialog.Finish，最多 2s，就关只识别、重启接待回正常模式）
等回复 ──对应 reply──► 念 ──► 待机
       ──超时 / 发送失败──► 念超时语 / 出错语 ──► 待机
       ──唤醒词──► 念忙碌语，继续等
```

- **只识别模式**：接待启动时看到 `/data/pns.lab` 就进原厂的实验室测试模式，识别请求带上「不做 NLP、不做 TTS」，云端只回识别结果（`is_nlp_request: false`），不理解、不执行设备指令（2026-10-08 实测）。`/data/pns.lab` 是指向 `/tmp/butler-asr-only` 的软链，开关只在内存盘里建删文件；client 启动、`butler.sh stop`、超时都会回到正常模式，「小爱同学」平时完全照旧。
- 已经接管过的 `dialog_id` 的残余行不再当成下一句。
- 迟到的回复：待机时照念，听写中不插嘴。
- `abort_on`：`final`（默认）等 `is_final`；`stop` 在 `is_stop` 就交给 NAS。静默唤醒下原厂不给 `is_stop`，两者实际一样。

### 5.2 Server 规则（`butler.ts`）

1. 去空白；命中新会话关键词（默认 新对话 / 新会话 / 重新开始）→ 丢 session，回「好，新开一轮」；关键词后跟了正事就新开一轮再执行后半句。
2. 有 Codex 在跑 → 回忙碌语，正在跑的不受影响。
3. session 空闲超过 30 分钟 → 视为新开一轮。
4. 有 session → `codex exec resume <id>`；`resume` 在 Codex 动手前就失败（如 session 文件没了）→ 换新 session 重试一次；动手后失败不重试，免得重复开关设备。
5. 超时 / 出错 / 页面「停止」→ 对应话术；停止 = 空文本，音箱不念只回待机。

Codex 调用的固定参数（`codex.ts`）：工作目录 = 进程 cwd = `~/mihome-workspace`；`workspace-write` 沙箱、开网络、可写 `~/.mihome`。`resume` 不认 `-C/-s/--add-dir`，所以 cwd 由 spawn 决定、沙箱走 `-c`。session id 取自 JSONL 首行 `thread.started.thread_id`。

---

## 6. 借原厂识别踩过的坑

静默唤醒（`pnshelper event_notify {"src":1,"event":0}`）与重启接待单独用都没问题，循环连用会出状态问题。对策都在 `speaker/client/src/device.rs`，细节见 [`speaker/README.md`](../speaker/README.md)。

| 现象 | 对策 |
| --- | --- |
| 借原厂识别时，小米云端会顺手理解并执行设备指令（抢跑，打断拦不住） | 管家这一轮用只识别模式（`/data/pns.lab`） |
| 中途打断后下一次静默唤醒没反应 | 整套重置时一并 `/etc/init.d/pns restart`（只识别模式下正常轮次已不需要打断） |
| 中途打断后顶灯跑马灯不灭 | 整套重置时补 `/bin/wakeup.sh ready` |
| 同一接待进程第二次静默唤醒识别为空（不发首包音频） | 每次唤醒前空闲重启接待 |
| 收音中途重启接待后「正在听」灯效（41 号）挂着，`ledd` 空转拖慢 KWS | 每次强行重启后清理挂着的对话灯效 |
| TTS 文本含引号/反斜杠会坏 JSON、可能注入 | `tts_safe` 清理后作为 argv 传给 `tts_play.sh` |
| KWS 小模型把「管」听成「缓」等 | `keywords.txt` 补听偏的拼音写法 |

---

## 7. 配置在哪

| 配置 | 位置 | 谁写 | 谁读 |
| --- | --- | --- | --- |
| 管家话术、超时、Codex 工作目录/模型 | NAS `~/.mihome/butler.json` | 配置页 | Server（话术和音箱参数再下发给 client） |
| 端口 | 同上的 `wsPort`/`webPort`，或 `BUTLER_WS_PORT`/`BUTLER_WEB_PORT` | 手工 | Server |
| 小米 OAuth | NAS `~/.mihome/config.json` | `mihome account bind` | mihome |
| Codex 模型/服务商 | NAS `~/.codex/config.toml` | 手工 | Codex |
| Codex 人设 / 回话原则 / skill | `~/mihome-workspace/AGENTS.md`、`.codex/skills/*/` | 从仓库 `workspace/` 拷贝后自己改 | Codex |
| 飞书应用与授权 | NAS `~/.mihome/lark-cli/`、`~/.mihome/lark-cli-keychain/`（Codex 沙箱只能写 `~/.mihome`，所以从 `~/.lark-cli`、`~/.local/share/lark-cli` 挪过来再软链回去） | `lark-cli config init` / `auth login` | `go-to-work` |
| 滴滴 MCP Key、路线、上限、环境 | NAS `~/.mihome/didi.json`（样例 `taxi/didi.example.json`）；状态 `didi-state.json` | 手工 | `taxi` |
| 上班前的城市、设备动作、是否查日程 | NAS `~/.mihome/go-to-work.json`（样例 `go-to-work/go-to-work.example.json`） | 手工 | `go-to-work` |
| 音箱连谁 | 音箱 `/data/open-xiaoai/server.txt` | 手工 | client |
| 音箱话术缓存 | 音箱 `/data/open-xiaoai/butler-config.json` | client 自动 | client |
| 唤醒词 | 音箱 `/data/open-xiaoai/kws/keywords.txt` | 仓库拷贝 | KWS |

---

## 8. 开发、测试、部署

```bash
# mihome（NAS 上用 uv tool 可编辑安装，改代码即生效）
uv run pytest

# butler Server
cd butler && npm test && npx tsc --noEmit
systemctl --user restart butler                 # NAS 上更新代码后

# butler-client（交叉编译，见 speaker/README.md）
cd speaker/client && cargo test && cargo clippy --all-targets
cargo zigbuild --release --target armv7-unknown-linux-gnueabihf.2.25
# 音箱上：butler.sh stop → 覆盖 /data/open-xiaoai/butler-client → butler.sh start
```

在 Mac 上开发、NAS 上运行时，用 rsync 把改动同步到 NAS 的仓库目录即可。

测试覆盖：mihome 42 个（含多台控制、`--verify`、脱敏）；Server 35 个（含假音箱 WS 端到端、假 codex 进程）；client 26 个（状态机、tail、解析、重启协调）。

---

## 9. 已知限制与待办

- 两拍交互：先「你好管家」、等「在呢」、再说事。一句说完需要自建 ASR（Open-XiaoAI `xiaozhi` 示例的路线）。
- 延迟主要在云端模型；可选：换更快的服务商、或让 Codex 执行完命令立刻 `butler-say`（未做）。
- 「10 秒后关灯」这类延时任务要等执行完才回话；回合结束会按进程组收掉，后台任务也会被杀。需要另设定时机制（未做）。
- 多轮问答（「要叫车吗 → 要 → 确认」）每句都要重新喊唤醒词；「追问后自动收音」未做。
- KWS 是 5MB 小模型，偶有误唤醒 / 漏唤醒；调法见 `speaker/kws/README.md`。
- 只支持 Open-XiaoAI 能刷的型号；OH2 等不在支持列表的音箱接不了。
