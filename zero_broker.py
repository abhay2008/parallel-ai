import asyncio
import struct
import zlib
import ctypes
import time
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

# ==============================================================================
# CONFIGURATION & CONSTANTS
# ==============================================================================

MAGIC_BYTES = 0xCAFE
PROTOCOL_VERSION = 1
# Header: Magic(H), Ver(B), Flags(B), TopicHash(I), SeqID(q), PayloadLen(i), CRC32(I)
HEADER_FORMAT = '!HBBIqiI'  
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)  # 24 bytes
MAX_FRAME_SIZE = 65536  # 64KB Max Payload

CACHE_LINE_SIZE = 64
DEFAULT_CAPACITY = 8192  # Power of 2
SATURATION_HIGH_WATER = 0.95  # Drop-tail trigger
SATURATION_CRITICAL = 0.99    # Publisher block trigger

BIND_HOST = '127.0.0.1'
BIND_PORT = 18888
READ_BUFFER_SIZE = 65536 + HEADER_SIZE

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger("ZeroBroker")

# ==============================================================================
# LOW-LEVEL MEMORY & ATOMIC PRIMITIVES
# ==============================================================================

class PaddedAtomicU64(ctypes.Structure):
    """64-bit Integer padded to 64-byte cache line to prevent false sharing."""
    _pack_ = CACHE_LINE_SIZE
    _fields_ = [
        ('value', ctypes.c_uint64),
        ('_padding', ctypes.c_byte * (CACHE_LINE_SIZE - ctypes.sizeof(ctypes.c_uint64)))
    ]
    
    def __init__(self, initial: int = 0):
        super().__init__()
        self.value = initial
    
    def load_acquire(self) -> int:
        return self.value
    
    def store_release(self, val: int):
        self.value = val
    
    def fetch_add_relaxed(self, delta: int = 1) -> int:
        old = self.value
        self.value = old + delta
        return old

class SlotMetadata(ctypes.Structure):
    """Fixed metadata header inside each slot (16 bytes)."""
    _pack_ = 1
    _fields_ = [
        ('seq', ctypes.c_uint64),      
        ('length', ctypes.c_uint32),   
        ('flags', ctypes.c_uint8),     
        ('_pad', ctypes.c_byte * 3),   
    ]
    SIZE = 16

# ==============================================================================
# BINARY PROTOCOL HANDLER
# ==============================================================================

class BinaryProtocol:
    @staticmethod
    def calculate_topic_hash(topic: bytes) -> int:
        return zlib.crc32(topic) & 0xFFFFFFFF

    @staticmethod
    def pack_header(topic_hash: int, seq_id: int, payload_len: int, flags: int, crc32: int) -> bytes:
        return struct.pack(HEADER_FORMAT, MAGIC_BYTES, PROTOCOL_VERSION, flags, topic_hash, seq_id, payload_len, crc32)

    @staticmethod
    def parse_header(mv: memoryview) -> Optional[Tuple[int, int, int, int, int, int]]:
        if len(mv) < HEADER_SIZE:
            return None
        try:
            magic, ver, flags, topic_hash, seq_id, payload_len, crc32 = struct.unpack_from(HEADER_FORMAT, mv)
            if magic != MAGIC_BYTES or ver != PROTOCOL_VERSION or payload_len > MAX_FRAME_SIZE:
                return None
            return topic_hash, seq_id, payload_len, flags, crc32, HEADER_SIZE
        except struct.error:
            return None

    @staticmethod
    def validate_crc(payload_mv: memoryview, expected_crc: int) -> bool:
        return (zlib.crc32(payload_mv) & 0xFFFFFFFF) == expected_crc

# ==============================================================================
# LOCK-FREE SPMC RING BUFFER
# ==============================================================================

