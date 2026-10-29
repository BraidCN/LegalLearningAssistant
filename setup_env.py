"""创建虚拟环境并安装依赖。运行：python setup_env.py"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"


def main() -> int:
    print("Python:", sys.version.split()[0])
    if not (VENV / "Scripts" / "python.exe").exists():
        print("创建虚拟环境 .venv ...")
        subprocess.check_call([sys.executable, "-m", "venv", str(VENV)])
    py = VENV / "Scripts" / "python.exe"
    print("升级 pip ...")
    subprocess.check_call([str(py), "-m", "pip", "install", "--upgrade", "pip", "-q"])
    print("安装 jieba ...")
    subprocess.check_call([str(py), "-m", "pip", "install", "-q", "jieba"])
    subprocess.check_call([str(py), "-c", "import jieba; print('jieba', jieba.__version__)"])
    print("环境就绪：", py)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
