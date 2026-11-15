"""OpenAI-compatible Chat Completions with explicit capabilities and safe logs."""

from pathlib import Path
from threading import BoundedSemaphore, Lock
import base64
import json
import time
import uuid

from jsonschema import Draft202012Validator
import requests

from .config import Profile, canonical, digest


class ModelError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class BudgetError(ModelError):
    def __init__(self):
        super().__init__("context_budget", "Input exceeds configured character or conservative context budget; no truncation performed")


class ChatClient:
    def __init__(self, profile: Profile, cache_dir: Path, log_file: Path):
        self.profile = profile
        self.cache_dir = cache_dir
        self.log_file = log_file
        cache_dir.mkdir(parents=True, exist_ok=True)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        self.semaphore = BoundedSemaphore(profile.concurrency)
        self.log_lock = Lock()

    def _redact(self, value):
        if isinstance(value, str):
            return value.replace(self.profile.api_key, "[REDACTED]") if self.profile.api_key else value
        if isinstance(value, dict):
            return {k: self._redact(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._redact(v) for v in value]
        return value

    def _log(self, event):
        with self.log_lock, self.log_file.open("a", encoding="utf-8") as stream:
            stream.write(canonical(self._redact(event)) + "\n")

    def fits(self, messages, output_schema, image=False):
        # UTF-8 byte count is a deliberately conservative text-token upper estimate.
        # Image accounting is provider-specific: reserve a separately configured context margin.
        text = canonical(messages)
        if image:
            messages = [{**m, "content": [p for p in m["content"] if p.get("type") == "text"]}
                        if isinstance(m.get("content"), list) else m for m in messages]
            text = canonical(messages)
        return (len(text) <= self.profile.max_input_chars
                and len(text.encode()) + len(canonical(output_schema).encode())
                + self.profile.max_output_tokens + self.profile.token_margin <= self.profile.context_tokens)

    def complete(self, stage, messages, output_schema, context, image=False, semantic_validator=None):
        if image and "image" not in self.profile.input_modalities:
            raise ModelError("unsupported_modality", "Image input is not enabled for this profile")
        if not self.fits(messages, output_schema, image):
            raise BudgetError()
        p = self.profile
        body = {"model": p.model, "messages": messages, "stream": False,
                p.token_parameter: p.max_output_tokens, **p.extra_body}
        if p.temperature is not None:
            body["temperature"] = p.temperature
        if p.output_mode == "json_schema":
            body["response_format"] = {"type": "json_schema", "json_schema": {
                "name": "legal_" + stage.lower(), "strict": True, "schema": output_schema}}
        elif p.output_mode == "json_object":
            body["response_format"] = {"type": "json_object"}
        key = digest({"request": body, "profile": p.public(), "context": context, "schema": output_schema})
        cache = self.cache_dir / (key + ".json")
        if cache.exists():
            try:
                record = json.loads(cache.read_text())
                valid = isinstance(record, dict) and "output" in record and not list(Draft202012Validator(output_schema).iter_errors(record["output"]))
            except (ValueError, TypeError):
                valid = False
            if not valid:
                cache.unlink(missing_ok=True)
                raise ModelError("cache_invalid", "Invalid cache entry removed; retry the item explicitly")
            if semantic_validator:
                try:
                    semantic_validator(record["output"])
                except ValueError:
                    cache.unlink(missing_ok=True)
                    raise ModelError("semantic_validation", "Cached output failed stage grounding/ID checks and was invalidated") from None
            self._log({"event": "cache_hit", "stage": stage, "request_hash": key, "profile": p.name})
            return record["output"]
        headers = {"Content-Type": "application/json"}
        if p.auth_mode == "bearer":
            headers["Authorization"] = "Bearer " + p.api_key
        validator = Draft202012Validator(output_schema)
        last_error = None
        with self.semaphore:
            for attempt in range(p.retries + 1):
                started = time.monotonic()
                try:
                    response = requests.post(p.base_url + "/chat/completions", headers=headers, json=body, timeout=p.timeout)
                    status = response.status_code
                    if status >= 400:
                        retryable = status in {408, 409, 425, 429} or status >= 500
                        raise ModelError("http_retryable" if retryable else "http_permanent", f"Chat endpoint returned HTTP {status}; response body omitted")
                    data = response.json()
                    choice = data["choices"][0]
                    message = choice["message"]
                    if not isinstance(choice, dict) or not isinstance(message, dict):
                        raise ModelError("protocol", "Invalid choice/message structure")
                    if message.get("refusal"):
                        raise ModelError("refusal", "Model refused the annotation request")
                    if choice.get("finish_reason") != "stop":
                        raise ModelError("incomplete", "Model output was incomplete or not a normal text completion")
                    content = message.get("content")
                    if not isinstance(content, str):
                        raise ModelError("protocol", "Endpoint did not return string message.content")
                    output = json.loads(content)
                    errors = list(validator.iter_errors(output))
                    if errors:
                        raise ModelError("schema", "Model response failed the stage JSON Schema")
                    if semantic_validator:
                        try:
                            semantic_validator(output)
                        except ValueError:
                            raise ModelError("semantic_validation", "Model output failed stage grounding/ID checks") from None
                    record = {"output": output, "stage": stage, "profile": p.public(), "request_hash": key,
                              "context": context, "usage": data.get("usage"),
                              "elapsed_seconds": round(time.monotonic() - started, 3), "attempt": attempt,
                              "system_fingerprint": data.get("system_fingerprint")}
                    safe = self._redact(record)
                    temp = cache.with_suffix("." + uuid.uuid4().hex + ".tmp")
                    temp.write_text(canonical(safe) + "\n", encoding="utf-8")
                    temp.replace(cache)
                    self._log({k: v for k, v in safe.items() if k != "output"})
                    return self._redact(output)
                except requests.exceptions.JSONDecodeError:
                    last_error = ModelError("protocol", "Endpoint returned invalid JSON")
                except requests.RequestException:
                    last_error = ModelError("transport", "Connection or timeout error; request headers and URL details omitted")
                except (ValueError, KeyError, IndexError, TypeError):
                    last_error = ModelError("protocol", "Endpoint returned invalid JSON or Chat Completions structure")
                except ModelError as error:
                    last_error = error
                self._log({"event": "model_error", "stage": stage, "profile": p.name,
                           "request_hash": key, "attempt": attempt, "error_code": last_error.code})
                if last_error.code in {"http_permanent", "refusal", "incomplete", "semantic_validation"}:
                    break
                if attempt < p.retries:
                    time.sleep(min(p.retry_backoff * 2 ** attempt, 30))
        raise last_error


def image_part(path: Path):
    data = path.read_bytes()
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif data.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        raise ValueError("Vision input must be a PNG, JPEG or WebP image; PDF must be rendered first")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64," + base64.b64encode(data).decode()}}
