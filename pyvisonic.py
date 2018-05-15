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
PowerLinkTimeout = 600
MasterCode = bytearray.fromhex('56 50')
DisarmArmCode = bytearray.fromhex('56 50')
DownloadCode = bytearray.fromhex('0D 02')

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
   "MSG_INIT"        : VisonicCommand(bytearray.fromhex('AB 0A 00 01 00 00 00 00 00 00 00 43'), None, False,  "Initializing PowerMax/Master PowerLink Connection" ),
   "MSG_ALIVE"       : VisonicCommand(bytearray.fromhex('AB 03 00 00 00 00 00 00 00 00 00 43'), None, False, "I'm Alive Message To Panel" ),
   "MSG_ZONENAME"    : VisonicCommand(bytearray.fromhex('A3 00 00 00 00 00 00 00 00 00 00 43'), None, False, "Requesting Zone Names" ),
   "MSG_ZONETYPE"    : VisonicCommand(bytearray.fromhex('A6 00 00 00 00 00 00 00 00 00 00 43'), None, False, "Requesting Zone Types" ),
   "MSG_X10NAMES"    : VisonicCommand(bytearray.fromhex('AC 00 00 00 00 00 00 00 00 00 00 43'), None, False, "Requesting X10 Names" ),
   "MSG_RESTORE"     : VisonicCommand(bytearray.fromhex('AB 06 00 00 00 00 00 00 00 00 00 43'), 0xA5, False, "Restore PowerMax/Master Connection" ),
   "MSG_ENROLL"      : VisonicCommand(bytearray.fromhex('AB 0A 00 00 99 99 00 00 00 00 00 43'), None, False, "Auto-Enroll of the PowerMax/Master" ),
   "MSG_REENROLL"    : VisonicCommand(bytearray.fromhex('AB 06 00 00 99 99 00 00 00 00 00 43'), None, False, "Re-enrolling" ),
   "MSG_EVENTLOG"    : VisonicCommand(bytearray.fromhex('A0 00 00 00 12 34 00 00 00 00 00 43'), 0xA0, False, "Retrieving Event Log" ),
   "MSG_ARM"         : VisonicCommand(bytearray.fromhex('A1 00 00 00 12 34 00 00 00 00 00 43'), None, False, "Arming System" ),
   "MSG_STATUS"      : VisonicCommand(bytearray.fromhex('A2 00 00 00 00 00 00 00 00 00 00 43'), 0xA5, False, "Getting Status" ),
   "MSG_BYPASSTAT"   : VisonicCommand(bytearray.fromhex('A2 00 00 20 00 00 00 00 00 00 00 43'), 0xA5, False, "Bypassing" ),
   "MSG_X10PGM"      : VisonicCommand(bytearray.fromhex('A4 00 00 00 00 00 12 34 00 00 43')   , None, False, "X10 Data" ),
   "MSG_BYPASSEN"    : VisonicCommand(bytearray.fromhex('AA 12 34 00 00 00 00 43')            , None, False, "BYPASS Enable" ),
   "MSG_BYPASSDIS"   : VisonicCommand(bytearray.fromhex('AA 12 34 00 00 00 00 12 34 43')      , None, False, "BYPASS Disable" ),
   "MSG_DOWNLOAD"    : VisonicCommand(bytearray.fromhex('24 00 00 00 99 99 00 00 00 00 00')   , 0x3C, False, "Start Download Mode" ),
   "MSG_SETTIME"     : VisonicCommand(bytearray.fromhex('46 F8 00 01 02 03 04 05 06 FF FF')   , 0XA0, False, "Setting Time" ),   # may not need an ack
   "MSG_DL"          : VisonicCommand(bytearray.fromhex('3E 00 00 00 00 B0 00 00 00 00 00')   , 0x3F, False, "Download Data Set" ),
   "MSG_SER_TYPE"    : VisonicCommand(bytearray.fromhex('5A 30 04 01 00 00 00 00 00 00 00')   , 0x33, False, "Get Serial Type" ),
   "MSG_START"       : VisonicCommand(bytearray.fromhex('0A')                                 , 0x33, False, "Start" ),
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
   0x25 : PanelCallBack( None, True ),     # 14 Download Retry  
   0x33 : PanelCallBack( None, True ),     # 14 Download Settings
   0x3C : PanelCallBack( None, True ),     # 14 Panel Info
   0x3F : PanelCallBack( None, True ),   # Download Info
   0xA0 : PanelCallBack( None, True ),     # 15 Event Log
   0xA5 : PanelCallBack(   15, True ),     # 15 Status Update       Length was 15 but panel seems to send different lengths
   0xA7 : PanelCallBack( None, True ),     # 15 Panel Status Change
   0xAB : PanelCallBack( None, True ),     # 15 Enroll Request 0x0A  OR Ping 0x03      Length was 15 but panel seems to send different lengths
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
   1 : "PowerMax", 2 : "PowerMax+", 3 : "PowerMax Pro", 4 : "PowerMax Complete", 5 : "PowerMax Pro Part",
   6  : "PowerMax Complete Part", 7 : "PowerMax Express", 8 : "PowerMaster10",     9 : "PowerMaster30"
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
    ReceiveLastPacketTime = None # datetime of when last ReceiveData packet is received
    PowerLinkTimeoutTime = datetime.now()
    tListDelay = object
    pmVarLenMsg = False
    #ForceStandardMode = False
    pmIncomingPduLen = 0
    pmSendMsgRetries = 0
    
    pmLastSentTime = datetime.now() - timedelta(seconds=1)  ## take off 1 second so the first command goes through immediatelly
    pmCrcErrorCount = 0
    pmStarting = False
    
    pmPowerMaster = False
    
    msgType_t = None
    
    pmLastSentMessage = None
    pmSyncTimeCheck = None
    
    keepalivecounter = 0

    pmExpectedResponse = []
    pmPowerlinkMode = False
    pmWaitingForAckFromPanel = False
    
    log.debug("Initialising")
    
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
        self.pmRawSettings_t = {}
        self.pmSirenDev_t = {}
        self.lock = threading.Lock()
 
    def pmTimeFunction(self) -> datetime:
        return datetime.now()
        
    def PowerLinkTimeoutExpired(self):
        """We timed out, try to restore the connection."""
        log.debug("[PowerLinkTimeoutExpired] Powerlink Timer Expired")
        self.SendCommand("MSG_RESTORE")
        # restart the timer
        self.reset_powerlink_timeout()

    def reset_powerlink_timeout(self):
        # log.debug("Resetting Powerlink Timeout")
        try:
            tPowerLinkKeepAlive.cancel()
        except:
            pass
        tPowerLinkKeepAlive = threading.Timer(PowerLinkTimeout, self.PowerLinkTimeoutExpired)
        tPowerLinkKeepAlive.start()
        #self.PowerLinkTimeoutTime = self.pmTimeFunction();

    def keep_alive_messages_timer(self):
        global DownloadMode
        self.lock.acquire()
        #if not DownloadMode:
        self.keepalivecounter = self.keepalivecounter + 1
        if not DownloadMode and len(self.SendList) == 0 and self.keepalivecounter >= 30:   #  
            log.debug("Send list is empty so sending I'm alive message")
            self.lock.release()
            self.keepalivecounter = 0
            self.SendCommand("MSG_ALIVE")
            # self.SendCommand("MSG_STATUS")
        else:
            log.debug("DownloadMode set to {0}     keepalivecounter {1}      len(self.SendList) {2}".format(DownloadMode, self.keepalivecounter, len(self.SendList)))
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
        log.info('[connection_made] connected')
        
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
        self.ReceiveLastPacketTime = self.pmTimeFunction()
        self.SendCommand("MSG_INIT")

        # Define powerlink seconds timer and start it for PowerLink communication
        self.reset_powerlink_timeout()

        # Send the download command, this should initiate the communication
        # Only skip it, if we force standard mode
        if not self.ForceStandardMode:
            log.info("Sending Download Start")
            self.Start_Download()
        else:
            self.ProcessSettings()
            # INTERFACE: set "PowerlinkMode" to "Standard" mode in HA interface
            
    def data_received(self, data): 
       """Add incoming data to ReceiveData."""
       #log.debug('[data_received] received data: %s', toString(data))
       for databyte in data:
           #log.debug("Processing " + hex(databyte))
           self.handle_received_byte(databyte)
           
