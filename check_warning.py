#!/usr/bin/env python3
"""투자경고종목 — 다음날 상태 확인 도구.

장 종료 이후 (저녁 9시 등) 실행. 입력: 종목명 한 개. 출력: 멀티라인 텍스트.

흐름:
1. Naver 자동완성 → 종목코드
2. 최근 공시 조회
3. 최신 관련 공시가 "지정해제" 류면 → 해제일 추출 후 표시
4. 최신이 "지정"이면 → 본문에서 판단일 추출
   - 내일 < 판단일: 판단일만 표시
   - 내일 >= 판단일: 가격 데이터로 해제 조건(cond1/2/3) 및 거래정지 임계가 계산
"""

import sys
import json
import re
import html as html_lib
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
CACHE_FILE = Path(__file__).resolve().parent / "stock_codes.json"

TICK_TABLE = [
    (2_000, 1), (5_000, 5), (20_000, 10), (50_000, 50),
    (200_000, 100), (500_000, 500), (float("inf"), 1_000),
]
WEEKDAY_KO = ["월", "화", "수", "목", "금", "토", "일"]


def floor_to_tick(price):
    for limit, tick in TICK_TABLE:
        if price < limit:
            return int(price // tick * tick)
    return int(price)


def ceil_to_tick(price):
    import math
    for limit, tick in TICK_TABLE:
        if price < limit:
            return int(math.ceil(price / tick) * tick)
    return int(price)


import ssl

_SSL_CTX = ssl.create_default_context()
_SSL_CTX_UNVERIFIED = ssl._create_unverified_context()


def _http_get(url):
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=15, context=_SSL_CTX) as r:
            return r.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as e:
        # 사내 프록시 등으로 인증서 체인 검증 실패 시 — 공개 데이터 조회이므로 미검증 재시도
        reason = getattr(e, "reason", None)
        if isinstance(reason, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(e):
            with urllib.request.urlopen(req, timeout=15, context=_SSL_CTX_UNVERIFIED) as r:
                return r.read().decode("utf-8", errors="replace")
        raise


def _load_cache():
    if CACHE_FILE.exists():
        try:
            data = json.loads(CACHE_FILE.read_text())
            return {k: (v if isinstance(v, str) else v.get("code", "")) for k, v in data.items()}
        except Exception:
            return {}
    return {}


def _save_cache(cache):
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2))


def resolve_code(name):
    cache = _load_cache()
    if name in cache and cache[name]:
        return cache[name]
    url = f"https://ac.stock.naver.com/ac?q={quote(name)}&target=stock"
    data = json.loads(_http_get(url))
    items = data.get("items", [])
    if not items:
        raise ValueError(f"종목코드를 찾을 수 없음: {name}")
    code = items[0].get("code")
    if not code:
        raise ValueError(f"코드 추출 실패: {name}")
    cache[name] = code
    _save_cache(cache)
    return code


def prev_trading_day(d):
    d = d - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def next_trading_day(today=None):
    if today is None:
        today = date.today()
    nxt = today + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    return nxt


def is_market_open():
    """Naver Finance API로 현재 정규장 진행 중 여부 판단.
    Returns True if 정규장 중, False otherwise (장전/장후/휴장/주말).
    """
    try:
        url = "https://m.stock.naver.com/api/stock/005930/basic"
        data = json.loads(_http_get(url))
        return data.get("marketStatus") == "OPEN"
    except Exception:
        return False


def auto_basis():
    """가격 데이터가 확정된 마지막 거래일 (= T-1) 자동 결정.
    장중이면 어제(T-1) → T=오늘, 장후면 오늘(T-1) → T=내일.
    """
    today = date.today()
    if is_market_open():
        return prev_trading_day(today)
    return today


def fetch_disclosures(code, size=20):
    url = f"https://m.stock.naver.com/api/stock/{code}/disclosure?page=1&size={size}"
    return json.loads(_http_get(url))


def fetch_disclosure_detail(code, disclosure_id):
    url = f"https://m.stock.naver.com/api/stock/{code}/disclosure/{disclosure_id}"
    data = json.loads(_http_get(url))
    return data["disclosure"]["contents"]


def fetch_daily_prices(code, start, end):
    """일별 종가. 거래정지일(시가/거래량 0) 자동 skip."""
    url = (
        f"https://fchart.stock.naver.com/siseJson.naver?symbol={code}"
        f"&requestType=1&startTime={start.strftime('%Y%m%d')}"
        f"&endTime={end.strftime('%Y%m%d')}&timeframe=day"
    )
    raw = _http_get(url)
    rows = re.findall(r"\[([^\[\]]+)\]", raw)
    out = []
    for row in rows[1:]:
        parts = [p.strip().strip('"').strip("'") for p in row.split(",")]
        if len(parts) < 6:
            continue
        try:
            d = datetime.strptime(parts[0], "%Y%m%d").date()
            open_p = float(parts[1])
            close = int(float(parts[4]))
            volume = float(parts[5])
        except (ValueError, IndexError):
            continue
        if open_p == 0 or volume == 0:
            continue  # 거래정지일 skip
        out.append((d, close))
    out.sort()
    return out


PREFERRED_SUFFIX_RE = re.compile(r"\(([^()]+우[BC]?)\)$")


def is_preferred_stock_name(name):
    """우선주 표기 (이름이 '우', '우B', '우C'로 끝남)."""
    return name.endswith("우") or name.endswith("우B") or name.endswith("우C")


def common_code_for_preferred(preferred_code):
    """우선주 코드 → 보통주 코드 (마지막 자리 → 0). 005935 → 005930."""
    return preferred_code[:-1] + "0"


def disclosure_matches_target(title, target_name):
    """공시 제목이 target_name 종목 대상인지 판정.
    제목 끝에 '(...우)' 표기가 있으면 그 표기가 target_name과 일치할 때만 True.
    없으면 target_name이 보통주일 때만 True."""
    m = PREFERRED_SUFFIX_RE.search(title or "")
    if m:
        return m.group(1) == target_name
    return not is_preferred_stock_name(target_name)


