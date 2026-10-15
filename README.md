# pumpfun

Pump.fun 페이퍼트레이딩 시뮬레이터를 만들기 위한 학습 프로젝트입니다. 지금 있는 건 데이터 수집기(collector)입니다.

- 프로토콜 정리: [docs/protocol-notes.md](docs/protocol-notes.md)
- 시뮬레이터 설계: [docs/sim-design.md](docs/sim-design.md)

## Collector

Solana RPC 웹소켓으로 Pump 프로그램(`6EF8rrec...`)의 로그를 구독하고, 공식 IDL(`idl/pump.json`)로 이벤트를 디코딩해서 SQLite에 저장합니다.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 collector.py
```

- 기본은 공용 메인넷 RPC(`wss://api.mainnet-beta.solana.com`)입니다. 다른 RPC를 쓰려면 `SOLANA_WS_URL` 환경변수나 `--ws-url`을 지정하세요.
- 저장 위치는 기본 `data/pump.db`이고 `--db`로 바꿀 수 있습니다.
- `Ctrl-C`로 종료합니다. 30초마다 상태가 한 줄 출력됩니다 (`--stats-interval`). 새 코인 로그가 너무 많으면 `--quiet-coins`를 쓰세요.

2026-10-15에 공용 RPC로 75초 동안 돌린 결과: 새 코인 31개, 거래 2,687건, 디코딩 에러 0건. 같은 구간의 온체인 기록과 비교했을 때 코인 생성은 31/31건 모두 수집되었습니다. Pump 프로그램을 건드린 트랜잭션의 절반쯤은 실패한 트랜잭션(주로 스나이핑 봇)이라 저장하지 않습니다.

### 테이블

| 테이블 | 내용 |
| --- | --- |
| `coins` | `CreateEvent` 하나당 한 행 (민트, 이름, 심볼, 크리에이터, 시작 리저브, mayhem/holder rewards 여부, 원본 JSON) |
| `trades` | `TradeEvent` 하나당 한 행 (매수/매도, 수량, **거래 직후의 리저브**, 수수료, 인스트럭션 이름) |
| `lifecycle` | `CompleteEvent`, `PostCompleteBuyEvent`, `CompletePumpAmmMigrationEvent` (졸업, 마이그레이션) |
| `connections` | 웹소켓 세션 기록. 끊긴 시간대는 데이터가 비어 있다는 뜻 |

- 모든 이벤트는 `(signature, event_index)`로 중복 없이 저장됩니다.
- `connections`의 `first_slot`..`last_slot`이 실제로 수집한 구간입니다 (1초마다 갱신). 이 구간 밖은 데이터가 없습니다.
- 공용 RPC는 서명이 `111…1`인 가짜 알림을 보내기도 합니다. 지금 수집기는 버리지만, 2026-10-15 19:44 이전 데이터에는 남아 있으니 `signature != '1111…'`로 제외하세요.
- `slot`은 블록 번호, `timestamp`는 온체인 시간(초), `received_at`은 수집기가 받은 로컬 시간입니다.
- 금액은 전부 원시 단위입니다: SOL은 lamports(10^9), 토큰은 소수점 6자리(10^6).
- `quote_mint`가 `11111111111111111111111111111111`이면 SOL 페어 코인입니다. 다른 값이면 USDC나 다른 pump 코인 페어이고, 이때는 `sol_amount` 대신 `quote_amount`/`virtual_quote_reserves`를 봐야 합니다.

### 예시 쿼리

```sql
-- 최근 새 코인
SELECT datetime(timestamp, 'unixepoch'), symbol, name, mint FROM coins ORDER BY slot DESC LIMIT 20;

-- 한 코인의 가격 흐름 (SOL/토큰, 거래 직후 기준)
SELECT slot, is_buy, sol_amount / 1e9 AS sol,
       (virtual_sol_reserves / 1e9) / (virtual_token_reserves / 1e6) AS price_sol
FROM trades WHERE mint = '<MINT>' ORDER BY slot, signature, event_index;
```

