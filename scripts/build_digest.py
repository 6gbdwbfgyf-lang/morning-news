"""毎朝のニュースダイジェストを作るスクリプト。

1. 日本のニュースRSSから直近の記事を集める
2. Claude に「重要なニュース約10件」を選ばせて日本語で要約させる
3. docs/data/ に JSON として保存する(GitHub Pages がそのまま表示する)

要約に使うAIは、環境変数で自動的に切り替わる:
- ANTHROPIC_API_KEY があれば Claude(有料・高品質)
- なければ GEMINI_API_KEY で Google Gemini の無料枠
- どちらも無い・失敗したときは AIなし(記事の概要文をそのまま載せる)

使い方:
    python scripts/build_digest.py                       # AIなし
    GEMINI_API_KEY=... python scripts/build_digest.py    # 無料AI要約
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic
import feedparser

JST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "docs" / "data"

MODEL = "claude-opus-5-5"
TARGET_COUNT = 10
LOOKBACK_HOURS = 30  # 前日朝〜当日朝までをカバー

# ニュースソース(RSS)。追加・削除はここを編集するだけでOK。
FEEDS: list[tuple[str, str]] = [
    ("主要", "https://news.yahoo.co.jp/rss/topics/top-picks.xml"),
    ("国内", "https://news.yahoo.co.jp/rss/topics/domestic.xml"),
    ("国際", "https://news.yahoo.co.jp/rss/topics/world.xml"),
    ("経済", "https://news.yahoo.co.jp/rss/topics/business.xml"),
    ("IT", "https://news.yahoo.co.jp/rss/topics/it.xml"),
    ("科学", "https://news.yahoo.co.jp/rss/topics/science.xml"),
    ("スポーツ", "https://news.yahoo.co.jp/rss/topics/sports.xml"),
    ("エンタメ", "https://news.yahoo.co.jp/rss/topics/entertainment.xml"),
    ("地域", "https://news.yahoo.co.jp/rss/topics/local.xml"),
]
# ※ NHKの旧RSS(www3.nhk.or.jp/rss/…)は2026年8月で更新が止まっているため使わない

CATEGORIES = ["政治", "経済", "社会", "国際", "科学・IT", "スポーツ", "文化・エンタメ", "天気・災害", "その他"]

DIGEST_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {
            "type": "string",
            "description": "今朝のニュース全体を一言でまとめた見出し(30字程度)",
        },
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "分かりやすく言い換えた見出し"},
                    "summary": {"type": "string", "description": "2〜3文の要約"},
                    "why_it_matters": {"type": "string", "description": "なぜ重要か・生活への影響を1文で"},
                    "category": {"type": "string", "enum": CATEGORIES},
                    "source_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "根拠にした記事の id(1つ以上)",
                    },
                    "terms": {
                        "type": "array",
                        "description": "見出し・要約に出てくる、知らない人がつまずきそうな言葉(0〜3個)",
                        "items": {
                            "type": "object",
                            "properties": {
                                "term": {"type": "string", "description": "本文中の表記そのまま"},
                                "explanation": {"type": "string", "description": "中学生にも分かる1〜2文の説明"},
                            },
                            "required": ["term", "explanation"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["title", "summary", "why_it_matters", "category", "source_ids", "terms"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["headline", "items"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = f"""あなたは日本の朝のニュース番組の編集者です。
渡される記事一覧(RSSの見出しと概要)から、前日から今朝にかけての重要なニュースを{TARGET_COUNT}件前後選び、
朝の通勤時間に読める日本語の要約を作ってください。

選び方:
- 社会的な影響が大きいもの、多くの人の生活に関わるものを優先する
- 同じ出来事を扱う記事は1件にまとめ、source_ids に該当する記事の id をすべて入れる
- 政治・経済・国際・社会・科学・スポーツなど、ジャンルが偏りすぎないようにする
- 重要度の高い順に並べる

