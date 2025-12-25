from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from typing import Optional, AsyncGenerator
import os
import json
import asyncio
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor

from app.twitter_client import TwitterClient, TweetMetrics, UserInfo, FetchProgress
from app.cache import TweetCache
from app.config import settings
from app.debug_logger import log_api_response, log_user_response, list_debug_logs, get_debug_log, clear_debug_logs
from app.content_analyzer import analyze_tweet, analyze_patterns, rewrite_for_linkedin, TweetAnalysis, PatternInsights
from app.rate_limit_tracker import rate_limit_tracker

app = FastAPI(
    title="Twitter Top Tweets Analyzer",
    description="Find the top performing tweets for any Twitter/X user",
    version="1.0.0"
)

# Get the directory where this file is located
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")

# Thread pool for running blocking Twitter API calls
executor = ThreadPoolExecutor(max_workers=4)

# Initialize cache
cache = TweetCache()


class AnalyzeRequest(BaseModel):
    username: str
    months: int = 1
    top_n: int = 10


class TweetResponse(BaseModel):
    tweet_id: str
    text: str
    created_at: str
    likes: int
    retweets: int
    replies: int
    quotes: int
    bookmarks: int
    url: str
    engagement_score: float
    engagement_rate: float


class UserResponse(BaseModel):
    id: str
    username: str
    name: str
    followers_count: int
    following_count: int
    tweet_count: int
    profile_image_url: str
    description: str


class AnalyzeResponse(BaseModel):
    user: UserResponse
    tweets: list[TweetResponse]
    total_tweets_analyzed: int
    time_period_months: int


class CacheStatsResponse(BaseModel):
    username: str
    tweet_count: int
    last_fetched: str
    age_days: int
    expires_in_days: int


def check_api_configured() -> bool:
    """Check if Twitter API is configured."""
    return bool(settings.TWITTER_BEARER_TOKEN)


def tweet_to_dict(tweet: TweetMetrics) -> dict:
    """Convert TweetMetrics to JSON-serializable dict."""
    return {
        "tweet_id": tweet.tweet_id,
        "text": tweet.text,
        "created_at": tweet.created_at.isoformat() if isinstance(tweet.created_at, datetime) else tweet.created_at,
        "likes": tweet.likes,
        "retweets": tweet.retweets,
        "replies": tweet.replies,
        "quotes": tweet.quotes,
        "bookmarks": tweet.bookmarks,
        "url": tweet.url,
        "engagement_score": round(tweet.engagement_score, 2),
        "engagement_rate": round(tweet.engagement_rate, 4),
        "conversation_id": tweet.conversation_id,
        "is_thread_starter": tweet.is_thread_starter,
        "thread_position": tweet.thread_position
    }


def user_to_dict(user: UserInfo) -> dict:
    """Convert UserInfo to JSON-serializable dict."""
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


def calculate_top_tweets(tweets: list[TweetMetrics], follower_count: int, top_n: int = 10) -> list[TweetMetrics]:
    """Calculate engagement scores and return top N tweets."""
    if not tweets:
        return []

    # Calculate engagement scores
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

    # Normalize and rank
    max_score = max(t.engagement_score for t in tweets) if tweets else 1
    max_rate = max(t.engagement_rate for t in tweets) if tweets else 1

    def ranking_score(tweet: TweetMetrics) -> float:
        normalized_score = tweet.engagement_score / max_score if max_score > 0 else 0
        normalized_rate = tweet.engagement_rate / max_rate if max_rate > 0 else 0
        return (normalized_score * 0.7) + (normalized_rate * 0.3)

    sorted_tweets = sorted(tweets, key=ranking_score, reverse=True)
    return sorted_tweets[:top_n]


def filter_tweets_by_months(tweets: list[TweetMetrics], months: int) -> list[TweetMetrics]:
    """Filter tweets to only include those within the specified month range."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=months * 30)
    filtered = []
    for t in tweets:
        # Handle both naive and aware datetimes
        tweet_time = t.created_at
        if tweet_time.tzinfo is None:
            tweet_time = tweet_time.replace(tzinfo=timezone.utc)
        if tweet_time >= cutoff:
            filtered.append(t)
    return filtered


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Render the home page."""
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "api_configured": check_api_configured()
        }
    )


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "api_configured": check_api_configured()
    }


