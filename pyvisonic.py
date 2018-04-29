"""Asyncio protocol implementation of Visonic PowerMaster/PowerMax.
  Based on the DomotiGa implementation:

  Credits:
    Initial setup by Wouter Wolkers and Alexander Kuiper.
    Thanks to everyone who helped decode the data.

  Converted to Python module by Wouter Wolkers
"""

import struct
import re
#from collections import defaultdict
#from enum import Enum
#from typing import Any, Callable, Dict, Generator, cast
from datetime import datetime
from time import sleep
from datetime import timedelta
from dateutil.relativedelta import *
import asyncio
import concurrent
import logging
from functools import partial
from typing import Callable, List
from serial_asyncio import create_serial_connection
import sys
import pkg_resources
import threading
import queue

########################################################
# PowerMax/Master send messages
########################################################

### ACK messages, depending on the type, we need to ACK differently ###
VMSG_ACK1 = bytearray.fromhex('02') # NONE
VMSG_ACK2 = bytearray.fromhex('02 43') # NONE

### Init, restore and enroll messages ###
VMSG_INIT    = bytearray.fromhex('AB 0A 00 01 00 00 00 00 00 00 00 43') # NONE
VMSG_RESTORE = bytearray.fromhex('AB 06 00 00 00 00 00 00 00 00 00 43') # NONE
#VMSG_RESTORE_PIN = bytearray.fromhex('AB 06 00 00 18 92 00 00 00 00 00 43') # NONE
VMSG_ENROLL  = bytearray.fromhex('AB 0A 00 00 18 92 00 00 00 00 00 43') # NONE - DownloadCode: 4 & 5

VMSG_EVENTLOG  = bytearray.fromhex('A0 00 00 00 00 00 00 00 00 00 00 43') # A0 - MasterCode: 4 & 5
VMSG_ARMDISARM = bytearray.fromhex('A1 00 00 00 00 00 00 00 00 00 00 43') # NONE - MasterCode: 4 & 5
VMSG_STATUS    = bytearray.fromhex('A2 00 00 00 00 00 00 00 00 00 00 43') # A5

#### PowerMaster message ###
VMSG_PMASTER_STAT1 = bytearray.fromhex('B0 01 04 06 02 FF 08 03 00 00 43') # B0 STAT1
#Private VMSG_PMASTER_STAT2 As Byte[] = [&HB0, &H01, &H05, &H06, &H02, &HFF, &H08, &H03, &H00, &H00, &H43] 'B0 STAT2
#Private VMSG_PMASTER_STAT3 As Byte[] = [&HB0, &H01, &H07, &H06, &H02, &HFF, &H08, &H03, &H00, &H00, &H43] 'B0 STAT3

### Start/stop download information ###
VMSG_DL_START = bytearray.fromhex('24 00 00 01 00 00 00 00 00 00 00') # 3C - DownloadCode: 3 & 4
VMSG_DL_GET   = bytearray.fromhex('0A') # 33
VMSG_DL_EXIT  = bytearray.fromhex('0F') # NONE

log = logging.getLogger(__name__)

TIMEOUT = timedelta(seconds=5)
PowerLinkTimeout = 60
ForceStandardMode = False
MasterCode = 0x5650
DisarmArmCode = 0x5650
DownloadCode = 0x5650

def testBit(int_type, offset):
    mask = 1 << offset
    return(int_type & mask)

class ProtocolBase(asyncio.Protocol):
    """Manage low level Visonic protocol."""

    transport = None  # type: asyncio.Transport
    receiveheaderfound = False
    ReceiveLastPacket = datetime.now() # When last ReceiveData packet is received
    SendDelay = 500 # 500 msec delay between last received message and the new message
    SendQueue = queue.Queue()
    tQueueDelay = object
    bQueueDelay = False
    tQueueTimeout = object
    DownloadMode = False
    
    def __init__(self, loop=None, disconnect_callback=None) -> None:
        """Initialize class."""
        if loop:
            self.loop = loop
        else:
            self.loop = asyncio.get_event_loop()
        self.packet = bytearray()
        self.ReceiveData = bytearray()
        self.disconnect_callback = disconnect_callback
 
    def PowerLinkTimeoutExpired(self):
        """We timed out, try to restore the connection."""
        
        log.debug("[PowerLinkTimeoutExpired] Powerlink Timer Expired")
        
        # restart the timer
        try:
            tPowerLinkKeepAlive.cancel()
        except:
            pass
        tPowerLinkKeepAlive = threading.Timer(PowerLinkTimeout, self.PowerLinkTimeoutExpired)
        tPowerLinkKeepAlive.start()

        self.QueueCommand(VMSG_RESTORE, "Restore PowerMax/Master Connection", 0x00)

        
    def connection_made(self, transport):
        """Just logging for now."""
        self.transport = transport
        log.debug('[connection_made] connected')
        
        # Define 60 seconds timer and start it for PowerLink communication
        try:
            tPowerLinkKeepAlive.cancel()
        except:
            # guess there was no timer running. no problem
            pass
        tPowerLinkKeepAlive = threading.Timer(PowerLinkTimeout, self.PowerLinkTimeoutExpired)
        tPowerLinkKeepAlive.start()

        self.ReceiveLastPacket = datetime.now()
        self.QueueCommand(VMSG_INIT, "Initializing PowerMax/Master PowerLink Connection", 0x00)
        sleep(0.5)
        # Send the download command, this should initiate the communication
        # Only skip it, if we force standard mode
        if not ForceStandardMode:
              self.SendMsg_DL_START()

    def data_received(self, data): 
       """Add incoming data to ReceiveData."""
       log.debug('[data_received] received data: %s', repr(data))
       for databyte in data:
           self.handle_received_byte(databyte)

    def handle_received_byte(self, data):
        """Assemble incoming data per byte."""
        
        if self.receiveheaderfound:
            # Check for timeout
            dt = datetime.now() - self.lastpreambletime
            if ((dt.total_seconds * 1000 + dt.microseconds / 1000.0) > 3000):
                log.debug("[handle_received_byte] TIMEOUT ERROR: ", dt.total_seconds * 1000 + dt.microseconds / 1000.0)
                self.ReceiveData = bytearray()
                self.receiveheaderfound = False
        
            if len(self.ReceiveData) >= 192:
                # packets cannot be longer than \xC0 / 192 bytes
                log.debug("[handle_received_byte] Packet too long!")
                self.ReceiveData = bytearray()
                self.receiveheaderfound = False
        else:
            self.lastpreambletime = datetime.now()
        
        # We detected a preamble, set the HeaderFound to True. It doesn't matter if 
        # we find more 0x0D in the data, because it Is already set To True anyway
        if (data == b'\x0D'):
            receiveheaderfound = True

        self.ReceiveData += data.to_bytes(1, byteorder='big')
        
        if self.valid_packet():
            self.handle_packet(self.ReceiveData)

    def send_ack(self):
        """ Send ACK if packet is valid
            Needs to be fixed to send Type1/Type2 ACK when needed, now just sending ACK1 for simplicity
        """
        self.send_packet(VMSG_ACK1)
        
    
    def valid_packet(self) -> bool:
        """Verify if packet is valid.
            >>> Packets start with a preamble (\x0D) and end with postamble (\x0A)

        """
        packet = self.ReceiveData
        if packet[:1] != b'\x0D':
            return False
        if packet[-1:] != b'\x0A':
            return False
        if packet[-2:-1] == self.calculate_crc(packet[1:-2]):
