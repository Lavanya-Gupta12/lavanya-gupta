## 01 The visit: The technician has one hour at one station next week. Which station, what exactly do they do,and what do they check first? Why that station and not the second-best one?

The technician has one hour at one station, so I would send them to S04
(Foothills Community Garden).

S04 has two problems. First, on sunny afternoons its temperature reads too warm
compared with the other stations, and the gap keeps growing week by week. At night
it is normal. That points to the radiation shield not letting air flow through,
so the sun heats the sensor. The likely cause is the repaint on 15 March — paint may
be blocking the shield slats. Second, the rain gauge was blocked by leaves. It missed
the 29–30 March storm and then dumped 33 mm when it was cleaned.

What they should do, in order:
1. First, look at the shield slats for paint or blockage. This tests the main suspect.
2. Hold a hand thermometer in the shade next to the shield and note both readings.
   This proves whether the station really reads too warm.
3. Clean or replace the shield.
4. Fit a leaf guard on the rain gauge, pour in a measured cup of water and count the
   tips to check it works.

Why not S03, the runner-up? Its temperature sensor is also failing, but in an obvious
way — values like -40 and 85 °C and a reading stuck at 6.5 °C for 80 hours. check.py
catches and removes these automatically, and only about 3.5 days are lost. A volunteer
can check the sensor's plug. S04's error is worse because it is quiet: the numbers look
believable, so they would sneak into the climate summary as fake warm afternoons, and
software cannot fix them.

The other stations do not need a visit: S06 needs its clock setting changed, S07 its
wind unit setting, S02 a calibration kit, S08 a SIM top-up, and S05's humidity fixed
itself.