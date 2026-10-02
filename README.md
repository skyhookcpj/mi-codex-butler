# mi-codex-butler · 管家

把小爱音箱接上 Codex，做一个听得懂人话、能按你的需要扩展的家庭管家。

> Turn a flashed XiaoAI smart speaker into a voice front-end for [Codex CLI](https://github.com/openai/codex). Say "你好管家", and Codex controls your Mi Home devices through the bundled `mihome` CLI, or runs any skill you write. Docs are in Chinese.

**非小米官方项目**，与小米公司没有关联。给音箱刷机有风险，请先看 [NOTICE](NOTICE.md)。

## 它能做什么

「小爱同学」照旧是原厂的小爱。喊「你好管家」，后面这句话就交给 Codex：

```
你：你好管家
管家：在呢
你：把客厅灯都关了，主卧空调调到 24 度
管家：客厅灯都关了，主卧空调调到 24 度了。

你：你好管家 / 我要上班去了
管家：好，上班前帮你看了下。客厅窗帘给你拉开了。现在多云，二十度，下午三点后可能下雨，记得带伞。
      你的飞书日程里，上午十点半有个周会，开一小时，是飞书视频会议……要帮你叫车去公司吗？
你：你好管家 / 要
管家：去公司的快车大约四十二元，确认叫车吗？
你：你好管家 / 确认
管家：叫到了，白色比亚迪，车牌京 A 一二三四五，大约五分钟到，车牌发到你手机了。
```

能做什么取决于你给 Codex 写了什么 skill。仓库里带了三个示例：

| skill | 说什么 | 做什么 |
| --- | --- | --- |
| `home-devices` | 「把客厅灯关了」「窗帘开一半」 | 按设备表拼 `mihome` 命令控灯、窗帘、空调。设备表可以用脚本从米家账号生成初稿 |
| `go-to-work` | 「我要上班去了」 | 同时开窗帘、查天气、查今天的飞书日程，播报一段，最后问要不要叫车 |
| `taxi` | 「帮我叫车去公司」 | 调滴滴 MCP：先报价，你说「确认」才下单；车牌推送到手机 |

## 怎么工作

```
「你好管家」→ 音箱上的 KWS 听到唤醒词 → butler-client 念「在呢」，静默叫醒原厂识别
「把灯关了」→ 原厂把语音识别成文字 → butler-client 打断小爱，把这句话发给 NAS
NAS：butler Server 调 codex exec（同一轮对话 resume 同一个 session）
     Codex 读工作区的 AGENTS.md 和 skill，执行 mihome 等命令
回复 → butler-client 让音箱念出来
```

| 目录 | 是什么 |
| --- | --- |
| `src/mihome/` | `mihome` CLI（Python）：经小米云 OAuth 列设备、查/改属性、触发动作、推送通知 |
| `butler/` | 管家 Server（Node/TS）：接音箱、管会话、调 Codex，带一个配置页 |
| `speaker/` | 音箱端：butler-client（Rust）、自定义唤醒词、开机自启脚本 |
| `workspace/` | Codex 工作区模板：`AGENTS.md` 和三个示例 skill |
| `scripts/` | 安装 mihome、装 Server 服务、生成设备表 |

实现细节、协议、状态机和踩过的坑见 [docs/architecture.md](docs/architecture.md)。

## 需要准备

- **音箱**：Open-XiaoAI 支持刷机的型号。作者用的是 Xiaomi 智能音箱 Pro（OH2P）；小爱音箱 Pro（LX06）理论上可用，没测过。刷机见 [Open-XiaoAI](https://github.com/idootop/open-xiaoai)。
- **一台常开的 Linux 主机**（NAS、迷你主机、树莓派都行，下文统称 NAS），和音箱在同一个局域网：Python 3.11+、[uv](https://docs.astral.sh/uv/)、Node 20+、[Codex CLI](https://github.com/openai/codex)，以及 Codex 能用的模型（OpenAI，或者任何兼容的服务商）。
- **米家账号**，设备已经接入米家。
- 可选：飞书（`go-to-work` 查日程用）、滴滴开放平台 MCP Key（`taxi` 叫车用）。

## 快速开始

下面的命令都在 NAS 上执行。

### 1. 装 mihome，绑定米家账号

```bash
git clone https://github.com/skyhook520/mi-codex-butler.git ~/mi-codex-butler
cd ~/mi-codex-butler
./scripts/install.sh            # 装到 ~/.local/bin/mihome
mihome account bind             # 浏览器打开打印出的链接，登录后把授权码贴回终端
mihome device list
```

### 2. 准备 Codex 工作区

```bash
cp -r workspace ~/mihome-workspace
python3 scripts/gen-home-devices.py > ~/mihome-workspace/.codex/skills/home-devices/SKILL.md
```

生成的设备表是初稿：把名称改成家里人平时的叫法，删掉不想让管家碰的设备，补上「没点名时怎么做」这类约定。可以参照 `workspace/.codex/skills/home-devices/SKILL.md` 里虚构的示例。再按家里情况改 `~/mihome-workspace/AGENTS.md`。

先在终端里试一下 Codex 能不能控设备：

```bash
cd ~/mihome-workspace
codex exec --skip-git-repo-check -s workspace-write \
  -c sandbox_workspace_write.network_access=true --add-dir ~/.mihome "把客厅灯关了"
```

### 3. 启动管家 Server

```bash
cd ~/mi-codex-butler/butler && npm ci --omit=dev && cd ..
sudo loginctl enable-linger $USER     # 一次即可：让用户服务开机自启
./scripts/install-butler-service.sh
```

打开配置页 `http://<NAS_IP>:8787`，在「模拟一句」里输入「把客厅灯关了」，能看到 Codex 的回复，就说明 Server 这一侧通了。也可以用 `npm run fake-speaker -- ws://<NAS_IP>:4399` 假装一台音箱连上来。

### 4. 接上音箱

刷好 Open-XiaoAI 补丁后，把唤醒词、butler-client 和启动脚本放到音箱上，步骤见 [speaker/README.md](speaker/README.md#从零部署)。butler-client 可以从 Releases 下载，也可以自己交叉编译。

喊「你好管家」，听到「在呢」后说一句话。

### 5. 可选：上班前和叫车

- `go-to-work`：复制 `go-to-work.example.json` 到 `~/.mihome/go-to-work.json`，填城市坐标和出门要动的设备。查日程要装 [lark-cli](https://www.npmjs.com/package/@larksuite/cli) 并登录飞书；Codex 沙箱只能写 `~/.mihome`，所以要把 lark-cli 的配置目录挪进去再软链回来（见 [architecture.md §7](docs/architecture.md#7-配置在哪)）。不用飞书就把 `agenda` 设成 `false`。
- `taxi`：复制 `didi.example.json` 到 `~/.mihome/didi.json`（权限设成 600），填 MCP Key 和家、公司的坐标。**先用 `sandbox` 调通**，改成 `production` 后会真的叫车。

## 写自己的 skill

skill 放在工作区的 `.codex/skills/<名字>/SKILL.md`，Codex 在工作区里启动时自动加载。要访问外部服务时，写一个小脚本放在同一个目录：凭据只在脚本里读，不进模型上下文；花钱、不可逆的操作把保护规则写进脚本，不靠提示词。`go-to-work` 和 `taxi` 就是这样写的，可以照着改。

回复会被音箱念出来，怎么说话（长短、口吻）写在工作区的 `AGENTS.md` 里，改完下一句就生效，不用重启 Server。

## mihome 命令

| 命令 | 作用 |
| --- | --- |
| `account bind / authorize / status / unbind` | 绑定小米账号 |
| `home list / use` | 多套房时选定当前家庭 |
| `device list / spec / control / props / action` | 查设备、看规格、改属性、读状态、触发动作 |
| `scene list / trigger` | 米家手动场景 |
| `notify push --text "..."` | 推送到米家 App |

多台设备写进一条 `device control`，一次请求下发：`mihome device control <did> <iid> <value> [<did> <iid> <value> ...] --verify`。`--verify` 会回读，没到位的自动补发一次。

数据都在 `~/.mihome`（可用 `MIHOME_HOME` 改）。

## 许可证

- 本项目代码：[MIT](LICENSE)。
- `src/mihome/_miot/`：从 [Xiaomi Miloco](https://github.com/XiaoMi/xiaomi-miloco) 裁出，沿用 Xiaomi Miloco 许可证，**仅限个人非商业使用**。
- 音箱刷机补丁和唤醒词模型来自 [Open-XiaoAI](https://github.com/idootop/open-xiaoai)。

详见 [NOTICE](NOTICE.md)。
