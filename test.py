import asyncio
import logging
import pyvisonic
import time
from time import sleep

logging.basicConfig(level=logging.DEBUG)
log = logging.getLogger(__name__)


def add_visonic_device(visonic_devices):
    
    if visonic_devices == None:
        _LOGGER.warning("Visonic attempt to add device when sensor is undefined")
        return
    if type(visonic_devices) == defaultdict:
        _LOGGER.warning("Visonic got new sensors {0}".format( visonic_devices ))
    elif type(visonic_devices) == pyvisonic.SensorDevice:
        # This is an update of an existing device
        _LOGGER.warning("Visonic got a sensor update {0}".format( visonic_devices ))
        
    #elif type(visonic_devices) == visonicApi.SwitchDevice:   # doesnt exist yet
    
    else:
        _LOGGER.warning("Visonic attempt to add device with type {0}  device is {1}".format(type(visonic_devices), visonic_devices ))

pyvisonic.setConfig("OverrideCode", -1)
pyvisonic.setConfig("PluginDebug", True)
pyvisonic.setConfig("ForceStandard", False)

useTask = True

if useTask:
    pyvisonic.create_tcp_visonic_connection_task(
                        address='192.168.0.8',
                        port=20024)
    while True:
        log.debug("I'm Here")
        sleep(5.0)
else:
    testloop = asyncio.get_event_loop()

    #conn = pyvisonic.create_usb_visonic_connection(
    #    port='/dev/ttyUSB1',
    #    loop=testloop)

    conn = pyvisonic.create_tcp_visonic_connection(
                        address='192.168.0.8',
                        port=20024,
                        loop=testloop,
                        event_callback=add_visonic_device)
                        
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
