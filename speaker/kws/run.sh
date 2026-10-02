#!/bin/sh
# 前台跑 KWS（sherpa-onnx 小模型），命中写 /tmp/open-xiaoai/kws.log。
# 只写日志：欢迎语、叫醒由 butler-client 做（不用上游的 monitor）。
# 参数抄自 Open-XiaoAI examples/kws/init.sh；`noop` 是 /etc/asound.conf 里共享录音的 dsnoop 设备。
DIR=/data/open-xiaoai/kws
mkdir -p /tmp/open-xiaoai
exec "$DIR/kws" \
    --model-type=zipformer2 \
    --tokens="$DIR/models/tokens.txt" \
    --encoder="$DIR/models/encoder.onnx" \
    --decoder="$DIR/models/decoder.onnx" \
    --joiner="$DIR/models/joiner.onnx" \
    --keywords-file="$DIR/keywords.txt" \
    --provider=cpu \
    --num-threads=1 \
    --chunk-size=1024 \
    noop
