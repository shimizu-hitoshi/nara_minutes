"""奈良市議会の議事録を取得するスクリプト

対象URL: https://ssp.kaigiroku.net/tenant/narashi/SpTop.html

このサイトはJavaScriptで動的にコンテンツを生成するため（単純なリンクではない）、
Playwrightを使用してブラウザを操作します。

各会議のリンクはJavaScript onclickハンドラ経由で遷移するため、
通常のHTTPリクエストでは取得できません。
"""

import asyncio
import csv
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Optional

from playwright.async_api import Page, async_playwright

BASE_URL = "https://ssp.kaigiroku.net/tenant/narashi"
TOP_URL = f"{BASE_URL}/SpTop.html"
SEARCH_URL = f"{BASE_URL}/SpSearch.html"


@dataclass
class Meeting:
    """会議情報"""

    title: str
    date: str
    council_id: str = ""
    schedule_id: str = ""
    url: str = ""
    meeting_type: str = ""


@dataclass
class MinuteEntry:
    """議事録エントリ（発言者と発言内容）"""

    speaker: str
    content: str


@dataclass
class Minutes:
    """議事録"""

    meeting_title: str
    meeting_date: str
    council_id: str
    schedule_id: str
    url: str
    entries: list[MinuteEntry] = field(default_factory=list)
    full_text: str = ""
    fetched_at: str = field(default_factory=lambda: datetime.now().isoformat())


def _extract_ids_from_url(url: str) -> tuple[str, str]:
    """URLからcouncil_idとschedule_idを抽出"""
    council_id = re.search(r"council_id=(\d+)", url)
    schedule_id = re.search(r"schedule_id=(\d+)", url)
    return (
        council_id.group(1) if council_id else "",
        schedule_id.group(1) if schedule_id else "",
    )


def _extract_ids_from_onclick(onclick: str) -> tuple[str, str]:
    """onclick属性からcouncil_idとschedule_idを抽出

    パターン例:
      onclick="location.href='SpMinuteView.html?council_id=1&schedule_id=2'"
      onclick="goMinuteView(1, 2)"
    """
    # URLパターン（council_id=X&schedule_id=Y）
    url_match = re.search(r"council_id=(\d+).*?schedule_id=(\d+)", onclick)
    if url_match:
        return url_match.group(1), url_match.group(2)

    # 関数呼び出しパターン（2つの数値引数）
    numbers = re.findall(r"\b(\d+)\b", onclick)
    if len(numbers) >= 2:
        return numbers[0], numbers[1]

    return "", ""


async def _wait_for_dynamic_content(page: Page, timeout: int = 5000) -> None:
    """JavaScriptによる動的コンテンツの読み込みを待機"""
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout)
    except Exception:
        pass
    await page.wait_for_timeout(2000)


async def _extract_meetings_from_page(page: Page) -> list[Meeting]:
    """現在のページから会議情報を抽出

    kaigiroku.netのSpページでは、会議一覧がJavaScriptのonclick属性を
    持つ要素（li, tr, a, divなど）として表示されます。
    """
    meetings: list[Meeting] = []
    seen_ids: set[tuple[str, str]] = set()

    # JavaScriptで生成された会議アイテムを探す
    # kaigiroku.netではonclick属性にIDが含まれることが多い
    candidate_selectors = [
        "li[onclick]",
        "tr[onclick]",
        "div[onclick]",
        'a[href*="SpMinuteView"]',
        'a[href*="MinuteView"]',
        'a[onclick*="council"]',
        'a[onclick*="schedule"]',
        "[onclick*='council_id']",
        "[onclick*='MinuteView']",
        ".meeting-item",
        ".list-item",
    ]

    found_elements = []
    for selector in candidate_selectors:
        try:
            elements = await page.query_selector_all(selector)
            if elements:
                found_elements = elements
                break
        except Exception:
            continue

    for element in found_elements:
        try:
            # onclick・href属性からIDを抽出
            onclick = await element.get_attribute("onclick") or ""
            href = await element.get_attribute("href") or ""

            council_id, schedule_id = "", ""
            url = ""

            if href and ("council_id" in href or "MinuteView" in href):
                council_id, schedule_id = _extract_ids_from_url(href)
                url = (
                    href
                    if href.startswith("http")
                    else f"{BASE_URL}/{href.lstrip('/')}"
                )
            elif onclick:
                council_id, schedule_id = _extract_ids_from_onclick(onclick)
                if council_id and schedule_id:
                    url = f"{BASE_URL}/SpMinuteView.html?council_id={council_id}&schedule_id={schedule_id}"

            if not council_id and not url:
                continue

            # 重複を除外
            key = (council_id, schedule_id)
            if key in seen_ids:
                continue
            seen_ids.add(key)

            # タイトルを取得
            title = (await element.text_content() or "").strip()
            if not title:
                title_el = await element.query_selector(
                    ".title, .name, span, strong, p"
                )
                if title_el:
                    title = (await title_el.text_content() or "").strip()
            if not title:
                title = f"会議 council_id={council_id} schedule_id={schedule_id}"

            # 日付を取得
            date = ""
            date_el = await element.query_selector(".date, .time, [class*='date']")
            if date_el:
                date = (await date_el.text_content() or "").strip()
            if not date:
                date_match = re.search(
                    r"((?:令和|平成|昭和)\d+年\d+月\d+日|\d{4}年\d+月\d+日)", title
                )
                if date_match:
                    date = date_match.group(1)

            meetings.append(
                Meeting(
                    title=title,
                    date=date,
                    council_id=council_id,
                    schedule_id=schedule_id,
                    url=url,
                )
            )
        except Exception as e:
            print(f"  会議情報の抽出中にエラー: {e}", file=sys.stderr)
            continue

    return meetings


