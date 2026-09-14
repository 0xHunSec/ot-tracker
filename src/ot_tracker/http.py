from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping


class FetchError(RuntimeError):
    pass


def strip_xssi_prefix(data: bytes) -> bytes:
    stripped = data.lstrip()
    if stripped.startswith(b")]}\'"):
        newline = stripped.find(b"\n")
        if newline < 0:
            return b""
        return stripped[newline + 1 :]
    return data


@dataclass(frozen=True)
class HttpClient:
    timeout_seconds: float = 30.0
    retries: int = 3
    user_agent: str = "chrome-ot-tracker/0.1"

    def get_bytes(
        self,
        url: str,
        params: Mapping[str, str | int] | None = None,
    ) -> bytes:
        if params:
            query = urllib.parse.urlencode(params)
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}{query}"

        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json, text/plain, */*",
                "User-Agent": self.user_agent,
            },
        )
        last_error: BaseException | None = None
        attempts = max(1, self.retries)
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(
                    request, timeout=self.timeout_seconds
                ) as response:
                    return response.read()
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    delay = min(8.0, (2**attempt) + random.random() * 0.25)
                    time.sleep(delay)
        raise FetchError(f"GET failed after {attempts} attempts: {url}: {last_error}")

    def get_json(
        self,
        url: str,
        params: Mapping[str, str | int] | None = None,
        *,
        xssi: bool = True,
    ) -> Any:
        data = self.get_bytes(url, params=params)
        if xssi:
            data = strip_xssi_prefix(data)
        try:
            return json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise FetchError(f"invalid JSON from {url}: {exc}") from exc
