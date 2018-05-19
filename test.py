import asyncio
import logging

import pyvisonic

logging.basicConfig(level=logging.DEBUG)

testloop = asyncio.get_event_loop()

#conn = pyvisonic.create_visonic_connection(
#    protocol=pyvisonic.VisonicProtocol,
#    port='/dev/ttyUSB1',
#    loop=testloop)
conn = pyvisonic.create_tcpvisonic_connection(
    protocol=pyvisonic.VisonicProtocol,
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