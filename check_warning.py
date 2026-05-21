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


def _http_get(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode("utf-8", errors="replace")


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


def next_trading_day(today=None):
    if today is None:
        today = date.today()
    nxt = today + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    return nxt


def fetch_disclosures(code, size=20):
    url = f"https://m.stock.naver.com/api/stock/{code}/disclosure?page=1&size={size}"
    return json.loads(_http_get(url))


def fetch_disclosure_detail(code, disclosure_id):
    url = f"https://m.stock.naver.com/api/stock/{code}/disclosure/{disclosure_id}"
    data = json.loads(_http_get(url))
    return data["disclosure"]["contents"]


def fetch_daily_prices(code, start, end):
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
        if len(parts) < 5:
            continue
        try:
            d = datetime.strptime(parts[0], "%Y%m%d").date()
            close = int(float(parts[4]))
        except (ValueError, IndexError):
            continue
        out.append((d, close))
    out.sort()
    return out


def html_to_text(html_content):
    text = re.sub(r"<[^>]+>", "\n", html_content)
    text = html_lib.unescape(text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n", "\n", text)
    return text


def classify_disclosure(title):
    """Returns: 'release', 'designation', or None."""
    t = title or ""
    if "투자경고종목" not in t and "투자경고" not in t:
        return None
    if "지정해제" in t:
        return "release"
    if "지정예고" in t:
        return None
    if "지정" in t:
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
    last_15 = closes[-15:]
    max_15 = max(c for _, c in last_15)
    max_15_date = next(d for d, c in last_15 if c == max_15)

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
    lines.append(f"종목명: {name}")
    lines.append(f"종목코드: {code}")
    lines.append(f"실행일: {_fmt_date(today)}")
    lines.append(f"다음 거래일: {_fmt_date(tomorrow)}")

    disclosures = fetch_disclosures(code)
    rel, des = find_relevant_disclosures(disclosures, cutoff_date=today)

    if not rel and not des:
        lines.append("")
        lines.append("상태: 투자경고 관련 공시 없음")
        return "\n".join(lines)

    latest_is_release = False
    if rel and des:
        rel_dt = datetime.fromisoformat(rel["datetime"])
        des_dt = datetime.fromisoformat(des["datetime"])
        latest_is_release = rel_dt > des_dt
    elif rel:
        latest_is_release = True

    if latest_is_release:
        contents = fetch_disclosure_detail(code, rel["disclosureId"])
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
    contents = fetch_disclosure_detail(code, des["disclosureId"])
    text = html_to_text(contents)
    parsed = parse_designation_notice(text)

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
    prices = fetch_daily_prices(code, start, today)
    if not prices:
        lines.append("")
        lines.append("가격 데이터 조회 실패")
        return "\n".join(lines)

    today_close = prices[-1][1]
    calc = calculate_release_scenario(prices, parsed, today_close)

    lines.append("")
    lines.append("=== 해제 시나리오 (판단일 진행 중) ===")
    lines.append(f"오늘 종가 (T-1): {calc['today_close']:,}원  [{calc['t_minus_1'][0]}]")
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
    lines.append(f"binding ({binding_kind}): {calc['binding']:,.0f}원")

    pct_chg = (calc["binding"] / today_close - 1) * 100
    lines.append("")
    if calc["binding"] == today_close:
        lines.append(f"내일 해제 조건: 종가가 오늘({today_close:,}원)보다 낮게 마감")
    elif calc["binding"] > today_close:
        lines.append(
            f"내일 해제 조건: 종가 < {calc['binding']:,.0f}원 ({pct_chg:+.1f}% 미만 상승)"
        )
    else:
        lines.append(
            f"내일 해제 조건: 종가 < {calc['binding']:,.0f}원 ({pct_chg:+.1f}%, 오늘보다 하락 필요)"
        )

    if calc["halt"]:
        h = calc["halt"]
        halt_pct = (h["price"] / today_close - 1) * 100
        if h["reachable"]:
            lines.append(
                f"⚠ 거래정지: 종가 ≥ {h['price']:,.0f}원 ({halt_pct:+.1f}%) "
                f"[상한가 {h['sanghan']:,}원]"
            )
        else:
            lines.append(
                f"거래정지: {h['price']:,.0f}원 ({halt_pct:+.1f}%) — "
                f"상한가({h['sanghan']:,}원) 초과로 도달 불가"
            )

    return "\n".join(lines)


def main():
    if len(sys.argv) < 2:
        print("사용법: python3 check_warning.py <종목명>", file=sys.stderr)
        sys.exit(1)
    name = sys.argv[1]
    try:
        print(check_warning_stock(name))
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