async def get_meeting_list(
    page: Page, max_meetings: Optional[int] = None
) -> list[Meeting]:
    """トップページから会議リストを取得

    kaigiroku.netのSpTop.htmlはJavaScriptで動的に会議一覧を表示します。
    単純なHTTPリクエストではなく、ブラウザ操作が必要です。
    """
    meetings: list[Meeting] = []

    print(f"トップページを読み込み中: {TOP_URL}")
    try:
        await page.goto(TOP_URL, wait_until="domcontentloaded", timeout=60000)
        await _wait_for_dynamic_content(page)
    except Exception as e:
        print(f"トップページの読み込みに失敗しました: {e}", file=sys.stderr)
        return meetings

    # 「会議一覧」リンクがあれば移動してより多くの会議を取得
    for link_text in ["会議一覧", "一覧", "MeetingList"]:
        try:
            list_link = await page.query_selector(
                f'a:has-text("{link_text}"), a[href*="MeetingList"], a[href*="meetinglist"]'
            )
            if list_link:
                await list_link.click()
                await _wait_for_dynamic_content(page)
                print(f"会議一覧ページに移動しました")
                break
        except Exception:
            continue

    # 現在のページから会議を抽出
    meetings.extend(await _extract_meetings_from_page(page))

    # ページネーション: 「次へ」ボタンがある場合はすべてのページを処理
    page_num = 1
    while max_meetings is None or len(meetings) < max_meetings:
        next_btn = None
        for next_text in ["次", "次へ", "Next", ">"]:
            try:
                btn = await page.query_selector(
                    f'a:has-text("{next_text}"), button:has-text("{next_text}"), '
                    f'a[rel="next"], .next-page, .pagination-next'
                )
                if btn and await btn.is_visible():
                    next_btn = btn
                    break
            except Exception:
                continue

        if not next_btn:
            break

        try:
            await next_btn.click()
            await _wait_for_dynamic_content(page)
            page_num += 1
            print(f"  ページ {page_num} を読み込み中...")
            new_meetings = await _extract_meetings_from_page(page)
            if not new_meetings:
                break
            meetings.extend(new_meetings)
        except Exception as e:
            print(f"  次のページへの移動に失敗しました: {e}", file=sys.stderr)
            break

    if max_meetings:
        meetings = meetings[:max_meetings]

    print(f"{len(meetings)} 件の会議が見つかりました")
    return meetings