書き方:
- 記事に書かれている事実だけを使い、推測で補わない
- 要約は2〜3文、平易な言葉で
- 見出しは略語や業界用語を避け、「誰が・何をした」が一読で分かる20〜35字の文にする
"""


def collect_articles(now: datetime) -> list[dict]:
    """RSSを読み、直近 LOOKBACK_HOURS 時間の記事を重複なしで返す。"""
    cutoff = now - timedelta(hours=LOOKBACK_HOURS)
    seen: set[str] = set()
    articles: list[dict] = []
    for source, url in FEEDS:
        feed = feedparser.parse(url, agent="morning-news-digest/1.0")
        status = getattr(feed, "status", "?")
        dates = [e.get("published_parsed") or e.get("updated_parsed") for e in feed.entries]
        dates = [d for d in dates if d]
        newest = datetime(*max(dates)[:6]).strftime("%Y-%m-%d %H:%M") if dates else "日付なし"
        print(f"[feed] {source}: HTTP {status}, {len(feed.entries)}件, 最新 {newest} UTC", file=sys.stderr)
        if not feed.entries:
            if feed.bozo:
                print(f"[warn] {source} を読めませんでした: {feed.bozo_exception}", file=sys.stderr)
            continue
        for e in feed.entries:
            link = e.get("link", "")
            title = _clean(e.get("title") or "")
            key = link or title
            if not title or key in seen:
                continue
            published = None
            for attr in ("published_parsed", "updated_parsed"):
                if e.get(attr):
                    published = datetime(*e[attr][:6], tzinfo=timezone.utc)
                    break
            if published and published < cutoff:
                continue
            seen.add(key)
            articles.append(
                {
                    "id": len(articles) + 1,
                    "source": source,
                    "title": title,
                    "description": _clean(e.get("summary") or "")[:300],
                    "link": link,
                    "published": published.astimezone(JST).strftime("%m/%d %H:%M") if published else "",
                }
            )
    return articles


def summarize(articles: list[dict], now: datetime) -> dict:
    client = anthropic.Anthropic()  # ANTHROPIC_API_KEY を環境変数から読む
    lines = [
        f"[{a['id']}] ({a['source']} {a['published']}) {a['title']}"
        + (f" — {a['description']}" if a["description"] else "")
        for a in articles
    ]
    user = f"今日は {now:%Y年%m月%d日} です。記事一覧:\n\n" + "\n".join(lines)

    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user}],
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": DIGEST_SCHEMA}},
        # 安全フィルタで断られた場合はサーバー側で別モデルに自動で切り替える
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal":
        raise RuntimeError(f"Claude が要約を断りました: {response.stop_details}")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("出力が長すぎて途中で切れました")
    text = next(b.text for b in response.content if b.type == "text")
    digest = json.loads(text)
    for it in digest["items"]:
        it["terms"] = _check_terms(it.get("terms"), f"{it['title']} {it['summary']} {it['why_it_matters']}")
    return digest


FEED_CATEGORY = {
    "主要": "主要", "国内": "社会", "国際": "国際", "経済": "経済", "IT": "科学・IT",
    "科学": "科学・IT", "スポーツ": "スポーツ", "エンタメ": "文化・エンタメ", "地域": "社会",
}
# 記事を選ぶ順番(編集部が選んだ「主要」を優先し、その後ジャンルを順番に回る)
FREE_ORDER = ["主要", "国内", "経済", "国際", "IT", "科学", "スポーツ", "エンタメ", "地域"]


def _bigrams(s: str) -> set[str]:
    s = "".join(s.split())
    return {s[i : i + 2] for i in range(len(s) - 1)}


def _similar(a: str, b: str) -> bool:
    x, y = _bigrams(a), _bigrams(b)
    return bool(x and y) and len(x & y) / min(len(x), len(y)) > 0.5


def pick_candidates(articles: list[dict], count: int) -> list[dict]:
    """各ソースを順番に回り、似た見出しを除いて count 件の記事を選ぶ(AIを使わない)。"""
    queues = {name: [a for a in articles if a["source"] == name] for name in FREE_ORDER}
    picked: list[dict] = []
    while len(picked) < count and any(queues.values()):
        for name in FREE_ORDER:
            q = queues[name]
            # 主要ニュースは1周で2件ずつ取る
            for _ in range(2 if "主要" in name else 1):
                while q:
                    a = q.pop(0)
                    if not any(_similar(a["title"], p["title"]) for p in picked):
                        picked.append(a)
                        break
            if len(picked) >= count:
                break
    return picked


_INVISIBLE = re.compile("[\u200b-\u200f\u2060-\u2064\ufeff\u00ad]")


def _clean(text: str) -> str:
    """記事に紛れ込んでいる見えない文字(ゼロ幅スペースなど)を取り除く。"""
    return _INVISIBLE.sub("", text).strip()


def _sentences(text: str, n: int) -> str:
    """n文字以内で、文の切れ目(。)までにそろえる。途中で切れた文は落とす。"""
    text = re.sub(r"^[［\[【][^］\]】]{1,30}[］\]】]\s*-?\s*", "", text)  # 先頭の【ワシントン共同】など
    if len(text) <= n and text.endswith("。"):
        return text
    cut = text[:n]
    end = cut.rfind("。")
    if end >= 20:
        return cut[: end + 1]
    out = _short(text, n)
    return out if out.endswith(("。", "…")) else out + "…"


def _short(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n] + "…"


def pick_without_ai(articles: list[dict]) -> dict:
    """完全無料モード: ルールで約10件選び、RSSの概要文をそのまま使う。"""
    items = [
        {
            "title": a.get("full_title") or a["title"],
            "summary": _sentences(a["description"], 170),
            "why_it_matters": "",
            "category": FEED_CATEGORY.get(a["source"], "その他"),
            "source_ids": [a["id"]],
        }
        for a in pick_candidates(articles, TARGET_COUNT)
    ]
    return {"headline": "", "items": items}


UA = {"User-Agent": "Mozilla/5.0 (compatible; morning-news-digest/1.0)"}


def _get(url: str, limit: int = 400_000) -> str:
    import urllib.request

    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=15) as res:
        return res.read(limit).decode("utf-8", "replace")


def _meta(page: str, *names: str) -> str:
    import html

    for name in names:
        for pat in (
            rf'<meta[^>]+(?:property|name)="{name}"[^>]+content="([^"]*)"',
            rf'<meta[^>]+content="([^"]*)"[^>]+(?:property|name)="{name}"',
        ):
            m = re.search(pat, page)
            if m and m.group(1).strip():
                return _clean(html.unescape(m.group(1)))
    return ""


def _tidy_title(title: str, limit: int = 40) -> str:
    """「〇〇　△△　□□(毎日新聞) - Yahoo!ニュース」→「〇〇　△△」(長すぎる見出しは区切りで短く)"""
    title = re.sub(r"\s*[-|｜]\s*Yahoo!ニュース\s*$", "", title)
    title = re.sub(r"[（(][^（）()]{1,20}[）)]\s*$", "", title).strip()
    if len(title) <= limit:
        return title
    out = ""
    for part in re.split(r"[　 ]+", title):
        if out and len(out) + 1 + len(part) > limit:
            break
        out = f"{out}　{part}" if out else part
    return out if len(out) >= 10 else title[:limit] + "…"


def _enrich_one(a: dict) -> None:
    """Yahoo!のトピックスページ → 元記事ページ をたどり、正式な見出しと冒頭の文章を取る。"""
    page = _get(a["link"])
    if not a["description"]:
        a["description"] = _meta(page, "og:description", "description")
    m = re.search(r'https://news\.yahoo\.co\.jp/articles/[0-9a-zA-Z]+', page)
    if not m:
        return
    a["article_url"] = m.group(0)
    article = _get(m.group(0))
    full = _tidy_title(_meta(article, "og:title"))
    if full and len(full) > len(a["title"]):
        a["full_title"] = full
    desc = _meta(article, "og:description", "description")
    if len(desc) > len(a["description"]):
        a["description"] = desc


def enrich_descriptions(articles: list[dict]) -> None:
    """記事ページから正式な見出しと概要文を補う(並列で取得)。"""
    from concurrent.futures import ThreadPoolExecutor

    targets = [a for a in articles if a["link"]]

    def work(a: dict) -> None:
        try:
            _enrich_one(a)
        except Exception as e:
            print(f"[warn] 記事ページの取得に失敗: {a['title']} ({e})", file=sys.stderr)

    with ThreadPoolExecutor(max_workers=6) as ex:
        list(ex.map(work, targets))
    print(
        f"[enrich] {len(targets)}件中 概要 {sum(1 for a in targets if a['description'])}件 /"
        f" 正式な見出し {sum(1 for a in targets if a.get('full_title'))}件",
        file=sys.stderr,
    )
    for a in targets[:3]:
        print(f"[enrich] 例: {a['title']} → {a.get('full_title', '(見出しなし)')}", file=sys.stderr)


# ---- 用語解説(タップで表示) ---------------------------------------------------
# AIなしのときは、記事中の固有名詞や略語らしき言葉を拾い、Wikipedia の冒頭の説明を付ける。
COMMON_WORDS = set(
    "ニュース インタビュー コメント サービス システム データ ネット スマートフォン スマホ クレジットカード "
    "デビットカード カード チーム メンバー グループ ファン ボーカル プロ トップ ポイント ケース レベル "
    "イベント プラン センター ライブ テレビ ドラマ アニメ ゲーム メディア ビジネス マーケット リスク "
    "コスト ルール スタート シーズン ゴール ホーム オンライン アプリ ユーザー サイト ページ".split()
)
TERM_PATTERNS = [
    r"[一-龥ァ-ヴー]{1,8}(?:海峡|条約|会議|機構|協定|委員会|同盟|連合|銀行|法案|制度|政策)",
    r"[A-Z][A-Z0-9&]{1,9}",                         # COP31, CEO, NATO など
    r"[ァ-ヴー]{2,}(?:・[ァ-ヴー]{2,})*",               # エヌビディア、ジェンスン・フアン など
]


def _term_candidates(text: str) -> list[str]:
    import unicodedata

    text = unicodedata.normalize("NFKC", text)
    seen: list[str] = []
    for pat in TERM_PATTERNS:
        for m in re.finditer(pat, text):
            t = m.group(0)
            # 「トランプ氏」「ドジャースの佐々木選手」のような人名は同名の別物を引きやすいので除く
            if re.match(r"(?:氏|さん|米|大統領|首相|選手|監督|容疑者|被告)", text[m.end() : m.end() + 3]):
                continue
            if t in COMMON_WORDS or len(t) < 2 or any(t in x for x in seen):
                continue
            seen.append(t)
    return seen


def _find_original(text: str, normalized_term: str) -> str:
    """正規化(全角→半角など)した言葉が、元の文章ではどう書かれているかを返す。"""
    import unicodedata

    norm, starts = "", []
    for i, ch in enumerate(text):
        n = unicodedata.normalize("NFKC", ch)
        starts.extend([i] * len(n))
        norm += n
    starts.append(len(text))
    k = norm.find(normalized_term)
    if k < 0:
        return ""
    return text[starts[k] : starts[k + len(normalized_term) - 1] + 1]


def _wikipedia_summary(term: str, cache: dict) -> dict | None:
    import urllib.parse

    if term in cache:
        return cache[term]
    result = None
    try:
        url = "https://ja.wikipedia.org/api/rest_v1/page/summary/" + urllib.parse.quote(term) + "?redirect=true"
        data = json.loads(_get(url, 200_000))
        extract = _clean(data.get("extract") or "")
        # 「〇〇（読み方、英語名など）は」の括弧部分を省いて読みやすくする
        extract = re.sub(r"^([^（(]{1,40}?)\s*[（(].*?[）)]\s*(とは|は)", r"\1\2", extract, count=1)
        # 括弧が閉じずに残った場合(「〇〇（よみがな、とは…」)も省く
        extract = re.sub(r"^([^（(]{1,40}?)\s*[（(][^）)]*?、?\s*(とは|は、)", r"\1\2", extract, count=1)
        extract = re.sub(r"\s+(は|とは)", r"\1", extract, count=1)
        if data.get("type") == "standard" and extract:
            first = re.split(r"(?<=。)", extract)
            text = "".join(first[:2]) if len(first[0]) < 60 else first[0]
            result = {
                "term": term,
                "explanation": _short(text, 160),
                "url": (data.get("content_urls") or {}).get("mobile", {}).get("page", ""),
            }
    except Exception:
        pass
    cache[term] = result
    return result


def add_glossary_from_wikipedia(items: list[dict], max_per_item: int = 3, max_lookups: int = 45) -> None:
    import unicodedata

    cache: dict = {}
    for item in items:
        if item.get("terms"):
            continue
        text = item["title"] + " " + item["summary"]
        terms = []
        for cand in _term_candidates(text):
            if len(cache) >= max_lookups or len(terms) >= max_per_item:
                break
            info = _wikipedia_summary(cand, cache)
            if info:
                # 画面では元の表記(全角など)で探すので、本文中の実際の表記に合わせる
                original = _find_original(text, cand)
                if original:
                    terms.append({**info, "term": original})
        item["terms"] = terms
    found = sum(len(i["terms"]) for i in items)
    print(f"[glossary] Wikipedia で {len(cache)}語を調べ、{found}語に解説を付けました", file=sys.stderr)


def _check_terms(terms, text: str) -> list[dict]:
    """AIが返した用語のうち、本文に実際に出てくるものだけ残す。"""
    out = []
    for t in terms or []:
        term = str(t.get("term") or "").strip()
        exp = str(t.get("explanation") or "").strip()
        if term and exp and term in text:
            out.append({"term": term, "explanation": exp,
                        "url": "https://ja.wikipedia.org/w/index.php?search=" + term})
    return out[:4]


# ---- 無料AI要約: Google Gemini API の無料枠 ------------------------------------
# GEMINI_API_KEY(Google AI Studio で無料発行)があれば使う。
# 無料枠のモデルは入れ替わることがあるので、順番に試して使えたものを使う。
GEMINI_MODELS = [m for m in os.environ.get("GEMINI_MODEL", "").split(",") if m] or [
    "gemini-3.8-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemma-4-31b-it",
]
CANDIDATE_COUNT = 30

FREE_AI_PROMPT = f"""あなたは日本の朝のニュース番組の編集者です。
候補記事から、前日から今朝にかけての重要なニュースを{TARGET_COUNT}件選び、重要度の高い順に並べてください。
同じ出来事は1件にまとめ、ジャンルが偏りすぎないようにします。記事に書かれた事実だけを使い、推測で補わないでください。