#            log.debug("[valid_packet] VALID PACKET!")
            return True
        log.debug("[valid_packet] Not valid packet, CRC failed, too bad")
        return False

    def calculate_crc(self, msg: bytearray):
        log.debug("[calculate_crc] Calculating for: %s", repr(msg))
        checksum = 0
        for char in msg[0:len(msg)]:
            checksum += char
        checksum = checksum % 255
        if checksum % 0xFF != 0:
            checksum = checksum ^ 0xFF
        log.debug("[calculate_crc] Calculated CRC is: %s", repr(bytearray([checksum])))
        return bytearray([checksum])

    def ProcessQueue(self, *bResend):
        """ Process the queue, restart timer or send command. Also possible to resend the last message
        """
        
        # Resend the request, but reset the receive counter to the original value
        if bResend:
            if self.SendLastCommand:
                    
                # Compare if we already have the same command in the buffer, then pop it first 
                bAddToQueue = True
                if self.SendQueue.qsize() >= 1:
# FIXME
                    if self.SendQueue[0].Msg == self.SendLastCommand.Msg and self.SendQueue[0].command == self.SendLastCommand.command and self.SendQueue[0].receive == self.SendLastCommand.receive :
                        bAddToQueue = False
                
                if bAddToQueue:
                    self.SendLastCommand.ReceiveCnt = self.SendLastCommand.ReceiveCntFixed
                    SendQueue.put(self.SendLastCommand, 0)
                    log.debug("[ProcessQueue] Resending Request '{0}' {1}. Queue Count={2}".format(self.SendLastCommand.Msg, hex(self.SendLastCommand.Command), self.SendQueue.qsize()))
                else:
                    log.debug("[ProcessQueue] ERROR: Resend is requested, but the last request is NULL?")
                    
        if self.SendQueue.empty():
            self.SendLastCommand = Null
            return
        
        # Check the timer again - possible something is received
#FIXME
        iMSec = relativedelta(datetime.now(), self.ReceiveLastPacket).microseconds
        
        # Check if the difference is at least 500+ msec
        if iMSec < self.SendDelay:
            # Start timer again for fast sending the command
            StartTimer(SendDelay - iMSec)
            return
#FIXME END        
        # Doublecheck if we got something in the queue
        if self.SendQueue.qsize() >= 1:
            # Send first queued command and remove it
            self.SendLastCommand = self.SendQueue.get()
            
            # Options are set, do our magic on the outgoing packet
            if self.SendLastCommand.options:
                
                aStr = self.SendLastCommand.options.split(':')
                if len(aStr) >= 1:
                
                    if aStr[0] == "PIN":
                    
                        # The Pin has to be 3 fields
                        if len(aStr) == 3:
                            iCode = 0
                            
                            if aStr[1] == "MasterCode":
                                iCode = MasterCode
                            elif aStr[1] == "DownloadCode":
                                iCode = DownloadCode
                            elif aStr[1] == "DisarmArmCode":
                                iCode = DisarmArmCode
                            else:
                                log.debug("[ProcessQueue] ERROR: Unknown Code defined ({0})".format(aStr[1]))
                    
                            if iCode != 0:
                                self.SendLastCommand.command[int(aStr[2])] = iCode >> 8 and 0xFF
                                self.SendLastCommand.command[int(aStr[2]) + 1] = iCode and 0xFF
                            else:
                                log.debug("[ProcessQueue] ERROR: The pincode is zero ({0}".format(hex(self.SendLastCommand.command)))
                        else:
                            log.debug("[ProcessQueue] ERROR: Invalid Option '{0}', Expected: 3, Received: {1}".format(self.SendLastCommand.options, aStr.count))
                        
                    else:
                        log.debug("[ProcessQueue] ERROR: Unknown Option '{0}'".format(self.SendLastCommand.options))
                else:
                    log.debug("[ProcessQueue] ERROR: Invalid Option '{0}'".format(self.SendLastCommand.options))
            
            # Now send it to the PowerMax/Master
            self.SendCommand(self.SendLastCommand.command)
            
            log.debug(self.SendLastCommand.message)


    def QueueCommand(self, packet: bytearray, msg: str, response: int, **kwargs):
        """ Add a command to the send queue
        The queue is needed to prevent sending messages to quickly
        normally it requires 500msec between messages """

# FIXME: LOTS more needed here still...
        # Copy the msg, command and response into our queue entry
#        cEntry.Msg = sMsg
#        cEntry.Command = packet
#        cEntry.Receive = response
        options = kwargs.get('options', None)
        
        # Log some usefull information in debug mode
        log.debug("[QueueCommand] Queued Command ({0})".format(msg))
                        
        # Download commands, can have answers split across multiple message
        # Determine the number of messages we should get
        if packet[0] == b'\x3E':
            iCnt = CInt(packet[4]) * b'\x100' + packet[3]
            ReceiveCnt = Floor(iCnt / b'\xB0') + 1
        else:
            ReceiveCnt = 1

        ReceiveCntFixed = ReceiveCnt
                                                
        # Add it to the queue
        self.SendQueue.put(VisonicQueueEntry(msg = msg, command = packet, receive = response, options = options, 
                           receivecount = ReceiveCnt, receivecountfixed = ReceiveCntFixed))
#        self.SendQueue.put(cEntry)

        # Kick off queue handling if required
        if not self.SendQueue.empty():
            self.ProcessQueue()
                                                              
    def ClearQueue(self):
        """ Clear the queue, preventing any retry causing issue. """

        # Clear the queue
        while not self.SendQueue.empty():
            try:
                self.SendQueue.get(False)
            except Empty:
                continue
            self.SendQueue.task_done()
        self.SendLastCommand = ''
    
    def SendCommand(self, packet: bytearray):
        """Encode and put packet string onto write buffer."""
        
        log.debug('[SendCommand] input data: %s', repr(packet))
        # First add preamble (0x0D), then the packet, then crc and postamble (0x0A)
        sData = b'\x0D'
        sData += packet
        sData += self.calculate_crc(packet)
        sData += b'\x0A'

        log.debug('[SendCommand] Writing data: %s', repr(sData))
        self.transport.write(sData)

    def connection_lost(self, exc):
        """Log when connection is closed, if needed call callback."""
        if exc:
            log.exception('disconnected due to exception')
        else:
            log.info('disconnected because of close/abort.')
        if self.disconnect_callback:
            self.disconnect_callback(exc)

class VisonicQueueEntry:
    def __init__(self, **kwargs):
        self.message = kwargs.get('message', None)
        self.command = kwargs.get('command', None)
        self.receive = kwargs.get('receive', None)
        self.receivecount = kwargs.get('receivecount', None)
        self.receivecountfixed = kwargs.get('receivecountfixed', None) # Need to store it extra, because with a re-queue it can get lost
        self.receiveretries = kwargs.get('receiveretries', None)
        self.options = kwargs.get('options', None)

            
