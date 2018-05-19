"""Asyncio protocol implementation of Visonic PowerMaster/PowerMax.
  Based on the DomotiGa implementation:

  Credits:
    Initial setup by Wouter Wolkers and Alexander Kuiper.
    Thanks to everyone who helped decode the data.

  Converted to Python module by Wouter Wolkers
"""

########################################################
# PowerMax/Master send messages
########################################################

import struct
import re
import asyncio
import concurrent
import logging
import sys
import pkg_resources
import threading
import collections
import time

from datetime import datetime
from time import sleep
from datetime import timedelta
from dateutil.relativedelta import *
from functools import partial
from typing import Callable, List
from serial_asyncio import create_serial_connection
from collections import namedtuple

msleep = lambda x: sleep(x/1000.0)

PLUGIN_VERSION = "0.1"
pmMotionOffDelay = 3
pmLang = "EN"
pmRemoteArm = False
pmSensorArm = "auto"

MSG_RETRIES = 3
MAX_CRC_ERROR = 5
MAX_EXCEPTIONS = 3
TIMEOUT = timedelta(seconds=50)
PowerLinkTimeout = 60
MasterCode = bytearray.fromhex('56 50')
DisarmArmCode = bytearray.fromhex('56 50')
DownloadCode = bytearray.fromhex('56 50')

# not currently used
pmPanelInit_t = [
   ( "PluginVersion", PLUGIN_VERSION, False ), # False: update
   ( "MotionOffDelay", pmMotionOffDelay, True ), # True: create only
   ( "PluginLanguage", pmLang, True ),
   ( "PluginDebug", "0", True ),
   ( "ForceStandard", "0", True ),
   ( "AutoCreate", "1", True ),
   ( "AutoSyncTime", "1", True ),
   ( "EnableRemoteArm", pmRemoteArm, True ),
   ( "SensorArm", pmSensorArm, True ),
   ( "DoorZones", "", True ),
   ( "MotionZones", "", True ),
   ( "SmokeZones", "", True ),
   ( "Devices", "", True ),
   ( "PowerlinkMode", "Unknown", False ),
   ( "PanelStatusCode", "", True ),
   ( "PanelStatusData", "", True ),
   ( "PanelAlarmType", "", True ),
   ( "PanelTroubleType", "", True )
]

# use a named tuple for data and acknowledge
#    replytype is a message type from the Panel that we should get in response
#    waitforack, if True means that we should wait for the acknowledge from the Panel before progressing
VisonicCommand = collections.namedtuple('VisonicCommand', 'data replytype waitforack msg')
pmSendMsg = {
   "MSG_INIT"        : VisonicCommand(bytearray.fromhex('AB 0A 00 01 00 00 00 00 00 00 00 43'), None, True,  "Initializing PowerMax/Master PowerLink Connection" ),
   "MSG_ALIVE"       : VisonicCommand(bytearray.fromhex('AB 03 00 00 00 00 00 00 00 00 00 43'), None, False, "I'm Alive Message To Panel" ),
   "MSG_ZONENAME"    : VisonicCommand(bytearray.fromhex('A3 00 00 00 00 00 00 00 00 00 00 43'), None, False, "Requesting Zone Names" ),
   "MSG_ZONETYPE"    : VisonicCommand(bytearray.fromhex('A6 00 00 00 00 00 00 00 00 00 00 43'), None, False, "Requesting Zone Types" ),
   "MSG_X10NAMES"    : VisonicCommand(bytearray.fromhex('AC 00 00 00 00 00 00 00 00 00 00 43'), None, False, "Requesting X10 Names" ),
   "MSG_RESTORE"     : VisonicCommand(bytearray.fromhex('AB 06 00 00 00 00 00 00 00 00 00 43'), 0xA5, False, "Restore PowerMax/Master Connection" ),
   "MSG_ENROLL"      : VisonicCommand(bytearray.fromhex('AB 0A 00 00 99 99 00 00 00 00 00 43'), 0xAB, False, "Auto-Enroll of the PowerMax/Master" ),
   "MSG_EVENTLOG"    : VisonicCommand(bytearray.fromhex('A0 00 00 00 12 34 00 00 00 00 00 43'), 0xA0, False, "Retrieving Event Log" ),
   "MSG_ARM"         : VisonicCommand(bytearray.fromhex('A1 00 00 00 12 34 00 00 00 00 00 43'), None, False, "Arming System" ),
   "MSG_STATUS"      : VisonicCommand(bytearray.fromhex('A2 00 00 00 00 00 00 00 00 00 00 43'), 0xA5, False, "Getting Status" ),
   "MSG_BYPASSTAT"   : VisonicCommand(bytearray.fromhex('A2 00 00 20 00 00 00 00 00 00 00 43'), 0xA5, False, "Bypassing" ),
   "MSG_X10PGM"      : VisonicCommand(bytearray.fromhex('A4 00 00 00 00 00 12 34 00 00 43')   , None, False, "X10 Data" ),
   "MSG_BYPASSEN"    : VisonicCommand(bytearray.fromhex('AA 12 34 00 00 00 00 43')            , None, False, "BYPASS Enable" ),
   "MSG_BYPASSDIS"   : VisonicCommand(bytearray.fromhex('AA 12 34 00 00 00 00 12 34 43')      , None, False, "BYPASS Disable" ),
   "MSG_DOWNLOAD"    : VisonicCommand(bytearray.fromhex('24 00 00 99 99 00 00 00 00 00 00')   , 0x3C,  True, "Start Download Mode" ),
   "MSG_SETTIME"     : VisonicCommand(bytearray.fromhex('46 F8 00 01 02 03 04 05 06 FF FF')   , None, False, "Setting Time" ),   # may not need an ack
   "MSG_DL"          : VisonicCommand(bytearray.fromhex('3E 00 00 00 00 B0 00 00 00 00 00')   , 0x3F, False, "Download Data Set" ),
   "MSG_SER_TYPE"    : VisonicCommand(bytearray.fromhex('5A 30 04 01 00 00 00 00 00 00 00')   , 0x33, False, "Get Serial Type" ),
   "MSG_START"       : VisonicCommand(bytearray.fromhex('0A')                                 , 0x0B, False, "Start" ),    ## waiting for download complete from panel
   "MSG_EXIT"        : VisonicCommand(bytearray.fromhex('0F')                                 , None, False, "Exit" ),
   # PowerMaster specific
   "MSG_POWERMASTER" : VisonicCommand(bytearray.fromhex('B0 01 12 34 43')                     , 0xB0, False, "Powermaster Command" )
}

pmSendMsgB0_t = {
   "ZONE_STAT1" : bytearray.fromhex('04 06 02 FF 08 03 00 00'),
   "ZONE_STAT2" : bytearray.fromhex('07 06 02 FF 08 03 00 00'),
   "ZONE_NAME"  : bytearray.fromhex('21 02 05 00'),
   "ZONE_TYPE"  : bytearray.fromhex('2D 02 05 00')
}

# To use the following, use  "MSG_DL" above and replace bytes 1 to 4 with the following
#       for example:    self.SendCommand("MSG_DL", item = pmDownloadItem_t["MSG_DL_PANELFW"] )     # Request the panel FW
pmDownloadItem_t = {
   "MSG_DL_TIME"         : bytearray.fromhex('F8 00 06 00'),   # F8 00 20 00
   "MSG_DL_COMMDEF"      : bytearray.fromhex('01 01 1E 00'),
   "MSG_DL_PHONENRS"     : bytearray.fromhex('36 01 20 00'),
   "MSG_DL_PINCODES"     : bytearray.fromhex('FA 01 10 00'),
   "MSG_DL_PGMX10"       : bytearray.fromhex('14 02 D5 00'),
   "MSG_DL_PARTITIONS"   : bytearray.fromhex('00 03 F0 00'),
   "MSG_DL_PANELFW"      : bytearray.fromhex('00 04 20 00'),
   "MSG_DL_SERIAL"       : bytearray.fromhex('30 04 08 00'),
   "MSG_DL_ZONES"        : bytearray.fromhex('00 09 78 00'),
   "MSG_DL_KEYFOBS"      : bytearray.fromhex('78 09 40 00'),
   "MSG_DL_2WKEYPAD"     : bytearray.fromhex('00 0A 08 00'),
   "MSG_DL_1WKEYPAD"     : bytearray.fromhex('20 0A 40 00'),
   "MSG_DL_SIRENS"       : bytearray.fromhex('60 0A 08 00'),
   "MSG_DL_X10NAMES"     : bytearray.fromhex('30 0B 10 00'),
   "MSG_DL_ZONENAMES"    : bytearray.fromhex('40 0B 1E 00'),
   "MSG_DL_EVENTLOG"     : bytearray.fromhex('DF 04 28 03'),
   "MSG_DL_ZONESTR"      : bytearray.fromhex('00 19 00 02'),
   "MSL_DL_ZONECUSTOM"   : bytearray.fromhex('A0 1A 50 00'),
   "MSG_DL_MR_ZONENAMES" : bytearray.fromhex('60 09 40 00'),
   "MSG_DL_MR_PINCODES"  : bytearray.fromhex('98 0A 60 00'),
   "MSG_DL_MR_SIRENS"    : bytearray.fromhex('E2 B6 50 00'),
   "MSG_DL_MR_KEYPADS"   : bytearray.fromhex('32 B7 40 01'),
   "MSG_DL_MR_ZONES"     : bytearray.fromhex('72 B8 80 02'),
   "MSG_DL_MR_SIRKEYZON" : bytearray.fromhex('E2 B6 10 04'), # Combines Sirens keypads and sensors
   "MSG_DL_ALL"          : bytearray.fromhex('00 00 00 FF')
}

# Message types we can receive with their length (None=unknown) and whether they need an ACK
PanelCallBack = collections.namedtuple("PanelCallBack", 'length ackneeded' )
pmReceiveMsg_t = {
   0x02 : PanelCallBack( None, False ),  # Ack
   0x06 : PanelCallBack( None, False ),  # Timeout. See the receiver function for ACK handling
   0x08 : PanelCallBack( None, True ),   # Access Denied
   0x0B : PanelCallBack( None, True ),   # Stop --> Download Complete
   0x25 : PanelCallBack(   14, True ),     # 14 Download Retry  
   0x33 : PanelCallBack(   14, True ),  # 14 Download Settings   Do not acknowledge 0x33 back to panel
   0x3C : PanelCallBack(   14, True ),     # 14 Panel Info
   0x3F : PanelCallBack( None, True ),   # Download Info
   0xA0 : PanelCallBack(   15, True ),     # 15 Event Log
   0xA5 : PanelCallBack(   15, True ),     # 15 Status Update       Length was 15 but panel seems to send different lengths
   0xA7 : PanelCallBack(   15, True ),     # 15 Panel Status Change
   0xAB : PanelCallBack(   15, True ),     # 15 Enroll Request 0x0A  OR Ping 0x03      Length was 15 but panel seems to send different lengths
   0xB0 : PanelCallBack( None, True ),
   0xF1 : PanelCallBack( None, False )      # 9
}

pmReceiveMsgB0_t = {
   0x04 : "Zone status",
   0x18 : "Open/close status",
   0x39 : "Activity"
}

pmLogEvent_t = { 
   "EN" : (
           "None", "Interior Alarm", "Perimeter Alarm", "Delay Alarm", "24h Silent Alarm", "24h Audible Alarm",
           "Tamper", "Control Panel Tamper", "Tamper Alarm", "Tamper Alarm", "Communication Loss", "Panic From Keyfob",
           "Panic From Control Panel", "Duress", "Confirm Alarm", "General Trouble", "General Trouble Restore", 
           "Interior Restore", "Perimeter Restore", "Delay Restore", "24h Silent Restore", "24h Audible Restore",
           "Tamper Restore", "Control Panel Tamper Restore", "Tamper Restore", "Tamper Restore", "Communication Restore",
           "Cancel Alarm", "General Restore", "Trouble Restore", "Not used", "Recent Close", "Fire", "Fire Restore", 
           "No Active", "Emergency", "No used", "Disarm Latchkey", "Panic Restore", "Supervision (Inactive)",
           "Supervision Restore (Active)", "Low Battery", "Low Battery Restore", "AC Fail", "AC Restore",
           "Control Panel Low Battery", "Control Panel Low Battery Restore", "RF Jamming", "RF Jamming Restore",
           "Communications Failure", "Communications Restore", "Telephone Line Failure", "Telephone Line Restore",
           "Auto Test", "Fuse Failure", "Fuse Restore", "Keyfob Low Battery", "Keyfob Low Battery Restore", "Engineer Reset",
           "Battery Disconnect", "1-Way Keypad Low Battery", "1-Way Keypad Low Battery Restore", "1-Way Keypad Inactive",
           "1-Way Keypad Restore Active", "Low Battery", "Clean Me", "Fire Trouble", "Low Battery", "Battery Restore",
           "AC Fail", "AC Restore", "Supervision (Inactive)", "Supervision Restore (Active)", "Gas Alert", "Gas Alert Restore",
           "Gas Trouble", "Gas Trouble Restore", "Flood Alert", "Flood Alert Restore", "X-10 Trouble", "X-10 Trouble Restore",
           "Arm Home", "Arm Away", "Quick Arm Home", "Quick Arm Away", "Disarm", "Fail To Auto-Arm", "Enter To Test Mode",
           "Exit From Test Mode", "Force Arm", "Auto Arm", "Instant Arm", "Bypass", "Fail To Arm", "Door Open",
           "Communication Established By Control Panel", "System Reset", "Installer Programming", "Wrong Password",
           "Not Sys Event", "Not Sys Event", "Extreme Hot Alert", "Extreme Hot Alert Restore", "Freeze Alert", 
           "Freeze Alert Restore", "Human Cold Alert", "Human Cold Alert Restore", "Human Hot Alert",
           "Human Hot Alert Restore", "Temperature Sensor Trouble", "Temperature Sensor Trouble Restore",
           # new values partition models
           "PIR Mask", "PIR Mask Restore", "", "", "", "", "", "", "", "", "", "",
           "Alarmed", "Restore", "Alarmed", "Restore", "", "", "", "", "", "", "", "", "", "",
           "", "", "", "", "", "Exit Installer", "Enter Installer", "", "", "", "", "" ), 
   "NL" : (
           "Geen", "In alarm", "In alarm", "In alarm", "In alarm", "In alarm", 
           "Sabotage alarm", "Systeem sabotage", "Sabotage alarm", "Add user", "Communicate fout", "Paniekalarm",
           "Code bedieningspaneel paniek", "Dwang", "Bevestig alarm", "Successful U/L", "Probleem herstel",
           "Herstel", "Herstel", "Herstel", "Herstel", "Herstel", 
           "Sabotage herstel", "Systeem sabotage herstel", "Sabotage herstel", "Sabotage herstel", "Communicatie herstel",
           "Stop alarm", "Algemeen herstel", "Brand probleem herstel", "Systeem inactief", "Recent close", "Brand", "Brand herstel",
           "Niet actief", "Noodoproep", "Remove user", "Controleer code", "Bevestig alarm", "Supervisie", 
           "Supervisie herstel", "Batterij laag", "Batterij OK", "230VAC uitval", "230VAC herstel",
           "Controlepaneel batterij laag", "Controlepaneel batterij OK", "Radio jamming", "Radio herstel",
           "Communicatie mislukt", "Communicatie hersteld", "Telefoonlijn fout", "Telefoonlijn herstel", 
           "Automatische test", "Zekeringsfout", "Zekering herstel", "Batterij laag", "Batterij OK", "Monteur reset",
           "Accu vermist", "Batterij laag", "Batterij OK", "Supervisie", 
           "Supervisie herstel", "Lage batterij bevestiging", "Reinigen", "Probleem", "Batterij laag", "Batterij OK",
           "230VAC uitval", "230VAC herstel", "Supervisie", "Supervisie herstel", "Gas alarm", "Gas herstel",
           "Gas probleem", "Gas probleem herstel", "Lekkage alarm", "Lekkage herstel", "Probleem", "Probleem herstel", 
           "Deelschakeling", "Ingeschakeld", "Snel deelschakeling", "Snel ingeschakeld", "Uitgezet", "Inschakelfout (auto)", "Test gestart",
           "Test gestopt", "Force aan", "Geheel in (auto)", "Onmiddelijk", "Overbruggen", "Inschakelfout",
           "Log verzenden", "Systeem reset", "Installateur programmeert", "Foutieve code", "Overbruggen" )
}