#pmIncomingPduLen is only used in this function
    def handle_received_byte(self, data):
        """Process a single byte as incoming data."""
        pduLen = len(self.ReceiveData)

        # If we were expecting a message of a particular length and what we have is already greater then that length then dump the message and resynchronise.
        if self.pmIncomingPduLen > 0 and pduLen >= self.pmIncomingPduLen:   # waiting for pmIncomingPduLen bytes but got more and haven't been able to validate a PDU
            log.debug("Building PDU: Dumping Current PDU " + toString(self.ReceiveData))
            self.ReceiveData = bytearray(b'')
            pduLen = len(self.ReceiveData)
            
        if pduLen == 4 and self.pmVarLenMsg:
            # Determine length of variable size message
            msgType = self.ReceiveData[1]
            pmIncomingPduLen = ((int(self.ReceiveData[3]) * 0x100) + int(self.ReceiveData[2]))
            if pmIncomingPduLen >= 0xB0:  # then more than one message
                pmIncomingPduLen = 0 # set it to 0 for this loop around
            log.info("Variable length Message Being Receive  Message {0}     pmIncomingPduLen {1}".format(msgType, self.pmIncomingPduLen))
   
        if pduLen == 0:
            self.msgType_t = None
            if data == 0x0D: # preamble
                #response = ""
                #for i in range(1, len(self.pmExpectedResponse)):
                #    response = response + string.format(" %02X", string.byte(self.pmExpectedResponse, i))
                #if (response != ""):
                #    response = "; Expecting" + response
                #log.debug("Start of new PDU detected, expecting response " + response)
                #log.debug("Start of new PDU detected")
                self.ReceiveData = bytearray(b'')
                self.ReceiveData.append(data)
                #log.debug("Starting PDU " + toString(self.ReceiveData))
        elif pduLen == 1:
            msgType = data
            #log.debug("Received message Type %d", data)
            if msgType in pmReceiveMsg_t:
                self.msgType_t = pmReceiveMsg_t[msgType]
                self.pmIncomingPduLen = (self.msgType_t != None) and self.msgType_t.length or 0
                if msgType == 0x3F: # and self.msgType_t != None and self.pmIncomingPduLen == 0:
                    self.pmVarLenMsg = True
            else:
                log.error("ERROR : Construction of incoming packet unknown - Message Type {0}".format(hex(msgType)))
            #log.debug("Building PDU: It's a message %02X; pmIncomingPduLen = %d", data, self.pmIncomingPduLen)
            self.ReceiveData.append(data)
        elif (self.pmIncomingPduLen == 0 and data == 0x0A) or (pduLen + 1 == self.pmIncomingPduLen): # postamble   (standard length message and data terminator) OR (actual length == calculated length)
            self.ReceiveData.append(data)
            #log.debug("Building PDU: Checking it " + toString(self.ReceiveData))
            msgType = self.ReceiveData[1]
            if (data != 0x0A) and (self.ReceiveData[pduLen] == 0x43):
                log.info("Building PDU: Special Case 42 ********************************************************************************************")
                self.pmIncomingPduLen = self.pmIncomingPduLen + 1 # for 0x43
            elif self.validate_packet(self.ReceiveData):
                log.info("Building PDU: Got Validated PDU type 0x%02x   full data %s", int(msgType), toString(self.ReceiveData))
                self.pmVarLenMsg = False
                self.pmLastPDU = self.ReceiveData
                self.pmLogPdu(self.ReceiveData, "<-PM  ")
                pmLastSentTime = self.pmTimeFunction()  # get time now to track how long it takes for a reply
         
                if (self.msgType_t == None):
                    log.info(string.format("Unhandled message %02X", msgType))
                    self.pmSendAck()
                else:
                    # Send an ACK if needed
                    if self.msgType_t.ackneeded:
                        #log.debug("Sending an ack as needed by last panel status message " + hex(msgType))
                        self.pmSendAck()
                    # Handle the message
                    #log.debug("[receive] Received message " + hex(msgType))
                    self.handle_packet(self.ReceiveData)
                    # Check response
                    #log.debug(self.pmExpectedResponse)
                    if (len(self.pmExpectedResponse) > 0 and msgType != 2):   ## 2 is a simple acknowledge from the panel so ignore those
                        # We've sent something and are waiting for a reponse - this is it
                        if (msgType in self.pmExpectedResponse):
                            while msgType in self.pmExpectedResponse:
                                log.debug("Got one of the needed response messages from the Panel")
                                self.pmExpectedResponse.remove(msgType)
                            self.pmSendMsgRetries = 0
                        else:
                            log.debug("msgType not in self.pmExpectedResponse   Waiting for next PDU :  expected {0}   got {1}".format(self.pmExpectedResponse, msgType))
                            #if ignore: # ignore unexpected messages
                            #    log.debug("ignoring expected response")
                            if self.pmSendMsgRetries == 0:
                                self.pmSendMsgRetries = 1 # don't resend immediately - first wait for the next response
                            else:
                                if (self.pmSendMsgRetries == MSG_RETRIES):
                                    #self.pmHandleCommException("wrong response")
                                    log.info("Waiting a long time for the Expected Response")
                                else:
                                    self.pmSendMsgRetries = self.pmSendMsgRetries + 1
                self.ReceiveData = bytearray(b'')
            else: # CRC check failed. However, it could be an 0x0A in the middle of the data packet and not the terminator of the message
                if len(self.ReceiveData) > 0xB0:
                    log.info("PDU with CRC error %s", toString(self.ReceiveData))
                    self.pmLogPdu(self.ReceiveData, "<-PM   PDU with CRC error")
                    pmLastSentTime = self.pmTimeFunction() - timedelta(seconds=1)
                    self.ReceiveData = bytearray(b'')
                    if msgType != 0xF1:        # ignore CRC errors on F1 message
                        self.pmCrcErrorCount = self.pmCrcErrorCount + 1
                    if (self.pmCrcErrorCount > MAX_CRC_ERROR):
                        self.pmHandleCommException("CRC errors")
                else:
                    a = self.calculate_crc(self.ReceiveData[1:-2])[0]
                    log.info("Building PDU: Length is now %d bytes (apparently PDU not complete)    %s    checksum calcs %02x", len(self.ReceiveData), toString(self.ReceiveData), a)
        elif pduLen <= 0xC0:
            #log.debug("Current PDU " + toString(self.ReceiveData) + "    adding " + str(hex(data)))
            self.ReceiveData.append(data)
        else:
            log.info("Dumping Current PDU " + toString(self.ReceiveData))
            self.ReceiveData = bytearray(b'') # messages should never be longer than 0xC0
        #log.debug("Building PDU " + toString(self.ReceiveData))
    #return true
    
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
        """ Send ACK if packet is valid
            Needs to be fixed to send Type1/Type2 ACK when needed, now just sending ACK1 for simplicity
        """
        lastType = self.pmLastPDU[1]
        normalMode = (lastType >= 0x80) or ((lastType < 0x10) and (self.pmLastPDU[len(self.pmLastPDU) - 2] == 0x43))
   
        self.lock.acquire()
        #log.debug("[sending_ack] Sending an ack back to Alarm")
        if normalMode:
            self.transport.write(bytearray.fromhex('0D 02 43 00 0A'))
        else:
            self.transport.write(bytearray.fromhex('0D 02 00 0A'))
        self.lock.release()
    
    def validate_packet(self, packet : bytearray) -> bool:
        """Verify if packet is valid.
            >>> Packets start with a preamble (\x0D) and end with postamble (\x0A)
        """
        #packet = self.ReceiveData
        
        #testy = bytearray.fromhex("A0 FB AC 00 05 0C 02 01 00 00 60 43")
        #t1 = self.calculate_crc(testy)
        #log.debug("should be FE     t1 is %02X", t1[0])
        
        #log.debug("[validate_packet] Calculating for: %s", toString(packet))
        
        if packet[:1] != b'\x0D':
            return False
        if packet[-1:] != b'\x0A':
            return False
        if packet[-2:-1] == self.calculate_crc(packet[1:-2]):
            #log.debug("[validate_packet] VALID PACKET!")
            return True

        log.debug("[validate_packet] Not valid packet, CRC failed, may be ongoing and not final 0A")
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
#            log.debug("Checksum was 0xFF, forsing to 0x00")
        #log.info("[calculate_crc] Calculating for: %s     calculated CRC is: %s", toString(msg), toString(bytearray([checksum])))
        return bytearray([checksum])

    def SendCommand(self, message_type, **kwargs):
        """ Add a command to the send List 
            The List is needed to prevent sending messages too quickly normally it requires 500msec between messages """
        self.lock.acquire()
        interval = self.pmTimeFunction() - self.pmLastSentTime
        timeout = (interval > TIMEOUT)

        # command may be set to None on entry
        # Always add the command to the list
        if message_type != None:
            message = pmSendMsg[message_type]
            assert(message != None)
            options = kwargs.get('options', None)
            # need to test if theres an im alive message already in the queue, if not then add it
            self.SendList.append(VisonicListEntry(command = message, options = options))
            log.debug("[QueueMessage] Queueing %s" % (VisonicListEntry(command = message, options = options)))
        
        # This will send commands from the list, oldest first        
        if len(self.SendList) > 0:
            #log.debug("[SendMessage] Start    interval {0}    timeout {1}     pmExpectedResponse {2}     pmWaitingForAckFromPanel {3}".format(interval, timeout, self.pmExpectedResponse, self.pmWaitingForAckFromPanel))
            if not self.pmWaitingForAckFromPanel and interval != None and not timeout: # we are ready to send    and len(self.pmExpectedResponse) == 0
                # check if the last command was sent at least 500 ms ago
                td = timedelta(milliseconds=500)
                ok_to_send = (interval > td) # pmMsgTiming_t[pmTiming].wait)
                #log.debug("ok_to_send {0}    {1}  {2}".format(ok_to_send, interval, td))
                if ok_to_send: 
                    # pop the oldest item from the list, this could be the only item.
                    instruction = self.SendList.pop(0)
                    command = instruction.command
                    self.pmWaitingForAckFromPanel = command.waitforack
                    self.reset_keep_alive_messages()   ## no need to send i'm alive message for a while as we're about to send a command anyway
                    self.pmLastSentTime = self.pmTimeFunction()
                    self.pmLastSentMessage = instruction       
                    if command.replytype != None and command.replytype not in self.pmExpectedResponse:
                        # This message has a specific response associated with it, add it to the list of responses if not already there
                        self.pmExpectedResponse.append(command.replytype)
                        log.debug("[SendMessage]      waiting for message response " + hex(command.replytype))
                    self.pmSendPdu(instruction)
        self.lock.release()
                                                
    def ClearList(self):
        self.lock.acquire()
        """ Clear the List, preventing any retry causing issue. """

        # Clear the List
        log.debug("Setting queue empty")
        self.SendList = []
        #while len(self.SendList) > 0:
        #    try:
        #        self.SendList.get(False)
        #    except Empty:
        #        continue
        #    self.SendList.task_done()
        self.pmLastSentMessage = None
        self.lock.release()
    
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
                log.debug("Options {0} {1} {2}".format(instruction.options, type(s), type(a)))
                for i in (0, len(a)-1):
                    data[s + i] = a[i]
        
        #log.debug('[pmSendPdu] input data: %s', toString(packet))
        # First add preamble (0x0D), then the packet, then crc and postamble (0x0A)
        sData = b'\x0D'
        sData += data
        sData += self.calculate_crc(data)
        sData += b'\x0A'

        #log.debug('[pmSendPdu] Writing data: %s', toString(sData))
        # Log some usefull information in debug mode
        log.info("[pmSendPdu] Sending Command ({0})  at time {1}".format(command.msg, self.pmTimeFunction()))
        self.pmLogPdu(sData, "  PM->")
        self.transport.write(sData)

    def connection_lost(self, exc):
        """Log when connection is closed, if needed call callback."""
        if exc:
            log.exception('disconnected due to exception')
        else:
            log.info('disconnected because of close/abort.')
        if self.disconnect_callback:
            self.disconnect_callback(exc)

    # pmWriteSettings: add a certain setting to the settings table
    def pmWriteSettings(self, page, index, setting):
        settings_len = len(setting)
        wrap = (index + settings_len - 0x100)
        sett = []

        if settings_len > 0xB1:
            log.debug("Write Settings too long")
            return
        if wrap > 0:
            sett.append(setting[0:settings_len - wrap - 1])
            sett.append(setting[settings_len - wrap:settings_len])
            wrap = 1
        else:
            sett.append(setting)
            wrap = 0
        for i in range(0, wrap):
            #if (self.pmRawSettings_t[page + i] == None):
            #    self.pmRawSettings_t[page + i] = string.rep(string.char(0xFF), 0x100)
            settings_len = len(sett[i]) + 1
            if i == 1:
                index = 0 
            self.pmRawSettings_t[page + i] = self.pmRawSettings_t[page + i][1:index] + sett[i] + self.pmRawSettings_t[page + i][index + settings_len:0x100]

    # pmReadSettings
    def pmReadSettings(self, item):
        index = item[0] + 1
        page = item[1]
        settings_len = item[2] + 0x100 * item[3]
        s = ""
        #if (self.pmRawSettings_t[page] != None):
        #    while (settings_len > 0):
        #        str = string.sub(self.pmRawSettings_t[page], index, index + settings_len - 1)
        #        s = s + str
        #        page = page + 1
        #        index = 1
        #        settings_len = settings_len - settings_len(str)
        return s

    def dump_settings(self):
        if pmLogDebug:
            dumpfile = pmSettingsFilename
            outf = open(dumpfile , 'w')
            if outf == None:
                log.debug("Cannot write to debug file.")
                return
            log.debug("Dumping PowerMax settings to file")
            outf.write("PowerMax settings on %s\n", os.date('%Y-%m-%d %H:%M:%S'))
            for i in range(0, 0xFF):
                if pmRawSettings_t[i] != None:
                    for j in range(0, 0x0F):
                        s = ""
                        outf.write(string.format("%08X: ", i * 0x100 + j * 0x10))
                        for k in range (1, 0x10):
                            byte = string.byte(pmRawSettings_t[i], j * 0x10 + k)
                            outf.write(string.format(" %02X", byte))
                            s = (byte < 0x20 or (byte >= 0x80 and byte < 0xA0)) and (s + ".") or (s + string.char(byte))
                        outf.write("  " + s + "\n")
            outf.close()

    def Start_Download(self):
        global DownloadMode
        """ Start download mode """
        #if self.pmStarting == False:
        if not DownloadMode:
            self.pmStarting = True # INTERFACE used in interface to tell user we're starting up
            self.pmWaitingForAckFromPanel = False
            log.info("[Start_Download] Starting download mode")
            self.SendCommand("MSG_DOWNLOAD", options = [4, DownloadCode]) # 
            DownloadMode = True
        else:
            log.debug("[Start_Download] Already in Download Mode (so not doing anything)")
            
    def pmCreateDevice(self, childDevices, s, stype="", zoneName="", zoneTypeName=""):
        log.debug("Trying to create a device ")   # INTERFACE
        return "NOTUSED"
        
    # Find child device (Taken from GE Caddx Panel but comes originally from guessed)
    def findChild(self, deviceId, label):
        #for k, v in pairs(luup.devices) do
        #    if (v.device_num_parent == deviceId and v.id == label) then
        #        return k
        return "";
        
    # ProcessSettings
    def ProcessSettings(self):
        log.info("[ProcessSettings] Process Settings Start")
        # Process settings
        #luup.chdev.sync(pmPanelDev, childDevices)
        if self.pmPowerlinkMode:
            self.SendCommand("MSG_RESTORE") # also gives status
        else:
            self.SendCommand("MSG_STATUS")
        self.pmStarting = False
        log.info("Ready for use")
            
    # pmHandleCommException: we have run into a communication error
    def pmHandleCommException(self, what):
        #outf = open(pmCrashFilename , 'a')
        #if (outf == None):
        #    log.debug("Cannot write to crash file.")
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
        log.info("Reading panel settings")
        # set displayed state in HA to "DOWNLOAD"
        self.SendCommand("MSG_DL", options = [2, pmDownloadItem_t["MSG_DL_PANELFW"]] )     # Request the panel FW
        self.SendCommand("MSG_DL", options = [2, pmDownloadItem_t["MSG_DL_SERIAL"]] )      # Request serial & type (not always sent by default)
        self.SendCommand("MSG_DL", options = [2, pmDownloadItem_t["MSG_DL_ZONESTR"]] )     # Read the names of the zones