class PacketHandling(ProtocolBase):
    """Handle decoding of Visonic packets."""
    
    sendlastcommand = VisonicQueueEntry()

    def __init__(self, *args, packet_callback: Callable = None,
                 **kwargs) -> None:
        """Add packethandling specific initialization.

        packet_callback: called with every complete/valid packet
        received.
        """
        super().__init__(*args, **kwargs)
        if packet_callback:
            self.packet_callback = packet_callback

#    def handle_raw_packet(self, raw_packet):
#        """Parse raw packet string into packet dict."""
#        log.debug('got packet: %s', raw_packet)
#        packet = raw_packet

#        if packet:
#            if 'ok' in packet:
#                # handle response packets internally
#                log.debug('command response: %s', packet)
#                self._last_ack = packet
#                self._command_ack.set()
#            else:
#                self.handle_packet(packet)
#        else:
#            log.warning('no valid packet')

    def handle_packet(self, packet):
        """Handle one raw incoming packet."""
        log.debug("[handle_packet] Parsing complete valid packet: %s", repr(packet))

        if packet[1:2] == b'\x02': # ACK
            self.handle_msgtype02()
        elif packet[1:2] == b'\x06': # Timeout
            self.handle_msgtype06()
        elif packet[1:2] == b'\x08': # Access Denied
            self.ACK = True
            self.handle_msgtype08()
        elif packet[1:2] == b'\x0B': # Stopped
            self.ACK = True
            self.Response = True
            log.debug("[handle_packet] Stopped")
        elif packet[1:2] == b'\x25': # Download retry
            self.ACK = True
            self.Response = True
            self.handle_msgtype25()
        elif packet[1:2] == b'\x33': # Settings send after a MSGV_START
            self.ACK = True
            self.Response = True
            self.handle_msgtype33()
        elif packet[1:2] == b'\x3c': # Message when start the download
            self.handle_msgtype3C()
        elif packet[1:2] == b'\x3f': # Download information
            self.ACK = True
            self.Response = True
            self.handle_msgtype3F()
        elif packet[1:2] == b'\xa0': # Event log
            self.ACK = True
            self.Response = True
            self.handle_msgtypeA0()
        elif packet[1:2] == b'\xa5': # General Event
            self.ACK = True
            self.Response = True
            self.handle_msgtypeA5()
        elif packet[1:2] == b'\xab': # PowerLink Event
            self.ACK = True
            self.Response = True
            self.handle_msgtypeAB()
        else:
            log.debug("[handle_packet] Unknown/Unhandled packet type {0}".format(packet[1:2]))
        # clear our buffer again so we can receive a new packet.
        self.ReceiveData = b''

            
    def displayzonebin(self, bits):
        """ Display Zones in reverse binary format
          Zones are from e.g. 1-8, but it is stored in 87654321 order in binary format """
          
        return bin(bits)

    def handle_msgtype02(self): # ACK
        log.debug("[handle_msgtype02] Acknowledgement")
          
        # Check if the last message is Download Exit, if this is the case, process the settings
        if self.sendlastcommand == True:
            if self.sendlastcommand.command[0] == b'\x0F':                  
                DownloadMode = False
                        
                # We received a download exit message, restart timer
                try:
                    tPowerLinkKeepAlive.cancel()
                except:
                    pass
                tPowerLinkKeepAlive = threading.Timer(PowerLinkTimeout, self.PowerLinkTimeoutExpired)
                tPowerLinkKeepAlive.start()
                                                      
                ProcessSettings()
            elif cSendLastCommand.command.count >= 3 and cSendLastCommand.command[0] == b'\xAB' and cSendLastCommand.command[1] == b'\x0A' and cSendLastCommand.command[2] == b'\x00':
                # The queue timer
                self.bQueueDelay = False
                try:
                    self.tQueueDelay.cancel()
                except:
                    pass
                self.tQueueDelay = threading.Timer(self.isenddelay, self.tQueueDelayExpired)
                self.tQueueDelay.start()

    def tQueueDelayExpired(self):
        """ Timer expired, start processing the queue (can restart the timer) """
    
        try:
            self.tQueueDelay.cancel()
        except:
            pass
      
        # Timer expired, kick off the processing of the queue
        if self.SendLastCommand and self.bQueueDelay:
            self.ProcessQueue(True)
        else:
            self.ProcessQueue()
                      
    def handle_msgtype06(self): 
        """ MsgType=06 - Time out
        Timeout message from the PM, most likely we are/were in download mode """
        log.debug("[handle_msgtype06] Timeout Received")
        
        send_message(VMSG_DL_EXIT)

    def handle_msgtype08(self):
          log.debug("[handle_msgtype08] Access Denied")
          
          # If LastMsgType And &HF0 = &HA0 Then
          #   Wrong pin  enroll, eventlog, arm, bypass
          # Endif
                
          # If LastMsgType And &HF0 = &H24 Then
          #   Wrong download pin
          # Endif


    def handle_msgtype25(self): # Download retry
        """ MsgType=25 - Download retry
        Unit is not ready to enter download mode
        """
        # Format: <MsgType> <?> <?> <delay in sec>
            
        iDelay = self.ReceiveData[4]
              
        log.debug("[handle_msgtype25] Download Retry, have to wait {0} seconds".format(iDelay))
                
        # Restart the queue timer
        self.bQueueDelay = True
        try:
            self.tQueueDelay.cancel()
        except:
            pass
        self.tQueueDelay = threading.Timer(iDelay, self.tQueueDelayExpired)
        self.tQueueDelay.start()
        
    def handle_MsgType33(self):
        """ MsgType=33 - Settings
        Message send after a VMSG_START. We will store the information in an internal array/collection """

        if self.ReceiveData.Count != 11:
            log.debug("[handle_msgtype33] ERROR: MSGTYPE=0x33 Expected=11, Received={0}".format(self.ReceiveData.count()))
            return
                
        # Format is: <MsgType> <index> <page> <data 8x bytes>
        # Extract Page and Index information
        iIndex = self.ReceiveData[1]
        iPage = self.ReceiveData[2]
                        
        # Write to memory map structure, but remove the first 3 bytes from the data
#FIXME        WriteMemoryMap(iPage, iIndex, self.ReceiveData[3:])
                            
    def handle_msgtype3C(self): # Messsage when start the download
        """ The panel information is in 5 & 6
           6=PanelType e.g. PowerMax, PowerMaster
           5=Sub model type of the panel - just informational
           """
           
        self.PanelType = self.ReceiveData[6]
        self.ModelType = self.ReceiveData[5]
        
        PowerMaster = (PanelType >= 7)
          
        log.debug("[handle_msgtype3C] PanelType={0}, Model={1}".format(DisplayPanelType(), DisplayPanelModel()))
            
        # We got a first response, now we can continue enrollment the PowerMax/Master PowerLink
        PowerLinkEnrolled()


    def handle_MsgType3F(self):
        """ MsgType=3F - Download information
        Multiple 3F can follow eachother, if we request more then &HFF bytes """

        # Format is normally: <MsgType> <index> <page> <length> <data ...>
        # If the <index> <page> = FF, then it is an additional PowerMaster MemoryMap
        iIndex = self.ReceiveData[1]
        iPage = self.ReceiveData[2]
        iLength = self.ReceiveData[3]
                
        # Check length and data-length
        if iLength != (self.ReceiveData.count() - 4):
            log.debug("[handle_msgtype3F] ERROR: Type=3F has an invalid length, Received: {0}, Expected: {1}".format(len(self.ReceiveData)-4, iLength))
                          
        # Write to memory map structure, but remove the first 4 bytes (3F/index/page/length) from the data
