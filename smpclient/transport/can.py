"""A serial SMPTransport."""

import asyncio
import logging
import math
from enum import IntEnum, unique, Enum
from functools import cached_property
from typing import Final, List
import can
from serial import Serial
from smp import packet as smppacket
from typing_extensions import override
from smpclient.transport import SMPTransport, SMPTransportDisconnected

logger = logging.getLogger(__name__)
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

class SMPCANTransport(SMPTransport):

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
        max_smp_encoded_frame_size: int = 1024,
        line_length: int = 512,
        line_buffers: int = 2,
        device: CANDevice = CANDevice.PeakUSB,
        rx_id: int = 0xDEAD,
        tx_id: int = 0xBEEF,
        bitrate:int= 1000000
    ) -> None:
        """Initialize the serial transport.

        Parameters:
        - `max_smp_encoded_frame_size`: The maximum size of an encoded SMP
            frame.  The SMP server needs to have a buffer large enough to
            receive the encoded frame packets and to store the decoded frame.
        - `line_length`: The maximum SMP packet size.
        - `line_buffers`: The number of line buffers in the serial buffer.
        """
        if max_smp_encoded_frame_size < line_length * line_buffers:
            logger.error(
                f"{max_smp_encoded_frame_size=} is less than {line_length=} * {line_buffers=}!"
            )
        elif max_smp_encoded_frame_size != line_length * line_buffers:
            logger.warning(
                f"{max_smp_encoded_frame_size=} is not equal to {line_length=} * {line_buffers=}!"
            )
        self._max_smp_encoded_frame_size: Final = max_smp_encoded_frame_size
        self._line_length: Final = line_length
        self._line_buffers: Final = line_buffers
        self._device = device
        self._bus = None
        self._rx_id = rx_id
        self._tx_id = tx_id
        self._ext_tx = tx_id > STD_MAX_ID
        self._ext_rx = rx_id > STD_MAX_ID
        self._bitrate = bitrate
        self._msg_queue = asyncio.Queue()
        self._buffer = SMPCANTransport._ReadBuffer()
        self.loop = asyncio.new_event_loop()
        logger.debug(f"Initialized {self.__class__.__name__}")

    def _rx_cb(self, msg: can.Message) -> None:
        if msg.arbitration_id != self._rx_id:
            return
        result = self.loop.run_until_complete(self._msg_queue.put(msg))


    @override
    async def connect(self, address, timeout_s: float) -> None:
        logger.debug(f"Connecting to %s "%(self._device))
        if self._device == CANDevice.PeakUSB:
            if address is None:
                self._bus = can.interface.Bus(interface='pcan',
                                            bitrate=self._bitrate,
                                            channel="PCAN_USBBUS1")
            else:
                self._bus = can.interface.Bus(interface='pcan',
                                            bitrate=self._bitrate,
                                            device_id=address)         
        elif self._device == CANDevice.SeedStudio:
            self._bus = can.interface.Bus(bustype='seeedstudio',
                                         channel=address,
                                         baudrate=2000000,
                                         bitrate=self._bitrate,
                                         frame_type='STD',
                                         operation_mode='normal')
            # Seedstudio interface does not initialize 
            # correctly without a delay
            await asyncio.sleep(0.1)
        self._reader = can.AsyncBufferedReader()
        listeners: List[can.notifier.MessageRecipient] = [
            self._rx_cb,  # Callback function
            self._reader  # AsyncBufferedReader() listener
        ]
        self.notifier = can.Notifier(self._bus, listeners)
        logger.debug(f"Connected to %s "%(self._device))

    @override
    async def disconnect(self) -> None:
        logger.debug(f"Disconnecting from {self._device=}")
        if self._bus is not None:
            self._bus.shutdown()
        logger.debug(f"Disconnected from {self._device=}")

    @override
    async def send(self, data: bytes) -> None:
        if len(data) > self.max_unencoded_size:
            raise ValueError(
                f"Data size {len(data)} exceeds maximum unencoded size {self.max_unencoded_size}"
            )
        logger.debug(f"Sending {len(data)} bytes")
        try:
            for packet in smppacket.encode(data, line_length=self._line_length):
                can_packets = [packet[i:i + CAN_DL_SIZE] for i in range(0, len(packet), CAN_DL_SIZE)]
                logger.debug(f"Writing encoded packet of size {len(packet)}B; {self._line_length=}")
                logger.debug(f"Total CAN Msgs {len(can_packets)}")
                for can_p in can_packets:
                    msg = can.Message(arbitration_id=self._tx_id,
                                          data=can_p,
                                          is_extended_id=self._ext_tx)
                    self._bus.send(msg)
                    if self._device == CANDevice.SeedStudio:
                        await asyncio.sleep(0.0001)
        except Exception as e:
            if self._device == CANDevice.SeedStudio:
                device = self._bus.channel
            elif self._device == CANDevice.PeakUSB:
                try:
                    device = self._bus.device_id
                except:
                    device = self._bus.channel
            raise SMPTransportDisconnected(
                    f"{self.__class__.__name__} disconnected from {device}"
                )
        logger.debug(f"Sent {len(data)} bytes")

    @override
    async def receive(self) -> bytes:
        decoder = smppacket.decode()
        next(decoder)

        logger.debug("Waiting for response")
        while True:
            try:
                b = await self._readuntil()
                decoder.send(b)
            except StopIteration as e:
                logger.debug(f"Finished receiving {len(e.value)} byte response")
                return e.value

    @override
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

    @override
    async def send_and_receive(self, data: bytes) -> bytes:
        await self.send(data)
        return await self.receive()

    @override
    @property
    def mtu(self) -> int:
        return self._max_smp_encoded_frame_size

    @cached_property
    def max_unencoded_size(self) -> int:
        """The serial transport encodes each packet instead of sending SMP messages as raw bytes."""

        # For each packet, AKA line_buffer, include the cost of the base64
        # encoded frame_length and CRC16 and the start/continue delimiter.
        # Add to that the cost of the stop delimiter.
        packet_framing_size: Final = (
            _base64_cost(smppacket.FRAME_LENGTH_STRUCT.size + smppacket.CRC16_STRUCT.size)
            + smppacket.DELIMITER_SIZE
        ) * self._line_buffers + len(smppacket.END_DELIMITER)

        # Get the number of unencoded bytes that can fit in self.mtu and
        # subtract the cost of framing the separate packets.
        # This is the maximum number of unencoded bytes that can be received by
        # the SMP server with this transport configuration.
        return _base64_max(self.mtu) - packet_framing_size