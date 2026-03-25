import asyncio
import json
import os
import sys

try:
    import browser_cookie3
except Exception:  # pragma: no cover - optional recovery path
    browser_cookie3 = None

from twikit import Client


def _chrome_cookie_dict() -> dict:
    if browser_cookie3 is None:
        raise RuntimeError("browser-cookie3 is unavailable in the twikit backend runtime")
    cookie_map = {}
    for domain in ("x.com", ".x.com", "twitter.com", ".twitter.com"):
        try:
            for cookie in browser_cookie3.chrome(domain_name=domain):
                cookie_map[cookie.name] = cookie.value
        except Exception:
            continue
    return cookie_map


def _tweet_payload(tweet) -> dict:
    user = getattr(tweet, "user", None)
    return {
        "id": getattr(tweet, "id", None),
        "url": f"https://x.com/{getattr(user, 'screen_name', '')}/status/{getattr(tweet, 'id', '')}"
        if getattr(tweet, "id", None) and getattr(user, "screen_name", None)
        else None,
        "text": getattr(tweet, "full_text", None) or getattr(tweet, "text", None),
        "user": _user_payload(user) if user is not None else None,
        "reply_to": getattr(tweet, "reply_to", None),
    }


def _user_payload(user) -> dict:
    if user is None:
        return {}
    return {
        "id": getattr(user, "id", None),
        "name": getattr(user, "name", None),
        "screen_name": getattr(user, "screen_name", None),
        "followers_count": getattr(user, "followers_count", None),
        "following_count": getattr(user, "following_count", None),
        "description": getattr(user, "description", None),
    }


async def _client_from_login(request: dict) -> Client:
    username = (request.get("account_username") or request.get("username") or "").strip()
    email = (request.get("email") or "").strip()
    password = (request.get("password") or "").strip()
    cookies_file = request.get("cookies_file")
    if not password or not (username or email):
        raise RuntimeError(
            "twikit_direct requires saved X credentials. Username or email plus password must be available."
        )
    client = Client("en-US")
    try:
        await client.login(
            auth_info_1=username or email,
            auth_info_2=email if username and email else None,
            password=password,
            cookies_file=cookies_file,
        )
        if cookies_file:
            os.makedirs(os.path.dirname(cookies_file), exist_ok=True)
            client.save_cookies(cookies_file)
        return client
    except Exception as error:
        raise RuntimeError(f"twikit_direct login failed: {error}") from error


async def _client_from_cookies() -> Client:
    try:
        cookies = _chrome_cookie_dict()
    except Exception as error:
        raise RuntimeError(f"Chrome X cookie import failed: {error}") from error
    if "ct0" not in cookies or "auth_token" not in cookies:
        raise RuntimeError(
            "Chrome X cookies are unavailable. Keep Google Chrome signed into X so twitter_x can import a live session."
        )
    client = Client("en-US")
    client.set_cookies(cookies, clear_cookies=True)
    return client


async def _client_from_exported_cookies(request: dict) -> Client:
    cookies = request.get("cookies") or []
    if not cookies:
        raise RuntimeError("No exported X cookies were provided")
    cookie_map = {}
    for cookie in cookies:
        name = cookie.get("name")
        value = cookie.get("value")
        if name and value:
            cookie_map[name] = value
    if "ct0" not in cookie_map or "auth_token" not in cookie_map:
        raise RuntimeError("Exported X cookies did not include auth_token and ct0")
    client = Client("en-US")
    client.set_cookies(cookie_map, clear_cookies=True)
    return client


async def _client(request: dict) -> Client:
    exported_cookie_error = None
    if request.get("cookies"):
        try:
            return await _client_from_exported_cookies(request)
        except Exception as error:
            exported_cookie_error = str(error)
    login_error = None
    if any((request.get("username"), request.get("email"), request.get("password"))):
        try:
            return await _client_from_login(request)
        except Exception as error:
            login_error = str(error)
    try:
        return await _client_from_cookies()
    except Exception as cookie_error:
        errors = [item for item in (exported_cookie_error, login_error, str(cookie_error)) if item]
        if len(errors) > 1:
            raise RuntimeError("\n".join(errors))
        if login_error:
            raise RuntimeError(f"{login_error}\nChrome import fallback failed: {cookie_error}")
        raise


async def _run(request: dict) -> dict:
    action = request["action"]
    client = await _client(request)

    if action == "status":
        user = await client.user()
        return {
            "ok": True,
            "output": "ready",
            "data": {
                "status": "ready",
                "backend": "twikit_direct",
                "account": _user_payload(user),
                "supported_capabilities": {
                    "post": True,
                    "comment": True,
                    "article": False,
                },
            },
        }

    if action == "my_profile":
        user = await client.user()
        return {"ok": True, "output": json.dumps(_user_payload(user)), "data": _user_payload(user)}

    if action == "profile_by_username":
        user = await client.get_user_by_screen_name(request["username"])
        payload = _user_payload(user)
        return {"ok": True, "output": json.dumps(payload), "data": payload}

    if action == "get_tweet":
        tweet = await client.get_tweet_by_id(request["tweet_id"])
        payload = _tweet_payload(tweet)
        return {"ok": True, "output": json.dumps(payload), "data": payload}

    if action == "send_tweet":
        tweet = await client.create_tweet(
            text=request["text"],
            reply_to=request.get("in_reply_to_id"),
        )
        payload = _tweet_payload(tweet)
        return {"ok": True, "output": json.dumps(payload), "data": payload}

    if action == "search_tweets":
        product = {
            "top": "Top",
            "latest": "Latest",
            "photos": "Media",
            "videos": "Media",
            None: "Top",
        }.get(request.get("search_mode"), "Top")
        results = await client.search_tweet(
            request["query"],
            product,
            count=min(int(request.get("count") or 10), 20),
        )
        payload = [_tweet_payload(tweet) for tweet in list(results)]
        return {"ok": True, "output": json.dumps(payload), "data": payload}

    if action == "get_user_tweets":
        user = await client.get_user_by_screen_name(request["username"])
        results = await client.get_user_tweets(
            user.id,
            "Tweets",
            count=min(int(request.get("count") or 10), 40),
        )
        payload = [_tweet_payload(tweet) for tweet in list(results)]
        return {"ok": True, "output": json.dumps(payload), "data": payload}

    if action == "search_profiles":
        raise RuntimeError("twikit_direct backend does not implement search_profiles yet")

    if action == "get_followers":
        user = await client.get_user_by_screen_name(request["username"])
        results = await client.get_user_followers(
            user.id,
            count=min(int(request.get("count") or 20), 20),
        )
        payload = [_user_payload(item) for item in list(results)]
        return {"ok": True, "output": json.dumps(payload), "data": payload}

    if action == "get_following":
        user = await client.get_user_by_screen_name(request["username"])
        results = await client.get_user_following(
            user.id,
            count=min(int(request.get("count") or 20), 20),
        )
        payload = [_user_payload(item) for item in list(results)]
        return {"ok": True, "output": json.dumps(payload), "data": payload}

    if action == "follow_user":
        user = await client.get_user_by_screen_name(request["username"])
        followed = await client.follow_user(user.id)
        payload = _user_payload(followed)
        return {"ok": True, "output": json.dumps(payload), "data": payload}

    raise RuntimeError(f"Unsupported action for twikit_direct backend: {action}")


def main() -> int:
    request = json.loads(sys.stdin.read())
    try:
        result = asyncio.run(asyncio.wait_for(_run(request), timeout=30))
    except Exception as error:
        result = {"ok": False, "error": str(error)}
    sys.stdout.write(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
