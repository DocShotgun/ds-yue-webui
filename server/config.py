"""Server configuration: defaults < config.yaml < environment < CLI flags."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

RESIDENCY_MODES = ("on-demand", "always")
FORMATS = {"flac", "mp3"}


class ConfigError(ValueError):
    """Invalid configuration value."""


def _root() -> Path:
    return Path(__file__).resolve().parents[1]


@dataclass
class Settings:
    root: Path
    config_path: Path
    host: str = "0.0.0.0"
    port: int = 8765
    data_dir: Path = None  # type: ignore[assignment]
    residency: str = "on-demand"
    release_idle_minutes: float = 10.0
    offline: bool = False
    memory_budget_gib: float = 24.0
    yue2_dir: Path = None  # type: ignore[assignment]
    yue2_model: str = "m-a-p/YuE2-3B"
    yue2_vae: str = "m-a-p/YuE2-Vae"
    yue2_vae_legacy: str = "m-a-p/YuE2-Vae-legacy"
    yue2_device: str = "auto"
    yue2_backend: str = "torch"
    yue2_quantization: str = "none"
    sheetsage2_dir: Path = None  # type: ignore[assignment]
    sheetsage2_model: str = "m-a-p/SheetSage2"
    sheetsage2_device: str = "cuda"
    sheetsage2_dtype: str = "bf16"
    worker_python: Path = None  # type: ignore[assignment]
    worker_python_yue2: Path = None  # type: ignore[assignment]
    worker_python_sheetsage2: Path = None  # type: ignore[assignment]
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_sources(cls, config_path: Path | str | None = None, environ=None) -> "Settings":
        environ = os.environ if environ is None else environ
        root = _root()
        path = Path(config_path) if config_path else (
            Path(environ["YUE_WEBUI_CONFIG"]) if environ.get("YUE_WEBUI_CONFIG") else root / "config.yaml")
        raw = {}
        if path.is_file():
            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
            raw = loaded if isinstance(loaded, dict) else {}
            if not isinstance(raw, dict):
                raise ConfigError(f"{path}: top-level YAML must be a mapping")
        settings = cls(root=root, config_path=path, raw=dict(raw))
        settings.apply_raw(raw)
        settings.apply_environ(environ)
        settings.finalize()
        return settings

    def apply_raw(self, raw: dict) -> None:
        simple = {"host": str, "port": int, "residency": str, "release_idle_minutes": float,
                  "offline": bool, "memory_budget_gib": float}
        for key, kind in simple.items():
            if key in raw and raw[key] is not None:
                setattr(self, key, self._coerce(key, raw[key], kind))
        yue2 = raw.get("yue2") or {}
        sheetsage = raw.get("sheetsage2") or {}
        worker = raw.get("worker") or {}
        if not isinstance(yue2, dict) or not isinstance(sheetsage, dict) or not isinstance(worker, dict):
            raise ConfigError("yue2:, sheetsage2: and worker: sections must be mappings")
        for key in ("dir", "model", "vae", "vae_legacy", "device", "backend", "quantization"):
            if key in yue2 and yue2[key] is not None:
                setattr(self, "yue2_" + ("dir" if key == "dir" else key), str(yue2[key]))
        for key in ("dir", "model", "device", "dtype"):
            if key in sheetsage and sheetsage[key] is not None:
                setattr(self, "sheetsage2_" + key, str(sheetsage[key]))
        for key in ("python", "python_yue2", "python_sheetsage2"):
            if key in worker and worker[key] is not None:
                setattr(self, "worker_" + key, str(worker[key]))

    def apply_environ(self, environ: dict) -> None:
        if environ.get("YUE_WEBUI_PORT"):
            self.port = self._coerce("port", environ["YUE_WEBUI_PORT"], int)
        if environ.get("YUE_WEBUI_HOST"):
            self.host = str(environ["YUE_WEBUI_HOST"])
        if environ.get("YUE_WEBUI_RESIDENCY"):
            self.residency = self._check_residency(str(environ["YUE_WEBUI_RESIDENCY"]))
        if environ.get("YUE_WEBUI_DATA"):
            self.data_dir = Path(environ["YUE_WEBUI_DATA"])
        if environ.get("YUE_WEBUI_OFFLINE"):
            self.offline = self._as_bool(environ["YUE_WEBUI_OFFLINE"])
        if environ.get("YUE_WEBUI_YUE2_DIR"):
            self.yue2_dir = Path(environ["YUE_WEBUI_YUE2_DIR"])
        if environ.get("YUE_WEBUI_SHEETSAGE_DIR"):
            self.sheetsage2_dir = Path(environ["YUE_WEBUI_SHEETSAGE_DIR"])

    def finalize(self) -> None:
        config = self.config_path.parent
        self.data_dir = (config / self.data_dir if self.data_dir and not Path(self.data_dir).is_absolute()
                         else (self.data_dir or config / "data")).resolve()
        # Local checkouts are opt-in: set yue2.dir / sheetsage2.dir in config.yaml
        # (install.sh writes them when --yue-dir / --sheetsage-dir are passed) or
        # via the YUE_WEBUI_YUE2_DIR / YUE_WEBUI_SHEETSAGE_DIR environment. Without
        # them the runtime installs from GitHub and abc_tools comes from the
        # vendored copy in vendor/yue2/.
        self.yue2_dir = self._resolve_dir(self.yue2_dir) if self.yue2_dir else None
        self.sheetsage2_dir = self._resolve_dir(self.sheetsage2_dir) if self.sheetsage2_dir else None
        default_python = Path(sys.executable)
        self.worker_python = self._python(self.worker_python, default_python, "worker.python")
        self.worker_python_yue2 = self._python(self.worker_python_yue2, self.worker_python, "worker.python_yue2")
        self.worker_python_sheetsage2 = self._python(self.worker_python_sheetsage2, self.worker_python,
                                                     "worker.python_sheetsage2")
        self.residency = self._check_residency(self.residency)
        if not 1 <= int(self.port) <= 65535:
            raise ConfigError(f"port must be in [1, 65535], got {self.port}")
        if not 0 < float(self.memory_budget_gib):
            raise ConfigError(f"memory_budget_gib must be positive, got {self.memory_budget_gib}")
        if float(self.release_idle_minutes) < 0:
            raise ConfigError(f"release_idle_minutes must be >= 0, got {self.release_idle_minutes}")
        if self.sheetsage2_model == "auto":
            # A local checkout is only usable as a model when it also carries weights;
            # otherwise fall back to the HF Hub repo (downloads on first use).
            weights = any(self.sheetsage2_dir.glob("*.safetensors")) if self.sheetsage2_dir else False
            self.sheetsage2_model = str(self.sheetsage2_dir) \
                if self.sheetsage2_dir and (self.sheetsage2_dir / "config.json").is_file() \
                and weights else "m-a-p/SheetSage2"
        if self.sheetsage2_dtype not in (None, "bf16", "fp32"):
            raise ConfigError(f"sheetsage2.dtype must be bf16 or fp32, got {self.sheetsage2_dtype}")
        self.sheetsage2_device = self.sheetsage2_device or "cuda"
        if self.yue2_backend not in ("torch", "torch-eager", "vllm"):
            raise ConfigError(f"yue2.backend must be torch, torch-eager or vllm, got {self.yue2_backend!r}")
        if self.yue2_quantization not in ("none", "fp8"):
            raise ConfigError(f"yue2.quantization must be none or fp8, got {self.yue2_quantization!r}")
        if not str(self.yue2_device).strip():
            raise ConfigError("yue2.device must be a nonempty device string (auto, cuda, cpu)")

    @staticmethod
    def _resolve_dir(value) -> Path:
        path = Path(value).expanduser()
        return path.resolve()

    @staticmethod
    def _python(value, default: Path, label: str) -> Path | None:
        if value is None or (isinstance(value, str) and value == "auto"):
            return default
        path = Path(value).expanduser()
        if not path.is_file():
            raise ConfigError(f"{label}: interpreter not found: {path}")
        return path.resolve()

    @staticmethod
    def _check_residency(value: str) -> str:
        if value not in RESIDENCY_MODES:
            raise ConfigError(f"residency must be one of {RESIDENCY_MODES}, got {value!r}")
        return value

    @staticmethod
    def _as_bool(value) -> bool:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _coerce(key: str, value, kind):
        try:
            if kind is bool:
                return Settings._as_bool(value)
            return kind(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"invalid {key}: {value!r} ({exc})") from exc

    def update_runtime(self, **fields) -> None:
        """Persist residency/release_idle_minutes style overrides back to config.yaml."""
        simple = {"residency", "release_idle_minutes"}
        unknown = set(fields) - simple
        if unknown:
            raise ConfigError(f"Uneditable config keys: {sorted(unknown)}")
        if "residency" in fields:
            fields["residency"] = self._check_residency(str(fields["residency"]))
        if "release_idle_minutes" in fields:
            value = self._coerce("release_idle_minutes", fields["release_idle_minutes"], float)
            if value < 0:
                raise ConfigError("release_idle_minutes must be >= 0")
            fields["release_idle_minutes"] = value
        raw = dict(self.raw)
        for key, value in fields.items():
            raw[key] = value
            setattr(self, key, value)
        self.raw = raw
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8")

    # Derived paths ---------------------------------------------------------
    @property
    def specs_dir(self) -> Path:
        return self.data_dir / "specs"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def outputs_dir(self) -> Path:
        return self.data_dir / "outputs"

    @property
    def plans_dir(self) -> Path:
        return self.data_dir / "plans"

    @property
    def transcripts_dir(self) -> Path:
        return self.data_dir / "transcripts"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def web_dir(self) -> Path:
        return self.root / "web"

    @property
    def smoke_result_path(self) -> Path:
        return self.data_dir / "smoke-result.json"

    @property
    def abc_tools_path(self) -> Path:
        if self.yue2_dir is None:
            raise FileNotFoundError(
                "no YuE checkout configured: set yue2.dir in config.yaml (or pass "
                "--yue-dir) and run install.sh again to refresh the vendored abc_tools")
        return self.yue2_dir / "skills" / "yue2-music" / "scripts" / "abc_tools.py"

    @property
    def vendored_abc_tools_path(self) -> Path:
        return self.root / "vendor" / "yue2" / "abc_tools.py"

    def resolved_abc_tools_path(self) -> Path:
        """Vendored copy first; the YuE checkout is only needed at install time."""
        vendor = self.vendored_abc_tools_path
        if vendor.is_file():
            return vendor
        if self.yue2_dir:
            return self.abc_tools_path
        return vendor

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.specs_dir, self.logs_dir, self.outputs_dir, self.plans_dir,
                     self.transcripts_dir, self.uploads_dir, self.cache_dir):
            path.mkdir(parents=True, exist_ok=True)

    def snapshot(self) -> dict:
        return {
            "host": self.host, "port": self.port,
            "data_dir": str(self.data_dir),
            "residency": self.residency,
            "release_idle_minutes": self.release_idle_minutes,
            "offline": self.offline,
            "memory_budget_gib": self.memory_budget_gib,
            "yue2": {"dir": str(self.yue2_dir) if self.yue2_dir else None,
                     "model": self.yue2_model, "vae": self.yue2_vae,
                     "vae_legacy": self.yue2_vae_legacy,
                     "abc_tools": str(self.resolved_abc_tools_path())},
            "sheetsage2": {"dir": str(self.sheetsage2_dir) if self.sheetsage2_dir else None,
                           "model": self.sheetsage2_model,
                           "device": self.sheetsage2_device, "dtype": self.sheetsage2_dtype},
            "worker": {"python": str(self.worker_python), "python_yue2": str(self.worker_python_yue2),
                       "python_sheetsage2": str(self.worker_python_sheetsage2)},
            "config_path": str(self.config_path),
        }
