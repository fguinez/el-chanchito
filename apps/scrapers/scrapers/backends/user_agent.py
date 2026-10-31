"""A desktop Chrome user agent for the engine and host actually running.

Headless Chromium advertises "HeadlessChrome"; the Santander and Banco
Falabella scrapers replace its UA with the one a headed Chrome on the same
host would send: same major version, same OS token. The OS token is Chrome's
own frozen "unified platform" (UA reduction, `GetUnifiedPlatform` in
Chromium's components/embedder_support/user_agent_utils.cc), which does not
vary with the CPU: a real Chrome on Apple Silicon says "Intel Mac OS X", and
one on a Raspberry Pi (aarch64) says "Linux x86_64". Claiming the real
architecture would contradict what every genuine Chrome on that host sends.
"""

_MAC = "Macintosh; Intel Mac OS X 10_15_7"
_WINDOWS = "Windows NT 10.0; Win64; x64"
_LINUX = "X11; Linux x86_64"

_OS_TOKENS = {
    "darwin": _MAC,
    "win32": _WINDOWS,
    "cygwin": _WINDOWS,
    "linux": _LINUX,
}


def os_token(platform: str) -> str:
    """Chrome's UA platform token for a `sys.platform` value (Linux by default)."""
    return _OS_TOKENS.get(platform, _LINUX)


def user_agent_for(browser_version: str, platform: str) -> str:
    """A desktop Chrome UA for the engine actually running, minus "Headless"."""
    major = browser_version.split(".")[0]
    return (
        f"Mozilla/5.0 ({os_token(platform)}) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
    )
