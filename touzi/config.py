"""配置加载。

只读 TOML（Python 3.11+ 标准库 tomllib），不改写配置文件。
对外暴露 `load_config()` 返回一个支持点号访问的 Config 对象。
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

# 项目根目录：touzi/config.py -> touzi/ -> 项目根
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "settings.toml"


class Config(dict):
    """支持 `cfg.pmi.strong_level` 这种点号访问的 dict。"""

    def __getattr__(self, name: str) -> Any:
        try:
            value = self[name]
        except KeyError as exc:  # pragma: no cover - 配置写错时的友好提示
            raise AttributeError(
                f"配置项 {name!r} 不存在，请检查 config/settings.toml"
            ) from exc
        if isinstance(value, dict) and not isinstance(value, Config):
            value = Config(value)
            self[name] = value
        return value

    def get_path(self, dotted: str, default: Any = None) -> Any:
        """按 "a.b.c" 路径取值，缺失时返回 default。"""
        node: Any = self
        for part in dotted.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return default
        return node

    def resolve(self, relative: str) -> Path:
        """把配置里的相对路径解析为项目根目录下的绝对路径。"""
        p = Path(relative)
        return p if p.is_absolute() else (PROJECT_ROOT / p)


def load_config(path: str | Path | None = None) -> Config:
    """读取配置文件。"""
    cfg_path = Path(path) if path else DEFAULT_CONFIG
    if not cfg_path.exists():
        raise FileNotFoundError(f"找不到配置文件：{cfg_path}")
    with cfg_path.open("rb") as fh:
        raw = tomllib.load(fh)
    # GitHub Actions 等云端环境没有本机代理。只要显式设置了环境变量，
    # 即使值为空也覆盖配置文件，避免去连接 127.0.0.1。
    if "TOUZI_PROXY" in os.environ:
        raw.setdefault("general", {})["proxy"] = os.environ["TOUZI_PROXY"].strip()
    cfg = Config(raw)
    cfg["_path"] = str(cfg_path)
    cfg["_root"] = str(PROJECT_ROOT)
    return cfg


__all__ = ["Config", "load_config", "PROJECT_ROOT", "DEFAULT_CONFIG"]