@app.get("/api/cache")
async def list_cache():
    """List all cached users."""
    return {"cached_users": cache.list_cached_users()}


@app.get("/api/cache/{username}")
async def get_cache_stats(username: str):
    """Get cache stats for a specific user."""
    stats = cache.get_cache_stats(username)
    if not stats:
        raise HTTPException(status_code=404, detail=f"No cache found for @{username}")
    return stats


@app.delete("/api/cache/{username}")
async def clear_user_cache(username: str):
    """Clear cache for a specific user."""
    if cache.clear_cache(username):
        return {"message": f"Cache cleared for @{username}"}
    raise HTTPException(status_code=404, detail=f"No cache found for @{username}")


@app.delete("/api/cache")
async def clear_all_caches():
    """Clear all cached data."""
    count = cache.clear_all_cache()
    return {"message": f"Cleared {count} cached users"}


# Debug log endpoints
@app.get("/api/debug/logs")
async def get_debug_logs():
    """List all debug log files."""
    return {"logs": list_debug_logs()}


@app.get("/api/debug/logs/{filename}")
async def get_debug_log_file(filename: str):
    """Get a specific debug log file."""
    log = get_debug_log(filename)
    if not log:
        raise HTTPException(status_code=404, detail=f"Log file {filename} not found")
    return log


@app.delete("/api/debug/logs")
async def clear_all_debug_logs():
    """Clear all debug logs."""
    count = clear_debug_logs()
    return {"message": f"Cleared {count} debug log files"}


# Rate limit tracking endpoints
@app.get("/api/rate-limit")
async def get_rate_limit_status():
    """Get current rate limit status."""
    return rate_limit_tracker.get_status()


@app.delete("/api/rate-limit")
async def clear_rate_limit_status():
    """Clear rate limit state (for testing/debugging)."""
    rate_limit_tracker.clear()
    return {"message": "Rate limit state cleared"}