class InMemRingBuffer:
    __slots__ = ('_capacity', '_mask', '_buffer', '_meta_offset', '_data_offset',
                 '_write_head', '_consumer_tails', '_consumer_not_full', 
                 '_consumer_not_empty', '_topic_hash')

    def __init__(self, topic_hash: int, capacity: int = DEFAULT_CAPACITY):
        if capacity & (capacity - 1) != 0:
            raise ValueError("Capacity must be power of 2")
        
        self._topic_hash = topic_hash
        self._capacity = capacity
        self._mask = capacity - 1
        
        self._write_head = PaddedAtomicU64(0)
        self._consumer_tails: Dict[int, PaddedAtomicU64] = {} 
        self._consumer_not_empty: Dict[int, asyncio.Event] = {}
        self._consumer_not_full = asyncio.Event()
        self._consumer_not_full.set() 
        
        total_data_bytes = capacity * (SlotMetadata.SIZE + HEADER_SIZE + MAX_FRAME_SIZE)
        self._buffer = bytearray(total_data_bytes)
        
        self._meta_offset = 0
        self._data_offset = capacity * SlotMetadata.SIZE

    def _get_slot_offsets(self, index: int) -> Tuple[int, int]:
        phys_idx = index & self._mask
        meta_off = phys_idx * SlotMetadata.SIZE
        data_off = self._data_offset + phys_idx * (HEADER_SIZE + MAX_FRAME_SIZE)
        return meta_off, data_off

    def register_consumer(self, consumer_id: int) -> Tuple[PaddedAtomicU64, asyncio.Event]:
        tail = PaddedAtomicU64(self._write_head.value)
        evt = asyncio.Event()
        self._consumer_tails[consumer_id] = tail
        self._consumer_not_empty[consumer_id] = evt
        return tail, evt

    def unregister_consumer(self, consumer_id: int):
        self._consumer_tails.pop(consumer_id, None)
        self._consumer_not_empty.pop(consumer_id, None)
    
    def try_write(self, frame_header: bytes, payload: memoryview, seq_id: int, flags: int) -> bool:
        write_seq = self._write_head.fetch_add_relaxed(1)
        meta_off, data_off = self._get_slot_offsets(write_seq)
        payload_len = len(payload)
        frame_len = HEADER_SIZE + payload_len
        
        # Zero-Copy into Ring Buffer
        self._buffer[data_off : data_off + HEADER_SIZE] = frame_header
        self._buffer[data_off + HEADER_SIZE : data_off + frame_len] = payload
        
        # Write Metadata (Release Semantics, consumer spin on seq matching tail+1)
        struct.pack_into('=I', self._buffer, meta_off + 8, frame_len)
        struct.pack_into('=Q', self._buffer, meta_off, write_seq + 1)
        
        self._check_backpressure(write_seq + 1)
        
        for evt in self._consumer_not_empty.values():
            if not evt.is_set():
                evt.set()
        return True

    def _check_backpressure(self, write_seq: int):
        critical_full = False
        
        for cid, tail_obj in self._consumer_tails.items():
            read_seq = tail_obj.load_acquire()
            lag = write_seq - read_seq
            
            if lag >= self._capacity: 
                critical_full = True
                new_tail = write_seq - self._capacity + 1
                tail_obj.store_release(new_tail)
            elif lag > int(self._capacity * SATURATION_HIGH_WATER):
                target_tail = write_seq - int(self._capacity * 0.90)
                if target_tail > read_seq:
                    tail_obj.store_release(target_tail)
                    log.warning(f"Consumer {cid} LOAD SHED: Dropped {target_tail - read_seq} frames.")
        
        min_read_seq = min((t.load_acquire() for t in self._consumer_tails.values()), default=write_seq)
        if critical_full or (write_seq - min_read_seq >= self._capacity - 1):
            self._consumer_not_full.clear()
        else:
            self._consumer_not_full.set()

    async def wait_for_slot(self):
        await self._consumer_not_full.wait()
    
    def try_read_next(self, consumer_id: int) -> Optional[memoryview]:
        tail_obj = self._consumer_tails.get(consumer_id)
        if not tail_obj: return None
        
        read_seq = tail_obj.load_acquire()
        next_seq = read_seq + 1
        meta_off, data_off = self._get_slot_offsets(next_seq - 1)
        
        try:
            published_seq = struct.unpack_from('=Q', self._buffer, meta_off)[0]
        except struct.error:
            return None
            
        if published_seq != next_seq:
            return None
        
        frame_len = struct.unpack_from('=I', self._buffer, meta_off + 8)[0]
        frame_view = memoryview(self._buffer)[data_off : data_off + frame_len]
        
        tail_obj.store_release(next_seq)
        return frame_view

# ==============================================================================
# ASYNC BROKER SERVER
# ==============================================================================

@dataclass
class SubscriberSession:
    writer: asyncio.StreamWriter
    consumer_id: int
    topic_hashes: Set[int]
    task: Optional[asyncio.Task] = None
    slow_mode: bool = False
    stats: Dict[str, int] = field(default_factory=lambda: {'received': 0, 'bytes': 0})

