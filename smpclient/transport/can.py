"""A serial SMPTransport."""

import asyncio
import logging
import math
from enum import IntEnum, unique, Enum
from functools import cached_property
from typing import Final
import can
from serial import Serial
from smp import packet as smppacket

logger = logging.getLogger(__name__)
TX_ID = 123
RX_ID = 122
CAN_DL_SIZE = 8
STD_MAX_ID = 0x7FF
AWAIT_MSG_TIMEOUT = 0.001
MAX_TIMEOUT_CNT = 10000


def _base64_cost(size: int) -> int:
    """The worst case size required to encode `size` `bytes`."""

    if size == 0:
        return 0

    return math.ceil(4 / 3 * size) + 2


def _base64_max(size: int) -> int:
    """Given a max `size`, return how many bytes can be encoded."""

    if size < 4:
        return 0

    return math.floor(3 / 4 * size) - 2

class CANDevice(Enum):
    PeakUSB = 'PeakUSB'
    """
    Peak USB CAN
    """

    SeedStudio = 'SeedStudio'
    """
    The USB-CAN adapter from seedstudio
    """

class SMPCANTransport:

    class _ReadBuffer:
        """The state of the read buffer."""

        @unique
        class State(IntEnum):
            SMP = 0
            """An SMP start or continue delimiter has been received and the
            `smp_buffer` is being filled with the remainder of the SMP packet.
            """

            SER = 1
            """The SMP start delimiter has not been received and the
            `ser_buffer` is being filled with data.
            """

        def __init__(self) -> None:
            self.smp = bytearray([])
            """The buffer for the SMP packet."""

            self.ser = bytearray([])
            """The buffer for serial data that is not part of an SMP packet."""

            self.state = SMPCANTransport._ReadBuffer.State.SER
            """The state of the read buffer."""

    def __init__(
        self,
        mtu: int = 512,
        device: CANDevice = CANDevice.PeakUSB
    ) -> None:
        self._mtu: Final = mtu
        self._device = device
        self._bus = None
        self._ext_tx = TX_ID > STD_MAX_ID
        self._ext_rx = RX_ID > STD_MAX_ID
        self._msg_queue = asyncio.Queue()
        self._buffer = SMPCANTransport._ReadBuffer()
        self.loop = asyncio.new_event_loop()
        logger.debug(f"Initialized {self.__class__.__name__}")

    def _rx_cb(self, msg: can.Message) -> None:
        if msg.arbitration_id != RX_ID:
            return
        result = self.loop.run_until_complete(self._msg_queue.put(msg))


    async def connect(self, address: str) -> None:
        logger.debug(f"Connecting to %s "%(self._device))
        if self._device == CANDevice.PeakUSB:
            self._bus = can.interface.Bus(interface='pcan',
                                          fd=False,
                                          nom_brp=1,
                                          nom_tseg1=129,
                                          nom_tseg2=30,
                                          nom_sjw=1,
                                          data_brp=1,
                                          data_tseg1=9,
                                          data_tseg2=6,
                                          data_sjw=1,
                                          bitrate=1000000,
                                          channel="PCAN_USBBUS1")
        elif self._device == CANDevice.SeedStudio:
            self._bus = can.interface.Bus(bustype='seeedstudio',
                                         channel=address,
                                         baudrate=2000000,
                                         bitrate=1000000,
                                         frame_type='STD',
                                         operation_mode='normal')
        self._reader = can.AsyncBufferedReader()
        listeners: List[can.notifier.MessageRecipient] = [
            self._rx_cb,  # Callback function
            self._reader  # AsyncBufferedReader() listener
        ]
        self.notifier = can.Notifier(self._bus, listeners)
        logger.debug(f"Connected to %s "%(self._device))

    async def disconnect(self) -> None:
        logger.debug(f"Disconnecting from {self._device=}")
        if self._bus is not None:
            self._bus.shutdown()
        logger.debug(f"Disconnected from {self._device=}")

    async def send(self, data: bytes) -> None:
        logger.debug(f"Sending {len(data)} bytes")
        for packet in smppacket.encode(data, line_length=self.mtu):
            if len(packet) > self.mtu:  # pragma: no cover
                raise Exception(
                    f"Encoded packet size {len(packet)} exceeds {self.mtu=}, this is a bug!"
                )

            can_packets = [packet[i:i + CAN_DL_SIZE] for i in range(0, len(packet), CAN_DL_SIZE)]
            logger.debug(f"Writing encoded packet of size {len(packet)}B; {self.mtu=}")
            logger.debug(f"Total CAN Msgs {len(can_packets)}")
            for can_p in can_packets:
                msg = can.Message(arbitration_id=TX_ID,
                                      data=can_p,
                                      is_extended_id=self._ext_tx)
                self._bus.send(msg)

        logger.debug(f"Sent {len(data)} bytes")

    async def receive(self) -> bytes:
        decoder = smppacket.decode()
        next(decoder)

        #logger.info("Waiting for response")
        while True:
            try:
                b = await self._readuntil()
                decoder.send(b)
            except StopIteration as e:
                logger.debug(f"Finished receiving {len(e.value)} byte response")
                return e.value

    async def _readuntil(self) -> bytes:
        """Read `bytes` until the `delimiter` then return the `bytes` including the `delimiter`."""

        START_DELIMITER: Final = smppacket.SIXTY_NINE
        CONTINUE_DELIMITER: Final = smppacket.FOUR_TWENTY
        END_DELIMITER: Final = b"\n"

        # fake async until I get around to replacing pyserial

        i_smp_start = 0
        i_smp_end = 0
        i_start: int | None = None
        i_continue: int | None = None
        initial_msg = True
        timeout_cnt = 0
        while True:
            try:
                msg = await asyncio.wait_for(self._msg_queue.get(), timeout=AWAIT_MSG_TIMEOUT) # type: can.Message
            except:
                timeout_cnt += 1
                if timeout_cnt >= MAX_TIMEOUT_CNT:
                    raise Exception(
                        f"CAN RX Timeout"
                    )
                continue
            timeout_cnt = 0
            if self._buffer.state == SMPCANTransport._ReadBuffer.State.SER:
                # read the entire OS buffer
                try:
                    self._buffer.ser.extend(msg.data)
                except StopIteration:
                    pass

                try:  # search the buffer for the index of the start delimiter
                    i_start = self._buffer.ser.index(START_DELIMITER)
                except ValueError:
                    i_start = None

                try:  # search the buffer for the index of the continue delimiter
                    i_continue = self._buffer.ser.index(CONTINUE_DELIMITER)
                except ValueError:
                    i_continue = None

                if i_start is not None and i_continue is not None:
                    i_smp_start = min(i_start, i_continue)
                elif i_start is not None:
                    i_smp_start = i_start
                elif i_continue is not None:
                    i_smp_start = i_continue
                else:  # no delimiters found yet, clear non SMP data and wait
                    while True:
                        try:  # search the buffer for newline characters
                            i = self._buffer.ser.index(b"\n")
                            try:  # log as a string if possible
                                logger.warning(
                                    f"{self._conn.port}: {self._buffer.ser[:i].decode()}"
                                )
                            except UnicodeDecodeError:  # log as bytes if not
                                logger.warning(f"{self._conn.port}: {self._buffer.ser[:i].hex()}")
                            self._buffer.ser = self._buffer.ser[i + 1 :]
                        except ValueError:
                            break
                    continue

                if i_smp_start != 0:  # log the rest of the serial buffer
                    try:  # log as a string if possible
                        logger.warning(
                            f"{self._conn.port}: {self._buffer.ser[:i_smp_start].decode()}"
                        )
                    except UnicodeDecodeError:  # log as bytes if not
                        logger.warning(f"{self._conn.port}: {self._buffer.ser[:i_smp_start].hex()}")

                self._buffer.smp = self._buffer.ser[i_smp_start:]
                self._buffer.ser.clear()
                self._buffer.state = SMPCANTransport._ReadBuffer.State.SMP
                i_smp_end = 0

                # don't await since the buffer may already contain the end delimiter

            elif self._buffer.state == SMPCANTransport._ReadBuffer.State.SMP:
                # read the entire OS buffer
                try:
                    self._buffer.smp.extend(msg.data)
                except StopIteration:
                    pass

                try:  # search the buffer for the index of the delimiter
                    i_smp_end = self._buffer.smp.index(END_DELIMITER, i_smp_end) + len(
                        END_DELIMITER
                    )
                except ValueError:  # delimiter not found yet, wait
                    continue

                # out is everything up to and including the delimiter
                out = self._buffer.smp[:i_smp_end]
                logger.debug(f"Received {len(out)} byte chunk")

                # there may be some leftover to save for the next read, but
                # it's not necessarily SMP data
                self._buffer.ser = self._buffer.smp[i_smp_end:]

                self._buffer.state = SMPCANTransport._ReadBuffer.State.SER

                return out

    async def send_and_receive(self, data: bytes) -> bytes:
        await self.send(data)
        return await self.receive()

    @property
    def mtu(self) -> int:
        return self._mtu

    @cached_property
    def max_unencoded_size(self) -> int:
        """The serial transport encodes each packet instead of sending SMP messages as raw bytes.

        The worst case size of an encoded SMP packet is:
        ```
        base64_cost(
            len(smp_message) + len(frame_length) + len(frame_crc16)
        ) + len(delimiter) + len(line_ending)
        ```
        This simplifies to:
        ```
        base64_cost(len(smp_message) + 4) + 3
        ```

        This property specifies the maximum size of an SMP message before it has been encoded for
        the serial transport.
        """

        packet_framing_size: Final = (
            _base64_cost(smppacket.FRAME_LENGTH_STRUCT.size + smppacket.CRC16_STRUCT.size)
            + smppacket.DELIMITER_SIZE
            + len(smppacket.CR)
        )

        return _base64_max(self.mtu) - packet_framing_size