@app.get("/analyze/stream")
async def analyze_stream(username: str, months: int = 1, top_n: int = 10):
    """
    Stream tweet analysis results using Server-Sent Events with smart caching.

    - Uses cached tweets if available
    - Only fetches new tweets since last cache
    - Merges cached + new tweets for ranking
    - Saves updated cache after completion
    """
    if not check_api_configured():
        async def error_stream():
            yield f"data: {json.dumps({'type': 'error', 'message': 'Twitter API not configured'})}\n\n"
        return StreamingResponse(error_stream(), media_type="text/event-stream")

    username = username.lstrip("@").strip().lower()
    if not username:
        async def error_stream():
            yield f"data: {json.dumps({'type': 'error', 'message': 'Username is required'})}\n\n"
        return StreamingResponse(error_stream(), media_type="text/event-stream")

    async def event_stream() -> AsyncGenerator[str, None]:
        try:
            client = TwitterClient()

            # Check if we're already rate limited or monthly cap exceeded before making any calls
            rate_status = rate_limit_tracker.get_status()

            # Check monthly cap first (not recoverable by waiting)
            if rate_status["monthly_cap_exceeded"]:
                yield f"data: {json.dumps({'type': 'monthly_cap', 'data': {'total_fetched': 0, 'cached_count': 0, 'new_count': 0, 'top_tweets': [], 'no_tweets_yet': True, 'message': 'Twitter API monthly usage cap was exceeded. Your free tier limit has been reached. Please wait until next month or upgrade your Twitter API plan.'}})}\n\n"
                return

            if rate_status["is_rate_limited"]:
                wait_time = rate_status["seconds_until_reset"] or 900
                yield f"data: {json.dumps({'type': 'rate_limited', 'data': {'total_fetched': 0, 'cached_count': 0, 'new_count': 0, 'wait_seconds': wait_time, 'top_tweets': [], 'no_tweets_yet': True, 'message': 'API rate limited from previous request'}})}\n\n"

                # Wait and countdown - send updates every 10 seconds
                for remaining in range(wait_time, 0, -10):
                    await asyncio.sleep(10)
                    yield f"data: {json.dumps({'type': 'waiting', 'data': {'remaining_seconds': max(0, remaining - 10)}})}\n\n"

                yield f"data: {json.dumps({'type': 'resuming'})}\n\n"
                rate_limit_tracker.clear()

            # Check for cached data
            cached_data = cache.get_cached_data(username)
            cached_tweets: list[TweetMetrics] = []
            newest_tweet_id: Optional[str] = None

            if cached_data:
                cached_tweets = cached_data["tweets"]
                newest_tweet_id = cached_data.get("newest_tweet_id")
                cached_user = cached_data["user"]

                # Send cache info
                yield f"data: {json.dumps({'type': 'cache_info', 'data': {'cached_count': len(cached_tweets), 'last_fetched': cached_data['last_fetched'].isoformat(), 'newest_tweet_id': newest_tweet_id}})}\n\n"

                # Use cached user info initially (will refresh)
                yield f"data: {json.dumps({'type': 'user', 'data': user_to_dict(cached_user)})}\n\n"

                # Show cached results immediately
                filtered_cached = filter_tweets_by_months(cached_tweets, months)
                if filtered_cached:
                    current_top = calculate_top_tweets(filtered_cached.copy(), cached_user.followers_count, top_n)
                    yield f"data: {json.dumps({'type': 'progress', 'data': {'total_fetched': len(filtered_cached), 'cached_count': len(filtered_cached), 'new_count': 0, 'status': 'showing cached', 'top_tweets': [tweet_to_dict(t) for t in current_top]}})}\n\n"

            # Get fresh user info
            loop = asyncio.get_event_loop()
            try:
                user = await loop.run_in_executor(executor, client.get_user_info, username)
                rate_limit_tracker.record_success(endpoint="get_user")
            except Exception as e:
                error_str = str(e)
                log_user_response(username, None, error=error_str)

                if "Rate limit" in error_str or "429" in error_str or "Too Many Requests" in error_str:
                    wait_time = client.RATE_LIMIT_WAIT_SECONDS
                    rate_limit_tracker.record_rate_limit(wait_time, endpoint="get_user")

                    # Use cached user if available
                    if cached_data and cached_data.get("user"):
                        user = cached_data["user"]
                        yield f"data: {json.dumps({'type': 'user', 'data': user_to_dict(user)})}\n\n"
                        yield f"data: {json.dumps({'type': 'rate_limited', 'data': {'total_fetched': len(cached_tweets), 'cached_count': len(cached_tweets), 'new_count': 0, 'wait_seconds': wait_time, 'top_tweets': [tweet_to_dict(t) for t in calculate_top_tweets(filter_tweets_by_months(cached_tweets, months), user.followers_count, top_n)] if cached_tweets else [], 'no_tweets_yet': len(cached_tweets) == 0, 'message': 'Rate limited during user lookup'}})}\n\n"
                    else:
                        yield f"data: {json.dumps({'type': 'rate_limited', 'data': {'total_fetched': 0, 'cached_count': 0, 'new_count': 0, 'wait_seconds': wait_time, 'top_tweets': [], 'no_tweets_yet': True, 'message': 'Rate limited during user lookup'}})}\n\n"

                    # Wait and countdown
                    for remaining in range(wait_time, 0, -10):
                        await asyncio.sleep(10)
                        yield f"data: {json.dumps({'type': 'waiting', 'data': {'remaining_seconds': max(0, remaining - 10)}})}\n\n"

                    yield f"data: {json.dumps({'type': 'resuming'})}\n\n"
                    rate_limit_tracker.clear()

                    # Retry user lookup
                    try:
                        user = await loop.run_in_executor(executor, client.get_user_info, username)
                        rate_limit_tracker.record_success(endpoint="get_user")
                    except Exception as retry_e:
                        yield f"data: {json.dumps({'type': 'error', 'message': f'Error fetching user info: {str(retry_e)}'})}\n\n"
                        return
                else:
                    yield f"data: {json.dumps({'type': 'error', 'message': f'Error fetching user info: {error_str}'})}\n\n"
                    return

            # Log user lookup response
            log_user_response(username, user, error=None if user else "User not found")

            if not user:
                yield f"data: {json.dumps({'type': 'error', 'message': f'User @{username} not found'})}\n\n"
                return

            # Send updated user info
            yield f"data: {json.dumps({'type': 'user', 'data': user_to_dict(user)})}\n\n"

            # Fetch new tweets
            all_tweets: list[TweetMetrics] = list(cached_tweets)  # Start with cached
            new_tweets: list[TweetMetrics] = []

            start_time = datetime.now(timezone.utc) - timedelta(days=months * 30)
            pagination_token = None

            # Determine fetch strategy
            if newest_tweet_id:
                yield f"data: {json.dumps({'type': 'status', 'data': {'message': f'Fetching new tweets since last cache...'}})}\n\n"
            else:
                yield f"data: {json.dumps({'type': 'status', 'data': {'message': 'Fetching all tweets...'}})}\n\n"

            while len(new_tweets) < client.MAX_TOTAL_TWEETS:
                try:
                    # Build request params
                    # note_tweet field contains full text for longer tweets (280+ chars)
                    params = {
                        "id": user.id,
                        "start_time": start_time,
                        "max_results": client.MAX_TWEETS_PER_REQUEST,
                        "tweet_fields": ["created_at", "public_metrics", "conversation_id", "note_tweet"],
                        "exclude": ["retweets", "replies"]
                    }

                    if pagination_token:
                        params["pagination_token"] = pagination_token

                    # If we have cache, only get newer tweets
                    if newest_tweet_id and not pagination_token:
                        params["since_id"] = newest_tweet_id

                    response = client.client.get_users_tweets(**params)

                    # Record successful API call
                    rate_limit_tracker.record_success(endpoint="get_users_tweets")

                    # Log the raw API response
                    log_api_response(
                        endpoint="get_users_tweets",
                        username=username,
                        response_data=response.data,
                        response_meta=response.meta,
                        params={k: str(v) for k, v in params.items()}  # Convert to strings for JSON
                    )

                    if not response.data:
                        break

                    # Add tweets to collection
                    for tweet in response.data:
                        metrics = tweet.public_metrics
                        tweet_id_str = str(tweet.id)
                        conv_id = str(tweet.conversation_id) if tweet.conversation_id else tweet_id_str
                        # A tweet is a thread starter if it's the start of a conversation
                        # and has self-replies (we detect this by reply_count > 0 for original tweets)
                        is_thread_starter = (conv_id == tweet_id_str) and metrics["reply_count"] > 0

                        # Get full text from note_tweet if available (for longer tweets 280+ chars)
                        full_text = tweet.text
                        if hasattr(tweet, 'note_tweet') and tweet.note_tweet:
                            # note_tweet contains the full untruncated text
                            if hasattr(tweet.note_tweet, 'text'):
                                full_text = tweet.note_tweet.text
                            elif isinstance(tweet.note_tweet, dict) and 'text' in tweet.note_tweet:
                                full_text = tweet.note_tweet['text']

                        new_tweet = TweetMetrics(
                            tweet_id=tweet_id_str,
                            text=full_text,
                            created_at=tweet.created_at,
                            likes=metrics["like_count"],
                            retweets=metrics["retweet_count"],
                            replies=metrics["reply_count"],
                            quotes=metrics.get("quote_count", 0),
                            bookmarks=metrics.get("bookmark_count", 0),
                            url=f"https://x.com/i/status/{tweet.id}",
                            conversation_id=conv_id,
                            is_thread_starter=is_thread_starter,
                            thread_position=0
                        )
                        new_tweets.append(new_tweet)

                    # Merge with cached and calculate top
                    all_tweets = cache.merge_tweets(cached_tweets, new_tweets)
                    filtered = filter_tweets_by_months(all_tweets, months)
                    current_top = calculate_top_tweets(filtered.copy(), user.followers_count, top_n)

                    yield f"data: {json.dumps({'type': 'progress', 'data': {'total_fetched': len(filtered), 'cached_count': len(cached_tweets), 'new_count': len(new_tweets), 'status': 'fetching', 'top_tweets': [tweet_to_dict(t) for t in current_top]}})}\n\n"

                    # Check for more pages
                    if response.meta and "next_token" in response.meta:
                        pagination_token = response.meta["next_token"]
                        await asyncio.sleep(0.1)
                    else:
                        break

                except Exception as e:
                    error_str = str(e)

                    # Log the error
                    log_api_response(
                        endpoint="get_users_tweets",
                        username=username,
                        response_data=None,
                        response_meta=None,
                        params={k: str(v) for k, v in params.items()},
                        error=error_str
                    )

                    if "Too Many Requests" in error_str or "429" in error_str:
                        # Check if this is a monthly cap (not recoverable by waiting)
                        is_monthly_cap = "Usage cap exceeded" in error_str or "Monthly" in error_str

                        if is_monthly_cap:
                            # Monthly cap - can't recover by waiting
                            rate_limit_tracker.record_monthly_cap_exceeded(endpoint="get_users_tweets")

                            all_tweets = cache.merge_tweets(cached_tweets, new_tweets)
                            filtered = filter_tweets_by_months(all_tweets, months)
                            current_top = calculate_top_tweets(filtered.copy(), user.followers_count, top_n) if filtered else []

                            yield f"data: {json.dumps({'type': 'monthly_cap', 'data': {'total_fetched': len(filtered), 'cached_count': len(cached_tweets), 'new_count': len(new_tweets), 'top_tweets': [tweet_to_dict(t) for t in current_top], 'no_tweets_yet': len(current_top) == 0, 'message': 'Twitter API monthly usage cap exceeded. Your free tier limit has been reached. Please wait until next month or upgrade your Twitter API plan.'}})}\n\n"
                            return

                        wait_time = client.RATE_LIMIT_WAIT_SECONDS

                        # Record rate limit for tracking
                        rate_limit_tracker.record_rate_limit(wait_time, endpoint="get_users_tweets")

                        # Merge what we have so far
                        all_tweets = cache.merge_tweets(cached_tweets, new_tweets)
                        filtered = filter_tweets_by_months(all_tweets, months)
                        current_top = calculate_top_tweets(filtered.copy(), user.followers_count, top_n) if filtered else []

                        yield f"data: {json.dumps({'type': 'rate_limited', 'data': {'total_fetched': len(filtered), 'cached_count': len(cached_tweets), 'new_count': len(new_tweets), 'wait_seconds': wait_time, 'top_tweets': [tweet_to_dict(t) for t in current_top], 'no_tweets_yet': len(current_top) == 0}})}\n\n"

                        # Wait and countdown - send updates every 10 seconds
                        for remaining in range(wait_time, 0, -10):
                            await asyncio.sleep(10)
                            yield f"data: {json.dumps({'type': 'waiting', 'data': {'remaining_seconds': remaining - 10}})}\n\n"

                        yield f"data: {json.dumps({'type': 'resuming'})}\n\n"
                    else:
                        yield f"data: {json.dumps({'type': 'error', 'message': f'Error fetching tweets: {error_str}'})}\n\n"
                        return

            # Final merge and save to cache
            all_tweets = cache.merge_tweets(cached_tweets, new_tweets)

            # Save to cache (all tweets, not filtered)
            if all_tweets:
                newest_id = max(all_tweets, key=lambda t: t.created_at).tweet_id if all_tweets else None
                oldest_id = min(all_tweets, key=lambda t: t.created_at).tweet_id if all_tweets else None
                cache.save_cache(username, user, all_tweets, newest_id, oldest_id)

            # Filter for requested time period and send final results
            filtered = filter_tweets_by_months(all_tweets, months)
            final_top = calculate_top_tweets(filtered, user.followers_count, top_n)

            yield f"data: {json.dumps({'type': 'complete', 'data': {'total_analyzed': len(filtered), 'cached_count': len(cached_tweets), 'new_count': len(new_tweets), 'top_tweets': [tweet_to_dict(t) for t in final_top], 'user': user_to_dict(user), 'cache_saved': True}})}\n\n"

        except ValueError as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': f'An error occurred: {str(e)}'})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


