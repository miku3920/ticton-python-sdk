from __future__ import annotations
from decimal import Decimal
import logging
import asyncio
import time

# from tonsdk.utils import Address
from tonsdk.utils import bytes_to_b64str
from tonsdk.boc import Cell, begin_cell
from tonsdk.contract.wallet import Wallets
from tonsdk.contract import Address
from tonpy import CellSlice
from typing import Dict, Tuple, Optional, Literal, Callable, TypedDict, List, Type
from .arithmetic import FixedFloat, to_token, token_to_float
from os import getenv

# from .toncenter import TonCenterClient
from pytoncenter import get_client, AsyncTonCenterClientV3
from pytoncenter.v3.models import (
    AddressLike,
    RunGetMethodRequest,
    GetAccountRequest,
    GetJettonWalletsRequest,
    GetMethodParameterInput,
    GetWalletRequest,
    GetTransactionRequest,
    GetSpecifiedJettonWalletRequest,
    ExternalMessage,
)
from pytoncenter.address import Address as PyAddress
from .decoder import (
    OracleMetadata,
    OracleMetadataDecoder,
    AlarmMetadata,
    AlarmMetadataDecoder,
    AlarmAddressDecoder,
    EstimateData,
    EstimateDataDecoder,
)

__all__ = ["TicTonAsyncClient"]


