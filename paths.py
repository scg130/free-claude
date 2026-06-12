from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
PARAM_DIR = ROOT_DIR / "params"
PROFILES_DIR = ROOT_DIR / ".profiles"


def provider_param_dir(provider_id: str) -> Path:
    return PARAM_DIR / provider_id


def provider_param_file(provider_id: str, filename: str = "session.json") -> Path:
    return provider_param_dir(provider_id) / filename


def provider_profile_dir(provider_id: str) -> Path:
    return PROFILES_DIR / provider_id


def ensure_provider_dir(provider_id: str) -> None:
    provider_param_dir(provider_id).mkdir(parents=True, exist_ok=True)
