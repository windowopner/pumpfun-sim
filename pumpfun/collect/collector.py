"""
Pump.fun event collector.

Subscribes to the Pump program's logs over a Solana RPC websocket
(`logsSubscribe` with a `mentions` filter), decodes CreateEvent, TradeEvent
and the curve completion / migration events with the official IDL, and saves
them to SQLite.

    pumpfun collect                            # public mainnet RPC, data/pump.db
    SOLANA_WS_URL=wss://... pumpfun collect --db data/pump.db

Stop with Ctrl-C. Every websocket session is recorded in the `connections`
table, so the time between a disconnect and the next connect is a known gap.
"""

import argparse
import asyncio
import base64
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

import websockets

from pumpfun.collect.idl_decoder import IdlEventDecoder
from pumpfun.collect.logs import is_truncated, program_data
from pumpfun.collect.store import SAVED_EVENTS, EventStore
from pumpfun.paths import IDL_PATH, default_db

DEFAULT_WS_URL = "wss://api.mainnet-beta.solana.com"

COMMIT_INTERVAL      = 1.0    # seconds between SQLite commits
STALE_TIMEOUT        = 60.0   # reconnect if no log notification arrives for this long
RECONNECT_DELAY_INIT = 1.0
RECONNECT_DELAY_MAX  = 60.0

PHANTOM_SIGNATURE = "1" * 64

log = logging.getLogger("collector")


@dataclass
class Stats:
    txs:            int = 0
    failed_txs:     int = 0
    phantom_txs:    int = 0
    truncated_txs:  int = 0
    decode_errors:  int = 0
    duplicates:     int = 0
    saved:          Dict[str, int] = field(default_factory=dict)
    last_event_ts:  Optional[int] = None

    def line(self) -> str:
        saved = ", ".join(f"{k.replace('Event', '')}={v}" for k, v in sorted(self.saved.items())) or "none"
        lag = f"{time.time() - self.last_event_ts:.0f}s" if self.last_event_ts else "-"
        return (f"txs={self.txs} saved[{saved}] failed_tx={self.failed_txs} phantom={self.phantom_txs} "
                f"truncated={self.truncated_txs} decode_err={self.decode_errors} "
                f"dupes={self.duplicates} lag={lag}")


class Collector:
    def __init__(self, ws_url: str, db_path: str, commitment: str, stats_interval: float):
        self.ws_url = ws_url
        self.commitment = commitment
        self.stats_interval = stats_interval
        self.decoder = IdlEventDecoder.from_file(IDL_PATH)
        self.program_id = self.decoder.address
        self.store = EventStore(db_path)
        self.stats = Stats()

    async def run(self):
        delay = RECONNECT_DELAY_INIT
        while True:
            conn_id = self._conn_id = self.store.open_connection()
            self._first_slot: Optional[int] = None
            self._last_slot: Optional[int] = None
            reason = "closed"
            try:
                await self._session()
                delay = RECONNECT_DELAY_INIT
            except asyncio.CancelledError:
                reason = "stopped"
                raise
            except Exception as e:  # network errors, RPC errors, stale stream
                reason = f"{type(e).__name__}: {e}"[:300]
                log.warning("connection lost (%s), reconnecting in %.0fs", reason, delay)
            finally:
                self.store.close_connection(conn_id, self._first_slot, self._last_slot, reason)
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_DELAY_MAX)

    async def _session(self):
        async with websockets.connect(self.ws_url, ping_interval=20, max_size=None) as ws:
            await ws.send(json.dumps({
                "jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
                "params": [{"mentions": [self.program_id]}, {"commitment": self.commitment}],
            }))
            ack = json.loads(await asyncio.wait_for(ws.recv(), STALE_TIMEOUT))
            if "error" in ack:
                raise RuntimeError(f"logsSubscribe rejected: {ack['error']}")
            log.info("subscribed to %s logs (%s), subscription %s",
                     self.program_id, self.commitment, ack.get("result"))

            last_commit = last_stats = last_notification = time.time()
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), STALE_TIMEOUT))
                now = time.time()
                if msg.get("method") == "logsNotification":
                    self._handle(msg["params"]["result"])
                    last_notification = now
                elif now - last_notification >= STALE_TIMEOUT:
                    raise TimeoutError(f"no log notification for {STALE_TIMEOUT:.0f}s")
                if now - last_commit >= COMMIT_INTERVAL:
                    self.store.update_connection(self._conn_id, self._first_slot, self._last_slot)
                    self.store.commit()
                    last_commit = now
                if now - last_stats >= self.stats_interval:
                    log.info(self.stats.line())
                    last_stats = now

    def _handle(self, result: dict):
        slot = result["context"]["slot"]
        value = result["value"]
        self.stats.txs += 1
        # The RPC also sends notifications with an all-zero signature (base58 "111...1").
        # Their trades never landed: the state they start from is taken by a real trade.
        if value["signature"] == PHANTOM_SIGNATURE:
            self.stats.phantom_txs += 1
            return
        if self._first_slot is None:
            self._first_slot = slot
        self._last_slot = slot

        # A failed transaction changed nothing on chain, so its events never happened.
        if value.get("err") is not None:
            self.stats.failed_txs += 1
            return
        logs = value.get("logs") or []
        if is_truncated(logs):
            self.stats.truncated_txs += 1

        for index, payload in enumerate(program_data(logs, self.program_id)):
            try:
                event = self.decoder.decode(payload)
            except Exception as e:
                self.stats.decode_errors += 1
                log.debug("decode error in %s: %s", value["signature"], e)
                continue
            if event is None or event.name not in SAVED_EVENTS:
                continue
            if self.store.add_event(event.name, event.fields, value["signature"], index, slot):
                self.stats.saved[event.name] = self.stats.saved.get(event.name, 0) + 1
                if event.fields.get("timestamp"):
                    self.stats.last_event_ts = event.fields["timestamp"]
                if event.name == "CreateEvent":
                    f = event.fields
                    log.info("new coin %s (%s) %s", f["symbol"], f["name"][:32], f["mint"])
            else:
                self.stats.duplicates += 1


def add_arguments(parser: argparse.ArgumentParser):
    parser.add_argument("--ws-url", default=os.environ.get("SOLANA_WS_URL", DEFAULT_WS_URL),
                        help="Solana RPC websocket URL (default: $SOLANA_WS_URL or public mainnet)")
    parser.add_argument("--db", default=None, help="SQLite file (default: data/pump.db)")
    parser.add_argument("--commitment", default="confirmed", choices=["processed", "confirmed", "finalized"])
    parser.add_argument("--stats-interval", type=float, default=30.0, help="seconds between stats lines")
    parser.add_argument("--quiet-coins", action="store_true", help="do not log every new coin")


def main(args: argparse.Namespace):
    args.db = args.db or str(default_db())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    if args.quiet_coins:
        log.addFilter(lambda r: not r.getMessage().startswith("new coin"))

    collector = Collector(args.ws_url, args.db, args.commitment, args.stats_interval)
    log.info("writing to %s", args.db)
    try:
        asyncio.run(collector.run())
    except KeyboardInterrupt:
        pass
    finally:
        log.info("stopped. %s", collector.stats.line())
        collector.store.close()

