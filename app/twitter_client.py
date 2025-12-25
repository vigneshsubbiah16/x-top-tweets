import tweepy
import time
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Callable
from dataclasses import dataclass, field

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass
class TweetMetrics:
    tweet_id: str
    text: str
    created_at: datetime
    likes: int
    retweets: int
    replies: int
    quotes: int
    bookmarks: int
    url: str
    engagement_score: float = 0.0
    engagement_rate: float = 0.0
    # Thread/conversation fields
    conversation_id: Optional[str] = None
    is_thread_starter: bool = False  # True if this tweet starts a thread
    thread_position: int = 0  # 0 = standalone or thread starter, 1+ = reply in thread


@dataclass
class UserInfo:
    id: str
    username: str
    name: str
    followers_count: int
    following_count: int
    tweet_count: int
    profile_image_url: str
    description: str


@dataclass
class FetchProgress:
    """Track progress of tweet fetching."""
    total_fetched: int = 0
    cached_count: int = 0
    new_count: int = 0
    pages_fetched: int = 0
    is_rate_limited: bool = False
    wait_seconds: int = 0
    status: str = "starting"
    using_cache: bool = False


class TwitterClient:
    RATE_LIMIT_WAIT_SECONDS = 15 * 60  # 15 minutes
    MAX_TWEETS_PER_REQUEST = 100
    MAX_TOTAL_TWEETS = 3200  # Twitter's limit for user timeline

    def __init__(self):
        if not settings.TWITTER_BEARER_TOKEN:
            raise ValueError("TWITTER_BEARER_TOKEN is required")
        self.client = tweepy.Client(
            bearer_token=settings.TWITTER_BEARER_TOKEN,
            wait_on_rate_limit=False  # We'll handle this ourselves
        )

    def get_user_info(self, username: str, retry_on_rate_limit: bool = True) -> Optional[UserInfo]:
        """Fetch user information by username with rate limit retry."""
        max_retries = 3 if retry_on_rate_limit else 1

        for attempt in range(max_retries):
            try:
                user = self.client.get_user(
                    username=username,
                    user_fields=["public_metrics", "profile_image_url", "description"]
                )
                if not user.data:
                    return None

                data = user.data
                metrics = data.public_metrics

                return UserInfo(
                    id=str(data.id),
                    username=data.username,
                    name=data.name,
                    followers_count=metrics["followers_count"],
                    following_count=metrics["following_count"],
                    tweet_count=metrics["tweet_count"],
                    profile_image_url=data.profile_image_url or "",
                    description=data.description or ""
                )
            except tweepy.errors.NotFound:
                return None
            except tweepy.errors.Unauthorized:
                raise ValueError("Invalid Twitter API credentials")
            except tweepy.errors.TooManyRequests as e:
                if attempt < max_retries - 1:
                    wait_time = self._get_rate_limit_reset(e)
                    logger.info(f"Rate limited on user lookup. Waiting {wait_time}s...")
                    time.sleep(wait_time)
                else:
                    raise ValueError(f"Rate limit exceeded. Try again in 15 minutes.")

    def _get_rate_limit_reset(self, error: tweepy.errors.TooManyRequests) -> int:
        """Extract wait time from rate limit error, default to 15 minutes."""
        try:
            if hasattr(error, 'response') and error.response is not None:
                reset_time = error.response.headers.get('x-rate-limit-reset')
                if reset_time:
                    wait_seconds = int(reset_time) - int(time.time())
                    if wait_seconds > 0:
                        return min(wait_seconds + 5, self.RATE_LIMIT_WAIT_SECONDS)
        except Exception:
            pass
        return self.RATE_LIMIT_WAIT_SECONDS

    def fetch_new_tweets_since(
        self,
        user_id: str,
        since_id: str,
        months: int = 1,
        retry_on_rate_limit: bool = True,
        progress_callback: Optional[Callable[[FetchProgress], None]] = None
    ) -> list[TweetMetrics]:
        """
        Fetch only tweets newer than since_id.

        This is used for smart caching - only fetch what we don't have.
        """
        start_time = datetime.now(timezone.utc) - timedelta(days=months * 30)
        tweets: list[TweetMetrics] = []
        pagination_token = None
        progress = FetchProgress(using_cache=True)

        def update_progress(status: str, rate_limited: bool = False, wait_secs: int = 0):
            progress.new_count = len(tweets)
            progress.total_fetched = len(tweets)
            progress.status = status
            progress.is_rate_limited = rate_limited
            progress.wait_seconds = wait_secs
            if progress_callback:
                progress_callback(progress)

        update_progress("fetching new tweets")

        while True:
            try:
                response = self.client.get_users_tweets(
                    id=user_id,
                    since_id=since_id,  # Only get tweets newer than this
                    start_time=start_time,
                    max_results=self.MAX_TWEETS_PER_REQUEST,
                    tweet_fields=["created_at", "public_metrics"],
                    pagination_token=pagination_token,
                    exclude=["retweets", "replies"]
                )

                if not response.data:
                    break

                for tweet in response.data:
                    metrics = tweet.public_metrics
                    tweets.append(TweetMetrics(
                        tweet_id=str(tweet.id),
                        text=tweet.text,
                        created_at=tweet.created_at,
                        likes=metrics["like_count"],
                        retweets=metrics["retweet_count"],
                        replies=metrics["reply_count"],
                        quotes=metrics.get("quote_count", 0),
                        bookmarks=metrics.get("bookmark_count", 0),
                        url=f"https://x.com/i/status/{tweet.id}"
                    ))

                progress.pages_fetched += 1
                update_progress(f"fetched {len(tweets)} new tweets")

                if response.meta and "next_token" in response.meta:
                    pagination_token = response.meta["next_token"]
                else:
                    break

            except tweepy.errors.TooManyRequests as e:
                if retry_on_rate_limit:
                    wait_time = self._get_rate_limit_reset(e)
                    logger.info(f"Rate limited. Waiting {wait_time}s...")
                    update_progress(f"rate limited", rate_limited=True, wait_secs=wait_time)
                    time.sleep(wait_time)
                    update_progress("resuming")
                else:
                    raise ValueError(f"Rate limit exceeded after fetching {len(tweets)} tweets.")
            except Exception as e:
                raise ValueError(f"Error fetching tweets: {str(e)}")

        update_progress(f"completed - {len(tweets)} new tweets")
        return tweets

    def get_user_tweets(
        self,
        user_id: str,
        months: int = 1,
        max_tweets: int = 3200,
        retry_on_rate_limit: bool = True,
        progress_callback: Optional[Callable[[FetchProgress], None]] = None
    ) -> list[TweetMetrics]:
        """
        Fetch user tweets from the last N months with automatic pagination
        and rate limit handling.

        Args:
            user_id: Twitter user ID
            months: Number of months to look back
            max_tweets: Maximum tweets to fetch (up to 3200)
            retry_on_rate_limit: If True, wait and retry when rate limited
            progress_callback: Optional callback for progress updates

        Returns:
            List of TweetMetrics, stitched across all paginated API calls
        """
        start_time = datetime.now(timezone.utc) - timedelta(days=months * 30)
        max_tweets = min(max_tweets, self.MAX_TOTAL_TWEETS)

        tweets: list[TweetMetrics] = []
        pagination_token = None
        progress = FetchProgress()

        def update_progress(status: str, rate_limited: bool = False, wait_secs: int = 0):
            progress.total_fetched = len(tweets)
            progress.status = status
            progress.is_rate_limited = rate_limited
            progress.wait_seconds = wait_secs
            if progress_callback:
                progress_callback(progress)

        update_progress("fetching")

        while len(tweets) < max_tweets:
            try:
                response = self.client.get_users_tweets(
                    id=user_id,
                    start_time=start_time,
                    max_results=self.MAX_TWEETS_PER_REQUEST,
                    tweet_fields=["created_at", "public_metrics"],
                    pagination_token=pagination_token,
                    exclude=["retweets", "replies"]  # Only original tweets
                )

                if not response.data:
                    break

                # Stitch tweets from this page
                for tweet in response.data:
                    if len(tweets) >= max_tweets:
                        break
                    metrics = tweet.public_metrics
                    tweets.append(TweetMetrics(
                        tweet_id=str(tweet.id),
                        text=tweet.text,
                        created_at=tweet.created_at,
                        likes=metrics["like_count"],
                        retweets=metrics["retweet_count"],
                        replies=metrics["reply_count"],
                        quotes=metrics.get("quote_count", 0),
                        bookmarks=metrics.get("bookmark_count", 0),
                        url=f"https://x.com/i/status/{tweet.id}"
                    ))

                progress.pages_fetched += 1
                update_progress(f"fetched {len(tweets)} tweets")

                # Check for more pages
                if response.meta and "next_token" in response.meta:
                    pagination_token = response.meta["next_token"]
                else:
                    break  # No more pages

            except tweepy.errors.TooManyRequests as e:
                if retry_on_rate_limit:
                    wait_time = self._get_rate_limit_reset(e)
                    logger.info(f"Rate limited. Waiting {wait_time}s before continuing...")
                    update_progress(
                        f"rate limited - waiting {wait_time // 60} min",
                        rate_limited=True,
                        wait_secs=wait_time
                    )
                    time.sleep(wait_time)
                    update_progress("resuming fetch")
                    # Continue loop - don't break, retry the same request
                else:
                    update_progress("rate limited - stopped")
                    raise ValueError(
                        f"Rate limit exceeded after fetching {len(tweets)} tweets. "
                        f"Try again in 15 minutes to continue."
                    )
            except Exception as e:
                update_progress(f"error: {str(e)}")
                raise ValueError(f"Error fetching tweets: {str(e)}")

        update_progress(f"completed - {len(tweets)} tweets")
        return tweets

    def calculate_engagement_scores(
        self,
        tweets: list[TweetMetrics],
        follower_count: int
    ) -> list[TweetMetrics]:
        """
        Calculate engagement scores for tweets.

        Engagement Score = likes + (retweets * 2) + replies + quotes + bookmarks
        Engagement Rate = engagement_score / follower_count * 100
        """
        for tweet in tweets:
            tweet.engagement_score = (
                tweet.likes +
                (tweet.retweets * 2) +
                tweet.replies +
                tweet.quotes +
                tweet.bookmarks
            )

            if follower_count > 0:
                tweet.engagement_rate = (tweet.engagement_score / follower_count) * 100
            else:
                tweet.engagement_rate = 0.0

        return tweets

    def get_top_tweets(
        self,
        username: str,
        months: int = 1,
        top_n: int = 10,
        retry_on_rate_limit: bool = True,
        progress_callback: Optional[Callable[[FetchProgress], None]] = None
    ) -> tuple[UserInfo, list[TweetMetrics], int]:
        """
        Get top N tweets for a user based on engagement.

        Ranking formula:
        - 70% weight on absolute engagement score
        - 30% weight on engagement rate

        Returns:
            Tuple of (UserInfo, top tweets, total tweets analyzed)
        """
        # Get user info
        user = self.get_user_info(username, retry_on_rate_limit=retry_on_rate_limit)
        if not user:
            raise ValueError(f"User @{username} not found")

        # Get all tweets with pagination and rate limit handling
        tweets = self.get_user_tweets(
            user.id,
            months=months,
            retry_on_rate_limit=retry_on_rate_limit,
            progress_callback=progress_callback
        )

        total_analyzed = len(tweets)

        if not tweets:
            return user, [], 0

        # Calculate engagement scores
        tweets = self.calculate_engagement_scores(tweets, user.followers_count)

        # Normalize scores for ranking
        max_score = max(t.engagement_score for t in tweets) if tweets else 1
        max_rate = max(t.engagement_rate for t in tweets) if tweets else 1

        def ranking_score(tweet: TweetMetrics) -> float:
            normalized_score = tweet.engagement_score / max_score if max_score > 0 else 0
            normalized_rate = tweet.engagement_rate / max_rate if max_rate > 0 else 0
            return (normalized_score * 0.7) + (normalized_rate * 0.3)

        sorted_tweets = sorted(tweets, key=ranking_score, reverse=True)
        return user, sorted_tweets[:top_n], total_analyzed