次の形のJSONだけを出力してください:
{{"headline": "今朝のニュース全体を一言で(30字程度)",
  "items": [{{"id": 候補の番号, "title": "分かりやすい見出し", "summary": "2〜3文の要約",
             "why_it_matters": "なぜ重要か・生活への影響を1文で",
             "category": "{'/'.join(CATEGORIES)} のどれか",
             "terms": [{{"term": "見出しか要約に出てくる難しい言葉(そのままの表記)",
                         "explanation": "中学生にも分かる1〜2文の説明(どんな組織・人・制度か)"}}]}}]}}

見出しのルール: 略語や業界用語を避け、「誰が・何をした」が一読で分かる20〜35字の文にする。
terms のルール: 組織名・人名・略語・専門用語など、知らない人がつまずきそうな言葉を各記事0〜3個。"""


def _call_gemini(model: str, prompt: str) -> str:
    import urllib.error
    import urllib.request

    body = {
        "systemInstruction": {"parts": [{"text": FREE_AI_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.3},
    }
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        data=json.dumps(body).encode(),
        headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"], "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as res:
            data = json.load(res)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}") from e
    try:
        return "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"想定外の応答: {json.dumps(data, ensure_ascii=False)[:300]}") from e


def summarize_with_gemini(candidates: list[dict], now: datetime) -> dict:
    lines = [
        f"[{a['id']}] ({a['source']}) {a.get('full_title') or a['title']} — {_short(a['description'], 150)}"
        for a in candidates
    ]
    prompt = f"今日は {now:%Y年%m月%d日} です。候補記事:\n" + "\n".join(lines)
    import time

    errors, content = [], None
    # 無料枠は混雑で一時的に断られる(503)ことがあるので、間をあけて最大3周試す
    for attempt, wait in enumerate((0, 30, 90)):
        time.sleep(wait)
        for model in GEMINI_MODELS:
            try:
                content = _call_gemini(model, prompt)
                print(f"[ai] {model} で要約しました({attempt + 1}周目)", file=sys.stderr)
                break
            except Exception as e:
                errors.append(f"{model}: {str(e)[:120]}")
        if content is not None:
            break
    if content is None:
        raise RuntimeError(" / ".join(errors))
    # ```json ... ``` で囲まれて返ってくる場合にも対応
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end < 0:
        raise RuntimeError(f"JSONが含まれていません: {content[:300]}")
    out = json.loads(content[start : end + 1])

    valid_ids = {a["id"] for a in candidates}
    items = []
    for it in out.get("items", []):
        if it.get("id") not in valid_ids or not it.get("summary"):
            continue
        items.append(
            {
                "title": str(it.get("title") or ""),
                "summary": str(it["summary"]),
                "why_it_matters": str(it.get("why_it_matters") or ""),
                "category": it.get("category") if it.get("category") in CATEGORIES else "その他",
                "source_ids": [it["id"]],
                "terms": _check_terms(
                    it.get("terms"), f"{it.get('title')} {it['summary']} {it.get('why_it_matters')}"
                ),
            }
        )
    if len(items) < 3:
        raise RuntimeError(f"AIの出力が不十分でした({len(items)}件)")
    return {"headline": str(out.get("headline") or ""), "items": items[: TARGET_COUNT + 2]}


def attach_sources(digest: dict, articles: list[dict]) -> list[dict]:
    by_id = {a["id"]: a for a in articles}
    items = []
    for item in digest["items"]:
        sources = [
            {
                "name": by_id[i]["source"],
                "title": by_id[i].get("full_title") or by_id[i]["title"],
                "url": by_id[i].get("article_url") or by_id[i]["link"],
            }
            for i in item.pop("source_ids")
            if i in by_id
        ]
        items.append({**item, "sources": sources})
    return items


def save(result: dict, date_str: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    body = json.dumps(result, ensure_ascii=False, indent=2)
    (DATA_DIR / f"{date_str}.json").write_text(body, encoding="utf-8")
    (DATA_DIR / "latest.json").write_text(body, encoding="utf-8")

    index_path = DATA_DIR / "index.json"
    dates = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else []
    if date_str not in dates:
        dates.append(date_str)
    dates.sort(reverse=True)
    index_path.write_text(json.dumps(dates, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Claude を呼ばずに記事一覧だけ表示")
    parser.add_argument("--articles", type=Path, help="RSSの代わりに記事JSONファイルを使う(テスト用)")
    parser.add_argument("--free", action="store_true", help="Claude APIキーがあっても使わない")
    parser.add_argument("--no-ai", action="store_true", help="AIを一切使わない")
    parser.add_argument("--retry-if-free", action="store_true",
                        help="今日の分がAIなしで作られていたときだけ作り直す(朝の再挑戦用)")
    args = parser.parse_args()

    now = datetime.now(JST)
    if args.retry_if_free:
        today = DATA_DIR / f"{now:%Y-%m-%d}.json"
        if today.exists() and json.loads(today.read_text(encoding="utf-8")).get("mode") != "free":
            print("今日の分はAI要約済みなので何もしません", file=sys.stderr)
            return
        if not os.environ.get("GEMINI_API_KEY") and not os.environ.get("ANTHROPIC_API_KEY"):
            print("AIのキーが無いので再挑戦しません", file=sys.stderr)
            return
    if args.articles:
        articles = json.loads(args.articles.read_text(encoding="utf-8"))
    else:
        articles = collect_articles(now)
    print(f"{len(articles)} 件の記事を取得しました", file=sys.stderr)
    if not articles:
        sys.exit("記事が1件も取得できませんでした")

    if args.dry_run:
        for a in articles:
            print(f"[{a['id']}] {a['source']} | {a['title']}")
        return

    if os.environ.get("ANTHROPIC_API_KEY") and not args.free:
        enrich_descriptions(articles)
        mode, digest = "ai", summarize(articles, now)
    else:
        candidates = pick_candidates(articles, CANDIDATE_COUNT)
        enrich_descriptions(candidates)
        mode, digest = "free", pick_without_ai(candidates)
        if os.environ.get("GEMINI_API_KEY") and not args.no_ai:
            try:
                mode, digest = "gemini", summarize_with_gemini(candidates, now)
            except Exception as e:  # 無料枠の上限などで失敗しても、AIなしで必ず更新する
                print(f"[warn] Gemini での要約に失敗したためAIなしで作成します: {e}", file=sys.stderr)
    add_glossary_from_wikipedia(digest["items"])  # AIが付けなかった記事は Wikipedia で補う
    date_str = now.strftime("%Y-%m-%d")
    result = {
        "date": date_str,
        "generated_at": now.isoformat(timespec="minutes"),
        "mode": mode,
        "headline": digest["headline"],
        "items": attach_sources(digest, articles),
        "article_count": len(articles),
    }
    save(result, date_str)
    print(f"docs/data/{date_str}.json に {len(result['items'])} 件を保存しました", file=sys.stderr)


if __name__ == "__main__":
    main()
