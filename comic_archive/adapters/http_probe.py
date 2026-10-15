"""One ordinary browser-free identity GET; no retries or account data export."""
import json
import time

from ..transport import protect_http_logs
from ..models import ArchiveError


def probe_session(cookies, client=None, expected_account_id=None, network_guard=None):
    import httpx
    protect_http_logs()
    if network_guard is not None: network_guard.check()
    jar = httpx.Cookies()
    for cookie in cookies:
        if cookie.get("domain", "").lstrip(".") != "tapas.io":
            continue
        expires = cookie.get("expires", -1)
        if type(expires) in (int, float) and expires >= 0 and expires <= time.time():
            continue
        jar.set(cookie["name"], cookie["value"], domain=cookie["domain"],
                path=cookie.get("path", "/"))
    owned = client is None
    if owned:
        client = httpx.Client(timeout=15, follow_redirects=False, trust_env=False)
    result = {"authenticated": False, "http_status": None,
              "reason": None, "request_count": 1, "reader_or_images_requested": False}
    response = None
    try:
        request = client.build_request("GET", "https://tapas.io/who-am-i", cookies=jar,
                                       headers={"Accept": "application/json"})
        response = client.send(request, stream=True, follow_redirects=False)
        result["http_status"] = response.status_code
        if response.status_code != 200:
            result["reason"] = "http_refused_or_redirected"
            return result
        body = bytearray()
        for chunk in response.iter_bytes():
            if network_guard is not None: network_guard.check()
            body.extend(chunk)
            if len(body) > 2 * 1024 * 1024:
                result["reason"] = "metadata_too_large"
                return result
        try:
            payload = json.loads(body)
            user = payload["data"]["user"]
            valid = (payload.get("error_details") is None and isinstance(user, dict)
                     and type(user.get("id")) is int and user["id"] > 0)
        except (ValueError, KeyError, TypeError, AttributeError):
            result["reason"] = "metadata_unexpected"
            return result
        if valid and expected_account_id is not None and str(user["id"]) != expected_account_id:
            result["reason"] = "account_changed"
            return result
        result.update(authenticated=valid, reason=None if valid else "login_required")
        return result
    except ArchiveError:
        raise
    except Exception:
        result["reason"] = "network_or_client_error"
        return result
    finally:
        if response is not None:
            response.close()
        if owned:
            client.close()
