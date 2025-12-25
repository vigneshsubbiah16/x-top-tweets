"""
Content analysis and LinkedIn rewrite features.
"""
import re
import httpx
from typing import Optional
from dataclasses import dataclass, field
from collections import Counter

from app.twitter_client import TweetMetrics
from app.config import settings


@dataclass
class TweetAnalysis:
    """Analysis of a single tweet."""
    tweet_id: str
    text: str

    # Hook analysis
    hook: str  # First sentence/line
    hook_type: str  # question, statement, number, story, hot_take

    # Structure
    structure: str  # single, list, thread_starter, story, question
    has_list: bool
    list_count: int
    has_question: bool
    has_call_to_action: bool

    # Metrics
    char_count: int
    word_count: int
    line_count: int

    # Topics (basic keyword extraction)
    topics: list[str] = field(default_factory=list)

    # Engagement context
    likes: int = 0
    retweets: int = 0
    engagement_score: float = 0.0


@dataclass
class PatternInsights:
    """Patterns across multiple top tweets."""
    total_tweets: int

    # Format patterns
    avg_length: float
    most_common_structure: str
    structure_breakdown: dict[str, int]

    # Content patterns
    top_topics: list[tuple[str, int]]
    question_rate: float  # % of tweets with questions
    list_rate: float  # % of tweets with lists
    cta_rate: float  # % with call to action

    # Hook patterns
    most_common_hook_type: str
    hook_type_breakdown: dict[str, int]

    # Best performers
    best_performing_structure: str
    best_performing_hook_type: str


# Topic keywords to look for
TOPIC_KEYWORDS = {
    "ai": ["ai", "artificial intelligence", "gpt", "llm", "machine learning", "ml", "chatgpt", "claude", "openai"],
    "startups": ["startup", "founder", "vc", "venture", "fundraise", "seed", "series a", "bootstrap", "yc", "y combinator"],
    "product": ["product", "ship", "launch", "feature", "user", "customer", "feedback", "mvp", "beta"],
    "career": ["career", "job", "hire", "hiring", "interview", "resume", "promotion", "salary"],
    "productivity": ["productivity", "habit", "routine", "morning", "focus", "deep work", "time management"],
    "leadership": ["leader", "leadership", "team", "manage", "ceo", "executive", "culture"],
    "money": ["money", "revenue", "profit", "income", "mrr", "arr", "pricing", "business model"],
    "growth": ["growth", "scale", "grow", "marketing", "viral", "acquisition", "retention"],
    "personal": ["i learned", "my story", "i realized", "years ago", "when i was", "my journey"],
    "tech": ["code", "programming", "developer", "software", "engineering", "api", "database", "deploy"],
}


def analyze_hook(text: str) -> tuple[str, str]:
    """Extract and classify the hook (first line/sentence)."""
    # Get first line or sentence
    lines = text.strip().split('\n')
    first_line = lines[0].strip()

    # If first line is very short, might be part of a pattern
    if len(first_line) < 20 and len(lines) > 1:
        hook = first_line
    else:
        # Try to get first sentence
        sentences = re.split(r'[.!?]', first_line)
        hook = sentences[0].strip() if sentences else first_line

    # Classify hook type
    hook_lower = hook.lower()

    if hook.endswith('?') or '?' in hook:
        hook_type = "question"
    elif re.match(r'^\d+', hook) or re.search(r'\b\d+\s+(things|ways|tips|reasons|lessons)', hook_lower):
        hook_type = "number_list"
    elif any(word in hook_lower for word in ["unpopular opinion", "hot take", "controversial", "nobody talks about"]):
        hook_type = "hot_take"
    elif any(word in hook_lower for word in ["i learned", "i realized", "years ago", "my story", "when i"]):
        hook_type = "story"
    elif any(word in hook_lower for word in ["stop", "don't", "never", "always", "you need", "you should"]):
        hook_type = "directive"
    else:
        hook_type = "statement"

    return hook, hook_type


def analyze_structure(text: str) -> tuple[str, bool, int]:
    """Analyze the structure of the tweet."""
    lines = [l.strip() for l in text.split('\n') if l.strip()]

    # Check for list patterns
    list_patterns = [
        r'^[\d]+[.\)]\s',  # 1. or 1)
        r'^[-•]\s',  # bullet points
        r'^[→•▸]\s',  # arrows
    ]

    list_items = 0
    for line in lines:
        for pattern in list_patterns:
            if re.match(pattern, line):
                list_items += 1
                break

    has_list = list_items >= 2

    # Determine structure type
    if has_list:
        structure = "list"
    elif len(lines) == 1:
        structure = "single"
    elif text.endswith('🧵') or 'thread' in text.lower():
        structure = "thread_starter"
    elif '?' in text and len(lines) <= 3:
        structure = "question"
    elif any(word in text.lower() for word in ["i learned", "my story", "years ago"]):
        structure = "story"
    else:
        structure = "multi_line"

    return structure, has_list, list_items


def extract_topics(text: str) -> list[str]:
    """Extract topics from tweet text."""
    text_lower = text.lower()
    found_topics = []

    for topic, keywords in TOPIC_KEYWORDS.items():
        if any(kw in text_lower for kw in keywords):
            found_topics.append(topic)

    return found_topics