pmLogUser_t = [
   "System ", "Zone 01", "Zone 02", "Zone 03", "Zone 04", "Zone 05", "Zone 06", "Zone 07", "Zone 08",
   "Zone 09", "Zone 10", "Zone 11", "Zone 12", "Zone 13", "Zone 14", "Zone 15", "Zone 16", "Zone 17", "Zone 18",
   "Zone 19", "Zone 20", "Zone 21", "Zone 22", "Zone 23", "Zone 24", "Zone 25", "Zone 26", "Zone 27", "Zone 28", 
   "Zone 29", "Zone 30", "Fob  01", "Fob  02", "Fob  03", "Fob  04", "Fob  05", "Fob  06", "Fob  07", "Fob  08", 
   "User 01", "User 02", "User 03", "User 04", "User 05", "User 06", "User 07", "User 08", "Pad  01", "Pad  02",
   "Pad  03", "Pad  04", "Pad  05", "Pad  06", "Pad  07", "Pad  08", "Sir  01", "Sir  02", "2Pad 01", "2Pad 02",
   "2Pad 03", "2Pad 04", "X10  01", "X10  02", "X10  03", "X10  04", "X10  05", "X10  06", "X10  07", "X10  08",
   "X10  09", "X10  10", "X10  11", "X10  12", "X10  13", "X10  14", "X10  15", "PGM    ", "GSM    ", "P-LINK ",
   "PTag 01", "PTag 02", "PTag 03", "PTag 04", "PTag 05", "PTag 06", "PTag 07", "PTag 08" 
]

pmSysStatus_t = { 
   "EN" : (
           "Disarmed", "Home Exit Delay", "Away Exit Delay", "Entry Delay", "Armed Home", "Armed Away", "User Test",
           "Downloading", "Programming", "Installer", "Home Bypass", "Away Bypass", "Ready", "Not Ready", "??", "??", 
           "Disarmed Instant", "Home Instant Exit Delay", "Away Instant Exit Delay", "Entry Delay Instant", "Armed Home Instant", 
           "Armed Away Instant" ),
   "NL" : (
           "Uitgeschakeld", "Deel uitloopvertraging", "Totaal uitloopvertraging", "Inloopvertraging", "Deel ingeschakeld",
           "Totaal ingeschakeld", "Gebruiker test", "Downloaden", "Programmeren", "Monteurmode", "Deel met overbrugging",
           "Totaal met overbrugging", "Klaar", "Niet klaar", "??", "??", "Direct uitschakelen", "Direct Deel uitloopvertraging",
           "Direct Totaal uitloopvertraging", "Direct inloopvertraging", "Direct Deel", "Direct Totaal" )
}

pmSysStatusFlags_t = { 
   "EN" : ( "Ready", "Alert in memory", "Trouble", "Bypass on", "Last 10 seconds", "Zone event", "Status changed", "Alarm event" ),
   "NL" : ( "Klaar", "Alarm in geheugen", "Probleem", "Overbruggen aan", "Laatste 10 seconden", "Zone verstoord", "Status gewijzigd", "Alarm actief")
}

pmArmed_t = {
   0x03 : "", 0x04 : "", 0x05 : "", 0x0A : "", 0x0B : "", 0x13 : "", 0x14 : "", 0x15 : ""
}

pmArmMode_t = {
   "Disarmed" : 0x00, "Stay" : 0x04, "Armed" : 0x05, "UserTest" : 0x06, "StayInstant" : 0x14, "ArmedInstant" : 0x15, "Night" : 0x04, "NightInstant" : 0x14 
}

pmDetailedArmMode_t = (
   "Disarmed", "ExitDelay_ArmHome", "ExitDelay_ArmAway", "EntryDelay", "Stay", "Armed", "UserTest", "Downloading", "Programming", "Installer",
   "Home Bypass", "Away Bypass", "Ready", "NotReady", "??", "??", "Disarm", "ExitDelay", "ExitDelay", "EntryDelay", "StayInstant", "ArmedInstant"
) # Not used: Night, NightInstant, Vacation

pmEventType_t = { 
   "EN" : (
           "None", "Tamper Alarm", "Tamper Restore", "Open", "Closed", "Violated (Motion)", "Panic Alarm", "RF Jamming",
           "Tamper Open", "Communication Failure", "Line Failure", "Fuse", "Not Active", "Low Battery", "AC Failure", 
           "Fire Alarm", "Emergency", "Siren Tamper", "Siren Tamper Restore", "Siren Low Battery", "Siren AC Fail" ),
   "NL" : (
           "Geen", "Sabotage alarm", "Sabotage herstel", "Open", "Gesloten", "Verstoord (beweging)", "Paniek alarm", "RF verstoring",
           "Sabotage open", "Communicatie probleem", "Lijnfout", "Zekering", "Niet actief", "Lage batterij", "AC probleem",
           "Brandalarm", "Noodoproep", "Sirene sabotage", "Sirene sabotage herstel", "Sirene lage batterij", "Sirene AC probleem" )
}

pmPanelAlarmType_t = {
   0x01 : "Intruder",  0x02 : "Intruder", 0x03 : "Intruder", 0x04 : "Intruder", 0x05 : "Intruder", 0x06 : "Tamper", 
   0x07 : "Tamper",    0x08 : "Tamper",   0x09 : "Tamper",   0x0B : "Panic",    0x0C : "Panic",    0x20 : "Fire", 
   0x23 : "Emergency", 0x49 : "Gas",      0x4D : "Flood"
}

pmPanelTroubleType_t = {
   0x0A  : "Communication", 0x0F  : "General",   0x29  : "Battery", 0x2B: "Power",   0x2D : "Battery", 0x2F : "Jamming", 
   0x31  : "Communication", 0x33  : "Telephone", 0x36  : "Power",   0x38 : "Battery", 0x3B : "Battery", 0x3C : "Battery",
   0x40  : "Battery",       0x43  : "Battery"
}

pmPanelType_t = {
   0 : "PowerMax", 1 : "PowerMax+", 2 : "PowerMax Pro", 3 : "PowerMax Complete", 4 : "PowerMax Pro Part",
   5  : "PowerMax Complete Part", 6 : "PowerMax Express", 7 : "PowerMaster10",   8 : "PowerMaster30"
}

# Config for each panel type (1-9)
pmPanelConfig_t = {
   "CFG_PARTITIONS"  : (   1,   1,   1,   1,   3,   3,   1,   3,   3 ),
   "CFG_EVENTS"      : ( 250, 250, 250, 250, 250, 250, 250, 250,1000 ),
   "CFG_KEYFOBS"     : (   8,   8,   8,   8,   8,   8,   8,   8,  32 ),
   "CFG_1WKEYPADS"   : (   8,   8,   8,   8,   8,   8,   8,   0,   0 ),
   "CFG_2WKEYPADS"   : (   2,   2,   2,   2,   2,   2,   2,   8,  32 ),
   "CFG_SIRENS"      : (   2,   2,   2,   2,   2,   2,   2,   4,   8 ),
   "CFG_USERCODES"   : (   8,   8,   8,   8,   8,   8,   8,   8,  48 ),
   "CFG_PROXTAGS"    : (   0,   0,   8,   0,   8,   8,   0,   8,  32 ),
   "CFG_WIRELESS"    : (  28,  28,  28,  28,  28,  28,  28,  29,  62 ), # 30, 64
   "CFG_WIRED"       : (   2,   2,   2,   2,   2,   2,   1,   1,   2 ),
   "CFG_ZONECUSTOM"  : (   0,   5,   5,   5,   5,   5,   5,   5,   5 )
}

