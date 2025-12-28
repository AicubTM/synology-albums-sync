"""Reusable building blocks for the Synology Photos Team Space synchronizer."""

__version__ = "0.1.0-dev"

from .config import (
	APP_CONFIG,
	RUNTIME_STATE,
	AppConfig,
	RuntimeState,
	ConfigError,
	ConfigFileError,
	MissingEnvError,
	build_runtime_state,
	load_app_config,
)
from .synology_api import SynologyPhotosAPI
from .synology_web import SynologyWebSharing

__all__ = [
	"__version__",
	"APP_CONFIG",
	"RUNTIME_STATE",
	"AppConfig",
	"RuntimeState",
	"ConfigError",
	"ConfigFileError",
	"MissingEnvError",
	"build_runtime_state",
	"load_app_config",
	"SynologyPhotosAPI",
	"SynologyWebSharing",
]