#FIXME
#       WriteMemoryMap(iPage, iIndex, self.ReceiveData[4:])

    def handle_MsgTypeA0(self):
        """ MsgType=A0 - Event Log """
#FIXME entire def
#        Dim iSec, iMin, iHour, iDay, iMonth, iYear As Integer
#        Dim iEventZone As Integer
#        Dim iLogEvent As Integer
#        Dim sEventZone As String
#        Dim sLogEvent As String
#        Dim sDate As String
#        Dim dDate As Date
              
        # Check for the first entry, it only contains the number of events
        if self.ReceiveData[2] == 0x01:
            log.debug("[handle_msgtypeA0] Eventlog received")
                      
            self.SendLastCommand.ReceiveCnt = self.ReceiveData[1] - 1
            self.SendLastCommand.ReceiveCntFixed = self.ReceiveData[1] - 1
        else:
            if self.PowerMaster:
                # Set sec, min, etc to 1-Jan-2000 00:00:00
                iSec = 0
                iMin = 0
                iHour = 0
                iDay = 1
                iMonth = 1
                iYear = 2000
                
                # timestamp looks unixtime
#FIXME
#                sDate = "0x" & format(self.ReceiveData[7], '02x') & format(self.ReceiveData[6], '02x') & format(self.ReceiveData[5], '02x') & format(self.ReceiveData[4], '02x')
#                try:
#                    dDate = DateAdd(CDate("1/1/1970"), gb.Second, Val(sDate))
#                if Not Error:
#                    iSec = Second(dDate)
#                    iMin = Minute(dDate)
#                    iHour = Hour(dDate)
#                    iDay = Day(dDate)
#                    iMonth = Month(dDate)
#                    iYear = Year(dDate)
            else:
                iSec = self.ReceiveData[3]
                iMin = self.ReceiveData[4]
                iHour = self.ReceiveData[5]
                iDay = self.ReceiveData[6]
                iMonth = self.ReceiveData[7]
                iYear = CInt(self.ReceiveData[8]) + 2000
                
            iEventZone = self.ReceiveData[9]
            iLogEvent = self.ReceiveData[10]
#FIXME
#            try:
#                sEventZone = self.Config["zonenname"][iEventZone]
#            if Error:
#                sEventZone = ""
            sLogEvent = DisplayLogEvent(iLogEvent)
                
#            if self.Debug:
#                sDate = CStr(iYear) & "/" & Format$(iMonth, "00") & "/" & Format$(iDay, "00") & " " & Format$(iHour, "00") & ":" & Format$(iMin, "00") & ":" & Format$(iSec, "00")
#                log.debug("  " & sDate & " " & sLogEvent & IIf(iEventZone <> 0 And iEventZone <= 64, ", Zone: " & iEventZone & IIf(sEventZone, " (" & sEventZone & ")", ""), ""))
                
    def handle_msgtypeA5(self): # Status Message
        packet = self.ReceiveData
        log.debug("[handle_msgtypeA5] Parsing A5 packet")
        if packet[3:4] == b'\x01': # Log event print
            log.debug("[handle_msgtypeA5] Log Event Print")
        elif packet[3:4] == b'\x02': # Status message zones
            log.debug("[handle_msgtypeA5] Status and Battery Message")
            log.debug("[handle_msgtypeA5] Status Zones 01-08: %s ", displayzonebin(packet[4:5]))
            log.debug("[handle_msgtypeA5] Status Zones 09-16: %s ", displayzonebin(packet[5:6]))
            log.debug("[handle_msgtypeA5] Status Zones 17-24: %s ", displayzonebin(packet[6:7]))
            log.debug("[handle_msgtypeA5] Status Zones 25-30: %s ", displayzonebin(packet[7:8]))
            log.debug("[handle_msgtypeA5] Battery Zones 01-08: %s ", displayzonebin(packet[8:9]))
            log.debug("[handle_msgtypeA5] Battery Zones 09-16: %s ", displayzonebin(packet[9:10]))
            log.debug("Battery Zones 17-24: %s ", displayzonebin(packet[10:11]))
            log.debug("Battery Zones 25-30: %s ", displayzonebin(packet[11:12]))
        elif packet[3:4] == b'\x03': # Tamper Event
            log.debug("Tamper event")
            log.debug("Status Zones 01-08: %s ", displayzonebin(packet[4:5]))
            log.debug("Status Zones 09-16: %s ", displayzonebin(packet[5:6]))
            log.debug("Status Zones 17-24: %s ", displayzonebin(packet[6:7]))
            log.debug("Status Zones 25-30: %s ", displayzonebin(packet[7:8]))
            log.debug("Battery Zones 01-08: %s ", displayzonebin(packet[8:9]))
            log.debug("Battery Zones 09-16: %s ", displayzonebin(packet[9:10]))
            log.debug("Battery Zones 17-24: %s ", displayzonebin(packet[10:11]))
            log.debug("Battery Zones 25-30: %s ", displayzonebin(packet[11:12]))
        elif packet[3:4] == b'\x04': # Zone event
            if packet[4:5] == b'\x00':
                slog = "Disarmed"
                sarm = "Disarmed"
            elif packet[4:5] == b'\x01':
                slog = "Exit Delay, Arming Home"
                sarm = "Disarmed"
            elif packet[4:5] == b'\x02':
                slog = "Exit Delay, Arming Away"
                sarm = "Disarmed"
            elif packet[4:5] == b'\x03':
                slog = "Entry Delay"
                sarm = "Armed"
            elif packet[4:5] == b'\x04':
                slog = "Armed Home"
                sarm = "Armed"
            elif packet[4:5] == b'\x05':
                slog = "Armed Away"
                sarm = "Armed"
            elif packet[4:5] == b'\x06':
                slog = "User Test"
                sarm = "Disarmed"
            elif packet[4:5] == b'\x07':
                slog = "Downloading"
                sarm = "Disarmed"
            elif packet[4:5] == b'\x08':
                slog = "Programming"
                sarm = "Disarmed"
            elif packet[4:5] == b'\x09':
                slog = "Installer"
                sarm = "Disarmed"
            elif packet[4:5] == b'\x0A':
                slog = "Home Bypass"
                sarm = "Armed"
            elif packet[4:5] == b'\x0B':
                slog = "Away Bypass"
                sarm = "Armed"
            elif packet[4:5] == b'\x0C':
                slog = "Ready"
                sarm = "Disarmed"
            elif packet[4:5] == b'\x0D':
                slog = "Not Ready"
                sarm = "Disarmed"
            elif packet[4:5] == b'\x10':
                slog = "Disarm"
                sarm = "Disarmed"
            elif packet[4:5] == b'\x11':
                slog = "Exit Delay"
                sarm = "Disarmed"
            elif packet[4:5] == b'\x12':
                slog = "Exit Delay"
                sarm = "Disarmed"
            elif packet[4:5] == b'\x13':
                slog = "Entry Delay"
                sarm = "Disarmed"
            elif packet[4:5] == b'\x14':
                slog = "Armed Home Instant"
                sarm = "Armed"
            elif packet[4:5] == b'\x15':
                slog = "Armed Away Instant"
                sarm = "Armed"
            else:
                slog = "Unknown {0})".format(repr(packet[4:5]))
                sarm = "Disarmed"
            log.debug("[handle_msgtypeA5] log: {0}, arm: {1}".format(slog, sarm))
