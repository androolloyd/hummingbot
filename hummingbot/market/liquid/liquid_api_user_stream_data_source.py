#!/usr/bin/env python

import asyncio
import logging
from typing import (
    Any,
    AsyncIterable,
    Dict,
    Optional,
    List,
)
import time
import ujson
import websockets
from websockets.exceptions import ConnectionClosed
from hummingbot.logger import HummingbotLogger
from hummingbot.core.data_type.user_stream_tracker_data_source import UserStreamTrackerDataSource
from hummingbot.core.data_type.order_book_message import OrderBookMessage
from hummingbot.market.liquid.constants import Constants
from hummingbot.market.liquid.liquid_api_order_book_data_source import LiquidAPIOrderBookDataSource
from hummingbot.market.liquid.liquid_auth import LiquidAuth
from hummingbot.market.liquid.liquid_order_book import LiquidOrderBook


class LiquidAPIUserStreamDataSource(UserStreamTrackerDataSource):

    _lausds_logger: Optional[HummingbotLogger] = None

    @classmethod
    def logger(cls) -> HummingbotLogger:
        if cls._lausds_logger is None:
            cls._lausds_logger = logging.getLogger(__name__)
        return cls._lausds_logger

    def __init__(self, liquid_auth: LiquidAuth, trading_pairs: Optional[List[str]] = []):
        self._liquid_auth: LiquidAuth = liquid_auth
        self._trading_pairs = trading_pairs
        self._current_listen_key = None
        self._listen_for_user_stream_task = None
        super().__init__()

    @property
    def order_book_class(self):
        """
        *required
        Get relevant order book class to access class specific methods
        :returns: OrderBook class
        """
        return LiquidOrderBook

    async def listen_for_user_stream(self, ev_loop: asyncio.BaseEventLoop, output: asyncio.Queue):
        """
        *required
        Subscribe to user stream via web socket, and keep the connection open for incoming messages
        :param ev_loop: ev_loop to execute this function in
        :param output: an async queue where the incoming messages are stored
        """
        while True:
            try:
                async with websockets.connect(Constants.BAEE_WS_URL) as ws:
                    ws: websockets.WebSocketClientProtocol = ws

                    # Send a auth request first
                    auth_request: Dict[str, Any] = {
                        "event": Constants.WS_AUTH_REQUEST_EVENT,
                        "data": self._liquid_auth.get_ws_auth_data()
                    }
                    await ws.send(ujson.dumps(auth_request))

                    active_markets_df = await LiquidAPIOrderBookDataSource.get_active_exchange_markets()
                    quoted_currencies = [
                        active_markets_df.loc[trading_pair, 'quoted_currency']
                        for trading_pair in self._trading_pairs
                    ]

                    for trading_pair, quoted_currency in zip(self._trading_pairs, quoted_currencies):
                        subscribe_request: Dict[str, Any] = {
                            "event": Constants.WS_PUSHER_SUBSCRIBE_EVENT,
                            "data": {
                                "channel": Constants.WS_USER_ACCOUNTS_SUBSCRIPTION.format(
                                    quoted_currency=quoted_currency.lower()
                                )
                            }
                        }
                        await ws.send(ujson.dumps(subscribe_request))
                    async for raw_msg in self._inner_messages(ws):
                        diff_msg = ujson.loads(raw_msg)

                        event_type = diff_msg.get('event', None)
                        if event_type == 'updated':
                            # Channel example: 'user_executions_cash_ethusd'
                            trading_pair = diff_msg.get('channel').split('_')[-1].upper()
                            diff_timestamp: float = time.time()
                            diff_msg: OrderBookMessage = LiquidOrderBook.diff_message_from_exchange(
                                diff_msg,
                                diff_timestamp,
                                metadata={"trading_pair": trading_pair}
                            )
                            output.put_nowait(diff_msg)
                        elif not event_type:
                            raise ValueError(f"Liquid Websocket message does not contain an event type - {diff_msg}")
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger().error("Unexpected error with Liquid WebSocket connection. "
                                    "Retrying after 30 seconds...", exc_info=True)
                await asyncio.sleep(30.0)

    async def _inner_messages(self,
                              ws: websockets.WebSocketClientProtocol) -> AsyncIterable[str]:
        """
        Generator function that returns messages from the web socket stream
        :param ws: current web socket connection
        :returns: message in AsyncIterable format
        """
        # Terminate the recv() loop as soon as the next message timed out, so the outer loop can reconnect.
        try:
            while True:
                try:
                    msg: str = await asyncio.wait_for(ws.recv(), timeout=Constants.MESSAGE_TIMEOUT)
                    yield msg
                except asyncio.TimeoutError:
                    try:
                        pong_waiter = await ws.ping()
                        await asyncio.wait_for(pong_waiter, timeout=Constants.PING_TIMEOUT)
                    except asyncio.TimeoutError:
                        raise
        except asyncio.TimeoutError:
            self.logger().warning("WebSocket ping timed out. Going to reconnect...")
            return
        except ConnectionClosed:
            return
        finally:
            await ws.close()
