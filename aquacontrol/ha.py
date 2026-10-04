"""Tiny Home Assistant REST client (urllib only). Error messages are German and never contain the token."""
from __future__ import annotations

import http.client
import json
import socket
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT_S = 10


class HAError(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib would resend the Authorization header to the redirect target; refuse instead."""

    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


class HAClient:
    def __init__(self, base_url: str, token: str, timeout: float = TIMEOUT_S):
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.timeout = timeout

    def __repr__(self) -> str:  # keep the token out of tracebacks and logs
        return f"HAClient({self.base_url!r})"

    def _request(self, method: str, path: str, body: dict | None = None) -> bytes:
        if not self.base_url.lower().startswith(("http://", "https://")):
            raise HAError("Home Assistant: URL muss mit http:// oder https:// beginnen")
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with _OPENER.open(req, timeout=self.timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            e.close()
            if e.code in (401, 403):
                raise HAError("Home Assistant: Zugriff verweigert (Token ungültig?)") from None
            if e.code == 404:
                raise HAError("Home Assistant: nicht gefunden (Entity oder Service unbekannt?)") from None
            raise HAError(f"Home Assistant: Fehler HTTP {e.code}") from None
        except (TimeoutError, socket.timeout):
            raise HAError("Home Assistant: keine Antwort innerhalb der Zeit") from None
        except urllib.error.URLError as e:
            if isinstance(e.reason, (TimeoutError, socket.timeout)):
                raise HAError("Home Assistant: keine Antwort innerhalb der Zeit") from None
            raise HAError(f"Home Assistant nicht erreichbar: {e.reason}") from None
        except http.client.HTTPException:  # IncompleteRead, BadStatusLine, ...: not OSErrors
            raise HAError("Home Assistant: ungültige oder abgebrochene Antwort") from None
        except ValueError:  # e.g. a header value with a newline: the message would contain the token
            raise HAError("Home Assistant: ungültige Anfrage (URL oder Token fehlerhaft)") from None
        except OSError as e:  # connection reset, ...
            raise HAError(f"Home Assistant nicht erreichbar: {e}") from None

    def get_state(self, entity_id: str) -> dict:
        raw = self._request("GET", "/api/states/" + urllib.parse.quote(entity_id, safe=""))
        try:
            state = json.loads(raw)
        except ValueError:
            raise HAError("Home Assistant: Antwort ist kein gültiges JSON") from None
        if not isinstance(state, dict):
            raise HAError("Home Assistant: unerwartete Antwort")
        return state

    def call(self, domain: str, service: str, data: dict) -> None:
        self._request("POST", f"/api/services/{urllib.parse.quote(domain, safe='')}/"
                              f"{urllib.parse.quote(service, safe='')}", data)