#FIXME
            log.debug("[handle_msgtypeA5] Zone Event {0}".format(sLog))
      
#            iDeviceId = Devices.Find(Instance, "Panel", InterfaceId, IIf($bPowerMaster, "Visonic PowerMaster", "Visonic PowerMax"))
#            if iDeviceId:
#                Devices.ValueUpdate(iDeviceId, 1, sArm)
#                Devices.ValueUpdate(iDeviceId, 2, sLog)
                                          
            if (self.ReceiveData[4] and 1):
                log.debug("[handle_msgtypeA5] Bit 0 set, Ready")
            if (self.ReceiveData[4] and 2):
                log.debug("[handle_msgtypeA5] Bit 1 set, Alert in Memory")
            if (self.ReceiveData[4] and 4):
                log.debug("[handle_msgtypeA5] Bit 2 set, Trouble!")
            if (self.ReceiveData[4] and 8):
                log.debug("[handle_msgtypeA5] Bit 3 set, Bypass")
            if (self.ReceiveData[4] and 16):
                log.debug("[handle_msgtypeA5] Bit 4 set, Last 10 seconds of entry/exit")
            if (self.ReceiveData[4] and 32):
                if self.ReceiveData[6] == 0x00: # None
                    sLog = "No Zone Event"
                elif self.ReceiveData[6] == 0x01: # Tamper Alarm
                    sLog = "Tamper Alarm"
                elif self.ReceiveData[6] == 0x02: # Tamper Restore
                    sLog = "Tamper Alarm Restored"
                elif self.ReceiveData[6] == 0x03: # Open
                    sLog = "Zone Open"
                elif self.ReceiveData[6] == 0x04: # Closed
                    sLog = "Zone Closed"
                elif self.ReceiveData[6] == 0x05: # Violated (Motion)
                    sLog = "Zone Violation, Motion Detected"
                elif self.ReceiveData[6] == 0x06: # Panic Alarm
                    sLog = "Zone Panic Alarm"
                elif self.ReceiveData[6] == 0x07: # RF Jamming
                    sLog = "RF Jamming Detected"
                elif self.ReceiveData[6] == 0x08: # Tamper Open
                    sLog = "Zone Tamper Alarm Open"
                elif self.ReceiveData[6] == 0x09: # Communication Failure
                    sLog = "Zone Communication Failure"
                elif self.ReceiveData[6] == 0x0A: # Line Failure
                    sLog = "Zone Line Failure"
                elif self.ReceiveData[6] == 0x0B: # Fuse
                    sLog = "Zone Fuse"
                elif self.ReceiveData[6] == 0x0C: # Not Active
                    sLog = "Zone Not Active"
                elif self.ReceiveData[6] == 0x0D: # Low Battery
                    sLog = "Zone Low Battery"
                elif self.ReceiveData[6] == 0x0E: # AC Failure
                    sLog = "Zone AC Failure"
                elif self.ReceiveData[6] == 0x0F: # Fire Alarm
                    sLog = "Zone Fire Alarm"
                elif self.ReceiveData[6] == 0x10: # Emergency
                    sLog = "Zone Emergency"
                elif self.ReceiveData[6] == 0x11: # Siren Tamper
                    sLog = "Zone Siren Tamper"
                elif self.ReceiveData[6] == 0x12: # Siren Tamper Restore
                    sLog = "Zone Siren Tamper Restored"
                elif self.ReceiveData[6] == 0x13: # Siren Low Battery
                    sLog = "Zone Siren Low Battery"
                elif self.ReceiveData[6] == 0x14: # Siren AC Fail
                    sLog = "Zone Siren AC Failure"
                else:
                    sLog = "Unknown ({0})".format(hex(self.ReceiveData[6]))
                
                log.debug("[handle_msgtypeA5] Bit 5 set, Zone Event")
                log.debug("[handle_msgtypeA5] Zone: {0}, {1}".format(self.ReceiveData[5], sLog))
                
            if (self.ReceiveData[4] and 64):
                log.debug("[handle_msgtypeA5] Bit 6 set, Status Changed")
            if (self.ReceiveData[4] and 128): 
                log.debug("[handle_msgtypeA5] Bit 7 set, Alarm Event")
                
            # Trigger a zone event will only work for PowerMax and not for PowerMaster
            if not self.PowerMaster:
                #If $sReceiveData[6] = 3 Or If $sReceiveData[6] = 4 Or If $sReceiveData[6] = 5 Then
                if self.ReceiveData[6] == 5:
                
#                    if self.Config and if self.Config.Exist("zoneinfo") and if self.Config["zoneinfo"].Exist(self.ReceiveData[5]) and if self.Config["zoneinfo"][self.ReceiveData[5]]["sensortype"]:
#FIXME                        
                        # Try to find it, it will autocreate the device if needed
#                        iDeviceId = Devices.Find(Instance, "Z" & Format$($sReceiveData[5], "00"), InterfaceId, $cConfig["zoneinfo"][$sReceiveData[5]]["autocreate"])
#                        If iDeviceId Then Devices.ValueUpdate(iDeviceId, 1, "On")
                
#                        if $cConfig["zoneinfo"][$sReceiveData[5]]["sensortype"] == "Motion":
#                            EnableTripTimer("Z" & Format$($sReceiveData[5], "00"))
#                        Endif
#                    else:
                        log.write("[handle_msgtypeA5] ERROR: Zone information for zone '" & self.ReceiveData[5] & "' is unknown, possible the Plugin couldn't retrieve the information from the panel?")

