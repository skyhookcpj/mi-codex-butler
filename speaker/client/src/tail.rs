//! 轮询式 tail。音箱上的 `instruction.log` 每轮对话被清空重写（不是删了重建），
//! 只比大小会漏掉「清空后又写得比原来长」，所以还比对文件开头：每轮 dialog_id 不同，开头必变。

use std::fs::File;
use std::io::{Read, Seek, SeekFrom};
use std::os::unix::fs::MetadataExt;
use std::path::PathBuf;

const HEAD_BYTES: usize = 96;
const MAX_PARTIAL: usize = 64 * 1024;

pub struct Tail {
    path: PathBuf,
    pos: u64,
    ino: Option<u64>,
    head: Vec<u8>,
    partial: Vec<u8>,
}

impl Tail {
    /// `from_end = true`：启动前已有的内容不要（老对话、老唤醒）。
    pub fn new(path: impl Into<PathBuf>, from_end: bool) -> Self {
        let mut t = Self { path: path.into(), pos: 0, ino: None, head: Vec::new(), partial: Vec::new() };
        if from_end {
            if let Ok(mut f) = File::open(&t.path) {
                if let Ok(meta) = f.metadata() {
                    t.pos = meta.len();
                    t.ino = Some(meta.ino());
                    t.head = read_head(&mut f);
                }
            }
        }
        t
    }

    /// 读出自上次以来新写完的整行。半行留到下次。
    pub fn poll(&mut self) -> Vec<String> {
        let Ok(mut f) = File::open(&self.path) else {
            self.ino = None;
            self.pos = 0;
            self.head.clear();
            self.partial.clear();
            return Vec::new();
        };
        let Ok(meta) = f.metadata() else { return Vec::new() };
        let len = meta.len();
        let head = read_head(&mut f);

        let replaced = self.ino.is_some_and(|ino| ino != meta.ino());
        let shrunk = len < self.pos;
        let rewritten = !self.head.is_empty() && !head.starts_with(&self.head);
        if replaced || shrunk || rewritten {
            self.pos = 0;
            self.partial.clear();
        }
        self.ino = Some(meta.ino());
        self.head = head;

        if len <= self.pos {
            return Vec::new();
        }
        if f.seek(SeekFrom::Start(self.pos)).is_err() {
            return Vec::new();
        }
        let mut buf = Vec::with_capacity((len - self.pos) as usize);
        let Ok(n) = f.take(len - self.pos).read_to_end(&mut buf) else { return Vec::new() };
        self.pos += n as u64;
        self.partial.extend_from_slice(&buf);

        let mut lines = Vec::new();
        while let Some(i) = self.partial.iter().position(|&b| b == b'\n') {
            let line: Vec<u8> = self.partial.drain(..=i).collect();
            let s = String::from_utf8_lossy(&line).trim().to_string();
            if !s.is_empty() {
                lines.push(s);
            }
        }
        if self.partial.len() > MAX_PARTIAL {
            self.partial.clear();
        }
        lines
    }
}

fn read_head(f: &mut File) -> Vec<u8> {
    let mut head = vec![0u8; HEAD_BYTES];
    if f.seek(SeekFrom::Start(0)).is_err() {
        return Vec::new();
    }
    let mut n = 0;
    while n < HEAD_BYTES {
        match f.read(&mut head[n..]) {
            Ok(0) | Err(_) => break,
            Ok(k) => n += k,
        }
    }
    head.truncate(n);
    head
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs::{self, OpenOptions};
    use std::io::Write;

    fn append(path: &std::path::Path, s: &str) {
        OpenOptions::new().create(true).append(true).open(path).unwrap().write_all(s.as_bytes()).unwrap();
    }

    #[test]
    fn skips_existing_content_and_reads_new_lines() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("a.log");
        append(&p, "old line\n");
        let mut t = Tail::new(&p, true);
        assert!(t.poll().is_empty());
        append(&p, "one\ntw");
        assert_eq!(t.poll(), vec!["one"]);
        append(&p, "o\n");
        assert_eq!(t.poll(), vec!["two"]);
    }

    #[test]
    fn waits_for_missing_file_then_reads_from_start() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("kws.log");
        let mut t = Tail::new(&p, true);
        assert!(t.poll().is_empty());
        append(&p, "1@你好管家\n");
        assert_eq!(t.poll(), vec!["1@你好管家"]);
    }

    #[test]
    fn truncate_and_rewrite_longer_is_detected() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("instruction.log");
        append(&p, "{\"dialog_id\":\"aaaa\"} first\n");
        let mut t = Tail::new(&p, false);
        assert_eq!(t.poll().len(), 1);
        // 新一轮对话：清空后一口气写得比原来还长，大小没有变小
        fs::write(&p, "{\"dialog_id\":\"bbbb\"} new dialog line one\n{\"dialog_id\":\"bbbb\"} two\n").unwrap();
        let lines = t.poll();
        assert_eq!(lines.len(), 2, "{lines:?}");
        assert!(lines[0].contains("bbbb"));
    }

    #[test]
    fn plain_truncate_is_detected() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("instruction.log");
        append(&p, "long long long long line\n");
        let mut t = Tail::new(&p, true);
        fs::write(&p, "").unwrap();
        assert!(t.poll().is_empty());
        append(&p, "x\n");
        assert_eq!(t.poll(), vec!["x"]);
    }

    #[test]
    fn replaced_file_is_detected() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("kws.log");
        append(&p, "1@a\n");
        let mut t = Tail::new(&p, true);
        let tmp = dir.path().join("new");
        fs::write(&tmp, "1@a\n2@b\n").unwrap();
        fs::rename(&tmp, &p).unwrap();
        assert_eq!(t.poll(), vec!["1@a", "2@b"]);
    }
}