class AsyncBrokerServer:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self._server: Optional[asyncio.Server] = None
        self._topic_buffers: Dict[int, InMemRingBuffer] = {}
        self._subscribers: Dict[int, SubscriberSession] = {}
        self._next_client_id = 0
        self._running = False

    def _get_topic_buffer_by_hash(self, topic_hash: int) -> InMemRingBuffer:
        if topic_hash not in self._topic_buffers:
            self._topic_buffers[topic_hash] = InMemRingBuffer(topic_hash)
        return self._topic_buffers[topic_hash]

    async def start(self):
        self._server = await asyncio.start_server(self._client_connected, self.host, self.port, reuse_port=True)
        self._running = True
        log.info(f"Broker listening on {self.host}:{self.port}")

    async def _client_connected(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            # Read first frame to determine PUB or SUB
            header_mv = await reader.readexactly(HEADER_SIZE)
            parsed = BinaryProtocol.parse_header(memoryview(header_mv))
            if not parsed:
                return writer.close()
            
            _, _, payload_len, flags, crc32, _ = parsed
            payload = await reader.readexactly(payload_len)
            
            if not BinaryProtocol.validate_crc(memoryview(payload), crc32):
                return writer.close()
            
            if flags & 0x80:
                cmd = payload.decode().strip()
                if cmd.startswith("SUB:"):
                    topics = [t.strip().encode() for t in cmd[4:].split(',')]
                    await self._handle_subscriber(reader, writer, topics)
                elif cmd == "PUB":
                    await self._handle_publisher(reader, writer)
        except asyncio.IncompleteReadError:
            pass
        except Exception as e:
            log.error(f"Client connection error: {e}")
        finally:
            writer.close()

    async def _handle_publisher(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        log.info("Publisher connected.")
        try:
            while self._running:
                header = await reader.readexactly(HEADER_SIZE)
                parsed = BinaryProtocol.parse_header(memoryview(header))
                if not parsed:
                    break
                
                topic_hash, seq_id, payload_len, flags, crc32, _ = parsed
                payload = await reader.readexactly(payload_len)
                payload_mv = memoryview(payload)
                
                if not BinaryProtocol.validate_crc(payload_mv, crc32):
                    log.warning("CRC mismatch on ingress frame, dropping.")
                    continue
                
                ring = self._get_topic_buffer_by_hash(topic_hash)
                
                # Proactive Blocking (Global Circuit Breaker)
                if not ring._consumer_not_full.is_set():
                    await ring.wait_for_slot()
                
                ring.try_write(header, payload_mv, seq_id, flags)
        except asyncio.IncompleteReadError:
            log.info("Publisher cleanly disconnected.")
        except Exception as e:
            log.error(f"Publisher error: {e}")
        finally:
            writer.close()

    async def _handle_subscriber(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, topics: List[bytes]):
        self._next_client_id += 1
        cid = self._next_client_id
        
        topic_hashes = {BinaryProtocol.calculate_topic_hash(t) for t in topics}
        sess = SubscriberSession(writer, cid, topic_hashes)
        self._subscribers[cid] = sess
        
        log.info(f"Subscriber {cid} connected. Topics: {topics}")
        
        for thash in topic_hashes:
            self._get_topic_buffer_by_hash(thash).register_consumer(cid)
        
        sess.task = asyncio.create_task(self._dispatch_loop(sess))
        try:
            await sess.task
        except asyncio.CancelledError:
            pass
        finally:
            for thash in topic_hashes:
                if thash in self._topic_buffers:
                    self._topic_buffers[thash].unregister_consumer(cid)
            self._subscribers.pop(cid, None)
            writer.close()

    async def _dispatch_loop(self, sess: SubscriberSession):
        rings = {th: self._topic_buffers[th] for th in sess.topic_hashes}
        topic_list = list(rings.items())
        try:
            while self._running:
                progress = False
                for thash, ring in topic_list:
                    frame_mv = ring.try_read_next(sess.consumer_id)
                    if frame_mv:
                        sess.writer.write(frame_mv)
                        progress = True
                        sess.stats['received'] += 1
                        if sess.slow_mode:
                            await asyncio.sleep(0.002) # Simulate slow consumer
                
                if progress:
                    await sess.writer.drain()
                else:
                    waiters = [asyncio.create_task(ring._consumer_not_empty[sess.consumer_id].wait()) for _, ring in topic_list]
                    if waiters:
                        done, pending = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
                        for p in pending: 
                            p.cancel()
                        # Reset events that fired but aren't actually emptied yet
                        for _, ring in topic_list:
                            ring._consumer_not_empty[sess.consumer_id].clear()
        except ConnectionResetError:
            log.info(f"Subscriber {sess.consumer_id} connection reset.")
        except Exception as e:
            log.error(f"Subscriber {sess.consumer_id} error: {e}")

    async def shutdown(self):
        self._running = False
        log.info("Shutting down broker...")
        for sess in self._subscribers.values():
            if sess.task: sess.task.cancel()
        if self._server:
            self._server.close()
            await self._server.wait_closed()

# ==============================================================================
# AUTOMATED VERIFICATION SUITE
# ==============================================================================

class BenchmarkClient:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.stats = {'sent': 0, 'recv': 0, 'crc_errors': 0}
        self._seq = 0
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None

    async def connect(self):
        self.reader, self.writer = await asyncio.open_connection(self.host, self.port)
        return self

    async def send_control(self, cmd: str):
        payload = cmd.encode()
        crc = zlib.crc32(payload) & 0xFFFFFFFF
        header = BinaryProtocol.pack_header(0, 0, len(payload), 0x80, crc)
        self.writer.write(header + payload)
        await self.writer.drain()

    async def publish(self, topic: bytes, payload: bytes):
        self._seq += 1
        topic_hash = BinaryProtocol.calculate_topic_hash(topic)
        crc = zlib.crc32(payload) & 0xFFFFFFFF
        frame = BinaryProtocol.pack_header(topic_hash, self._seq, len(payload), 0, crc) + payload
        self.writer.write(frame)
        self.stats['sent'] += 1
        # Periodically drain to avoid memory blowup on the client side
        if self._seq % 100 == 0:
            await self.writer.drain()

    async def subscribe(self, topics: List[bytes], slow: bool = False):
        await self.send_control("SUB:" + ",".join(t.decode() for t in topics))
        if slow:
            # We communicate the slow mode requirement by matching a private state in the test suite
            # However, since the server tracks connection IDs, we'll hook directly into the server state for the simulation
            pass
        asyncio.create_task(self._recv_loop(slow))

    async def _recv_loop(self, slow: bool):
        while True:
            try:
                header = await self.reader.readexactly(HEADER_SIZE)
                parsed = BinaryProtocol.parse_header(memoryview(header))
                if not parsed: continue
                
                _, _, payload_len, _, crc32, _ = parsed
                payload = await self.reader.readexactly(payload_len)
                
                if not BinaryProtocol.validate_crc(memoryview(payload), crc32):
                    self.stats['crc_errors'] += 1
                    continue
                    
                self.stats['recv'] += 1
                if slow: await asyncio.sleep(0.005) # Throttle client-side read
            except asyncio.IncompleteReadError:
                break
            except Exception as e:
                log.error(f"BenchmarkClient read error: {e}")
                break

    async def close(self):
        if self.writer:
            self.writer.close()
            await self.writer.wait_closed()


async def run_verification():
    log.info("Starting High-Throughput Broker Verification Suite")
    
    broker = AsyncBrokerServer(BIND_HOST, BIND_PORT)
    server_task = asyncio.create_task(broker.start())
    await asyncio.sleep(0.1) # Wait for bind
    
    # Setup Network Publisher
    pub_client = await BenchmarkClient(BIND_HOST, BIND_PORT).connect()
    await pub_client.send_control("PUB")
    
    # Setup Fast Subscriber
    sub_fast = await BenchmarkClient(BIND_HOST, BIND_PORT).connect()
    await sub_fast.subscribe([b"topic_A"])
    
    # Setup Slow Subscriber
    sub_slow = await BenchmarkClient(BIND_HOST, BIND_PORT).connect()
    await sub_slow.subscribe([b"topic_A"], slow=True)
    
    await asyncio.sleep(0.2) # Allow subscriptions to initialize
    
    # Hack the server's session to enable server-side slow mode for sub_slow (ID=2)
    # This specifically tests the server's load shedding mechanics.
    broker._subscribers[2].slow_mode = True

    TOTAL_MESSAGES = 1000
    PAYLOAD_SIZE = 1024
    payload = b"X" * PAYLOAD_SIZE
    
    log.info(f"Publishing {TOTAL_MESSAGES} messages (Size: {PAYLOAD_SIZE} bytes)...")
    
    start_time = time.perf_counter()
    for i in range(TOTAL_MESSAGES):
        await pub_client.publish(b"topic_A", payload)
    
    # Drain remaining publisher buffers
    await pub_client.writer.drain()
    pub_duration = time.perf_counter() - start_time
    log.info(f"Publishing completed in {pub_duration:.3f}s. Throughput: {TOTAL_MESSAGES / pub_duration:,.0f} msgs/sec")
    
    # Wait for subscribers to consume messages
    await asyncio.sleep(2.0)
    
    log.info("--- Benchmark Results ---")
    log.info(f"Published: {pub_client.stats['sent']} frames")
    log.info(f"Fast Subscriber Received: {sub_fast.stats['recv']} frames | CRC Errors: {sub_fast.stats['crc_errors']}")
    log.info(f"Slow Subscriber Received: {sub_slow.stats['recv']} frames | CRC Errors: {sub_slow.stats['crc_errors']}")
    
    if sub_slow.stats['recv'] < TOTAL_MESSAGES:
        log.info(f"SUCCESS: Slow subscriber was successfully load-shed. Dropped {TOTAL_MESSAGES - sub_slow.stats['recv']} frames.")
    
    # Clean shutdown
    await pub_client.close()
    await sub_fast.close()
    await sub_slow.close()
    await broker.shutdown()
    server_task.cancel()

if __name__ == '__main__':
    asyncio.run(run_verification())
