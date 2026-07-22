from __future__ import annotations

import hashlib
import json
import re
import sys
from urllib import request, error


class LocalOllamaEmbeddings:
    def __init__(self, settings, task_type: str = "semantic_similarity"):
        self.settings = settings
        self.task_type = task_type
        self.base_url = getattr(settings, "ollama_base_url", "http://localhost:11434")

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        try:
            payload = {
                "model": self.settings.embedding_model,
                "input": texts,
            }
            response = self._post("/api/embed", payload)
            embeddings = response.get("embeddings", [])
            if embeddings:
                return embeddings
        except Exception:
            pass
        return [self._fallback_embedding(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        try:
            payload = {
                "model": self.settings.embedding_model,
                "input": [text],
            }
            response = self._post("/api/embed", payload)
            embeddings = response.get("embeddings", [])
            if embeddings:
                return embeddings[0]
        except Exception:
            pass
        return self._fallback_embedding(text)

    def _fallback_embedding(self, text: str, dims: int = 384) -> list[float]:
        normalized = text.lower().strip()
        if not normalized:
            return [0.0] * dims
        vector = [0.0] * dims
        tokens = [t for t in re.findall(r"[a-z0-9]+", normalized) if t]
        if not tokens:
            return [0.0] * dims
        for idx, token in enumerate(tokens):
            digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
            value = int(digest[:8], 16) / 0xFFFFFFFF
            vector[idx % dims] += (value * 2.0) - 1.0
        norm = sum(v * v for v in vector) ** 0.5
        if norm > 0:
            vector = [v / norm for v in vector]
        return vector

    def _post(self, path: str, payload: dict) -> dict:
        data = json.dumps(payload).encode("utf-8")
        req = request.Request(self.base_url + path, data=data, headers={"Content-Type": "application/json"})
        try:
            with request.urlopen(req, timeout=180) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except error.HTTPError as exc:
            raise RuntimeError(f"Ollama request failed: {exc.read().decode('utf-8')}") from exc


class LocalOllamaChatStream:
    def __init__(self, base_url: str, payload: dict):
        self.base_url = base_url
        self.payload = payload

    def __iter__(self):
        req = request.Request(
            self.base_url + "/api/chat",
            data=json.dumps(self.payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with request.urlopen(req, timeout=180) as resp:
                for line in resp.read().decode("utf-8").splitlines():
                    if not line.strip():
                        continue
                    chunk = json.loads(line)
                    content = chunk.get("message", {}).get("content", "")
                    if content:
                        yield type("Chunk", (), {"content": content})()
        except error.HTTPError as exc:
            raise RuntimeError(f"Ollama chat request failed: {exc.read().decode('utf-8')}") from exc

    async def __aiter__(self):
        for chunk in self.__iter__():
            yield chunk


class LocalOllamaChat:
    def __init__(self, settings, temperature: float | None = None, max_tokens: int | None = None):
        self.settings = settings
        self.base_url = getattr(settings, "ollama_base_url", "http://localhost:11434")
        self.temperature = settings.llm_temperature if temperature is None else temperature
        self.max_tokens = settings.llm_max_output_tokens if max_tokens is None else max_tokens

    def astream(self, messages):
        payload = {
            "model": self.settings.llm_model,
            "messages": [self._message_to_dict(message) for message in messages],
            "stream": True,
            "options": {
                "temperature": self.temperature,
                "num_predict": self.max_tokens,
            },
        }
        return LocalOllamaChatStream(self.base_url, payload)

    async def ainvoke(self, prompt: str):
        payload = {
            "model": self.settings.llm_model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "options": {
                "temperature": self.temperature,
                "num_predict": self.max_tokens,
            },
        }
        req = request.Request(
            self.base_url + "/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with request.urlopen(req, timeout=180) as resp:
                response = json.loads(resp.read().decode("utf-8"))
                content = response.get("message", {}).get("content", "")
                return type("Response", (), {"content": content})()
        except error.HTTPError as exc:
            raise RuntimeError(f"Ollama chat request failed: {exc.read().decode('utf-8')}") from exc

    @staticmethod
    def _message_to_dict(message):
        role = getattr(message, "type", None) or getattr(message, "role", "user")
        if role == "human":
            role = "user"
        elif role == "ai":
            role = "assistant"
        elif role == "system":
            role = "system"
        return {"role": role, "content": str(getattr(message, "content", message))}


def build_embeddings_client(settings, task_type: str = "semantic_similarity"):
    return LocalOllamaEmbeddings(settings, task_type=task_type)


def build_llm_client(settings, temperature: float | None = None, max_tokens: int | None = None):
    provider = str(getattr(settings, "llm_provider", "") or "").lower()
    if provider in {"ollama", "local"} or getattr(settings, "ollama_base_url", None):
        return LocalOllamaChat(settings, temperature=temperature, max_tokens=max_tokens)

    return LocalOllamaChat(settings, temperature=temperature, max_tokens=max_tokens)
