# NOTICE

本项目是个人爱好项目，**不是小米官方项目**，与小米公司没有关联。

## 许可证

- 除下面说明的目录外，代码以 [MIT](LICENSE) 许可发布。
- `src/mihome/_miot/` 是从 [Xiaomi Miloco](https://github.com/XiaoMi/xiaomi-miloco) 的 `backend/miot` 裁出的云端 HTTP / OAuth / spec 子集，不含摄像头、局域网、MQTT。版权归小米，**沿用 Xiaomi Miloco 许可证**（全文见 [`src/mihome/_miot/LICENSE.md`](src/mihome/_miot/LICENSE.md)）：仅限个人非商业使用，再分发时须附带该许可证并保留版权声明。未改动其协议实现，只去掉了本 CLI 用不到的模块。

## 参考与第三方

- [Open-XiaoAI](https://github.com/idootop/open-xiaoai)（MIT，Copyright (c) 2024 Del Wang）：音箱刷机补丁和 KWS 预编译包来自该项目。`speaker/client/` 的 WS 帧格式（`Request` / `Response` / `Event`）、音箱原厂命令（`pnshelper` 静默唤醒、`mico_aivs_lab restart` 打断、`tts_play.sh`）和 `instruction.log` / `kws.log` 的用法参考了它，代码为重写。`speaker/kws/keywords.py`、`tokens.txt` 来自其 `examples/kws`。
- 天气数据来自 [Open-Meteo](https://open-meteo.com/)；飞书日程通过 [lark-cli](https://www.npmjs.com/package/@larksuite/cli)；叫车通过滴滴开放平台 MCP。使用这些服务须遵守各自的条款。

## 风险提示

- 给音箱刷机会失去保修，操作不当可能变砖。请先读 Open-XiaoAI 的说明和用户协议（仅供个人学习研究）。
- `taxi` 示例会真的下单叫车、产生费用。先用 `sandbox` 环境调试。
