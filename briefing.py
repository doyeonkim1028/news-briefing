# ------------------------------------------------------------
# briefing.py
# settings.json 의 모든 RSS 주소에서 기사를 모은 뒤,
#   1) 제목이나 소개문에 키워드가 들어간 기사를 먼저 고르고
#   2) 모자라면 최신 기사로 채워서
#   3) 최대 5개를 고르고
#   4) 기사마다 Gemini 로 세 줄 요약을 만들어
#   5) news.json(데이터)과 index.html(웹페이지)로 저장합니다.
#
# 키워드는 settings.json 의 "keywords" 에서, Gemini 키는 .env 의 GEMINI_API_KEY 에서 읽습니다.
# 기사 본문은 가져오지 않고, RSS 에 들어 있는 제목/소개문만 사용합니다.
# 키가 없거나 요약이 실패해도 index.html 은 항상 만들어집니다.
#
# 실행 방법:  python briefing.py
# ------------------------------------------------------------

import json
import os                          # 환경 변수 읽기 (GitHub Actions 에서 API 키를 받을 때)
import re                        # 정규식: 글자 속에서 특정 모양(예: HTML 태그)을 찾는 기능
import html                        # HTML 특수 기호 처리 (&amp; ↔ & 등)
import urllib.request
import urllib.error                # 인터넷 요청이 실패했을 때의 오류 종류
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

# fetch_news.py 에 이미 만들어 둔 기능을 다시 가져다 씁니다. (같은 코드를 두 번 쓰지 않기 위해)
#   download      : URL 에서 내용 받아오기
#   make_safe_url : URL 속 한글을 안전한 모양으로 바꾸기
#   to_datetime   : RSS 날짜 글자를 비교 가능한 날짜 값으로 바꾸기
from fetch_news import download, make_safe_url, to_datetime

MAX_ARTICLES = 5                                    # 최종으로 고를 기사 수
FOLDER = Path(__file__).parent                      # 이 파일이 있는 폴더
SETTINGS_FILE = FOLDER / "settings.json"            # 읽을 설정 파일
ENV_FILE = FOLDER / ".env"                          # API 키가 들어 있는 파일
JSON_FILE = FOLDER / "news.json"                    # 저장할 데이터 파일
HTML_FILE = FOLDER / "index.html"                   # 저장할 웹페이지 파일

# 사용할 Gemini 모델 목록. 앞의 모델이 실패하면(응답 없음, 오류 등) 다음 모델로 다시 시도합니다.
#   gemini-3.5-flash         : 기본 모델
#   gemini-flash-lite-latest : 더 가볍고 빠른 예비 모델
# (참고: "gemini-flash-latest" 별명은 테스트할 때 응답이 오지 않아 쓰지 않았습니다.)
GEMINI_MODELS = ["gemini-3.5-flash", "gemini-flash-lite-latest"]
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_TIMEOUT = 30   # 한 번 부를 때 최대 몇 초 기다릴지

# 요약을 부탁할 때 Gemini 에게 보내는 지시문. {article_info} 자리에 기사 정보가 들어갑니다.
SUMMARY_PROMPT = """아래 기사 정보만 보고 한국어로 세 줄 요약을 써 줘.
- 각 줄은 한 문장, 줄 앞에 "- " 를 붙인다
- 기사 정보에 없는 내용, 숫자, 이름은 절대 쓰지 않는다
- 정보가 부족하면 마지막 줄에 "- 자세한 내용은 원문 확인 필요" 라고 쓴다

[기사 정보]
{article_info}"""


# ============================================================
# 1. 설정 읽기
# ============================================================

def load_settings():
    """settings.json 을 읽어서 (RSS 주소 목록, 키워드 목록)을 돌려줍니다."""
    with open(SETTINGS_FILE, encoding="utf-8") as f:
        settings = json.load(f)
    return settings["feeds"], settings["keywords"]


