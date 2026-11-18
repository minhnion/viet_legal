"""OpenAI-compatible Chat Completions with explicit capabilities and safe logs."""

from pathlib import Path
from queue import Empty, Queue
from threading import BoundedSemaphore, Lock, Thread
import atexit
import base64
import hashlib
import json
import logging
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
        super().__init__("context_budget", "Prompt tokens plus reserved output exceed the context window; no truncation performed")


LOG = logging.getLogger(__name__)


def _attr(key, value):
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    return {"key": key, "value": {"stringValue": value if isinstance(value, str) else canonical(value)}}


class LangfuseTracer:
    """Best-effort OTLP/HTTP export to Langfuse; tracing failures never affect annotation.

    One trace per case (``trace_key``), one generation per model call, session = run hash.
    Prompt and response bodies are only exported when ``capture_io`` is enabled.
    """

    def __init__(self, host, public_key, secret_key, capture_io=False, batch_size=50, interval=2.0, timeout=10.0):
        self.endpoint = host.rstrip("/") + "/api/public/otel/v1/traces"
        self.auth = (public_key, secret_key)
        self.capture_io = capture_io
        self.batch_size, self.interval, self.timeout = batch_size, interval, timeout
        self.queue = Queue()
        self.dropped = 0
        self.worker = Thread(target=self._run, name="langfuse-exporter", daemon=True)
        self.worker.start()
        atexit.register(self.flush)

    @staticmethod
    def _hex(text, size):
        return hashlib.sha256(text.encode()).hexdigest()[:size]

    def record(self, *, stage, model, trace, started_ns, ended_ns, request_hash, attempt=None, usage=None,
               error_code=None, cache_hit=False, messages=None, output=None):
        trace = trace or {}
        trace_key = trace.get("trace_key") or request_hash
        attrs = [
            _attr("langfuse.observation.type", "generation"),
            _attr("gen_ai.request.model", model),
            _attr("langfuse.trace.name", trace.get("trace_name") or f"case {trace_key}"),
            _attr("langfuse.observation.metadata.stage", stage),
            _attr("langfuse.observation.metadata.request_hash", request_hash),
            _attr("langfuse.observation.metadata.cache_hit", cache_hit),
        ]
        if trace.get("session_id"):
            attrs.append(_attr("session.id", trace["session_id"]))
        for key in ("item_id", "case_id"):
            if trace.get(key):
                attrs.append(_attr(f"langfuse.observation.metadata.{key}", trace[key]))
        if attempt is not None:
            attrs.append(_attr("langfuse.observation.metadata.attempt", attempt))
        if usage:
            for name, key in (("prompt_tokens", "gen_ai.usage.input_tokens"), ("completion_tokens", "gen_ai.usage.output_tokens")):
                if isinstance(usage.get(name), int):
                    attrs.append(_attr(key, usage[name]))
        if error_code:
            attrs += [_attr("langfuse.observation.level", "ERROR"), _attr("langfuse.observation.status_message", error_code)]
        if self.capture_io and messages is not None:
            attrs.append(_attr("langfuse.observation.input", canonical(messages)))
        if self.capture_io and output is not None:
            attrs.append(_attr("langfuse.observation.output", canonical(output)))
        self.queue.put({"traceId": self._hex(trace_key, 32), "spanId": uuid.uuid4().hex[:16], "name": stage, "kind": 1,
                        "startTimeUnixNano": str(started_ns), "endTimeUnixNano": str(max(ended_ns, started_ns)),
                        "attributes": attrs, "status": {"code": 2 if error_code else 1}})

    def _send(self, spans):
        body = {"resourceSpans": [{"resource": {"attributes": [_attr("service.name", "viet_legal-rule-recall")]},
                                   "scopeSpans": [{"scope": {"name": "legal_recall"}, "spans": spans}]}]}
        try:
            response = requests.post(self.endpoint, auth=self.auth, json=body, timeout=self.timeout)
            if response.status_code >= 400:
                raise requests.RequestException(f"HTTP {response.status_code}")
        except requests.RequestException as error:
            self.dropped += len(spans)
            LOG.warning("Langfuse export failed (%s); %s spans dropped so far", type(error).__name__, self.dropped)

    def _drain(self, limit):
        spans = []
        while len(spans) < limit:
            try:
                spans.append(self.queue.get_nowait())
            except Empty:
                break
        return spans

    def _run(self):
        while True:
            time.sleep(self.interval)
            while spans := self._drain(self.batch_size):
                self._send(spans)

    def flush(self):
        while spans := self._drain(self.batch_size):
            self._send(spans)