@app.post("/api/analyze", response_model=AnalyzeResponse)
async def analyze_user(request: AnalyzeRequest):
    """
    Analyze a Twitter user's tweets and return top performers.
    Uses smart caching to minimize API calls.
    """
    if not check_api_configured():
        raise HTTPException(
            status_code=503,
            detail="Twitter API not configured. Please set TWITTER_BEARER_TOKEN."
        )

    username = request.username.lstrip("@").strip().lower()
    if not username:
        raise HTTPException(status_code=400, detail="Username is required")

    if request.months not in [1, 3]:
        raise HTTPException(status_code=400, detail="Months must be 1 or 3")

    if request.top_n < 1 or request.top_n > 50:
        raise HTTPException(status_code=400, detail="top_n must be between 1 and 50")

    try:
        client = TwitterClient()

        # Check cache first
        cached_data = cache.get_cached_data(username)
        cached_tweets: list[TweetMetrics] = []
        newest_tweet_id: Optional[str] = None

        if cached_data:
            cached_tweets = cached_data["tweets"]
            newest_tweet_id = cached_data.get("newest_tweet_id")

        # Get fresh user info
        user = client.get_user_info(username)
        if not user:
            raise ValueError(f"User @{username} not found")

        # Fetch new tweets only
        new_tweets: list[TweetMetrics] = []
        if newest_tweet_id:
            new_tweets = client.fetch_new_tweets_since(user.id, newest_tweet_id, months=request.months)
        else:
            new_tweets = client.get_user_tweets(user.id, months=request.months)

        # Merge tweets
        all_tweets = cache.merge_tweets(cached_tweets, new_tweets)

        # Save to cache
        if all_tweets:
            newest_id = max(all_tweets, key=lambda t: t.created_at).tweet_id
            oldest_id = min(all_tweets, key=lambda t: t.created_at).tweet_id
            cache.save_cache(username, user, all_tweets, newest_id, oldest_id)

        # Filter and rank
        filtered = filter_tweets_by_months(all_tweets, request.months)
        total_analyzed = len(filtered)

        if not filtered:
            return AnalyzeResponse(
                user=UserResponse(**user_to_dict(user)),
                tweets=[],
                total_tweets_analyzed=0,
                time_period_months=request.months
            )

        top_tweets = calculate_top_tweets(filtered, user.followers_count, request.top_n)

        tweet_responses = [
            TweetResponse(**tweet_to_dict(t))
            for t in top_tweets
        ]

        return AnalyzeResponse(
            user=UserResponse(**user_to_dict(user)),
            tweets=tweet_responses,
            total_tweets_analyzed=total_analyzed,
            time_period_months=request.months
        )

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"An error occurred: {str(e)}")


