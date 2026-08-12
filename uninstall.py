# -*- coding: utf-8 -*-
"""txt2ebook 卸载程序
用法: 双击 uninstall.bat 或运行 python uninstall.py
按提示勾选要删除的内容(程序文件 / 本地书库数据 / Playwright / Calibre / Python)。
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent


def ask(prompt, default="n"):
    d = "y" if default == "y" else "n"
    while True:
        r = input(f"{prompt} (y/n, 默认 {d}): ").strip().lower()
        if not r:
            return default == "y"
        if r in ("y", "yes"):
            return True
        if r in ("n", "no"):
            return False


def run(cmd, **kw):
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=300, **kw)
    except Exception as e:
        print("  ⚠️ 执行失败:", e)
        return None


def stop_server():
    """只结束运行中的 txt2ebook 服务(命令行含 server.py 的 python 进程),
    绝不误杀其他 python 程序。"""
    print("\n[1/5] 停止运行中的服务...")
    ps = ("Get-CimInstance Win32_Process -Filter \"Name like '%python%'\" "
          "| Where-Object { $_.CommandLine -match 'server\\.py' } "
          "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
    run(["powershell", "-NoProfile", "-Command", ps])
    print("  已停止(如服务未运行则跳过)")


def delete_data():
    print("\n[2/5] 删除本地数据...")
    for name in ("library", "output", "backgrounds",
                 "readstate.json", "history.json", "config.json",
                 "backgrounds.json", "server_run.log", "server_run.err"):
        p = BASE / name
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
            print(f"  已删除目录 {name}/")
        elif p.is_file():
            p.unlink(missing_ok=True)
            print(f"  已删除文件 {name}")


def uninstall_playwright():
    print("\n[3/5] 卸载 Playwright(浏览器渲染组件)...")
    run([sys.executable, "-m", "pip", "uninstall", "-y", "playwright"])
    # 清理下载过的 Chromium 内核缓存(若存在)
    cache = Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"
    if cache.exists():
        shutil.rmtree(cache, ignore_errors=True)
        print("  已删除 Chromium 内核缓存(~150MB)")
    print("  Playwright 已卸载")


def uninstall_calibre():
    print("\n[4/5] 卸载 Calibre(转换引擎)...")
    r = run(["winget", "uninstall", "--id", "calibre.calibre",
             "--accept-source-agreements", "--disable-interactivity"])
    if r and r.returncode == 0:
        print("  Calibre 已卸载")
    else:
        print("  ⚠️ Calibre 卸载失败或未找到(可手动到 设置-应用 卸载)")


def uninstall_python():
    print("\n[5/5] 卸载 Python 3.13...")
    r = run(["winget", "uninstall", "--id", "Python.Python.3.13",
             "--accept-source-agreements", "--disable-interactivity"])
    if r and r.returncode == 0:
        print("  Python 3.13 已卸载")
    else:
        print("  ⚠️ Python 卸载失败或未找到(可手动到 设置-应用 卸载)")


def main():
    print("=" * 52)
    print("   txt2ebook 卸载程序")
    print("=" * 52)
    print(f"程序目录: {BASE}")
    print("卸载后可通过 GitHub 重新获取: "
          "github.com/2849506174-eng/txt2ebook-TXT-Kindle-")
    print()

    if not ask("确定卸载 txt2ebook 吗?"):
        print("已取消。")
        return

    stop_server()

    del_data = ask("\n删除本地数据(书库/输出/阅读记录/配置/背景图)?\n"
                   "  选 n 会保留这些文件,方便以后重装接着用", "n")
    if del_data:
        delete_data()

    print("\n以下依赖是可选安装的,请勾选要一并卸载的:")
    do_pw = ask("  卸载 Playwright(浏览器渲染组件,约 30MB+内核缓存)?", "n")
    do_cal = ask("  卸载 Calibre(转换引擎,约 200MB)?", "n")
    do_py = ask("  卸载 Python 3.13(程序运行环境,约 60MB)?", "n")

    if do_pw:
        uninstall_playwright()
    if do_cal:
        uninstall_calibre()
    if do_py:
        uninstall_python()

    # 删除程序文件(保留数据时只删程序相关文件)
    print("\n删除程序文件...")
    keep = not del_data
    for p in BASE.iterdir():
        name = p.name
        if name in ("uninstall.py", "uninstall.bat"):
            continue
        if keep and name in ("library", "output", "backgrounds",
                             "readstate.json", "history.json",
                             "config.json", "backgrounds.json"):
            continue  # 保留用户数据
        try:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)
        except OSError:
            pass
    print("  程序文件已删除")

    print("\n" + "=" * 52)
    print("   卸载完成 ✅")
    if keep:
        print("  已保留本地数据(书库/输出/配置)在程序目录中")
    print("  若安装了 Python/Calibre 之外的依赖(如 7-Zip),\n"
          "  可自行到 设置-应用 中卸载。")
    print("=" * 52)
    input("\n按回车退出...")


if __name__ == "__main__":
    main()
