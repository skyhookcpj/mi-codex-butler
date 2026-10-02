#!/bin/sh
# 音箱上的管家进程管理：KWS + butler-client，各自一个守护循环，挂了 3 秒后重拉。
# 用法：butler.sh start|stop|restart|status
# 日志：client 进 syslog（/var/log/messages，tag=butler-client）。原厂 syslog-ng 只留 warning 及以上，所以用 user.warning。
# KWS 输出丢掉（每秒刷屏，写 /tmp 一天约 36 MB）。
B=/data/open-xiaoai

run_kws() {
    while true; do
        "$B/kws/run.sh" >/dev/null 2>&1
        logger -t butler -p user.warning "kws 退出，3 秒后重启"
        sleep 3
    done
}

run_client() {
    while true; do
        "$B/butler-client" 2>&1 | logger -t butler-client -p user.warning
        logger -t butler -p user.warning "butler-client 退出，3 秒后重启"
        sleep 3
    done
}

start() {
    mkdir -p /tmp/open-xiaoai
    start-stop-daemon -S -q -b -m -p /tmp/butler-kws.pid -x /bin/sh -- "$0" run-kws
    start-stop-daemon -S -q -b -m -p /tmp/butler-client.pid -x /bin/sh -- "$0" run-client
}

stop() {
    # 先停守护循环，再停它拉起的进程
    start-stop-daemon -K -q -p /tmp/butler-kws.pid
    start-stop-daemon -K -q -p /tmp/butler-client.pid
    rm -f /tmp/butler-kws.pid /tmp/butler-client.pid
    killall kws butler-client 2>/dev/null
    return 0
}

status() {
    echo "kws:           $(pidof kws || echo 未运行)"
    echo "butler-client: $(pidof butler-client || echo 未运行)"
}

case "$1" in
    start) start ;;
    stop) stop ;;
    restart) stop; sleep 1; start ;;
    status) status ;;
    run-kws) run_kws ;;
    run-client) run_client ;;
    *) echo "用法：$0 start|stop|restart|status"; exit 1 ;;
esac