def load_env():
    """.env 파일을 읽어서 {"이름": "값"} 모양의 사전으로 돌려줍니다. 파일이 없으면 빈 사전."""
    values = {}
    if not ENV_FILE.exists():
        return values
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        # 빈 줄, # 으로 시작하는 설명 줄, = 가 없는 줄은 건너뜁니다.
        if not line or line.startswith("#") or "=" not in line:
            continue
        # partition("=") : 첫 번째 = 를 기준으로 (앞, =, 뒤) 세 조각으로 나눔
        name, _, value = line.partition("=")
        values[name.strip()] = value.strip().strip('"').strip("'")   # 값을 감싼 따옴표도 제거
    return values


# ============================================================
# 2. RSS 에서 기사 모으기
# ============================================================

def clean_text(text):
    """소개문에 섞여 있는 HTML 태그(<a ...>, <b> 등)를 지우고 순수한 글자만 남깁니다."""
    text = re.sub(r"<[^>]+>", " ", text or "")   # < 로 시작해 > 로 끝나는 부분(태그)을 공백으로
    text = html.unescape(text)                   # &amp; → &,  &quot; → " 등으로 되돌림
    return " ".join(text.split())                # 여러 칸의 공백/줄바꿈을 한 칸으로 정리


def parse_articles(xml_bytes, feed_url):
    """RSS 내용에서 기사마다 제목/링크/날짜/소개문/언론사를 뽑아 리스트로 돌려줍니다."""
    root = ET.fromstring(xml_bytes)
    channel_name = clean_text(root.findtext("channel/title"))   # RSS 전체의 이름 (예비용)
    articles = []
    for item in root.iter("item"):
        title = clean_text(item.findtext("title"))

        # 언론사 이름은 RSS 마다 들어 있는 곳이 다릅니다.
        #   구글 뉴스: <source>,  매일경제: <author>,  둘 다 없으면 RSS 이름을 씁니다.
        source = clean_text(item.findtext("source") or item.findtext("author")) or channel_name

        # 구글 뉴스 제목은 "기사 제목 - 언론사" 모양이라, 뒤의 " - 언론사" 를 떼어 냅니다.
        if source and title.endswith(" - " + source):
            title = title[: -len(" - " + source)]

        articles.append({
            "title": title,
            "link": (item.findtext("link") or "").strip(),
            "date": (item.findtext("pubDate") or "").strip(),
            "description": clean_text(item.findtext("description")),   # RSS 의 '소개문'
            "source": source,
        })
    return articles


def find_keywords(article, keywords):
    """제목이나 소개문에 들어 있는 키워드만 골라서 리스트로 돌려줍니다. (없으면 빈 리스트)"""
    text = article["title"] + " " + article["description"]
    return [word for word in keywords if word in text]


def collect_articles(feeds, keywords):
    """모든 RSS 에서 기사를 모아, 키워드 기사 우선 + 최신순으로 최대 5개를 돌려줍니다."""
    all_articles = []
    for feed_url in feeds:
        try:
            data = download(make_safe_url(feed_url))
            all_articles.extend(parse_articles(data, feed_url))
        except Exception as error:
            # 한 곳이 실패해도 멈추지 않고, 화면에 알려 준 뒤 다음 주소로 넘어갑니다.
            print(f"[RSS 건너뜀] {feed_url} → {error}")

    # 같은 기사가 여러 RSS 에 실린 경우를 대비해, 링크가 같은 기사는 하나만 남깁니다.
    unique = {}
    for article in all_articles:
        unique.setdefault(article["link"], article)
    all_articles = list(unique.values())

    # 전체를 최신순(최근 날짜가 앞)으로 정렬합니다.
    all_articles.sort(key=lambda a: to_datetime(a["date"]), reverse=True)

    # 각 기사에 걸린 키워드를 적고, '키워드 있는 기사'와 '없는 기사'로 나눕니다.
    matched, others = [], []
    for article in all_articles:
        article["matched_keywords"] = find_keywords(article, keywords)
        (matched if article["matched_keywords"] else others).append(article)

    print(f"전체 {len(all_articles)}개 중 키워드 기사 {len(matched)}개")
    # 키워드 기사를 먼저, 그 뒤에 최신 기사를 이어 붙인 다음 앞에서 5개만 자릅니다.
    return (matched + others)[:MAX_ARTICLES]


# ============================================================
# 3. Gemini 로 세 줄 요약 만들기
# ============================================================

