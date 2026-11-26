#!/usr/bin/env python3
"""Build and verify the distributable skill, excluding personal/runtime data."""
import argparse
import hashlib
import os
from pathlib import Path
import stat
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
FILES = (
    ".gitignore", "SKILL.md", "README.md", "agents/openai.yaml",
    "references/history.md", "scripts/agy_runtime.py", "scripts/call.py", "scripts/history.py",
    "scripts/app_server.py", "scripts/build_package.py", "tests/test_call.py",
    "tests/test_history.py", "tests/test_app_server.py",
)


def build(destination):
    if destination.resolve().is_relative_to(ROOT):
        raise ValueError("安装包应保存在技能源码目录之外")
    sources = []
    for name in FILES:
        path = ROOT / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"缺少普通源文件：{name}")
        sources.append((name, path.read_bytes(), stat.S_IMODE(path.stat().st_mode)))
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as stream:
            temporary = Path(stream.name)
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data, mode in sources:
                entry = zipfile.ZipInfo(f"{ROOT.name}/{name}")
                entry.create_system = 3
                entry.external_attr = (stat.S_IFREG | mode) << 16
                entry.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(entry, data)
        with zipfile.ZipFile(temporary) as archive:
            assert archive.testzip() is None
            assert len(archive.infolist()) == len(sources)
            for name, data, mode in sources:
                entry = archive.getinfo(f"{ROOT.name}/{name}")
                assert archive.read(entry) == data == (ROOT / name).read_bytes()
                assert stat.S_IMODE(entry.external_attr >> 16) == mode
        temporary.chmod(0o644)
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return hashlib.sha256(destination.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", nargs="?", type=Path, default=ROOT.with_suffix(".zip"))
    args = parser.parse_args()
    destination = args.destination.expanduser().absolute()
    digest = build(destination)
    print(f"{destination}\n{len(FILES)} verified files\nSHA256 {digest}")


if __name__ == "__main__":
    main()
