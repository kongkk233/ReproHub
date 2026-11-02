#!/usr/bin/env python3
"""写入离线演示样例（等价于 `reprohub demo-seed`）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reprohub.db import get_conn  # noqa: E402
from reprohub.demo import seed  # noqa: E402
from reprohub.settings import get_settings  # noqa: E402

if __name__ == "__main__":
    s = get_settings()
    print(f"写入 {seed(s, get_conn(s))} 条演示样例")
