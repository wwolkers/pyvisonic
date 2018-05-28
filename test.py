import asyncio
import logging

import pyvisonic

logging.basicConfig(level=logging.DEBUG)

testloop = asyncio.get_event_loop()

#conn = pyvisonic.create_usb_visonic_connection(
#    port='/dev/ttyUSB1',
#    loop=testloop)

pyvisonic.setConfig("OverrideCode", -1)
pyvisonic.setConfig("PluginDebug", True)
pyvisonic.setConfig("ForceStandard", False)

conn = pyvisonic.create_tcp_visonic_connection(
                    address='192.168.0.8',
                    port=20024,
                    loop=testloop)
testloop.create_task(conn)
#protocol = testloop.run_until_complete(conn)
#protocol.send_command_ack(packet = VMSG_ENROLL)
# loop.run_until_complete(m.transport)
try:
    testloop.run_forever()
except KeyboardInterrupt:
    # cleanup connection
    conn.close()
    testloop.run_forever()
    testloop.close()
finally:
    testloop.close()