"""Build a pinned upstream CLI locally. Explicit invocation requires network access."""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REVISION = "c353350232c35356b34085abf5780a2fcb81e80f"
SOURCE = ROOT / ".local/wirelog-v0.70.0"
TOOLS = ROOT / ".local/wirelog-tools"


def main() -> None:
    if not SOURCE.exists():
        subprocess.run(
            [
                "git",
                "clone",
                "--depth",
                "1",
                "--branch",
                "v0.70.0",
                "https://github.com/semantic-reasoning/wirelog.git",
                str(SOURCE),
            ],
            check=True,
        )
    actual = subprocess.check_output(
        ["git", "-C", str(SOURCE), "rev-parse", "HEAD"], text=True
    ).strip()
    if actual != REVISION:
        raise SystemExit("Refusing to build an unexpected upstream revision")
    if subprocess.check_output(
        ["git", "-C", str(SOURCE), "status", "--porcelain", "--untracked-files=no"],
        text=True,
    ).strip():
        raise SystemExit("Refusing to build modified tracked upstream sources")
    if not (TOOLS / "bin/python").exists():
        subprocess.run(["uv", "venv", str(TOOLS), "--python", "3.12"], check=True)
    subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(TOOLS / "bin/python"),
            "meson==1.9.1",
            "ninja==1.13.0",
        ],
        check=True,
    )
    env = os.environ | {"NINJA": str(TOOLS / "bin/ninja")}
    build = SOURCE / "build"
    setup = [str(TOOLS / "bin/meson"), "setup"]
    if (build / "meson-private/coredata.dat").exists():
        setup.append("--reconfigure")
    subprocess.run(
        setup
        + [
            str(build),
            str(SOURCE),
            "-Dtests=false",
            "-Dwirelog_log_max_level=error",
            "-Db_lto=false",
        ],
        env=env,
        check=True,
    )
    subprocess.run(
        [
            str(TOOLS / "bin/meson"),
            "compile",
            "-C",
            str(build),
            "-j",
            "2",
            "wirelog_cli",
        ],
        env=env,
        check=True,
    )
    subprocess.run([str(build / "wirelog_cli"), "--version"], check=True)
    print(f"Source revision: {REVISION}")
    print(f"CLI: {build / 'wirelog_cli'}")


if __name__ == "__main__":
    main()
