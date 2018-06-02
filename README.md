# pyvisonic
Python module for connecting to Visonic PowerMax / PowerMaster

## Release
This software is still in pre Alpha development and does not have any version numbers. All releases are currently version 0.0.1 and will remain so until Alpha testing

We are not taking any comments or requests or anything else really at the moment but you can log an issue, perhaps with your "log.txt" from a debug run output and we'll take a look if we get the chance.

## Instructions and what works so far
It's easy to run. 
- For a TCP/IP connection, edit test.py and 
    - Alter the address and port to your settings. 
- For a USB (RS232) connection, edit test.py and 
    - Comment out the tcp connection and 
    - Uncomment the usb connection. 
    - Also change useTask to False.
    - Change the usb port to your settings
- From the command prompt run "python3 test.py" and hopefully watch it connect in powerlink! 

It tries to connect in Powerlink mode by default (unless you set ForceStandard to True in test.py).

Set PluginDebug to True or False to output more or less text on your display window

### From the command prompt, linux terminal or from within PyCharm (on windows)
When I run it it usually connects in powerlink mode. If it doesn't and you want it to then, in the following order:
- Wait 5 minutes and try it again
- If that doesn't work then reset your panel (enter installer mode on the panel and "OK" back out)

### Running it in Home Assistant
I have also developed the HA python code to create the sensors from a callback. 
When I run it in Home Assistant and examine the HA log file, I keep getting "Access Denied" after each Enroll attempt. It then defaults to Standard Mode after 4 attempts.

I am not releasing this code yet, but I will when it's more robust!

    
## What has changed since the last release

3rd June 2018 at 00:20
1. Implemented ForceStandard and tested
    - In doing this, the code gets the zone names OK but when I tried getting zone types I'm not sure what it's sending me
    - See code comments in handle_msgtypeA6
2. Implemented going in to Standard mode when powerlink fails (after 4 tries)
3. Initial Integration in to Home Assistant:
    - It always times out and goes in to Standard Mode 
        - i.e. Powerlink gets multiple Access Denied messages from panel and I don't know why
    - HA shows sensors in the main web page. I have not included HA code yet as it's still being developed.
4. I started using PyCharm for my development and it "fixed" some of the spacing and layout for me.
    - It also showed me some of my bad python programming such as "if AAA == None:"  should be "if AAA is None:"
5. With the problems of integrating in to HA and trying to get Powerlink working, I wondered if it's a timing issue
    - As in general, there seem to be lots of time critical exchanges with the panel
    - So I implemented a tasking interface as well as an asyncio interface. Still didn't make it work but I left the code in.
    - This commited version uses the tasking interface. It creates a task and then creates an asyncio construct inside the task
6. Getting the panel event log works, the call is there as a test on line 1860, uncomment it then look for handle_MsgTypeA0 in the log (only works when it gets in to powerlink mode)



