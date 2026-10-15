# Pump.fun 프로토콜 노트 (Phase 1)

> 기준일: **2026-10-15**
> 목적: Pump.fun 위에서 돌아가는 트레이딩 봇을 만들기 전, 프로토콜 구조를 공부하기 위한 정리.
> 주의: Pump.fun은 몇 주 단위로 계정 레이아웃과 인스트럭션을 바꿉니다. 코드를 쓰기 전에 아래 "출처"의 공식 문서와 IDL을 다시 확인할 것.

**출처 (우선순위 순)**

1. 공식 문서 repo [`pump-fun/pump-public-docs`](https://github.com/pump-fun/pump-public-docs), 마지막 커밋 `639b87f` (2026-10-13)
2. 공식 SDK 소스 `@pump-fun/pump-sdk` 4.1.0, `@pump-fun/pump-swap-sdk` 2.1.0 (npm)
3. 메인넷 계정을 직접 읽은 값 (2026-10-15, `api.mainnet-beta.solana.com`): `Global`, `FeeConfig`
4. 뉴스·분석 기사 (비즈니스 동향만, 섹션 9)

---

## 1. 큰 그림

```
create_v2 ──► 본딩커브 (Pump 프로그램) ──► 커브 소진(complete) ──► migrate ──► PumpSwap 풀 (AMM)
              가상 리저브 x·y=k               real_token_reserves == 0     누구나 호출 가능    상수곱 AMM, LP 토큰은 소각
```

- 코인을 만들면 바로 **본딩커브**에서 거래됩니다. 초기 유동성은 필요 없습니다 (가상 리저브 사용).
- 커브에서 판매용 토큰이 다 팔리면 커브가 `complete` 되고, 유동성이 **PumpSwap**(Pump 자체 AMM)으로 옮겨집니다. 이걸 흔히 "졸업(graduation)"이라고 부릅니다.
- 예전에는 Raydium으로 갔지만(`withdraw` 인스트럭션), 지금은 비활성화되었고 `migrate` → PumpSwap만 씁니다.
- 마이그레이션으로 받은 LP 토큰은 소각됩니다.

## 2. 주소 모음

### 프로그램

| 이름 | 주소 | 비고 |
| --- | --- | --- |
| Pump (본딩커브) | `6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P` | 메인넷·데브넷 동일 |
| PumpSwap (AMM) | `pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA` | 메인넷·데브넷 동일 |
| Pump Fees | `pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ` | 수수료 티어, 크리에이터 수수료 공유 |
| Mayhem | `MAyhSmzXzV1pTf7LsNkrNwkWKTo4ougAJ1PPg47MD4e` | `create_v2` 계정 목록에 포함 |
| Token-2022 | `TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb` | `create_v2`로 만든 코인의 토큰 프로그램 |
| SPL Token (레거시) | `TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA` | 예전 `create` 코인 |
| Associated Token | `ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL` | |

### 주요 계정 / PDA

| 계정 | 주소 또는 시드 |
| --- | --- |
| Pump `Global` | `4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf` (`["global"]`) |
| PumpSwap `GlobalConfig` | `ADyA8hdefvWN2dbGGWFotbzWxrAvLW83WG6QCVXvJKqw` (`["global_config"]`) |
| Pump용 `FeeConfig` | `8Wf5TiAheLUqBrKXeYg2JtAFFMWtKdG2BSFgqUcPVwTt` (Fees 프로그램, `["fee_config", pump_program_id]`) |
| PumpSwap용 `FeeConfig` | `5PHirr8joyTMp9JMm6nW7hNDVyEYdkzDqazxPD7RaTjx` (메인넷 조회로 확인) |
| 본딩커브 | `["bonding-curve", mint]` (Pump) |
| 커브의 토큰 계정 | `mint`에 대한 ATA, owner = bonding_curve |
| 크리에이터 볼트 (Pump) | `["creator-vault", bonding_curve.creator]` |
| 크리에이터 볼트 (PumpSwap) | `["creator_vault", pool.coin_creator]` ← 하이픈/언더스코어 다름 주의 |
| 유저 볼륨 누적기 | `["user_volume_accumulator", user]` (Pump) |
| 수수료 공유 설정 | `["sharing-config", mint]` (Fees 프로그램) |
| PumpSwap 풀 | `["pool", index(u16), creator, base_mint, quote_mint]`, 마이그레이션 풀은 `index = 0` |
| 이벤트 권한 | `["__event_authority"]` |
| Wrapped SOL | `So11111111111111111111111111111111111111112` |
| USDC | `EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v` |

수수료 수령 주소는 총 24개 (일반 8 / mayhem 전용 8 / buyback 8). 목록은 공식 [`FEE_RECIPIENTS.md`](https://github.com/pump-fun/pump-public-docs/blob/main/docs/FEE_RECIPIENTS.md) 참고. 트랜잭션 처리량을 위해 거래마다 무작위로 하나를 고르라고 권장합니다.

## 3. 토큰과 본딩커브 수치

메인넷 `Global` 계정에서 직접 읽은 값 (2026-10-15):

| 필드 | 원시값 | 의미 |
| --- | --- | --- |
| `token_total_supply` | 1,000,000,000,000,000 | 10억 개 (소수점 6자리) |
| `initial_virtual_token_reserves` | 1,073,000,000,000,000 | 가상 토큰 리저브 10.73억 개 |
| `initial_virtual_sol_reserves` | 30,000,000,000 | 가상 SOL 리저브 30 SOL |
| `initial_real_token_reserves` | 793,100,000,000,000 | 커브에서 실제로 팔리는 양 7.931억 개 |
| `pool_migration_fee` | 15,000,001 lamports | 약 0.015 SOL, 마이그레이션 계정 생성비 |

여기서 계산되는 값 (SOL 페어 기준):

| 항목 | 값 |
| --- | --- |
| 시작 시가총액 | 30 × 1000 / 1073 ≈ **27.96 SOL** |
| 졸업 시 가상 SOL 리저브 | ≈ 115.005 SOL |
| 커브가 모으는 실제 SOL | ≈ **85.005 SOL** |
| 졸업 시 시가총액 | ≈ **410.9 SOL** |
| PumpSwap 풀로 가는 토큰 | 10억 − 7.931억 = **2.069억 개** |
| 풀로 가는 SOL | ≈ 85.005 − 0.015 ≈ 84.99 SOL |

> PumpSwap 첫 수수료 구간 경계가 420 SOL인 건 이 졸업 시총(~411 SOL) 근처라서입니다.

### 커브 수식 (SDK 4.1.0 `bondingCurve.ts`에서 확인)

Uniswap V2 상수곱 공식에 가상 리저브를 넣은 형태입니다. `vT` = virtual_token_reserves, `vQ` = virtual_quote_reserves (예전 이름 virtual_sol_reserves).

```text
# 토큰 amount 개를 살 때 드는 quote (수수료 전)
cost = floor(amount * vQ / (vT - amount)) + 1

# quote in 만큼 넣을 때 받는 토큰 (수수료는 먼저 뺀다)
net_in  = floor((in - 1) * 10000 / (10000 + total_fee_bps))
tokens  = floor(net_in * vT / (vQ + net_in))
tokens  = min(tokens, real_token_reserves)

# 토큰 amount 개를 팔 때 받는 quote (수수료 전)
out = floor(amount * vQ / (vT + amount))

# 수수료는 올림
fee(x, bps) = ceil(x * bps / 10000)
buy 총비용  = cost + fee(cost, protocol_bps) + fee(cost, creator_bps)
sell 수령액 = out  - fee(out, protocol_bps)  - fee(out, creator_bps)
```

- 시가총액(lamports) = `vQ * mint_supply / vT`. 수수료 구간을 고를 때 이 값을 씁니다.
- 레거시 코인 중 `bonding_curve.creator == Pubkey::default()`이면 크리에이터 수수료가 붙지 않습니다.
- 매수/매도 시 virtual과 real 리저브는 같은 양만큼 함께 움직입니다.
- 커브는 `real_token_reserves == 0`이 되는 매수의 끝에서 `complete = true`가 됩니다.

### `BondingCurve` 계정 필드

`virtual_token_reserves`, `virtual_quote_reserves`, `real_token_reserves`, `real_quote_reserves`, `token_total_supply`, `complete`, 그리고 나중에 덧붙은 필드들: `creator`, `is_mayhem_mode`, `is_cashback_coin`, `quote_mint`, `creator_fee_bps`, `can_edit_creator_fee`, `is_holder_reward`, `protocol_fees`, `creator_fee`, `post_complete_base_out`, `post_complete_quote_in`, `depth` 등.

- **오래된 계정은 데이터가 짧습니다.** 없는 뒤쪽 필드는 `0 / false / Pubkey::default()`로 읽어야 합니다. Rust라면 `pump_rust_client::state` 타입(짧은 버퍼를 0으로 채워줌)을 쓰고, IDL에서 생성한 raw 타입은 쓰지 말 것.
- `real_sol_reserves` → `real_quote_reserves`, `virtual_sol_reserves` → `virtual_quote_reserves`로 이름이 바뀌었습니다. 예전 블로그 글은 옛 이름을 씁니다.

## 4. 수수료 (메인넷 `FeeConfig` 직접 조회)

### 본딩커브

| 프로토콜 | 크리에이터 | LP | 합계 |
| --- | --- | --- | --- |
| 0.95% | 0.30% | 0% | **1.25%** |

현재 구간이 하나뿐이라 시총과 상관없이 1.25%입니다 (`Global.fee_basis_points = 95`도 일치).

### PumpSwap (마이그레이션된 canonical 풀)

시가총액(SOL)에 따라 바뀌는 동적 수수료입니다. 프로토콜은 0.05%로 고정이고 크리에이터 몫이 줄어듭니다.

| 시총 (SOL) | 대략 USD (2025-09 공지 당시 환율) | LP | 프로토콜 | 크리에이터 | 합계 |
| --- | --- | --- | --- | --- | --- |
| 0 – 420 | ~$85k 이하 | 0.02% | 0.93% | 0.30% | 1.25% |
| 420 – 1,470 | | 0.20% | 0.05% | 0.95% | 1.20% |
| 1,470 – 2,460 | | 0.20% | 0.05% | 0.90% | 1.15% |
| 2,460 – 3,440 | | 0.20% | 0.05% | 0.85% | 1.10% |
| 3,440 – 4,420 | | 0.20% | 0.05% | 0.80% | 1.05% |
| 4,420 – 9,820 | | 0.20% | 0.05% | 0.75% | 1.00% |
| … (구간당 크리에이터 −0.05%p 내외) | | | | | |
| 93,330 – 98,240 | | 0.20% | 0.05% | 0.08% | 0.33% |
| 98,240 이상 | ~$20M 이상 | 0.20% | 0.05% | 0.05% | **0.30%** |

- 전체 25구간 원본은 공식 docs의 `fees.png`와 같고, 온체인 값과도 일치했습니다.
- **USDC 페어 코인**은 별도 `stable_fee_tiers`를 씁니다. 경계가 USDC 금액(59k, 300k, 500k, … 20M)이고 수수료율은 SOL 구간과 같습니다.
- **canonical이 아닌 풀**(누가 직접 만든 풀)은 고정 수수료: LP 0.25% + 프로토콜 0.05% = 0.30%.
- SOL·USDC·pump 코인이 아닌 "exotic" 페어는 `exotic_flat_fees` (LP 0.20 / 프로토콜 0.05 / 크리에이터 0.05).
- 공식 권장: 수수료 로직을 하드코딩하지 말고 `FeeConfig`를 읽어 `calculateFeeTier`로 계산할 것. 그러면 구간이 바뀌어도 코드는 그대로입니다.

### 프로토콜 수수료 중 "buyback" 몫

2026-04-28 업그레이드부터 프로토콜 수수료 일부가 buyback 수령 주소로 갑니다. 이 몫은 항상 거래 시점에 바로 지급됩니다 (아래 v3의 "수수료 적립"과 무관).

## 5. 거래 인스트럭션 세대 (가장 헷갈리는 부분)

같은 매수/매도가 세 세대로 공존합니다. **가격과 수수료 금액은 모두 같고**, 계정 수와 수수료 지급 시점만 다릅니다.

| 세대 | 본딩커브 (Pump) | 계정 수 | PumpSwap | 비고 |
| --- | --- | --- | --- | --- |
| 레거시 | `buy`, `sell`, `buy_exact_sol_in` | 18 / 16 | `buy`, `sell`, `buy_exact_quote_in` | SOL 페어 전용, 코인 종류마다 계정이 다름 |
| v2 | `buy_v2`, `sell_v2`, `buy_exact_quote_in_v2` | 27 / 26 | — | 모든 quote(SOL·USDC·pump 코인)에 같은 계정 순서. 수수료 매 거래 지급 |
| v3 (최신) | `buy_v3`, `sell_v3`, `buy_exact_quote_in_v3` | **17** | `buy_v2`, `sell_v2`, `buy_exact_quote_in_v2` (17) | 수수료를 커브/풀에 쌓아두고 나중에 sweep |

- 2026-10-08부터 v3가 나왔습니다. 봇 입장에서는 계정이 적어 트랜잭션에 더 많이 넣을 수 있으니 **v3를 기본으로** 공부하면 됩니다.
- 예외: **cashback 코인은 v3(커브)·v2(PumpSwap)를 못 씁니다** (`CashbackCoinNotSupported`). 이런 코인은 이전 세대 인스트럭션을 써야 합니다.
- SOL 페어 코인이라도 v2/v3에서는 `quote_mint`에 wrapped SOL 주소를 넘깁니다. 실제 이동은 네이티브 SOL입니다.
- v3에서 `associated_base_user`(내 토큰 계정)는 **미리 존재해야** 합니다. 거래가 만들어주지 않습니다.
- v3의 `user_volume_accumulator`는 없으면 생성되고 렌트 약 0.0018444 SOL을 냅니다 (첫 거래 비용).
- `multi_hop_swap` (PumpSwap): SOL → 코인 A → 코인 B를 한 인스트럭션으로. 중간 코인 토큰 계정이 필요 없습니다. mayhem 커브/풀은 hop 불가.

### v3 계정 순서 (17개, 3종 공통)

1 `global` · 2 `base_mint` · 3 `quote_mint` · 4 `base_token_program` · 5 `quote_token_program` · 6 `bonding_curve` · 7 `associated_base_bonding_curve` · 8 `associated_quote_bonding_curve` · 9 `user` · 10 `associated_base_user` · 11 `associated_quote_user` · 12 `user_volume_accumulator` · 13 `fee_config` · 14 `buyback_fee_recipient` · 15 `system_program` · 16 `event_authority` · 17 `program`

인자: `buy_v3(amount, max_sol_cost, partial_fill?)`, `buy_exact_quote_in_v3(spendable_quote_in, min_tokens_out, partial_fill?)`, `sell_v3(amount, min_sol_output)`.

### 수수료 적립과 sweep

- v3(커브)와 v2(PumpSwap) 거래는 프로토콜·크리에이터 수수료를 `BondingCurve.protocol_fees / creator_fee`, `Pool.protocol_fees / creator_fees`에 쌓아둡니다.
- 누구나 `sweep_protocol_fee`, `sweep_creator_fee`로 지급할 수 있습니다 (permissionless). 받는 주소는 고정이라 아무나 불러도 안전합니다.
- 크리에이터는 `sweep_creator_fee` 후 `collect_creator_fee_v2`(커브) / `collect_coin_creator_fee`(풀)를 같은 트랜잭션에서 호출합니다.
- **풀 quote 금고 잔액에는 아직 지급 안 된 수수료가 섞여 있습니다.** 그래서 아래 "effective reserves"를 써야 합니다.

## 6. 졸업(마이그레이션)

### 일반 흐름

1. 매수로 `real_token_reserves`가 0이 되면 `complete = true`, `CompleteEvent` 발생.
2. 이후 그 커브에서의 모든 매수·매도는 `BondingCurveComplete`로 실패.
3. 누구나 `migrate`(또는 `migrate_v2`)를 호출해 PumpSwap 풀을 생성. 멱등이라 두 번 불러도 안전합니다. 보통 Pump 쪽이 바로 처리합니다.
4. `CompletePumpAmmMigrationEvent` 발생, PumpSwap 쪽에선 `CreatePoolEvent`.

### Synthetic migration (2026-10 신규)

- v3 매수에서 커브를 비우는 **마지막 한 번의 매수는 크기 제한이 없습니다.** 남은 양은 커브 가격으로, 초과분은 "곧 생길 PumpSwap 풀"의 가격(같은 상수곱 공식)으로 채워집니다. 풀은 그 매수가 멈춘 가격에서 열립니다.
- 두 구간 모두 **본딩커브 수수료(1.25%)**가 적용됩니다.
- 이벤트 3개가 순서대로: `TradeEvent`(커브 부분) → `CompleteEvent` → `PostCompleteBuyEvent`(풀 부분). 매수자 총량 = 두 이벤트의 합.
- 레거시/v2 매수는 그대로: 남은 양보다 크면 `NotEnoughTokensToBuy`.
- mayhem 코인은 synthetic migration 없음. `partial_fill = [true]`면 남은 만큼만 사고 커브를 완료.

오프체인 계산 (공식 문서 그대로):

```text
remaining   = bonding_curve.real_token_reserves
curve_quote = remaining 개에 대한 일반 커브 비용
pool_base   = associated_base_bonding_curve.amount - remaining
pool_quote  = bonding_curve.real_quote_reserves + curve_quote - Global.pool_migration_fee   (SOL 페어)
추가 토큰 out 의 비용: quote_in = ceil(pool_quote * out / (pool_base - out))
```

## 7. PumpSwap 가격 계산: effective quote reserves

```text
effective_quote_reserves = pool_quote_token_account.amount + Pool.virtual_quote_reserves
base_reserves            = pool_base_token_account.amount   (변화 없음)
```

- `virtual_quote_reserves`는 **부호 있는 `i128`**이고 2026-09-30부터 **음수가 될 수 있습니다** (적립된 수수료만큼 빼기 때문).
- 절대 `u64`/`u128`로 읽지 말 것. 인덱서 DB 컬럼도 signed로.
- 합계는 음수가 되거나 오버플로하지 않는다고 프로그램이 보장합니다.
- 금고 잔액을 그대로 유동성으로 보면 가격이 틀립니다.
- "boost": 풀이 boost 상태인지는 `virtual_quote_reserves + protocol_fees + creator_fees != 0`으로 판단 (이벤트의 `can_boost`).

## 8. 코인 종류와 생성

`create_v2` 하나로 생성합니다 (Token-2022 민트, 소수점 6자리). 인자: `name`(≤32), `symbol`(≤13), `uri`(≤200), `creator`, `is_mayhem_mode`, `is_cashback_enabled?`(폐지), `creator_fee_bps?`, `is_holder_reward?`.

| 종류 | 내용 | 봇이 신경 쓸 점 |
| --- | --- | --- |
| 일반 코인 | 크리에이터 수수료가 creator 지갑으로 | — |
| **Mayhem 모드** | 생성 시 선택. 공식 문서엔 동작 설명이 없고, 보도(2025-11)로는 공급량이 20억으로 늘고 AI 에이전트가 첫 24시간 매매, 남은 에이전트 물량은 소각 | 수수료 수령 주소가 다름(reserved 8개), synthetic migration 없음, multi-hop 불가. 초반 가격 움직임이 다르게 나올 수 있음 |
| **Holder rewards** (2026-09) | 크리에이터 수수료가 홀더에게 분배됨. 생성 시 선택 또는 CTO로 전환, 되돌릴 수 없음 | 거래 인스트럭션은 동일. 이벤트에 `holder_rewards_bps / holder_rewards` 추가 |
| Cashback (폐지) | 크리에이터 수수료를 매수자에게 캐시백. 신규 생성 불가, 기존 코인은 계속 거래 | v3/PumpSwap v2 사용 불가 → 구세대 인스트럭션 필요 |
| USDC 페어 | quote가 USDC (2026-05 도입) | 레거시 인스트럭션으로는 거래 불가, v2/v3만 |
| Pump 코인 페어 | 다른 pump 코인을 quote로 (깊이 2까지: SOL → A → B → C) | 체인의 각 코인 리저브를 실시간으로 읽어야 가격 계산 가능 |

- 레거시 `create`(SPL Token 민트)로 만든 오래된 코인도 여전히 거래됩니다. `base_token_program`을 코인마다 확인할 것.
- 크리에이터 수수료 지갑 변경은 출시 후 1회로 제한 (2026-03, "anti-vamping"; 공식 문서가 아닌 [CoinMarketCap 보도](https://coinmarketcap.com/top-stories/69c46a058f4fa15bd954557a/) 기준).
- 크리에이터 수수료 공유(`sharing_config`): 여러 지갑에 bps로 나눠 지급.

## 9. 신규 코인/거래 감지 (봇 관점 메모)

- 감지 대상 이벤트 (Pump IDL): `CreateEvent`, `TradeEvent`, `CompleteEvent`, `PostCompleteBuyEvent`, `CompletePumpAmmMigrationEvent`, `SweepBondingCurveFeeEvent`.
- PumpSwap IDL: `CreatePoolEvent`, `BuyEvent`, `SellEvent`, `SweepPoolFeeEvent`.
- 계정 목록에 `event_authority` + `program`이 들어가는 건 Anchor `#[event_cpi]` 패턴입니다. 실제 메인넷 거래를 확인해 보니 `TradeEvent`는 로그(`Program data:`)와 self-CPI inner instruction 양쪽에 남습니다. 그래서 `logsSubscribe`만으로도 거래를 감지할 수 있습니다 (12절 참고).
- 실시간성: 공용 RPC는 느리고 제한이 많습니다. 실전 봇은 보통 Geyser gRPC(Yellowstone) 계열이나 유료 RPC의 웹소켓을 씁니다.
- `TradeEvent`의 `ix_name`으로 어떤 세대 인스트럭션이었는지 구분됩니다. v3 거래면 `fee_recipient`가 0 주소입니다.
- CU 한도: 공식 FAQ는 시뮬레이션 대신 **고정 100,000 CU** 정도를 권장 (PDA bump·인자 크기·커브 완료 여부로 사용량이 달라짐).
- 슬리피지: 수수료가 시총 구간에 따라 바뀌므로 수수료를 정확히 계산하지 않으면 슬리피지 에러가 납니다.
- 민트 주소가 `...pump`로 끝나는 건 pump.fun 프론트엔드가 vanity 주소를 쓰기 때문으로 보입니다. 프로그램 규칙은 아니니 필터로 쓰면 SDK/직접 생성 코인을 놓칩니다.

## 10. 최근 변경 타임라인

| 날짜 | 변경 | 봇 영향 |
| --- | --- | --- |
| 2025-09-01 | 동적 수수료(시총 구간), 거래에 fee 계정 2개 필수화 | 수수료 계산 필수 |
| 2025-11 | `create_v2`(Token-2022), Mayhem 모드 | 토큰 프로그램 분기 |
| 2026-01 | 크리에이터 수수료 공유 | `sharing_config` 계정 |
| 2026-02 | Cashback 모드 | (현재 폐지) |
| 2026-03 | 영구 수수료 분배(공식 커밋), 수수료 지갑 변경 1회 제한(보도) | — |
| 2026-04-28 | buyback 수수료 수령 주소 8개 추가 (breaking) | 계정 수 변경 |
| 2026-05 | `buy_v2/sell_v2`, USDC 페어 코인 | 통합 인터페이스 |
| 2026-07-16 | PumpSwap `virtual_quote_reserves` 추가 (breaking, 7/20 기한) | effective reserves로 가격 계산 |
| 2026-09-12 | Holder rewards 코인, cashback 신규 생성 중단 | 이벤트 필드 추가 |
| 2026-09-30 | `virtual_quote_reserves` 음수 가능 | signed 처리 |
| 2026-10-08 | v3 거래(17계정), multi-hop, pump 코인 quote, 수수료 적립·sweep, synthetic migration | 최신 인터페이스 |
| 2026-10-13 | pump 코인 quote 깊이 2 | — |

**배운 점:** 계정이 append 방식으로 계속 길어지고, 몇 달마다 breaking change가 있습니다. 봇은 IDL/SDK 버전을 고정하고, 공식 repo 커밋을 주기적으로 확인해야 합니다.

## 11. 비즈니스 동향 (참고만, 뉴스 출처)

- 2025년 총 프로토콜 수익 약 $971M, 2026년은 연환산 약 $320M 수준으로 감소 ([CoinDesk, 2026-04-29](https://www.coindesk.com/markets/2026/04/29/pump-fun-burns-36-of-pump-supply-in-usd370-million-wipe-locks-50-revenue-into-ongoing-buybacks)).
- 2026-04: 수익 100% PUMP 바이백 정책을 50% 바이백 / 50% 운영으로 변경, 매입분(유통량의 약 36%) 소각 (같은 기사).
- 2026-07부터 매월 팀·투자자 언락 진행 중, 2029년까지 계속 ([Messari](https://messari.io/project/pump-fun)).
- Mayhem 모드는 출시 첫 주 코인 생성 수에 큰 변화가 없었다는 보도 ([The Block](https://www.theblock.co/post/379285/pump-funs-new-mayhem-mode-fails-boost-token-launches-revenue)).

## 12. 아직 확인 못 한 것 (Phase 1 남은 질문)

- [ ] Mayhem 모드의 현재 동작 (공식 문서에 설명 없음, 2025-11 보도 기준 정보만 있음)
- [x] 이벤트 위치 확인 (2026-10-15, 메인넷 거래 4건): `TradeEvent`가 로그(`Program data:`)와 self-CPI inner instruction **둘 다**에 남음 → `logsSubscribe`로 감지 가능. `CreateEvent`는 아직 직접 확인 안 함. 같은 샘플에서 레거시 `Sell` / `BuyExactSolIn`이 여전히 많이 쓰이고 있었음.
- [ ] 데브넷에서 v3 매수/매도를 직접 보내보고 수식 결과와 실제 체결값 비교
- [ ] 하루 코인 생성 수, 졸업 비율 같은 통계 (봇 전략 검토용)
- [ ] pump.fun 프론트엔드 API(비공식)의 현재 상태와 이용 약관

## 참고 링크

- 공식 문서: https://github.com/pump-fun/pump-public-docs
- TS SDK: https://www.npmjs.com/package/@pump-fun/pump-sdk/v/4.1.0
- PumpSwap SDK: https://www.npmjs.com/package/@pump-fun/pump-swap-sdk/v/2.1.0
- Rust 클라이언트: https://crates.io/crates/pump-rust-client/0.4.1
- Pump 프로그램 (Solscan): https://solscan.io/account/6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P
- PumpSwap 프로그램 (Solscan): https://solscan.io/account/pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA
