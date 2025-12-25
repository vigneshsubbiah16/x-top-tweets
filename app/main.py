from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from typing import Optional
import os

from app.twitter_client import TwitterClient, TweetMetrics, UserInfo
from app.config import settings

app = FastAPI(
    title="Twitter Top Tweets Analyzer",
    description="Find the top performing tweets for any Twitter/X user",
    version="1.0.0"
)

# Get the directory where this file is located
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


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


def check_api_configured() -> bool:
    """Check if Twitter API is configured."""
    return bool(settings.TWITTER_BEARER_TOKEN)


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


@app.post("/api/analyze", response_model=AnalyzeResponse)
async def analyze_user(request: AnalyzeRequest):
    """
    Analyze a Twitter user's tweets and return top performers.

    - **username**: Twitter handle (without @)
    - **months**: Time period to analyze (1 or 3)
    - **top_n**: Number of top tweets to return (default: 10)
    """
    if not check_api_configured():
        raise HTTPException(
            status_code=503,
            detail="Twitter API not configured. Please set TWITTER_BEARER_TOKEN."
        )

    # Validate inputs
    username = request.username.lstrip("@").strip()
    if not username:
        raise HTTPException(status_code=400, detail="Username is required")

    if request.months not in [1, 3]:
        raise HTTPException(status_code=400, detail="Months must be 1 or 3")

    if request.top_n < 1 or request.top_n > 50:
        raise HTTPException(status_code=400, detail="top_n must be between 1 and 50")

    try:
        client = TwitterClient()
        user, tweets = client.get_top_tweets(
            username=username,
            months=request.months,
            top_n=request.top_n
        )

        # Convert to response models
        user_response = UserResponse(
            id=user.id,
            username=user.username,
            name=user.name,
            followers_count=user.followers_count,
            following_count=user.following_count,
            tweet_count=user.tweet_count,
            profile_image_url=user.profile_image_url,
            description=user.description
        )

        tweet_responses = [
            TweetResponse(
                tweet_id=t.tweet_id,
                text=t.text,
                created_at=t.created_at.isoformat(),
                likes=t.likes,
                retweets=t.retweets,
                replies=t.replies,
                quotes=t.quotes,
                bookmarks=t.bookmarks,
                url=t.url,
                engagement_score=round(t.engagement_score, 2),
                engagement_rate=round(t.engagement_rate, 4)
            )
            for t in tweets
        ]

        return AnalyzeResponse(
            user=user_response,
            tweets=tweet_responses,
            total_tweets_analyzed=len(tweets),
            time_period_months=request.months
        )

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"An error occurred: {str(e)}")


@app.post("/analyze", response_class=HTMLResponse)
async def analyze_form(
    request: Request,
    username: str = Form(...),
    months: int = Form(1)
):
    """Handle form submission and render results."""
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

    try:
        client = TwitterClient()
        user, tweets = client.get_top_tweets(
            username=username,
            months=months,
            top_n=10
        )

        return templates.TemplateResponse(
            "results.html",
            {
                "request": request,
                "user": user,
                "tweets": tweets,
                "months": months,
                "api_configured": True
            }
        )

    except ValueError as e:
        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "api_configured": True,
                "error": str(e),
                "username": username
            }
        )
    except Exception as e:
        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "api_configured": True,
                "error": f"An error occurred: {str(e)}",
                "username": username
            }
        )
