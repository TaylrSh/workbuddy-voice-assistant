"""构建可直接上传到 WorkBuddy 开放平台的独立技能 ZIP。"""

from __future__ import annotations

import shutil
import tempfile
import zipfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
SKILL_SRC = BASE / "skills" / "workbuddy-voice"
OUT_DIR = BASE / "dist"
OUT_ZIP = OUT_DIR / "workbuddy-voice-assistant.zip"

SOURCE_FILES = [
    "README.md",
    "LICENSE",
    "requirements.txt",
    "启动语音助手.bat",
]


def copy_app(stage_templates: Path) -> None:
    stage_templates.mkdir(parents=True, exist_ok=True)

    for path in sorted(BASE.glob("*.py")):
        if path.name in {"verify.py"}:
            continue
        shutil.copy2(path, stage_templates / path.name)

    for name in SOURCE_FILES:
        src = BASE / name
        if src.exists():
            shutil.copy2(src, stage_templates / name)

    # Official Skill packages support at most two directory levels.
    # Keep icons as flat files under templates/ instead of assets/.
    for name in ("app-icon.png", "preview.png"):
        src = BASE / "assets" / name
        if src.exists():
            shutil.copy2(src, stage_templates / name)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if OUT_ZIP.exists():
        OUT_ZIP.unlink()

    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp) / "workbuddy-voice"
        shutil.copytree(SKILL_SRC, stage)
        copy_app(stage / "templates")

        max_depth = max(
            (len(path.relative_to(stage).parts) for path in stage.rglob("*")),
            default=0,
        )
        if max_depth > 2:
            raise RuntimeError(f"Package exceeds two directory levels: {max_depth}")

        forbidden = [
            path
            for path in stage.rglob("*")
            if path.suffix.lower() in {".log", ".wav"}
            or path.name
            in {
                "voice-assistant-config.json",
                "overlay-position.json",
            }
        ]
        if forbidden:
            raise RuntimeError(f"Personal data found in package: {forbidden}")

        with zipfile.ZipFile(OUT_ZIP, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(stage.rglob("*")):
                if path.is_file():
                    zf.write(path, path.relative_to(stage.parent))

    print(f"Built: {OUT_ZIP}")
    print(f"Size: {OUT_ZIP.stat().st_size} bytes")


if __name__ == "__main__":
    main()
