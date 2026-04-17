#!/usr/bin/env python3
"""
Seedance macOS App 打包脚本
===========================
在 macOS 上运行此脚本，生成双击即用的 .app 应用。

前提:
    pip install pyinstaller

用法:
    python3 build_macos.py

输出:
    dist/Seedance.app  (可直接复制到 /Applications)
"""

import os
import subprocess
import sys

APP_NAME = "Seedance"
APP_VERSION = "2.0"
BUNDLE_ID = "cn.seedance.app"


def check_pyinstaller():
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("错误: 未安装 pyinstaller")
        print("请先运行: pip install pyinstaller")
        sys.exit(1)


def build():
    check_pyinstaller()

    # 当前目录下的资源文件
    root = os.path.dirname(os.path.abspath(__file__))
    icon_path = os.path.join(root, "favicon.png")

    # PyInstaller 命令参数
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name", APP_NAME,
        "--windowed",          # macOS GUI 模式，不显示终端
        "--onedir",            # 单目录 .app（比 onefile 启动更快）
        "--osx-bundle-identifier", BUNDLE_ID,
        # 添加数据文件: src:dst
        "--add-data", f"{os.path.join(root, 'index.html')}:",
        "--add-data", f"{os.path.join(root, 'favicon.png')}:",
        # 隐藏导入（volcengine SDK 是可选的，如果已安装则包含）
        "--hidden-import", "volcengine.ApiInfo",
        "--hidden-import", "volcengine.Credentials",
        "--hidden-import", "volcengine.ServiceInfo",
        "--hidden-import", "volcengine.auth.SignerV4",
        "--hidden-import", "volcengine.base.Service",
        # 清理旧构建
        "--noconfirm",
        # 入口脚本
        os.path.join(root, "app.py"),
    ]

    # 如果存在图标，使用图标
    if os.path.exists(icon_path):
        # PyInstaller macOS 支持 .icns，这里用 PNG 需要借助其他工具转换
        # 暂时不设置图标，后续可以用 iconutil / sips 转换
        pass

    print("=" * 50)
    print(f"正在打包 {APP_NAME} v{APP_VERSION}...")
    print("=" * 50)
    print(" ".join(cmd))
    print()

    result = subprocess.run(cmd, cwd=root)
    if result.returncode != 0:
        print("\n打包失败，请查看上方错误信息。")
        sys.exit(1)

    app_path = os.path.join(root, "dist", f"{APP_NAME}.app")
    print(f"\n打包成功!")
    print(f"应用路径: {app_path}")
    print(f"\n你可以:")
    print(f"  1. 双击运行: open '{app_path}'")
    print(f"  2. 复制到应用目录: cp -r '{app_path}' /Applications/")


def convert_png_to_icns(png_path, output_dir):
    """将 PNG 转换为 macOS .icns 图标（可选）"""
    import tempfile
    import shutil

    base_name = os.path.splitext(os.path.basename(png_path))[0]
    iconset_dir = os.path.join(tempfile.gettempdir(), f"{base_name}.iconset")
    os.makedirs(iconset_dir, exist_ok=True)

    sizes = [16, 32, 64, 128, 256, 512]
    for size in sizes:
        dest = os.path.join(iconset_dir, f"icon_{size}x{size}.png")
        dest2x = os.path.join(iconset_dir, f"icon_{size}x{size}@2x.png")
        subprocess.run(["sips", "-z", str(size), str(size), png_path, "--out", dest], capture_output=True)
        subprocess.run(["sips", "-z", str(size * 2), str(size * 2), png_path, "--out", dest2x], capture_output=True)

    icns_path = os.path.join(output_dir, f"{base_name}.icns")
    subprocess.run(["iconutil", "-c", "icns", iconset_dir, "-o", icns_path], check=True)
    shutil.rmtree(iconset_dir)
    return icns_path


if __name__ == "__main__":
    build()
