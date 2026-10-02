# KWS（自定义唤醒词）

用 Open-XiaoAI 预编译的 sherpa-onnx 关键词识别（`kws.tar.gz`，约 9 MB，解开约 16 MB），只负责把命中写进 `/tmp/open-xiaoai/kws.log`。

- 下载：https://gitee.com/idootop/artifacts/releases/download/open-xiaoai-kws/kws.tar.gz
  sha256 `690a38b94159f30f600494d5d6501f22083cbf6a967512833fb8e216707b07b9`（2026-10-01 取）
- 调试版 `kws-debug`（只打印识别出的拼音，用来调唤醒词）：同目录 `kws-debug`，
  sha256 `0ef3af499e47778aa314891b87496f75eafdc8eac9a4449f44806beeaa6d1c84`。放 `/tmp` 用，别占 `/data`。

## 改唤醒词

改 `my-keywords.txt`，然后：

```bash
uv run keywords.py --tokens tokens.txt --output keywords.txt --text my-keywords.txt
```

`keywords.txt` 里除了标准拼音，还手加了 2026-10-01 用 `kws-debug` 实测听偏的写法（好→和、管→缓、管读一声），命中任一行都算同一个唤醒词。重新生成会丢掉这些行，记得补回。

把 `keywords.txt` 拷到音箱 `/data/open-xiaoai/kws/keywords.txt`，重启 KWS。识别不灵时用 `kws-debug` 听自己说出来是什么拼音，再手改 `keywords.txt`（可加 `:2.0` 提升权重、`#0.6` 调阈值）。

`keywords.py`、`tokens.txt` 来自 Open-XiaoAI `examples/kws`（MIT）。
