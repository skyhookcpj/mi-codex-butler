#!/bin/sh
# 部署为音箱 /data/init.sh：原厂 /etc/rc.local 开机时会后台执行它。
# 等原厂语音服务起来再拉管家（KWS 不依赖网络；client 自己会重连 NAS）。
sleep 15
/data/open-xiaoai/butler.sh start