class ChatClient:
    def __init__(self, profile: Profile, cache_dir: Path, log_file: Path, tracer: LangfuseTracer | None = None):
        self.profile = profile
        self.cache_dir = cache_dir
        self.log_file = log_file
        self.tracer = tracer
        cache_dir.mkdir(parents=True, exist_ok=True)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        self.semaphore = BoundedSemaphore(profile.concurrency)
        self.log_lock = Lock()
        self.token_cache = {}
        self.token_lock = Lock()

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

    def _headers(self):
        headers = {"Content-Type": "application/json"}
        if self.profile.auth_mode == "bearer":
            headers["Authorization"] = "Bearer " + self.profile.api_key
        return headers

    def _tokenize_url(self):
        root = self.profile.base_url.rstrip("/")
        return (root[:-3] if root.endswith("/v1") else root) + "/tokenize"

    def count_tokens(self, messages):
        """Exact prompt token count from the serving endpoint (vLLM ``/tokenize``), chat template applied."""
        key = digest(messages)
        with self.token_lock:
            if key in self.token_cache:
                return self.token_cache[key]
        p = self.profile
        body = {"model": p.model, "messages": messages, "add_generation_prompt": True}
        if isinstance(p.extra_body.get("chat_template_kwargs"), dict):
            body["chat_template_kwargs"] = p.extra_body["chat_template_kwargs"]
        error = None
        for attempt in range(p.retries + 1):
            try:
                response = requests.post(self._tokenize_url(), headers=self._headers(), json=body, timeout=p.timeout)
                if response.status_code >= 400:
                    error = ModelError("tokenize", f"Tokenize endpoint returned HTTP {response.status_code}; response body omitted")
                    if response.status_code not in {408, 409, 425, 429} and response.status_code < 500:
                        break
                else:
                    count = response.json()["count"]
                    if not isinstance(count, int) or count < 0:
                        raise TypeError
                    with self.token_lock:
                        if len(self.token_cache) >= 4096:
                            self.token_cache.clear()
                        self.token_cache[key] = count
                    return count
            except requests.RequestException:
                error = ModelError("tokenize", "Tokenize endpoint unreachable; request headers and URL details omitted")
            except (ValueError, KeyError, TypeError):
                error = ModelError("tokenize", "Tokenize endpoint returned an invalid token count")
                break
            if attempt < p.retries:
                time.sleep(min(p.retry_backoff * 2 ** attempt, 30))
        raise error

    def _save_invalid(self, stage, key, attempt, code, content):
        """Keep rejected model text (truncated/invalid JSON) for debugging; never used as a label."""
        try:
            folder = self.log_file.parent / "invalid_outputs"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"{key}.{attempt}.{code}.txt").write_text(f"stage={stage}\n\n" + self._redact(content), encoding="utf-8")
        except OSError:
            LOG.warning("Could not save invalid output for stage %s", stage)

    def fits(self, messages, output_schema, image=False, max_output_tokens=None):
        # Prompt tokens are counted exactly by the endpoint; output and margin are reserved on top.
        # With json_schema output mode the schema constrains decoding and is not part of the prompt;
        # any schema text embedded in the messages is counted with them.
        # Image accounting is provider-specific: images are excluded and covered by the token margin.
        if image:
            messages = [{**m, "content": [p for p in m["content"] if p.get("type") == "text"]}
                        if isinstance(m.get("content"), list) else m for m in messages]
        reserved = max_output_tokens or self.profile.max_output_tokens
        return self.count_tokens(messages) + reserved + self.profile.token_margin <= self.profile.context_tokens
        # Previous estimate (disabled): character cap plus UTF-8 bytes as a conservative token upper bound.
        # It over-counted Vietnamese about 4-5x (~1.28 bytes vs ~0.3 tokens per character) and rejected
        # requests that fit the context window.
        # text = canonical(messages)
        # return (len(text) <= self.profile.max_input_chars
        #         and len(text.encode()) + len(canonical(output_schema).encode())
        #         + self.profile.max_output_tokens + self.profile.token_margin <= self.profile.context_tokens)

    def _trace(self, **event):
        if self.tracer:
            try:
                self.tracer.record(model=self.profile.model, **event)
            except Exception:  # Tracing is diagnostic only.
                LOG.warning("Langfuse span could not be built for stage %s", event.get("stage"))

    def complete(self, stage, messages, output_schema, context, image=False, semantic_validator=None, trace=None, max_output_tokens=None):
        if image and "image" not in self.profile.input_modalities:
            raise ModelError("unsupported_modality", "Image input is not enabled for this profile")
        if not self.fits(messages, output_schema, image, max_output_tokens):
            raise BudgetError()
        p = self.profile
        body = {"model": p.model, "messages": messages, "stream": False,
                p.token_parameter: max_output_tokens or p.max_output_tokens, **p.extra_body}
        if p.temperature is not None:
            body["temperature"] = p.temperature
        if p.output_mode == "json_schema":
            body["response_format"] = {"type": "json_schema", "json_schema": {
                "name": "legal_" + stage.lower(), "strict": True, "schema": output_schema}}
        elif p.output_mode == "json_object":
            body["response_format"] = {"type": "json_object"}
        # Cache identity is the exact request plus schema: same prompt, model, sampling and output cap reuse the
        # same response across runs. Run/config bookkeeping (run_hash, item_id, timeouts) deliberately stays out.
        key = digest({"request": body, "schema": output_schema})
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
            now = time.time_ns()
            self._trace(stage=stage, trace=trace, started_ns=now, ended_ns=now, request_hash=key, cache_hit=True,
                        messages=self._redact(messages), output=record["output"])
            return record["output"]
        headers = self._headers()
        validator = Draft202012Validator(output_schema)
        last_error = None
        with self.semaphore:
            for attempt in range(p.retries + 1):
                started = time.monotonic()
                started_ns = time.time_ns()
                usage = None
                content = None
                try:
                    response = requests.post(p.base_url + "/chat/completions", headers=headers, json=body, timeout=p.timeout)
                    status = response.status_code
                    if status >= 400:
                        retryable = status in {408, 409, 425, 429} or status >= 500
                        raise ModelError("http_retryable" if retryable else "http_permanent", f"Chat endpoint returned HTTP {status}; response body omitted")
                    data = response.json()
                    usage = data.get("usage") if isinstance(data, dict) else None
                    choice = data["choices"][0]
                    message = choice["message"]
                    if not isinstance(choice, dict) or not isinstance(message, dict):
                        raise ModelError("protocol", "Invalid choice/message structure")
                    if message.get("refusal"):
                        raise ModelError("refusal", "Model refused the annotation request")
                    content = message.get("content")  # captured before the finish check so truncated text can be saved
                    if choice.get("finish_reason") != "stop":
                        raise ModelError("incomplete", "Model output was incomplete or not a normal text completion")
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
                    self._trace(stage=stage, trace=trace, started_ns=started_ns, ended_ns=time.time_ns(), request_hash=key,
                                attempt=attempt, usage=usage, messages=self._redact(messages), output=safe["output"])
                    return self._redact(output)
                except requests.exceptions.JSONDecodeError:
                    last_error = ModelError("protocol", "Endpoint returned invalid JSON")
                except requests.RequestException:
                    last_error = ModelError("transport", "Connection or timeout error; request headers and URL details omitted")
                except (ValueError, KeyError, IndexError, TypeError):
                    last_error = ModelError("protocol", "Endpoint returned invalid JSON or Chat Completions structure")
                except ModelError as error:
                    last_error = error
                if isinstance(content, str) and last_error.code in {"incomplete", "schema", "semantic_validation", "protocol"}:
                    self._save_invalid(stage, key, attempt, last_error.code, content)
                self._log({"event": "model_error", "stage": stage, "profile": p.name,
                           "request_hash": key, "attempt": attempt, "error_code": last_error.code})
                self._trace(stage=stage, trace=trace, started_ns=started_ns, ended_ns=time.time_ns(), request_hash=key,
                            attempt=attempt, usage=usage, error_code=last_error.code, messages=self._redact(messages))
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
