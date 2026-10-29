"""拉取 just-laws 数据快照。

优先用 tarball 下载（Python 的 HTTPS 可用），避免 git 在部分 Windows 机器上
因 schannel 证书链报 SEC_E_UNTRUSTED_ROOT。
"""
import json
import shutil
import ssl
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
DEST = RAW / "just-laws"
TMP = RAW / "just-laws.tar.gz"
TMPDIR = RAW / "_tmp_justlaws"
REPO = "ImCa0/just-laws"


def _open(url, timeout=60):
    try:
        return urllib.request.urlopen(url, timeout=timeout)
    except Exception:
        return urllib.request.urlopen(url, timeout=timeout, context=ssl._create_unverified_context())


def default_branch() -> str:
    try:
        with _open("https://api.github.com/repos/" + REPO) as resp:
            return json.loads(resp.read().decode("utf-8")).get("default_branch", "master")
    except Exception:
        return "master"


def download(url: str, dest: Path) -> None:
    total = 0
    with _open(url, timeout=300) as resp, dest.open("wb") as fh:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)
            total += len(chunk)
    print("下载完成 %.1f MB" % (total / 1e6))


def main() -> int:
    RAW.mkdir(parents=True, exist_ok=True)
    if (DEST / "docs").exists():
        print("已存在快照：", DEST)
        return 0
    branch = default_branch()
    url = "https://codeload.github.com/%s/tar.gz/refs/heads/%s" % (REPO, branch)
    print("下载", url)
    download(url, TMP)

    if TMPDIR.exists():
        shutil.rmtree(TMPDIR)
    print("解压 ...")
    with tarfile.open(TMP) as tf:
        members = []
        for m in tf.getmembers():
            parts = m.name.split("/", 1)
            if len(parts) < 2:
                continue
            m.name = parts[1]
            members.append(m)
        tf.extractall(TMPDIR, members=members, filter="data")
    TMPDIR.rename(DEST)
    TMP.unlink(missing_ok=True)
    print("完成：", DEST)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