#FIXME until here
        elif packet[3:4] == b'\x06': # Status message enrolled/bypassed
            log.debug("[handle_msgtypeA5] Enrollment and Bypass Message")
            log.debug("Enrolled Zones 01-08: " & displayzonebin(packet[4:5]))
            log.debug("Enrolled Zones 09-16: " & displayzonebin(packet[5:6]))
            log.debug("Enrolled Zones 17-24: " & displayzonebin(packet[6:7]))
            log.debug("Enrolled Zones 25-30: " & displayzonebin(packet[7:8]))
            log.debug("Bypassed Zones 01-08: " & displayzonebin(packet[8:9]))
            log.debug("Bypassed Zones 09-16: " & displayzonebin(packet[9:10]))
            log.debug("Bypassed Zones 17-24: " & displayzonebin(packet[10:11]))
            log.debug("Bypassed Zones 25-30: " & displayzonebin(packet[11:12]))
        else:
            log.debug("[handle_msgtypeA5] Unknown A5 Event: %s", repr(packet[3:4]))

    def handle_msgtypeA7(self):
        """ MsgType=A7 - Panel Status Change """
        log.debug("[handle_msgtypeA7] Panel Status Change")
          
        log.debug("Zone/User: " & DisplayZoneUser(packet[3:4]))
        log.debug("Log Event: " & DisplayLogEvent(packet[4:5]))
                
    def handle_msgtypeAB(self): # PowerLink Message
        """ MsgType=AB - PowerLink message """
        
        if self.ReceiveData[2] == b'\x03': # PowerLink keep-alive
            log.debug("[handle_msgtypeAB] PowerLink Keep-Alive ({0})".format(ByteToString(self.ReceiveData[6:4])))
              
            DownloadMode = False
              
            # We received a keep-alive, restart timer
            try:
                tPowerLinkKeepAlive.cancel()
            except:
                pass
            tPowerLinkKeepAlive = threading.Timer(PowerLinkTimeout, self.PowerLinkTimeoutExpired)
            tPowerLinkKeepAlive.start()
                                                        
        elif self.ReceiveData[2] == b'\x05': # Phone message
            if self.ReceiveData[4] == b'\x01':
                log.debug("PowerLink Phone: Calling User")
            elif self.ReceiveData[4] == b'\x02':
                log.debug("PowerLink Phone: User Acknowledged")
            else:
                log.debug("PowerLink Phone: Unknown Action {0}".format(hex(self.ReceiveData[3])))

        elif self.ReceiveData[2] == 10: # PowerLink most likely wants to auto-enroll
            log.debug("PowerLink most likely wants to auto-enroll")
            if self.ReceiveData[4] == 1:
                # Send the auto-enroll message with the new download pin/code
                self.SendMsg_ENROLL()
                                                                                                                                                        
                # Restart the timer
                try:
                    tPowerLinkKeepAlive.cancel()
                except:
                    pass
                tPowerLinkKeepAlive = threading.Timer(PowerLinkTimeout, self.PowerLinkTimeoutExpired)
                tPowerLinkKeepAlive.start()
        else:
            log.debug("Unknown AB Message: {0}".format(hex(self.ReceiveData[2])))


            
    def SendMsg_ENROLL(self):
        """ Auto enroll the PowerMax/Master unit """

        # Remove anything else from the queue, we need to restart
        self.ClearQueue()
        # Send the request, the pin will be added when the command is send
#        self.QueueCommand(VMSG_ENROLL, "Auto-Enroll of the PowerMax/Master", 0, options = "PIN:DownloadCode:4")
        self.QueueCommand(VMSG_ENROLL, "Auto-Enroll of the PowerMax/Master", 0)
        
        # We are doing an auto-enrollment, most likely the download failed. Lets restart the download stage.
        if self.DownloadMode:
            log.debug("[SendMsg_ENROLL] Resetting download mode to 'Off'")
            self.DownloadMode = False
                      
        self.SendMsg_DL_START()
                        
    def SendMsg_DL_START(self):
        """ Start download mode """
                        
        if not self.DownloadMode:
            self.DownloadMode = True
            self.QueueCommand(VMSG_DL_START, "Start Download Mode", 0x3C, options = "PIN:DownloadCode:3")
        else:
            log.debug("[SendMsg_DL_START] Already in Download Mode?")

            
class CommandSerialization(ProtocolBase):
    """Logic for ensuring asynchronous commands are send in order."""
        
    def __init__(self, *args, packet_callback: Callable = None,
                 **kwargs) -> None:
        """Add packethandling specific initialization."""
        super().__init__(*args, **kwargs)
        if packet_callback:
            self.packet_callback = packet_callback
        self._command_ack = asyncio.Event(loop=self.loop)
        self._ready_to_send = asyncio.Lock(loop=self.loop)
                                                                                         
    @asyncio.coroutine
    def send_command_ack(self, device_id, action):
        """Send command, wait for gateway to repond with acknowledgment."""
        # serialize commands
        yield from self._ready_to_send.acquire()
        acknowledgement = None
        try:
            self._command_ack.clear()
            self.send_command(device_id, action)
                                                                                                                                                                         
            log.debug('waiting for acknowledgement')
            try:
                yield from asyncio.wait_for(self._command_ack.wait(),
                                            TIMEOUT.seconds, loop=self.loop)
                log.debug('packet acknowledged')
            except concurrent.futures._base.TimeoutError:
                acknowledgement = {'ok': False, 'message': 'timeout'}
                log.warning('acknowledge timeout')
            else:
                acknowledgement = self._last_ack.get('ok', False)
        finally:
            # allow next command
            self._ready_to_send.release()
                                                                                                                                                                                                                                                                                                                                                         
        return acknowledgement
            
class EventHandling(PacketHandling):
    """Breaks up packets into individual events with ids'.

    Most packets represent a single event (light on, measured
    temparature), but some contain multiple events (temperature and
    humidity). This class adds logic to convert packets into individual
    events each with their own id based on packet details (protocol,
    switch, etc).
    """

    def __init__(self, *args, event_callback: Callable = None,
                 ignore: List[str] = None, **kwargs) -> None:
        """Add eventhandling specific initialization."""
        super().__init__(*args, **kwargs)
        self.event_callback = event_callback
        # suppress printing of packets
        if not kwargs.get('packet_callback'):
            self.packet_callback = lambda x: None
        if ignore:
            log.debug('ignoring: %s', ignore)
            self.ignore = ignore
        else:
            self.ignore = []

    def _handle_packet(self, packet):
        """Event specific packet handling logic.

        Break packet into events and fires configured event callback or
        nicely prints events for console.
        """
#        events = packet_events(packet)

#        for event in events:
#            if self.ignore_event(event['id']):
#                log.debug('ignoring event with id: %s', event)
#                continue
#            log.debug('got event: %s', event)
#            if self.event_callback:
#                self.event_callback(event)
#            else:
#                self.handle_event(event)

    def handle_event(self, event):
        """Default handling of incoming event (print)."""
        string = '{id:<32} '
        if 'command' in event:
            string += '{command}'
        elif 'version' in event:
            if 'hardware' in event:
                string += '{hardware} {firmware} '
            string += 'V{version} R{revision}'
        else:
            string += '{value}'
            if event.get('unit'):
                string += ' {unit}'

        print(string.format(**event))

    def handle_packet(self, packet):
        """Apply event specific handling and pass on to packet handling."""
#        self._handle_packet(packet)
        super().handle_packet(packet)

    def ignore_event(self, event_id):
        """Verify event id against list of events to ignore.

        >>> e = EventHandling(ignore=[
        ...   'test1_00',
        ...   'test2_*',
        ... ])
        >>> e.ignore_event('test1_00')
        True
        >>> e.ignore_event('test2_00')
        True
        >>> e.ignore_event('test3_00')
        False
        """
        for ignore in self.ignore:
            if (ignore == event_id or
                    (ignore.endswith('*') and event_id.startswith(ignore[:-1]))):
                return True
        return False

class VisonicProtocol(CommandSerialization, EventHandling):
    """Combine preferred abstractions that form complete Rflink interface."""