@app.get("/analyze", response_class=HTMLResponse)
async def analyze_page(request: Request, username: str = "", months: int = 1):
    """Render the streaming analysis page."""
    if not check_api_configured():
        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "api_configured": False,
                "error": "Twitter API not configured."
            }
        )

    if not username:
        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "api_configured": True
            }
        )

    # Check if we have cache for this user
    cache_stats = cache.get_cache_stats(username.lower())

    return templates.TemplateResponse(
        "stream.html",
        {
            "request": request,
            "username": username.lstrip("@").strip(),
            "months": months,
            "api_configured": True,
            "cache_stats": cache_stats
        }
    )


@app.get("/u/{username}", response_class=HTMLResponse)
async def user_analyze_page(request: Request, username: str, months: int = 1, top_n: int = 10):
    """
    User-specific analysis page with clean URL.
    Example: /u/levelsio or /u/@levelsio
    """
    if not check_api_configured():
        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "api_configured": False,
                "error": "Twitter API not configured."
            }
        )

    username = username.lstrip("@").strip()
    if not username:
        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "api_configured": True
            }
        )

    # Check if we have cache for this user
    cache_stats = cache.get_cache_stats(username.lower())

    return templates.TemplateResponse(
        "stream.html",
        {
            "request": request,
            "username": username,
            "months": months,
            "top_n": top_n,
            "api_configured": True,
            "cache_stats": cache_stats
        }
    )


