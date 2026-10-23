"""Sanitised macOS command samples; no host data or command execution."""

SYSTEM_OUTPUTS = {
    "interfaces": """lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
    options=1203<RXCSUM,TXCSUM,TXSTATUS,SW_TIMESTAMP>
    inet 127.0.0.1 netmask 0xff000000
    inet 127.0.2.2 netmask 0xff000000
    inet6 ::1 prefixlen 128
    nd6 options=201<PERFORMNUD,DAD>
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
    ether 02:00:00:00:00:01
    inet 192.0.2.10 netmask 0xffffff00 broadcast 192.0.2.255
    inet6 fe80::1%en0 prefixlen 64 secured scopeid 0xe
    inet6 2001:db8::10 prefixlen 64 autoconf temporary
    media: autoselect
    status: active
bridge0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
    Configuration:
        id 0:0:0:0:0:0 priority 0 hellotime 0 fwddelay 0
        maxage 0 holdcnt 0 proto stp maxaddr 100 timeout 1200
        root id 0:0:0:0:0:0 priority 0 ifcost 0 port 0
        ipfilter disabled flags 0x0
    member: en1 flags=3<LEARNING,DISCOVER>
            ifmaxaddr 0 port 10 priority 0 path cost 0
utun7: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1300
    inet 172.16.0.2 --> 172.16.0.2 netmask 0xffffffff
    inet6 fe80::2%utun7 prefixlen 64 scopeid 0x19
""",
    "routes": """Routing tables

Internet:
Destination        Gateway            Flags      Netif Expire
default            192.0.2.1           UGScg      en0
192.0.2/23         utun7               Uc         utun7
10.254/16          utun7               Uc         utun7
192.0.2.20         02:00:00:00:00:02   UHLWI      en0  1153
169.254            link#14             UCS        en0  !

Internet6:
Destination        Gateway             Flags      Netif Expire
default            fe80::1%en0         UGcg       en0
fe80::%utun7/64    link#25             UcI        utun7
2001:db8::10       link#14             UHL        lo0
""",
    "default_route": """   route to: default
destination: default
       mask: default
    gateway: 192.0.2.1
  interface: en0
      flags: <UP,GATEWAY,DONE,STATIC,PRCLONING,GLOBAL>
 recvpipe  sendpipe  ssthresh  rtt,msec    rttvar  hopcount      mtu     expire
       0         0         0         0         0         0      1500         0
""",
    "selected_route": """   route to: 10.254.0.1
destination: 10.254.0.0
       mask: 255.255.0.0
    gateway: 192.0.2.2
  interface: utun8
      flags: <UP,GATEWAY,DONE,STATIC>
 recvpipe sendpipe ssthresh rtt,msec rttvar hopcount mtu expire
        0        0        0        1      2        0 1300      0
""",
    "resolvers": """DNS configuration

resolver #1
  search domain[0] : example.test
  search domain[1] : internal.test
  nameserver[0] : 192.0.2.53
  nameserver[1] : 2001:db8::53
  flags    : Request A records, Request AAAA records
  reach    : 0x00030002 (Reachable,Local Address,Directly Reachable Address)

resolver #2
  domain   : local
  options  : mdns
  timeout  : 5
  flags    : Request A records, Request AAAA records
  reach    : 0x00000000 (Not Reachable)
  order    : 300000

DNS configuration (for scoped queries)

resolver #1
  nameserver[0] : fe80::53%en0
  if_index : 14 (en0)
  flags    : Scoped, Request A records, Request AAAA records
  reach    : 0x00000002 (Reachable)
""",
    "network_services": """An asterisk (*) denotes that a network service is disabled.
Thunderbolt Bridge
Wi-Fi
*Disabled Service
""",
    "service_dns": "192.0.2.53\n2001:db8::53\n",
    "proxies": """<dictionary> {
  HTTPEnable : 1
  HTTPPort : 8080
  HTTPProxy : proxy.example.test
  HTTPSEnable : 0
  ProxyAutoConfigEnable : 1
  ProxyAutoConfigURLString : https://user:secret@proxy.example.test/pac?token=secret
  ExceptionsList : <array> {
    0 : *.example.test
  }
}
""",
    "macos_build": "ProductName:\tmacOS\nProductVersion:\t15.7.5\nBuildVersion:\t24G624\n",
    "firewall": "Firewall is enabled. (State = 1)\n",
    "packet_filter": """Status: Enabled for 1 days 02:03:04           Debug: Urgent

State Table                          Total             Rate
  current entries                       12
  searches                            1000            1.5/s
Counters
  match                                 50            0.1/s
  bad-offset                             0            0.0/s
""",
    "helper_service": """system/dev.stelvio.tunnel = {
    type = LaunchDaemon
    state = running
    program = /Library/PrivilegedHelperTools/stelvio-traforo
    runs = 2
    pid = 42
    last exit code = (never exited)
    environment = {
        AWS_SECRET_ACCESS_KEY = secret
        state = secret
        pid = 999
    }
    arguments = {
        secret
    }
    sockets = {
        listener = {
            type = stream
        }
    }
}
""",
}

RESOLVER_FILE = """nameserver 192.0.2.53
nameserver 2001:db8::53
port 5353
domain example.test
search_order 100
timeout 2
"""