def create_visonic_connection(port=None, baud=9600, protocol=VisonicProtocol,
                             packet_callback=None, event_callback=None,
                             disconnect_callback=None, ignore=None, loop=None):
    """Create Visonic manager class, returns transport coroutine."""
    # use default protocol if not specified
    protocol = partial(
        protocol,
        loop=loop if loop else asyncio.get_event_loop(),
        packet_callback=packet_callback,
        event_callback=event_callback,
        disconnect_callback=disconnect_callback,
        ignore=ignore if ignore else [],
    )

    # setup serial connection
    baud = baud
    conn = create_serial_connection(loop, protocol, port, baud)

    return conn

#~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# Display Short PanelType name
#~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
def DisplayPanelType(self):
    switcher = {
    0: "PowerMax",
    1: "PowerMax+",
    2: "PowerMax Pro",
    3: "PowerMax Complete",
    4: "PowerMax Pro Part",
    5: "PowerMax Complete Part",
    6: "PowerMax Express",
    7: "PowerMaster10",
    8: "PowerMaster30",
    }
    return switcher.get(self.PanelType, "Unknown")
                                                                                                        
#~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# Display the panels model name
#~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
def DisplayPanelModel(self):
    switcher = {
    
    0x0000: "PowerMax",
    0x0001: "PowerMax LT",
    0x0004: "PowerMax A",
    0x0005: "PowerMax",
    0x0006: "PowerMax LT",
    0x0009: "PowerMax B",
    0x000A: "PowerMax A",
    0x000B: "PowerMax",
    0x000C: "PowerMax LT",
    0x000F: "PowerMax B",
    0x0014: "PowerMax A",
    0x0015: "PowerMax",
    0x0016: "PowerMax",
    0x0017: "PowerArt",
    0x0018: "PowerMax SC",
    0x0019: "PowerMax SK",
    0x001A: "PowerMax SV",
    0x001B: "PowerMax T",
    0x001E: "PowerMax WSS",
    0x001F: "PowerMax Smith",
    0x0100: "PowerMax+",
    0x0103: "PowerMax+ UK (3)",
    0x0104: "PowerMax+ JP",
    0x0106: "PowerMax+ CTA",
    0x0108: "PowerMax+",
    0x010a: "PowerMax+ SH",
    0x010b: "PowerMax+ CF",
    0x0112: "PowerMax+ WSS",
    0x0113: "PowerMax+ 2INST",
    0x0114: "PowerMax+ HL",
    0x0115: "PowerMax+ UK",
    0x0116: "PowerMax+ 2INST3",
    0x0118: "PowerMax+ CF",
    0x0119: "PowerMax+ 2INST",
    0x011A: "PowerMax+",
    0x011C: "PowerMax+ WSS",
    0x011D: "PowerMax+ UK",
    0x0120: "PowerMax+ 2INST33",
    0x0121: "PowerMax+",
    0x0122: "PowerMax+ CF",
    0x0124: "PowerMax+ UK",
    0x0127: "PowerMax+ 2INST_MONITOR",
    0x0128: "PowerMax+ KeyOnOff",
    0x0129: "PowerMax+ 2INST_MONITOR",
    0x012A: "PowerMax+ 2INST_MONITOR42",
    0x012B: "PowerMax+ 2INST33",
    0x012C: "PowerMax+ One Inst_1_44_0",
    0x012D: "PowerMax+ CF_1_45_0",
    0x012E: "PowerMax+ SA_1_46",
    0x012F: "PowerMax+ UK_1_47",
    0x0130: "PowerMax+ SA UK_1_48",
    0x0132: "PowerMax+ KeyOnOff 1_50",
    0x0201: "PowerMax Pro",
    0x0202: "PowerMax Pro-Nuon",
    0x0204: "PowerMax Pro-PortugalTelecom",
    0x020a: "PowerMax Pro-PortugalTelecom2",
    0x020c: "PowerMax HW-V9 Pro",
    0x020d: "PowerMax ProSms",
    0x0214: "PowerMax Pro-PortugalTelecom_4_5_02",
    0x0216: "PowerMax HW-V9_4_5_02 Pro",
    0x0217: "PowerMax ProSms_4_5_02",
    0x0218: "PowerMax UK_DD243_4_5_02 Pro M",
    0x021B: "PowerMax Pro-Part2__2_27",
    0x0223: "PowerMax Pro Bell-Canada",
    0x0301: "PowerMax Complete",
    0x0302: "PowerMax Complete_NV",
    0x0303: "PowerMax Complete-PortugalTelecom",
    0x0307: "PowerMax Complete_1_0_07",
    0x0308: "PowerMax Complete_NV_1_0_07",
    0x030A: "PowerMax Complete_UK_DD243_1_1_03",
    0x030B: "PowerMax Complete_COUNTERFORCE_1_0_06",
    0x0401: "PowerMax Pro-Part",
    0x0402: "PowerMax Pro-Part CellAdaptor",
    0x0405: "PowerMax Pro-Part_5_0_08",
    0x0406: "PowerMax Pro-Part CellAdaptor_5_2_04",
    0x0407: "PowerMax Pro-Part KeyOnOff_5_0_08",
    0x0408: "PowerMax UK Pro-Part_5_0_08",
    0x0409: "PowerMax SectorUK Pro-Part_5_0_08",
    0x040A: "PowerMax Pro-Part CP1 4_10",
    0x040C: "PowerMax Pro-Part_Cell_key_4_12",
    0x040D: "PowerMax Pro-Part UK 4_13",
    0x040E: "PowerMax SectorUK Pro-Part_4_14",
    0x040F: "PowerMax Pro-Part UK 4_15",
    0x0410: "PowerMax Pro-Part CP1 4_16",
    0x0411: "PowerMax NUON key 4_17",
    0x0433: "PowerMax Pro-Part2__4_51",
    0x0434: "PowerMax UK Pro-Part2__4_52",
    0x0436: "PowerMax Pro-Part2__4_54",
    0x0437: "PowerMax Pro-Part2__4_55 (CP_01)",
    0x0438: "PowerMax Pro-Part2__4_56",
    0x0439: "PowerMax Pro-Part2__4_57 (NUON)",
    0x043a: "PowerMax Pro 4_58",
    0x043c: "PowerMax Pro 4_60",
    0x043e: "PowerMax Pro-Part2__4_62",
    0x0440: "PowerMax Pro-Part2__4_64",
    0x0442: "PowerMax 4_66",
    0x0443: "PowerMax Pro 4_67",
    0x0444: "PowerMax Pro 4_68",
    0x0445: "PowerMax Pro 4_69",
    0x0446: "PowerMax Pro-Part2__4_70",
    0x0447: "PowerMax 4_71",
    0x0449: "PowerMax 4_73",
    0x044b: "PowerMax Pro-Part2__4_75",
    0x0451: "PowerMax Pro 4_81",
    0x0452: "PowerMax Pro 4_82",
    0x0454: "PowerMax 4_84",
    0x0455: "PowerMax 4_85",
    0x0456: "PowerMax 4_86",
    0x0503: "PowerMax UK Complete partition 1_5_00",
    0x050a: "PowerMax Complete partition GPRS",
    0x050b: "PowerMax Complete partition NV GPRS",
    0x050c: "PowerMax Complete partition GPRS NO-BBA",
    0x050d: "PowerMax Complete partition NV GPRS NO-BBA",
    0x050e: "PowerMax Complete part. GPRS NO-BBA UK_5_14",
    0x0511: "PowerMax Pro-Part CP1 GPRS 5_17",
    0x0512: "PowerMax Complete part. BBA UK_5_18",
    0x0533: "PowerMax Complete part2 5_51",
    0x0534: "PowerMax Complete part2 5_52 (UK)",
    0x0536: "PowerMax Complete 5_54 (GR)",
    0x0537: "PowerMax Complete 5_55",
    0x053A: "PowerMax Complete 5_58 (PT)",
    0x053B: "PowerMax Complete part2 5_59 (NV)",
    0x053C: "PowerMax Complete 5_60",
    0x053E: "PowerMax Complete 5_62",
    0x053F: "PowerMax Complete part2  5_63",
    0x0540: "PowerMax Complete 5_64",
    0x0541: "PowerMax Complete 5_65",
    0x0543: "PowerMax Complete 5_67",
    0x0544: "PowerMax Complete 5_68",
    0x0545: "PowerMax Complete 5_69",
    0x0546: "PowerMax Complete 5_70",
    0x0547: "PowerMax Complete 5_71",
    0x0549: "PowerMax Complete 5_73",
    0x054B: "PowerMax Complete 5_75",
    0x054F: "PowerMax Complete 5_79",
    0x0601: "PowerMax Express",
    0x0603: "PowerMax Express CP 01",
    0x0605: "PowerMax Express OEM 6_5",
    0x0607: "PowerMax Express BBA 6_7",
    0x0608: "PowerMax Express CP 01 BBA 6_8",
    0x0609: "PowerMax Express OEM1 BBA 6_9",
    0x060B: "PowerMax Express BBA 6_11",
    0x0633: "PowerMax Express 6_51",
    0x063B: "PowerMax Express 6_59",
    0x063D: "PowerMax Express 6_61",
    0x063E: "PowerMax Express 6_62 (UK)",
    0x0645: "PowerMax Express 6_69",
    0x0647: "PowerMax Express 6_71",
    0x0648: "PowerMax Express 6_72",
    0x0649: "PowerMax Express 6_73",
    0x064A: "PowerMax Activa 6_74",
    0x064C: "PowerMax Express 6_76",
    0x064D: "PowerMax Express 6_77",
    0x064E: "PowerMax Express 6_78",
    0x064F: "PowerMax Secure 6_79",
    0x0650: "PowerMax Express 6_80",
    0x0650: "PowerMax Express part2 M 6_80",
    0x0651: "PowerMax Express 6_81",
    0x0652: "PowerMax Express 6_82",
    0x0653: "PowerMax Express 6_83",
    0x0654: "PowerMax 6_84",
    0x0655: "PowerMax 6_85",
    0x0658: "PowerMax 6_88",
    0x0659: "PowerMax 6_89",
    0x065A: "PowerMax 6_90",
    0x065B: "PowerMax 6_91",
    0x0701: "PowerMax PowerCode-G 7_1",
    0x0702: "PowerMax PowerCode-G 7_2",
    0x0704: "PowerMaster10 7_4",
    0x0705: "PowerMaster10 7_05",
    0x0707: "PowerMaster10 7_07",
    0x070C: "PowerMaster10 7_12",
    0x070F: "PowerMaster10 7_15",
    0x0710: "PowerMaster10 7_16",
    0x0711: "PowerMaster10 7_17",
    0x0712: "PowerMaster10 7_18",
    0x0713: "PowerMaster10 7_19",
    0x0735: "PowerMaster10 7_53",
    0x0802: "PowerMax Complete PowerCode-G 8_2",
    0x0803: "PowerMaster30 8_3",
    0x080F: "PowerMaster30 8_15",
    0x0810: "PowerMaster30 8_16",
    0x0812: "PowerMaster30 8_18",
    0x0813: "PowerMaster30 8_19",
    0x0815: "PowerMaster30 8_21",
    }

    return switcher.get(self.PanelType*0x100 + self.ModelType, "Unknown ({0})".format(hex(self.PanelType) * 0x100 + self.ModelType))

