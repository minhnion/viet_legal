"""Environment-only connection and dataset configuration; no provider defaults."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import hashlib
import json
import os
import re
import math
from urllib.parse import urlsplit

from dotenv import dotenv_values


class ConfigError(ValueError):
    pass


# Model-backed stages: ISSUE builds issues/queries from court citations, EVIDENCE confirms sources and picks clauses.
STAGES = ("ISSUE", "EVIDENCE", "AUGMENT")


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def boolean(value: str, name: str) -> bool:
    if value.lower() not in {"true", "false", "1", "0", "yes", "no"}:
        raise ConfigError(f"{name} must be true or false")
    return value.lower() in {"true", "1", "yes"}


@dataclass(frozen=True)
class Profile:
    name: str
    base_url: str
    model: str
    api_key: str = field(repr=False)
    auth_mode: str = "bearer"
    output_mode: str = "json_schema"
    token_parameter: str = "max_tokens"
    max_output_tokens: int = 2048
    context_tokens: int = 32768
    timeout: float = 180
    retries: int = 2
    retry_backoff: float = 1
    concurrency: int = 1
    temperature: float | None = None
    extra_body: dict = field(default_factory=dict)
    input_modalities: tuple = ("text",)
    instruction_role: str = "system"
    token_margin: int = 512

    def public(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k != "api_key"}


@dataclass(frozen=True)
class Config:
    env_file: Path
    judgment_path: Path
    tvpl_path: Path
    phapdien_path: Path
    output_dir: Path
    prompts_file: Path
    profiles: dict[str, Profile]
    stage_profiles: dict[str, str]
    law_max_records: int = 0
    case_max_records: int = 0
    annotation_limit: int = 50
    categories: tuple = ()
    case_ids: tuple = ()
    seed: int = 20261115
    max_issues: int = 3
    fail_fast: bool = False
    train_ratio: float = 0.8
    dev_ratio: float = 0.1
    family_map: Path | None = None
    fewshot_file: Path | None = None
    vision_manifest: Path | None = None
    official_domains: tuple = ("vbpl.vn", "vbpl.moj.gov.vn", "vanban.chinhphu.vn", "congbao.chinhphu.vn", "phapdien.moj.gov.vn", "datafiles.chinhphu.vn")
    # Per-stage max output tokens (e.g. {"EVIDENCE": 1536}); stages not listed use the profile default.
    stage_max_output_tokens: dict = field(default_factory=dict)
    # Optional Langfuse OTLP tracing; disabled unless RR_LANGFUSE_HOST is set.
    langfuse_host: str | None = None
    langfuse_public_key: str = ""
    langfuse_secret_key: str = field(default="", repr=False)
    langfuse_capture_io: bool = False

    @property
    def db_path(self) -> Path:
        return self.output_dir / "pipeline.sqlite3"

    def profile(self, stage: str) -> Profile:
        name = self.stage_profiles.get(stage)
        if name not in self.profiles:
            raise ConfigError(f"No configured profile for stage {stage}")
        return self.profiles[name]

    def public(self) -> dict:
        result = {}
        for key, value in self.__dict__.items():
            if key == "langfuse_secret_key":
                continue
            if key == "profiles":
                result[key] = {n: p.public() for n, p in value.items()}
            elif isinstance(value, Path):
                result[key] = str(value)
            else:
                result[key] = value
        return result


def load_config(path: str | Path) -> Config:
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise ConfigError(f"Environment file does not exist: {path}")
    # Do not expand $() or resolve interpolation against unrelated process secrets.
    values = {k: v for k, v in dotenv_values(path, interpolate=False).items() if v is not None}
    values.update({k: v for k, v in os.environ.items() if k.startswith("RR_")})

    def get(name, default=None):
        value = values.get("RR_" + name, default)
        if value is None or value == "":
            if default is None:
                raise ConfigError(f"RR_{name} is required")
            return default
        return value

    def number(name, default, minimum=0, integer=True):
        try:
            n = int(get(name, str(default))) if integer else float(get(name, str(default)))
        except (ValueError, TypeError):
            raise ConfigError(f"RR_{name} must be a number") from None
        if not math.isfinite(n) or n < minimum:
            raise ConfigError(f"RR_{name} must be >= {minimum}")
        return n

    def resolve(name, default=None, optional=False):
        value = get(name, "" if optional else default)
        if optional and not value:
            return None
        p = Path(value).expanduser()
        return (path.parent / p).resolve() if not p.is_absolute() else p.resolve()

    def array(name, default="[]"):
        try:
            value = json.loads(get(name, default))
        except (ValueError, TypeError):
            raise ConfigError(f"RR_{name} must be a JSON array") from None
        if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
            raise ConfigError(f"RR_{name} must be an array of strings")
        return tuple(value)

    names = [n.strip().upper() for n in get("PROFILES").split(",") if n.strip()]
    if not names or len(names) != len(set(names)):
        raise ConfigError("RR_PROFILES must contain unique profile names")
    profiles = {}
    reserved = {"model", "messages", "response_format", "stream", "max_tokens", "max_completion_tokens", "api_key", "authorization", "temperature"}
    for name in names:
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
            raise ConfigError("Profile names must use uppercase letters, digits and underscores")
        prefix = f"{name}_"
        url = get(prefix + "BASE_URL").rstrip("/")
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise ConfigError(f"RR_{prefix}BASE_URL must be an HTTP API root without credentials/query/fragment")
        if parts.path.endswith(("/chat/completions", "/responses")):
            raise ConfigError(f"RR_{prefix}BASE_URL must be an API root, not a method URL")
        auth = get(prefix + "AUTH_MODE", "bearer")
        key = get(prefix + "API_KEY", "")
        if auth not in {"none", "bearer"}:
            raise ConfigError(f"Invalid auth mode for profile {name}")
        if auth == "bearer" and not key.strip():
            raise ConfigError(f"RR_{prefix}API_KEY is required for bearer authentication")
        output_mode = get(prefix + "OUTPUT_MODE", "json_schema")
        token_param = get(prefix + "TOKEN_PARAMETER", "max_tokens")
        if output_mode not in {"json_schema", "json_object", "prompt_json"}:
            raise ConfigError(f"Invalid output mode for {name}")
        if token_param not in {"max_tokens", "max_completion_tokens"}:
            raise ConfigError(f"Invalid token parameter for {name}")
        try:
            extra = json.loads(get(prefix + "EXTRA_BODY", "{}"))
            temp = get(prefix + "TEMPERATURE", "")
            temp = float(temp) if temp else None
        except (ValueError, TypeError):
            raise ConfigError(f"Invalid generation configuration for {name}") from None
        if not isinstance(extra, dict) or reserved.intersection(k.lower() for k in extra):
            raise ConfigError(f"Extra body for {name} cannot override standard or credential fields")
        if temp is not None and not 0 <= temp <= 2:
            raise ConfigError(f"Temperature for {name} must be between 0 and 2")
        modalities = array(prefix + "MODALITIES", '["text"]')
        if not set(modalities) <= {"text", "image"} or "text" not in modalities:
            raise ConfigError(f"Invalid modalities for {name}")
        role = get(prefix + "INSTRUCTION_ROLE", "system")
        if role not in {"system", "developer", "user"}:
            raise ConfigError(f"Invalid instruction role for {name}")
        profile = Profile(
            name, url, get(prefix + "MODEL"), key, auth, output_mode, token_param,
            number(prefix + "MAX_OUTPUT_TOKENS", 2048, 128),
            number(prefix + "CONTEXT_TOKENS", 32768, 1024),
            number(prefix + "TIMEOUT", 180, 1, False),
            number(prefix + "RETRIES", 2), number(prefix + "RETRY_BACKOFF", 1, 0, False),
            number(prefix + "CONCURRENCY", 1, 1), temp, extra, modalities, role,
            number(prefix + "TOKEN_MARGIN", 512),
        )
        if profile.max_output_tokens + profile.token_margin >= profile.context_tokens:
            raise ConfigError(f"Output budget exceeds context for {name}")
        profiles[name] = profile
    default = get("DEFAULT_PROFILE", names[0]).upper()
    stages = {stage: get(f"PROFILE_{stage}", default).upper() for stage in STAGES}
    vision = get("PROFILE_P0V", "").upper()
    if vision:
        stages["P0V"] = vision
    if not set(stages.values()) <= profiles.keys():
        raise ConfigError("Stage bindings reference an undefined profile")
    if vision and "image" not in profiles[vision].input_modalities:
        raise ConfigError("P0V requires a profile with image modality")
    train = number("TRAIN_RATIO", 0.8, 0, False)
    dev = number("DEV_RATIO", 0.1, 0, False)
    if train + dev > 1:
        raise ConfigError("TRAIN_RATIO + DEV_RATIO must be <= 1")
    try:
        stage_caps = json.loads(get("STAGE_MAX_OUTPUT_TOKENS", "{}"))
    except (ValueError, TypeError):
        raise ConfigError("RR_STAGE_MAX_OUTPUT_TOKENS must be a JSON object") from None
    if not isinstance(stage_caps, dict) or any(k not in set(STAGES) | {"P0V"} or not isinstance(v, int) or v < 128 for k, v in stage_caps.items()):
        raise ConfigError("RR_STAGE_MAX_OUTPUT_TOKENS must map stage names to integers >= 128")
    langfuse_host = get("LANGFUSE_HOST", "").rstrip("/") or None
    langfuse_public, langfuse_secret = get("LANGFUSE_PUBLIC_KEY", ""), get("LANGFUSE_SECRET_KEY", "")
    if langfuse_host:
        parts = urlsplit(langfuse_host)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise ConfigError("RR_LANGFUSE_HOST must be an HTTP root URL without credentials/query/fragment")
        if not langfuse_public.strip() or not langfuse_secret.strip():
            raise ConfigError("RR_LANGFUSE_PUBLIC_KEY and RR_LANGFUSE_SECRET_KEY are required when RR_LANGFUSE_HOST is set")
    return Config(
        path, resolve("JUDGMENTS_PATH"), resolve("TVPL_PATH"), resolve("PHAPDIEN_PATH"),
        resolve("OUTPUT_DIR"), resolve("PROMPTS_FILE"), profiles, stages,
        number("LAW_MAX_RECORDS", 0), number("CASE_MAX_RECORDS", 0), number("ANNOTATION_LIMIT", 50),
        array("CATEGORIES"), array("CASE_IDS"), number("SEED", 20261115),
        number("MAX_ISSUES", 3, 1),
        boolean(get("FAIL_FAST", "false"), "FAIL_FAST"), train, dev,
        resolve("FAMILY_MAP", optional=True), resolve("FEWSHOT_FILE", optional=True),
        resolve("VISION_MANIFEST", optional=True),
        array("OFFICIAL_DOMAINS", '["vbpl.vn","vbpl.moj.gov.vn","vanban.chinhphu.vn","congbao.chinhphu.vn","phapdien.moj.gov.vn","datafiles.chinhphu.vn"]'),
        stage_caps,
        langfuse_host, langfuse_public, langfuse_secret,
        boolean(get("LANGFUSE_CAPTURE_IO", "false"), "LANGFUSE_CAPTURE_IO"),
    )