pmPanelName_t = {
   "0000" : "PowerMax", "0001" : "PowerMax LT", "0004" : "PowerMax A", "0005" : "PowerMax", "0006" : "PowerMax LT",
   "0009" : "PowerMax B", "000a" : "PowerMax A", "000b" : "PowerMax", "000c" : "PowerMax LT", "000f" : "PowerMax B",
   "0014" : "PowerMax A", "0015" : "PowerMax", "0016" : "PowerMax", "0017" : "PowerArt", "0018" : "PowerMax SC",
   "0019" : "PowerMax SK", "001a" : "PowerMax SV", "001b" : "PowerMax T", "001e" : "PowerMax WSS", "001f" : "PowerMax Smith",
   "0100" : "PowerMax+", "0103" : "PowerMax+ UK (3)", "0104" : "PowerMax+ JP", "0106" : "PowerMax+ CTA", "0108" : "PowerMax+",
   "010a" : "PowerMax+ SH", "010b" : "PowerMax+ CF", "0112" : "PowerMax+ WSS", "0113" : "PowerMax+ 2INST", 
   "0114" : "PowerMax+ HL", "0115" : "PowerMax+ UK", "0116" : "PowerMax+ 2INST3", "0118" : "PowerMax+ CF", 
   "0119" : "PowerMax+ 2INST", "011a" : "PowerMax+", "011c" : "PowerMax+ WSS", "011d" : "PowerMax+ UK", 
   "0120" : "PowerMax+ 2INST33", "0121" : "PowerMax+", "0122" : "PowerMax+ CF", "0124" : "PowerMax+ UK",
   "0127" : "PowerMax+ 2INST_MONITOR", "0128" : "PowerMax+ KeyOnOff", "0129" : "PowerMax+ 2INST_MONITOR",
   "012a" : "PowerMax+ 2INST_MONITOR42", "012b" : "PowerMax+ 2INST33", "012c" : "PowerMax+ One Inst_1_44_0",
   "012d" : "PowerMax+ CF_1_45_0", "012e" : "PowerMax+ SA_1_46", "012f" : "PowerMax+ UK_1_47", "0130" : "PowerMax+ SA UK_1_48",
   "0132" : "PowerMax+ KeyOnOff 1_50", "0201" : "PowerMax Pro", "0202" : "PowerMax Pro-Nuon ", 
   "0204" : "PowerMax Pro-PortugalTelecom", "020a" : "PowerMax Pro-PortugalTelecom2", "020c" : "PowerMax HW-V9 Pro",
   "020d" : "PowerMax ProSms", "0214" : "PowerMax Pro-PortugalTelecom_4_5_02", "0216" : "PowerMax HW-V9_4_5_02 Pro",
   "0217" : "PowerMax ProSms_4_5_02", "0218" : "PowerMax UK_DD243_4_5_02 Pro M", "021b" : "PowerMax Pro-Part2__2_27",
   "0223" : "PowerMax Pro Bell-Canada", "0301" : "PowerMax Complete", "0302" : "PowerMax Complete_NV",
   "0303" : "PowerMax Complete-PortugalTelecom", "0307" : "PowerMax Complete_1_0_07", "0308" : "PowerMax Complete_NV_1_0_07",
   "030a" : "PowerMax Complete_UK_DD243_1_1_03", "030b" : "PowerMax Complete_COUNTERFORCE_1_0_06", "0401" : "PowerMax Pro-Part",
   "0402" : "PowerMax Pro-Part CellAdaptor", "0405" : "PowerMax Pro-Part_5_0_08", "0406" : "PowerMax Pro-Part CellAdaptor_5_2_04",
   "0407" : "PowerMax Pro-Part KeyOnOff_5_0_08", "0408" : "PowerMax UK Pro-Part_5_0_08", 
   "0409" : "PowerMax SectorUK Pro-Part_5_0_08", "040a" : "PowerMax Pro-Part CP1 4_10", "040c" : "PowerMax Pro-Part_Cell_key_4_12",
   "040d" : "PowerMax Pro-Part UK 4_13", "040e" : "PowerMax SectorUK Pro-Part_4_14", "040f" : "PowerMax Pro-Part UK 4_15",
   "0410" : "PowerMax Pro-Part CP1 4_16", "0411" : "PowerMax NUON key 4_17", "0433" : "PowerMax Pro-Part2__4_51",
   "0434" : "PowerMax UK Pro-Part2__4_52", "0436" : "PowerMax Pro-Part2__4_54", "0437" : "PowerMax Pro-Part2__4_55 (CP_01)",
   "0438" : "PowerMax Pro-Part2__4_56", "0439" : "PowerMax Pro-Part2__4_57 (NUON)", "043a" : "PowerMax Pro 4_58",
   "043c" : "PowerMax Pro 4_60", "043e" : "PowerMax Pro-Part2__4_62", "0440" : "PowerMax Pro-Part2__4_64",
   "0442" : "PowerMax 4_66", "0443" : "PowerMax Pro 4_67", "0444" : "PowerMax Pro 4_68", "0445" : "PowerMax Pro 4_69",
   "0446" : "PowerMax Pro-Part2__4_70", "0447" : "PowerMax 4_71", "0449" : "PowerMax 4_73", "044b" : "PowerMax Pro-Part2__4_75",
   "0451" : "PowerMax Pro 4_81", "0452" : "PowerMax Pro 4_82", "0454" : "PowerMax 4_84", "0455" : "PowerMax 4_85",
   "0456" : "PowerMax 4_86", "0503" : "PowerMax UK Complete partition 1_5_00", "050a" : "PowerMax Complete partition GPRS",
   "050b" : "PowerMax Complete partition NV GPRS", "050c" : "PowerMax Complete partition GPRS NO-BBA",
   "050d" : "PowerMax Complete partition NV GPRS NO-BBA", "050e" : "PowerMax Complete part. GPRS NO-BBA UK_5_14",
   "0511" : "PowerMax Pro-Part CP1 GPRS 5_17", "0512" : "PowerMax Complete part. BBA UK_5_18", 
   "0533" : "PowerMax Complete part2  5_51", "0534" : "PowerMax Complete part2 5_52 (UK)", 
   "0536" : "PowerMax Complete 5_54 (GR)", "0537" : "PowerMax Complete  5_55", "053a" : "PowerMax Complete 5_58 (PT)",
   "053b" : "PowerMax Complete part2 5_59 (NV)", "053c" : "PowerMax Complete  5_60", "053e" : "PowerMax Complete 5_62",
   "053f" : "PowerMax Complete part2  5_63", "0540" : "PowerMax Complete  5_64", "0541" : "PowerMax Complete  5_65",
   "0543" : "PowerMax Complete  5_67", "0544" : "PowerMax Complete  5_68", "0545" : "PowerMax Complete  5_69",
   "0546" : "PowerMax Complete  5_70", "0547" : "PowerMax Complete  5_71", "0549" : "PowerMax Complete  5_73",
   "054b" : "PowerMax Complete  5_75", "054f" : "PowerMax Complete  5_79", "0601" : "PowerMax Express",
   "0603" : "PowerMax Express CP 01", "0605" : "PowerMax Express OEM 6_5", "0607" : "PowerMax Express BBA 6_7",
   "0608" : "PowerMax Express CP 01 BBA 6_8", "0609" : "PowerMax Express OEM1 BBA 6_9", "060b" : "PowerMax Express BBA 6_11",
   "0633" : "PowerMax Express 6_51", "063b" : "PowerMax Express 6_59", "063d" : "PowerMax Express 6_61",
   "063e" : "PowerMax Express 6_62 (UK)", "0645" : "PowerMax Express 6_69", "0647" : "PowerMax Express 6_71",
   "0648" : "PowerMax Express 6_72", "0649" : "PowerMax Express 6_73", "064a" : "PowerMax Activa 6_74",
   "064c" : "PowerMax Express 6_76", "064d" : "PowerMax Express 6_77", "064e" : "PowerMax Express 6_78",
   "064f" : "PowerMax Secure 6_79", "0650" : "PowerMax Express 6_80", "0650" : "PowerMax Express part2 M 6_80",
   "0651" : "PowerMax Express 6_81", "0652" : "PowerMax Express 6_82", "0653" : "PowerMax Express 6_83",
   "0654" : "PowerMax 6_84", "0655" : "PowerMax 6_85", "0658" : "PowerMax 6_88", "0659" : "PowerMax 6_89",
   "065a" : "PowerMax 6_90", "065b" : "PowerMax 6_91", "0701" : "PowerMax PowerCode-G 7_1", "0702" : "PowerMax PowerCode-G 7_2",
   "0704" : "PowerMaster10 7_4", "0705" : "PowerMaster10 7_05", "0707" : "PowerMaster10 7_07", "070c" : "PowerMaster10 7_12",
   "070f" : "PowerMaster10 7_15", "0710" : "PowerMaster10 7_16", "0711" : "PowerMaster10 7_17", "0712" : "PowerMaster10 7_18",
   "0713" : "PowerMaster10 7_19", "0802" : "PowerMax Complete PowerCode-G 8_2", "0803" : "PowerMaster30 8_3",
   "080f" : "PowerMaster30 8_15", "0810" : "PowerMaster30 8_16", "0812" : "PowerMaster30 8_18", "0813" : "PowerMaster30 8_19",
   "0815" : "PowerMaster30 8_21"
}

pmZoneType_t = { 
   "EN" : (
           "Non-Alarm", "Emergency", "Flood", "Gas", "Delay 1", "Delay 2", "Interior-Follow", "Perimeter", "Perimeter-Follow", 
           "24 Hours Silent", "24 Hours Audible", "Fire", "Interior", "Home Delay", "Temperature", "Outdoor", "16" ),
   "NL" : (
           "Geen alarm", "Noodtoestand", "Water", "Gas", "Vertraagd 1", "Vertraagd 2", "Interieur volg", "Omtrek", "Omtrek volg", 
           "24 uurs stil", "24 uurs luid", "Brand", "Interieur", "Thuis vertraagd", "Temperatuur", "Buiten", "16" )
} # "Arming Key", "Guard" ??

# Zone names are taken from the panel, so no langauage support needed
pmZoneName_t = (
   "Attic", "Back door", "Basement", "Bathroom", "Bedroom", "Child room", "Closet", "Den", "Dining room", "Downstairs", 
   "Emergency", "Fire", "Front door", "Garage", "Garage door", "Guest room", "Hall", "Kitchen", "Laundry room", "Living room", 
   "Master bathroom", "Master bedroom", "Office", "Upstairs", "Utility room", "Yard", "Custom 1", "Custom 2", "Custom 3",
   "Custom4", "Custom 5", "Not Installed"
)

pmZoneChime_t = ("Chime Off", "Melody Chime", "Zone Name Chime")

# Note: names need to match to VAR_xxx
pmZoneSensor_t = {
   0x3 : "Motion", 0x4 : "Motion", 0x5 : "Magnet", 0x6 : "Magnet", 0x7 : "Magnet", 0xA : "Smoke", 0xB : "Gas", 0xC : "Motion", 0xF : "Wired"
} # unknown to date: Push Button, Flood, Universal

#pmZoneSensorMaster_t = {
#   0x01 : { name = "Next PG2", func = "Motion" }, [0x04 : { name = "Next CAM PG2", func = "Camera" },
#   0x16 : { name = "SMD-426 PG2", func = "Smoke" }, [0x1A : { name = "TMD-560 PG2", func = "Temperature" },
#   0x2A : { name = "MC-302 PG2", func = "Magnet"}, [0xFE : { name = "Wired", func = "Wired" }
#}

log = logging.getLogger(__name__)
level = logging.getLevelName('DEBUG')  # INFO, DEBUG
log.setLevel(level)
DownloadMode = False

def toString(array_alpha : bytearray):
    return "".join("%02x " % b for b in array_alpha)
    
class SensorDevice:
    def __init__(self, **kwargs):
        self.id = kwargs.get('id', None)
        self.name = kwargs.get('name', None)
        self.stype = kwargs.get('stype', None)
        self.sid = kwargs.get('sid', None)
        self.sname = kwargs.get('sname', None) 
        self.ztype = kwargs.get('ztype', None)
        self.zname = kwargs.get('zname', None)
        self.zchime = kwargs.get('zchime', None)
        self.partition = kwargs.get('partition', None)
        self.bypass = kwargs.get('bypass', None)
        self.lowbatt = kwargs.get('lowbatt', None)

class VisonicListEntry:
    def __init__(self, **kwargs):
        #self.message = kwargs.get('message', None)
        self.command = kwargs.get('command', None)
        #self.receive = kwargs.get('receive', None)
        #self.receivecount = kwargs.get('receivecount', None)
        #self.receivecountfixed = kwargs.get('receivecountfixed', None) # Need to store it extra, because with a re-List it can get lost
        #self.receiveretries = kwargs.get('receiveretries', None)
        self.options = kwargs.get('options', None)
    def __str__(self):
        return "Command:{0}    Options:{1}".format(self.command.msg, self.options)

  
