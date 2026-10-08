# 音箱端

小爱音箱（OH2P，已刷 Open-XiaoAI 补丁）上跑的东西。程序和配置都放在 `/data/open-xiaoai/`（根分区只读、空间已满）。

## butler-client

盯两份日志，管家路径在音箱本地闭环，只把识别出的一句话交给 NAS：

1. `/tmp/open-xiaoai/kws.log` 出现唤醒词 → 以**只识别模式**重启接待，同时念「在呢」（缓存的 mp3）→ 静默叫醒原厂接待
2. `/tmp/mico_aivs_lab/instruction.log` 出最终识别 → `butler_utterance` 发给 NAS；等接待写出 `Dialog.Finish`（最多 2s）→ 关只识别、重启接待回正常模式
3. 收到 `butler_reply` → 等接待重启做完再念

### 只识别模式（2026-10-08 实测）

接待（`mico_aivs_lab`）启动时如果 `/data/pns.lab` 存在（空文件即可），就进原厂的实验室测试模式：识别请求带「disable NLP / disable TTS」，云端只回识别结果（`is_nlp_request: false`），**不理解、不执行设备指令、不播报**。

这解决了借原厂识别的根本问题：以前识别完成的同时，小米云端已经把设备指令发出去了，音箱上打断只能让小爱不开口，拦不住执行（比如「关闭餐厅的灯」被小爱理解成厨房的灯一起关掉、扫地机被先启动全屋清扫）。

- `/data/pns.lab` 做成指向 `/tmp/butler-asr-only` 的软链，开关只在内存盘里建删文件，不写闪存；音箱重启后默认是正常模式。
- 模式只在接待启动时读一次，所以每次开关都要重启接待（唤醒前本来就要重启）。
- client 启动、`butler.sh stop`、听写超时、等不到 `Dialog.Finish` 都会回到正常模式，「小爱同学」平时完全照旧。

### 借原厂识别踩过的坑（2026-10-01 实测）

静默唤醒（`pnshelper event_notify {"src":1,"event":0}`）和重启接待单独用都没问题，连着用会出几个状态问题。Open-XiaoAI 的示例没遇到，是因为没有一个示例是「静默唤醒 + 借原厂识别 + 重启打断」反复循环。有了只识别模式，正常的一轮不再中途打断，前两条只在超时、重新唤醒时的整套重置里用到。

| 现象 | 原因 | 对策（`src/device.rs`） |
| --- | --- | --- |
| 打断后下一次静默唤醒没反应（`local stream inflowed when transmitend`） | 对话中途杀接待，mipns 收不到 `aivs dialog finish`，卡在收音态；「小爱同学」走 pre-wakeup 不受影响 | 打断时再 `/etc/init.d/pns restart` |
| 回复后顶灯跑马灯一直转 | 识别出字时原厂调了 `wakeup.sh think`，正常结束才调 `ready` | 打断时补 `/bin/wakeup.sh ready`（顺带让 mediaplayer 退出唤醒态） |
| 唤醒了但云端识别为空（`asr response truncated since no new response`） | 同一个接待进程只有第一次静默唤醒会发首包音频（日志里没有 `asr.send.first.binary`） | 每次唤醒前空闲重启接待，0.3s 后再唤醒；和念「在呢」重叠，不加延迟 |
| 顶灯「正在听」灯效一直挂着，`ledd` 空转吃掉大半个核，拖慢同一颗 CPU 上的 KWS（2026-10-08 实测 18 小时里 41 号打开 37 次、关闭 22 次） | 收音中途重启接待，原厂不会再调 `wakeup.sh asrend` 关 41 号；`ready` 在 OH2P 上只关 2 号 | 每次强行重启接待后查 `ubus call led status`，只关对话相关的 1/2/3/9/41 号灯效，静音、夜间等提示灯不碰 |

另有自愈：叫醒后 15s 还没有最终识别，整套重置一次（顺带回正常模式）。整套重置约 1.5s 恢复静默唤醒、1.7s 恢复原厂 TTS；这期间「小爱同学」也听不到。正常轮次只重启接待（约 0.3s），不再重启 pns。

话术和超时由 NAS 的 `butler_config` 下发，本地存 `/data/open-xiaoai/butler-config.json`。「小爱同学」完全不碰。

