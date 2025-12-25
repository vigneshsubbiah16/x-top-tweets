import tweepy
from datetime import datetime, timedelta
from typing import Optional
from dataclasses import dataclass

from app.config import settings


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


class TwitterClient:
    def __init__(self):
        if not settings.TWITTER_BEARER_TOKEN:
            raise ValueError("TWITTER_BEARER_TOKEN is required")
        self.client = tweepy.Client(bearer_token=settings.TWITTER_BEARER_TOKEN)

    def get_user_info(self, username: str) -> Optional[UserInfo]:
        """Fetch user information by username."""
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

    def get_user_tweets(
        self,
        user_id: str,
        months: int = 1,
        max_results: int = 100
    ) -> list[TweetMetrics]:
        """Fetch user tweets from the last N months."""
        start_time = datetime.utcnow() - timedelta(days=months * 30)

        tweets = []
        pagination_token = None

        while True:
            try:
                response = self.client.get_users_tweets(
                    id=user_id,
                    start_time=start_time,
                    max_results=min(max_results, 100),
                    tweet_fields=["created_at", "public_metrics"],
                    pagination_token=pagination_token,
                    exclude=["retweets", "replies"]  # Only original tweets
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

                # Check for more pages
                if response.meta and "next_token" in response.meta:
                    pagination_token = response.meta["next_token"]
                else:
                    break

            except tweepy.errors.TooManyRequests:
                raise ValueError("Rate limit exceeded. Please try again later.")
            except Exception as e:
                raise ValueError(f"Error fetching tweets: {str(e)}")

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

        Final ranking considers both absolute engagement and rate.
        """
        for tweet in tweets:
            # Weighted engagement score
            tweet.engagement_score = (
                tweet.likes +
                (tweet.retweets * 2) +  # Retweets weighted higher
                tweet.replies +
                tweet.quotes +
                tweet.bookmarks
            )

            # Engagement rate (as percentage)
            if follower_count > 0:
                tweet.engagement_rate = (tweet.engagement_score / follower_count) * 100
            else:
                tweet.engagement_rate = 0.0

        return tweets

    def get_top_tweets(
        self,
        username: str,
        months: int = 1,
        top_n: int = 10
    ) -> tuple[UserInfo, list[TweetMetrics]]:
        """
        Get top N tweets for a user based on engagement.

        Ranking formula:
        - 70% weight on absolute engagement score
        - 30% weight on engagement rate
        """
        # Get user info
        user = self.get_user_info(username)
        if not user:
            raise ValueError(f"User @{username} not found")

        # Get tweets
        tweets = self.get_user_tweets(user.id, months=months)
        if not tweets:
            return user, []

        # Calculate engagement scores
        tweets = self.calculate_engagement_scores(tweets, user.followers_count)

        # Normalize scores for ranking
        max_score = max(t.engagement_score for t in tweets) if tweets else 1
        max_rate = max(t.engagement_rate for t in tweets) if tweets else 1

        # Combined ranking (70% absolute, 30% rate)
        def ranking_score(tweet: TweetMetrics) -> float:
            normalized_score = tweet.engagement_score / max_score if max_score > 0 else 0
            normalized_rate = tweet.engagement_rate / max_rate if max_rate > 0 else 0
            return (normalized_score * 0.7) + (normalized_rate * 0.3)

        # Sort by ranking score and return top N
        sorted_tweets = sorted(tweets, key=ranking_score, reverse=True)
        return user, sorted_tweets[:top_n]
