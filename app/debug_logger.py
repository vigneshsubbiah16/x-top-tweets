import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

# Debug logs directory
DEBUG_DIR = Path(__file__).parent.parent / "debug_logs"
DEBUG_DIR.mkdir(exist_ok=True)


def log_api_response(
    endpoint: str,
    username: str,
    response_data: Any,
    response_meta: Any = None,
    params: dict = None,
    error: str = None
):
    """
    Log raw API response to a JSON file for debugging.

    Files are saved as: debug_logs/{username}_{endpoint}_{timestamp}.json
    """
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename = f"{username}_{endpoint}_{timestamp}.json"
    filepath = DEBUG_DIR / filename

    log_entry = {
        "timestamp": datetime.utcnow().isoformat(),
        "endpoint": endpoint,
        "username": username,
        "params": params,
        "error": error,
        "response": {
            "data": None,
            "meta": response_meta
        }
    }

    # Convert response data to serializable format
    if response_data is not None:
        if hasattr(response_data, '__iter__'):
            log_entry["response"]["data"] = [
                serialize_tweet(item) for item in response_data
            ]
        else:
            log_entry["response"]["data"] = serialize_tweet(response_data)

    with open(filepath, 'w') as f:
        json.dump(log_entry, f, indent=2, default=str)

    print(f"[DEBUG] Logged API response to: {filepath}")
    return filepath


def serialize_tweet(tweet) -> dict:
    """Convert a tweet object to a serializable dict."""
    if tweet is None:
        return None

    if isinstance(tweet, dict):
        return tweet

    # Handle tweepy Tweet object
    result = {
        "id": str(tweet.id) if hasattr(tweet, 'id') else None,
        "text": tweet.text if hasattr(tweet, 'text') else None,
        "created_at": tweet.created_at.isoformat() if hasattr(tweet, 'created_at') and tweet.created_at else None,
    }

    if hasattr(tweet, 'public_metrics') and tweet.public_metrics:
        result["public_metrics"] = tweet.public_metrics

    # Add any other available fields
    for field in ['author_id', 'conversation_id', 'in_reply_to_user_id', 'lang']:
        if hasattr(tweet, field):
            result[field] = getattr(tweet, field)

    return result


def serialize_user(user) -> dict:
    """Convert a user object to a serializable dict."""
    if user is None:
        return None

    if isinstance(user, dict):
        return user

    result = {
        "id": str(user.id) if hasattr(user, 'id') else None,
        "username": user.username if hasattr(user, 'username') else None,
        "name": user.name if hasattr(user, 'name') else None,
    }

    if hasattr(user, 'public_metrics') and user.public_metrics:
        result["public_metrics"] = user.public_metrics

    for field in ['profile_image_url', 'description', 'created_at', 'verified']:
        if hasattr(user, field):
            val = getattr(user, field)
            result[field] = val.isoformat() if hasattr(val, 'isoformat') else val

    return result


def log_user_response(username: str, user_data: Any, error: str = None):
    """Log user lookup response."""
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    filename = f"{username}_user_lookup_{timestamp}.json"
    filepath = DEBUG_DIR / filename

    log_entry = {
        "timestamp": datetime.utcnow().isoformat(),
        "endpoint": "get_user",
        "username": username,
        "error": error,
        "response": serialize_user(user_data)
    }

    with open(filepath, 'w') as f:
        json.dump(log_entry, f, indent=2, default=str)

    print(f"[DEBUG] Logged user lookup to: {filepath}")
    return filepath


def list_debug_logs() -> list[dict]:
    """List all debug log files."""
    logs = []
    for log_file in DEBUG_DIR.glob("*.json"):
        logs.append({
            "filename": log_file.name,
            "size_bytes": log_file.stat().st_size,
            "created": datetime.fromtimestamp(log_file.stat().st_mtime).isoformat()
        })
    return sorted(logs, key=lambda x: x["created"], reverse=True)


def get_debug_log(filename: str) -> dict:
    """Read a specific debug log file."""
    filepath = DEBUG_DIR / filename
    if not filepath.exists():
        return None
    with open(filepath, 'r') as f:
        return json.load(f)


def clear_debug_logs() -> int:
    """Clear all debug logs. Returns count of files deleted."""
    count = 0
    for log_file in DEBUG_DIR.glob("*.json"):
        log_file.unlink()
        count += 1
    return count