#        if syncTime:  # should we sync time between the HA and the Alarm Panel
#            t = datetime.now()
#            if t.year > 2000:
#                year = t.year - 2000
#                timePdu = bytearray(year, t.month, t.day, t.hours, t.minute, t.seconds)
#                pmSyncTimeCheck = t
#                self.SendCommand("MSG_SETTIME", time = timePdu )
#            else:
#                log.debug("Please correct your time.")
        if self.pmPowerMaster:
            self.SendCommand("MSG_DL", options = [2, pmDownloadItem_t["MSG_DL_MR_SIRKEYZON"]] )
        self.SendCommand("MSG_START")      # Start sending all relevant settings please
        self.SendCommand("MSG_EXIT")       # Exit download mode
        
            
class PacketHandling(ProtocolBase):
    """Handle decoding of Visonic packets."""
    
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
        self.reset_powerlink_timeout()
        
        """Handle one raw incoming packet."""
        #log.debug("[handle_packet] Parsing complete valid packet: %s", toString(packet))

        if packet[1:2] == b'\x02': # ACK
            self.handle_msgtype02(packet[2:-2])  # remove the header and command bytes as the start. remove the footer and the checksum at the end
        elif packet[1:2] == b'\x06': # Timeout
            self.handle_msgtype06(packet[2:-2])
        elif packet[1:2] == b'\x08': # Access Denied
            self.handle_msgtype08(packet[2:-2])
        elif packet[1:2] == b'\x0B': # Stopped
            log.debug("[handle_packet] Stopped")
        elif packet[1:2] == b'\x25': # Download retry
            self.handle_msgtype25(packet[2:-2])
        elif packet[1:2] == b'\x33': # Settings send after a MSGV_START
            self.handle_msgtype33(packet[2:-2])
        elif packet[1:2] == b'\x3c': # Message when start the download
            self.handle_msgtype3C(packet[2:-2])
        elif packet[1:2] == b'\x3f': # Download information
            self.handle_msgtype3F(packet[2:-2])
        elif packet[1:2] == b'\xa0': # Event log
            self.handle_msgtypeA0(packet[2:-2])
        elif packet[1:2] == b'\xa5': # General Event
            self.handle_msgtypeA5(packet[2:-2])
        elif packet[1:2] == b'\xa7': # General Event
            self.handle_msgtypeA7(packet[2:-2])
        elif packet[1:2] == b'\xab': # PowerLink Event
            self.handle_msgtypeAB(packet[2:-2])
        else:
            log.info("[handle_packet] Unknown/Unhandled packet type {0}".format(packet[1:2]))
        # clear our buffer again so we can receive a new packet.
        self.ReceiveData = bytearray(b'')
            
    def displayzonebin(self, bits):
        """ Display Zones in reverse binary format
          Zones are from e.g. 1-8, but it is stored in 87654321 order in binary format """
        v = bits[0:1]
        log.debug("v = {0}   type(v) = {1}".format(v, type(v)))
        return bin(v)
        #return bin(bits)

    def handle_msgtype02(self, data): # ACK
        global DownloadMode
        #log.debug("[handle_msgtype02] Acknowledgement")
        
        self.pmWaitingForAckFromPanel = False
        # Check if the last message is Download Exit, if this is the case, process the settings
        if self.pmLastSentMessage != None:
            lastCommandData = self.pmLastSentMessage.command.data        
            if lastCommandData[0] == b'\x0F':                  
                DownloadMode = False
                log.info("We're in powerlink mode *********************************************************************")
                self.pmPowerlinkMode = True  # INTERFACE set State to "PowerLink"
                # We received a download exit message, restart timer
                self.reset_powerlink_timeout()
                self.ProcessSettings()