def summarize(article, api_key):
    """GEMINI_MODELS 의 모델을 차례로 시도해서, 처음 성공한 요약을 돌려줍니다.
    모든 모델이 실패하면 마지막 오류를 일으키고, 부르는 쪽에서 처리합니다."""
    last_error = None
    for model in GEMINI_MODELS:
        try:
            return summarize_with(model, article, api_key)
        except Exception as error:
            last_error = error
            print(f"  ({model} 실패: {error} → 다음 모델 시도)")
    raise last_error


def summarize_with(model, article, api_key):
    """기사 하나를 지정한 Gemini 모델에게 보내 세 줄 요약(문장 3개짜리 리스트)을 받아 옵니다.
    실패하면 오류(Exception)를 일으킵니다."""
    # Gemini 에게 줄 '기사 정보'. 본문은 없고 RSS 에 있는 것만 넣습니다.
    article_info = (
        f"제목: {article['title']}\n"
        f"언론사: {article['source']}\n"
        f"날짜: {article['date']}\n"
        f"소개문: {article['description'] or '(없음)'}"
    )

    # Gemini API 가 요구하는 모양으로 요청 내용을 만듭니다.
    body = {
        "contents": [{"parts": [{"text": SUMMARY_PROMPT.format(article_info=article_info)}]}],
        # temperature 가 낮을수록 지어내지 않고 덜 들쭉날쭉하게 답합니다.
        "generationConfig": {"temperature": 0.2},
    }
    request = urllib.request.Request(
        GEMINI_URL.format(model=model),            # URL 의 {model} 자리에 모델 이름을 넣음
        data=json.dumps(body).encode("utf-8"),    # 사전 → JSON 글자 → 바이트
        headers={
            "Content-Type": "application/json",
            # 키는 URL 이 아니라 헤더에 넣습니다. (오류 메시지나 기록에 키가 찍히지 않도록)
            "x-goog-api-key": api_key,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=GEMINI_TIMEOUT) as response:
            reply = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        # 400(요청 오류), 403(키 문제), 429(사용량 초과) 등. 숫자만 알려 줍니다.
        raise RuntimeError(f"Gemini 응답 오류 (HTTP {error.code})") from None

    # 응답에서 글자 부분만 꺼냅니다. 구조: candidates[0] → content → parts[] → text
    parts = reply["candidates"][0]["content"]["parts"]
    text = "".join(part.get("text", "") for part in parts)

    # "- " 로 시작하는 줄만 골라 앞의 "- " 를 떼고, 최대 3줄만 씁니다.
    lines = [line.strip()[2:].strip() for line in text.splitlines() if line.strip().startswith("- ")]
    if not lines:
        raise RuntimeError("요약 형식이 예상과 달라요")
    return lines[:3]


def add_summaries(articles, api_key):
    """기사마다 요약을 붙입니다. 실패한 기사에는 요약 대신 실패 이유를 적어 둡니다."""
    for article in articles:
        article["summary"] = []          # 요약 문장들 (실패하면 빈 리스트)
        article["summary_error"] = ""    # 실패 이유 (성공하면 빈 글자)

        if not api_key:
            article["summary_error"] = ".env 에 GEMINI_API_KEY 가 없어 요약을 만들지 못했습니다."
            continue                     # 다음 기사로 넘어감

        try:
            article["summary"] = summarize(article, api_key)
            print(f"[요약 완료] {article['title'][:30]}")
        except Exception as error:
            article["summary_error"] = f"요약을 만들지 못했습니다. ({error})"
            print(f"[요약 실패] {article['title'][:30]} → {error}")


# ============================================================
# 4. 결과 저장 (news.json, index.html)
# ============================================================

def format_date(date_text):
    """RSS 날짜('Sun, 08 Nov 2026 08:11:28 GMT')를 한국 시간 '11월 8일 17:11' 모양으로 바꿉니다."""
    moment = to_datetime(date_text)
    if moment.year == 1:                  # to_datetime 이 날짜를 못 읽으면 '아주 옛날(1년)'을 돌려줌
        return date_text                  # 그럴 땐 원래 글자를 그대로 보여 줌
    korea = moment.astimezone(timezone(timedelta(hours=9)))   # 한국 시간(UTC+9)으로 변환
    return f"{korea.month}월 {korea.day}일 {korea:%H:%M}"


def build_html(articles, page_title, collected_at):
    """기사 목록으로 index.html 에 들어갈 HTML 글자를 만듭니다."""
    # html.escape : 기사 제목에 < > & " 같은 기호가 있어도 페이지가 깨지지 않게 바꿔 줌
    e = html.escape

    cards = []
    for a in articles:
        # 걸린 키워드를 작은 꼬리표(태그)로 보여 줍니다. 키워드 없이 채워진 기사는 '최신'으로 표시.
        if a["matched_keywords"]:
            tags = "".join(f'<span class="tag">{e(word)}</span>' for word in a["matched_keywords"])
        else:
            tags = '<span class="tag tag-latest">최신</span>'

        if a["summary"]:
            summary_html = '<ul class="summary">' + "".join(f"<li>{e(line)}</li>" for line in a["summary"]) + "</ul>"
        else:
            summary_html = f'<p class="error">{e(a["summary_error"])}</p>'

        # 카드 한 장 = 기사 하나. 제목을 누르면 원문이 새 탭에서 열립니다.
        cards.append(f"""
      <article class="card">
        <div class="card-top">
          <span class="source">{e(a["source"])}</span>
          <span class="dot">·</span>
          <span class="date">{e(format_date(a["date"]))}</span>
          <span class="tags">{tags}</span>
        </div>
        <h2><a href="{e(a["link"])}" target="_blank" rel="noopener">{e(a["title"])}</a></h2>
        {summary_html}
        <a class="more" href="{e(a["link"])}" target="_blank" rel="noopener">원문 보기 ›</a>
      </article>""")

    if not cards:
        cards.append('\n      <p class="error empty">가져온 기사가 없습니다. 인터넷 연결이나 RSS 주소를 확인해 주세요.</p>')

    # 아래 {{ }} 는 CSS 의 중괄호입니다. f-string 안에서는 { } 가 '변수 자리'라서 두 번 겹쳐 써야 글자 그대로 나옵니다.
    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{e(page_title)}</title>
  <style>
    /* 색상표: 한곳에 모아 두면 나중에 바꾸기 쉽습니다 */
    :root {{
      --green: #03c75a;        /* 포인트 색 */
      --green-light: #e8f9ef;  /* 키워드 꼬리표 배경 */
      --bg: #f5f6f8;           /* 페이지 배경 */
      --text: #1e1e23;         /* 본문 글자 */
      --sub: #505055;          /* 요약 글자 */
      --muted: #8c8c91;        /* 날짜 등 흐린 글자 */
      --line: #e5e5ea;         /* 테두리 */
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0; background: var(--bg); color: var(--text);
      font-family: "Apple SD Gothic Neo", "Malgun Gothic", "맑은 고딕", sans-serif;
      font-size: 16px; line-height: 1.7; word-break: keep-all;   /* keep-all: 한글 단어 중간에서 줄이 안 끊기게 */
    }}

    /* 맨 위 초록 머리 영역 */
    header {{ background: var(--green); color: #fff; }}
    .header-inner {{ max-width: 720px; margin: 0 auto; padding: 28px 20px 24px; }}
    header .label {{ font-size: 13px; font-weight: 700; opacity: .85; letter-spacing: .5px; }}
    header h1 {{ margin: 4px 0 6px; font-size: 26px; font-weight: 800; }}
    header .collected {{ margin: 0; font-size: 14px; opacity: .9; }}

    main {{ max-width: 720px; margin: 0 auto; padding: 20px 20px 8px; }}

    /* 기사 카드 */
    .card {{
      background: #fff; border: 1px solid var(--line); border-radius: 12px;
      padding: 22px 24px; margin-bottom: 14px;
    }}
    .card-top {{ display: flex; align-items: center; flex-wrap: wrap; gap: 6px; font-size: 13px; color: var(--muted); }}
    .source {{ color: var(--text); font-weight: 700; }}
    .tags {{ margin-left: auto; display: flex; gap: 4px; }}
    .tag {{ background: var(--green-light); color: #00a94f; font-weight: 700; font-size: 12px; padding: 2px 8px; border-radius: 10px; }}
    .tag-latest {{ background: #f0f0f3; color: var(--muted); }}

    .card h2 {{ margin: 10px 0 12px; font-size: 19px; line-height: 1.45; font-weight: 700; }}
    .card h2 a {{ color: var(--text); text-decoration: none; }}
    .card h2 a:hover {{ text-decoration: underline; }}

    /* 세 줄 요약: 왼쪽에 초록 세로줄 */
    .summary {{
      list-style: none; margin: 0 0 14px; padding: 12px 16px;
      background: #fafafb; border-left: 3px solid var(--green); border-radius: 0 8px 8px 0;
      color: var(--sub); font-size: 15px;
    }}
    .summary li {{ position: relative; padding-left: 14px; }}
    .summary li + li {{ margin-top: 4px; }}
    .summary li::before {{ content: ""; position: absolute; left: 0; top: .72em; width: 5px; height: 5px; border-radius: 50%; background: var(--green); }}

    .more {{ font-size: 14px; font-weight: 700; color: var(--green); text-decoration: none; }}
    .more:hover {{ text-decoration: underline; }}

    .error {{ margin: 0 0 14px; padding: 10px 14px; background: #fff7ed; color: #b45309; border-radius: 8px; font-size: 14px; }}
    .empty {{ text-align: center; }}

    footer {{ max-width: 720px; margin: 0 auto; padding: 8px 20px 40px; text-align: center; color: var(--muted); font-size: 13px; }}

    /* 휴대폰처럼 좁은 화면에서는 여백과 글자를 조금 줄입니다 */
    @media (max-width: 480px) {{
      .card {{ padding: 18px; }}
      .card h2 {{ font-size: 17px; }}
      header h1 {{ font-size: 22px; }}
    }}
  </style>
</head>
<body>
  <header>
    <div class="header-inner">
      <div class="label">오늘의 뉴스 브리핑</div>
      <h1>{e(page_title)}</h1>
      <p class="collected">수집 시각 {e(collected_at)}</p>
    </div>
  </header>
  <main>{"".join(cards)}
  </main>
  <footer>⚠ AI 요약은 틀릴 수 있음 · 정확한 내용은 원문을 확인하세요</footer>
</body>
</html>
"""


def save_results(articles, page_title, collected_at):
    """news.json 과 index.html 을 저장합니다."""
    # news.json : 저장할 항목만 골라 정리 (소개문은 키워드/요약용이라 저장하지 않음)
    data = [
        {
            "title": a["title"],
            "link": a["link"],
            "date": a["date"],
            "source": a["source"],
            "matched_keywords": a["matched_keywords"],
            "summary": a["summary"],
        }
        for a in articles
    ]
    with open(JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    with open(HTML_FILE, "w", encoding="utf-8") as f:
        f.write(build_html(articles, page_title, collected_at))


# ============================================================
# 5. 프로그램 시작점
# ============================================================

def main():
    # 수집 시각: 지금 한국 시간을 "2026-11-08 17:40" 모양으로
    # (GitHub Actions 서버는 세계 표준시(UTC)라서, 한국 시간(UTC+9)으로 직접 맞춰 줍니다)
    collected_at = datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M")
    feeds, keywords = load_settings()

    # 페이지 제목은 키워드를 '·' 로 이어서 만듭니다. → "반도체·수주·컨센서스"
    # (settings.json 의 키워드를 바꾸면 제목도 따라 바뀝니다)
    page_title = "·".join(keywords)

    # 어떤 단계가 실패해도 index.html 은 꼭 만들어지도록, 실패하면 빈 목록으로 진행합니다.
    try:
        articles = collect_articles(feeds, keywords)
    except Exception as error:
        print(f"[기사 수집 실패] {error}")
        articles = []

    # 키는 환경 변수(GitHub Actions 의 Secrets)를 먼저 보고, 없으면 .env 파일에서 찾습니다.
    api_key = os.environ.get("GEMINI_API_KEY") or load_env().get("GEMINI_API_KEY", "")
    add_summaries(articles, api_key)

    save_results(articles, page_title, collected_at)
    print(f"{len(articles)}개 기사를 {JSON_FILE.name}, {HTML_FILE.name} 에 저장했습니다.")


if __name__ == "__main__":
    main()