class ProtocolBase(asyncio.Protocol):
    """Manage low level Visonic protocol."""

    transport = None  # type: asyncio.Transport
    #ReceiveLastPacketTime = None # datetime of when last ReceiveData packet is received
    #PowerLinkTimeoutTime = datetime.now()
    tListDelay = object
    pmVarLenMsg = False
    #ForceStandardMode = False
    pmIncomingPduLen = 0
    pmSendMsgRetries = 0
    
    pmLastTransactionTime = datetime.now() - timedelta(seconds=1)  ## take off 1 second so the first command goes through immediately
    pmCrcErrorCount = 0
    pmStarting = True    # INTERFACE used in interface to tell user we're starting up
    
    pmPowerMaster = False
    
    msgType_t = None
    
    pmLastSentMessage = None
    #pmSyncTimeCheck = None
    pmSyncTime = True   # INTERFACE   Sync time with the panel or not True/False
    
    keepalivecounter = 0    # only used in keep_alive_messages_timer

    pmExpectedResponse = []
    pmPowerlinkMode = False
    pmWaitingForAckFromPanel = False
    
    pmAutoCreate = True
    
    log.debug("Initialising Protocol")
    
    def __init__(self, loop=None, disconnect_callback=None) -> None:
        """Initialize class."""
        if loop:
            self.loop = loop
        else:
            self.loop = asyncio.get_event_loop()
        self.packet = bytearray()
        self.ReceiveData = bytearray()
        self.disconnect_callback = disconnect_callback
        self.SendList = []
        self.pmSensorDev_t = {} # id, name, stype, sid, sname, ztype, zname, zchime, partition, bypass, lowbatt
        self.pmRawSettings = {}
        self.pmSirenDev_t = {}
        self.lock = threading.Lock()
 
    def pmTimeFunction(self) -> datetime:
        return datetime.now()
        
    def PowerLinkTimeoutExpired(self):
        """We timed out, try to restore the connection."""
        log.debug("[PowerLinkTimeout] Powerlink Timer Expired")
        self.pmWaitingForAckFromPanel = False
        self.pmExpectedResponse = []    
        # restart the timer
        self.reset_powerlink_timeout()
        self.SendCommand("MSG_RESTORE")

    def reset_powerlink_timeout(self):
        global PowerLinkTimeout
        # log.debug("[PowerLinkTimeout] Resetting Powerlink Timeout")
        try:
            self.tPowerLinkKeepAlive.cancel()
        except:
            pass
        self.tPowerLinkKeepAlive = threading.Timer(PowerLinkTimeout, self.PowerLinkTimeoutExpired)
        self.tPowerLinkKeepAlive.start()

    def keep_alive_messages_timer(self):
        global DownloadMode
        self.lock.acquire()
        #if not DownloadMode:
        self.keepalivecounter = self.keepalivecounter + 1
        if not DownloadMode and len(self.SendList) == 0 and self.keepalivecounter >= 200:   #  
            #log.debug("Send list is empty so sending I'm alive message")
            self.lock.release()
            self.keepalivecounter = 0
            self.SendCommand("MSG_ALIVE")
            # self.SendCommand("MSG_STATUS")
        else:
            #log.debug("DownloadMode set to {0}     keepalivecounter {1}      len(self.SendList) {2}".format(DownloadMode, self.keepalivecounter, len(self.SendList)))
            self.lock.release()
            self.SendCommand(None)  ## check send queue
        # restart the timer
        self.reset_keep_alive_messages()

    def reset_keep_alive_messages(self):
        try:
            self.tKeepAlive.cancel()
        except:
            pass
        # 30 seconds
        self.tKeepAlive = threading.Timer(0.1, self.keep_alive_messages_timer)
        self.tKeepAlive.start()

    def connection_made(self, transport):
        """Make the protocol connection to the Panel."""
        self.transport = transport
        log.info('[connection_made] Connected')
        
        # INTERFACE: Get some settings from the user in HA
        # globals
        pmLang = "EN"                      # INTERFACE : Get the plugin language from HA, either "EN" or "NL"
        pmMotionOffDelay = 3               # INTERFACE : Get the motion sensor off delay time (between subsequent triggers)
        # local to class
        self.ForceStandardMode = False     # INTERFACE : Get user variable from HA to force standard mode or try for PowerLink
        self.pmRemoteArm = True            # INTERFACE : Does the user allow remote setting of the alarm
        self.pmSensorArm = "auto"          # INTERFACE : Does the user allow sensor arming, "auto" "bypass" or "live"
        
        # exit anything ongoing
        self.SendCommand("MSG_EXIT")
        sleep(1.0)
        #self.ReceiveLastPacketTime = self.pmTimeFunction()
        self.SendCommand("MSG_INIT")

        # Define powerlink seconds timer and start it for PowerLink communication
        self.reset_powerlink_timeout()

        # Send the download command, this should initiate the communication
        # Only skip it, if we force standard mode
        if not self.ForceStandardMode:
            log.info("[connection_made] Sending Download Start")
            self.Start_Download()
        else:
            self.ProcessSettings()
            # INTERFACE: set "PowerlinkMode" to "Standard" mode in HA interface
            
    def data_received(self, data): 
       """Add incoming data to ReceiveData."""
       #log.debug('[data receiver] received data: %s', toString(data))
       for databyte in data:
           #log.debug("[data receiver] Processing " + hex(databyte).upper())
           self.handle_received_byte(databyte)
           
    #pmIncomingPduLen is only used in this function
    def handle_received_byte(self, data):
        """Process a single byte as incoming data."""
        pduLen = len(self.ReceiveData)

        # If we were expecting a message of a particular length and what we have is already greater then that length then dump the message and resynchronise.
        if self.pmIncomingPduLen > 0 and pduLen >= self.pmIncomingPduLen:   # waiting for pmIncomingPduLen bytes but got more and haven't been able to validate a PDU
            log.debug("[data receiver] Building PDU: Dumping Current PDU " + toString(self.ReceiveData))
            self.ReceiveData = bytearray(b'')
            pduLen = len(self.ReceiveData)
            
        if pduLen == 4 and self.pmVarLenMsg:
            # Determine length of variable size message
            msgType = self.ReceiveData[1]
            self.pmIncomingPduLen = 7 + int(data) # (((int(self.ReceiveData[3]) * 0x100) + int(self.ReceiveData[2])))
            #if self.pmIncomingPduLen >= 0xB0:  # then more than one message
            #    self.pmIncomingPduLen = 0 # set it to 0 for this loop around
            #log.info("[data receiver] Variable length Message Being Receive  Message {0}     pmIncomingPduLen {1}".format(hex(msgType).upper(), self.pmIncomingPduLen))
   
        if pduLen == 0:
            self.msgType_t = None
            if data == 0x0D: # preamble
                #response = ""
                #for i in range(1, len(self.pmExpectedResponse)):
                #    response = response + string.format(" %02X", string.byte(self.pmExpectedResponse, i))
                #if (response != ""):
                #    response = "; Expecting" + response
                #log.debug("[data receiver] Start of new PDU detected, expecting response " + response)
                #log.debug("[data receiver] Start of new PDU detected")
                self.ReceiveData = bytearray(b'')
                self.ReceiveData.append(data)
                #log.debug("[data receiver] Starting PDU " + toString(self.ReceiveData))
        elif pduLen == 1:
            msgType = data
            #log.debug("[data receiver] Received message Type %d", data)
            if msgType in pmReceiveMsg_t:
                self.msgType_t = pmReceiveMsg_t[msgType]
                self.pmIncomingPduLen = (self.msgType_t != None) and self.msgType_t.length or 0
                if msgType == 0x3F: # and self.msgType_t != None and self.pmIncomingPduLen == 0:
                    self.pmVarLenMsg = True
            else:
                log.error("[data receiver] ERROR : Construction of incoming packet unknown - Message Type {0}".format(hex(msgType).upper()))
            #log.debug("[data receiver] Building PDU: It's a message %02X; pmIncomingPduLen = %d", data, self.pmIncomingPduLen)
            self.ReceiveData.append(data)
        elif (self.pmIncomingPduLen == 0 and data == 0x0A) or (pduLen + 1 == self.pmIncomingPduLen): # postamble   (standard length message and data terminator) OR (actual length == calculated length)
            self.ReceiveData.append(data)
            #log.debug("[data receiver] Building PDU: Checking it " + toString(self.ReceiveData))
            msgType = self.ReceiveData[1]
            if (data != 0x0A) and (self.ReceiveData[pduLen] == 0x43):
                log.info("[data receiver] Building PDU: Special Case 42 ********************************************************************************************")
                self.pmIncomingPduLen = self.pmIncomingPduLen + 1 # for 0x43
            elif self.validatePDU(self.ReceiveData):
                #log.info("[data receiver] Building PDU: Got Validated PDU type 0x%02x   full data %s", int(msgType), toString(self.ReceiveData))
                self.pmVarLenMsg = False
                self.pmLastPDU = self.ReceiveData
                self.pmLogPdu(self.ReceiveData, "<-PM  ")
                pmLastTransactionTime = self.pmTimeFunction()  # get time now to track how long it takes for a reply
         
                if (self.msgType_t == None):
                    log.info("[data receiver] Unhandled message {0}".format(hex(msgType)))
                    self.pmSendAck()
                else:
                    # Send an ACK if needed
                    if self.msgType_t.ackneeded:
                        #log.debug("[data receiver] Sending an ack as needed by last panel status message " + hex(msgType).upper())
                        self.pmSendAck()
                    # Handle the message
                    #log.debug("[data receiver] Received message " + hex(msgType).upper())
                    self.handle_packet(self.ReceiveData)
                    # Check response
                    if (len(self.pmExpectedResponse) > 0 and msgType != 2):   ## 2 is a simple acknowledge from the panel so ignore those
                        # We've sent something and are waiting for a reponse - this is it
                        #log.debug("[data receiver] msgType {0}  expected one of {1}".format(hex(msgType).upper(), [hex(no).upper() for no in self.pmExpectedResponse]))
                        if (msgType in self.pmExpectedResponse):
                            self.pmExpectedResponse.remove(msgType)
                            log.debug("[data receiver] msgType {0} got it so removed from list, list is now {1}".format(hex(msgType).upper(), [hex(no).upper() for no in self.pmExpectedResponse]))
                            #while msgType in self.pmExpectedResponse:
                            #    log.debug("[data receiver] Got one of the needed response messages from the Panel")
                            #    self.pmExpectedResponse.remove(msgType)
                            self.pmSendMsgRetries = 0
                        else:
                            log.debug("[data receiver] msgType not in self.pmExpectedResponse   Waiting for next PDU :  expected {0}   got {1}".format([hex(no).upper() for no in self.pmExpectedResponse], hex(msgType).upper()))
                            #if ignore: # ignore unexpected messages
                            #    log.debug("[data receiver] ignoring expected response")
                            if self.pmSendMsgRetries == 0:
                                self.pmSendMsgRetries = 1 # don't resend immediately - first wait for the next response
                            elif (self.pmSendMsgRetries < MSG_RETRIES):
                                self.pmSendMsgRetries = self.pmSendMsgRetries + 1
                self.ReceiveData = bytearray(b'')
            else: # CRC check failed. However, it could be an 0x0A in the middle of the data packet and not the terminator of the message
                if len(self.ReceiveData) > 0xB0:
                    log.info("[data receiver] PDU with CRC error %s", toString(self.ReceiveData))
                    self.pmLogPdu(self.ReceiveData, "<-PM   PDU with CRC error")
                    pmLastTransactionTime = self.pmTimeFunction() - timedelta(seconds=1)
                    self.ReceiveData = bytearray(b'')
                    if msgType != 0xF1:        # ignore CRC errors on F1 message
                        self.pmCrcErrorCount = self.pmCrcErrorCount + 1
                    if (self.pmCrcErrorCount > MAX_CRC_ERROR):
                        self.pmHandleCommException("CRC errors")
                else:
                    a = self.calculate_crc(self.ReceiveData[1:-2])[0]
                    log.info("[data receiver] Building PDU: Length is now %d bytes (apparently PDU not complete)    %s    checksum calcs %02x", len(self.ReceiveData), toString(self.ReceiveData), a)
        elif pduLen <= 0xC0:
            #log.debug("[data receiver] Current PDU " + toString(self.ReceiveData) + "    adding " + str(hex(data).upper()))
            self.ReceiveData.append(data)
        else:
            log.info("[data receiver] Dumping Current PDU " + toString(self.ReceiveData))
            self.ReceiveData = bytearray(b'') # messages should never be longer than 0xC0
        #log.debug("[data receiver] Building PDU " + toString(self.ReceiveData))
    
    # PDUs can be logged in a file. We use this to discover new never before seen
    #      PowerMax messages we need to decode and make sense of in long evenings...
    def pmLogPdu(self, PDU, message):
        #log.debug("Logging PDU " + message + "   :    " + toString(PDU))
        a = 0
#        if pmLogDebug:
#            logfile = pmLogFilename
#            outf = io.open(logfile , 'a')
#            if outf == None:
#                log.debug("Cannot write to debug file.")
#                return
#        filesize = outf:seek("end")
#        outf:close()
#        # empty file if it reaches 500 kb
#        if filesize > 500 * 1024:
#            outf = io.open(logfile , 'w')
#            outf:write('')
#            outf:close()
#        outf = io.open(logfile, 'a')
#        now = self.pmTimeFunction()
#        outf:write(string.format("%s%s %s %s\n", os.date("%F %X", now), string.gsub(string.format("%.3f", now), "([^%.]*)", "", 1), direction, PDU))
#        outf:close()

    def pmSendAck(self):
        """ Send ACK if packet is valid """
        lastType = self.pmLastPDU[1]
        normalMode = (lastType >= 0x80) or ((lastType < 0x10) and (self.pmLastPDU[len(self.pmLastPDU) - 2] == 0x43))
   
        self.lock.acquire()
        #log.debug("[sending ack] Sending an ack back to Alarm powerlink = {0}".format(self.pmPowerlinkMode))
        if self.pmPowerlinkMode: #normalMode:
            self.transport.write(bytearray.fromhex('0D 02 43 BA 0A'))
        else:
            self.transport.write(bytearray.fromhex('0D 02 FD 0A'))
        self.lock.release()
    
    def validatePDU(self, packet : bytearray) -> bool:
        """Verify if packet is valid.
            >>> Packets start with a preamble (\x0D) and end with postamble (\x0A)
        """
        #packet = self.ReceiveData
        
        #log.debug("[validatePDU] Calculating for: %s", toString(packet))
        
        if packet[:1] != b'\x0D':
            return False
        if packet[-1:] != b'\x0A':
            return False
        if packet[-2:-1] == self.calculate_crc(packet[1:-2]):
            #log.debug("[validatePDU] VALID PACKET!")
            return True

        log.debug("[validatePDU] Not valid packet, CRC failed, may be ongoing and not final 0A")
        return False

    def calculate_crc(self, msg: bytearray):
        """ Calculate CRC Checksum """
        #log.debug("[calculate_crc] Calculating for: %s", toString(msg))
        checksum = 0
        for char in msg[0:len(msg)]:
            checksum += char
        checksum = 0xFF - (checksum % 0xFF)
        if checksum == 0xFF:
            checksum = 0x00
