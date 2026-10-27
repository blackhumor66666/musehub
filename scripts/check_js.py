"""提取前端模板里的 <script> 并用 node --check 校验语法.

用法: python scripts/check_js.py [模板路径]
"""
import re
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    tpl = Path(sys.argv[1] if len(sys.argv) > 1 else "app/web/templates/index.html")
    html = tpl.read_text(encoding="utf-8")
    m = re.search(r"<script>(.*)</script>", html, re.S)
    if not m:
        print("未找到 <script>")
        return 1
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as f:
        f.write(m.group(1))
        tmp = f.name
    r = subprocess.run(["node", "--check", tmp], capture_output=True, text=True)
    if r.returncode:
        print("JS 语法错误:\n" + r.stderr[:2000])
        return 1
    print("JS 语法 OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
