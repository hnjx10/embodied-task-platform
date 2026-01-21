"""仓库级密钥扫描：任何提交都不许出现真实凭据。

为什么这条要写成测试而不是靠 code review：凭据一旦进入 git 历史，删文件也清不掉，
只能改写历史 + 吊销重发。把检查放进 ``pytest``，它就会随 CI 在每次提交上跑。

模式串用拼接构造，避免测试文件自身命中自己。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

SKIP_DIRS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    "var",
    "htmlcov",
    "build",
    "dist",
    "node_modules",
}
SKIP_SUFFIXES = {
    ".pyc",
    ".pyo",
    ".db",
    ".db-wal",
    ".db-shm",
    ".jsonl",
    ".png",
    ".jpg",
    ".gif",
    ".pdf",
}
MAX_BYTES = 2 * 1024 * 1024

PATTERNS: dict[str, str] = {
    # GitHub：classic PAT / OAuth / 细粒度 PAT
    "github_classic": "gh" + r"[pousr]_[A-Za-z0-9]{36}",
    "github_fine_grained": "github" + r"_pat_[A-Za-z0-9_]{50,}",
    # 云厂商长期密钥
    "aws_access_key": "AK" + r"IA[0-9A-Z]{16}",
    # 模型服务密钥（测试里的 sk-test 这类短值不会命中）
    "openai_style": "sk" + r"-[A-Za-z0-9]{32,}",
    "slack": "xo" + r"x[abprs]-[A-Za-z0-9-]{10,}",
    # 私钥块
    "private_key": "-----BEGIN" + r" [A-Z ]*PRIVATE KEY-----",
}

COMPILED = {name: re.compile(pattern) for name, pattern in PATTERNS.items()}


def _iter_text_files() -> list[Path]:
    out: list[Path] = []
    for path in sorted(REPO_ROOT.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(REPO_ROOT)
        if any(part in SKIP_DIRS or part.endswith(".egg-info") for part in rel.parts[:-1]):
            continue
        if path.suffix in SKIP_SUFFIXES or path.name.startswith(".coverage"):
            continue
        try:
            if path.stat().st_size > MAX_BYTES:
                continue
        except OSError:
            continue
        out.append(path)
    return out


def test_no_credentials_in_repository() -> None:
    """工作区里任何文本文件都不该出现真实凭据字面量。"""
    findings: list[str] = []
    for path in _iter_text_files():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for name, regex in COMPILED.items():
            if regex.search(text):
                findings.append(f"{path.relative_to(REPO_ROOT)}: {name}")

    assert not findings, "疑似凭据泄漏，请改用环境变量或凭据文件：\n  " + "\n  ".join(findings)


@pytest.mark.parametrize("name", sorted(PATTERNS))
def test_scanner_detects_planted_secret(name: str, tmp_path: Path) -> None:
    """扫描器自身要有效——用构造出来的假凭据验证它真的能命中。"""
    samples = {
        "github_classic": "gh" + "p_" + "a" * 36,
        "github_fine_grained": "github" + "_pat_" + "b" * 60,
        "aws_access_key": "AK" + "IA" + "C" * 16,
        "openai_style": "sk" + "-" + "d" * 40,
        "slack": "xo" + "xb-" + "1234567890abc",
        "private_key": "-----BEGIN" + " RSA PRIVATE KEY-----",
    }
    assert COMPILED[name].search(samples[name]), f"{name} 的检测规则失效了"
