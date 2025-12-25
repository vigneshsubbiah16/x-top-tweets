# Twitter Top Tweets Analyzer

A web app that finds the top 10 performing tweets for any Twitter/X user based on engagement metrics.

## Features

- Analyze any public Twitter/X account
- Filter by time period (last 1 or 3 months)
- Engagement scoring formula:
  - `Score = Likes + (Retweets × 2) + Replies + Quotes + Bookmarks`
- Ranking combines absolute engagement (70%) and engagement rate (30%)
- Clean, responsive UI

## Setup

### 1. Get Twitter API Access

1. Go to [Twitter Developer Portal](https://developer.twitter.com/en/portal/dashboard)
2. Create a new project and app
3. Generate a **Bearer Token** (under Keys and Tokens)

### 2. Install Dependencies

```bash
cd twitter-analyzer
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Configure Environment

```bash
cp .env.example .env
# Edit .env and add your TWITTER_BEARER_TOKEN
```

### 4. Run the App

```bash
uvicorn app.main:app --reload
```

Visit [http://localhost:8000](http://localhost:8000)

## API Endpoints

### `POST /api/analyze`

Analyze a user's tweets programmatically.

**Request:**
```json
{
  "username": "elonmusk",
  "months": 1,
  "top_n": 10
}
```

**Response:**
```json
{
  "user": {
    "id": "...",
    "username": "elonmusk",
    "name": "Elon Musk",
    "followers_count": 100000000,
    ...
  },
  "tweets": [
    {
      "tweet_id": "...",
      "text": "...",
      "likes": 50000,
      "retweets": 10000,
      "engagement_score": 70000,
      "engagement_rate": 0.07,
      "url": "https://x.com/i/status/..."
    },
    ...
  ],
  "total_tweets_analyzed": 10,
  "time_period_months": 1
}
```

## Limitations

- **Rate Limits**: Twitter API has rate limits. The Basic tier allows ~15 requests per 15 minutes for user timeline
- **No Impressions**: Twitter doesn't expose impression data for other users' tweets, so we use engagement metrics as a proxy
- **Public Tweets Only**: Can only analyze public accounts

## Tech Stack

- **Backend**: FastAPI + Python
- **Twitter API**: Tweepy
- **Frontend**: Jinja2 templates + Tailwind CSS