```bash
# 开发机上（工具链：rustup + cargo-zigbuild，不需要 Docker）
cd speaker/client
cargo test
cargo zigbuild --release --target armv7-unknown-linux-gnueabihf.2.25
# 产物 target/armv7-unknown-linux-gnueabihf/release/butler-client（音箱 glibc 2.25）
```

在开发机上联调：`BUTLER_DRY_RUN=1` 只打印原厂命令；`BUTLER_KWS_LOG` / `BUTLER_INSTRUCTION_LOG` / `BUTLER_HOME` 指到临时文件，往里写行即可模拟。

## 部署与开机自启

音箱上的布局：

```
/data/init.sh                     ← speaker/init.sh（原厂 rc.local 开机调用）
/data/open-xiaoai/butler.sh       ← speaker/butler.sh：start|stop|restart|status
/data/open-xiaoai/butler-client   ← 交叉编译产物
/data/open-xiaoai/server.txt      ← ws://<NAS_IP>:4399
/data/open-xiaoai/butler-config.json  ← NAS 下发的话术和参数（自动写）
/data/open-xiaoai/kws/            ← kws 程序、models/、keywords.txt、run.sh（见 kws/README.md）
```

`butler.sh` 给 KWS 和 client 各起一个守护循环（busybox 没有 nohup / procd 不能加服务，根分区只读），挂了 3 秒后重拉。client 日志进 syslog：`grep butler /var/log/messages`。

更新 client：`butler.sh stop` → 覆盖 `/data/open-xiaoai/butler-client` → `butler.sh start`。

没有 KWS 或想远程测试时，可以手动触发一次唤醒：`echo "$(date +%s)@你好管家" >> /tmp/open-xiaoai/kws.log`

## 从零部署

前提：音箱已按 [Open-XiaoAI](https://github.com/idootop/open-xiaoai) 的说明刷好补丁，能 `ssh root@<音箱IP>` 登录；NAS 上的 Server 已经跑起来。

**验证范围**：只在 Xiaomi 智能音箱 Pro（OH2P）固件 1.62.2 上验证过。

- `butler-client` 是 armv7 hard-float 程序，按 glibc 2.25 编译，系统 glibc 不低于 2.25 就能启动。
- 它依赖的原厂行为（`instruction.log` 里的 `RecognizeResult`、`pnshelper` 静默唤醒、`mico_aivs_lab` / `pns` 重启、`wakeup.sh`、`tts_play.sh`）随型号和固件可能不同。读不懂的日志行会被跳过，不会崩溃，表现是唤醒后没反应。这时要改的是 `client/src/device.rs`、`logs.rs`，换个编译版本解决不了。

1. **KWS**：下载 `kws.tar.gz`（地址和校验值见 [kws/README.md](kws/README.md)），把里面的 `kws` 程序和 `models/` 放到音箱 `/data/open-xiaoai/kws/`，再放上本目录的 `kws/run.sh`、`kws/keywords.txt`。
2. **client**：从 GitHub Releases 下载 `butler-client`，或者按上面的命令自己编译。
3. **拷到音箱**。音箱上没有 sftp-server，新版 OpenSSH 的 `scp` 要加 `-O` 走旧协议；音箱只认 ssh-rsa 主机密钥，再加两个 `-o`：

   ```bash
   alias spkscp='scp -O -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedAlgorithms=+ssh-rsa'
   spkscp butler-client speaker/butler.sh root@<音箱IP>:/data/open-xiaoai/
   spkscp speaker/kws/run.sh speaker/kws/keywords.txt root@<音箱IP>:/data/open-xiaoai/kws/
   spkscp speaker/init.sh root@<音箱IP>:/data/init.sh
   ```

4. **在音箱上**：

   ```sh
   chmod +x /data/open-xiaoai/butler-client /data/open-xiaoai/butler.sh /data/open-xiaoai/kws/run.sh /data/open-xiaoai/kws/kws
   echo ws://<NAS_IP>:4399 > /data/open-xiaoai/server.txt
   /data/open-xiaoai/butler.sh start
   /data/open-xiaoai/butler.sh status
   ```

5. 喊「你好管家」，听到「在呢」后说一句话。NAS 配置页（`http://<NAS_IP>:8787`）上能看到这句话和回复。

`/data/init.sh` 由原厂 `/etc/rc.local` 开机执行，之后断电重启也会自动拉起。如果你之前给 Open-XiaoAI 的其他客户端写过 `/data/init.sh`，先备份，再把两边的启动命令合在一起。
