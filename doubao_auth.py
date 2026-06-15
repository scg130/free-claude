"""豆包凭证刷新 CLI（兼容入口）。"""

import sys

from auth import main

if __name__ == "__main__":
    sys.argv = [sys.argv[0], "doubao", *sys.argv[1:]]
    main()
