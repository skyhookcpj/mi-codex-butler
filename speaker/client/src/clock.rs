use std::time::Instant;

/// 进程内单调毫秒，状态机和执行器共用一个起点。
#[derive(Clone, Copy)]
pub struct Clock(Instant);

impl Clock {
    pub fn start() -> Self {
        Self(Instant::now())
    }

    pub fn now(&self) -> u64 {
        self.0.elapsed().as_millis() as u64
    }
}

/// 带时间戳的 stdout 日志（音箱上由 init 脚本转给 syslog）。
#[macro_export]
macro_rules! log {
    ($($arg:tt)*) => {{
        let secs = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0);
        // 东八区
        let s = (secs + 8 * 3600) % 86_400;
        println!("{:02}:{:02}:{:02} {}", s / 3600, s / 60 % 60, s % 60, format_args!($($arg)*));
    }};
}
