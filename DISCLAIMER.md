# Legal Disclaimer / 법적 고지

**Last updated:** 2026-05-21

This is a **personal, open-source tool** written by Sangwoo "Paul" Cheung. It is
**not** a commercial product and is **not affiliated with backtest.co.kr or any
other commercial entity**. backtest.co.kr is referenced only as one possible
*external* source of the portfolio JSON files this tool can read.

## English

This software (`kis_passive_trader`) is an **informational tool and trade-execution
helper** that runs on the user's own machine with the user's own brokerage API
credentials. It is not a managed investment service.

**The author makes no warranty, express or implied, regarding:**

- The correctness, completeness, or timeliness of any data read from a portfolio
  JSON file, from backtest.co.kr, from KIS, or from any other source.
- The profitability or risk profile of any portfolio constructed from such
  data.
- The fill quality, slippage, or execution quality of any order submitted
  through this software.
- The reliability, availability, or uptime of the KIS Open API or any other
  third-party service this software interacts with.

**The user acknowledges and accepts that:**

- **All investment decisions are the user's own.** This software does not
  provide investment advice or recommendations. Any portfolio and order data
  it processes are the result of the user's own choices and of statistical
  analysis performed elsewhere, not professional investment advice.
- **All orders require explicit user confirmation before execution.** The
  software shows an execution plan and requires confirmation before any order
  is submitted, and prompts again on any position whose live price has drifted
  materially from the signal's reference close.
- **The passive peg-to-best execution strategy may result in unfilled orders.**
  Because the software never crosses the bid-ask spread, if the market moves
  away from the current best bid/ask your order may simply not fill.
- **Past backtest performance does not guarantee future results.** No
  strategy — including any portfolio this tool helps execute — is guaranteed
  to be profitable.
- **The software is provided "as is", without warranty of any kind.** See the
  MIT license in `LICENSE` for the full disclaimer of warranty and liability.
- **KIS Open API credentials enable real trading on real-money accounts.**
  Handle them with appropriate care and test against the KIS mock (paper)
  server first.

## 한국어

본 소프트웨어(`kis_passive_trader`)는 사용자가 자신의 컴퓨터에서 자신의 증권사
API 인증 정보를 사용하여 실행하는 **정보 조회 및 주문 집행 보조 도구**입니다.
투자일임 서비스 또는 투자자문 서비스가 아닙니다. 본 도구는 개인이 작성한
오픈소스 도구이며, backtest.co.kr 또는 기타 상업적 주체와 제휴 관계가 없습니다.
backtest.co.kr은 본 도구가 읽어들일 수 있는 포트폴리오 JSON 파일의 **외부**
출처 중 하나로서만 언급됩니다.

**제작자는 다음 사항에 대하여 어떠한 명시적 또는 묵시적 보증도 제공하지
않습니다:**

- 포트폴리오 JSON 파일, backtest.co.kr, 한국투자증권(KIS), 또는 기타 출처로부터
  읽어들인 데이터의 정확성·완전성·적시성
- 해당 데이터로 구성된 포트폴리오의 수익성 또는 위험 특성
- 본 소프트웨어를 통해 제출된 주문의 체결 품질, 슬리피지, 또는 집행 품질
- KIS Open API 등 본 소프트웨어가 연동하는 제3자 서비스의 신뢰성·가용성·가동시간

**이용자는 다음 사항을 이해하고 수락합니다:**

- **모든 투자 판단은 이용자 본인의 책임입니다.** 본 소프트웨어는 투자 권유
  또는 추천을 제공하지 않습니다.
- **모든 주문은 집행 전에 이용자의 명시적 동의가 필요합니다.** 본 소프트웨어는
  실행 계획을 표시하고 주문 제출 전 확인을 요구하며, 신호의 기준 종가 대비
  현재가가 크게 벗어난 종목에 대해서는 추가로 확인을 요청합니다.
- **패시브 peg-to-best 집행 전략은 미체결을 야기할 수 있습니다.** 본
  소프트웨어는 호가 스프레드를 넘지 않으므로, 시장이 현재의 최우선 매수/매도
  호가에서 멀어질 경우 주문이 체결되지 않을 수 있습니다.
- **과거 백테스트 성과는 미래 수익을 보장하지 않습니다.**
- **본 소프트웨어는 "있는 그대로(as is)" 제공되며 어떠한 종류의 보증도
  포함하지 않습니다.** 보증 부인 및 책임 제한에 관한 전체 내용은 `LICENSE`
  파일의 MIT 라이선스를 참조하십시오.
- **KIS Open API 인증 정보는 실전 계좌에서 실제 자금 거래를 가능하게 합니다.**
  주의하여 관리하고, 반드시 KIS 모의투자(mock) 서버에서 먼저 테스트하십시오.

---

## Regulatory context / 규제 관련

The author is an individual, **not** a licensed investment advisor under the
Korean Financial Investment Services and Capital Markets Act
(자본시장과 금융투자업에 관한 법률). This software does not constitute
investment advisory services (투자자문업) or discretionary investment services
(투자일임업).

제작자는 개인이며 자본시장법상 투자자문업자 또는 투자일임업자로 등록되어 있지
않습니다. 본 소프트웨어는 투자자문업 또는 투자일임업 서비스에 해당하지
않습니다.