def html_to_text(html_content):
    text = re.sub(r"<[^>]+>", "\n", html_content)
    text = html_lib.unescape(text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n", "\n", text)
    return text


DESIGNATION_RE = re.compile(r"투자경고종목\s*지정(\s*\([^)]+\))?$")


def classify_disclosure(title):
    """Returns: 'release', 'designation', or None.

    Designation requires the title to END with '투자경고종목 지정' (optionally
    followed by a parenthetical like '(재지정)'). This excludes ancillary
    notices such as '매매거래 정지 및 재개(투자경고종목 지정중)' that contain
    the keyword but are not the original designation notice.
    """
    t = title or ""
    if "투자경고종목" not in t and "투자경고" not in t:
        return None
    if "지정해제" in t:
        # 해제 + 재지정 예고는 재지정 트리거(①지정전일·②해제전일·③T-2 +40%)를 별도 계산
        return "release_redesig" if "재지정" in t else "release"
    if "지정예고" in t:
        # 투자경고종목 지정예고 (투자주의 단계 → 투자경고 전환 요건 안내)
        return "predesignation" if "투자경고" in t else None
    if DESIGNATION_RE.search(t):
        return "designation"
    return None


def find_relevant_disclosures(disclosures, cutoff_date=None):
    """Return (latest_release, latest_designation). cutoff_date로 백테스트 시점 필터."""
    latest_release = None
    latest_designation = None
    for d in disclosures:
        if cutoff_date is not None:
            try:
                d_date = datetime.fromisoformat(d["datetime"]).date()
                if d_date > cutoff_date:
                    continue
            except (ValueError, KeyError):
                continue
        cat = classify_disclosure(d.get("title", ""))
        if cat == "release" and latest_release is None:
            latest_release = d
        elif cat == "designation" and latest_designation is None:
            latest_designation = d
        if latest_release and latest_designation:
            break
    return latest_release, latest_designation


def find_halt_preview(disclosures, today, lookback_days=3):
    """매매거래정지 예고 공시 검색. T-1(=today) 전후 lookback_days 내에서."""
    cutoff_start = today - timedelta(days=lookback_days)
    for d in disclosures:
        try:
            d_date = datetime.fromisoformat(d["datetime"]).date()
        except (ValueError, KeyError):
            continue
        if d_date < cutoff_start or d_date > today:
            continue
        title = d.get("title", "") or ""
        if "매매거래정지" in title and "예고" in title:
            return d
    return None


def find_risk_escalation(disclosures, after_date=None, cutoff_date=None):
    """투자위험종목 격상 감지. after_date 이후 발행된 공시 중에서."""
    for d in disclosures:
        try:
            d_date = datetime.fromisoformat(d["datetime"]).date()
        except (ValueError, KeyError):
            continue
        if after_date and d_date <= after_date:
            continue
        if cutoff_date and d_date > cutoff_date:
            continue
        title = d.get("title", "") or ""
        if (
            "투자위험종목" in title
            and "지정" in title
            and "해제" not in title
            and "예고" not in title
        ):
            return d
    return None


def find_share_restructure(disclosures, after_date=None, cutoff_date=None):
    """액면병합/분할/주식병합 등 가격 정합이 깨지는 이벤트 감지."""
    for d in disclosures:
        try:
            d_date = datetime.fromisoformat(d["datetime"]).date()
        except (ValueError, KeyError):
            continue
        if after_date and d_date <= after_date:
            continue
        if cutoff_date and d_date > cutoff_date:
            continue
        title = d.get("title", "") or ""
        if "변경상장" in title and ("액면" in title or "병합" in title or "분할" in title):
            return d
    return None


def parse_korean_date(s, ref_year=None, ref_month=None):
    m = re.search(r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일", s)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일", s)
    if m:
        mon = int(m.group(1))
        day = int(m.group(2))
        yr = ref_year if ref_year else date.today().year
        if ref_year and ref_month and mon < ref_month - 2:
            yr = ref_year + 1
        return date(yr, mon, day)
    return None


def parse_release_notice(text):
    """해제 공시 본문 → 해제일."""
    m = re.search(
        r"지정해제[^|\n]*?(?:예고일|일자|일)[^|\n]*?\|\s*([^\n|]+)",
        text,
    )
    if m:
        d = parse_korean_date(m.group(1))
        if d:
            return d
    m = re.search(r"투자경고종목에서\s*해제되어\s*(\d{4}[.\-/]\s*\d{1,2}[.\-/]\s*\d{1,2})", text)
    if m:
        s = re.sub(r"[.\-/]", "-", m.group(1)).replace(" ", "")
        try:
            return datetime.strptime(s, "%Y-%m-%d").date()
        except ValueError:
            pass
    m = re.search(r"해제되어\s*(\d{4}\.\d{1,2}\.\d{1,2})", text)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y.%m.%d").date()
        except ValueError:
            pass
    m = re.search(r"해제[^\n]*?(\d{4}\s*년\s*\d{1,2}\s*월\s*\d{1,2}\s*일)", text)
    if m:
        return parse_korean_date(m.group(1))
    return None


def parse_designation_notice(text):
    """지정 공시 본문 → 지정일, 최초 판단일, pct1, pct2, no_halt."""
    out = {
        "지정일": None,
        "최초_판단일": None,
        "pct1": None,
        "pct2": None,
        "no_halt": False,
    }

    m = re.search(r"2\.\s*지정일[^|]*\|\s*([^\n|]+)", text)
    if m:
        out["지정일"] = parse_korean_date(m.group(1))

    ref_year = out["지정일"].year if out["지정일"] else None
    ref_month = out["지정일"].month if out["지정일"] else None
    m = re.search(r"최초\s*판단일[^\d]*((?:\d{4}\s*년\s*)?\d{1,2}\s*월\s*\d{1,2}\s*일)", text)
    if m:
        out["최초_판단일"] = parse_korean_date(m.group(1), ref_year, ref_month)

    m = re.search(r"5일\s*전날[^%]*?(\d+)\s*%\s*이상\s*상승", text)
    if m:
        out["pct1"] = int(m.group(1)) / 100

    m = re.search(r"15일\s*전날[^%]*?(\d+)\s*%\s*이상\s*상승", text)
    if m:
        out["pct2"] = int(m.group(1)) / 100

    if "별도의 매매거래정지" in text:
        out["no_halt"] = True

    return out


def calculate_release_scenario(prices, conditions, today_close):
    """오늘 종가 + 가격 데이터 → cond1/2/3 + 거래정지 임계."""
    closes = prices
    if len(closes) < 15:
        raise ValueError(f"가격 데이터 부족 ({len(closes)}일)")

    t_minus_1 = closes[-1]
    t_minus_2 = closes[-2]
    t_minus_5 = closes[-5]
    t_minus_15 = closes[-15]
    last_14 = closes[-14:]  # T-1 ~ T-14 (T 포함 15일에서 T 제외)
    max_15 = max(c for _, c in last_14)
    max_15_date = next(d for d, c in last_14 if c == max_15)

    pct1 = conditions["pct1"] if conditions["pct1"] is not None else 0.45
    pct2 = conditions["pct2"] if conditions["pct2"] is not None else 0.75

    cond1 = t_minus_5[1] * (1 + pct1)
    cond2 = t_minus_15[1] * (1 + pct2)
    cond3 = max_15
    binding = min(cond1, cond2, cond3)

    halt = None
    if not conditions.get("no_halt"):
        halt_price = t_minus_2[1] * 1.4
        sanghan = floor_to_tick(today_close * 1.3)
        halt = {
            "price": halt_price,
            "sanghan": sanghan,
            "reachable": halt_price <= sanghan,
        }

    return {
        "today_close": today_close,
        "t_minus_1": t_minus_1,
        "t_minus_2": t_minus_2,
        "t_minus_5": t_minus_5,
        "t_minus_15": t_minus_15,
        "max_15": max_15,
        "max_15_date": max_15_date,
        "cond1": cond1,
        "cond2": cond2,
        "cond3": cond3,
        "binding": binding,
        "halt": halt,
    }


def _fmt_date(d):
    return f"{d.strftime('%Y-%m-%d')} ({WEEKDAY_KO[d.weekday()]})"


def check_warning_stock(name, today=None):
    """메인 진입점. 종목명 → 멀티라인 문자열."""
    lines = []
    if today is None:
        today = date.today()
    tomorrow = next_trading_day(today)

    code = resolve_code(name)
    is_preferred = is_preferred_stock_name(name)
    if is_preferred:
        disclosure_code = common_code_for_preferred(code)
        price_code = code
    else:
        disclosure_code = code
        price_code = code

    lines.append(f"종목명: {name}")
    if is_preferred:
        lines.append(f"종목코드: {code} (우선주)  | 공시조회: {disclosure_code} (보통주)")
    else:
        lines.append(f"종목코드: {code}")
    lines.append(f"실행일: {_fmt_date(today)}")
    lines.append(f"다음 거래일: {_fmt_date(tomorrow)}")

    disclosures = fetch_disclosures(disclosure_code)
    # 우선주/보통주 구분: 제목 끝 (XXX우) 표기로 필터
    disclosures = [d for d in disclosures if disclosure_matches_target(d.get("title", ""), name)]
    rel, des = find_relevant_disclosures(disclosures, cutoff_date=today)
    pre = find_latest_predesignation(disclosures, cutoff_date=today)
    redesig = find_latest_by_class(disclosures, "release_redesig", cutoff_date=today)

    if not rel and not des and not pre and not redesig:
        lines.append("")
        lines.append("상태: 투자경고 관련 공시 없음")
        return "\n".join(lines)

    # 가장 최신 관련 공시 종류로 분기
    present = [
        (d, k) for d, k in (
            (rel, "release"), (des, "designation"),
            (pre, "predesignation"), (redesig, "release_redesig"),
        ) if d
    ]
    newest_kind = max(present, key=lambda x: _disc_datetime(x[0]))[1]
    if newest_kind == "predesignation":
        rep = _predesignation_report(
            name, code, price_code, disclosure_code, pre, today, tomorrow, disclosures
        )
        if rep is None:  # 판단기간 만료 → 공시 없는 종목과 동일 출력
            lines.append("")
            lines.append("상태: 투자경고 관련 공시 없음")
        else:
            lines += rep
        return "\n".join(lines)
    if newest_kind == "release_redesig":
        rep = _redesignation_report(
            name, code, price_code, disclosure_code, redesig, today, tomorrow, disclosures
        )
        if rep is None:  # 재지정 판단기간 만료 → 공시 없는 종목과 동일 출력
            lines.append("")
            lines.append("상태: 투자경고 관련 공시 없음")
        else:
            lines += rep
        return "\n".join(lines)

    latest_is_release = False
    if rel and des:
        rel_dt = datetime.fromisoformat(rel["datetime"])
        des_dt = datetime.fromisoformat(des["datetime"])
        latest_is_release = rel_dt > des_dt
    elif rel:
        latest_is_release = True

    if latest_is_release:
        contents = fetch_disclosure_detail(disclosure_code, rel["disclosureId"])
        text = html_to_text(contents)
        release_date = parse_release_notice(text)

        lines.append("")
        lines.append(f"최신 공시: {rel['title']}")
        lines.append(f"공시일시: {rel['datetime']}")
        is_preview = "예고" in (rel.get("title") or "")
        if release_date:
            lines.append(f"해제일: {_fmt_date(release_date)}")
            if release_date <= today:
                lines.append(f"상태: 이미 해제 상태 ({_fmt_date(release_date)} 해제)")
                if is_preview:
                    lines.append("주의: 예고 공시 — 이후 재지정 가능성 확인 필요")
            elif release_date <= tomorrow:
                lines.append(f"상태: 내일 {_fmt_date(tomorrow)}부터 투자경고 해제")
            else:
                lines.append(f"상태: {_fmt_date(release_date)} 해제 예정 (아직 경고 상태)")
        else:
            lines.append("상태: 해제 공시 발견 (해제일 파싱 실패 — 본문 수동 확인 필요)")
        return "\n".join(lines)

    # 지정 공시가 최신
    contents = fetch_disclosure_detail(disclosure_code, des["disclosureId"])
    text = html_to_text(contents)
    parsed = parse_designation_notice(text)

    # 투자위험 격상 감지 (지정 이후로)
    des_date = datetime.fromisoformat(des["datetime"]).date()
    risk_esc = find_risk_escalation(disclosures, after_date=des_date, cutoff_date=today)
    if risk_esc:
        lines.append("")
        lines.append(f"⚠ 투자위험종목 격상 감지: {risk_esc['datetime'][:10]} {risk_esc['title']}")
        lines.append("→ 투자경고 분석은 무효. 투자위험종목 별도 처리 필요.")
        return "\n".join(lines)

    # 액면병합/분할 등 가격 정합 깨지는 이벤트 감지
    restructure = find_share_restructure(disclosures, after_date=des_date, cutoff_date=today)
    if restructure:
        lines.append("")
        lines.append(f"⚠ 주식 구조변경(액면병합/분할 등) 감지: {restructure['datetime'][:10]} {restructure['title']}")
        lines.append("→ 변경상장 전후 가격이 fchart에 액면조정 없이 들어있어 시계열 비교 무효.")
        return "\n".join(lines)

    lines.append("")
    lines.append(f"최신 공시: {des['title']}")
    lines.append(f"공시일시: {des['datetime']}")
    if parsed["지정일"]:
        lines.append(f"지정일: {_fmt_date(parsed['지정일'])}")
    if parsed["최초_판단일"]:
        lines.append(f"최초 판단일: {_fmt_date(parsed['최초_판단일'])}")
    if parsed["pct1"] is not None and parsed["pct2"] is not None:
        lines.append(
            f"해제요건 임계: T-5 +{int(parsed['pct1']*100)}% / "
            f"T-15 +{int(parsed['pct2']*100)}%"
        )
    lines.append(f"매매거래정지: {'없음' if parsed['no_halt'] else '있음'}")

    pandanil = parsed["최초_판단일"]
    if pandanil is None:
        lines.append("")
        lines.append("판단일 파싱 실패 — 공시 본문 수동 확인 필요")
        return "\n".join(lines)

    if tomorrow < pandanil:
        lines.append("")
        lines.append(
            f"상태: 아직 판단일 도달 전 (다음 거래일 {tomorrow} < 최초 판단일 {pandanil})"
        )
        return "\n".join(lines)

    if parsed["pct1"] is None or parsed["pct2"] is None:
        lines.append("")
        lines.append("해제요건 % 파싱 실패 — cond1/cond2 계산 불가")
        return "\n".join(lines)

    start = (parsed["지정일"] or today) - timedelta(days=60)
    prices = fetch_daily_prices(price_code, start, today)
    if not prices:
        lines.append("")
        lines.append("가격 데이터 조회 실패")
        return "\n".join(lines)

    today_close = prices[-1][1]
    calc = calculate_release_scenario(prices, parsed, today_close)

    lines.append("")
    lines.append("=== 해제 시나리오 (판단일 진행 중) ===")
    lines.append(f"최근 거래일 종가 (T-1): {calc['today_close']:,}원  [{calc['t_minus_1'][0]}]")
    lines.append(f"T-2 종가: {calc['t_minus_2'][1]:,}원  [{calc['t_minus_2'][0]}]")
    lines.append(f"T-5 종가: {calc['t_minus_5'][1]:,}원  [{calc['t_minus_5'][0]}]")
    lines.append(f"T-15 종가: {calc['t_minus_15'][1]:,}원  [{calc['t_minus_15'][0]}]")
    lines.append(f"최근 15일 최고: {calc['max_15']:,}원  [{calc['max_15_date']}]")
    lines.append("")
    lines.append(f"cond1 (T-5 × {1+parsed['pct1']:.2f}): {calc['cond1']:,.0f}원")
    lines.append(f"cond2 (T-15 × {1+parsed['pct2']:.2f}): {calc['cond2']:,.0f}원")
    lines.append(f"cond3 (15일 최고가): {calc['cond3']:,}원")
    binding_kind = ["cond1", "cond2", "cond3"][
        [calc["cond1"], calc["cond2"], calc["cond3"]].index(calc["binding"])
    ]
    lines.append(f"해제 임계가 ({binding_kind}): {calc['binding']:,.0f}원")

    pct_chg = (calc["binding"] / today_close - 1) * 100
    lines.append("")
    if calc["binding"] == today_close:
        lines.append(f"다음 종가 해제 조건: T-1({today_close:,}원)보다 낮게 마감")
    elif calc["binding"] > today_close:
        lines.append(
            f"다음 종가 해제 조건: < {calc['binding']:,.0f}원 ({pct_chg:+.1f}% 미만 상승)"
        )
    else:
        lines.append(
            f"다음 종가 해제 조건: < {calc['binding']:,.0f}원 ({pct_chg:+.1f}%, T-1보다 하락 필요)"
        )

    if calc["halt"]:
        halt_preview = find_halt_preview(disclosures, today)
        if not halt_preview:
            lines.append("거래정지 위험: 없음 (매매거래정지 예고 공시 없음)")
        else:
            h = calc["halt"]
            halt_pct = (h["price"] / today_close - 1) * 100
            lines.append(
                f"매매거래정지 예고: {halt_preview['datetime'][:10]} 발행"
            )
            if h["reachable"]:
                lines.append(
                    f"⚠ 거래정지 트리거: 종가 ≥ {h['price']:,.0f}원 ({halt_pct:+.1f}%) "
                    f"[상한가 {h['sanghan']:,}원]"
                )
                effective = min(calc["binding"], h["price"])
                effective_pct = (effective / today_close - 1) * 100
                lines.append(
                    f"실효 해제 임계가 (보수): < {effective:,.0f}원 ({effective_pct:+.1f}% 미만)"
                )
            else:
                lines.append(
                    f"거래정지 트리거: {h['price']:,.0f}원 ({halt_pct:+.1f}%) — "
                    f"상한가({h['sanghan']:,}원) 초과로 도달 불가"
                )

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────
# 투자경고종목 "지정예고" → "지정" 전환 요건 분석
# (투자주의 단계에서, 판단일 T에 어떤 종가를 찍으면 투자경고로 지정되는지)
# ─────────────────────────────────────────────────────────────────────────

PRE_ANCHOR_RE = re.compile(r"①\s*판단일")


def _disc_datetime(d):
    """공시 datetime 문자열 파싱 (ISO 또는 MM/DD/YYYY 형식 모두 허용)."""
    s = d.get("datetime", "") or ""
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        for fmt in ("%m/%d/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y.%m.%d %H:%M:%S"):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
    raise ValueError(f"datetime 파싱 실패: {s}")


def fetch_market(name, code):
    """ac 자동완성에서 시장 구분(KOSPI/KOSDAQ) 추출. 실패 시 None."""
    try:
        data = json.loads(_http_get(f"https://ac.stock.naver.com/ac?q={quote(name)}&target=stock"))
        for it in data.get("items", []):
            if it.get("code") == code:
                tc = (it.get("typeCode") or "").upper()
                if "KOSPI" in tc:
                    return "KOSPI"
                if "KOSDAQ" in tc:
                    return "KOSDAQ"
    except Exception:
        pass
    return None


def fetch_market_cap(code):
    """m.stock integration → (시총 문자열, 억원 정수). 실패 시 (None, None)."""
    try:
        data = json.loads(_http_get(f"https://m.stock.naver.com/api/stock/{code}/integration"))
        for info in data.get("totalInfos", []):
            if info.get("code") == "marketValue":
                raw = info.get("value", "") or ""
                eok = 0
                m_jo = re.search(r"([\d,]+)\s*조", raw)
                m_eok = re.search(r"([\d,]+)\s*억", raw)
                if m_jo:
                    eok += int(m_jo.group(1).replace(",", "")) * 10000
                if m_eok:
                    eok += int(m_eok.group(1).replace(",", ""))
                return raw, (eok or None)
    except Exception:
        pass
    return None, None


def find_latest_predesignation(disclosures, cutoff_date=None):
    """가장 최신 '투자경고종목 지정예고' 공시. cutoff_date로 백테스트 시점 필터."""
    return find_latest_by_class(disclosures, "predesignation", cutoff_date)


def find_latest_by_class(disclosures, klass, cutoff_date=None):
    """주어진 classify_disclosure 분류의 가장 최신 공시. cutoff_date로 시점 필터."""
    for d in disclosures:
        if cutoff_date is not None:
            try:
                if _disc_datetime(d).date() > cutoff_date:
                    continue
            except ValueError:
                continue
        if classify_disclosure(d.get("title", "")) == klass:
            return d
    return None


def parse_redesignation_notice(text):
    """'지정해제 및 재지정 예고' 본문 → 해제일/판단일/순연 + 재지정 기준일·조건.

    재지정 요건(모두 충족 시 다음날 재지정):
      ① T종가 > 지정 전일 종가   ② T종가 > 해제 전일 종가
      ③ T종가 ≥ T-2 종가 × (1+pct)   ④ 시총 100위 밖
    """
    out = {
        "해제일": None, "최초_판단일": None, "순연_마감일": None,
        "지정전일": None, "해제전일": None, "t2_pct": None, "needs_rank": "100위" in text,
    }

    m = re.search(r"해제되어\s*(\d{4})[.\-/]\s*(\d{1,2})[.\-/]\s*(\d{1,2})", text)
    if m:
        out["해제일"] = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    else:
        m = re.search(r"재지정\s*예고일[^\d]*((?:\d{4}\s*년\s*)?\d{1,2}\s*월\s*\d{1,2}\s*일)", text)
        if m:
            out["해제일"] = parse_korean_date(m.group(1))
    ref_y = out["해제일"].year if out["해제일"] else None
    ref_m = out["해제일"].month if out["해제일"] else None

    m = re.search(r"최초\s*판단일은?\s*((?:\d{4}\s*년\s*)?\d{1,2}\s*월\s*\d{1,2}\s*일)", text)
    if m:
        out["최초_판단일"] = parse_korean_date(m.group(1), ref_y, ref_m)

    m = re.search(r"\(((?:\d{4}\s*년\s*)?\d{1,2}\s*월\s*\d{1,2}\s*일)\s*까지\)", text)
    if m:
        out["순연_마감일"] = parse_korean_date(m.group(1), ref_y, ref_m)

    m = re.search(r"지정\s*전일\s*\(\s*((?:\d{4}\s*년\s*)?\d{1,2}\s*월\s*\d{1,2}\s*일)\s*\)", text)
    if m:
        out["지정전일"] = parse_korean_date(m.group(1), ref_y, ref_m)

    m = re.search(r"해제\s*전일\s*\(\s*((?:\d{4}\s*년\s*)?\d{1,2}\s*월\s*\d{1,2}\s*일)\s*\)", text)
    if m:
        out["해제전일"] = parse_korean_date(m.group(1), ref_y, ref_m)

    m = re.search(r"2일\s*전일\s*\(?\s*T-?\s*2\s*\)?\s*종가보다\s*(\d+)\s*%", text)
    if m:
        out["t2_pct"] = int(m.group(1)) / 100
    return out


def _redesignation_report(name, code, price_code, disclosure_code, redesig, today, tomorrow, disclosures):
    """'지정해제 및 재지정 예고' → 해제 안내 + 재지정 트리거 가격 리포트."""
    text = html_to_text(fetch_disclosure_detail(disclosure_code, redesig["disclosureId"]))
    p = parse_redesignation_notice(text)

    # 재지정 판단기간(순연 마감)이 지난 옛 공시 → 비활성: None 반환 → '공시 없음'과 동일 출력
    if p["순연_마감일"] and tomorrow > p["순연_마감일"]:
        return None

    lines = []
    lines.append("")
    lines.append(f"최신 공시: {redesig['title']}")
    lines.append(f"공시일시: {redesig['datetime']}")
    if p["해제일"]:
        lines.append(f"해제일: {_fmt_date(p['해제일'])}")
    if p["최초_판단일"]:
        lines.append(f"재지정 최초 판단일: {_fmt_date(p['최초_판단일'])}")
    if p["순연_마감일"]:
        lines.append(f"재지정 순연 마감: {_fmt_date(p['순연_마감일'])}")
    if p["해제일"]:
        if p["해제일"] <= today:
            rel_state = f"이미 해제 ({_fmt_date(p['해제일'])})"
        elif p["해제일"] <= tomorrow:
            rel_state = f"{_fmt_date(p['해제일'])} 해제 예정"
        else:
            rel_state = f"{_fmt_date(p['해제일'])} 해제 예정"
        lines.append(f"해제 상태: {rel_state} (해제일 당일은 투자주의 1일, 판단일 아님)")
        if p["최초_판단일"]:
            lines.append(
                f"재지정 판단: {_fmt_date(p['해제일'])} 기산 → 첫 판단일 {_fmt_date(p['최초_판단일'])}(해제 익일)"
                f"부터 {_fmt_date(p['순연_마감일']) if p['순연_마감일'] else '순연'}까지"
            )

    if not (p["지정전일"] and p["해제전일"] and p["t2_pct"] is not None and p["최초_판단일"]):
        lines.append("")
        lines.append("재지정 요건 파싱 실패 — 본문 수동 확인 필요")
        return lines

    start = p["지정전일"] - timedelta(days=40)
    prices = fetch_daily_prices(price_code, start, today)
    if len(prices) < 3:
        lines.append("")
        lines.append(f"가격 데이터 부족 ({len(prices)}일)")
        return lines

    def close_on_or_before(d):
        vd = vc = None
        for dd, cc in prices:
            if dd <= d:
                vd, vc = dd, cc
        return vd, vc

    T = max(next_trading_day(prices[-1][0]), p["최초_판단일"])  # 재지정 판단일
    last_date, last_close = prices[-1]
    # 판단일 T가 마지막 확정 종가일에서 몇 거래일 뒤인지
    gap = 0
    d = T
    while d > last_date:
        d = prev_trading_day(d)
        gap += 1
    rel_txt = f"{p['해제일'].month}/{p['해제일'].day}" if p["해제일"] else "해제"
    Tstr = f"{T.month}/{T.day}"

    # 판단일이 2거래일 이상 남았으면(=다음 종가가 판단일이 아니면) 날짜만 안내
    if gap >= 2:
        lines.append("")
        lines.append(f"▶ {_fmt_date(T)} 판단 (현재 {gap}거래일 전 — 구체 트리거 가격은 판단 1거래일 전부터 산출)")
        lines.append("")
        lines.append("=== 안내 문구 (복붙용) ===")
        lines.append(f"{name} : {rel_txt} 투자경고 해제 → {Tstr} 재지정 판단 (구체 트리거는 판단 임박 시 산출)")
        return lines

    # gap == 1: 다음 거래일 종가가 곧 판단일 → 구체 계산 + 상한가 도달 여부
    g_d, g = close_on_or_before(p["지정전일"])      # ① 지정 전일 종가
    r_d, r = close_on_or_before(p["해제전일"])      # ② 해제 전일 종가
    t2_date = prev_trading_day(prev_trading_day(T))
    t2_d, t2c = close_on_or_before(t2_date)
    if None in (g, r, t2c):
        lines.append("")
        lines.append("기준 종가 조회 실패 — 가격 데이터 확인 필요")
        return lines

    pct = p["t2_pct"]
    conds = [
        ("①", f"지정전일({g_d}) {g:,}원 초과", g, True),
        ("②", f"해제전일({r_d}) {r:,}원 초과", r, True),
        ("③", f"T-2({t2_d}) {t2c:,} × {1+pct:.2f}", t2c * (1 + pct), False),
    ]
    trig_sym, trig_desc, trig, trig_strict = max(conds, key=lambda x: x[2])
    cmp = "이상" if not trig_strict else "초과"
    sanghan = floor_to_tick(last_close * 1.3)   # T-1 종가 기준 당일(T) 상한가
    reachable = trig <= sanghan

    lines.append("")
    lines.append(f"판단일(T) = {_fmt_date(T)} (다음 거래일 = 판단일)  |  T-1 종가 {last_close:,}원 [{last_date}]")
    for sym, desc, val, strict in conds:
        lines.append(f"  {sym} {desc} = {val:,.0f}원")
    lines.append(f"  ④ 합산 시총 100위 밖 — 통상 충족 (정밀순위는 KRX)")
    reach_txt = "도달가능" if reachable else "이번 판단일 도달불가 (상한가 초과)"
    lines.append(
        f"  ▶ 재지정 트리거({trig_sym}): 종가 {trig:,.0f}원 {cmp}  "
        f"(T-1 대비 {(trig/last_close-1)*100:+.2f}%)  | {Tstr} 상한가 {sanghan:,}원 → {reach_txt}"
    )

    lines.append("")
    lines.append("=== 안내 문구 (복붙용) ===")
    if reachable:
        lines.append(
            f"{name} : {rel_txt} 투자경고 해제, {Tstr} 종가 {trig:,.0f}원 {cmp} 마감 시 "
            f"투경 재지정 (가격 요건만 — 소수계좌 무관)"
        )
    else:
        lines.append(
            f"{name} : {rel_txt} 투자경고 해제, 재지정 트리거 {trig:,.0f}원이나 "
            f"{Tstr} 상한가({sanghan:,}원) 초과로 {Tstr}엔 재지정 불가 (이후 추가 상승 필요)"
        )
    return lines


def parse_predesignation_notice(text):
    """지정예고 공시 본문 → 예고일/판단일/순연마감 + 지정 경로(paths) 목록.

    paths 각 원소: {kind, pct|excess, cond3, needs_15d_high, needs_rank, label}
      kind: 'short_index5x'(단기급등) | 'short_account'(단기상승·불건전) | 'long'(초장기)
      cond3: 'index5x'(종합주가지수 5배, 계산가능) | 'account'(매수관여율, 비공개)
    """
    out = {"지정예고일": None, "최초_판단일": None, "순연_마감일": None, "paths": []}

    m = re.search(r"지정예고일[^\d]*((?:\d{4}\s*년\s*)?\d{1,2}\s*월\s*\d{1,2}\s*일)", text)
    if m:
        out["지정예고일"] = parse_korean_date(m.group(1))
    ref_year = out["지정예고일"].year if out["지정예고일"] else None
    ref_month = out["지정예고일"].month if out["지정예고일"] else None

    m = re.search(r"최초\s*판단일[^\d]*((?:\d{4}\s*년\s*)?\d{1,2}\s*월\s*\d{1,2}\s*일)", text)
    if m:
        out["최초_판단일"] = parse_korean_date(m.group(1), ref_year, ref_month)

    m = re.search(r"\(((?:\d{4}\s*년\s*)?\d{1,2}\s*월\s*\d{1,2}\s*일)\s*까지\)", text)
    if m:
        out["순연_마감일"] = parse_korean_date(m.group(1), ref_year, ref_month)

    anchors = [mm.start() for mm in PRE_ANCHOR_RE.finditer(text)]
    for i, pos in enumerate(anchors):
        end = anchors[i + 1] if i + 1 < len(anchors) else len(text)
        block = text[pos:end]
        path = {
            "kind": None, "pct": None, "excess": None, "cond3": None,
            "needs_15d_high": "15일" in block, "needs_rank": "100위" in block,
            "label": None,
        }
        m_long = re.search(r"1년간\s*초과\s*주가상승률이?\s*(\d+)\s*%", block)
        m_short = re.search(r"5일\s*전날\s*\(?\s*T-?\s*5\s*\)?\s*의?\s*종가보다\s*(\d+)\s*%", block)
        if m_long:
            pct = int(m_long.group(1))
            path.update(kind="long", excess=pct / 100, cond3="account",
                        label=f"초장기상승·불건전 (1년 초과상승률 {pct}%)")
        elif m_short:
            pct = int(m_short.group(1))
            path["pct"] = pct / 100
            if "5배" in block:
                path.update(kind="short_index5x", cond3="index5x",
                            label=f"단기급등 (T-5 +{pct}%)")
            elif "매수관여율" in block or "특정계좌" in block:
                path.update(kind="short_account", cond3="account",
                            label=f"단기상승·불건전 (T-5 +{pct}%)")
            else:
                path.update(kind="short", cond3=None, label=f"단기 (T-5 +{pct}%)")
        else:
            continue
        out["paths"].append(path)
    return out


def _predesignation_report(name, code, price_code, disclosure_code, pre, today, tomorrow, disclosures):
    """지정예고 공시 → 판단일 T 지정 요건/임계가 리포트 라인 리스트."""
    contents = fetch_disclosure_detail(disclosure_code, pre["disclosureId"])
    text = html_to_text(contents)
    parsed = parse_predesignation_notice(text)

    # 판단기간(순연 마감)이 지난 옛 지정예고 → 비활성: None 반환 → '공시 없음'과 동일 출력
    if parsed["순연_마감일"] and tomorrow > parsed["순연_마감일"]:
        return None

    lines = []
    lines.append("")
    lines.append(f"최신 공시: {pre['title']}")
    lines.append(f"공시일시: {pre['datetime']}")
    if parsed["지정예고일"]:
        lines.append(f"지정예고일: {_fmt_date(parsed['지정예고일'])}")
    if parsed["최초_판단일"]:
        lines.append(f"최초 판단일: {_fmt_date(parsed['최초_판단일'])}")
    if parsed["순연_마감일"]:
        lines.append(f"순연 마감(판단 종료): {_fmt_date(parsed['순연_마감일'])}")

    if not parsed["paths"]:
        lines.append("")
        lines.append("지정요건 파싱 실패 — 본문 수동 확인 필요")
        return lines

    need_long = any(p["kind"] == "long" for p in parsed["paths"])
    start = today - timedelta(days=520 if need_long else 80)
    prices = fetch_daily_prices(price_code, start, today)
    if len(prices) < 15:
        lines.append("")
        lines.append(f"가격 데이터 부족 ({len(prices)}일)")
        return lines

    t1_d, t1 = prices[-1]            # 판단일 직전 거래일 (T-1)
    t5_d, t5 = prices[-5]            # T-5
    prior14 = prices[-14:]          # T-1 ~ T-14 (T 제외한 직전 14거래일)
    max14 = max(c for _, c in prior14)
    max14_d = next(d for d, c in prior14 if c == max14)

    market = fetch_market(name, code) or "KOSDAQ"
    mktcap_raw, _ = fetch_market_cap(price_code)
    try:
        idx_prices = fetch_daily_prices(market, start, today)
    except Exception:
        idx_prices = []

    lines.append("")
    lines.append(f"시장: {market}  |  판단일(T) = {_fmt_date(tomorrow)} 기준 (다음 거래일)")
    lines.append(f"T-1 {_fmt_date(t1_d)} 종가 {t1:,}원  |  T-5 {_fmt_date(t5_d)} 종가 {t5:,}원")
    if mktcap_raw:
        lines.append(f"시가총액: {mktcap_raw}")
    lines.append(f"② 최근 15일 최고종가(T 제외): {max14:,}원 [{max14_d}] → T 종가가 이보다 높아야 충족")

    sanghan = floor_to_tick(t1 * 1.3)
    triggers = []

    for n, p in enumerate(parsed["paths"], 1):
        lines.append("")
        lines.append(f"── 경로 [{n}] {p['label']} ──")

        if p["kind"] == "long":
            ref = date(tomorrow.year - 1, tomorrow.month, tomorrow.day)  # 1년 전(판단일 기준)
            base = base_d = None
            for d, c in prices:
                if d <= ref:
                    base_d, base = d, c
            if base is None:
                lines.append("  1년 전 가격 없음 — 임계가 계산 불가")
                continue
            idx_ret = None
            if idx_prices:
                ib = None
                for d, c in idx_prices:
                    if d <= ref:
                        ib = c
                if ib:
                    idx_ret = idx_prices[-1][1] / ib - 1
            if idx_ret is None:
                lines.append("  지수 데이터 없음 — 초과상승률 임계가 계산 불가")
                continue
            thr = base * (1 + p["excess"] + idx_ret)
            trig = max(thr, max14)
            binder = "①" if thr >= max14 else "②"
            strict = thr < max14  # ②(최고가 초과)가 실효 기준이면 강부등호
            reach = "도달가능" if trig <= sanghan else f"도달불가(상한가 {sanghan:,} 초과)"
            lines.append(f"  ① 1년전 {_fmt_date(base_d)} 종가 {base:,}원, {market} 1년 {idx_ret*100:+.2f}% (지수는 T-1 근사)")
            lines.append(f"     임계가 = {base:,}×(1+{p['excess']:.2f}+{idx_ret:.4f}) = {thr:,.0f}원 (호가 {ceil_to_tick(thr):,})")
            lines.append(f"  ② {max14:,}원 초과 필요 ({'①가 상회' if thr >= max14 else '②가 실효 기준'})")
            lines.append(f"  ③ 매수관여율(특정계좌) — ⚠ 외부확인불가 (KRX 내부 감시데이터)")
            lines.append(f"  ④ 합산 시총 100위 밖 — {mktcap_raw or '확인필요'} (통상 충족·정밀순위는 KRX)")
            lines.append(
                f"  ▶ 가격 트리거({binder}): {trig:,.0f}원 {'초과' if strict else '이상'}  "
                f"(T-1 대비 {(trig/t1-1)*100:+.2f}%)  | 상한가 {sanghan:,}원 → {reach}"
            )
            triggers.append({
                "label": p["label"], "trig": trig, "pctchg": (trig / t1 - 1) * 100,
                "confirm": False, "is_long": True, "strict": strict,
                "note": "③ 매수관여율(비공개) 동반",
            })

        else:
            pct = p["pct"]
            thr = t5 * (1 + pct)
            trig = max(thr, max14)
            binder = "①" if thr >= max14 else "②"
            lines.append(f"  ① T-5 {t5:,} × {1+pct:.2f} = {thr:,.0f}원 (호가 {ceil_to_tick(thr):,})  |  T-1 대비 {(thr/t1-1)*100:+.2f}%")
            lines.append(f"  ② {max14:,}원 초과 필요 (①가 {'상회' if thr > max14 else '미달→②가 실효 기준'})")
            if p["cond3"] == "index5x":
                if len(idx_prices) >= 5:
                    idx_ret5 = idx_prices[-1][1] / idx_prices[-5][1] - 1
                    ok = pct >= 5 * idx_ret5
                    lines.append(
                        f"  ③ 주가상승률 ≥ {market} 5배: 지수 5일 {idx_ret5*100:+.2f}%, "
                        f"임계 {pct*100:.0f}% {'≥' if ok else '<'} {5*idx_ret5*100:.1f}% "
                        f"{'✅충족' if ok else '❌미달'} (T-1 근사)"
                    )
                    note = "③ 지수5배 " + ("충족" if ok else "미달")
                else:
                    lines.append(f"  ③ 주가상승률 ≥ {market} 5배 — 지수 데이터 없음")
                    note = "③ 지수5배(미확인)"
            else:
                lines.append(f"  ③ 매수관여율(특정계좌) — ⚠ 외부확인불가 (KRX 내부 감시데이터)")
                note = "③ 매수관여율(비공개) 동반"
            lines.append(f"  ④ 합산 시총 100위 밖 — {mktcap_raw or '확인필요'} (통상 충족)")
            strict = thr < max14  # ②가 실효 기준이면 강부등호(초과)
            reach = "도달가능" if trig <= sanghan else f"도달불가(상한가 {sanghan:,} 초과)"
            lines.append(
                f"  ▶ 가격 트리거({binder}): {trig:,.0f}원 {'초과' if strict else '이상'} (호가 {ceil_to_tick(trig):,})  "
                f"(T-1 대비 {(trig/t1-1)*100:+.2f}%)  | 상한가 {sanghan:,}원 → {reach}"
            )
            triggers.append({
                "label": p["label"], "trig": trig, "pctchg": (trig / t1 - 1) * 100,
                "confirm": (p["cond3"] == "index5x"), "is_long": False, "strict": strict,
                "note": note,
            })

    lines.append("")
    lines.append("=== 요약 (판단일 T 종가 트리거) ===")
    for t in triggers:
        cmp = "초과" if t["strict"] else "이상"
        lines.append(f"  {t['label']}: {t['trig']:,.0f}원 {cmp} (T-1 {t1:,} 대비 {t['pctchg']:+.2f}%)  [{t['note']}]")
    lines.append("  ※ ③ 매수관여율/특정계좌 요건은 KRX 비공개 데이터 — 가격 외 조건은 외부 확정 불가")

    # 복붙용 한 줄 안내 문구
    desig_day = next_trading_day(tomorrow)          # 요건 충족 시 지정 효력일 (T+1)
    dstr = f"{desig_day.month}/{desig_day.day}"

    def _clause(t):
        # ②(최근 15일 최고종가 초과)가 실효 기준이면 가격 대신 '초과' 표현
        if t["strict"]:
            if t["trig"] == t1:
                return f"어제 종가({t1:,}원)보다 높게 마감 시"
            return f"금일 종가 {t['trig']:,.0f}원 초과 ({t['pctchg']:+.2f}%) 마감 시"
        return f"금일 종가 기준 {t['trig']:,.0f}원 ({t['pctchg']:+.2f}%) 마감 시"

    confirms = sorted([t for t in triggers if t["confirm"]], key=lambda x: x["trig"])
    accounts = sorted([t for t in triggers if not t["confirm"]], key=lambda x: x["trig"])
    segs = []
    if accounts:
        segs.append(f"{_clause(accounts[0])} {dstr}부터 투경 가능성 있음 (소수 계좌 여부에 달림)")
    if confirms:
        c = confirms[0]
        if accounts:
            segs.append(
                f"{c['trig']:,.0f}원 ({c['pctchg']:+.2f}%) 이상 상승 마감시에는 "
                f"소수 계좌에 관계없이 투경 확정"
            )
        else:
            segs.append(f"{_clause(c)} {dstr}부터 투경 확정")

    lines.append("")
    lines.append("=== 안내 문구 (복붙용) ===")
    lines.append(f"{name} : " + ", ".join(segs))
    return lines


def main():
    args = sys.argv[1:]
    if not args:
        print("사용법: python3 check_warning.py <종목명> [기준일 YYYY-MM-DD]", file=sys.stderr)
        sys.exit(1)
    name = args[0]
    run_date = None
    if len(args) >= 2:
        try:
            run_date = datetime.strptime(args[1], "%Y-%m-%d").date()
        except ValueError:
            print("기준일 형식 오류 (YYYY-MM-DD)", file=sys.stderr)
            sys.exit(1)
    else:
        run_date = auto_basis()
    try:
        print(check_warning_stock(name, today=run_date))
    except ValueError as e:
        print(f"에러: {e}", file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        print(f"예상치 못한 에러: {type(e).__name__}: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        sys.exit(3)


if __name__ == "__main__":
    main()