async def fetch_minutes(page: Page, meeting: Meeting) -> Optional[Minutes]:
    """指定された会議の議事録を取得

    議事録ページはiframeを使用している場合があります。
    iframeのコンテンツも含めてテキストを抽出します。
    """
    if not meeting.url:
        print(f"  URLが不明のためスキップ: {meeting.title}", file=sys.stderr)
        return None

    print(f"  議事録を取得中: {meeting.title}")
    print(f"  URL: {meeting.url}")

    try:
        await page.goto(meeting.url, wait_until="domcontentloaded", timeout=60000)
        await _wait_for_dynamic_content(page)
    except Exception as e:
        print(f"  ページの読み込みに失敗しました: {e}", file=sys.stderr)
        return None

    content_html = ""
    full_text = ""

    # iframeがある場合はiframe内のコンテンツを使用
    # kaigiroku.netはiframeに議事録本文を表示することがある
    try:
        iframe_element = await page.query_selector(
            "iframe#minuteFrame, iframe[name='minuteFrame'], iframe"
        )
        if iframe_element:
            frame = await iframe_element.content_frame()
            if frame:
                await _wait_for_dynamic_content(frame)
                content_html = await frame.content()
                full_text = await frame.evaluate("document.body.innerText || ''")
    except Exception as e:
        print(f"  iframeの読み込みに失敗しました: {e}", file=sys.stderr)

    # iframeからコンテンツが取得できなかった場合はメインページを使用
    if not content_html:
        content_html = await page.content()
    if not full_text:
        full_text = await page.evaluate("document.body.innerText || ''")

    # 発言者ごとのエントリを抽出
    entries = _extract_entries(content_html, full_text)

    return Minutes(
        meeting_title=meeting.title,
        meeting_date=meeting.date,
        council_id=meeting.council_id,
        schedule_id=meeting.schedule_id,
        url=meeting.url,
        entries=entries,
        full_text=full_text,
    )


def _extract_entries(html: str, fallback_text: str = "") -> list[MinuteEntry]:
    """HTMLから発言者ごとのエントリを抽出

    kaigiroku.netの議事録は以下のいずれかの形式で格納されます：
    1. 発言者クラスと内容クラスを持つ要素（.speaker + .content等）
    2. テーブル形式（発言者列 | 内容列）
    3. プレーンテキスト（発言者名◯や【】で囲まれた形式）
    """
    # 発言者行と本文行を区別するための最大文字数
    MAX_SPEAKER_LINE_LENGTH = 40
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")

        entries: list[MinuteEntry] = []

        # パターン1: 発言者クラスを持つ要素
        speaker_elements = soup.select(
            ".speaker, .member-name, .hatsugen-sha, "
            "[class*='speaker'], [class*='hatsugen']"
        )
        if speaker_elements:
            for speaker_el in speaker_elements:
                speaker_name = speaker_el.get_text(strip=True)
                content_el = speaker_el.find_next_sibling()
                content = content_el.get_text(strip=True) if content_el else ""
                if speaker_name:
                    entries.append(MinuteEntry(speaker=speaker_name, content=content))
            if entries:
                return entries

        # パターン2: テーブル形式
        rows = soup.select("tr")
        for row in rows:
            cells = row.select("td, th")
            if len(cells) >= 2:
                speaker = cells[0].get_text(strip=True)
                content = cells[1].get_text(strip=True)
                if speaker and content and len(content) > len(speaker):
                    entries.append(MinuteEntry(speaker=speaker, content=content))
        if entries:
            return entries

        # パターン3: プレーンテキストから発言者を識別
        # 例: ◯山田委員\n発言内容... や 【山田委員】発言内容...
        text = soup.get_text(separator="\n", strip=True)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        speaker_pattern = re.compile(
            r"^(?:◯|○|【|「|）)(.{1,30}?)(?:】|」|（|）|\s|$)"
        )
        current_speaker = ""
        current_content_lines: list[str] = []
        for line in lines:
            m = speaker_pattern.match(line)
            if m and len(line) < MAX_SPEAKER_LINE_LENGTH:
                if current_content_lines:
                    entries.append(
                        MinuteEntry(
                            speaker=current_speaker,
                            content="\n".join(current_content_lines),
                        )
                    )
                current_speaker = m.group(1).strip()
                current_content_lines = [line[m.end() :].strip()]
            else:
                current_content_lines.append(line)
        if current_content_lines:
            entries.append(
                MinuteEntry(
                    speaker=current_speaker,
                    content="\n".join(current_content_lines),
                )
            )

        if entries:
            return entries

        # フォールバック: ページ全体のテキストを1エントリとして返す
        if text:
            return [MinuteEntry(speaker="", content=text)]
        if fallback_text:
            return [MinuteEntry(speaker="", content=fallback_text)]
        return []

    except ImportError:
        if fallback_text:
            return [MinuteEntry(speaker="", content=fallback_text)]
        return []