#            log.debug("[calculate_crc] Checksum was 0xFF, forsing to 0x00")
        #log.info("[calculate_crc] Calculating for: %s     calculated CRC is: %s", toString(msg), toString(bytearray([checksum])))
        return bytearray([checksum])

    # this does not have a lock as the lock is in SendCommand and this is only called from within that function
    def pmSendPdu(self, instruction : VisonicListEntry):
        """Encode and put packet string onto write buffer."""
        
        command = instruction.command
        data = command.data            
        # push in the options
        if instruction.options != None:
            if len(instruction.options) > 0:
                s = instruction.options[0]
                a = instruction.options[1]
                #log.debug("[pmSendPdu] Options {0} {1} {2} {3} {4} {5}".format(instruction.options, type(s), type(a), s, a, len(a)))
                for i in range(0, len(a)):
                    data[s + i] = a[i]
                    #log.debug("[pmSendPdu]        Inserting at {0}".format(s+i))
        #log.debug('[pmSendPdu]                 Raw data: %s', toString(data))
        
        #log.debug('[pmSendPdu] input data: %s', toString(packet))
        # First add preamble (0x0D), then the packet, then crc and postamble (0x0A)
        sData = b'\x0D'
        sData += data
        sData += self.calculate_crc(data)
        sData += b'\x0A'

        # Log some usefull information in debug mode
        log.info("[pmSendPdu] Sending Command ({0})  at time {1}".format(command.msg, self.pmTimeFunction()))
        #log.debug('[pmSendPdu]                 Writing data: %s', toString(sData))
        self.pmLogPdu(sData, "  PM->")
        self.transport.write(sData)

    def SendCommand(self, message_type, **kwargs):
        """ Add a command to the send List 
            The List is needed to prevent sending messages too quickly normally it requires 500msec between messages """
        self.lock.acquire()
        interval = self.pmTimeFunction() - self.pmLastTransactionTime
        timeout = (interval > TIMEOUT)

        # command may be set to None on entry
        # Always add the command to the list
        if message_type != None:
            message = pmSendMsg[message_type]
            assert(message != None)
            options = kwargs.get('options', None)
            # need to test if theres an im alive message already in the queue, if not then add it
            self.SendList.append(VisonicListEntry(command = message, options = options))
            #log.debug("[QueueMessage] Queueing %s" % (VisonicListEntry(command = message, options = options)))
        
        # This will send commands from the list, oldest first        
        if len(self.SendList) > 0:
            #log.debug("[SendMessage] Start    interval {0}    timeout {1}     pmExpectedResponse {2}     pmWaitingForAckFromPanel {3}".format(interval, timeout, self.pmExpectedResponse, self.pmWaitingForAckFromPanel))
            if not self.pmWaitingForAckFromPanel and interval != None and len(self.pmExpectedResponse) == 0: # and not timeout: # we are ready to send    
                # check if the last command was sent at least 500 ms ago
                td = timedelta(milliseconds=500)
                ok_to_send = (interval > td) # pmMsgTiming_t[pmTiming].wait)
                #log.debug("[SendMessage] ok_to_send {0}    {1}  {2}".format(ok_to_send, interval, td))
                if ok_to_send: 
                    # pop the oldest item from the list, this could be the only item.
                    instruction = self.SendList.pop(0)
                    command = instruction.command
                    self.pmWaitingForAckFromPanel = command.waitforack
                    self.reset_keep_alive_messages()   ## no need to send i'm alive message for a while as we're about to send a command anyway
                    self.pmLastTransactionTime = self.pmTimeFunction()
                    self.pmLastSentMessage = instruction       
                    if command.replytype != None: # and command.replytype not in self.pmExpectedResponse:
                        # This message has a specific response associated with it, add it to the list of responses if not already there
                        self.pmExpectedResponse.append(command.replytype)
                        log.debug("[SendMessage]      waiting for message response " + hex(command.replytype).upper())
                    self.pmSendPdu(instruction)
        self.lock.release()
                                                
    def ClearList(self):
        self.lock.acquire()
        """ Clear the List, preventing any retry causing issue. """

        # Clear the List
        log.debug("[ClearList] Setting queue empty")
        self.SendList = []
        #while len(self.SendList) > 0:
        #    try:
        #        self.SendList.get(False)
        #    except Empty:
        #        continue
        #    self.SendList.task_done()
        self.pmLastSentMessage = None
        self.lock.release()
    
    def connection_lost(self, exc):
        """Log when connection is closed, if needed call callback."""
        if exc:
            log.exception('ERROR Connection Lost : disconnected due to exception')
        else:
            log.info('ERROR Connection Lost : disconnected because of close/abort.')
        if self.disconnect_callback:
            self.disconnect_callback(exc)

    def Start_Download(self):
        global DownloadMode
        """ Start download mode """
        if not DownloadMode:
            self.pmWaitingForAckFromPanel = False
            log.info("[Start_Download] Starting download mode")
            self.SendCommand("MSG_DOWNLOAD", options = [3, DownloadCode]) # 
            DownloadMode = True
        else:
            log.debug("[Start_Download] Already in Download Mode (so not doing anything)")
            
    def pmCreateDevice(self, s, stype, zoneName="", zoneTypeName=""):
        log.debug("[Create Device] Create device {0} ref {1}".format(stype, s))   # INTERFACE
        return s
        
    # Find child device (Taken from GE Caddx Panel but comes originally from guessed)
    def findChild(self, deviceId, label):
        #for k, v in pairs(luup.devices) do
        #    if (v.device_num_parent == deviceId and v.id == label) then
        #        return k
        return "";
        
    # pmHandleCommException: we have run into a communication error
    def pmHandleCommException(self, what):
        #outf = open(pmCrashFilename , 'a')
        #if (outf == None):
        #    log.debug("ERROR Exception : Cannot write to crash file.")
        #    return
        when = self.pmTimeFunction()
        #outf:write(string.format("Exception %s occurred at %s\n", what, when))
        exceptions = 0 # get from backed up non volatile storage
        exceptions = exceptions + 1
        #outf:write("Exception count is now " + exceptions + "\n")
        log.warning("*** Houston - we have a communication problem (" + what + ")! Executing a reload. ***")
        self.pmExpectedResponse = []
        self.pmSendMsgRetries = 0
        # initiate reload initialisation
        self.SendCommand("MSG_INIT")

    def setDateTime(self, year, month, day, hour, minutes, seconds):
        buff = bytearray(0x46,0xF8,0x00,seconds,minutes,hour,day,month,year,0xFF,0xFF)
        return queueCommand(buff, sizeof(buff), "SET_DATE_TIME", 0xA0);
        
    # pmPowerlinkEnrolled
    def pmPowerlinkEnrolled(self):
        # TODO: update DL messages (length) based on panel type
        log.info("[Enrolling Powerlink] Reading panel settings")
        # set displayed state in HA to "DOWNLOAD"
        self.SendCommand("MSG_DL", options = [1, pmDownloadItem_t["MSG_DL_PANELFW"]] )     # Request the panel FW
        self.SendCommand("MSG_DL", options = [1, pmDownloadItem_t["MSG_DL_SERIAL"]] )      # Request serial & type (not always sent by default)
        self.SendCommand("MSG_DL", options = [1, pmDownloadItem_t["MSG_DL_ZONESTR"]] )     # Read the names of the zones
        if self.pmSyncTime:  # should we sync time between the HA and the Alarm Panel
            t = datetime.now()
            if t.year > 2000:
                year = t.year - 2000
                values = [t.second, t.minute, t.hour, t.day, t.month, year]
                timePdu = bytearray(values)
                #self.pmSyncTimeCheck = t
                self.SendCommand("MSG_SETTIME", options = [3, timePdu] )
            else:
                log.debug("[Enrolling Powerlink] Please correct your local time.")
        if self.pmPowerMaster:
            self.SendCommand("MSG_DL", options = [1, pmDownloadItem_t["MSG_DL_MR_SIRKEYZON"]] )
        self.SendCommand("MSG_START")      # Start sending all relevant settings please
        self.SendCommand("MSG_EXIT")       # Exit download mode
        # set displayed state in HA to "POWERLINK"
        
            
