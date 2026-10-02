# mi-codex-butler

把小爱音箱接上 Codex 的家庭管家。音箱当耳朵和嘴，Codex 当大脑，`mihome` CLI 打小米云控设备。

## 先读

- [README.md](README.md)：能做什么、怎么搭
- [docs/architecture.md](docs/architecture.md)：模块、一句话的完整链路、协议、状态机、配置位置
- [speaker/README.md](speaker/README.md)：音箱端部署和原厂状态坑

## 测试

```bash
uv run pytest                                  # mihome
cd butler && npm test && npx tsc --noEmit      # Server
cd speaker/client && cargo test                # 音箱 client
```

## 约定

- `src/mihome/_miot/` 沿用 Xiaomi Miloco 许可证，不改协议实现；新功能写在它外面。
- 控设备一律通过 `mihome` 命令，不要编造 did / spec_name / 枚举值；拿不准先 `mihome device spec <did>`。
- 门锁、摄像头、燃气阀这类设备，先问用户再执行。
- 仓库里不放任何人家里的真实信息（did、地址、IP、Key）。示例一律用假数据，真实配置放 `~/.mihome/`。