@app.post("/analyze", response_class=HTMLResponse)
async def analyze_form(
    request: Request,
    username: str = Form(...),
    months: int = Form(1)
):
    """Handle form submission - redirect to user-specific URL."""
    from fastapi.responses import RedirectResponse

    if not check_api_configured():
        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "api_configured": False,
                "error": "Twitter API not configured. Please set TWITTER_BEARER_TOKEN in .env file."
            }
        )

    username = username.lstrip("@").strip()
    if not username:
        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "api_configured": True,
                "error": "Please enter a username"
            }
        )

    # Redirect to user-specific URL
    return RedirectResponse(url=f"/u/{username}?months={months}", status_code=303)


# Content Analysis Endpoints

def analysis_to_dict(analysis: TweetAnalysis) -> dict:
    """Convert TweetAnalysis to JSON-serializable dict."""
    return {
        "tweet_id": analysis.tweet_id,
        "text": analysis.text,
        "hook": analysis.hook,
        "hook_type": analysis.hook_type,
        "structure": analysis.structure,
        "has_list": analysis.has_list,
        "list_count": analysis.list_count,
        "has_question": analysis.has_question,
        "has_call_to_action": analysis.has_call_to_action,
        "char_count": analysis.char_count,
        "word_count": analysis.word_count,
        "line_count": analysis.line_count,
        "topics": analysis.topics,
        "likes": analysis.likes,
        "retweets": analysis.retweets,
        "engagement_score": analysis.engagement_score
    }