class TicTonAsyncClient:
    def __init__(
        self,
        metadata: OracleMetadata,
        toncenter: AsyncTonCenterClientV3,
        oracle_addr: AddressLike,
        mnemonics: Optional[str] = None,
        wallet_version: Literal[
            "v2r1", "v2r2", "v3r1", "v3r2", "v4r1", "v4r2", "hv2"
        ] = "v4r2",
        threshold_price: float = 0.7,
        *,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        if mnemonics is not None:
            _, _, _, self.wallet = Wallets.from_mnemonics(mnemonics.split(" "), wallet_version)  # type: ignore
        self.oracle = PyAddress(oracle_addr)
        if logger is None:
            self.logger = logging.getLogger(__name__)
            self.logger.setLevel(logging.INFO)
            console_handler = logging.StreamHandler()
            self.logger.addHandler(console_handler)
        else:
            self.logger = logger

        self.toncenter = toncenter

        self.threshold_price = threshold_price
        self.metadata = metadata

        self.logger.info("TicTonAsyncClient initialized")

    @classmethod
    async def init(
        cls: Type[TicTonAsyncClient],
        mnemonics: Optional[str] = None,
        oracle_addr: Optional[str] = None,
        toncenter_api_key: Optional[str] = None,
        wallet_version: Literal[
            "v2r1", "v2r2", "v3r1", "v3r2", "v4r1", "v4r2", "hv2"
        ] = "v4r2",
        threshold_price: float = 0.01,
        *,
        testnet: bool = True,
        logger: Optional[logging.Logger] = None,
    ) -> TicTonAsyncClient:
        mnemonics = getenv("TICTON_WALLET_MNEMONICS", mnemonics)
        wallet_version = getenv("TICTON_WALLET_VERSION", wallet_version)  # type: ignore
        oracle_addr_str = getenv("TICTON_ORACLE_ADDRESS", oracle_addr)
        toncenter_api_key = getenv("TICTON_TONCENTER_API_KEY", toncenter_api_key)
        threshold_price = float(getenv("TICTON_THRESHOLD_PRICE", threshold_price))
        assert (
            mnemonics is not None
        ), "mnemonics must be provided, you can either pass it as a parameter or set TICTON_WALLET_MNEMONICS environment variable"
        assert (
            oracle_addr_str is not None
        ), "oracle_addr must be provided, you can either pass it as a parameter or set TICTON_ORACLE_ADDRESS environment variable"

        toncenter = get_client(
            version="v3",
            network="testnet" if testnet else "mainnet",
            custom_api_key=toncenter_api_key,
        )

        raw_oracle_metadata = await toncenter.run_get_method(
            RunGetMethodRequest(
                address=oracle_addr_str, method="getOracleData", stack=[]
            )
        )
        metadata = OracleMetadataDecoder().decode(raw_oracle_metadata)

        return cls(
            metadata=metadata,
            toncenter=toncenter,
            mnemonics=mnemonics,
            oracle_addr=oracle_addr_str,
            wallet_version=wallet_version,
            threshold_price=threshold_price,
            logger=logger,
        )

    async def _convert_price(self, price: float) -> FixedFloat:
        """
        Adjusts the given price by scaling it to match the decimal difference between the quote and base assets in a token pair.
        """
        assert price > 0, "price must be greater than 0"
        price = float(price)
        return (
            FixedFloat(price)
            * 10**self.metadata.quote_asset_decimals
            / 10**self.metadata.base_asset_decimals
        )

    async def _convert_fixedfloat_to_price(self, price: FixedFloat) -> float:
        """
        Adjusts the given price by scaling it to match the decimal difference between the quote and base assets in a token pair.
        """
        assert isinstance(price, FixedFloat), "price must be a FixedFloat"
        return (
            price.to_float()
            * 10**self.metadata.base_asset_decimals
            / 10**self.metadata.quote_asset_decimals
        )

    def assert_wallet_exists(self):
        assert hasattr(self, "wallet"), "wallet is not found"

    async def _get_user_balance(self) -> Tuple[Decimal, Decimal]:
        """
        get the user's balance of baseAsset and quoteAsset in nanoTON

        Returns
        -------
        base_asset_balance : Decimal
            The balance of baseAsset in nanoTON
        quote_asset_balance : Decimal
            The balance of quoteAsset in nanoTON
        """
        self.assert_wallet_exists()

        async def _get_balance(
            master_address: PyAddress, account_address: PyAddress
        ) -> Decimal:

            if master_address == PyAddress(
                "0:0000000000000000000000000000000000000000000000000000000000000000"
            ):
                account = await self.toncenter.get_account(
                    GetAccountRequest(address=account_address.to_string())
                )
                return Decimal(account.balance)
            else:
                jetton = await self.toncenter.get_jetton_wallets(
                    GetSpecifiedJettonWalletRequest(
                        owner_address=account_address.to_string(),
                        jetton_address=master_address.to_string(),
                    )
                )
                assert jetton is not None, "jetton wallet does not found"
                return Decimal(jetton.balance)

        base_asset_balance, quote_asset_balance = await self.toncenter.multicall(
            _get_balance(
                self.metadata.base_asset_address,  # type: ignore
                PyAddress(self.wallet.address.to_string(True)),
            ),
            _get_balance(
                self.metadata.quote_asset_address,  # type: ignore
                PyAddress(self.wallet.address.to_string(True)),
            ),
        )

        return (base_asset_balance, quote_asset_balance)

    async def _send(
        self,
        to_address: str,
        amount: int,
        seqno: int,
        body: Cell,
    ):
        """
        _send will send the given amount of tokens to to_address, if dry_run is set to True, it will
        call toncenter simulation api, otherwise it will send the transaction to the network directly.

        Parameters
        ----------
        amount : int
            The amount of TON to be sent
        seqno : int
            The seqno of user's wallet
        body : Cell
            The body of the transaction
        dry_run : bool
            Whether to call toncenter simulation api or not
        """
        self.assert_wallet_exists()
        query = self.wallet.create_transfer_message(
            to_addr=to_address,
            amount=amount,
            seqno=seqno,
            payload=body,
        )
        boc: bytearray = query["message"].to_boc(False)
        result = await self.toncenter.send_message(
            ExternalMessage(boc=bytes_to_b64str(boc))
        )
        return result

    async def _estimate_from_oracle_get_method(
        self,
        alarm_address: AddressLike,
        buy_num: int,
        new_price: int,
    ):
        result = await self.toncenter.run_get_method(
            RunGetMethodRequest(
                address=alarm_address,
                method="getEstimate",
                stack=[
                    {"type": "num", "value": buy_num},
                    {"type": "num", "value": new_price},
                ],
            )
        )
        estimate_data = EstimateDataDecoder().decode(result)
        return (
            estimate_data.can_buy,
            estimate_data.need_baseAsset_amount,
            estimate_data.need_quote_asset_amount,
        )

    async def get_alarm_address(self, alarm_id: int) -> PyAddress:
        result = await self.toncenter.run_get_method(
            RunGetMethodRequest(
                address=self.oracle.to_string(),
                method="getAlarmAddress",
                stack=[
                    GetMethodParameterInput(type="num", value=alarm_id),
                ],
            )
        )
        return AlarmAddressDecoder().decode(result)

    async def get_address_state(self, address: PyAddress) -> str:
        result = await self.toncenter.get_account(GetAccountRequest(address=address))  # type: ignore
        return result.status

    async def _estimate_wind(self, alarm_id: int, buy_num: int, new_price: float):
        alarm_address = await self.get_alarm_address(alarm_id)
        alarm_status = await self.get_address_state(alarm_address)
        assert alarm_status == "active", "alarm is not active"

        alarm_metadata = await self.get_alarm_metadata(alarm_address)

        new_price_ff = await self._convert_price(new_price)
        old_price_ff = FixedFloat(alarm_metadata.base_asset_price, skip_scale=True)
        price_delta = abs(new_price_ff - old_price_ff)

        if price_delta < self.threshold_price:
            return None, None, alarm_metadata

        (
            can_buy,
            need_base_asset,
            need_quote_asset,
        ) = await self._estimate_from_oracle_get_method(
            alarm_address.to_string(), buy_num, int(new_price_ff.raw_value)
        )

        return (
            can_buy,
            (Decimal(need_base_asset), Decimal(need_quote_asset)),
            alarm_metadata,
        )

    async def _can_afford(self, need_base_asset: Decimal, need_quote_asset: Decimal):
        base_asset_balance, quote_asset_balance = await self._get_user_balance()
        gas_fee = 1 * 10**9
        if (
            need_base_asset + gas_fee > base_asset_balance
            or need_quote_asset > quote_asset_balance
        ):
            return False
        return True

    async def _parse(self, in_msg_body: str):
        """
        parse the in_msg_body and out_msg_body
        """
        try:
            cs = CellSlice(in_msg_body)
            opcode = str(hex(cs.load_uint(32)))
            if opcode == "0x7362d09c":
                query_id = cs.load_uint(64)
                amount = cs.load_var_uint(16)
                sender_address = cs.load_address()
                forward_payload: CellSlice = cs.load_ref(as_cs=True)  # type: ignore
                oracle_opcode = forward_payload.load_uint(8)
                if oracle_opcode == 0:
                    expire_at = forward_payload.load_uint(256)
                    base_asset_price = forward_payload.load_int(256)
                    base_asset_price = await self._convert_fixedfloat_to_price(
                        FixedFloat(base_asset_price, skip_scale=True)
                    )
                    return {
                        "Tick": {
                            "watchmaker": sender_address,
                            "base_asset_price": base_asset_price,
                        }
                    }
            elif opcode == "0x8eb5cd4":
                alarm_id = cs.load_int(257)
                timekeeper = cs.load_address()
                new_base_asset_price = cs.load_uint(256)
                cs: CellSlice = cs.load_ref(as_cs=True)  # type: ignore
                new_scale = cs.load_int(257)
                refund_quote_asset_amount = cs.load_int(16)
                base_asset_price = cs.load_uint(256)
                cs: CellSlice = cs.load_ref(as_cs=True)  # type: ignore
                created_at = cs.load_int(257)
                remain_scale = cs.load_int(257)
                new_base_asset_price = await self._convert_fixedfloat_to_price(
                    FixedFloat(new_base_asset_price, skip_scale=True)
                )
                return {
                    "Wind": {
                        "timekeeper": timekeeper,
                        "alarm_id": alarm_id,
                        "new_base_asset_price": new_base_asset_price,
                        "remain_scale": remain_scale - new_scale / 2,
                    }
                }
            elif opcode == "0x54451598":
                query_id = cs.load_uint(257)
                alarm_id = cs.load_uint(257)
                created_at = cs.load_int(257)
                return {"Ring": {"alarm_id": alarm_id, "created_at": created_at}}

            elif opcode == "0x9c0fafb":
                alarm_id = cs.load_uint(256)
                scale = cs.load_uint(32)
                created_at = cs.load_int(257)
                watchmaker = cs.load_address()

                return {
                    "Tock": {
                        "new_alarm_id": alarm_id,
                        "created_at": created_at,
                    }
                }

            elif opcode == "0x89b71d09":
                origin = cs.load_address()
                receiver = cs.load_address()
                amount = cs.load_uint(257)

                return {
                    "JettonMint": {
                        "origin": origin,
                        "receiver": receiver,
                        "amount": amount,
                    }
                }
            return None
        except Exception as e:
            self.logger.error(f"Error while parsing {e}")
            return None

    async def _get_jetton_mint_data(self, out_msg_body):
        result = await self._parse(out_msg_body)
        if result is None:
            return None
        for op, data in result.items():
            if op == "JettonMint":
                return data
        return None

    async def _get_tock_data(self, out_msg_body):
        result = await self._parse(out_msg_body)
        if result is None:
            return None
        for op, data in result.items():
            if op == "Tock":
                return data

        return None

    async def get_alarm_metadata(self, alarm_address: PyAddress) -> AlarmMetadata:
        """
        get the alarm info
        """
        result = await self.toncenter.run_get_method(
            RunGetMethodRequest(
                address=alarm_address.to_string(), method="getAlarmMetadata", stack=[]
            )
        )
        return AlarmMetadataDecoder().decode(result)  # type: ignore

    async def check_alarms(self, alarm_id_list: List[int]):
        self.logger.info("Checking Alarms State")

        address_list = await self.toncenter.multicall(
            [self.get_alarm_address(alarm_id) for alarm_id in alarm_id_list]
        )

        # get alarm state
        state_list = await self.toncenter.multicall(
            [self.get_address_state(address) for address in address_list]
        )

        # update alarm dict
        alarm_dict = {}
        for alarm_id, alarm_address, alarm_state in zip(
            alarm_id_list, address_list, state_list
        ):
            alarm_dict[alarm_id] = {}
            alarm_dict[alarm_id]["state"] = alarm_state
            alarm_dict[alarm_id]["address"] = alarm_address

        return alarm_dict

    async def tick(
        self, price: float, *, timeout: int = 1000, extra_ton: float = 0.1, **kwargs
    ):
        """
        tick will open a position with the given price and timeout, the total amount
        of baseAsset and quoteAsset will be calculated automatically.

        Parameters
        ----------
        price : float
            The price of the position quoteAsset/baseAsset
        timeout : int
            The timeout of the position in seconds
        extra_ton : float
            The extra ton to be sent to the oracle
        dry_run : bool
            Whether to call toncenter simulation api or not

        Examples
        --------
        Assume the token pair is TON/USDT, the price is 2.5 USDT per TON

        >>> client = TicTonAsyncClient(...)
        >>> await client.init()
        >>> await client.tick(2.5)
        """
        assert extra_ton >= 0.1, "extra_ton must be greater than or equal to 0.1"
        assert price > 0, "price must be greater than 0"
        self.assert_wallet_exists()
        expire_at = int(time.time()) + timeout
        base_asset_price = await self._convert_price(price)
        quote_asset_transfered = FixedFloat(
            to_token(price, self.metadata.quote_asset_decimals)
        )
        forward_ton_amount = quote_asset_transfered / base_asset_price + to_token(
            extra_ton, self.metadata.base_asset_decimals
        )
        base_asset_price = int(base_asset_price.raw_value)
        quote_asset_transfered = quote_asset_transfered.to_float()
        forward_ton_amount = int(round(forward_ton_amount.to_float(), 0))
        gas_fee = int(0.13 * 10**9)

        can_afford = await self._can_afford(
            Decimal(forward_ton_amount + gas_fee), quote_asset_transfered
        )
        assert can_afford, "not enough balance"

        forward_info = (
            begin_cell()
            .store_uint(0, 8)
            .store_uint(expire_at, 256)
            .store_uint(base_asset_price, 256)
            .end_cell()
        )

        wallet_info = await self.toncenter.get_wallet(
            GetWalletRequest(address=self.wallet.address.to_string())
        )

        body = (
            begin_cell()
            .store_uint(0xF8A7EA5, 32)
            .store_uint(0, 64)
            .store_coins(quote_asset_transfered)
            .store_address(self.oracle)
            .store_address(self.wallet.address)
            .store_bit(False)
            .store_coins(forward_ton_amount)
            .store_ref(forward_info)
            .end_cell()
        )

        jetton_wallet = await self.toncenter.get_jetton_wallets(
            GetSpecifiedJettonWalletRequest(
                owner_address=self.wallet.address.to_string(),
                jetton_address=self.metadata.quote_asset_address,
            )
        )

        assert jetton_wallet is not None, "jetton wallet does not found"
        assert wallet_info.seqno is not None, "seqno is not found"

        result = await self._send(
            to_address=jetton_wallet.address.to_string(),  # type: ignore
            amount=forward_ton_amount + gas_fee,
            seqno=wallet_info.seqno,
            body=body,
        )

        args = [
            price,
            token_to_float(
                forward_ton_amount + gas_fee, self.metadata.base_asset_decimals
            ),
            token_to_float(quote_asset_transfered, self.metadata.quote_asset_decimals),
        ]
        log_info = (
            "Tick Success, tick price: {}, spend base asset: {}, spend quote asset: {}"
        ).format(*args)
        self.logger.info(log_info)

        return result

    async def ring(self, alarm_id: int, **kwargs):
        """
        ring will close the position with the given alarm_id

        Parameters
        ----------
        alarm_id : int
            The alarm_id of the position to be closed
        dry_run : bool
            Whether to call toncenter simulation api or not

        Examples
        --------
        >>> client = TicTonAsyncClient.init(...)
        >>> await client.ring(123)
        """
        self.assert_wallet_exists()
        alarm_address = await self.get_alarm_address(alarm_id)
        alarm_state = await self.get_address_state(alarm_address)
        assert alarm_state == "active", "Ring: alarm is not exist"
        wallet = await self.toncenter.get_wallet(
            GetWalletRequest(address=self.wallet.address.to_string())
        )
        assert wallet.seqno is not None, "Ring: seqno is not found in wallet info"
        gas_fee = int(0.35 * 10**9)
        body = (
            begin_cell()
            .store_uint(0xC3510A29, 32)
            .store_uint(1, 257)
            .store_uint(alarm_id, 257)
            .end_cell()
        )  # query_id cannot be 0
        result = await self._send(
            to_address=self.oracle.to_string(),
            amount=gas_fee,
            seqno=wallet.seqno,
            body=body,
        )

        args = [alarm_id]
        log_info = "Ring Success, alarm id: {}".format(*args)
        self.logger.info(log_info)

        return result

    async def wind(
        self,
        alarm_id: int,
        buy_num: int,
        new_price: float,
        skip_estimate: float = False,
        need_quote_asset: Optional[Decimal] = None,
        need_base_asset: Optional[Decimal] = None,
        **kwargs,
    ):
        """
        wind will arbitrage the position with the given alarm_id, buy_num and new_price

        Parameters
        ----------
        alarm_id : int
            The alarm_id of the position to be arbitrage
        buy_num : int
            The number of tokens to be bought, at least 1.
        new_price : float
            The new price of the position quoteAsset/baseAsset
        dry_run : bool
            Whether to call toncenter simulation api or not

        Examples
        --------
        Assume the token pair is TON/USDT, the price is 2.5 USDT per TON. The position is opened with 1 TON and 2.5 USDT with index 123.
        The new price is 5 USDT per TON, the buy_num is 1.

        >>> client = TicTonAsyncClient.init(...)
        >>> await client.wind(123, 1, 5)
        """
        self.assert_wallet_exists()
        assert new_price > 0, "new_price must be greater than 0"
        assert isinstance(buy_num, int), "buy_num must be an int"
        assert buy_num > 0, "buy_num must be greater than 0"

        new_price_ff = await self._convert_price(new_price)

        if skip_estimate:
            assert need_base_asset is not None, "need_base_asset must be provided"
            assert need_quote_asset is not None, "need_quote_asset must be provided"
        else:
            can_buy, need_asset_tup, _ = await self._estimate_wind(
                alarm_id, buy_num, new_price
            )
            assert can_buy, "Buy num is too large"
            assert (
                need_asset_tup is not None
            ), "The price difference is smaller than threshold price"

            need_base_asset, need_quote_asset = need_asset_tup

        wallet = await self.toncenter.get_wallet(
            GetWalletRequest(address=self.wallet.address.to_string())
        )
        assert wallet.seqno is not None, "seqno is not found in wallet info"

        gas_fee = int(0.5 * 10**9)

        can_afford = await self._can_afford(
            Decimal(need_base_asset + gas_fee), need_quote_asset
        )
        assert can_afford, "not enough balance"

        forward_info = (
            begin_cell()
            .store_uint(1, 8)
            .store_uint(alarm_id, 256)
            .store_uint(buy_num, 32)
            .store_uint(int(new_price_ff.raw_value), 256)
            .end_cell()
        )

        body = (
            begin_cell()
            .store_uint(0xF8A7EA5, 32)
            .store_uint(0, 64)
            .store_coins(int(need_quote_asset))
            .store_address(self.oracle)
            .store_address(self.wallet.address)
            .store_bit(False)
            .store_coins(int(need_base_asset) + gas_fee)
            .store_ref(forward_info)
            .end_cell()
        )

        jetton_wallet = await self.toncenter.get_jetton_wallets(
            GetSpecifiedJettonWalletRequest(
                owner_address=self.wallet.address.to_string(),
                jetton_address=self.metadata.quote_asset_address,
            )
        )

        assert jetton_wallet is not None, "jetton wallet does not found"

        result = await self._send(
            to_address=jetton_wallet.address.to_string(),  # type: ignore
            amount=int(need_base_asset) + gas_fee,
            seqno=wallet.seqno,
            body=body,
        )

        args = [
            alarm_id,
            buy_num,
            new_price,
            token_to_float(need_base_asset, self.metadata.base_asset_decimals),
            token_to_float(need_quote_asset, self.metadata.quote_asset_decimals),
        ]
        log_info = (
            "Wind Success, alarm id: {}, buy num: {}, wind price: {}, spend base asset: {}, spend quote asset: {}"
        ).format(*args)
        self.logger.info(log_info)

        return result

    async def subscribe(
        self,
        on_tick_success: Optional[Callable] = None,
        on_ring_success: Optional[Callable] = None,
        on_wind_success: Optional[Callable] = None,
        to_lt: int = 0,
    ):
        """
        subscribe will subscribe the oracle's transactions, handle the transactions and call the
        given callbacks.

        on_tick_success params:
        - watchmaker: str
        - base_asset_price: float
        - new_alarm_id: int
        - created_at: int

        on_ring_success params:
        - alarm_id: int
        - created_at: int
        - origin: str
        - receiver: str
        - amount: int

        on_wind_success params:
        - timekeeper: str
        - alarm_id: int
        - new_base_asset_price: float
        - remain_scale: int
        - new_alarm_id: int
        - created_at: int
        """
        self.logger.info(f"Start Subscribing: {self.oracle.to_string()}")
        toncenter_v2 = get_client(
            version="v2", network="testnet", custom_api_key=self.toncenter.api_key
        )
        while True:
            try:
                if to_lt == 0:
                    params = {"address": self.oracle.to_string(), "limit": 20}
                else:
                    params = {
                        "address": self.oracle.to_string(),
                        "latest_lt": to_lt,
                    }
                result = await toncenter_v2.get_transactions(**params)

                # start from the last transaction
                for transaction_tree in result[::-1]:
                    tx_lt = transaction_tree["transaction_id"]["lt"]
                    in_msg_body = transaction_tree["in_msg"]["msg_data"]["body"]  # type: ignore
                    if len(transaction_tree["out_msgs"]) == 0:
                        out_msg_body = ""
                    elif "body" not in transaction_tree["out_msgs"][0]["msg_data"]:
                        out_msg_body = ""
                    else:
                        out_msg_body = transaction_tree["out_msgs"][0]["msg_data"][
                            "body"
                        ]
                    if to_lt < int(tx_lt):
                        to_lt = int(tx_lt) + 1
                    result = await self._parse(in_msg_body)

                    if result is None:
                        continue
                    for op, data in result.items():
                        if op == "Tick":
                            tock_data = await self._get_tock_data(out_msg_body)
                            if tock_data is not None:
                                data.update(tock_data)
                            if on_tick_success is not None:
                                await on_tick_success(**data)
                        elif op == "Ring":
                            out_msg_body = transaction_tree["out_msgs"][1]["msg_data"]["body"]  # type: ignore
                            reward_data = await self._get_jetton_mint_data(out_msg_body)
                            if reward_data is not None:
                                data.update(reward_data)
                            if on_ring_success is not None:
                                await on_ring_success(**data)
                        elif op == "Wind":
                            tock_data = await self._get_tock_data(out_msg_body)
                            if tock_data is not None:
                                data.update(tock_data)
                            if on_wind_success is not None:
                                await on_wind_success(**data)

            except Exception as e:
                self.logger.error(f"Error while subscribing {e}")
                continue
