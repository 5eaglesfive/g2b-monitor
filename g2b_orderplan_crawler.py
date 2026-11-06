#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
나라장터(https://www.g2b.go.kr) 발주 > 발주게시판 크롤러

조건
  - 검색유형 : 발주계획
  - 업무구분 : 일반용역
  - 사업명   : "스마트경로당" "스마트 경로당" "경로당" "경로당 구축" (각각 검색 후 병합·중복제거)
  - 진행일자 : 오늘 ~ 오늘+6개월

설치
  pip install playwright
  playwright install chromium

실행
  python g2b_orderplan_crawler.py --headed --debug     # 최초 1회: 화면 보면서 확인
  python g2b_orderplan_crawler.py                       # 이후: 백그라운드 실행
  python g2b_orderplan_crawler.py --dump-xhr            # 내부 조회 API(XHR) 응답 저장 (구조 파악용)

※ 차세대 나라장터는 SPA(WebSquare)라서 화면 구조가 바뀌면 아래 [화면 설정] 값만 고치면 됩니다.
※ 사이트 부하를 줄이기 위해 호출 간격을 두었습니다. 상시 운영은 조달청 Open API를 권장합니다.
"""
from __future__ import annotations

import argparse
import calendar
import csv
import json
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

# ======================= 검색 조건 =======================
SEARCH_TYPE = "발주계획"
BUSINESS_TYPE = "일반용역"
KEYWORDS = ["스마트경로당", "스마트 경로당", "경로당", "경로당 구축"]
MONTHS = 6
DATE_FMT = "%Y%m%d"  # 날짜 입력칸에 타이핑할 형식 (마스크가 자동으로 YYYY/MM/DD로 바꿔줌)

# ======================= 화면 설정 (개편 시 여기만 수정) =======================
BASE_URL = "https://www.g2b.go.kr/"
MENU_PATH = ["발주", "발주게시판"]                    # 상단 메뉴 경로
DATE_LABELS = ["진행일자", "발주(예정)시기", "조회기간"]  # 날짜 입력칸 앞 라벨(순서대로 시도)
NAME_LABELS = ["사업명", "발주계획명", "공고명"]        # 사업명 입력칸 앞 라벨
SEARCH_BUTTON_TEXTS = ["검색", "조회"]
NEXT_PAGE_SELECTORS = [
    '[class*="pageList_next"]',
    'a[title*="다음"]',
    'button[title*="다음"]',
    'a:has-text("다음")',
]
CLOSE_POPUP_TEXTS = ["닫기", "오늘 하루 보지 않기", "확인"]
MAX_PAGES = 100
PAUSE = 1.2  # 동작 간 대기(초)

EXTRACT_JS = r"""
() => {
  const vis = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const clean = t => (t || '').replace(/\s+/g, ' ').trim();
  const rows = [...document.querySelectorAll('tr')]
    .filter(vis)
    .map(tr => [...tr.querySelectorAll(':scope > td')].map(td => clean(td.innerText)))
    .filter(c => c.length >= 4 && c.some(x => x));
  const heads = [...document.querySelectorAll('th, [class*="grid_head"] td')]
    .filter(vis).map(h => clean(h.innerText)).filter(Boolean);
  return {headers: heads, rows: rows};
}
"""


# ======================= 유틸 =======================
def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def norm(s: str) -> str:
    """공백 제거 정규화 ('스마트 경로당' == '스마트경로당')"""
    return re.sub(r"\s+", "", s or "")


def add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    y += d.year
    m += 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def first_visible(loc):
    try:
        for i in range(loc.count()):
            el = loc.nth(i)
            if el.is_visible():
                return el
    except Exception:
        pass
    return None


def click_text(page: Page, text: str, exact: bool = True) -> bool:
    """라디오/체크박스/탭/버튼/라벨/텍스트 순으로 찾아 첫 번째 보이는 요소를 클릭"""
    candidates = [
        page.get_by_role("radio", name=text, exact=exact),
        page.get_by_role("checkbox", name=text, exact=exact),
        page.get_by_role("tab", name=text, exact=exact),
        page.get_by_role("button", name=text, exact=exact),
        page.get_by_label(text, exact=exact),
        page.get_by_text(text, exact=exact),
    ]
    for loc in candidates:
        el = first_visible(loc)
        if el:
            el.click()
            page.wait_for_timeout(300)
            return True
    return False


def input_after_label(page: Page, labels: list[str], nth: int = 1):
    """라벨 텍스트 뒤에 나오는 n번째 텍스트 입력칸"""
    for label in labels:
        xp = (
            f"xpath=(//*[normalize-space(text())='{label}']"
            f"/following::input[not(@type='hidden') and not(@type='checkbox') and not(@type='radio')])[{nth}]"
        )
        loc = page.locator(xp)
        try:
            if loc.count() and loc.first.is_visible():
                return loc.first
        except Exception:
            continue
    return None


def type_value(el, value: str) -> None:
    el.click()
    el.press("Control+A")
    el.press("Delete")
    el.type(value, delay=30)
    el.press("Tab")


def snap(page: Page, out: Path, name: str, enabled: bool) -> None:
    if not enabled:
        return
    out.mkdir(parents=True, exist_ok=True)
    try:
        page.screenshot(path=str(out / f"{name}.png"), full_page=True)
        (out / f"{name}.html").write_text(page.content(), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        log(f"스냅샷 실패({name}): {e}")


def attach_xhr_dump(page: Page, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    counter = {"n": 0}

    def on_response(resp):
        try:
            if "json" not in (resp.headers.get("content-type") or ""):
                return
            body = resp.json()
        except Exception:  # noqa: BLE001
            return
        counter["n"] += 1
        payload = {
            "url": resp.url,
            "status": resp.status,
            "request_post_data": resp.request.post_data,
            "body": body,
        }
        (out_dir / f"{counter['n']:03d}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    page.on("response", on_response)


# ======================= 화면 조작 =======================
def dismiss_popups(page: Page) -> None:
    for t in CLOSE_POPUP_TEXTS:
        for _ in range(3):
            el = first_visible(page.get_by_text(t, exact=True))
            if not el:
                break
            try:
                el.click(timeout=2000)
                page.wait_for_timeout(300)
            except Exception:  # noqa: BLE001
                break


def open_board(page: Page, start_url: str | None) -> None:
    page.goto(start_url or BASE_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    dismiss_popups(page)
    if start_url:
        return
    top = first_visible(page.get_by_text(MENU_PATH[0], exact=True))
    if not top:
        raise RuntimeError(f"상단 메뉴 '{MENU_PATH[0]}'를 찾지 못했습니다. --headed --debug로 확인하세요.")
    top.hover()
    page.wait_for_timeout(600)
    sub = first_visible(page.get_by_text(MENU_PATH[1], exact=True))
    if not sub:
        top.click()
        page.wait_for_timeout(600)
        sub = first_visible(page.get_by_text(MENU_PATH[1], exact=True))
    if not sub:
        raise RuntimeError(f"하위 메뉴 '{MENU_PATH[1]}'를 찾지 못했습니다.")
    sub.click()
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:  # noqa: BLE001
        pass
    page.wait_for_timeout(1500)


def apply_filters(page: Page, keyword: str, d_from: date, d_to: date) -> None:
    if not click_text(page, SEARCH_TYPE):
        log(f"  ! 검색유형 '{SEARCH_TYPE}' 선택 요소를 찾지 못함")
    if not click_text(page, BUSINESS_TYPE):
        log(f"  ! 업무구분 '{BUSINESS_TYPE}' 선택 요소를 찾지 못함")

    i_from = input_after_label(page, DATE_LABELS, 1)
    i_to = input_after_label(page, DATE_LABELS, 2)
    if i_from and i_to:
        type_value(i_from, d_from.strftime(DATE_FMT))
        type_value(i_to, d_to.strftime(DATE_FMT))
    else:
        log("  ! 진행일자 입력칸을 찾지 못함 (날짜 조건 미적용)")

    i_name = input_after_label(page, NAME_LABELS, 1)
    if not i_name:
        raise RuntimeError("사업명 입력칸을 찾지 못했습니다. NAME_LABELS를 확인하세요.")
    type_value(i_name, keyword)


def run_search(page: Page) -> None:
    for t in SEARCH_BUTTON_TEXTS:
        if click_text(page, t):
            break
    else:
        raise RuntimeError("검색 버튼을 찾지 못했습니다.")
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:  # noqa: BLE001
        pass
    page.wait_for_timeout(int(PAUSE * 1000))


def go_next(page: Page, current: int) -> bool:
    # 1) 다음 번호 링크  2) '다음' 버튼 (페이지 그룹 이동)
    el = first_visible(page.get_by_role("link", name=str(current + 1), exact=True))
    if el:
        el.click()
        page.wait_for_timeout(int(PAUSE * 1000))
        return True
    for sel in NEXT_PAGE_SELECTORS:
        el = first_visible(page.locator(sel))
        if el:
            cls = el.get_attribute("class") or ""
            if "disabled" in cls or not el.is_enabled():
                continue
            el.click()
            page.wait_for_timeout(int(PAUSE * 1000))
            return True
    return False


# ======================= 수집 =======================
def collect_pages(page: Page, keyword: str) -> list[dict]:
    kw = norm(keyword)
    out: list[dict] = []
    seen_sig: set[str] = set()
    for p in range(1, MAX_PAGES + 1):
        data = page.evaluate(EXTRACT_JS)
        rows, heads = data["rows"], list(dict.fromkeys(data["headers"]))
        sig = json.dumps(rows[:3], ensure_ascii=False)
        if not rows or sig in seen_sig:
            break
        seen_sig.add(sig)

        for cells in rows:
            if kw not in norm(" ".join(cells)):  # 서버 필터 누락 방지: 키워드 재검증
                continue
            rec = (
                dict(zip(heads, cells))
                if len(heads) == len(cells)
                else {f"col{i + 1}": v for i, v in enumerate(cells)}
            )
            out.append(rec)
        log(f"  - {p}페이지: 행 {len(rows)}개 중 누적 매칭 {len(out)}건")
        if not go_next(page, p):
            break
    return out


def record_key(rec: dict) -> str:
    vals = [str(v) for v in rec.values()]
    if vals and vals[0].isdigit():  # 순번 열 제외
        vals = vals[1:]
    return "|".join(vals)


# ======================= 메인 =======================
def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="나라장터 발주계획 크롤러")
    ap.add_argument("--headed", action="store_true", help="브라우저 창 표시")
    ap.add_argument("--debug", action="store_true", help="단계별 스크린샷/HTML 저장")
    ap.add_argument("--dump-xhr", action="store_true", help="JSON 응답(XHR) 저장")
    ap.add_argument("--keywords", nargs="+", default=KEYWORDS, help="사업명 검색어(복수)")
    ap.add_argument("--months", type=int, default=MONTHS, help="오늘부터 N개월")
    ap.add_argument("--start-url", default=None, help="발주게시판 URL을 알고 있으면 직접 이동")
    ap.add_argument("--out", default="output", help="결과 폴더")
    ap.add_argument("--slow-mo", type=int, default=0, help="동작 지연(ms), 디버깅용")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    today = date.today()
    d_from, d_to = today, add_months(today, args.months)
    log(f"조건: {SEARCH_TYPE} / {BUSINESS_TYPE} / 진행일자 {d_from} ~ {d_to} / 검색어 {args.keywords}")

    merged: dict[str, dict] = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed, slow_mo=args.slow_mo)
        ctx = browser.new_context(locale="ko-KR", viewport={"width": 1600, "height": 1000})
        page = ctx.new_page()
        page.set_default_timeout(15000)
        if args.dump_xhr:
            attach_xhr_dump(page, out / "xhr")

        try:
            open_board(page, args.start_url)
            snap(page, out / "debug", "01_board", args.debug)

            for idx, kw in enumerate(args.keywords, 1):
                log(f"[{idx}/{len(args.keywords)}] 검색어: '{kw}'")
                apply_filters(page, kw, d_from, d_to)
                snap(page, out / "debug", f"02_filters_{idx}", args.debug)
                run_search(page)
                snap(page, out / "debug", f"03_result_{idx}", args.debug)

                for rec in collect_pages(page, kw):
                    key = record_key(rec)
                    if key in merged:
                        merged[key]["검색어"].append(kw)
                    else:
                        rec["검색어"] = [kw]
                        merged[key] = rec
                time.sleep(PAUSE)
        except Exception as e:  # noqa: BLE001
            log(f"오류: {e}")
            snap(page, out / "debug", "error", True)
            browser.close()
            return 2
        browser.close()

    records = list(merged.values())
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    for r in records:
        r["검색어"] = ", ".join(dict.fromkeys(r["검색어"]))
        r["수집일시"] = datetime.now().strftime("%Y-%m-%d %H:%M")

    json_path = out / f"g2b_orderplan_{stamp}.json"
    json_path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")

    if records:
        fields = list(dict.fromkeys(k for r in records for k in r))
        csv_path = out / f"g2b_orderplan_{stamp}.csv"
        with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(records)
        log(f"완료: 중복 제거 후 {len(records)}건 → {csv_path}")
        for r in records[:20]:
            title = next((v for k, v in r.items() if "사업명" in k or "계획명" in k), None)
            log(f"  · {title or list(r.values())[:4]}")
    else:
        log("완료: 조건에 맞는 건이 없습니다. (--headed --debug로 필터 적용 여부를 확인하세요)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
