"""Developer launcher: demo mode plus a switch to simulate "laptop out of range".

    LB_FLAGDIR=/some/dir python tools/run_dev.py --demo --port 8099 --data-dir /some/dir

Create the file <LB_FLAGDIR>/away-<ADDRESS> (e.g. away-SIM-DYU700-2) to make that simulated
light unreachable; delete it to bring the light back."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from light_bridge import chihiros, __main__ as m
FLAG = os.environ.get('LB_FLAGDIR', '/tmp')
orig_connect = chihiros.SimTransport.connect
def away(self):
    return os.path.exists(os.path.join(FLAG, f'away-{self.address}'))
async def connect(self, on_disconnect, allow_search=True):
    if away(self):
        raise ConnectionError('out of range (simulated)')
    self._odc = on_disconnect
    return await orig_connect(self, on_disconnect, allow_search)
def is_connected(self):
    if self.connected and away(self):
        self.connected = False
        cb = getattr(self, '_odc', None)
        if cb: cb()
    return self.connected
chihiros.SimTransport.connect = connect
chihiros.SimTransport.is_connected = property(is_connected)
sys.exit(m.main(sys.argv[1:]))
