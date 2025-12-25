import json
import os
from datetime import datetime, timedelta
from typing import Optional
from dataclasses import asdict
from pathlib import Path

from app.twitter_client import TweetMetrics, UserInfo


class TweetCache:
    """
    File-based cache for user tweets.

    Structure:
    - cache/{username}.json
    - Contains: user info, tweets, last_fetched timestamp, oldest_tweet_id, newest_tweet_id
    - Expires after 30 days of no updates
    """

    CACHE_DIR = Path(__file__).parent.parent / "cache"
    CACHE_EXPIRY_DAYS = 30

    def __init__(self):
        self.CACHE_DIR.mkdir(exist_ok=True)

    def _get_cache_path(self, username: str) -> Path:
        """Get cache file path for a user."""
        return self.CACHE_DIR / f"{username.lower()}.json"

    def _tweet_to_dict(self, tweet: TweetMetrics) -> dict:
        """Convert TweetMetrics to JSON-serializable dict."""
        return {
            "tweet_id": tweet.tweet_id,
            "text": tweet.text,
            "created_at": tweet.created_at.isoformat(),
            "likes": tweet.likes,
            "retweets": tweet.retweets,
            "replies": tweet.replies,
            "quotes": tweet.quotes,
            "bookmarks": tweet.bookmarks,
            "url": tweet.url,
            "engagement_score": tweet.engagement_score,
            "engagement_rate": tweet.engagement_rate,
            "conversation_id": tweet.conversation_id,
            "is_thread_starter": tweet.is_thread_starter,
            "thread_position": tweet.thread_position
        }

    def _dict_to_tweet(self, data: dict) -> TweetMetrics:
        """Convert dict back to TweetMetrics."""
        return TweetMetrics(
            tweet_id=data["tweet_id"],
            text=data["text"],
            created_at=datetime.fromisoformat(data["created_at"]),
            likes=data["likes"],
            retweets=data["retweets"],
            replies=data["replies"],
            quotes=data["quotes"],
            bookmarks=data["bookmarks"],
            url=data["url"],
            engagement_score=data.get("engagement_score", 0.0),
            engagement_rate=data.get("engagement_rate", 0.0),
            conversation_id=data.get("conversation_id"),
            is_thread_starter=data.get("is_thread_starter", False),
            thread_position=data.get("thread_position", 0)
        )

    def _user_to_dict(self, user: UserInfo) -> dict:
        """Convert UserInfo to dict."""
        return {
            "id": user.id,
            "username": user.username,
            "name": user.name,
            "followers_count": user.followers_count,
            "following_count": user.following_count,
            "tweet_count": user.tweet_count,
            "profile_image_url": user.profile_image_url,
            "description": user.description
        }

    def _dict_to_user(self, data: dict) -> UserInfo:
        """Convert dict back to UserInfo."""
        return UserInfo(
            id=data["id"],
            username=data["username"],
            name=data["name"],
            followers_count=data["followers_count"],
            following_count=data["following_count"],
            tweet_count=data["tweet_count"],
            profile_image_url=data["profile_image_url"],
            description=data["description"]
        )

    def get_cached_data(self, username: str) -> Optional[dict]:
        """
        Get cached data for a user.

        Returns:
            Dict with keys: user, tweets, last_fetched, newest_tweet_id, oldest_tweet_id
            Or None if no cache exists or cache is expired
        """
        cache_path = self._get_cache_path(username)

        if not cache_path.exists():
            return None

        try:
            with open(cache_path, 'r') as f:
                data = json.load(f)

            # Check if cache is expired (no updates in 30 days)
            last_fetched = datetime.fromisoformat(data["last_fetched"])
            if datetime.utcnow() - last_fetched > timedelta(days=self.CACHE_EXPIRY_DAYS):
                # Cache expired, delete it
                cache_path.unlink()
                return None

            # Convert tweets back to TweetMetrics
            data["tweets"] = [self._dict_to_tweet(t) for t in data["tweets"]]
            data["user"] = self._dict_to_user(data["user"])
            data["last_fetched"] = last_fetched

            return data

        except (json.JSONDecodeError, KeyError, ValueError):
            # Corrupted cache, delete it
            cache_path.unlink()
            return None

    def save_cache(
        self,
        username: str,
        user: UserInfo,
        tweets: list[TweetMetrics],
        newest_tweet_id: Optional[str] = None,
        oldest_tweet_id: Optional[str] = None
    ):
        """
        Save tweets to cache.

        Args:
            username: Twitter username
            user: UserInfo object
            tweets: List of TweetMetrics to cache
            newest_tweet_id: ID of the newest tweet (for fetching newer tweets later)
            oldest_tweet_id: ID of the oldest tweet (for fetching older tweets later)
        """
        cache_path = self._get_cache_path(username)

        # Deduplicate tweets by ID
        seen_ids = set()
        unique_tweets = []
        for tweet in tweets:
            if tweet.tweet_id not in seen_ids:
                seen_ids.add(tweet.tweet_id)
                unique_tweets.append(tweet)

        # Sort by created_at descending (newest first)
        unique_tweets.sort(key=lambda t: t.created_at, reverse=True)

        # Determine newest/oldest IDs if not provided
        if unique_tweets:
            if not newest_tweet_id:
                newest_tweet_id = unique_tweets[0].tweet_id
            if not oldest_tweet_id:
                oldest_tweet_id = unique_tweets[-1].tweet_id

        data = {
            "user": self._user_to_dict(user),
            "tweets": [self._tweet_to_dict(t) for t in unique_tweets],
            "last_fetched": datetime.utcnow().isoformat(),
            "newest_tweet_id": newest_tweet_id,
            "oldest_tweet_id": oldest_tweet_id,
            "tweet_count": len(unique_tweets)
        }

        with open(cache_path, 'w') as f:
            json.dump(data, f, indent=2)

    def merge_tweets(
        self,
        cached_tweets: list[TweetMetrics],
        new_tweets: list[TweetMetrics]
    ) -> list[TweetMetrics]:
        """
        Merge cached tweets with newly fetched tweets.
        Deduplicates by tweet_id and sorts by created_at.

        Args:
            cached_tweets: Previously cached tweets
            new_tweets: Newly fetched tweets

        Returns:
            Merged and deduplicated list of tweets
        """
        # Use dict to deduplicate by ID, preferring newer data
        tweet_map = {}

        # Add cached tweets first
        for tweet in cached_tweets:
            tweet_map[tweet.tweet_id] = tweet

        # Overwrite with new tweets (they have fresher engagement metrics)
        for tweet in new_tweets:
            tweet_map[tweet.tweet_id] = tweet

        # Sort by created_at descending
        merged = list(tweet_map.values())
        merged.sort(key=lambda t: t.created_at, reverse=True)

        return merged

    def get_cache_stats(self, username: str) -> Optional[dict]:
        """Get cache statistics for a user."""
        cache_path = self._get_cache_path(username)

        if not cache_path.exists():
            return None

        try:
            with open(cache_path, 'r') as f:
                data = json.load(f)

            last_fetched = datetime.fromisoformat(data["last_fetched"])
            age_days = (datetime.utcnow() - last_fetched).days

            return {
                "username": username,
                "tweet_count": data.get("tweet_count", len(data.get("tweets", []))),
                "last_fetched": last_fetched.isoformat(),
                "age_days": age_days,
                "expires_in_days": max(0, self.CACHE_EXPIRY_DAYS - age_days),
                "newest_tweet_id": data.get("newest_tweet_id"),
                "oldest_tweet_id": data.get("oldest_tweet_id")
            }
        except (json.JSONDecodeError, KeyError, ValueError):
            return None

    def clear_cache(self, username: str) -> bool:
        """Clear cache for a specific user."""
        cache_path = self._get_cache_path(username)
        if cache_path.exists():
            cache_path.unlink()
            return True
        return False

    def clear_all_cache(self) -> int:
        """Clear all cached data. Returns number of files deleted."""
        count = 0
        for cache_file in self.CACHE_DIR.glob("*.json"):
            cache_file.unlink()
            count += 1
        return count

    def list_cached_users(self) -> list[dict]:
        """List all cached users with their stats."""
        users = []
        for cache_file in self.CACHE_DIR.glob("*.json"):
            username = cache_file.stem
            stats = self.get_cache_stats(username)
            if stats:
                users.append(stats)
        return users