def analyze_tweet(tweet: TweetMetrics) -> TweetAnalysis:
    """Perform full analysis on a single tweet."""
    text = tweet.text

    # Hook analysis
    hook, hook_type = analyze_hook(text)

    # Structure analysis
    structure, has_list, list_count = analyze_structure(text)

    # Basic metrics
    char_count = len(text)
    word_count = len(text.split())
    line_count = len([l for l in text.split('\n') if l.strip()])

    # Content analysis
    has_question = '?' in text
    has_cta = any(cta in text.lower() for cta in [
        "follow", "subscribe", "check out", "link in bio", "dm me",
        "comment", "share", "retweet", "let me know", "what do you think"
    ])

    # Topics
    topics = extract_topics(text)

    return TweetAnalysis(
        tweet_id=tweet.tweet_id,
        text=text,
        hook=hook,
        hook_type=hook_type,
        structure=structure,
        has_list=has_list,
        list_count=list_count,
        has_question=has_question,
        has_call_to_action=has_cta,
        char_count=char_count,
        word_count=word_count,
        line_count=line_count,
        topics=topics,
        likes=tweet.likes,
        retweets=tweet.retweets,
        engagement_score=tweet.engagement_score
    )


def analyze_patterns(tweets: list[TweetMetrics]) -> PatternInsights:
    """Analyze patterns across multiple tweets."""
    if not tweets:
        return None

    analyses = [analyze_tweet(t) for t in tweets]

    # Structure breakdown
    structures = Counter(a.structure for a in analyses)
    hook_types = Counter(a.hook_type for a in analyses)

    # Topic frequency
    all_topics = []
    for a in analyses:
        all_topics.extend(a.topics)
    topic_counts = Counter(all_topics).most_common(5)

    # Rates
    question_rate = sum(1 for a in analyses if a.has_question) / len(analyses) * 100
    list_rate = sum(1 for a in analyses if a.has_list) / len(analyses) * 100
    cta_rate = sum(1 for a in analyses if a.has_call_to_action) / len(analyses) * 100

    # Average length
    avg_length = sum(a.char_count for a in analyses) / len(analyses)

    # Best performing patterns (by average engagement)
    structure_engagement = {}
    for a in analyses:
        if a.structure not in structure_engagement:
            structure_engagement[a.structure] = []
        structure_engagement[a.structure].append(a.engagement_score)

    best_structure = max(structure_engagement.keys(),
                         key=lambda s: sum(structure_engagement[s]) / len(structure_engagement[s]))

    hook_engagement = {}
    for a in analyses:
        if a.hook_type not in hook_engagement:
            hook_engagement[a.hook_type] = []
        hook_engagement[a.hook_type].append(a.engagement_score)

    best_hook = max(hook_engagement.keys(),
                    key=lambda h: sum(hook_engagement[h]) / len(hook_engagement[h]))

    return PatternInsights(
        total_tweets=len(analyses),
        avg_length=round(avg_length, 0),
        most_common_structure=structures.most_common(1)[0][0],
        structure_breakdown=dict(structures),
        top_topics=topic_counts,
        question_rate=round(question_rate, 1),
        list_rate=round(list_rate, 1),
        cta_rate=round(cta_rate, 1),
        most_common_hook_type=hook_types.most_common(1)[0][0],
        hook_type_breakdown=dict(hook_types),
        best_performing_structure=best_structure,
        best_performing_hook_type=best_hook
    )


async def rewrite_for_linkedin(
    tweet_text: str,
    tweet_analysis: Optional[TweetAnalysis] = None,
    api_key: Optional[str] = None
) -> str:
    """
    Rewrite a tweet as a LinkedIn post using Unbound API.
    """
    api_key = api_key or settings.UNBOUND_API_KEY
    if not api_key:
        return "Error: UNBOUND_API_KEY not configured in .env"

    # Build context from analysis
    context = ""
    if tweet_analysis:
        context = f"""
Original tweet analysis:
- Hook type: {tweet_analysis.hook_type}
- Structure: {tweet_analysis.structure}
- Topics: {', '.join(tweet_analysis.topics) if tweet_analysis.topics else 'general'}
- Engagement: {tweet_analysis.likes} likes, {tweet_analysis.retweets} retweets
"""

    prompt = f"""Rewrite this viral tweet as a LinkedIn post.

Original tweet:
"{tweet_text}"
{context}

Guidelines:
1. Keep the core insight/message intact
2. Expand with more context and professional framing
3. Use line breaks for readability (LinkedIn loves whitespace)
4. Start with a compelling hook
5. End with a question or call-to-action to drive engagement
6. Keep it authentic, not corporate-speak
7. Aim for 150-300 words

Write only the LinkedIn post, no explanations:"""

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api.getunbound.ai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": "gpt-4o-mini",
                    "messages": [
                        {"role": "user", "content": prompt}
                    ],
                    "max_tokens": 1000,
                    "temperature": 0.7
                },
                timeout=30.0
            )

            if response.status_code != 200:
                return f"Error: API returned {response.status_code} - {response.text}"

            data = response.json()
            return data["choices"][0]["message"]["content"]

    except Exception as e:
        return f"Error calling Unbound API: {str(e)}"
