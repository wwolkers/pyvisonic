# pyvisonic
Python module for connecting to Visonic PowerMax / PowerMaster

This software is still in Alpha development and does not have any version numbers. 
    All releases are currently version 0.0.1 and will remain so until Beta testing

What has changed since the last release

3rd June 2018 at 00:20 - commit
Implemented ForceStandard and tested
    In doing this, the code gets the zone names OK but when I tried getting zone types I'm not sure what it's sending me
    See code comments in handle_msgtypeA6
Implemented going in to Standard mode when powerlink fails (after 4 tries)
Initial Integration in to Home Assistant:
    It always times out and goes in to Standard Mode 
        i.e. Powerlink gets multiple Access Denied messages from panel and I don't know why
    HA shows sensors in the main web page. I have not included HA code yet as it's still being developed.
I started using PyCharm for my development and it "fixed" some of the spacing and layout for me.
    It also showed me some of my bad python programming such as "if AAA == None:"  should be "if AAA is None:"
With the problems of integrating in to HA and trying to get Powerlink working, I wondered if it's a timing issue
    As in general, there seem to be lots of time critical exchanges with the panel
    So I implemented a tasking interface as well as an asyncio interface. Still didn't make it work but I left the code in.
    This commited version uses the tasking interface. It creates a task and then creates an asyncio construct inside the task
Getting the panel event log works, the call is there as a test on line 1860, uncomment it then look for handle_MsgTypeA0 in the log (only works when it gets in to powerlink mode)