#            elif len(lastCommandData) >= 3 and lastCommandData[0] == b'\xAB' and lastCommandData[1] == b'\x0A' and lastCommandData[2] == b'\x00' and lastCommandData[3] == b'\x00':  ## ENROLL
#                # The List timer
#                try:
#                    self.tListDelay.cancel()
#                except:
#                    pass
#                self.tListDelay = threading.Timer(self.isenddelay, self.tListDelayExpired)
#                self.tListDelay.start()

    def handle_msgtype06(self, data): 
        global DownloadMode
        """ MsgType=06 - Time out
        Timeout message from the PM, most likely we are/were in download mode """
        log.info("[handle_msgtype06] Timeout Received")
        if DownloadMode:
            DownloadMode = False
            self.SendCommand("MSG_EXIT")
        else:
            self.pmSendAck()

    def handle_msgtype08(self, data):
        log.info("[handle_msgtype08] Access Denied")
        # If LastMsgType And &HF0 = &HA0 Then
        #   Wrong pin  enroll, eventlog, arm, bypass
        # Endif
                
        # If LastMsgType And &HF0 = &H24 Then
        #   Wrong download pin
        # Endif


    def handle_msgtype25(self, data): # Download retry
        """ MsgType=25 - Download retry
        Unit is not ready to enter download mode
        """
        # Format: <MsgType> <?> <?> <delay in sec>
            
        iDelay = self.ReceiveData[4]

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

        log.info("[handle_msgtype33] Getting Data")
        
        if len(self.ReceiveData) != 14:
            log.debug("[handle_msgtype33] ERROR: MSGTYPE=0x33 Expected=11, Received={0}".format(len(self.ReceiveData)))
            log.debug("                           " + toString(self.ReceiveData))
            return
                
        # Format is: <MsgType> <index> <page> <data 8x bytes>
        # Extract Page and Index information
        iIndex = self.ReceiveData[2]
        iPage = self.ReceiveData[3]
                        
        # Write to memory map structure, but remove the first 4 bytes from the data
        self.pmWriteSettings(iPage, iIndex, self.ReceiveData[4:11])
                            
    def handle_msgtype3C(self, data): # Panel Info Messsage when start the download
        """ The panel information is in 5 & 6
           6=PanelType e.g. PowerMax, PowerMaster
           5=Sub model type of the panel - just informational
           """
           
        self.PanelType = self.ReceiveData[6]
        self.ModelType = self.ReceiveData[5]
        
        PowerMaster = (self.PanelType >= 7)
          
        log.info("[handle_msgtype3C] PanelType={0}, Model={1}".format(self.PanelType, self.ModelType))
            
        # We got a first response, now we can continue enrollment the PowerMax/Master PowerLink
        self.pmPowerlinkEnrolled()

    def handle_msgtype3F(self, data):
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
        log.info("[handle_msgtype3F]")
                          
        # Write to memory map structure, but remove the first 4 bytes (3F/index/page/length) from the data
