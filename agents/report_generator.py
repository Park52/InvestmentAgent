"""Agent 8: ReportGeneratorAgent — 최종 리포트 생성.

전체 파이프라인 결과를 취합해 Sonnet으로 리포트를 작성하고,
면책 문구를 강제 삽입한 뒤 reports/report_YYYYMMDD.md로 저장한다.

CLAUDE.md 절대 규칙 5: 리포트에는 반드시 면책 문구를 포함한다.
"""

import os
from datetime import datetime, timezone

import config
from agents.base import BaseAgent
from utils import llm
from utils.notifier import NotificationService

# 발송 요약 말미에 들어가는 짧은 면책 문구 (텔레그램/이메일용)
_SHORT_DISCLAIMER = "[이 분석은 정보 제공 목적이며 투자 결정의 근거로 단독 사용 불가]"

# 면책 문구 — LLM 출력과 무관하게 코드가 강제로 삽입한다.
DISCLAIMER = (
    "> ⚠️ **면책 조항**: 이 분석은 정보 제공 목적이며 투자 결정의 근거로 단독 사용할 수 없습니다. "
    "모든 투자 판단과 그 결과에 대한 책임은 투자자 본인에게 있습니다."
)

_SYSTEM = "당신은 투자 리서치 애널리스트다. 수집된 데이터에 근거해 객관적으로 작성한다."

