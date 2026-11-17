"""生成 K10 界面用的 ASCII 字库（消息 48px 粗体 / 状态 18px 常规）。

界面只显示英文（DOOR / KNOCK / KETTLE / FIRE / CLOUD / WAIT / SCORE），
所以只需要 ASCII 0x20–0x7E（D-24、D-25）。

依赖 lv_font_conv（Node 工具）：

    npm i -g lv_font_conv
    python tools/make_board_fonts.py

或者指定本地的 lv_font_conv.js：

    python tools/make_board_fonts.py --lv-font-conv <path>/lv_font_conv.js

默认字体：
- 消息 48px：`C:/Windows/Fonts/arialbd.ttf`（Arial Bold）
- 状态 18px：`C:/Windows/Fonts/arial.ttf`（Arial）

输出 `board/fonts/font_msg48.bin`、`board/fonts/font_meta18.bin`，
上传到板子根目录。
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "board" / "fonts"

#: (字号, 输出文件, 默认字体)
JOBS = (
    (48, "font_msg48.bin", "C:/Windows/Fonts/arialbd.ttf"),
    (18, "font_meta18.bin", "C:/Windows/Fonts/arial.ttf"),
)


def main():
    parser = argparse.ArgumentParser(description="生成 K10 ASCII 字库")
    parser.add_argument("--font-msg", default=JOBS[0][2], help="消息字库用的粗体 TTF")
    parser.add_argument("--font-meta", default=JOBS[1][2], help="状态字库用的常规 TTF")
    parser.add_argument("--lv-font-conv", default="", help="lv_font_conv.js 的本地路径")
    args = parser.parse_args()

    if args.lv_font_conv:
        base = ["node", args.lv_font_conv]
    else:
        if shutil.which("npx") is None:
            print("找不到 npx：请先 npm i -g lv_font_conv")
            return 2
        base = ["npx", "--yes", "lv_font_conv"]

    font_for = {"font_msg48.bin": args.font_msg, "font_meta18.bin": args.font_meta}
    OUT.mkdir(parents=True, exist_ok=True)

    for size, name, _default in JOBS:
        font_path = font_for[name]
        if not font_path or not Path(font_path).is_file():
            print("找不到字体：%s（用 --font-msg / --font-meta 指定）" % font_path)
            return 2
        command = base + [
            "--font", font_path,
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
