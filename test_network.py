"""Run from this directory: python3 -m unittest"""
import unittest
from unittest import mock

# First: points the config at the fixtures before any module reads it.
import fakes  # noqa: F401
import network

UP = """lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
utun0: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1380
\tinet6 fe80::1%utun0 prefixlen 64 scopeid 0x10
utun4: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1400
\tinet 10.20.30.40 --> 10.20.30.40 netmask 0xffffffff
"""


class VpnTunnelTest(unittest.TestCase):
    def test_only_a_utun_with_an_ipv4_address_is_a_tunnel(self):
        # When
        found = [network.vpn_tunnel(UP), network.vpn_tunnel(UP.rsplit("utun4", 1)[0])]
        # Then
        self.assertEqual([True, False], found)


class ChecksTest(unittest.TestCase):
    def test_dns_per_host_and_globalprotect_from_the_process_table(self):
        # Given
        table = {1: (0, "/sbin/launchd"), 2: (1, "/Applications/GlobalProtect.app/Contents/Resources/PanGPS")}
        # When
        with mock.patch.object(network.socket, "getaddrinfo", side_effect=OSError("nodename nor servname")), \
                mock.patch.object(network, "vpn_tunnel", return_value=False), \
                mock.patch.object(network.procs, "processes", return_value=table):
            found = network.checks()
        # Then
        self.assertEqual({"vpn_tunnel": False, "globalprotect_running": True},
                         {k: v for k, v in found.items() if k != "dns"})
        self.assertEqual({False}, set(found["dns"].values()))
        self.assertIn(network.gerrit.HOST, found["dns"])


if __name__ == "__main__":
    unittest.main()
