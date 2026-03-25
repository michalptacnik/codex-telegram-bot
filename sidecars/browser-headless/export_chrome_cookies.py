import json
import sys

import browser_cookie3


def main() -> int:
    domain = sys.argv[1] if len(sys.argv) > 1 else "x.com"
    cookies = []
    for cookie in browser_cookie3.chrome(domain_name=domain):
        cookies.append(
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path,
                "secure": cookie.secure,
                "expires": cookie.expires,
                "httpOnly": bool(getattr(cookie, "_rest", {}).get("HTTPOnly") == ""),
            }
        )
    json.dump(cookies, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
