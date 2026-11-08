# ------------------------------------------------------------
# fetch_news.py
# settings.json 에 적힌 RSS 주소들에서 최신 글 3개씩 가져와
# 제목(title), 링크(link), 날짜(date)만 JSON 으로 화면에 보여 줍니다.
#
# 파이썬에 기본으로 들어 있는 기능만 사용하므로 따로 설치할 것이 없습니다.
# 실행 방법:  python fetch_news.py
# ------------------------------------------------------------

# import = "다른 곳에 만들어져 있는 기능을 가져와서 쓰겠다"는 뜻입니다.
import json                      # JSON 파일을 읽고, JSON 모양으로 출력하는 기능
import urllib.request            # 인터넷 주소(URL)에 접속해서 내용을 받아오는 기능
import urllib.parse              # URL 안의 한글 같은 글자를 안전한 모양으로 바꾸는 기능
import xml.etree.ElementTree as ET  # RSS(= XML 형식의 글)를 읽어서 분석하는 기능
from pathlib import Path         # 파일 경로를 쉽게 다루는 기능
from datetime import datetime, timezone          # 날짜·시간을 다루는 기능
from email.utils import parsedate_to_datetime    # RSS 날짜 글자를 날짜 값으로 바꾸는 기능

# 한 번에 가져올 글의 개수. 바꾸고 싶으면 이 숫자만 고치면 됩니다.
HOW_MANY = 3

# 이 파이썬 파일이 있는 폴더 안의 settings.json 을 가리킵니다.
# (어느 폴더에서 실행하든 같은 settings.json 을 찾도록 하기 위함)
SETTINGS_FILE = Path(__file__).parent / "settings.json"


def load_feeds():
    """settings.json 을 열어서 'feeds' 목록(RSS 주소들)을 돌려줍니다."""
    # encoding="utf-8" : 한글이 깨지지 않도록 UTF-8 로 읽습니다.
    with open(SETTINGS_FILE, encoding="utf-8") as f:
        settings = json.load(f)   # JSON 글자 → 파이썬의 딕셔너리(사전)로 변환
    return settings["feeds"]      # 그중 "feeds" 항목만 꺼내서 돌려줌


def make_safe_url(url):
    """URL 에 한글(예: SK하이닉스)이 있으면 인터넷이 이해하는 모양(%EC%..)으로 바꿉니다."""
    # safe=... 에 적힌 기호들은 URL 에서 의미가 있는 기호라서 그대로 둡니다.
    return urllib.parse.quote(url, safe=":/?&=%")


def download(url):
    """주어진 URL 에 접속해서 내용을 바이트(bytes) 그대로 받아옵니다."""
    # 일부 사이트는 '누가 접속했는지(User-Agent)'가 없으면 거절하기 때문에 이름표를 붙여 줍니다.
    request = urllib.request.Request(url, headers={"User-Agent": "news-briefing/1.0"})
    # timeout=10 : 10초 안에 응답이 없으면 포기합니다(무한정 기다리지 않도록).
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.read()


def parse_items(xml_bytes, how_many):
    """RSS 내용에서 글(item)들을 찾아 제목/링크/날짜만 뽑아 리스트로 돌려줍니다."""
    # 받아온 내용을 XML 나무 구조로 바꿉니다. (RSS 는 <item> 태그 안에 글 하나씩 들어 있음)
    root = ET.fromstring(xml_bytes)

    results = []  # 결과를 담을 빈 리스트
    # root.iter("item") : 문서 안의 모든 <item>(글 하나하나)을 차례대로 찾습니다.
    for item in root.iter("item"):
        results.append({
            # findtext("title") : <title> 태그 안의 글자를 꺼냅니다. 없으면 "" (빈 글자)
            # .strip() : 앞뒤의 쓸데없는 공백/줄바꿈을 지웁니다.
            "title": (item.findtext("title") or "").strip(),
            "link": (item.findtext("link") or "").strip(),
            "date": (item.findtext("pubDate") or "").strip(),  # RSS 에서 날짜는 <pubDate>
        })

    # 글이 항상 최신순으로 들어 있는 것은 아닙니다(예: 구글 뉴스 검색은 '관련도순').
    # 그래서 날짜를 기준으로 '최신 → 오래된' 순서로 직접 정렬합니다.
    # key=  : 무엇을 기준으로 정렬할지,  reverse=True : 큰 값(최근 날짜)이 앞으로
    results.sort(key=lambda news: to_datetime(news["date"]), reverse=True)

    # [:how_many] : 리스트의 앞에서부터 how_many 개만 잘라서 돌려줍니다.
    return results[:how_many]


def to_datetime(date_text):
    """'Sun, 08 Nov 2026 01:06:06 GMT' 같은 날짜 글자를 비교 가능한 날짜 값으로 바꿉니다."""
    try:
        # parsedate_to_datetime : RSS 에서 쓰는 날짜 형식을 읽어 주는 기본 기능
        # 일부 사이트는 시간대를 '+09:00' 처럼 쓰는데, 이 기능은 '+0900' 만 알아들어서 ':' 를 지워 줍니다.
        if date_text[-3:-2] == ":":
            date_text = date_text[:-3] + date_text[-2:]
        result = parsedate_to_datetime(date_text)
        # 시간대 정보가 없는 날짜는 비교할 수 없으므로 세계 표준시(UTC)로 간주합니다.
        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)
        return result
    except (TypeError, ValueError, IndexError):
        # 날짜가 없거나 읽을 수 없으면 '아주 옛날'로 취급해서 맨 뒤로 보냅니다.
        return datetime.min.replace(tzinfo=timezone.utc)


def main():
    """프로그램의 시작점: 모든 RSS 주소를 돌면서 결과를 모으고 출력합니다."""
    output = []  # 최종 결과(주소별 글 목록)를 담을 리스트

    for feed_url in load_feeds():          # settings.json 의 주소를 하나씩 꺼내 반복
        try:
            # try 안에서 오류가 나도 프로그램 전체가 멈추지 않고 except 로 넘어갑니다.
            data = download(make_safe_url(feed_url))
            items = parse_items(data, HOW_MANY)
            output.append({"feed": feed_url, "items": items})
        except Exception as error:
            # 인터넷 끊김, 잘못된 주소 등으로 실패하면 오류 내용을 결과에 함께 적어 둡니다.
            output.append({"feed": feed_url, "error": str(error)})

    # 파이썬 데이터를 JSON 글자로 바꿔 화면에 출력합니다.
    # ensure_ascii=False : 한글을 \uXXXX 가 아니라 한글 그대로 보여 줌
    # indent=2           : 보기 좋게 2칸씩 들여쓰기
    print(json.dumps(output, ensure_ascii=False, indent=2))


# 이 파일을 직접 실행했을 때만 main() 을 부릅니다.
# (다른 파일에서 import 해서 쓸 때는 자동으로 실행되지 않도록 하는 파이썬의 관례)
if __name__ == "__main__":
    main()
