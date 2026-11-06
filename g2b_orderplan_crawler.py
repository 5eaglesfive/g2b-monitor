#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
나라장터(https://www.g2b.go.kr) 발주 > 발주목록 크롤러

조건
  - 검색유형 : 발주계획
  - 업무구분 : 일반용역   (상세조건 영역의 체크박스)
  - 사업명   : "스마트경로당" "스마트 경로당" "경로당" "경로당 구축" (각각 검색 후 병합·중복제거)
  - 진행일자 : 오늘 포함 최근 6개월 (예: 오늘이 2026-11-06이면 2026/05/07 ~ 2026/11/06)
  - (선택) --order-months N : 결과의 '발주시기'가 이번 달부터 N개월 이내인 건만 추가로 남김

설치
  pip install playwright
  playwright install chromium

실행
  python g2b_orderplan_crawler.py --headed --debug   # 화면 보며 확인
  python g2b_orderplan_crawler.py                    # 백그라운드 실행
  python g2b_orderplan_crawler.py --inspect          # 입력요소 구조만 저장(개편 대응용)
  python g2b_orderplan_crawler.py --fast             # 포함관계 검색어(경로당 ⊃ 스마트경로당 …)는 1회만 검색
  python g2b_orderplan_crawler.py --send-mail        # 수집 후 Gmail 발송 (크롤링 이후에만 실행, 옵션 없으면 발송 안 함)
      # 환경변수: GMAIL_USER, GMAIL_APP_PASSWORD, MAIL_TO  /  --seen-file state/seen_plans.json 이면 신규 건만 발송

※ 선택자는 2026-11 화면(업로드 HTML)에서 확인한 id 기준입니다. 개편되면 [화면 설정]만 수정하세요.
※ 진행일자는 화면의 1/3/6개월 버튼을 누르고, 결과가 기대와 다르면 8자리 숫자(YYYYMMDD)로 직접 입력합니다.
"""
from __future__ import annotations

import argparse
import calendar
import csv
import html
import io
import json
import os
import re
import smtplib
import ssl
import sys
import time
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

# ======================= 검색 조건 =======================
SEARCH_TYPE = "발주계획"
BUSINESS_TYPE = "일반용역"
KEYWORDS = ["스마트경로당", "스마트 경로당", "경로당", "경로당 구축"]
PERIOD_MONTHS = 6          # 진행일자: 오늘 포함 최근 N개월
PAGE_SIZE = "100"          # 화면의 표시 건수 (10/30/50/100)

# ======================= 화면 설정 (개편 시 여기만 수정) =======================
BASE_URL = "https://www.g2b.go.kr/"
MENU_PATH = ["발주", "발주목록"]
SUB_MENU_NAMES = ["발주목록", "발주게시판"]

SEL = {
    "search_type_prefix": "mf_wfm_container_radSrchTy_input_",       # 라디오: 발주계획 등
    "biz_type_prefix": "mf_wfm_container_chkRqdcBsneSeCd_input_",    # 체크박스: 업무구분
    "period_radio": 'input[id*="btnMnth_input_"]',                   # 라디오: 1/3/6개월
    "date_from": 'input[id$="ibxStrDay"]',
    "date_to": 'input[id$="ibxEndDay"]',
    "biz_name": "#mf_wfm_container_txtBizNm",                        # 사업명 입력
    "search_btn": "#mf_wfm_container_btnS0001",                      # 검색 버튼
    "detail_toggle": 'input[id$="btnSearchToggle"]',                 # 상세조건 열기/닫기
    "page_size": "#mf_wfm_container_sbxRecordCountPerPage",
    "page_apply": "#mf_wfm_container_btnAply",
    "pager": "#mf_wfm_container_pagelist",
    "pager_next": "#mf_wfm_container_pagelist_next_btn a",
    "popup_close": "button.w2window_close, input.w2trigger.btn_cm.close",
}

# 결과 그리드 col_id → 출력 컬럼명
COLUMNS = [
    ("oderPlanNo", "발주계획번호"),
    ("prcmBsneSeNm", "업무구분"),
    ("pbancSeYnNm", "내외자"),
    ("bizNm", "사업명"),
    ("oderInstUntyGrpNm", "수요기관"),
    ("tkcgDeptNm", "담당부서"),
    ("picNm", "담당자"),
    ("prcsYmd", "진행일자"),
    ("oderPlanPgstNm", "진행상태"),
    ("oderYymm", "발주시기"),
    ("bgtSumAmt", "예산금액(원)"),
    ("bfSpecRfrnYn", "사전규격참조"),
]

MAX_PAGES = 200
PAUSE = 0.8  # 동작 간 대기(초)

GRID_JS = r"""
() => {
  const re = /^mf_wfm_container_gridView1_cell_(\d+)_(\d+)$/;
  const rows = {};
  document.querySelectorAll('td[col_id]').forEach(td => {
    const m = re.exec(td.id || '');
    if (!m) return;
    const r = (rows[m[1]] = rows[m[1]] || {});
    r[td.getAttribute('col_id')] = (td.innerText || '').replace(/\s+/g, ' ').trim();
  });
  return Object.keys(rows).map(Number).sort((a, b) => a - b).map(k => rows[k]);
}
"""

INSPECT_JS = r"""
() => {
  const vis = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const clean = t => (t || '').replace(/\s+/g, ' ').trim().slice(0, 80);
  const out = [];
  document.querySelectorAll(
    'input, select, textarea, button, label, a, [role="tab"], [role="radio"], [role="checkbox"]'
  ).forEach(el => {
    if (el.closest('[class*="w2grid"]')) return;
    const r = el.getBoundingClientRect();
    out.push({
      tag: el.tagName.toLowerCase(), type: el.type || '', id: el.id || '',
      cls: (el.className && el.className.toString ? el.className.toString() : '').slice(0, 80),
      text: clean(el.innerText || el.value || ''), title: el.title || '',
      forAttr: el.htmlFor || '', checked: !!el.checked, visible: vis(el),
      x: Math.round(r.x), y: Math.round(r.y)
    });
  });
  return out.filter(o => o.visible || o.type === 'radio' || o.type === 'checkbox')
            .sort((a, b) => (a.y - b.y) || (a.x - b.x));
}
"""


# ======================= 유틸 =======================
_T0 = time.time()


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')} +{int(time.time() - _T0)}s] {msg}", flush=True)


def norm(s: str) -> str:
    """공백 제거 정규화 ('스마트 경로당' == '스마트경로당')"""
    return re.sub(r"\s+", "", s or "")


def minimal_keywords(kws: list[str]) -> list[str]:
    """다른 검색어를 포함하는 검색어는 제외 (예: '경로당'이 있으면 '스마트경로당'은 별도 검색 불필요)"""
    out: list[str] = []
    for k in kws:
        nk = norm(k)
        if any(norm(o) != nk and norm(o) in nk for o in kws):
            continue
        if any(norm(o) == nk for o in out):
            continue
        out.append(k)
    return out


def first_visible(loc):
    try:
        for i in range(loc.count()):
            el = loc.nth(i)
            if el.is_visible():
                return el
    except Exception:  # noqa: BLE001
        pass
    return None


def safe_click(el) -> bool:
    if el is None:
        return False
    for fn in (
        lambda: el.click(timeout=3000),
        lambda: el.click(timeout=2000, force=True),
        lambda: el.evaluate("e => e.click()"),
    ):
        try:
            fn()
            return True
        except Exception:  # noqa: BLE001
            continue
    return False


def settle(page: Page, extra: float = PAUSE) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=4000)
    except Exception:  # noqa: BLE001
        pass
    page.wait_for_timeout(int(extra * 1000))


def snap(page: Page, out: Path, name: str, enabled: bool) -> None:
    if not enabled:
        return
    out.mkdir(parents=True, exist_ok=True)
    try:
        page.screenshot(path=str(out / f"{name}.png"), full_page=True)
        (out / f"{name}.html").write_text(page.content(), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        log(f"스냅샷 실패({name}): {e}")


def describe_page(page: Page) -> str:
    try:
        body = page.evaluate("() => (document.body ? document.body.innerText : '')")
    except Exception:  # noqa: BLE001
        body = ""
    flat = re.sub(r"\s+", " ", body)
    log(f"  페이지 제목: {page.title()!r} / URL: {page.url}")
    log(f"  본문 앞부분: {flat[:300]!r}")
    return flat


def inspect_form(page: Page, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    try:
        items = page.evaluate(INSPECT_JS)
        (out / "form_inspect.json").write_text(
            json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        log(f"  form_inspect.json 저장 ({len(items)}개 요소)")
    except Exception as e:  # noqa: BLE001
        log(f"  form_inspect 실패: {e}")


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
def close_popups(page: Page, rounds: int = 4) -> int:
    """공지사항 팝업(여러 개)을 모두 닫는다. 팝업이 클릭을 가로채므로 조건 입력 전에 필수."""
    closed = 0
    for _ in range(rounds):
        n = 0
        els = page.locator(SEL["popup_close"])
        for i in range(els.count()):
            el = els.nth(i)
            try:
                if el.is_visible():
                    el.click(timeout=2000)
                    n += 1
                    page.wait_for_timeout(300)
            except Exception:  # noqa: BLE001
                pass
        closed += n
        if n == 0:
            break
    if closed:
        log(f"  공지 팝업 {closed}개 닫음")
    return closed


def close_popups_until_clear(page: Page, max_s: float = 10, quiet_s: float = 2.0) -> None:
    """공지 팝업은 화면 로딩이 끝난 뒤 늦게 열리므로, 한동안 새 팝업이 없을 때까지 반복해서 닫는다."""
    end = time.time() + max_s
    last = time.time()
    while time.time() < end:
        if close_popups(page):
            last = time.time()
        elif time.time() - last >= quiet_s:
            break
        page.wait_for_timeout(500)


def wait_form_ready(page: Page, timeout_s: float = 60) -> None:
    """검색 화면이 실제로 준비될 때까지 대기: 사업명 입력칸 표시 + 기본 기간 채워짐 + '전체 N건' 표시.
    (준비 전에 조작하면 요소가 안 보이거나 기본값이 비어 있어 클릭/선택이 실패한다)"""
    end = time.time() + timeout_s
    while time.time() < end:
        try:
            box = page.locator(SEL["biz_name"])
            df = page.locator(SEL["date_from"])
            if (box.count() and box.first.is_visible()
                    and df.count() and df.first.input_value()
                    and get_total(page) is not None):
                log(f"  검색 화면 준비 완료 (+{int(time.time() - _T0)}s)")
                return
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(500)
    raise RuntimeError(f"발주목록 검색 화면이 {int(timeout_s)}초 안에 준비되지 않았습니다.")


def find_text(page: Page, text: str, wait_s: float = 20, exact: bool = True):
    end = time.time() + wait_s
    while True:
        for fr in page.frames:
            for loc in (
                fr.get_by_text(text, exact=exact),
                fr.get_by_role("link", name=text),
                fr.get_by_role("button", name=text),
            ):
                el = first_visible(loc)
                if el:
                    return el
        if time.time() >= end:
            return None
        page.wait_for_timeout(1000)


def goto_with_retry(page: Page, url: str, attempts: int = 3, timeout_ms: int = 45000) -> None:
    """접속 실패(시간 초과, chrome-error 페이지)는 일시적일 수 있어 간격을 늘리며 재시도한다."""
    last = ""
    for i in range(1, attempts + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            if not page.url.startswith("chrome-error"):
                return
            last = "브라우저 오류 페이지(chrome-error)"
        except Exception as e:  # noqa: BLE001
            last = str(e).splitlines()[0][:150]
        log(f"  접속 실패 ({i}/{attempts}): {last}")
        if i < attempts:
            page.wait_for_timeout(15000 * i)
    raise RuntimeError(
        f"나라장터({url})에 접속하지 못했습니다: {last}. "
        "사이트 장애이거나 실행 환경(해외 IP)이 제한되었을 수 있습니다. 국내 PC/서버에서 실행해 보세요."
    )


def open_board(page: Page, start_url: str | None) -> None:
    goto_with_retry(page, start_url or BASE_URL)
    settle(page, 3)
    close_popups(page)
    describe_page(page)
    if not start_url:
        top = find_text(page, MENU_PATH[0], 20)
        if not top:
            burger = find_text(page, "전체메뉴", 3)
            if burger:
                burger.click()
                page.wait_for_timeout(800)
                top = find_text(page, MENU_PATH[0], 5)
        if not top:
            raise RuntimeError(f"상단 메뉴 '{MENU_PATH[0]}'를 찾지 못했습니다.")
        top.hover()
        page.wait_for_timeout(600)
        sub = None
        for name in SUB_MENU_NAMES:
            sub = find_text(page, name, 3)
            if sub:
                break
        if not sub:
            top.click()
            page.wait_for_timeout(600)
            for name in SUB_MENU_NAMES:
                sub = find_text(page, name, 5)
                if sub:
                    break
        if not sub:
            raise RuntimeError(f"하위 메뉴 {SUB_MENU_NAMES}를 찾지 못했습니다.")
        sub.click()
    wait_form_ready(page)
    close_popups_until_clear(page)  # 발주목록의 공지 팝업은 화면이 준비된 뒤 열린다


def option_input(page: Page, prefix: str, title: str):
    loc = page.locator(f'input[id^="{prefix}"][title="{title}"]')
    return loc.first if loc.count() else None


def is_checked(inp) -> bool:
    return bool(inp.evaluate("e => e.checked"))


def click_option(page: Page, inp) -> bool:
    """라디오/체크박스는 연결된 label을 클릭해야 화면 로직이 반응한다."""
    iid = inp.get_attribute("id")
    lab = first_visible(page.locator(f'label[for="{iid}"]'))
    if lab and safe_click(lab):
        page.wait_for_timeout(300)
        return True
    return safe_click(inp)


def set_search_type(page: Page) -> None:
    inp = option_input(page, SEL["search_type_prefix"], SEARCH_TYPE)
    if not inp:
        raise RuntimeError(f"검색유형 '{SEARCH_TYPE}' 라디오를 찾지 못했습니다.")
    if not is_checked(inp):
        click_option(page, inp)
    log(f"  검색유형: {SEARCH_TYPE} ({'선택됨' if is_checked(inp) else '선택 실패'})")


def set_business_type(page: Page) -> None:
    prefix = SEL["biz_type_prefix"]
    target = option_input(page, prefix, BUSINESS_TYPE)
    if not target:
        raise RuntimeError(f"업무구분 '{BUSINESS_TYPE}' 체크박스를 찾지 못했습니다.")
    # 상세조건이 접혀 있으면 펼친다
    lab = page.locator(f'label[for="{target.get_attribute("id")}"]')
    if not first_visible(lab):
        safe_click(first_visible(page.locator(SEL["detail_toggle"])))
        page.wait_for_timeout(600)
    for _ in range(2):  # 전체/다른 항목 해제 → 대상만 체크 (2회 확인)
        for inp in page.locator(f'input[id^="{prefix}"]').all():
            want = inp.get_attribute("title") == BUSINESS_TYPE
            if is_checked(inp) != want:
                click_option(page, inp)
    states = {
        inp.get_attribute("title"): is_checked(inp)
        for inp in page.locator(f'input[id^="{prefix}"]').all()
    }
    checked = [k for k, v in states.items() if v]
    log(f"  업무구분 체크 상태: {checked}")
    if checked != [BUSINESS_TYPE]:
        log("  ! 업무구분이 의도와 다르게 선택됨 (결과는 업무구분으로 재필터링됩니다)")


def add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    y += d.year
    m += 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def ym_of(text: str) -> tuple[int, int] | None:
    """'2026/11', '202611', '2026-11-05' 등에서 (연, 월) 추출"""
    m = re.search(r"(\d{4})\D?(\d{1,2})", text or "")
    if not m:
        return None
    y, mo = int(m.group(1)), int(m.group(2))
    return (y, mo) if 1 <= mo <= 12 else None


def type_date(page: Page, selector: str, d: date) -> bool:
    el = page.locator(selector).first
    el.click()
    el.press("Control+A")
    el.type(d.strftime("%Y%m%d"), delay=40)  # maxlength=8, 포커스를 잃으면 YYYY/MM/DD로 표시됨
    el.press("Tab")
    page.wait_for_timeout(400)
    return el.input_value() == d.strftime("%Y/%m/%d")


def set_period(page: Page, months: int) -> str:
    """진행일자: 오늘 포함 최근 N개월. 1/3/6개월은 화면 버튼을 쓰고, 값이 다르면 직접 입력."""
    today = date.today()
    start = add_months(today, -months) + timedelta(days=1)
    want = (start.strftime("%Y/%m/%d"), today.strftime("%Y/%m/%d"))

    def current() -> tuple[str, str]:
        return (
            page.locator(SEL["date_from"]).first.input_value(),
            page.locator(SEL["date_to"]).first.input_value(),
        )

    if months in (1, 3, 6):
        inp = page.locator(f'{SEL["period_radio"]}[title="{months}개월"]')
        if inp.count() and not is_checked(inp.first):
            click_option(page, inp.first)
            page.wait_for_timeout(500)
    if current() != want:
        log("  기간 버튼 결과가 예상과 달라 날짜를 직접 입력합니다.")
        if not (type_date(page, SEL["date_from"], start) and type_date(page, SEL["date_to"], today)):
            log("  ! 진행일자 직접 입력도 확인되지 않았습니다. 아래 표시된 기간을 확인하세요.")
    d1, d2 = current()
    log(f"  진행일자: {d1} ~ {d2} (오늘 포함 최근 {months}개월)")
    return f"{d1} ~ {d2}"


def run_search(page: Page, keyword: str) -> None:
    box = page.locator(SEL["biz_name"]).first
    box.click()
    box.fill(keyword)
    before = (get_total(page), signature(grid_rows(page)))
    btn = first_visible(page.locator(SEL["search_btn"]))
    if not safe_click(btn):
        raise RuntimeError("검색 버튼을 클릭하지 못했습니다.")
    end = time.time() + 8  # 결과가 이전과 같을 수도 있으므로 최대 8초만 기다린다
    while time.time() < end:
        page.wait_for_timeout(300)
        if (get_total(page), signature(grid_rows(page))) != before:
            break
    page.wait_for_timeout(500)


def set_page_size(page: Page) -> None:
    try:
        sel = page.locator(SEL["page_size"]).first
        if sel.input_value() != PAGE_SIZE:
            sel.select_option(label=PAGE_SIZE)
            safe_click(first_visible(page.locator(SEL["page_apply"])))
            settle(page)
    except Exception as e:  # noqa: BLE001
        log(f"  표시 건수 변경 실패(무시): {str(e)[:120]}")


def get_total(page: Page) -> int | None:
    try:
        body = page.evaluate("() => document.body.innerText")
    except Exception:  # noqa: BLE001
        return None
    m = re.search(r"발주요청목록\s*전체\s*([\d,]+)\s*건", body) or re.search(r"전체\s*([\d,]+)\s*건", body)
    return int(m.group(1).replace(",", "")) if m else None


# ======================= 수집 =======================
def grid_rows(page: Page) -> list[dict]:
    rows = page.evaluate(GRID_JS)
    return [r for r in rows if r.get("bizNm") or r.get("oderPlanNo")]


def signature(rows: list[dict]) -> tuple:
    if not rows:
        return ()
    return (rows[0].get("oderPlanNo"), rows[-1].get("oderPlanNo"), len(rows))


def current_page(page: Page) -> int:
    try:
        txt = page.locator(f'{SEL["pager"]} a.w2pageList_label_selected').first.inner_text()
        return int(re.sub(r"\D", "", txt) or 1)
    except Exception:  # noqa: BLE001
        return 1


def go_next(page: Page, cur: int) -> bool:
    a = first_visible(
        page.locator(f'{SEL["pager"]} a.w2pageList_control_label[index="{cur + 1}"]')
    ) or first_visible(page.locator(SEL["pager_next"]))
    return safe_click(a)


def collect_pages(page: Page, expected_total: int | None) -> list[dict]:
    raw: list[dict] = []
    seen: set[tuple] = set()
    for _ in range(MAX_PAGES):
        rows = grid_rows(page)
        sig = signature(rows)
        if not rows or sig in seen:
            break
        seen.add(sig)
        raw.extend(rows)
        cur = current_page(page)
        log(f"  - {cur}페이지: {len(rows)}행 (누적 {len(raw)}/{expected_total or '?'})")
        if expected_total is not None and len(raw) >= expected_total:
            break
        if not go_next(page, cur):
            break
        end = time.time() + 12  # 그리드가 바뀔 때까지 대기
        while time.time() < end and signature(grid_rows(page)) == sig:
            page.wait_for_timeout(400)
    return raw


def to_record(row: dict) -> dict:
    return {label: row.get(col, "") for col, label in COLUMNS}


# ======================= 메일 발송 (Gmail) =======================
MAIL_COLS = ["사업명", "수요기관", "진행일자", "발주시기", "예산금액(원)", "진행상태", "발주계획번호"]
G2B_HOME = "https://www.g2b.go.kr/"


def record_id(r: dict) -> str:
    return r.get("발주계획번호") or f"{r.get('사업명', '')}|{r.get('수요기관', '')}"


def load_seen(path: str) -> set[str]:
    try:
        return set(json.loads(Path(path).read_text(encoding="utf-8")))
    except Exception:  # noqa: BLE001
        return set()


def save_seen(path: str, ids: set[str]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(sorted(ids), ensure_ascii=False, indent=1), encoding="utf-8")


def build_mail(records: list[dict], period: str, order_range: str, keywords: list[str],
               sender: str, to_addrs: list[str]) -> EmailMessage:
    n = len(records)
    today = datetime.now().strftime("%Y-%m-%d")
    subject = (f"[나라장터] {BUSINESS_TYPE} 경로당 {SEARCH_TYPE} "
               f"{'발주예정 ' if order_range else ''}신규 {n}건 ({today})")
    summary = [
        f"검색유형: {SEARCH_TYPE} / 업무구분: {BUSINESS_TYPE}",
        f"진행일자: {period or '-'}",
        *([f"발주시기: {order_range}"] if order_range else []),
        f"사업명 검색어: {', '.join(keywords)}",
        f"신규 건수: {n}건",
    ]
    e = html.escape
    th = "border:1px solid #ccc;padding:4px 8px;background:#f2f4f7;white-space:nowrap"
    td = "border:1px solid #ccc;padding:4px 8px"
    head = "".join(f'<th style="{th}">{e(c)}</th>' for c in MAIL_COLS)
    body_rows = "".join(
        "<tr>" + "".join(f'<td style="{td}">{e(str(r.get(c, "")))}</td>' for c in MAIL_COLS) + "</tr>"
        for r in records
    )
    table = (f'<table style="border-collapse:collapse;font-size:13px"><tr>{head}</tr>{body_rows}</table>'
             if records else "<p>신규 건이 없습니다.</p>")
    html_body = (
        "<div style=\"font-family:'Malgun Gothic',sans-serif;font-size:14px\">"
        + "".join(f"<div>{e(x)}</div>" for x in summary)
        + f"<br>{table}<br>"
        + f'<div>나라장터: <a href="{G2B_HOME}">{G2B_HOME}</a> (발주 &gt; 발주목록에서 발주계획번호로 검색)</div>'
        + "</div>"
    )
    text_lines = summary + [""] + [
        f"- [{r.get('진행일자', '')}] {r.get('사업명', '')} / {r.get('수요기관', '')} ({r.get('발주계획번호', '')})"
        for r in records
    ] + ["", f"나라장터: {G2B_HOME} (발주 > 발주목록에서 발주계획번호로 검색)"]

    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, sender, ", ".join(to_addrs)
    msg.set_content("\n".join(text_lines))
    msg.add_alternative(html_body, subtype="html")
    if records:
        buf = io.StringIO()
        fields = [label for _, label in COLUMNS] + ["검색어", "수집일시"]
        w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(records)
        msg.add_attachment(
            ("\ufeff" + buf.getvalue()).encode("utf-8"),
            maintype="text", subtype="csv",
            filename=f"g2b_orderplan_{datetime.now().strftime('%Y%m%d')}.csv",
        )
    return msg


def send_gmail(msg: EmailMessage) -> None:
    user = os.environ.get("GMAIL_USER", "").strip()
    pw = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "")
    if not user or not pw:
        raise RuntimeError("환경변수 GMAIL_USER / GMAIL_APP_PASSWORD 가 필요합니다.")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=30) as smtp:
        smtp.login(user, pw)
        smtp.send_message(msg)


# ======================= 메인 =======================
def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="나라장터 발주계획 크롤러")
    ap.add_argument("--headed", action="store_true", help="브라우저 창 표시")
    ap.add_argument("--debug", action="store_true", help="단계별 스크린샷/HTML 저장")
    ap.add_argument("--dump-xhr", action="store_true", help="JSON 응답(XHR) 저장")
    ap.add_argument("--inspect", action="store_true", help="입력요소 구조만 저장하고 종료")
    ap.add_argument("--keywords", nargs="+", default=KEYWORDS, help="사업명 검색어(복수)")
    ap.add_argument("--months", type=int, default=PERIOD_MONTHS,
                    help="진행일자: 오늘 포함 최근 N개월 (기본 6)")
    ap.add_argument("--order-months", type=int, default=0,
                    help="(선택) 발주시기가 이번 달부터 N개월 이내인 건만 남김. 0이면 사용 안 함")
    ap.add_argument("--start-url", default=None, help="발주목록 URL을 알면 직접 이동")
    ap.add_argument("--out", default="output", help="결과 폴더")
    ap.add_argument("--slow-mo", type=int, default=0, help="동작 지연(ms), 디버깅용")
    ap.add_argument("--fast", action="store_true",
                    help="다른 검색어를 포함하는 검색어(예: 스마트경로당)는 따로 검색하지 않고 결과에서 대조")
    ap.add_argument("--send-mail", action="store_true", help="수집 결과를 Gmail로 발송")
    ap.add_argument("--to", default=None, help="수신자(쉼표 구분). 미지정 시 MAIL_TO 환경변수")
    ap.add_argument("--seen-file", default=None, help="발송 이력 파일(예: state/seen_plans.json). 지정 시 신규 건만 발송")
    ap.add_argument("--send-empty", action="store_true", help="신규 건이 없어도 '없음' 메일 발송")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    ym_from = ym_to = None
    order_range = ""
    if args.order_months:
        today = date.today()
        end_ym = add_months(today, args.order_months)
        ym_from, ym_to = (today.year, today.month), (end_ym.year, end_ym.month)
        order_range = f"{ym_from[0]}/{ym_from[1]:02d} ~ {ym_to[0]}/{ym_to[1]:02d}"
    log(f"조건: {SEARCH_TYPE} / {BUSINESS_TYPE} / 진행일자 오늘 포함 최근 {args.months}개월"
        + (f" / 발주시기 {order_range}" if order_range else "")
        + f" / 검색어 {args.keywords}")

    merged: dict[str, dict] = {}
    period = ""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=not args.headed,
            slow_mo=args.slow_mo,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(
            locale="ko-KR",
            timezone_id="Asia/Seoul",
            viewport={"width": 1600, "height": 1000},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = ctx.new_page()
        page.set_default_timeout(15000)
        if args.dump_xhr:
            attach_xhr_dump(page, out / "xhr")

        try:
            open_board(page, args.start_url)
            snap(page, out / "debug", "01_board", args.debug)
            if args.inspect:
                inspect_form(page, out / "debug")
                snap(page, out / "debug", "form_inspect", True)
                browser.close()
                return 0

            search_kws = minimal_keywords(args.keywords) if args.fast else list(args.keywords)
            if args.fast:
                log(f"--fast: {search_kws}만 검색하고 나머지 검색어는 결과 사업명과 대조합니다.")
            for idx, kw in enumerate(search_kws, 1):
                log(f"[{idx}/{len(search_kws)}] 검색어: '{kw}'")
                close_popups(page)
                set_search_type(page)
                set_business_type(page)
                period = set_period(page, args.months)
                snap(page, out / "debug", f"02_filters_{idx}", args.debug)
                run_search(page, kw)
                set_page_size(page)
                total = get_total(page)
                log(f"  검색 결과 전체 {total if total is not None else '?'}건")
                snap(page, out / "debug", f"03_result_{idx}", args.debug)

                raw = collect_pages(page, total) if total != 0 else []
                kept = 0
                out_of_range = 0
                for row in raw:
                    # 서버 필터 누락 방지: 업무구분·키워드 재검증
                    if row.get("prcmBsneSeNm") != BUSINESS_TYPE:
                        continue
                    if norm(kw) not in norm(row.get("bizNm", "")):
                        continue
                    if ym_from:  # --order-months 지정 시에만 발주시기 필터 적용
                        ym = ym_of(row.get("oderYymm", ""))
                        if ym is None or not (ym_from <= ym <= ym_to):
                            out_of_range += 1
                            continue
                    matched = [k for k in args.keywords if norm(k) in norm(row.get("bizNm", ""))]
                    key = row.get("oderPlanNo") or "|".join(row.values())
                    rec = to_record(row)
                    if key in merged:
                        merged[key]["검색어"].extend(matched)
                    else:
                        rec["검색어"] = matched
                        merged[key] = rec
                    kept += 1
                log(f"  수집 {len(raw)}행 중 조건 일치 {kept}건"
                    + (f" (발주시기 범위 밖/미기재 {out_of_range}건 제외)" if ym_from else ""))
        except Exception as e:  # noqa: BLE001
            log(f"오류: {str(e)[:400]}")
            describe_page(page)
            snap(page, out / "debug", "error", True)
            inspect_form(page, out / "debug")
            browser.close()
            return 2
        browser.close()

    records = sorted(merged.values(), key=lambda r: r.get("진행일자", ""), reverse=True)
    if order_range:
        records.sort(key=lambda r: (r.get("발주시기", ""), r.get("사업명", "")))
    now = datetime.now()
    for r in records:
        r["검색어"] = ", ".join(dict.fromkeys(r["검색어"]))
        r["수집일시"] = now.strftime("%Y-%m-%d %H:%M")

    stamp = now.strftime("%Y%m%d_%H%M")
    (out / f"g2b_orderplan_{stamp}.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if records:
        fields = [label for _, label in COLUMNS] + ["검색어", "수집일시"]
        csv_path = out / f"g2b_orderplan_{stamp}.csv"
        with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(records)
        log(f"완료: 중복 제거 후 {len(records)}건 → {csv_path}")
        for r in records[:30]:
            log(f"  · [{r['진행일자']}] {r['사업명']} / {r['수요기관']} (발주 {r['발주시기']}, {r['발주계획번호']})")
    else:
        log("완료: 조건에 맞는 건이 없습니다.")

    rc = 0
    if args.send_mail:
        try:
            to_addrs = [a.strip() for a in (args.to or os.environ.get("MAIL_TO", "")).split(",") if a.strip()]
            if not to_addrs:
                raise RuntimeError("수신자가 없습니다. --to 또는 MAIL_TO 환경변수를 지정하세요.")
            seen = load_seen(args.seen_file) if args.seen_file else set()
            fresh = [r for r in records if record_id(r) not in seen]
            log(f"메일 대상: 신규 {len(fresh)}건 (전체 {len(records)}건, 기발송 {len(records) - len(fresh)}건)")
            if fresh or args.send_empty:
                sender = os.environ.get("GMAIL_USER", "").strip()
                send_gmail(build_mail(fresh, period, order_range, args.keywords, sender, to_addrs))
                log(f"메일 발송 완료 → {', '.join(to_addrs)}")
            else:
                log("신규 건이 없어 메일을 발송하지 않습니다.")
            if args.seen_file:  # 발송 성공 후에만 이력 갱신
                save_seen(args.seen_file, seen | {record_id(r) for r in records})
        except Exception as e:  # noqa: BLE001
            log(f"메일 발송 실패: {str(e)[:300]}")
            rc = 3
    log(f"총 소요 {int(time.time() - _T0)}초")
    return rc


if __name__ == "__main__":
    sys.exit(main())
