"""
Rate limit tracker for Twitter API calls.

Stores rate limit state in a JSON file so we can:
1. Show when the API will be available again
2. Avoid making API calls when we know we're rate limited
3. Track last successful API call time per endpoint
4. Track monthly quota status
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import threading


class RateLimitTracker:
    """Track Twitter API rate limit state."""

    TRACKER_FILE = Path(__file__).parent.parent / "rate_limit_state.json"

    def __init__(self):
        self._lock = threading.Lock()
        self._ensure_file_exists()

    def _get_default_state(self) -> dict:
        """Get default state structure."""
        return {
            "is_rate_limited": False,
            "rate_limit_reset_at": None,
            "last_api_call_at": None,
            "last_successful_call_at": None,
            "rate_limited_at": None,
            # Monthly quota tracking
            "monthly_cap_exceeded": False,
            "monthly_cap_exceeded_at": None,
            # Per-endpoint tracking
            "endpoints": {
                "get_user": {
                    "last_call_at": None,
                    "last_success_at": None,
                    "call_count": 0,
                    "success_count": 0,
                    "error_count": 0
                },
                "get_users_tweets": {
                    "last_call_at": None,
                    "last_success_at": None,
                    "call_count": 0,
                    "success_count": 0,
                    "error_count": 0
                },
                "search_tweets": {
                    "last_call_at": None,
                    "last_success_at": None,
                    "call_count": 0,
                    "success_count": 0,
                    "error_count": 0
                }
            },
            # Total counts
            "total_calls": 0,
            "total_successes": 0,
            "total_errors": 0,
            # Monthly reset tracking
            "current_month": datetime.now(timezone.utc).strftime("%Y-%m"),
            "monthly_call_count": 0
        }

    def _ensure_file_exists(self):
        """Create the tracker file if it doesn't exist."""
        if not self.TRACKER_FILE.exists():
            self._save_state(self._get_default_state())

    def _load_state(self) -> dict:
        """Load current state from file."""
        try:
            with open(self.TRACKER_FILE, 'r') as f:
                state = json.load(f)
                # Ensure all keys exist (in case of schema updates)
                default = self._get_default_state()
                for key in default:
                    if key not in state:
                        state[key] = default[key]
                # Check for month rollover
                current_month = datetime.now(timezone.utc).strftime("%Y-%m")
                if state.get("current_month") != current_month:
                    # New month - reset monthly counters
                    state["current_month"] = current_month
                    state["monthly_call_count"] = 0
                    state["monthly_cap_exceeded"] = False
                    state["monthly_cap_exceeded_at"] = None
                    self._save_state(state)
                return state
        except (json.JSONDecodeError, FileNotFoundError):
            return self._get_default_state()

    def _save_state(self, state: dict):
        """Save state to file."""
        with open(self.TRACKER_FILE, 'w') as f:
            json.dump(state, f, indent=2)

    def record_api_call(self, endpoint: str = "get_users_tweets"):
        """Record that an API call was made."""
        with self._lock:
            state = self._load_state()
            now = datetime.now(timezone.utc).isoformat()
            state["last_api_call_at"] = now
            state["total_calls"] = state.get("total_calls", 0) + 1
            state["monthly_call_count"] = state.get("monthly_call_count", 0) + 1

            if endpoint in state.get("endpoints", {}):
                state["endpoints"][endpoint]["last_call_at"] = now
                state["endpoints"][endpoint]["call_count"] = state["endpoints"][endpoint].get("call_count", 0) + 1

            self._save_state(state)

    def record_success(self, endpoint: str = "get_users_tweets"):
        """Record a successful API call."""
        with self._lock:
            state = self._load_state()
            now = datetime.now(timezone.utc).isoformat()
            state["last_api_call_at"] = now
            state["last_successful_call_at"] = now
            state["is_rate_limited"] = False
            state["rate_limit_reset_at"] = None
            state["rate_limited_at"] = None
            state["total_calls"] = state.get("total_calls", 0) + 1
            state["total_successes"] = state.get("total_successes", 0) + 1
            state["monthly_call_count"] = state.get("monthly_call_count", 0) + 1

            if endpoint in state.get("endpoints", {}):
                state["endpoints"][endpoint]["last_call_at"] = now
                state["endpoints"][endpoint]["last_success_at"] = now
                state["endpoints"][endpoint]["call_count"] = state["endpoints"][endpoint].get("call_count", 0) + 1
                state["endpoints"][endpoint]["success_count"] = state["endpoints"][endpoint].get("success_count", 0) + 1

            self._save_state(state)

    def record_rate_limit(self, reset_seconds: int = 900, endpoint: str = "get_users_tweets"):
        """
        Record that we hit a rate limit.

        Args:
            reset_seconds: Seconds until rate limit resets (default 15 min)
            endpoint: Which endpoint was rate limited
        """
        with self._lock:
            state = self._load_state()
            now = datetime.now(timezone.utc)
            reset_at = now.timestamp() + reset_seconds

            state["is_rate_limited"] = True
            state["rate_limited_at"] = now.isoformat()
            state["rate_limit_reset_at"] = datetime.fromtimestamp(reset_at, tz=timezone.utc).isoformat()
            state["last_api_call_at"] = now.isoformat()
            state["total_calls"] = state.get("total_calls", 0) + 1
            state["total_errors"] = state.get("total_errors", 0) + 1
            state["monthly_call_count"] = state.get("monthly_call_count", 0) + 1

            if endpoint in state.get("endpoints", {}):
                state["endpoints"][endpoint]["last_call_at"] = now.isoformat()
                state["endpoints"][endpoint]["call_count"] = state["endpoints"][endpoint].get("call_count", 0) + 1
                state["endpoints"][endpoint]["error_count"] = state["endpoints"][endpoint].get("error_count", 0) + 1

            self._save_state(state)

    def record_monthly_cap_exceeded(self, endpoint: str = "get_users_tweets"):
        """Record that monthly quota has been exceeded."""
        with self._lock:
            state = self._load_state()
            now = datetime.now(timezone.utc).isoformat()

            state["monthly_cap_exceeded"] = True
            state["monthly_cap_exceeded_at"] = now
            state["last_api_call_at"] = now
            state["total_calls"] = state.get("total_calls", 0) + 1
            state["total_errors"] = state.get("total_errors", 0) + 1
            state["monthly_call_count"] = state.get("monthly_call_count", 0) + 1

            if endpoint in state.get("endpoints", {}):
                state["endpoints"][endpoint]["last_call_at"] = now
                state["endpoints"][endpoint]["call_count"] = state["endpoints"][endpoint].get("call_count", 0) + 1
                state["endpoints"][endpoint]["error_count"] = state["endpoints"][endpoint].get("error_count", 0) + 1

            self._save_state(state)

    def get_status(self) -> dict:
        """
        Get current rate limit status.

        Returns:
            Dict with rate limit info and call stats
        """
        with self._lock:
            state = self._load_state()

            result = {
                "is_rate_limited": False,
                "seconds_until_reset": None,
                "reset_at": state.get("rate_limit_reset_at"),
                "last_api_call_at": state.get("last_api_call_at"),
                "last_successful_call_at": state.get("last_successful_call_at"),
                "rate_limited_at": state.get("rate_limited_at"),
                # Monthly quota
                "monthly_cap_exceeded": state.get("monthly_cap_exceeded", False),
                "monthly_cap_exceeded_at": state.get("monthly_cap_exceeded_at"),
                "current_month": state.get("current_month"),
                "monthly_call_count": state.get("monthly_call_count", 0),
                # Totals
                "total_calls": state.get("total_calls", 0),
                "total_successes": state.get("total_successes", 0),
                "total_errors": state.get("total_errors", 0),
                # Endpoints
                "endpoints": state.get("endpoints", {})
            }

            # Check if rate limit has expired
            if state.get("is_rate_limited") and state.get("rate_limit_reset_at"):
                reset_at = datetime.fromisoformat(state["rate_limit_reset_at"])
                now = datetime.now(timezone.utc)

                if now >= reset_at:
                    # Rate limit has expired
                    state["is_rate_limited"] = False
                    state["rate_limit_reset_at"] = None
                    state["rate_limited_at"] = None
                    self._save_state(state)
                    result["is_rate_limited"] = False
                else:
                    # Still rate limited
                    result["is_rate_limited"] = True
                    result["seconds_until_reset"] = int((reset_at - now).total_seconds())

            return result

    def is_rate_limited(self) -> bool:
        """Check if we're currently rate limited."""
        return self.get_status()["is_rate_limited"]

    def is_monthly_cap_exceeded(self) -> bool:
        """Check if monthly cap has been exceeded."""
        return self.get_status()["monthly_cap_exceeded"]

    def get_seconds_until_reset(self) -> Optional[int]:
        """Get seconds until rate limit resets, or None if not limited."""
        status = self.get_status()
        if status["is_rate_limited"]:
            return status["seconds_until_reset"]
        return None

    def get_endpoint_stats(self, endpoint: str) -> Optional[dict]:
        """Get stats for a specific endpoint."""
        status = self.get_status()
        return status.get("endpoints", {}).get(endpoint)

    def clear(self):
        """Clear rate limit state (keeps call counts)."""
        with self._lock:
            state = self._load_state()
            state["is_rate_limited"] = False
            state["rate_limit_reset_at"] = None
            state["rate_limited_at"] = None
            state["monthly_cap_exceeded"] = False
            state["monthly_cap_exceeded_at"] = None
            self._save_state(state)

    def reset_all(self):
        """Reset all state including counters."""
        with self._lock:
            self._save_state(self._get_default_state())


# Global instance
rate_limit_tracker = RateLimitTracker()