#FIXME
        self.pmWriteSettings(iPage, iIndex, self.ReceiveData[4:])

    def handle_msgtypeA0(self, data):
        """ MsgType=A0 - Event Log """
#FIXME entire def
#        Dim iSec, iMin, iHour, iDay, iMonth, iYear As Integer
#        Dim iEventZone As Integer
#        Dim iLogEvent As Integer
#        Dim sEventZone As String
#        Dim sLogEvent As String
#        Dim sDate As String
#        Dim dDate As Date
        log.info("[handle_MsgTypeA0]")
              
        # Check for the first entry, it only contains the number of events
        if self.ReceiveData[2] == 0x01:
            log.debug("[handle_msgtypeA0] Eventlog received")
                      
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
                
    # captured examples of A5 data
    #     0d a5 00 04 00 61 03 05 00 05 00 00 43 a4 0a
    def handle_msgtypeA5(self, data): # Status Message
        log.info("[handle_msgtypeA5]")
        packet = self.ReceiveData
        log.debug("[handle_msgtypeA5] Parsing A5 packet")
        if packet[3:4] == b'\x01': # Log event print
            log.debug("[handle_msgtypeA5] Log Event Print")
        elif packet[3:4] == b'\x02': # Status message zones
            log.debug("[handle_msgtypeA5] Status and Battery Message")
            log.debug("[handle_msgtypeA5] Status Zones 01-08: %s ", self.displayzonebin(packet[4:5]))
            log.debug("[handle_msgtypeA5] Status Zones 09-16: %s ", self.displayzonebin(packet[5:6]))
            log.debug("[handle_msgtypeA5] Status Zones 17-24: %s ", self.displayzonebin(packet[6:7]))
            log.debug("[handle_msgtypeA5] Status Zones 25-30: %s ", self.displayzonebin(packet[7:8]))
            log.debug("[handle_msgtypeA5] Battery Zones 01-08: %s ", self.displayzonebin(packet[8:9]))
            log.debug("[handle_msgtypeA5] Battery Zones 09-16: %s ", self.displayzonebin(packet[9:10]))
            log.debug("[handle_msgtypeA5] Battery Zones 17-24: %s ", self.displayzonebin(packet[10:11]))
            log.debug("[handle_msgtypeA5] Battery Zones 25-30: %s ", self.displayzonebin(packet[11:12]))
        elif packet[3:4] == b'\x03': # Tamper Event
            log.debug("Tamper event")
            log.debug("[handle_msgtypeA5] Status Zones 01-08: %s ", self.displayzonebin(packet[4:5]))
            log.debug("[handle_msgtypeA5] Status Zones 09-16: %s ", self.displayzonebin(packet[5:6]))
            log.debug("[handle_msgtypeA5] Status Zones 17-24: %s ", self.displayzonebin(packet[6:7]))
            log.debug("[handle_msgtypeA5] Status Zones 25-30: %s ", self.displayzonebin(packet[7:8]))
            log.debug("[handle_msgtypeA5] Tamper Zones 01-08: %s ", self.displayzonebin(packet[8:9]))
            log.debug("[handle_msgtypeA5] Tamper Zones 09-16: %s ", self.displayzonebin(packet[9:10]))
            log.debug("[handle_msgtypeA5] Tamper Zones 17-24: %s ", self.displayzonebin(packet[10:11]))
            log.debug("[handle_msgtypeA5] Tamper Zones 25-30: %s ", self.displayzonebin(packet[11:12]))
        elif packet[3:4] == b'\x04': # Zone event
            slog = pmDetailedArmMode_t[int(packet[4])]
            if packet[4:5] == b'\x03' or packet[4:5] == b'\x04' or packet[4:5] == b'\x05'or packet[4:5] == b'\x0A' or packet[4:5] == b'\x0B' or packet[4:5] == b'\x14' or packet[4:5] == b'\x15':
                sarm = "Armed"
            elif packet[4:5] > b'\x15':
                log.debug("Unknown state, assuming Disarmed")
                sarm = "Disarmed"
            else:
                sarm = "Disarmed"
            
            if (packet[4] & 64):
                log.debug("[handle_msgtypeA5] Bit 6 set, Status Changed")
            if (packet[4] & 128): 
                log.debug("[handle_msgtypeA5] Bit 7 set, Alarm Event")
                
            log.debug("[handle_msgtypeA5] log: {0}, arm: {1}".format(slog, sarm))