def patterns_to_dict(patterns: PatternInsights) -> dict:
    """Convert PatternInsights to JSON-serializable dict."""
    return {
        "total_tweets": patterns.total_tweets,
        "avg_length": patterns.avg_length,
        "most_common_structure": patterns.most_common_structure,
        "structure_breakdown": patterns.structure_breakdown,
        "top_topics": patterns.top_topics,
        "question_rate": patterns.question_rate,
        "list_rate": patterns.list_rate,
        "cta_rate": patterns.cta_rate,
        "most_common_hook_type": patterns.most_common_hook_type,
        "hook_type_breakdown": patterns.hook_type_breakdown,
        "best_performing_structure": patterns.best_performing_structure,
        "best_performing_hook_type": patterns.best_performing_hook_type
    }


@app.post("/api/analyze/tweet")
async def analyze_single_tweet(tweet_id: str, username: str):
    """Analyze a single tweet by ID."""
    username = username.lstrip("@").strip().lower()
    cached_data = cache.get_cached_data(username)

    if not cached_data:
        raise HTTPException(status_code=404, detail=f"No cached data for @{username}. Analyze user first.")

    tweet = next((t for t in cached_data["tweets"] if t.tweet_id == tweet_id), None)
    if not tweet:
        raise HTTPException(status_code=404, detail=f"Tweet {tweet_id} not found in cache")

    analysis = analyze_tweet(tweet)
    return {"analysis": analysis_to_dict(analysis)}


@app.get("/api/analyze/patterns/{username}")
async def get_pattern_insights(username: str, months: int = 1, top_n: int = 10):
    """Get pattern insights for a user's top tweets."""
    username = username.lstrip("@").strip().lower()
    cached_data = cache.get_cached_data(username)

    if not cached_data:
        raise HTTPException(status_code=404, detail=f"No cached data for @{username}. Analyze user first.")

    tweets = cached_data["tweets"]
    user = cached_data["user"]

    # Filter by months
    filtered = filter_tweets_by_months(tweets, months)
    if not filtered:
        raise HTTPException(status_code=404, detail=f"No tweets found for @{username} in the last {months} month(s)")

    # Get top tweets
    top_tweets = calculate_top_tweets(filtered.copy(), user.followers_count, top_n)

    # Analyze patterns
    patterns = analyze_patterns(top_tweets)
    if not patterns:
        raise HTTPException(status_code=404, detail="Could not analyze patterns")

    # Also return individual analyses
    analyses = [analyze_tweet(t) for t in top_tweets]

    return {
        "patterns": patterns_to_dict(patterns),
        "tweet_analyses": [analysis_to_dict(a) for a in analyses]
    }


class LinkedInRewriteRequest(BaseModel):
    tweet_text: str
    tweet_id: Optional[str] = None
    username: Optional[str] = None


