---
name: home-devices
description: 控制家里的灯、窗帘、空调。设备表在本 skill 内，按表拼 mihome，不要 list。缺 iid 才 spec。
---

# home-devices

<!--
示例：一个虚构的三室之家，did 都是假的。
用 `python3 scripts/gen-home-devices.py` 从你的米家账号生成初稿，再参照这份示例补上叫法和约定。
-->

只控下表设备。表里有 did/iid 就直接拼命令，禁止 `device list` / `mihome --help`，禁止编造。对不上就说找不到。

`mihome` 必须访问小米云：在可联网环境执行。命令一般 1 秒内返回，等 stdout，不要当后台任务。

表里缺 iid 或参数对不上时，再 `mihome device spec <did>`，从输出抄 `prop.s.p`（写成 `s.p`）。

多台写进**一条** `device control`（一次云端请求，几乎同时动作），单条 ≤10 路；不要用 `;` 拆成多条命令。用返回 JSON 的 `code` / `code_msg` 判断成败，一句话回。查状态必须带 iid。

没点名：该房间表里同类一起做。客厅灯 = 客厅全部灯；客厅窗帘 = 纱帘 + 布帘。

不要控表外设备（音箱、门锁、摄像头、厨房小家电）。

## 命令

iid 是 `siid.piid`，从表里抄。

灯：`mihome device control <did> <iid> true|false [<did> <iid> true|false ...] --verify`  
例：客厅灯全关 `mihome device control 100000001 2.1 false 3.1 false 100000002 2.1 false --verify`。`--verify` 会回读，没到位的自动补发一次；还不到位退出码 3，`message` 里写着哪几路，照实说。  
查灯：`mihome device props <did> <iid> [<did> <iid> ...]`（true=开 false=关）。

窗帘：`mihome device control <did> <电机iid> <n>` 或 `<目标iid> N`；窗帘别加 `--verify`（电机值执行完会变，回读对不上）。  
查位置：`mihome device props <did> <位置iid>`
- 客厅（杜亚）：电机 `2.2` 开=2 关=0 停=1；位置 `2.6`；目标 `2.7`
- 主卧（G10）：电机 `2.1` 开=0 关=1 停=2；位置 `2.3`；目标 `2.4`
- 开度 N=0–100，0=全关 100=全开。一半=50，三分之一≈33。全开全关走电机开/关。

空调：
- 开/关：`mihome device control <did> 2.1 true|false`
- 温度：`mihome device control <did> 2.4 N 2.1 true`（16–30）
- 风速：`mihome device control <did> 3.2 F 2.1 true` 自动=0 低=1 中=2 高=4
- 查：`mihome device props <did> 2.1 <did> 2.4`

## 客厅

灯（一个双键墙开 + 一盏吸顶灯）

| 名称 | 人话 | did | iid |
| --- | --- | --- | --- |
| 筒灯 | 筒灯、射灯 | 100000001 | 2.1 |
| 灯带 | 灯带、氛围灯 | 100000001 | 3.1 |
| 主灯 | 主灯、大灯 | 100000002 | 2.1 |

窗帘（杜亚）

| 名称 | 人话 | did |
| --- | --- | --- |
| 纱帘 | 纱帘、薄的那层 | 100000003 |
| 布帘 | 布帘、遮光帘 | 100000004 |

空调

| 名称 | did |
| --- | --- |
| 客厅空调 | 100000005 |

## 主卧

灯

| 名称 | did | iid |
| --- | --- | --- |
| 主灯 | 100000011 | 2.1 |
| 床头灯 | 100000012 | 2.1 |

「床头灯」可以调亮度：`2.2`（1–100）。

窗帘（G10，单扇）：100000013

空调：100000014

## 书房

| 名称 | 类型 | did | iid |
| --- | --- | --- | --- |
| 台灯 | 灯 | 100000021 | 2.1 |
| 书房空调 | 空调 | 100000022 | — |