#FIXME
            #log.debug("[handle_msgtypeA5] Zone Event {0}".format(sLog))
      
#            iDeviceId = Devices.Find(Instance, "Panel", InterfaceId, IIf($bPowerMaster, "Visonic PowerMaster", "Visonic PowerMax"))
#            if iDeviceId:
#                Devices.ValueUpdate(iDeviceId, 1, sArm)
#                Devices.ValueUpdate(iDeviceId, 2, sLog)
                                          
            if (packet[5] & 1):
                log.debug("[handle_msgtypeA5] Bit 0 set, Ready")
            if (packet[5] & 2):
                log.debug("[handle_msgtypeA5] Bit 1 set, Alert in Memory")
            if (packet[5] & 4):
                log.debug("[handle_msgtypeA5] Bit 2 set, Trouble!")
            if (packet[5] & 8):
                log.debug("[handle_msgtypeA5] Bit 3 set, Bypass")
            if (packet[5] & 16):
                log.debug("[handle_msgtypeA5] Bit 4 set, Last 10 seconds of entry/exit")
            if (packet[5] & 32):
                sEventLog = pmEventType_t["EN"][int(packet[7])]
                log.debug("[handle_msgtypeA5] Bit 5 set, Zone Event")
                log.debug("[handle_msgtypeA5]       Zone: {0}, {1}".format(packet[6], sEventLog))

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
        elif packet[3:4] == b'\x06': # Status message enrolled/bypassed
            log.debug("[handle_msgtypeA5] Enrollment and Bypass Message")
            log.debug("Enrolled Zones 01-08: " & self.displayzonebin(packet[4:5]))
            log.debug("Enrolled Zones 09-16: " & self.displayzonebin(packet[5:6]))
            log.debug("Enrolled Zones 17-24: " & self.displayzonebin(packet[6:7]))
            log.debug("Enrolled Zones 25-30: " & self.displayzonebin(packet[7:8]))
            log.debug("Bypassed Zones 01-08: " & self.displayzonebin(packet[8:9]))
            log.debug("Bypassed Zones 09-16: " & self.displayzonebin(packet[9:10]))
            log.debug("Bypassed Zones 17-24: " & self.displayzonebin(packet[10:11]))
            log.debug("Bypassed Zones 25-30: " & self.displayzonebin(packet[11:12]))
        else:
            log.debug("[handle_msgtypeA5] Unknown A5 Event: %s", toString(packet[3:4]))

    def handle_msgtypeA7(self, data):
        """ MsgType=A7 - Panel Status Change """
        log.info("[handle_msgtypeA7] Panel Status Change " + toString(data))
        #log.debug("Zone/User: " & DisplayZoneUser(packet[3:4]))
        #log.debug("Log Event: " & DisplayLogEvent(packet[4:5]))
        
        # 01 00 00 60 00 ff 00 35 00 00 43 7e        
        msgCnt = int(data[0])
        log.info("     A7 message contains {0} messages".format(msgCnt))
        for i in range(1, msgCnt+1):
            eventZone = int(data[2 * i])
            logEvent  = int(data[1 + (2 * i)])
            eventType = int(logEvent & 0x7F)
            s = (pmLogEvent_t[pmLang][eventType + 1] or "UNKNOWN") + " / " + (pmLogUser_t[eventZone + 1] or "UNKNOWN")
            alarmStatus = "None"
            if eventType in pmPanelAlarmType_t:
                alarmStatus = pmPanelAlarmType_t[eventType]
            troubleStatus = "None"
            if eventType in pmPanelTroubleType_t:
                troubleStatus = pmPanelTroubleType_t[eventType]
            log.info("               System message: " + s + "    alarmStatus " + alarmStatus + "    troubleStatus " + troubleStatus)
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
            #    pmStartDownload()
                
    # pmHandlePowerlink (0xAB)
    def handle_msgtypeAB(self, data): # PowerLink Message
        log.info("[handle_msgtypeAB]")
        global DownloadMode
        # Restart the timer
        self.reset_powerlink_timeout()

        subType = self.ReceiveData[2]
        if subType == 3: # keepalive message
            log.debug("[handle_msgtypeAB] PowerLink Keep-Alive ({0})".format(toString(self.ReceiveData[4:6])))
            DownloadMode = False
            self.pmSendAck()
            #pmLastKeepAlive = self.pmTimeFunction()
            #pmPowerlinkRetry = 0
            # return true, true -- ignore alive message for expected response (never expected)
        elif subType == 5: # -- phone message
            action = self.ReceiveData[4]
            if action == 1:
                log.debug("PowerLink Phone: Calling User")
                #pmMessage("Calling user " + pmUserCalling + " (" + pmPhoneNr_t[pmUserCalling] +  ").", 2)
                #pmUserCalling = pmUserCalling + 1
                #if (pmUserCalling > pmPhoneNr_t) then
                #    pmUserCalling = 1
            elif action == 2:
                log.debug("PowerLink Phone: User Acknowledged")
                #pmMessage("User " .. pmUserCalling .. " acknowledged by phone.", 2)
                #pmUserCalling = 1
            else:
                log.debug("PowerLink Phone: Unknown Action {0}".format(hex(self.ReceiveData[3])))
            pmSendAck(0x02)
        elif (subType == 10 and self.ReceiveData[4] == 0):
            log.info("PowerLink telling us what the code is for downloads")
            DownloadCode[0] = self.ReceiveData[5]
            DownloadCode[1] = self.ReceiveData[6]
        elif (subType == 10 and self.ReceiveData[4] == 1):
            log.debug("PowerLink most likely wants to auto-enroll, only doing auto enroll once")
            DownloadMode = False
            self.SendMsg_ENROLL()
            if 0xAB not in self.pmExpectedResponse:
                self.pmExpectedResponse.append(0xAB)

                    
    def handle_msgtypeAB_old(self, data): # PowerLink Message
        global DownloadMode
        """ MsgType=AB - PowerLink message """
        
        if self.ReceiveData[2] == 3: # PowerLink keep-alive
            log.debug("[handle_msgtypeAB] PowerLink Keep-Alive ({0})".format(ByteToString(self.ReceiveData[6:4])))
              
            DownloadMode = False
              
            # We received a keep-alive, restart timer
            self.reset_powerlink_timeout()
                                                        
        elif self.ReceiveData[2] == 5: # Phone message
            if self.ReceiveData[4] == 1:
                log.debug("PowerLink Phone: Calling User")
            elif self.ReceiveData[4] == 2:
                log.debug("PowerLink Phone: User Acknowledged")
            else:
                log.debug("PowerLink Phone: Unknown Action {0}".format(hex(self.ReceiveData[3])))

        elif self.ReceiveData[2] == 10: # PowerLink most likely wants to auto-enroll
            log.debug("PowerLink most likely wants to auto-enroll")
            if self.ReceiveData[4] == 1:
                # Send the auto-enroll message with the new download pin/code
                DownloadMode = False
                self.SendMsg_ENROLL()
                                                                                                                                                        
                # Restart the timer
                self.reset_powerlink_timeout()
        else:
            log.debug("Unknown AB Message: {0}".format(hex(self.ReceiveData[2])))

    def SendMsg_ENROLL(self):
        """ Auto enroll the PowerMax/Master unit """
        global DownloadMode
        log.info("[SendMsg_ENROLL]  download pin will be " + toString(DownloadCode))
        # Remove anything else from the List, we need to restart
        self.pmExpectedResponse = []
        # Clear the list
        self.ClearList()
        # Send the request, the pin will be added when the command is send
        self.SendCommand("MSG_REENROLL",  options = [4, DownloadCode])
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