# 폰에서 보기 좋은 HTML 템플릿 (charset/viewport 명시 → 인코딩·렌더링 안전)
_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>투자 정보 리포트 — {date}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans KR",
          "Malgun Gothic", sans-serif; max-width: 820px; margin: 0 auto;
          padding: 16px 18px 48px; line-height: 1.65; color: #1a1a1a; }}
  h1 {{ font-size: 1.5rem; border-bottom: 2px solid #333; padding-bottom: 8px; }}
  h2 {{ font-size: 1.25rem; margin-top: 1.8em; border-bottom: 1px solid #ddd; padding-bottom: 4px; }}
  h3 {{ font-size: 1.1rem; margin-top: 1.4em; }}
  table {{ border-collapse: collapse; width: 100%; margin: 12px 0; font-size: 0.95rem; }}
  th, td {{ border: 1px solid #ccc; padding: 7px 9px; text-align: left; }}
  th {{ background: #f4f6f8; }}
  blockquote {{ margin: 12px 0; padding: 8px 14px; background: #fff8e1;
                border-left: 4px solid #f5a623; border-radius: 4px; }}
  code, pre {{ background: #f5f5f5; border-radius: 4px; }}
  code {{ padding: 1px 5px; }}
  pre {{ padding: 12px; overflow-x: auto; }}
  hr {{ border: none; border-top: 1px solid #ddd; margin: 24px 0; }}
</style>
</head>
<body>
{body}
</body>
</html>
"""


def _render_html(markdown_text: str, date: str) -> str:
    """마크다운 리포트를 폰 친화 HTML로 변환한다. markdown 미설치 시 <pre> 폴백."""
    try:
        import markdown as _md
        body = _md.markdown(
            markdown_text,
            extensions=["tables", "fenced_code", "sane_lists", "nl2br"],
        )
    except Exception:
        import html as _html
        body = "<pre>" + _html.escape(markdown_text) + "</pre>"
    return _HTML_TEMPLATE.format(date=date, body=body)


def _date_str(context: dict) -> str:
    return context.get("date") or datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _fmt_leading(lead: dict) -> str:
    """선행지표를 한 줄 문자열로 압축한다 (None은 'N/A')."""
    if not lead:
        return "선행지표: N/A"
    def g(k):
        v = lead.get(k)
        return "N/A" if v is None else v
    return (
        f"EPS추세={g('eps_revision_trend')}({g('eps_revision_pct')}%) "
        f"내부자매수/매도={g('insider_buy_count')}/{g('insider_sell_count')} "
        f"공매도비율={g('short_interest_ratio')}(Δ{g('short_interest_change')}%) "
        f"구글트렌드={g('google_trend_score')}(Δ{g('google_trend_change')})"
    )


def _summarize_context(context: dict) -> str:
    """LLM에 넣을 데이터 요약을 만든다."""
    macro = context.get("macro", {}) or {}
    theses = context.get("theses", []) or []
    holdings_data = context.get("holdings_data", []) or []

    lines = [
        f"거시환경: VIX={macro.get('vix')}, USDKRW={macro.get('usdkrw')}, "
        f"리스크레벨={macro.get('market_risk_level')}",
        f"핫섹터: {', '.join(context.get('hot_sectors', [])) or '없음'}",
        f"뉴스 수집: {len(context.get('news', []) or [])}건",
        "",
        "분석 종목(후보):",
    ]
    if not theses:
        lines.append("  (해당 없음)")
    for t in theses:
        chart = t.get("chart", {}) or {}
        lines.append(
            f"- {t['ticker']}({t.get('sector')}): 판정={t.get('verdict')} "
            f"확신도={t.get('confidence')} | RSI={chart.get('rsi_14')} "
            f"MACD={chart.get('macd_signal')} 교차={chart.get('ma_cross')}"
        )
        lines.append(f"    선행지표: {_fmt_leading(t.get('leading', {}))}")
        if t.get("bull_case"):
            lines.append(f"    매수근거: {t['bull_case']}")
        if t.get("bear_case"):
            lines.append(f"    매도근거: {t['bear_case']}")

    # 보유 종목 모니터링 (선행지표 위주, verdict 없음)
    if holdings_data:
        lines.append("")
        lines.append("보유 종목 모니터링:")
        for h in holdings_data:
            chart = h.get("chart", {}) or {}
            lines.append(
                f"- {h['ticker']}({h.get('sector')}): RSI={chart.get('rsi_14')} "
                f"MACD={chart.get('macd_signal')}"
            )
            lines.append(f"    선행지표: {_fmt_leading(h.get('leading', {}))}")
    return "\n".join(lines)


def generate_narrative(context: dict) -> str:
    """Sonnet으로 리포트 본문을 작성한다. 실패 시 데이터 요약으로 fallback."""
    summary = _summarize_context(context)
    prompt = (
        "다음 데이터를 바탕으로 한국어 투자 정보 리포트를 마크다운으로 작성하라. "
        "거시환경 개요, 핫섹터, 각 종목의 강세/약세 논거와 종합 판단을 포함하라. "
        "선행지표(EPS 추세·내부자 거래·공매도·구글트렌드)를 해석에 적극 반영하고, "
        "보유 종목 모니터링 섹션도 별도로 작성하라.\n\n"
        f"{summary}"
    )
    try:
        text = llm.call_claude(
            prompt,
            system=_SYSTEM,
            model=config.MODEL_SMART,
            max_tokens=config.MAX_TOKENS_REPORT,
        )
        return text or summary
    except Exception:
        return summary


def build_summary(context: dict, report_path: str) -> str:
    """텔레그램/이메일용 핵심 요약(3~5줄)을 만든다. CLAUDE.md '발송 내용 형식' 준수."""
    date = _date_str(context)
    macro = context.get("macro", {}) or {}
    hot = context.get("hot_sectors", []) or []
    theses = context.get("theses", []) or []

    tickers = [t.get("ticker") for t in theses if t.get("ticker")]

    lines = [
        f"[투자 리포트] {date}",
        "",
        f"📊 오늘의 핫섹터: {', '.join(hot) or '없음'}",
        f"🔍 분석 종목: {', '.join(tickers) or '없음'}",
        f"⚠️ 시장 리스크: {macro.get('market_risk_level', 'N/A')}",
    ]

    # 상위 추천: bullish 우선, 확신도 내림차순 상위 3개
    ranked = sorted(
        theses,
        key=lambda t: (t.get("verdict") == "bullish", t.get("confidence") or 0),
        reverse=True,
    )
    top = [t for t in ranked if t.get("verdict") and t.get("verdict") != "neutral"][:3]
    if top:
        lines.append("")
        lines.append("상위 추천:")
        for t in top:
            lines.append(
                f"• {t['ticker']} — {t.get('verdict')} (confidence {t.get('confidence')})"
            )

    lines.append("")
    lines.append(f"전체 리포트: {report_path}")
    lines.append("")
    lines.append(_SHORT_DISCLAIMER)
    return "\n".join(lines)


class ReportGeneratorAgent(BaseAgent):
    name = "ReportGeneratorAgent"

    def execute(self, context: dict) -> dict:
        date = _date_str(context)
        narrative = generate_narrative(context)

        # 면책 문구를 상·하단에 강제 삽입 (LLM이 누락해도 항상 존재)
        content = (
            f"# 투자 정보 리포트 — {date}\n\n"
            f"{DISCLAIMER}\n\n"
            f"{narrative}\n\n"
            f"---\n\n{DISCLAIMER}\n"
        )

        os.makedirs(config.REPORTS_DIR, exist_ok=True)
        filename = f"report_{date.replace('-', '')}.md"
        path = os.path.join(config.REPORTS_DIR, filename)
        # utf-8-sig: 파일 앞에 UTF-8 BOM을 넣어 모바일/윈도우 뷰어가 한글을
        # 다른 인코딩(cp949 등)으로 오인식해 깨지는 것을 방지한다.
        with open(path, "w", encoding="utf-8-sig") as f:
            f.write(content)

        # 폰 친화 HTML 버전도 생성 (텔레그램 첨부용 — 브라우저로 예쁘게 렌더링)
        html_path = os.path.join(config.REPORTS_DIR, f"report_{date.replace('-', '')}.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(_render_html(content, date))

        self.log.info("리포트 저장: %s (+ HTML)", path)
        context["report_path"] = path
        context["report_html_path"] = html_path

        # 알림 발송 (실패해도 파이프라인은 계속 — NotificationService가 예외 흡수)
        # 첨부는 폰에서 보기 좋은 HTML로 전송한다.
        summary = build_summary(context, path)
        context["summary"] = summary
        sent = NotificationService().send(html_path, summary)
        context["notification_sent"] = sent
        self.log.info("알림 발송 결과(channel=%s): %s", config.NOTIFICATION_CHANNEL, sent)

        return {"report_path": path, "date": date, "notification_sent": sent}
