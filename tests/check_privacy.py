r"""打包/提交前守卫：插件目录里不该出现的东西。

为什么需要它：MPDT 0.6.6 **不读 `.mfpignore`**，它只用硬编码规则排除
``__pycache__`` / ``.git`` / ``.venv`` / ``node_modules`` / ``*.pyc`` /
以 ``.`` 开头的文件。也就是说，**只要插件目录里出现 ``config/`` 或 ``data/``，
它们会被原样打进 ``.mfp``** —— 这正是"请求头 / API key / 私人网关地址被存进插件"
的传染路径。``.mfpignore`` 与 ``.gitignore`` 只能约束 git 等工具，拦不住打包器。

本脚本在打包前跑一次，命中任何一条就 FAIL，并给出处理建议。

用法：

    python <插件目录>\tests\check_privacy.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except Exception:  # noqa: BLE001, S110 —— 控制台不支持重配置时忽略即可，输出编码由终端决定
    pass

PLUGIN_DIR = Path(__file__).resolve().parents[1]

#: 目录名（任意层级）——出现即失败
FORBIDDEN_DIRS = {"config", "data", "logs", "log", ".venv", "node_modules"}

#: 文件名/后缀模式——出现即失败
FORBIDDEN_FILE_PATTERNS = (
    re.compile(r"\.log$", re.IGNORECASE),
    re.compile(r"\.local\..+$", re.IGNORECASE),
    re.compile(r"^\.env"),
    re.compile(r"\.toml$", re.IGNORECASE),  # 插件目录内不该有配置/凭据文件
)

#: 文本内容里的敏感模式
SENSITIVE_PATTERNS: dict[str, re.Pattern[str]] = {
    "APIKey": re.compile(
        r"(?i)(api[_-]?key\s*[=:]\s*[\"']?[A-Za-z0-9\-_]{8,}"
        r"|sk-[A-Za-z0-9]{12,}"
        r"|Bearer\s+[A-Za-z0-9\-\._~\+/]{8,}"
        r"|pst-[A-Za-z0-9]{4,})"
    ),
    "IPv4": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    # 允许的外部链接：依赖来源 / 文档站，外加**本机回环地址** —— 1.0.18 起
    # memory.base_url 的默认值是 http://localhost:8000/booku-memory（本机记忆库，
    # 不含任何凭据、也不是私人网段），属于文档里就该写出来的默认配置。
    "RequestURL": re.compile(
        r"https?://(?!github\.com|qm\.qq\.com|docs\.mofox\.chat|gravatar\.com"
        r"|fsf\.org|www\.gnu\.org|localhost|127\.0\.0\.1)[^\s\"'\)\]，。]+"
    ),
    "AbsPath": re.compile(
        r"[A-Za-z]:\\[^\s\"'\)\]]+|/(?:home|Users|root|mnt|opt)/[^\s\"'\)\]]+"
    ),
    "OfficialAuthor": re.compile(r"MoFox\s*Team"),
}

TEXT_SUFFIXES = {
    ".py",
    ".md",
    ".json",
    ".txt",
    ".toml",
    ".yml",
    ".yaml",
    ".cfg",
    ".ini",
    ".html",
    ".js",
    ".ts",
    ".rst",
    ".patch",
}

#: 允许出现的外部链接（依赖来源、文档站、本机回环地址）
ALLOWED_URL_HOSTS_HINT = "github.com / qm.qq.com / docs.mofox.chat / localhost"

_failures: list[str] = []


def fail(name: str, detail: str) -> None:
    """记录并打印一条失败项。"""
    print(f"[FAIL] {name}  — {detail}")
    _failures.append(name)


def warn(name: str, detail: str) -> None:
    """打印一条警告（不计入失败）。"""
    print(f"[WARN] {name}  — {detail}")


def ok(name: str, detail: str = "") -> None:
    """打印一条通过项。"""
    print(f"[PASS] {name}" + (f"  — {detail}" if detail else ""))


def scan_build_output() -> None:
    """提示 dist/ 的坑：默认输出会被打包器跳过，换输出目录就不会。"""
    dist = PLUGIN_DIR / "dist"
    if not dist.is_dir():
        return
    packages = sorted(p.name for p in dist.glob("*") if p.is_file())
    if packages:
        warn(
            "dist/ 内已有构建产物",
            f"{packages} —— 用 `mpdt plugin build`（默认输出）会跳过根级 dist；"
            "但用 `-o <其它目录>` 时 dist/ 不再被识别，旧包会被打进新包。打包前建议清空 dist/",
        )
    else:
        ok("dist/ 为空（不会被打包器误带）")


def scan_dirs() -> None:
    """检查禁止出现的目录。"""
    hits: list[str] = []
    for path in PLUGIN_DIR.rglob("*"):
        if path.is_dir() and path.name in FORBIDDEN_DIRS:
            # tests/__pycache__ 之类不算问题：打包器本来就会排除
            if path.name == "__pycache__":
                continue
            hits.append(str(path.relative_to(PLUGIN_DIR)))
    if hits:
        fail(
            "插件目录内不得出现运行期/凭据目录",
            "命中: " + ", ".join(sorted(set(hits))) + "（MPDT 会把它们打进包）",
        )
    else:
        ok("无运行期/凭据目录（config/ data/ logs/ 等）")


def scan_files() -> None:
    """检查禁止出现的文件。"""
    hits: list[str] = []
    for path in PLUGIN_DIR.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        rel = str(path.relative_to(PLUGIN_DIR))
        if any(pattern.search(path.name) for pattern in FORBIDDEN_FILE_PATTERNS):
            hits.append(rel)
    if hits:
        fail(
            "插件目录内不得出现日志/本地配置/凭据文件",
            "命中: " + ", ".join(sorted(hits)),
        )
    else:
        ok("无日志/本地配置/凭据文件（*.log、*.local.*、.env*、*.toml）")


def scan_contents() -> None:
    """检查文本内容里的敏感信息。"""
    total = 0
    for path in PLUGIN_DIR.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        if path.stat().st_size > 2 * 1024 * 1024:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        rel = str(path.relative_to(PLUGIN_DIR))
        for name, pattern in SENSITIVE_PATTERNS.items():
            matches = pattern.findall(text)
            if not matches:
                continue
            shown = []
            for value in matches[:3]:
                value = value if isinstance(value, str) else str(value)
                if value not in shown:
                    shown.append(value)
            fail(f"文本敏感信息[{name}]", f"{rel}: {' | '.join(shown)}")
            total += 1
    if total == 0:
        ok("文本无敏感信息（key/Bearer/IPv4/请求 URL/绝对路径/官方署名）")


def scan_manifest() -> None:
    """检查 manifest 的署名与 tags 规范。"""
    manifest_path = PLUGIN_DIR / "manifest.json"
    if not manifest_path.is_file():
        fail("manifest.json 存在", "找不到 manifest.json")
        return
    raw = manifest_path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        fail("manifest.json 无 BOM", "带 BOM 会导致解析失败")
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        fail("manifest.json 可解析", f"{type(e).__name__}: {e}")
        return

    author = str(manifest.get("author") or "")
    if author.strip().lower() in {"mofox team", "mofox", "官方"}:
        fail(
            "manifest 署名不是官方署名",
            f"author = {author!r}（框架模板的默认值，应改成你自己）",
        )
    else:
        ok("manifest 署名为作者本人", f"author = {author!r}")

    bad_tags = [t for t in (manifest.get("tags") or []) if " " in str(t)]
    if bad_tags:
        fail(
            "manifest tags 不含空格",
            f"含空格的标签: {bad_tags}（应写成 text-to-image-generation 这种连字符形式）",
        )
    else:
        ok("manifest tags 无空格", f"tags = {manifest.get('tags')}")


def main() -> int:
    """入口。"""
    print(f"插件目录 : {PLUGIN_DIR}")
    print("-" * 72)
    scan_dirs()
    scan_files()
    scan_contents()
    scan_manifest()
    scan_build_output()
    print("-" * 72)
    if _failures:
        print(f"✗ {len(_failures)} 项未通过，**先清理再打包**：")
        for name in _failures:
            print(f"    - {name}")
        print()
        print("提示：MPDT 只认硬编码排除规则，不读 .mfpignore / .gitignore；")
        print("      打包前请确认插件目录里没有 config/、data/、logs/ 与任何凭据文件。")
        return 1
    print("✓ 全部通过，可以打包")
    return 0


if __name__ == "__main__":
    sys.exit(main())
