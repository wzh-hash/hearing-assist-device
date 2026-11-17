"""生成 K10 界面用的 ASCII 字库（48px / 20px）。

界面只显示英文（DOOR / KNOCK / KETTLE / FIRE / CLOUD / WAIT / SCORE），
所以字库只需要 ASCII 0x20–0x7E，不需要中文字形（D-24）。

依赖 lv_font_conv（Node 工具）：

    npm i -g lv_font_conv
    python tools/make_board_fonts.py

或者指定本地的 lv_font_conv.js：

    python tools/make_board_fonts.py --lv-font-conv <path>/lv_font_conv.js

默认字体为 Windows 黑体 `C:/Windows/Fonts/simhei.ttf`，可用 `--font` 替换。
输出 `board/fonts/font_ui48.bin` 与 `font_ui20.bin`，上传到板子根目录。
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "board" / "fonts"
SIZES = ((48, "font_ui48.bin"), (20, "font_ui20.bin"))
DEFAULT_FONT = "C:/Windows/Fonts/simhei.ttf" if sys.platform == "win32" else ""


def main():
    parser = argparse.ArgumentParser(description="生成 K10 ASCII 字库")
    parser.add_argument("--font", default=DEFAULT_FONT, help="TTF/TTC 字体路径")
    parser.add_argument("--lv-font-conv", default="", help="lv_font_conv.js 的本地路径")
    args = parser.parse_args()

    if not args.font or not Path(args.font).is_file():
        print("找不到字体文件：%s（用 --font 指定）" % args.font)
        return 2
    if args.lv_font_conv:
        base = ["node", args.lv_font_conv]
    else:
        if shutil.which("npx") is None:
            print("找不到 npx：请先 npm i -g lv_font_conv")
            return 2
        base = ["npx", "--yes", "lv_font_conv"]

    OUT.mkdir(parents=True, exist_ok=True)
    for size, name in SIZES:
        command = base + [
            "--font", args.font,
            "--size", str(size),
            "--bpp", "4",
            "--format", "bin",
            "-r", "0x20-0x7E",
            "--no-compress",
            "--no-kerning",
            "-o", str(OUT / name),
        ]
        print(" ".join(command))
        result = subprocess.run(command)
        if result.returncode != 0:
            print("生成失败：%s" % name)
            return result.returncode
        print("%s  %d bytes" % (name, (OUT / name).stat().st_size))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
