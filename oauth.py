import json
import logging
import os
from importlib import resources

import requests
from google_auth_oauthlib.flow import InstalledAppFlow

from colab_cli.auth import PUBLIC_SCOPES, REMOTE_REDIRECT_URI

logger = logging.getLogger(__name__)

_OAUTH_STATE_FILE = "oauth_state.json"


def _atomic_write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)


def _client_config():
    try:
        config_resource = resources.files("colab_cli").joinpath("oauth_config.json")
        if config_resource.is_file():
            return json.loads(config_resource.read_text())
    except Exception as e:
        logger.debug(f"Failed to load inlined oauth config: {e}")
    raise FileNotFoundError("colab_cli oauth_config.json not available")


def generate_auth_url(account_home: str) -> str:
    """Create a flow, persist PKCE state, and return the authorization URL."""
    config = _client_config()
    flow = InstalledAppFlow.from_client_config(config, PUBLIC_SCOPES)
    flow.redirect_uri = REMOTE_REDIRECT_URI
    url, _ = flow.authorization_url(prompt="consent", token_usage="remote")
    os.makedirs(account_home, exist_ok=True)
    _atomic_write(
        os.path.join(account_home, _OAUTH_STATE_FILE),
        json.dumps({"client_config": config, "code_verifier": flow.code_verifier}),
    )
    return url


def exchange_code(account_home: str, code: str) -> str:
    """Exchange the authorization code, persist token.json, return account email."""
    state_path = os.path.join(account_home, _OAUTH_STATE_FILE)
    if not os.path.exists(state_path):
        raise RuntimeError("No pending login found. Press ĐĂNG NHẬP first.")

    with open(state_path, encoding="utf-8") as f:
        saved = json.load(f)

    flow = InstalledAppFlow.from_client_config(
        saved["client_config"], PUBLIC_SCOPES
    )
    flow.redirect_uri = REMOTE_REDIRECT_URI
    flow.code_verifier = saved["code_verifier"]
    flow.fetch_token(code=code.strip())

    token_dir = os.path.join(account_home, ".config", "colab-cli")
    os.makedirs(token_dir, exist_ok=True)
    _atomic_write(os.path.join(token_dir, "token.json"), flow.credentials.to_json())
    try:
        os.remove(state_path)
    except OSError:
        pass

    return get_account_email(account_home)


def get_account_email(account_home: str) -> str:
    token_path = os.path.join(account_home, ".config", "colab-cli", "token.json")
    if not os.path.exists(token_path):
        raise RuntimeError("Account not logged in.")
    with open(token_path, encoding="utf-8") as f:
        creds = json.load(f)
    access_token = creds.get("token")
    if not access_token:
        raise RuntimeError("No access token available.")
    resp = requests.get(
        "https://oauth2.googleapis.com/tokeninfo",
        params={"access_token": access_token},
        timeout=20,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Failed to verify token: HTTP {resp.status_code}")
    return resp.json().get("email", "unknown@example.com")


def is_logged_in(account_home: str) -> bool:
    return os.path.exists(
        os.path.join(account_home, ".config", "colab-cli", "token.json")
    )