> 같은 slot 안의 거래 순서는 이 데이터만으로는 정확히 알 수 없습니다. `logsSubscribe` 알림에는 블록 내 트랜잭션 순서가 없기 때문입니다. 리저브 값으로 순서를 추정하거나, 필요하면 나중에 `getBlock`으로 보완해야 합니다.

## Simulator (in progress)

- `sim/market.py`: bonding curve market model. Reproduces every recorded trade exactly. Formulas and verification results are in [docs/sim-design.md](docs/sim-design.md) section 4.
- `sim/replay.py`: picks which coins can be simulated, restores on-chain order within a slot, and yields events in the order a strategy would see them, at slot + observation delay.

```python
from sim.replay import load_universe, observations
u = load_universe("data/pump.db")
print(u.summary())                       # how many coins are in, and why the rest were excluded
for obs in observations(u, observe_delay_slots=2):
    obs.kind, obs.visible_slot, obs.view.state.price, obs.view.unique_buyers
coin = u.coins[0]
coin.state_at(coin.create_slot + 4, "end")   # the real curve state in a given slot (for fills)
```
- `sim/engine.py`: runs a strategy and fills its orders accounting for latency (observation delay d, landing L), slippage failures, network fees, account rent, and the forced exit at graduation.

```python
from sim.engine import Strategy, Buy, Sell, EngineConfig, run

class BuyOnCreate(Strategy):
    def on_create(self, coin, ctx):
        return [Buy(100_000_000)]                       # 0.1 SOL, 15% slippage
    def on_tick(self, coin, pos, ctx):                  # every 25 slots while holding
        if pos.tokens and coin.slots_since_create > 1500 and not ctx.pending_orders(coin.mint):
            return [Sell(1.0, reason="time stop")]
        return []

result = run(u, BuyOnCreate(), EngineConfig(observe_delay_slots=2, land_slots=2))
for rt in result.round_trips:
    rt.exit_reason, rt.pnl, rt.pnl_pct
```

### Running a simulation

```bash
python3 run_sim.py --strategy launch_sniper --baseline          # side by side with the random baseline
python3 run_sim.py --strategy launch_sniper --sensitivity       # latency (d, L) x size grid
python3 run_sim.py --strategy launch_sniper --period test --save
python3 run_sim.py --help
```

- Strategies: `launch_sniper` (buy on creation), `random_baseline` (same exit rules, entry at a random moment). They're in `sim/strategies/` and share the exit rules (take profit / stop loss / time stop).
- Only coins with at least the hold time + entry window (default 20 minutes) of data after creation are used, so every run and the baseline trade the same set of coins.
- `--period tune|test`: splits the coins in half at the median creation time. Tune rules on `tune` and judge them on `test`.
- `--save` writes the config, summary, and every round trip and fill to `data/sims/<time>_<strategy>.json`.
- First results: [docs/sim-design.md](docs/sim-design.md) section 12.

## 테스트

```bash
python3 -m unittest discover tests
```

`tests/fixtures`의 실제 메인넷 트랜잭션(2026-10-15)으로 디코딩, 본딩커브 수식, 수수료 반올림, 중복 처리를 확인합니다. `data/pump.db`(또는 `$PUMP_DB`)가 있으면 수집된 모든 거래를 시장 모델로 재현하는 테스트도 함께 돕니다.

## 구조

```
collector.py            웹소켓 구독, 재연결, 통계
pumpfun/idl_decoder.py  IDL 기반 Borsh 이벤트 디코더 (필드가 추가된 신/구 버전 이벤트도 처리)
pumpfun/logs.py         로그에서 Pump 프로그램이 낸 "Program data:"만 골라냄
pumpfun/store.py        SQLite 스키마와 저장
idl/pump.json           공식 IDL (pump-public-docs, 2026-10-13 커밋)
```

Pump가 이벤트 필드를 추가하면 `idl/pump.json`을 공식 repo의 최신 파일로 교체하세요. 그전까지는 새 필드를 무시하고 기존 필드만 저장합니다.
