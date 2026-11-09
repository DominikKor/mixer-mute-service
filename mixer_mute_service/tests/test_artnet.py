"""Art-Net sender of the stage light (no device is contacted: the socket is
faked). Checks the ArtDmx packet byte by byte, that one socket with one
source port is used for every frame, the sequence numbers, and the ArtPoll
reply that tells whether the node merges a second controller."""

import struct
import unittest
from unittest.mock import patch

from app import native_controls as nc


class FakeSocket:
    def __init__(self, port=nc.ARTNET_PORT, replies=()):
        self.sent = []
        self.port = port
        self.replies = list(replies)
        self.closed = False

    def bind(self, address):
        if address[1] == nc.ARTNET_PORT and self.port != nc.ARTNET_PORT:
            raise OSError("address in use")

    def getsockname(self):
        return ("0.0.0.0", self.port)

    def sendto(self, data, address):
        self.sent.append((bytes(data), address))

    def recvfrom(self, size):
        return self.replies.pop(0)

    def close(self):
        self.closed = True


def fake_select(readers, _w, _x, _timeout):
    return (readers if readers[0].replies else [], [], [])


def poll_reply(host, short=b"Stage Node", merging=False, ltp=False, ports=1):
    data = bytearray(239)
    data[0:8] = b"Art-Net\x00"
    data[8:10] = struct.pack("<H", 0x2100)
    data[26:26 + len(short)] = short
    data[44:44 + 10] = b"Long name!"
    data[173] = ports
    data[182] = 0x80 | (0x08 if merging else 0) | (0x02 if ltp else 0)
    return bytes(data), (host, nc.ARTNET_PORT)


class ArtDmxTests(unittest.TestCase):
    def setUp(self):
        self.sockets = []

        def make(*_args):
            sock = FakeSocket()
            self.sockets.append(sock)
            return sock

        patcher = patch.object(nc.socket, "socket", side_effect=make)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sender = nc._ArtNetSender()

    def test_packet_layout(self):
        frame = [0] * 512
        frame[0], frame[1], frame[2] = 200, 255, 50
        self.sender.send_dmx("192.168.188.48", 0, frame)
        packet, address = self.sockets[0].sent[0]
        self.assertEqual(address, ("192.168.188.48", 6454))
        self.assertEqual(packet[:8], b"Art-Net\x00")
        self.assertEqual(packet[8:10], b"\x00\x50")          # OpDmx 0x5000, low byte first
        self.assertEqual(packet[10:12], b"\x00\x0e")         # protocol 14, high byte first
        self.assertEqual(packet[12], 1)                      # sequence (0 would mean "none")
        self.assertEqual(packet[13], 0)                      # physical
        self.assertEqual(packet[14:16], b"\x00\x00")         # SubUni, Net: universe 0
        self.assertEqual(packet[16:18], b"\x02\x00")         # length 512, high byte first
        self.assertEqual(list(packet[18:21]), [200, 255, 50])
        self.assertEqual(len(packet), 18 + 512)

    def test_one_socket_and_sequence_wraps_without_zero(self):
        for _ in range(257):
            self.sender.send_dmx("192.168.188.48", 0, [0] * 512)
        self.assertEqual(len(self.sockets), 1)               # same source port for every frame
        sequences = [packet[12] for packet, _ in self.sockets[0].sent]
        self.assertEqual(sequences[:3], [1, 2, 3])
        self.assertEqual(sequences[254:257], [255, 1, 2])
        self.assertNotIn(0, sequences)

    def test_invalid_frame_is_refused(self):
        with self.assertRaises(nc.NativeControlError):
            self.sender.send_dmx("192.168.188.48", 0, [0] * 511)
        with self.assertRaises(nc.NativeControlError):
            self.sender.send_dmx("192.168.188.48", 0, [256] + [0] * 511)
        self.assertEqual(self.sockets, [])

    def test_failed_send_opens_a_new_socket(self):
        self.sender.send_dmx("192.168.188.48", 0, [0] * 512)
        self.sockets[0].sendto = lambda *_: (_ for _ in ()).throw(OSError("network down"))
        with self.assertRaises(OSError):
            self.sender.send_dmx("192.168.188.48", 0, [0] * 512)
        self.assertTrue(self.sockets[0].closed)
        self.sender.send_dmx("192.168.188.48", 0, [0] * 512)
        self.assertEqual(len(self.sockets), 2)

    def test_module_function_uses_the_shared_sender(self):
        with patch.object(nc._ARTNET, "send_dmx") as send:
            nc._send_artnet_frame("192.168.188.48", 0, [0] * 512)
        send.assert_called_once_with("192.168.188.48", 0, [0] * 512)


class ArtPollTests(unittest.TestCase):
    def poll(self, sock, host="192.168.188.48"):
        sender = nc._ArtNetSender()
        with patch.object(nc.socket, "socket", return_value=sock), \
                patch.object(nc.select, "select", side_effect=fake_select):
            return sender.poll(host, timeout=0.2)

    def test_reply_without_merge(self):
        sock = FakeSocket()
        status = self.poll(sock)
        # Nothing old queued: the poll goes out, then the (empty) wait.
        self.assertEqual(sock.sent[0][0], b"Art-Net\x00" + b"\x00\x20" + b"\x00\x0e" + b"\x00\x00")
        self.assertIsNone(status)
        sock = FakeSocket()
        original = sock.sendto

        def answer(data, address):
            original(data, address)
            if data[8:10] == b"\x00\x20":
                sock.replies.append(poll_reply("10.0.0.9", short=b"Other"))     # another node: ignored
                sock.replies.append(poll_reply("192.168.188.48", ltp=True))
        sock.sendto = answer
        status = self.poll(sock)
        self.assertEqual(status["short_name"], "Stage Node")
        self.assertEqual(status["long_name"], "Long name!")
        self.assertEqual(status["outputs"], [{"sending_dmx": True, "merging": False, "merge_mode": "LTP"}])

    def test_reply_shows_merging(self):
        sock = FakeSocket()
        original = sock.sendto
        sock.sendto = lambda data, address: (original(data, address),
                                             sock.replies.append(poll_reply("192.168.188.48", merging=True)))
        status = self.poll(sock)
        self.assertTrue(status["outputs"][0]["merging"])
        self.assertEqual(status["outputs"][0]["merge_mode"], "HTP")

    def test_port_taken_means_no_reply_can_come(self):
        sock = FakeSocket(port=50000)
        with self.assertRaises(nc.NativeControlError) as caught:
            self.poll(sock)
        self.assertIn("6454", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