#~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# Display Zone/User
#~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
def DisplayZoneUser(self):
    switcher = {
    0x0: "System",
    0x1: "Zone 1",
    0x2: "Zone 2",
    0x3: "Zone 3",
    0x4: "Zone 4",
    0x5: "Zone 5",
    0x6: "Zone 6",
    0x7: "Zone 7",
    0x8: "Zone 8",
    0x9: "Zone 9",
    0xA: "Zone 10"
    0xB: "Zone 11",
    0xC: "Zone 12",
    0xD: "Zone 13",
    0xE: "Zone 14",
    0xF: "Zone 15",
    0x10: "Zone 16",
    0x11: "Zone 17",
    0x12: "Zone 18",
    0x13: "Zone 19",
    0x14: "Zone 20",
    0x15: "Zone 21",
    0x16: "Zone 22",
    0x17: "Zone 23",
    0x18: "Zone 24",
    0x19: "Zone 25",
    0x1A: "Zone 26",
    0x1B: "Zone 27",
    0x1C: "Zone 28",
    0x1D: "Zone 29",
    0x1E: "Zone 30",
    0x1F: "Keyfob1",
    0x20: "Keyfob2",
    0x21: "Keyfob3",
    0x22: "Keyfob4",
    0x23: "Keyfob5",
    0x24: "Keyfob6",
    0x25: "Keyfob7",
    0x26: "Keyfob8",
    0x27: "User1",
    0x28: "User2",
    0x29: "User3",
    0x2A: "User4",
    0x2B: "User5",
    0x2C: "User6",
    0x2D: "User7",
    0x2E: "User8",
    0x2F: "Wireless Commander1",
    0x30: "Wireless Commander2",
    0x31: "Wireless Commander3",
    0x32: "Wireless Commander4",
    0x33: "Wireless Commander5",
    0x34: "Wireless Commander6",
    0x35: "Wireless Commander7",
    0x36: "Wireless Commander8",
    0x37: "Wireless Siren1",
    0x38: "Wireless Siren2",
    0x39: "2Way Wireless Keypad1",
    0x3A: "2Way Wireless Keypad2",
    0x3B: "2Way Wireless Keypad3",
    0x3C: "2Way Wireless Keypad4",
    0x3D: "X10-1",
    0x3E: "X10-2",
    0x3F: "X10-3",
    0x40: "X10-4",
    0x41: "X10-5",
    0x42: "X10-6",
    0x43: "X10-7",
    0x44: "X10-8",
    0x45: "X10-9",
    0x46: "X10-10",
    0x47: "X10-11",
    0x48: "X10-12",
    0x49: "X10-13",
    0x4A: "X10-14",
    0x4B: "X10-15",
    0x4C: "PGM",
    0x4D: "GSM",
    0x4E: "Powerlink",
    0x4F: "Proxy Tag1",
    0x50: "Proxy Tag2",
    0x51: "Proxy Tag3",
    0x52: "Proxy Tag4",
    0x53: "Proxy Tag5",
    0x54: "Proxy Tag6",
    0x55: "Proxy Tag7",
    0x56: "Proxy Tag8",
    }

    return switcher.get(self.PanelType, "Unknown")