@app.post("/api/rewrite/linkedin")
async def rewrite_tweet_for_linkedin(request: LinkedInRewriteRequest):
    """Rewrite a tweet as a LinkedIn post using AI."""
    if not settings.UNBOUND_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="UNBOUND_API_KEY not configured. Please add it to your .env file."
        )

    # Get analysis if we have the tweet in cache
    analysis = None
    if request.tweet_id and request.username:
        username = request.username.lstrip("@").strip().lower()
        cached_data = cache.get_cached_data(username)
        if cached_data:
            tweet = next((t for t in cached_data["tweets"] if t.tweet_id == request.tweet_id), None)
            if tweet:
                analysis = analyze_tweet(tweet)

    # Call the AI rewrite
    linkedin_post = await rewrite_for_linkedin(
        tweet_text=request.tweet_text,
        tweet_analysis=analysis,
        api_key=settings.UNBOUND_API_KEY
    )

    return {
        "original_tweet": request.tweet_text,
        "linkedin_post": linkedin_post,
        "analysis_used": analysis_to_dict(analysis) if analysis else None
    }


@app.get("/api/thread/{conversation_id}")
async def get_thread_tweets(conversation_id: str, username: str):
    """
    Fetch all tweets in a thread using conversation_id.
    Returns tweets in chronological order (oldest first).
    """
    if not check_api_configured():
        raise HTTPException(status_code=503, detail="Twitter API not configured")

    username = username.lstrip("@").strip().lower()

    try:
        client = TwitterClient()

        # Use search to find all tweets in this conversation by the same author
        # Note: This requires at least Basic API tier for search
        loop = asyncio.get_event_loop()

        def fetch_thread():
            try:
                # Search for tweets in this conversation from this user
                response = client.client.search_recent_tweets(
                    query=f"conversation_id:{conversation_id} from:{username}",
                    max_results=100,
                    tweet_fields=["created_at", "public_metrics", "conversation_id", "in_reply_to_user_id", "note_tweet"],
                    expansions=["referenced_tweets.id"]
                )
                return response
            except Exception as e:
                return None

        response = await loop.run_in_executor(executor, fetch_thread)

        if not response or not response.data:
            # Fall back: just return the thread starter from cache
            cached_data = cache.get_cached_data(username)
            if cached_data:
                thread_starter = next(
                    (t for t in cached_data["tweets"] if t.tweet_id == conversation_id),
                    None
                )
                if thread_starter:
                    return {
                        "thread_tweets": [tweet_to_dict(thread_starter)],
                        "total_in_thread": 1,
                        "note": "Thread search not available. Showing thread starter only."
                    }
            raise HTTPException(status_code=404, detail="Thread not found")

        # Convert to TweetMetrics and sort chronologically
        thread_tweets = []
        for tweet in response.data:
            metrics = tweet.public_metrics
            tweet_id_str = str(tweet.id)
            conv_id = str(tweet.conversation_id) if tweet.conversation_id else tweet_id_str

            # Get full text from note_tweet if available
            full_text = tweet.text
            if hasattr(tweet, 'note_tweet') and tweet.note_tweet:
                if hasattr(tweet.note_tweet, 'text'):
                    full_text = tweet.note_tweet.text
                elif isinstance(tweet.note_tweet, dict) and 'text' in tweet.note_tweet:
                    full_text = tweet.note_tweet['text']

            thread_tweets.append(TweetMetrics(
                tweet_id=tweet_id_str,
                text=full_text,
                created_at=tweet.created_at,
                likes=metrics["like_count"],
                retweets=metrics["retweet_count"],
                replies=metrics["reply_count"],
                quotes=metrics.get("quote_count", 0),
                bookmarks=metrics.get("bookmark_count", 0),
                url=f"https://x.com/{username}/status/{tweet.id}",
                conversation_id=conv_id,
                is_thread_starter=(tweet_id_str == conversation_id),
                thread_position=0 if tweet_id_str == conversation_id else 1
            ))

        # Sort by created_at (oldest first for thread reading)
        thread_tweets.sort(key=lambda t: t.created_at)

        # Assign thread positions
        for i, tweet in enumerate(thread_tweets):
            tweet.thread_position = i

        return {
            "thread_tweets": [tweet_to_dict(t) for t in thread_tweets],
            "total_in_thread": len(thread_tweets)
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching thread: {str(e)}")