class PacketHandling(ProtocolBase):
    """Handle decoding of Visonic packets."""
    
    pmSensorShowBypass = False
    
    def __init__(self, *args, packet_callback: Callable = None,
                 **kwargs) -> None:
        """Add packethandling specific initialization.

        packet_callback: called with every complete/valid packet
        received.
        """
        super().__init__(*args, **kwargs)
        if packet_callback:
            self.packet_callback = packet_callback
        self.pmPhoneNr_t = {}
        self.pmEventLogDictionary = {}

    # pmWriteSettings: add a certain setting to the settings table
    #  So, to explain
    #      When we send a MSG_DL and insert the 4 bytes from pmDownloadItem_t, what we're doing is setting the page, index and len
    def pmWriteSettings(self, page, index, setting):
        settings_len = len(setting)
        wrap = (index + settings_len - 0x100)
        sett = []
        sett.append(bytearray(b''))
        sett.append(bytearray(b''))

        if settings_len > 0xB1:
            log.debug("[Write Settings] Write Settings too long *****************")
            return
        if wrap > 0:
            sett[0] = setting[ : settings_len - wrap - 1]
            sett[1] = setting[settings_len - wrap : ]
            wrap = 1
        else:
            sett[0] = setting
            wrap = 0
        
        for i in range(0, wrap+1):
            if (page + i) not in self.pmRawSettings:
                self.pmRawSettings[page + i] = bytearray()
                for v in range(0, 256):
                    self.pmRawSettings[page + i].append(255)
                if len(self.pmRawSettings[page + i]) != 256:
                    log.debug("[Write Settings] AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
                #else:
                #    log.debug("[Write Settings] WHOOOPEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEE")

            settings_len = len(sett[i])
            if i == 1:
                index = 0 
            #log.debug("[Write Settings] Writing settings page {0}  index {1}    setting {2}".format(page+i, index, toString(sett[i])))
            self.pmRawSettings[page + i] = self.pmRawSettings[page + i][0:index] + sett[i] + self.pmRawSettings[page + i][index + settings_len :]
            if len(self.pmRawSettings[page + i]) != 256:
                log.debug("[Write Settings] OOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOOO len = {0}".format(len(self.pmRawSettings[page + i])))
            #else:
            #    log.debug("[Write Settings] Page {0} is now {1}".format(page+i, toString(self.pmRawSettings[page + i])))
            
    # pmReadSettings
    def pmReadSettingsA(self, page, index, settings_len):
        retlen = settings_len
        retval = bytearray()
        while page in self.pmRawSettings and retlen > 0:
            fred = self.pmRawSettings[page][index : index + retlen]
            retval = retval + fred
            page = page + 1
            retlen = retlen - len(fred)
            index = 0
        if settings_len == len(retval):
            #log.debug("[Read Settings]  len " + str(settings_len) + " returning " + toString(retval))
            return retval
        # return a bytearray filled with 0xFF values
        retval = bytearray()
        for i in range(0, settings_len):
            retval.append(255)
        return retval

    # this can be called from an entry in pmDownloadItem_t such as
    #       for example "MSG_DL_PANELFW"      : bytearray.fromhex('00 04 20 00'),
    #            this defines the index, page and 2 bytes for length
    #            in this example page 4   index 0   length 32
    def pmReadSettings(self, item):
        return self.pmReadSettingsA(item[1], item[0], item[2] + (0x100 * item[3]))
        
    def dump_settings(self):
        log.debug("Dumping EPROM Settings")
#        if pmLogDebug:
#            dumpfile = pmSettingsFilename
#            outf = open(dumpfile , 'w')
#            if outf == None:
#                log.debug("Cannot write to debug file.")
#                return
#            log.debug("Dumping PowerMax settings to file")
#            outf.write("PowerMax settings on %s\n", os.date('%Y-%m-%d %H:%M:%S'))
#            for i in range(0, 0xFF):
#                if pmRawSettings[i] != None:
#                    for j in range(0, 0x0F):
#                        s = ""
#                        outf.write(string.format("%08X: ", i * 0x100 + j * 0x10))
#                        for k in range (1, 0x10):
#                            byte = string.byte(pmRawSettings[i], j * 0x10 + k)
#                            outf.write(string.format(" %02X", byte))
#                            s = (byte < 0x20 or (byte >= 0x80 and byte < 0xA0)) and (s + ".") or (s + string.char(byte))
#                        outf.write("  " + s + "\n")
#            outf.close()

    def calcBool(self, val, mask):
        return True if val & mask != 0 else False
    
    # ProcessSettings
    def ProcessSettings(self):
        log.info("[Process Settings] Process Settings Start")
        # Process settings
        x10_t = {}
        doorZoneStr = ""
        motionZoneStr = ""
        smokeZoneStr = ""
        deviceStr = ""
        pmPanelTypeNr = None
        panelSerialType = self.pmReadSettings(pmDownloadItem_t["MSG_DL_SERIAL"])
        if panelSerialType != None:
            pmPanelTypeNr = panelSerialType[7]
            if pmPanelTypeNr in pmPanelType_t:
                model = pmPanelType_t[pmPanelTypeNr]
            else:
                model = "UNKNOWN"   # INTERFACE : PanelType set to model
            self.dump_settings()

        if pmPanelTypeNr != None and pmPanelTypeNr > 0 and pmPanelTypeNr <= 8:
            #log.debug("[Process Settings] Panel Type Number " + str(pmPanelTypeNr) + "    serial string " + toString(panelSerialType))            
            zoneCnt = pmPanelConfig_t["CFG_WIRELESS"][pmPanelTypeNr] + pmPanelConfig_t["CFG_WIRED"][pmPanelTypeNr]
            customCnt = pmPanelConfig_t["CFG_ZONECUSTOM"][pmPanelTypeNr]
            userCnt = pmPanelConfig_t["CFG_USERCODES"][pmPanelTypeNr]
            partitionCnt = pmPanelConfig_t["CFG_PARTITIONS"][pmPanelTypeNr]
            sirenCnt = pmPanelConfig_t["CFG_SIRENS"][pmPanelTypeNr]
            keypad1wCnt = pmPanelConfig_t["CFG_1WKEYPADS"][pmPanelTypeNr]
            keypad2wCnt = pmPanelConfig_t["CFG_2WKEYPADS"][pmPanelTypeNr]
        
            if self.pmPowerlinkMode:
                # Check if time sync was OK
                #  if (pmSyncTimeCheck ~= nil) then
                #     setting = pmReadSettings(pmDownloadItem_t.MSG_DL_TIME)
                #     local timeRead = os.time({ day = string.byte(setting, 4), month = string.byte(setting, 5), year = string.byte(setting, 6) + 2000, 
                #        hour = string.byte(setting, 3), min = string.byte(setting, 2), sec = string.byte(setting, 1) })
                #     local timeSet = os.time(pmSyncTimeCheck)
                #     if (timeRead == timeSet) or (timeRead == timeSet + 1) then
                #        debug("Time sync OK (" .. os.date("%d/%m/%Y %H:%M:%S", timeRead) .. ")")
                #     else
                #        debug("Time sync FAILED (got " .. os.date("%d/%m/%Y %H:%M:%S", timeRead) .. "; expected " .. os.date("%d/%m/%Y %H:%M:%S", timeSet))
                #     end
                #  end
                
                log.debug("[Process Settings] Processing settings information")
                
                # Process zone names of this panel
                setting = self.pmReadSettings(pmDownloadItem_t["MSG_DL_ZONESTR"])
                log.debug("[Process Settings] Zone Type Names")
                for i in range(1, 27 + customCnt):
                    s = setting[(i - 1) * 0x10 + 1 : i * 0x10]
                    if s[0] != 0xFF:
                        #pmZoneName_t[i] = bytearray(s[0]) + s[2 : 0x0F]   # Why would we change a defined variable??
                        log.debug("[Process Settings]     Zone Type Names   {0}  name {1}".format(i, pmZoneName_t[i]))
                # INTERFACE : Add these zone names to the status panel
                
                # Process communication settings
                setting = self.pmReadSettings(pmDownloadItem_t["MSG_DL_PHONENRS"])
                log.debug("[Process Settings] Phone Numbers")
                for i in range(0, 4):
                    if i not in self.pmPhoneNr_t:
                        self.pmPhoneNr_t[i] = bytearray()
                    for j in range(0, 8):
                        #log.debug("[Process Settings] pos " + str((8 * i) + j))
                        nr = setting[(8 * i) + j]
                        if nr != None and nr != 0xFF:
                            #if j == 0:
                            #    self.pmPhoneNr_t[i] = bytearray()
                            #if self.pmPhoneNr_t[i] != None:
                            self.pmPhoneNr_t[i] = self.pmPhoneNr_t[i].append(nr)
                    if len(self.pmPhoneNr_t[i]) > 0:
                        log.debug("[Process Settings]      Phone nr " + str(i) + " = " + toString(self.pmPhoneNr_t[i]))
                        # INTERFACE : Add these phone numbers to the status panel
                
                # Process alarm settings
                setting = self.pmReadSettings(pmDownloadItem_t["MSG_DL_COMMDEF"])
                pmBellTime = setting[0x03]
                pmSilentPanic = self.calcBool(setting[0x19], 0x10)
                pmQuickArm = self.calcBool(setting[0x1A], 0x08)
                pmBypassOff = self.calcBool(setting[0x1B], 0xC0)
                pmForcedDisarmCode = setting[0x10 : 0x12]
                log.debug("[Process Settings] Alarm Settings pmBellTime {0} minutes     pmSilentPanic {1}   pmQuickArm {2}    pmBypassOff {3}  pmForcedDisarmCode {4}".format(pmBellTime, pmSilentPanic, pmQuickArm, pmBypassOff, pmForcedDisarmCode))
                if self.pmSensorArm == "auto":
                    self.pmSensorShowBypass = not pmBypassOff
                elif self.pmSensorArm == "bypass":
                    self.pmSensorShowBypass = True
                elif self.pmSensorArm == "live":
                    self.pmSensorShowBypass = False
                #  INTERFACE : Add these 6 params to the status panel

                # Process user pin codes
                if self.pmPowerMaster:
                    setting = self.pmReadSettings(pmDownloadItem_t["MSG_DL_MR_PINCODES"])
                else:
                    setting = self.pmReadSettings(pmDownloadItem_t["MSG_DL_PINCODES"])
                log.debug("[Process Settings] User Codes:")
                for i in range (0, userCnt):
                    code = setting[2 * i : 2 * i + 2]
                    log.debug("[Process Settings]      User {0} has code {1}".format(i, toString(code)))

                # Process software information
                setting = self.pmReadSettings(pmDownloadItem_t["MSG_DL_PANELFW"])
                panelEprom = setting[0x00 : 0x10]
                panelSoftware = setting[0x10 : 0x21]
                log.debug("[Process Settings] EPROM: {0}; SW: {1}".format(panelEprom.decode(), panelSoftware.decode()))
                #  INTERFACE : Add these 2 params to the status panel
                
                # Process panel type and serial
                idx = "{0:0>2}{1:0>2}".format(hex(panelSerialType[7]).upper()[2:], hex(panelSerialType[6]).upper()[2:])
                if idx in pmPanelName_t:
                    self.pmPanelName = pmPanelName_t[idx]
                else:
                    self.pmPanelName = "UNKNOWN"
                
                self.pmPanelSerial = ""
                for i in range(0, 6):
                    nr = panelSerialType[i]
                    if nr == 0xFF:
                        s = "." 
                    else:
                        s = "{0:0>2}".format(hex(nr).upper()[2:])
                    self.pmPanelSerial = self.pmPanelSerial + s
                log.debug("[Process Settings] Panel Name {0} with serial <{1}>".format(self.pmPanelName, self.pmPanelSerial))
                #  INTERFACE : Add these 2 params to the status panel

                # Store partition info & check if partitions are on
                partition = self.pmReadSettings(pmDownloadItem_t["MSG_DL_PARTITIONS"])
                if partition == None or partition[0] == 0:
                    partitionCnt = 1

                # Process zone settings
                zoneNames = bytearray()
                settingMr = bytearray()
                if not self.pmPowerMaster:
                    zoneNames = self.pmReadSettings(pmDownloadItem_t["MSG_DL_ZONENAMES"])
                else: # PowerMaster models
                    zoneNames = self.pmReadSettings(pmDownloadItem_t["MSG_DL_MR_ZONENAMES"])
                    settingMr = self.pmReadSettings(pmDownloadItem_t["MSG_DL_MR_ZONES"])
                  
                setting = self.pmReadSettings(pmDownloadItem_t["MSG_DL_ZONES"])
                
                #log.debug("[Process Settings] DL Zone settings " + toString(setting))
                
                if len(setting) > 0 and len(zoneNames) > 0:
                    log.debug("[Process Settings] Zones:")
                    for i in range(0, zoneCnt):
                        zoneName = pmZoneName_t[zoneNames[i]]
                        zoneEnrolled = False
                        if not self.pmPowerMaster: # PowerMax models
                            zoneEnrolled = setting[i * 4 : i * 4 + 3] != bytearray.fromhex("00 00 00")
                        else: # PowerMaster models (check only 5 of 10 bytes)
                            zoneEnrolled = settingMr[i * 10 : i * 10 + 5] != bytearray.fromhex("00 00 00 00 00")
                        if zoneEnrolled:
                            zoneInfo = 0
                            sensorId = 0
                            sensorType = 0
                            if not self.pmPowerMaster: #  PowerMax models
                                zoneInfo = int(setting[i * 4 + 3])
                                sensorId = int(setting[i * 4 + 2])
                                tmpid = sensorId & 0x0F
                                sensorType = "UNKNOWN " + str(tmpid)
                                if tmpid in pmZoneSensor_t:
                                    sensorType = pmZoneSensor_t[tmpid]
                            else: # PowerMaster models
                                zoneInfo = int(setting[i * 10])
                                sensorId = int(settingMr[i * 10 + 5])
                                if pmZoneSensorMaster_t[sensorId] == None:
                                    log.debug("[Process Settings] Unknown sensor ID found: " + sensorId)
                                    sensorType = "UNKNOWN"
                                else:
                                    sensorType = pmZoneSensorMaster_t[sensorId].func
                            zoneType = (zoneInfo & 0x0F)
                            zoneChime = ((zoneInfo >> 4) & 0x03)
                            part = {}
                            if partitionCnt > 1:
                                for j in range (1, partitionCnt):
                                    if partition[0x11 + i] & (2 ^ (j - 1)) > 0:
                                        log.debug("[Process Settings] doing table insert")
                                        #table.insert(part, j)
                            else:
                                part = { 1 }
                            log.debug("[Process Settings]       Zone {0}: {1} (chime = {2}; sensor type = {3} [{4}])".format(i+1, pmZoneType_t[pmLang][zoneType], pmZoneChime_t[zoneChime], sensorId, sensorType))
                            self.pmSensorDev_t[i] = SensorDevice(stype = sensorType, sid = sensorId, ztype = zoneType, ztypeName = pmZoneType_t[pmLang][zoneType], zname = zoneName, zchime = pmZoneChime_t[zoneChime], partition = part)
                            if sensorType == "Magnet" or sensorType == "Wired" or sensorType == "Temperature":
                                doorZoneStr = "{0},Z{1}".format(doorZoneStr, i)
                            elif sensorType == "Motion" or sensorType == "Camera":
                                motionZoneStr = "{0},Z{1}".format(motionZoneStr, i)
                            elif sensorType == "Smoke" or sensorType == "Gas":
                                smokeZoneStr = "{0},Z{1}".format(smokeZoneStr, i)
                        else:
                            #log.debug("[Process Settings]       Removing sensor {0} as it is not enrolled".format(i+1))
                            if i in self.pmSensorDev_t:
                                del self.pmSensorDev_t[i]
                                #self.pmSensorDev_t[i] = None # remove zone if needed
                #  INTERFACE : Add these self.pmSensorDev_t[i] params to the status panel

                # Process PGM/X10 settings
                setting = self.pmReadSettings(pmDownloadItem_t["MSG_DL_PGMX10"])
                x10Names = self.pmReadSettings(pmDownloadItem_t["MSG_DL_X10NAMES"])
                for i in range (0, 16):
                    enabled = False
                    x10Name = 0x1F
                    for j in range (0, 9):
                        enabled = enabled or setting[5 + i + j * 0x10] != 0
                    if (i > 0):
                        x10Name = x10Names[i]
                        x10_t[i] = pmZoneName_t[x10Name]
                    if enabled or x10Name != 0x1F:
                        if i == 0:
                            deviceStr = deviceStr + ",PGM"
                        else:
                            deviceStr = "{0},X{1}".format(deviceStr, i)
                            #if (string.find(devices, string.format("X%02dd", i)) ~= nil) then
                            #    deviceStr = deviceStr .. "d"

                if not self.pmPowerMaster:
                    # Process keypad settings
                    setting = self.pmReadSettings(pmDownloadItem_t["MSG_DL_1WKEYPAD"])
                    for i in range(0, keypad1wCnt):
                        keypadEnrolled = setting[i * 4 : i * 4 + 2] != bytearray.fromhex("00 00")
                        if keypadEnrolled:
                            deviceStr = "{0},K1 {1}".format(deviceStr, i)
                    setting = self.pmReadSettings(pmDownloadItem_t["MSG_DL_2WKEYPAD"])
                    for i in range (0, keypad2wCnt):
                        keypadEnrolled = setting[i * 4 : i * 4 + 3] != bytearray.fromhex("00 00 00")
                        if keypadEnrolled:
                            deviceStr = "{0},K2 {1}".format(deviceStr, i)
                    
                    # Process siren settings
                    setting = self.pmReadSettings(pmDownloadItem_t["MSG_DL_SIRENS"])
                    for i in range(0, sirenCnt):
                        sirenEnrolled = setting[i * 4 : i * 4 + 3] != bytearray.fromhex("00 00 00")
                        if sirenEnrolled:
                            deviceStr = "{0},S{1}".format(deviceStr, i)
                else: # PowerMaster
                    # Process keypad settings
                    setting = self.pmReadSettings(pmDownloadItem_t["MSG_DL_MR_KEYPADS"])
                    for i in range(0, keypad2wCnt):
                        keypadEnrolled = setting[i * 10 : i * 10 + 5] != bytearray.fromhex("00 00 00 00 00")
                        if keypadEnrolled:
                            deviceStr = "{0},K2 {1}".format(deviceStr, i)
                    # Process siren settings
                    setting = self.pmReadSettings(pmDownloadItem_t["MSG_DL_MR_SIRENS"])
                    for i in range (0, sirenCnt):
                        sirenEnrolled = setting[i * 10 : i * 10 + 5] != bytearray.fromhex("00 00 00 00 00")
                        if sirenEnrolled:
                            deviceStr = "{0},S{1}".format(deviceStr, i)

            # INTERFACE : Create Partitions in the interface
            for i in range(1, 2): # TODO: partitionCnt
                s = "Partition-%d".format(i)
                self.pmCreateDevice(i, s)
               
            log.debug("[Process Settings] Adding zone devices")
            # Use variables if pmAutoCreate is false or not in Powerlink mode, otherwise use what we read in the settings
            doorZones = doorZoneStr
            motionZones = motionZoneStr
            smokeZones = smokeZoneStr
            devices = deviceStr
            if not self.pmAutoCreate or not self.pmPowerlinkMode:
                log.debug("[Process Settings] Getting settings from user")
                #doorZones = luup.variable_get(PANEL_SID, "DoorZones", pmPanelDev)
                #motionZones = luup.variable_get(PANEL_SID, "MotionZones", pmPanelDev)
                #smokeZones = luup.variable_get(PANEL_SID, "SmokeZones", pmPanelDev)
                #devices = luup.variable_get(PANEL_SID, "Devices", pmPanelDev)
            elif self.pmPowerlinkMode:
                # INTERFACE - update interface with the 4 variables
                log.debug("[Process Settings] Updating interface with zone variables")
            
            #=================================================================================================================================
            #=================================================================================================================================
            #=================================================================================================================================
            #=================================================================================================================================
            # I think that the code here, bounded by the =============== should be done in the Home Assistant interface to create the devices
            #=================================================================================================================================
            #=================================================================================================================================
            #=================================================================================================================================
            #=================================================================================================================================
            # Start adding zones
            zoneName = ""
            zoneTypeName = ""
            for i in range(0, zoneCnt):
                s = "Z{0}".format(i)
                if i in self.pmSensorDev_t:
                    zoneName = self.pmSensorDev_t[i].zname
                    zoneTypeName = pmZoneType_t[pmLang][self.pmSensorDev_t[i].ztype]
                else:
                    zoneName = None
                    zoneTypeName = None
                stype = None
                if s in doorZones:
                    stype = "Magnet"
                elif s in motionZones:
                    stype = "Motion"
                elif s in smokeZones:
                    stype = "Smoke"
                    
                sensor = None
                if stype != None:
                    sensor = self.pmCreateDevice(i+1, stype, zoneName, zoneTypeName)

                if sensor != None:
                    if i not in self.pmSensorDev_t:
                        self.pmSensorDev_t[i] = {}
                    #self.pmSensorDev_t[i].id = findChild(pmPanelDev, sensor)
                    self.pmSensorDev_t[i].name = sensor
                    self.pmSensorDev_t[i].stype = stype
                    self.pmSensorDev_t[i].partition = self.pmSensorDev_t[i].partition or { 1 }

            # Add PGM and X10 devices
            for i in range (0, 15):
                if i == 0:
                    s = "PGM"
                    if s in devices:
                        log.debug("[Process Settings] Creating Device")
                        self.pmCreateDevice(s, "Switch", s)
                else:
                    s = "X%02d".format(i)
                    #string.find(devices, s)
                    if s in devices:
                        if devices[pos + 3 : pos + 3] == "d":
                            log.debug("[Process Settings] Creating Device")
                            self.pmCreateDevice(s, "Dim", x10_t[i], s)
                        else:
                            log.debug("[Process Settings] Creating Device")
                            self.pmCreateDevice(s, "Switch", x10_t[i], s)
                #x10_device = findChild(pmPanelDev, s)
                #if x10_device != None:
                #    x10_dev_nr = "%04x".format(2 ^ i) # INTERFACE - update in interface

            # Add sirens
            #for i in range (1, sirenCnt):
            #    sirenEnrolled = (string.find(devices, string.format("S%02d", i)) ~= nil)
            #    if (sirenEnrolled == true) then
            #        id = self.pmCreateDevice(i, "Siren")
            #        if (id ~= nil) then
            #           pmSirenDev_t[i] = findChild(pmPanelDev, id)

            # Add keypads
            #for i in range(1, keypad1wCnt):
            #    keypadEnrolled = (string.find(devices, string.format("K1%d", i)) ~= nil)
            #    if (keypadEnrolled == true) then
            #        self.pmCreateDevice(i, "Keypad", nil, "1-way")
               
            #for i in range (1, keypad2wCnt):
            #    keypadEnrolled = (string.find(devices, string.format("K2%d", i)) ~= nil)
            #    if (keypadEnrolled == true) then
            #        self.pmCreateDevice(i, "Keypad", nil, "2-way")
            #=================================================================================================================================
            #=================================================================================================================================
            #=================================================================================================================================
            #=================================================================================================================================
            #=================================================================================================================================
            #=================================================================================================================================
            #=================================================================================================================================
            #=================================================================================================================================
            
        #luup.chdev.sync(pmPanelDev, childDevices)
        if self.pmPowerlinkMode:
            self.SendCommand("MSG_RESTORE") # also gives status
        else:
            self.SendCommand("MSG_STATUS")
        self.pmStarting = False     # INTERFACE used in interface to tell user we're finished starting up
        log.info("[Process Settings] Ready for use")
            
    def handle_packet(self, packet):
        global DownloadMode
        self.reset_powerlink_timeout()
        
        """Handle one raw incoming packet."""
        #log.debug("[handle_packet] Parsing complete valid packet: %s", toString(packet))

        if len(packet) < 4:  # there must at least be a header, command, checksum and footer
            log.info("[handle_packet] Received invalid packet structure, not processing it " + toString(packet))
        elif packet[1] == 0x02: # ACK
            self.handle_msgtype02(packet[2:-2])  # remove the header and command bytes as the start. remove the footer and the checksum at the end
        elif packet[1] == 0x06: # Timeout
            self.handle_msgtype06(packet[2:-2])
        elif packet[1] == 0x08: # Access Denied
            self.handle_msgtype08(packet[2:-2])
        elif packet[1] == 0x0B: # Stopped
            self.handle_msgtype0B(packet[2:-2])
        elif packet[1] == 0x25: # Download retry
            self.handle_msgtype25(packet[2:-2])
        elif packet[1] == 0x33: # Settings send after a MSGV_START
            self.handle_msgtype33(packet[2:-2])
        elif packet[1] == 0x3c: # Message when start the download
            self.handle_msgtype3C(packet[2:-2])
        elif packet[1] == 0x3f: # Download information
            self.handle_msgtype3F(packet[2:-2])
        elif packet[1] == 0xa0: # Event log
            self.handle_msgtypeA0(packet[2:-2])
        elif packet[1] == 0xa5: # General Event
            self.handle_msgtypeA5(packet[2:-2])
        elif packet[1] == 0xa7: # General Event
            self.handle_msgtypeA7(packet[2:-2])
        elif packet[1] == 0xab and not self.ForceStandardMode: # PowerLink Event
            self.handle_msgtypeAB(packet[2:-2])
        elif packet[1] == 0xb0: # PowerMaster Event
            self.handle_msgtypeB0(packet[2:-2])
        else:
            log.info("[handle_packet] Unknown/Unhandled packet type {0}".format(packet[1:2]))
        # clear our buffer again so we can receive a new packet.
        self.ReceiveData = bytearray(b'')
            
    def displayzonebin(self, bits):
        """ Display Zones in reverse binary format
          Zones are from e.g. 1-8, but it is stored in 87654321 order in binary format """
        return bin(bits)

    def handle_msgtype02(self, data): # ACK
        """ Handle Acknowledges from the panel """
        # Normal acknowledges have msgtype 0x02 but no data, when in powerlink the panel also sends data byte 0x43
        #    I have not found this on the internet, this is my hypothesis
        self.pmWaitingForAckFromPanel = False

    def handle_msgtype06(self, data): 
        """ MsgType=06 - Time out
        Timeout message from the PM, most likely we are/were in download mode """
        global DownloadMode
        log.info("[handle_msgtype06] Timeout Received")
        if DownloadMode:
            DownloadMode = False
            self.SendCommand("MSG_EXIT")
        else:
            self.pmSendAck()

    def handle_msgtype08(self, data):
        log.info("[handle_msgtype08] Access Denied")
        if self.pmLastSentMessage != None:
            lastCommandData = self.pmLastSentMessage.command.data
            log.debug("[handle_msgtype08]                last command {0}".format(toString(lastCommandData)))
            if lastCommandData != None:            
                if lastCommandData[0] == 0x24:  
                    self.pmPowerlinkMode = False   # INTERFACE : Assume panel is going in to Standard mode
                    self.ProcessSettings()
                elif lastCommandData[0] & 0xA0 == 0xA0:  # this will match A0, A1, A2, A3 etc
                    log.info("[handle_msgtype08] Attempt to send a command message to the panel that has been denied, probably wrong pin code used")
                    # INTERFACE : tell user that wrong pin has been used

    def handle_msgtype0B(self, data): # STOP
        """ Handle STOP from the panel """
        log.debug("[handle_msgtype0B] Stop")
        # This is the message to tell us that the panel has finished download mode, so we too should stop download mode
        DownloadMode = False
        if self.pmLastSentMessage != None:
            lastCommandData = self.pmLastSentMessage.command.data
            log.debug("[handle_msgtype0B]                last command {0}".format(toString(lastCommandData)))
            if lastCommandData != None:            
                if lastCommandData[0] == 0x0A:                  
                    log.info("[handle_msgtype0B] We're in powerlink mode *****************************************")
                    self.pmPowerlinkMode = True  # INTERFACE set State to "PowerLink"
                    # We received a download exit message, restart timer
                    self.reset_powerlink_timeout()
                    self.ProcessSettings()

    def handle_msgtype25(self, data): # Download retry
        """ MsgType=25 - Download retry
        Unit is not ready to enter download mode
        """
        # Format: <MsgType> <?> <?> <delay in sec>
            
        iDelay = data[2]

        log.info("[handle_msgtype25] Download Retry, have to wait {0} seconds".format(iDelay))
                
        # Restart the List timer
        try:
            self.tListDelay.cancel()
        except:
            pass
        self.tListDelay = threading.Timer(iDelay, self.tListDelayExpired)
        self.tListDelay.start()
        
    def tListDelayExpired(self):
        """ Timer expired, start processing the List (can restart the timer) """
        try:
            self.tListDelay.cancel()
        except:
            pass
        # Timer expired, kick off the processing of the List
        self.ClearList()
        self.Start_Download()
                      
    def handle_msgtype33(self, data):
        """ MsgType=33 - Settings
        Message send after a VMSG_START. We will store the information in an internal array/collection """

        if len(data) != 10:
            log.debug("[handle_msgtype33] ERROR: MSGTYPE=0x33 Expected len=14, Received={0}".format(len(self.ReceiveData)))
            log.debug("[handle_msgtype33]                            " + toString(self.ReceiveData))
            return
                
        # Data Format is: <index> <page> <8 data bytes>
        # Extract Page and Index information
        iIndex = data[0]
        iPage = data[1]
                        
        log.info("[handle_msgtype33] Getting Data " + toString(data) + "   page " + hex(iPage) + "    index " + hex(iIndex))
        
        # Write to memory map structure, but remove the first 2 bytes from the data
        self.pmWriteSettings(iPage, iIndex, data[2:])
                            
    def handle_msgtype3C(self, data): # Panel Info Messsage when start the download
        """ The panel information is in 4 & 5
           5=PanelType e.g. PowerMax, PowerMaster
           4=Sub model type of the panel - just informational, not used
           """
        self.ModelType = data[4]
        self.PanelType = data[5]
        
        self.PowerMaster = (self.PanelType >= 7)
        modelname = pmPanelType_t[self.PanelType] or "UNKNOWN"  # INTERFACE set this in the user interface
          
        log.info("[handle_msgtype3C] PanelType={0} : {2} , Model={1}   Powermaster {3}".format(self.PanelType, self.ModelType, modelname, self.PowerMaster))

        # We got a first response, now we can continue enrollment the PowerMax/Master PowerLink
        self.pmPowerlinkEnrolled()

    def handle_msgtype3F(self, data):
        """ MsgType=3F - Download information
        Multiple 3F can follow eachother, if we request more then &HFF bytes """

        log.info("[handle_msgtype3F]")
        # data format is normally: <index> <page> <length> <data ...>
        # If the <index> <page> = FF, then it is an additional PowerMaster MemoryMap
        iIndex = data[0]
        iPage = data[1]
        iLength = data[2]
                
        # Check length and data-length
        if iLength != len(data) - 3:  # 3 because -->   index & page & length
            log.debug("[handle_msgtype3F] ERROR: Type=3F has an invalid length, Received: {0}, Expected: {1}".format(len(data)-3, iLength))
            log.debug("[handle_msgtype3F]                            " + toString(self.ReceiveData))
            return

        # Write to memory map structure, but remove the first 4 bytes (3F/index/page/length) from the data
        self.pmWriteSettings(iPage, iIndex, data[3:])

    def handle_msgtypeA0(self, data):
        """ MsgType=A0 - Event Log """
        log.info("[handle_MsgTypeA0]")
        
        eventNum = data[1]        
        # Check for the first entry, it only contains the number of events
        if eventNum == 0x01:
            log.debug("[handle_msgtypeA0] Eventlog received")
            self.eventCount = data[0]
        else:
            iSec = data[2]
            iMin = data[3]
            iHour = data[4]
            iDay = data[5]
            iMonth = data[6]
            iYear = CInt(data[7]) + 2000
                
            iEventZone = data[8]
            iLogEvent = data[9]
            zoneStr = pmLogUser_t[iEventZone] or "UNKNOWN"
            eventStr = pmLogEvent_t[pmLang][iLogEvent] or "UNKNOWN"
            idx = eventNum - 1

            # Create an event log array
            self.pmEventLogDictionary[idx] = {}
            if pmPanelConfig_t["CFG_PARTITIONS"][self.PanelType] > 1:
                part = 0
                for i in range(0, 3):
                    part = (iSec % (2 * i) >= i) and i or part
                self.pmEventLogDictionary[idx].partition = (part == 0) and "Panel" or part
                self.pmEventLogDictionary[idx].time = "{0}:{1}".format(iHour, iMin)
            else:
                self.pmEventLogDictionary[idx].partition = (eventZone == 0) and "Panel" or "1"
                self.pmEventLogDictionary[idx].time = "{0}:{1}:{2}".format(iHour, iMin, iSec)
            self.pmEventLogDictionary[idx].date = "{0}/{1}/{2}".format(iDay, iMonth, iYear)
            self.pmEventLogDictionary[idx].zone = zoneStr
            self.pmEventLogDictionary[idx].event = eventStr
            self.pmEventLogDictionary.items = idx
            self.pmEventLogDictionary.done = (eventNum == self.eventCount)
            
                
#    def displaySensorBypass(self, sensor):
#        armed = False
#        if self.pmSensorShowBypass:
#            armed = sensor.bypass
#        else:
#            zoneType = sensor.ztype
#            mode = bitw.band(pmSysStatus, 0x0F) -- armed or not: 4=armed home; 5=armed away
#		 local alwaysOn = { [2] = "", [3] = "", [9] = "", [10] = "", [11] = "", [14] = "" }
		 # Show as armed if
		 #    a) the sensor type always triggers an alarm: (2)flood, (3)gas, (11)fire, (14)temp, (9/10)24h (silent/audible)
		 #    b) the system is armed away (mode = 4)
		 #    c) the system is armed home (mode = 5) and the zone is not interior(-follow) (6,12)
#         armed = ((zoneType > 0) and (sensor['bypass'] ~= true) and ((alwaysOn[zoneType] ~= nil) or (mode == 0x5) or ((mode == 0x4) and (zoneType % 6 ~= 0)))) and "1" or "0"

    # captured examples of A5 data
    #     0d a5 00 04 00 61 03 05 00 05 00 00 43 a4 0a
    def handle_msgtypeA5(self, data): # Status Message
        log.info("[handle_msgtypeA5] Parsing A5 packet")
        
        msgTot = data[0]
        eventType = data[1]
        
        if eventType == 0x01: # Log event print
            log.debug("[handle_msgtypeA5] Log Event Print")
        elif eventType == 0x02: # Status message zones
            val = data[2]
            val = val + (0x100 * data[3])
            val = val + (0x10000 * data[4])
            val = val + (0x1000000 * data[5])
            log.debug("[handle_msgtypeA5] Status Message Zones 32-01: {:032b}".format(val))

            val = data[6]
            val = val + (0x100 * data[7])
            val = val + (0x10000 * data[8])
            val = val + (0x1000000 * data[9])
            log.debug("[handle_msgtypeA5] Battery Message Zones 32-01: {:032b}".format(val))

            #log.debug("[handle_msgtypeA5] Status Zones 01-08: %s ", self.displayzonebin(data[2]))
            #log.debug("[handle_msgtypeA5] Status Zones 09-16: %s ", self.displayzonebin(data[3]))
            #log.debug("[handle_msgtypeA5] Status Zones 17-24: %s ", self.displayzonebin(data[4]))
            #log.debug("[handle_msgtypeA5] Status Zones 25-30: %s ", self.displayzonebin(data[5]))
            #log.debug("[handle_msgtypeA5] Battery Zones 01-08: %s ", self.displayzonebin(data[6]))
            #log.debug("[handle_msgtypeA5] Battery Zones 09-16: %s ", self.displayzonebin(data[7]))
            #log.debug("[handle_msgtypeA5] Battery Zones 17-24: %s ", self.displayzonebin(data[8]))
            #log.debug("[handle_msgtypeA5] Battery Zones 25-30: %s ", self.displayzonebin(data[9]))
        elif eventType == 0x03: # Tamper Event
            log.debug("Tamper event")
            val = data[2]
            val = val + (0x100 * data[3])
            val = val + (0x10000 * data[4])
            val = val + (0x1000000 * data[5])
            log.debug("[handle_msgtypeA5] Status Message Zones 32-01: {:032b}".format(val))

            val = data[6]
            val = val + (0x100 * data[7])
            val = val + (0x10000 * data[8])
            val = val + (0x1000000 * data[9])
            log.debug("[handle_msgtypeA5] Tamper Message Zones 32-01: {:032b}".format(val))

            #log.debug("[handle_msgtypeA5] Status Zones 01-08: %s ", self.displayzonebin(data[2]))
            #log.debug("[handle_msgtypeA5] Status Zones 09-16: %s ", self.displayzonebin(data[3]))
            #log.debug("[handle_msgtypeA5] Status Zones 17-24: %s ", self.displayzonebin(data[4]))
            #log.debug("[handle_msgtypeA5] Status Zones 25-30: %s ", self.displayzonebin(data[5]))
            #log.debug("[handle_msgtypeA5] Tamper Zones 01-08: %s ", self.displayzonebin(data[6]))
            #log.debug("[handle_msgtypeA5] Tamper Zones 09-16: %s ", self.displayzonebin(data[7]))
            #log.debug("[handle_msgtypeA5] Tamper Zones 17-24: %s ", self.displayzonebin(data[8]))
            #log.debug("[handle_msgtypeA5] Tamper Zones 25-30: %s ", self.displayzonebin(data[9]))
        elif eventType == 0x04: # Zone event
            sysStatus = data[2]
            sysFlags  = data[3]
            eventZone = data[4]
            zoneType  = data[5]
            # dont know what 6 and 7 are
            x10stat1  = data[8]
            x10stat2  = data[9]
            x10status = x10stat1 + (x10stat2 * 0x100)
            
            log.debug("[handle_msgtypeA5] Zone Event sysStatus {0}   sysFlags {1}   eventZone {2}   zoneType {3}   x10status {4}".format(hex(sysStatus), hex(sysFlags), eventZone, zoneType, hex(x10status)))
            
            # Examine zone tripped status
            if eventZone != 0:
                log.debug("[handle_msgtypeA5] Event {0} in zone {1}".format(pmEventType_t[pmLang][zoneType] or "UNKNOWN", eventZone))
                sensor = self.pmSensorDev_t[eventZone]
                if sensor != None:
                    # set Tripped values in interface for different zone types
                    if zoneType == 0x03:
                        log.debug("[handle_msgtypeA5] zone type 3 device tripped " + str(eventZone))
                    elif zoneType == 0x04:
                        log.debug("[handle_msgtypeA5] zone type 4 device tripped " + str(eventZone))
                    elif zoneType == 0x05:
                        log.debug("[handle_msgtypeA5] zone type 5 device tripped " + str(eventZone))
                else:
                    log.debug("[handle_msgtypeA5] unable to locate zone device " + str(eventZone))
            
            # Examine X10 status
            for i in range(0, 16):
                if i == 0:
                   s = "PGM"
                else:
                   s = "{0}".format(i)
                #child = findChild(pmPanelDev, s)
                status = x10status & (1 << i)
                # INTERFACE : use this to set X10
            
            slog = pmDetailedArmMode_t[sysStatus]
            if sysStatus in [0x03, 0x04, 0x05, 0x0A, 0x0B, 0x14, 0x15]:
                sarm = "Armed"
            elif sysStatus > 0x15:
                log.debug("[handle_msgtypeA5] Unknown state, assuming Disarmed")
                sarm = "Disarmed"
            else:
                sarm = "Disarmed"
            
            if sysStatus & 64 != 0:
                log.debug("[handle_msgtypeA5] Bit 6 set, Status Changed")
            if sysStatus & 128 != 0: 
                log.debug("[handle_msgtypeA5] Bit 7 set, Alarm Event")
                
            log.debug("[handle_msgtypeA5] log: {0}, arm: {1}".format(slog, sarm))
                                          
            if sysFlags & 1 != 0:
                log.debug("[handle_msgtypeA5] Bit 0 set, Ready")
            if sysFlags & 2 != 0:
                log.debug("[handle_msgtypeA5] Bit 1 set, Alert in Memory")
            if sysFlags & 4 != 0:
                log.debug("[handle_msgtypeA5] Bit 2 set, Trouble!")
            if sysFlags & 8 != 0:
                log.debug("[handle_msgtypeA5] Bit 3 set, Bypass")
            if sysFlags & 16 != 0:
                log.debug("[handle_msgtypeA5] Bit 4 set, Last 10 seconds of entry/exit")
            if sysFlags & 32 != 0:
                sEventLog = pmEventType_t["EN"][zoneType]
                log.debug("[handle_msgtypeA5] Bit 5 set, Zone Event")
                log.debug("[handle_msgtypeA5]       Zone: {0}, {1}".format(eventZone, sEventLog))

            # Trigger a zone event will only work for PowerMax and not for PowerMaster
            # if not self.PowerMaster:
                #If $sReceiveData[6 : 3 Or If $sReceiveData[6 : 4 Or If $sReceiveData[6 : 5 Then
#            if self.ReceiveData[7] == 5:
#                    if self.Config and if self.Config.Exist("zoneinfo") and if self.Config["zoneinfo"].Exist(self.ReceiveData[5]) and if self.Config["zoneinfo"][self.ReceiveData[5]]["sensortype"]:
#FIXME                        
#                        # Try to find it, it will autocreate the device if needed
#                        iDeviceId = Devices.Find(Instance, "Z" & Format$($sReceiveData[5], "00"), InterfaceId, $cConfig["zoneinfo"][$sReceiveData[5]]["autocreate"])
#                        If iDeviceId Then Devices.ValueUpdate(iDeviceId, 1, "On")
#                
#                        if $cConfig["zoneinfo"][$sReceiveData[5]]["sensortype"] == "Motion":
#                            EnableTripTimer("Z" & Format$($sReceiveData[5], "00"))
#                        Endif
#                    else:
#                log.info("[handle_msgtypeA5] ERROR: Zone information for zone '" & self.ReceiveData[7] & "' is unknown, possible the Plugin couldn't retrieve the information from the panel?")

#FIXME until here
        elif eventType == 0x06: # Status message enrolled/bypassed
            val = data[2]
            val = val + (0x100 * data[3])
            val = val + (0x10000 * data[4])
            val = val + (0x1000000 * data[5])
            log.debug("[handle_msgtypeA5]      Enrolled Message Zones 32-01: {:032b}".format(val))

            val = data[6]
            val = val + (0x100 * data[7])
            val = val + (0x10000 * data[8])
            val = val + (0x1000000 * data[9])
            log.debug("[handle_msgtypeA5]      Bypassed Message Zones 32-01: {:032b}".format(val))

            #log.debug("[handle_msgtypeA5] Enrollment and Bypass Message")
            #log.debug("[handle_msgtypeA5] Enrolled Zones 01-08: %s ", self.displayzonebin(data[2]))
            #log.debug("[handle_msgtypeA5] Enrolled Zones 09-16: %s ", self.displayzonebin(data[3]))
            #log.debug("[handle_msgtypeA5] Enrolled Zones 17-24: %s ", self.displayzonebin(data[4]))
            #log.debug("[handle_msgtypeA5] Enrolled Zones 25-30: %s ", self.displayzonebin(data[5]))
            #log.debug("[handle_msgtypeA5] Bypassed Zones 01-08: %s ", self.displayzonebin(data[6]))
            #log.debug("[handle_msgtypeA5] Bypassed Zones 09-16: %s ", self.displayzonebin(data[7]))
            #log.debug("[handle_msgtypeA5] Bypassed Zones 17-24: %s ", self.displayzonebin(data[8]))
            #log.debug("[handle_msgtypeA5] Bypassed Zones 25-30: %s ", self.displayzonebin(data[9]))
        else:
            log.debug("[handle_msgtypeA5]      Unknown A5 Event: %s", hex(eventType))

    def handle_msgtypeA7(self, data):
        """ MsgType=A7 - Panel Status Change """
        log.info("[handle_msgtypeA7] Panel Status Change " + toString(data))
        #log.debug("[handle_msgtypeA7] Zone/User: " & DisplayZoneUser(packet[3:4]))
        #log.debug("[handle_msgtypeA7] Log Event: " & DisplayLogEvent(packet[4:5]))
        
        # 01 00 00 60 00 ff 00 35 00 00 43 7e        
        msgCnt = int(data[0])
        log.info("[handle_msgtypeA7]      A7 message contains {0} messages".format(msgCnt))
        for i in range(1, msgCnt+1):
            eventZone = int(data[2 * i])
            logEvent  = int(data[1 + (2 * i)])
            eventType = int(logEvent & 0x7F)
            s = (pmLogEvent_t[pmLang][eventType] or "UNKNOWN") + " / " + (pmLogUser_t[eventZone] or "UNKNOWN")
            alarmStatus = "None"
            if eventType in pmPanelAlarmType_t:
                alarmStatus = pmPanelAlarmType_t[eventType]
            troubleStatus = "None"
            if eventType in pmPanelTroubleType_t:
                troubleStatus = pmPanelTroubleType_t[eventType]
            log.info("[handle_msgtypeA7]                System message: " + s + "    alarmStatus " + alarmStatus + "    troubleStatus " + troubleStatus)
            # INTERFACE Set appropriate setting
            # Update siren status
            noSiren = ((eventType == 0x0B) or (eventType == 0x0C)) # and (pmSilentPanic == true)
            #if (alarmStatus != "None") and (eventType != 0x04) and (noSiren == False):
            #pmSirenActive = pmTimeFunction() + 60 * pmBellTime
            #if (eventType == 0x1B) and (pmSirenActive ~= nil) then -- Cancel Alarm
            #pmSirenActive = nil
            #for i = 1, #pmSirenDev_t do
            # luup.variable_set(SIREN_SID, "Status", "0", pmSirenDev_t[i])
            #if (eventType == 0x60):
            #    self.Start_Download()
                
    # pmHandlePowerlink (0xAB)
    def handle_msgtypeAB(self, data): # PowerLink Message
        log.info("[handle_msgtypeAB]")
        global DownloadMode
        # Restart the timer
        self.reset_powerlink_timeout()

        subType = self.ReceiveData[2]
        if subType == 3: # keepalive message
            log.debug("[handle_msgtypeAB] PowerLink Keep-Alive")
            DownloadMode = False
            self.pmSendAck()
            if self.pmPowerlinkMode == False:
                log.debug("[handle_msgtypeAB]     Got alive message while not in Powerlink mode; starting download")
                self.Start_Download()
        elif subType == 5: # -- phone message
            action = self.ReceiveData[4]
            if action == 1:
                log.debug("[handle_msgtypeAB] PowerLink Phone: Calling User")
                #pmMessage("Calling user " + pmUserCalling + " (" + pmPhoneNr_t[pmUserCalling] +  ").", 2)
                #pmUserCalling = pmUserCalling + 1
                #if (pmUserCalling > pmPhoneNr_t) then
                #    pmUserCalling = 1
            elif action == 2:
                log.debug("[handle_msgtypeAB] PowerLink Phone: User Acknowledged")
                #pmMessage("User " .. pmUserCalling .. " acknowledged by phone.", 2)
                #pmUserCalling = 1
            else:
                log.debug("[handle_msgtypeAB] PowerLink Phone: Unknown Action {0}".format(hex(self.ReceiveData[3]).upper()))
            self.pmSendAck()
        elif (subType == 10 and self.ReceiveData[4] == 0):
            log.info("[handle_msgtypeAB] PowerLink telling us what the code is for downloads, currently commented out as I'm not certain of this")
            #DownloadCode[0] = self.ReceiveData[5]
            #DownloadCode[1] = self.ReceiveData[6]
        elif (subType == 10 and self.ReceiveData[4] == 1):
            log.debug("[handle_msgtypeAB] PowerLink most likely wants to auto-enroll, only doing auto enroll once")
            DownloadMode = False
            self.SendMsg_ENROLL()
        else:
            self.pmSendAck()

    def handle_msgtypeB0(self, data): # PowerMaster Message
        msgSubTypes = [0x00, 0x01, 0x02, 0x03, 0x04, 0x07, 0x08, 0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x0E, 0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x18, 0x19, 0x1B, 0x1C, 0x1D, 0x1E, 0x1F, 0x20, 0x21, 0x24, 0x2D, 0x2E, 0x2F, 0x30, 0x31, 0x32, 0x33, 0x34, 0x38, 0x39, 0x3A ]
        msgType = data[0] # 00, 01, 04: req; 03: reply, so expecting 0x03
        subType = data[1]
        msgLen  = data[2]
        log.debug("[handle_msgtypeAB] Received PowerMaster message {0}/{1} (len = {2})".format(msgType, subType, msgLen))
        #if msgType == 0x03 and subType == 0x39:
        #    pmSendMessage("MSG_POWERMASTER", { msg = pmSendMsgB0_t.ZONE_STAT1 })
        #    pmSendMessage("MSG_POWERMASTER", { msg = pmSendMsgB0_t.ZONE_STAT2 })
                    
    def SendMsg_ENROLL(self):
        """ Auto enroll the PowerMax/Master unit """
        global DownloadMode
        log.info("[SendMsg_ENROLL]  download pin will be " + toString(DownloadCode))
        # Remove anything else from the List, we need to restart
        self.pmExpectedResponse = []
        # Clear the list
        self.ClearList()
        sleep(1.0)
        # Send the request, the pin will be added when the command is send
        #self.SendCommand("MSG_RESTORE") #,  options = [4, DownloadCode])
        self.SendCommand("MSG_ENROLL",  options = [4, DownloadCode])
        # We are doing an auto-enrollment, most likely the download failed. Lets restart the download stage.
        if DownloadMode:
            log.debug("[SendMsg_ENROLL] Resetting download mode to 'Off' in order to retrigger it")
            DownloadMode = False
        self.Start_Download()
                        
            
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
                yield from asyncio.wait_for(self._command_ack.wait(), TIMEOUT.seconds, loop=self.loop)
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
            #log.debug("In __init__")
            self.ignore = []

    def _handle_packet(self, packet):
        log.debug("In _handle_packet")
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
        log.debug("In handle_event")
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
        #log.debug("In handle_packet")
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
        log.debug("In ignore_event")
        for ignore in self.ignore:
            if (ignore == event_id or
                    (ignore.endswith('*') and event_id.startswith(ignore[:-1]))):
                return True
        return False

class VisonicProtocol(CommandSerialization, EventHandling):
    """Combine preferred abstractions that form complete Rflink interface."""

def create_tcpvisonic_connection(address=None, port=None, protocol=VisonicProtocol,
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

    # setup tcp connection
    #message = 'Hello World!'
#    conn = loop.create_connection(lambda: EchoClientProtocol(message, loop), address, port)
    address = address
    port = port
    conn = loop.create_connection(protocol, address, port)

    return conn

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