def save_to_csv(minutes_list: list[Minutes], output_file: str) -> None:
    """議事録一覧をCSVに保存（BOM付きUTF-8でExcel対応）"""
    with open(output_file, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["会議名", "開催日", "council_id", "schedule_id", "URL", "取得日時"]
        )
        for m in minutes_list:
            writer.writerow(
                [
                    m.meeting_title,
                    m.meeting_date,
                    m.council_id,
                    m.schedule_id,
                    m.url,
                    m.fetched_at,
                ]
            )
    print(f"会議一覧を保存しました: {output_file}")


def save_to_json(minutes_list: list[Minutes], output_file: str) -> None:
    """議事録をJSONに保存"""
    data = [asdict(m) for m in minutes_list]
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"議事録データを保存しました: {output_file}")


def save_minutes_text(minutes: Minutes, output_file: str) -> None:
    """議事録のテキストをファイルに保存"""
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(f"会議名: {minutes.meeting_title}\n")
        f.write(f"開催日: {minutes.meeting_date}\n")
        f.write(f"URL: {minutes.url}\n")
        f.write(f"取得日時: {minutes.fetched_at}\n")
        f.write("\n" + "=" * 60 + "\n\n")
        if minutes.entries:
            for entry in minutes.entries:
                if entry.speaker:
                    f.write(f"【{entry.speaker}】\n")
                f.write(f"{entry.content}\n\n")
        else:
            f.write(minutes.full_text)
    print(f"  テキストを保存しました: {output_file}")


async def main(
    max_meetings: Optional[int] = 5,
    output_dir: str = ".",
    headless: bool = True,
) -> None:
    """メイン処理"""
    print("奈良市議会 議事録取得ツール")
    print(f"対象URL: {TOP_URL}")
    print("-" * 60)

    os.makedirs(output_dir, exist_ok=True)
    all_minutes: list[Minutes] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        )
        page = await context.new_page()

        try:
            # 会議一覧を取得
            meetings = await get_meeting_list(page, max_meetings=max_meetings)

            if not meetings:
                print(
                    "会議が見つかりませんでした。サイトの構造が変わった可能性があります。",
                    file=sys.stderr,
                )
                return

            print(f"\n取得対象: {len(meetings)} 件の会議")
            print("-" * 60)

            # 各会議の議事録を取得
            for i, meeting in enumerate(meetings, 1):
                print(f"\n[{i}/{len(meetings)}]")
                minutes = await fetch_minutes(page, meeting)
                if minutes:
                    all_minutes.append(minutes)
                    safe_title = re.sub(r'[\\/:*?"<>|]', "_", meeting.title)[:50]
                    text_file = os.path.join(
                        output_dir,
                        f"minutes_{meeting.council_id}_{meeting.schedule_id}_{safe_title}.txt",
                    )
                    save_minutes_text(minutes, text_file)
                await asyncio.sleep(1)  # サーバーへの負荷を軽減

        finally:
            await browser.close()

    if all_minutes:
        json_file = os.path.join(output_dir, "nara_minutes.json")
        csv_file = os.path.join(output_dir, "nara_minutes.csv")
        save_to_json(all_minutes, json_file)
        save_to_csv(all_minutes, csv_file)
        print(f"\n完了: {len(all_minutes)} 件の議事録を取得しました")
    else:
        print("\n議事録の取得に失敗しました", file=sys.stderr)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="奈良市議会の議事録を取得します",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
例:
  python fetch_nara_minutes.py                  # 最新5件を取得
  python fetch_nara_minutes.py --max 10         # 最新10件を取得
  python fetch_nara_minutes.py --all            # すべて取得
  python fetch_nara_minutes.py --output ./data  # 出力先ディレクトリを指定
  python fetch_nara_minutes.py --no-headless    # ブラウザを表示して実行（デバッグ用）
        """,
    )
    parser.add_argument(
        "--max",
        type=int,
        default=5,
        metavar="N",
        help="取得する最大件数 (デフォルト: 5)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="すべての議事録を取得",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=".",
        metavar="DIR",
        help="出力ディレクトリ (デフォルト: カレントディレクトリ)",
    )
    parser.add_argument(
        "--no-headless",
        action="store_true",
        help="ブラウザを表示して実行 (デバッグ用)",
    )

    args = parser.parse_args()
    max_count = None if args.all else args.max

    asyncio.run(
        main(
            max_meetings=max_count,
            output_dir=args.output,
            headless=not args.no_headless,
        )
    )
